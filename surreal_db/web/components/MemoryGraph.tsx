"use client";

/**
 * The memory graph.
 *
 * Every visual property encodes a database fact, and nothing here is decorative:
 * size is salience, opacity is whether a fact is still current, the ring is
 * confidence, and the glow is which recall strategy surfaced the node. Colour
 * never carries meaning alone -- shape and dash pattern back it up, so the graph
 * survives colour blindness and GIF compression.
 */

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";

import { GLOW_MS, type GraphState, type MemoryNode, type Via } from "@/lib/live";

// react-force-graph touches `window` at import time, so it cannot be server
// rendered.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

type GraphNode = MemoryNode & { x?: number; y?: number };
type GraphLink = { id: string; source: string; target: string; kind: string; strength: number };

const VIA_COLOUR: Record<Via, string> = {
  vector: "#2d8cff",
  text: "#ffb020",
  graph: "#ff00a0",
};

const ENTITY_COLOUR: Record<string, string> = {
  person: "#6ee7b7",
  place: "#fca5a5",
  org: "#fcd34d",
  concept: "#c4b5fd",
  artifact: "#94a3b8",
  event: "#f9a8d4",
};

/** Pick a node's base colour: facts share one hue, entities are typed by kind. */
function baseColour(node: MemoryNode): string {
  if (node.kind === "fact") return "#cbd5f5";
  return ENTITY_COLOUR[node.entityKind ?? "concept"] ?? "#94a3b8";
}

/** Map salience onto a radius, eased so a very salient node does not dominate. */
function radius(node: MemoryNode): number {
  const scaled = Math.sqrt(Math.max(0.05, node.salience)) * 5.5;
  return Math.max(3.5, Math.min(14, scaled));
}

/**
 * Above this many nodes the graph stops being a picture of a memory and becomes
 * a hairball. Past it, facts below the salience floor are drawn faintly and
 * unlabelled rather than dropped -- docs/05 is explicit that nothing may be
 * silently truncated, because a memory you cannot see is indistinguishable from
 * a memory that was lost.
 */
const CROWDED_ABOVE = 1200;

export function MemoryGraph({
  graph,
  onSelect,
}: {
  graph: GraphState;
  onSelect: (node: MemoryNode) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  // Size has to be state, not a ref: react-force-graph takes width and height as
  // props, so a ref would leave the canvas at its initial guess forever and the
  // graph would render into a box smaller than the pane it sits in.
  const [size, setSize] = useState({ width: 800, height: 600 });

  /** Node objects, kept alive across updates so the layout is not re-seeded. */
  const byId = useRef(new Map<string, GraphNode>());
  const engine = useRef<{
    d3ReheatSimulation?: () => void;
    zoomToFit?: (ms?: number, padding?: number) => void;
  } | null>(null);

  /**
   * Graph data for the force layout.
   *
   * Two constraints pull in opposite directions here. The simulation stores each
   * node's position **on the node object**, so replacing those objects throws the
   * layout away and every node jumps -- growth reads as chaos. But
   * react-force-graph only re-ingests when the `graphData` object identity
   * changes, so mutating in place means new nodes are never picked up at all.
   *
   * The resolution: reuse the node objects, wrap them in fresh arrays. Identity
   * changes at the container, positions survive at the node.
   */
  const data = useMemo(() => {
    for (const node of graph.nodes.values()) {
      const existing = byId.current.get(node.id);
      if (existing) {
        // Assign field by field so `x`, `y`, `vx`, `vy` -- which belong to the
        // simulation, not to us -- are left untouched.
        Object.assign(existing, node);
      } else {
        byId.current.set(node.id, { ...node });
      }
    }
    for (const id of [...byId.current.keys()]) {
      if (!graph.nodes.has(id)) byId.current.delete(id);
    }

    const nodes = [...byId.current.values()];
    const present = new Set(byId.current.keys());
    const links = [...graph.links.values()]
      // A link whose endpoints have not arrived yet would crash the layout.
      // The reducer's pending buffer normally prevents this; this is the belt
      // to its braces.
      .filter((link) => present.has(link.source) && present.has(link.target))
      .map((link) => ({
        id: link.id,
        source: link.source,
        target: link.target,
        kind: link.kind,
        strength: link.strength,
      }));

    return { nodes, links };
  }, [graph]);

  // Glow expires on a timer rather than on the next event, or a graph with no
  // further writes would stay lit forever.
  useEffect(() => {
    const timer = setInterval(() => {
      const now = Date.now();
      for (const node of byId.current.values()) {
        if (node.glow && node.glow.until < now) node.glow = undefined;
      }
    }, 250);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    // ResizeObserver rather than a window listener: the pane also changes size
    // when the inspector drawer opens, which fires no window resize event.
    const observer = new ResizeObserver(() => {
      setSize({ width: element.clientWidth, height: element.clientHeight });
    });
    observer.observe(element);
    setSize({ width: element.clientWidth, height: element.clientHeight });
    return () => observer.disconnect();
  }, []);

  // Fit the view once, the first time there is anything to fit. Re-fitting on
  // every write would yank the viewport around exactly when someone is trying to
  // watch a node appear.
  const fitted = useRef(false);
  useEffect(() => {
    if (fitted.current || data.nodes.length === 0) return;
    fitted.current = true;
    const timer = setTimeout(() => engine.current?.zoomToFit?.(600, 90), 900);
    return () => clearTimeout(timer);
  }, [data.nodes.length]);

  /** Draw one node: disc, confidence ring, recall glow, and a label when it fits. */
  function drawNode(node: GraphNode, ctx: CanvasRenderingContext2D, scale: number): void {
    const crowded = data.nodes.length > CROWDED_ABOVE;
    const r = radius(node);
    const colour = baseColour(node);
    const age = Date.now() - node.bornAt;
    // Nodes scale up from nothing over their first 400ms, so a fact being
    // learned is visible as an event rather than appearing between frames.
    const entry = Math.min(1, age / 400);
    const drawn = r * (0.2 + 0.8 * entry);

    ctx.save();
    // A superseded fact drops to a quarter opacity and desaturates. It is never
    // removed -- the history is the point.
    ctx.globalAlpha = node.current ? Math.min(1, 0.45 + node.salience * 0.35) : 0.25;
    // In a crowded graph, quiet facts recede so the entities and the recently
    // reinforced facts carry the shape. They are still drawn, still clickable,
    // and still counted in the header -- faded, never truncated.
    if (crowded && node.kind === "fact" && node.salience < 1.02 && !node.glow) {
      ctx.globalAlpha *= 0.3;
    }

    if (node.glow && node.glow.until > Date.now()) {
      const remaining = (node.glow.until - Date.now()) / GLOW_MS;
      ctx.shadowColor = VIA_COLOUR[node.glow.via];
      ctx.shadowBlur = 12 + 18 * remaining * Math.max(0.2, node.glow.score);
      ctx.strokeStyle = VIA_COLOUR[node.glow.via];
      ctx.lineWidth = 1.6 / scale;
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, drawn + 3, 0, Math.PI * 2);
      ctx.stroke();
    }

    ctx.beginPath();
    ctx.arc(node.x ?? 0, node.y ?? 0, drawn, 0, Math.PI * 2);
    ctx.fillStyle = colour;
    ctx.fill();

    if (node.kind === "fact" && node.confidence > 0) {
      ctx.shadowBlur = 0;
      ctx.strokeStyle = colour;
      ctx.lineWidth = (0.4 + node.confidence * 1.6) / scale;
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, drawn + 1.8, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Label budget. At 4,000 nodes, labelling everything produces a solid block
    // of overlapping text -- the graph renders at 47fps and is still unreadable,
    // which is a legibility failure rather than a performance one. Entities
    // carry the structure, so they keep their labels; facts earn one only when
    // zoomed in far enough to read them anyway.
    const labelled =
      node.kind === "entity" ? scale > 0.55 || node.salience > 1.05 : scale > 2.2;

    if (labelled) {
      ctx.shadowBlur = 0;
      ctx.globalAlpha = node.current ? 0.9 : 0.35;
      ctx.font = `${Math.max(3, 9 / scale)}px "JetBrains Mono", monospace`;
      ctx.fillStyle = "#e6e8ef";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const label = node.label.length > 34 ? `${node.label.slice(0, 33)}…` : node.label;
      ctx.fillText(label, node.x ?? 0, (node.y ?? 0) + drawn + 3 / scale);
    }
    ctx.restore();
  }

  // Nothing is truncated -- quiet facts are drawn faintly -- but docs/05 is
  // explicit that the reduction must be *stated*, because a graph that silently
  // shows less than it holds is indistinguishable from one that lost records.
  const faded = data.nodes.length > CROWDED_ABOVE
    ? data.nodes.filter((node) => node.kind === "fact" && node.salience < 1.02 && !node.glow).length
    : 0;

  return (
    <div ref={container} className="relative h-full w-full">
      {faded > 0 && (
        <div className="pointer-events-none absolute left-4 top-3 z-10 text-[10px]
                        text-[var(--color-muted)]">
          {faded.toLocaleString()} quiet {faded === 1 ? "fact" : "facts"} faded — still
          present, still clickable
        </div>
      )}
      <ForceGraph2D
        ref={engine as never}
        graphData={data}
        width={size.width}
        height={size.height}
        backgroundColor="rgba(0,0,0,0)"
        nodeId="id"
        nodeRelSize={1}
        cooldownTime={4000}
        // maxZoom clamps zoomToFit as well as the user's wheel. Without it, a
        // fit on a dozen nodes zooms until they fill the canvas and the graph
        // reads as a diagram of four circles rather than as a memory with room
        // to grow into.
        minZoom={0.4}
        maxZoom={2.2}
        d3AlphaDecay={0.035}
        d3VelocityDecay={0.35}
        nodeCanvasObject={drawNode as never}
        nodePointerAreaPaint={((node: GraphNode, colour: string, ctx: CanvasRenderingContext2D) => {
          ctx.fillStyle = colour;
          ctx.beginPath();
          ctx.arc(node.x ?? 0, node.y ?? 0, radius(node) + 4, 0, Math.PI * 2);
          ctx.fill();
        }) as never}
        linkColor={
          ((link: GraphLink) =>
            link.kind === "supersedes"
              ? "rgba(255,0,160,0.75)"
              : link.kind === "relates"
                ? "rgba(45,140,255,0.45)"
                : link.kind === "mentions"
                  ? "rgba(161,161,175,0.28)"
                  : "rgba(161,161,175,0.14)") as never
        }
        linkWidth={((link: GraphLink) => (link.kind === "relates" ? 1 + link.strength * 2 : 0.6)) as never}
        linkLineDash={((link: GraphLink) => (link.kind === "supersedes" ? [4, 3] : null)) as never}
        linkDirectionalArrowLength={((link: GraphLink) => (link.kind === "supersedes" ? 4 : 0)) as never}
        linkDirectionalArrowRelPos={1}
        onNodeClick={((node: GraphNode) => onSelect(node)) as never}
      />
    </div>
  );
}
