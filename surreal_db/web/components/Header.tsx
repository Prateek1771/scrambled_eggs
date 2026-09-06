"use client";

/**
 * The header carries the two claims the project makes, as live state rather than
 * as copy: that the connection is a real subscription, and that one engine is
 * doing the work of five.
 */

import { useState } from "react";

import type { LiveStatus } from "@/lib/live";

const REPLACED = [
  ["Semantic recall", "Pinecone / pgvector"],
  ["Associative memory", "Neo4j"],
  ["Keyword recall", "Elasticsearch"],
  ["Conversation state", "Postgres"],
  ["Live updates to the UI", "Redis pub/sub + a websocket service"],
];

const STATUS_STYLE: Record<LiveStatus, { colour: string; label: string }> = {
  connecting: { colour: "#a1a1af", label: "connecting" },
  live: { colour: "#6ee7b7", label: "live" },
  reconnecting: { colour: "#ffb020", label: "reconnecting" },
  resyncing: { colour: "#ffb020", label: "resyncing" },
  error: { colour: "#fca5a5", label: "error" },
};

export function Header({
  status,
  counts,
}: {
  status: LiveStatus;
  counts: { facts: number; entities: number; links: number };
}) {
  const [open, setOpen] = useState(false);
  const style = STATUS_STYLE[status];

  return (
    <header className="flex items-center justify-between border-b border-[var(--color-border)] px-6 py-3">
      <div className="flex items-baseline gap-4">
        <span className="text-xl tracking-wide" style={{ fontFamily: "var(--font-display-loaded)" }}>
          CORTEX
        </span>
        <span className="text-[11px] text-[var(--color-muted)]">
          {counts.facts} facts · {counts.entities} entities · {counts.links} edges
        </span>
      </div>

      <div className="flex items-center gap-5">
        <div className="flex items-center gap-2 text-[11px]">
          <span
            className={status === "live" ? "dot-live" : ""}
            style={{
              width: 7,
              height: 7,
              borderRadius: 999,
              background: style.colour,
              display: "inline-block",
            }}
          />
          <span style={{ color: style.colour }}>{style.label}</span>
        </div>

        {/* The stack counter. Hovering expands it into the five engines this
            replaces -- the argument the project exists to make, stated as a
            number rather than a paragraph. */}
        <div
          className="relative"
          onMouseEnter={() => setOpen(true)}
          onMouseLeave={() => setOpen(false)}
        >
          <button className="rounded-[var(--radius-control)] border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-1 text-[11px]">
            <span className="text-[var(--color-primary)]">1 database</span>
            <span className="text-[var(--color-muted)]"> (vs 5)</span>
          </button>
          {open && (
            <div className="panel absolute right-0 z-20 mt-2 w-80 p-4 text-[11px]">
              <p className="mb-3 text-[var(--color-muted)]">
                One engine, one transaction, one query language. What it stands in for:
              </p>
              <ul className="space-y-1.5">
                {REPLACED.map(([job, tool]) => (
                  <li key={job} className="flex justify-between gap-4">
                    <span>{job}</span>
                    <span className="text-[var(--color-muted)] line-through">{tool}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
