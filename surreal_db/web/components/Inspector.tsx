"use client";

/**
 * The query inspector.
 *
 * Shows the exact SurrealQL the database ran, read straight from the `retrieval`
 * record -- not a reconstruction of it. Demos that hide their queries get read as
 * marketing, so this one hands the query over with a copy button and expects it
 * to be pasted into Surrealist and run against someone else's data.
 */

import { useState } from "react";

import type { GraphState, Retrieval, Via } from "@/lib/live";

const VIA_COLOUR: Record<Via, string> = {
  vector: "#2d8cff",
  text: "#ffb020",
  graph: "#ff00a0",
};

const VIA_LABEL: Record<Via, string> = {
  vector: "vector",
  text: "bm25",
  graph: "graph",
};

export function Inspector({
  graph,
  retrieval,
  onFocus,
}: {
  graph: GraphState;
  retrieval: Retrieval | null;
  onFocus: (factId: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const [copied, setCopied] = useState(false);

  if (!retrieval) {
    return (
      <div className="border-t border-[var(--color-border)] px-6 py-3 text-[11px] text-[var(--color-muted)]">
        inspector · waiting for a recall
      </div>
    );
  }

  const total = retrieval.timing?.total_ms;

  /** Copy the query so a reader can run it themselves. */
  async function copy() {
    await navigator.clipboard.writeText(retrieval!.surql);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="border-t border-[var(--color-border)]">
      <button
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between px-6 py-2 text-[11px]"
      >
        <span className="text-[var(--color-muted)]">
          <span className="mr-2">{open ? "▾" : "▸"}</span>
          inspector · {retrieval.id.split(":")[1]?.slice(0, 6)} · &ldquo;{retrieval.query}&rdquo;
        </span>
        <span className="text-[var(--color-muted)]">
          {typeof total === "number" ? `${total}ms · ` : ""}
          {retrieval.strategy}
        </span>
      </button>

      {open && (
        <div className="max-h-52 overflow-y-auto px-6 pb-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-widest text-[var(--color-muted)]">
              the query that ran
            </span>
            <button
              onClick={copy}
              className="rounded-[var(--radius-control)] border border-[var(--color-border)] px-2 py-0.5 text-[10px] text-[var(--color-muted)]"
            >
              {copied ? "copied" : "copy query"}
            </button>
          </div>
          <pre className="surql rounded-[var(--radius-control)] bg-black/35 p-3 text-[var(--color-muted)]">
            {retrieval.surql}
          </pre>

          <div className="mt-3 flex flex-wrap gap-2">
            {retrieval.hits.map((hit, index) => {
              const node = graph.nodes.get(hit.fact);
              return (
                <button
                  key={`${hit.fact}-${index}`}
                  onClick={() => onFocus(hit.fact)}
                  className="rounded-[var(--radius-control)] border px-2 py-1 text-left text-[10px]"
                  style={{ borderColor: VIA_COLOUR[hit.via] }}
                  title={node?.label ?? hit.fact}
                >
                  <span style={{ color: VIA_COLOUR[hit.via] }}>{VIA_LABEL[hit.via]}</span>
                  <span className="text-[var(--color-muted)]"> · {hit.score.toFixed(2)} · </span>
                  <span>
                    {(node?.label ?? hit.fact).slice(0, 44)}
                    {(node?.label ?? "").length > 44 ? "…" : ""}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
