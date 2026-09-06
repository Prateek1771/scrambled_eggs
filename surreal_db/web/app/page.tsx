"use client";

/**
 * The page.
 *
 * Chat left, memory right, the query that produced the last recall along the
 * bottom.
 *
 * The two panes are fed by two independent connections and that is the whole
 * point: the chat streams from the API, the graph is pushed by SurrealDB
 * straight to the browser. The graph is usually several nodes ahead of the
 * sentence being written about it. That desynchronisation is not a bug — it is
 * the demo.
 */

import { useMemo, useState } from "react";

import { Chat } from "@/components/Chat";
import { Header } from "@/components/Header";
import { Inspector } from "@/components/Inspector";
import { MemoryGraph } from "@/components/MemoryGraph";
import type { MemoryNode } from "@/lib/live";
import { useLiveMemory } from "@/lib/useLiveMemory";

const SURREAL_URL = process.env.NEXT_PUBLIC_SURREAL_URL ?? "ws://localhost:8000/rpc";
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

export default function Page() {
  const { graph, status, error } = useLiveMemory(SURREAL_URL, API_URL);
  const [selected, setSelected] = useState<string | null>(null);

  const counts = useMemo(() => {
    let facts = 0;
    let entities = 0;
    for (const node of graph.nodes.values()) {
      if (node.kind === "fact") facts += 1;
      else entities += 1;
    }
    return { facts, entities, links: graph.links.size };
  }, [graph]);

  const node = selected ? graph.nodes.get(selected) : null;
  const retrieval = graph.retrievals[0] ?? null;
  const empty = graph.nodes.size === 0 && status === "live";

  return (
    <div className="flex h-dvh flex-col">
      <Header status={status} counts={counts} />

      {status === "reconnecting" && (
        <div className="bg-[#ffb020]/10 px-6 py-1.5 text-[11px] text-[#ffb020]">
          lost the live connection — retrying, then resyncing. the graph below is last-known state
          {error ? ` (${error})` : ""}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <section className="flex w-[26rem] shrink-0 flex-col border-r border-[var(--color-border)]">
          <Chat apiUrl={API_URL} onFocusFact={setSelected} />
        </section>

        <main className="relative min-w-0 flex-1">
          {empty ? (
            // Not a blank canvas. An empty memory is the one moment where the
            // interface has to explain what it is for.
            <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
              <p className="text-[var(--color-muted)]">nothing remembered yet</p>
              <p className="max-w-sm text-[11px] text-[var(--color-muted)]">
                say something on the left, and watch the first fact appear here before
                the reply has finished
              </p>
            </div>
          ) : (
            <MemoryGraph graph={graph} onSelect={(picked: MemoryNode) => setSelected(picked.id)} />
          )}
        </main>

        <aside className="w-72 shrink-0 overflow-y-auto border-l border-[var(--color-border)] p-5">
          {node ? (
            <div className="space-y-4">
              <div>
                <p className="text-[10px] uppercase tracking-widest text-[var(--color-muted)]">
                  {node.kind}
                  {node.entityKind ? ` · ${node.entityKind}` : ""}
                  {node.kind === "fact" && !node.current ? " · superseded" : ""}
                </p>
                <p className="mt-1 leading-relaxed">{node.label}</p>
              </div>

              <dl className="space-y-1 text-[11px] text-[var(--color-muted)]">
                <div className="flex justify-between">
                  <dt>salience</dt>
                  <dd>{node.salience.toFixed(3)}</dd>
                </div>
                {node.kind === "fact" && (
                  <div className="flex justify-between">
                    <dt>confidence</dt>
                    <dd>{node.confidence.toFixed(2)}</dd>
                  </div>
                )}
                <div className="flex justify-between">
                  <dt>current</dt>
                  <dd>{String(node.current)}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt>id</dt>
                  <dd className="truncate">{node.id}</dd>
                </div>
              </dl>

              {!node.current && (
                <p className="rounded-[var(--radius-control)] bg-[#ff00a0]/10 p-3 text-[11px] text-[#ff00a0]">
                  This fact was superseded, not deleted. It is still stored, still queryable, and
                  still the answer to &ldquo;what did you used to think?&rdquo;
                </p>
              )}
            </div>
          ) : (
            <p className="text-[11px] text-[var(--color-muted)]">
              click a node to inspect it. facts are pale discs ringed by confidence; entities are
              coloured by kind. a dimmed node has been superseded and is still there.
            </p>
          )}
        </aside>
      </div>

      <Inspector graph={graph} retrieval={retrieval} onFocus={(factId) => setSelected(factId)} />
    </div>
  );
}
