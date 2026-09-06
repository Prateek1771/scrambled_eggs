"use client";

/**
 * The chat pane.
 *
 * Deliberately plain — it must not compete with the graph, which is the thing
 * worth watching. The one piece of decoration it earns is the line under each
 * reply saying what the turn did to memory: `3 recalled · 1 written · 1
 * superseded`. Those numbers are the product.
 */

import { useEffect, useRef, useState } from "react";

import { type Turn, sendMessage } from "@/lib/chat";

const VIA_COLOUR: Record<string, string> = {
  vector: "#2d8cff",
  text: "#ffb020",
  graph: "#ff00a0",
};

export function Chat({ apiUrl, onFocusFact }: { apiUrl: string; onFocusFact: (id: string) => void }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const session = useRef<{ sessionId?: string; threadId?: string }>({});
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  /**
   * Send the draft and fold each streamed event into the last turn.
   *
   * The assistant turn is appended immediately in a pending state, so the pane
   * shows something during the model call rather than sitting blank — the graph
   * is already moving by then, and a frozen chat next to a live graph reads as a
   * bug.
   */
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || busy) return;

    setDraft("");
    setBusy(true);
    setTurns((current) => [
      ...current,
      { role: "user", text: message },
      { role: "assistant", text: "", pending: true },
    ]);

    /** Update the assistant turn currently being streamed into. */
    const patch = (changes: Partial<Turn>) =>
      setTurns((current) => {
        const next = [...current];
        next[next.length - 1] = { ...next[next.length - 1], ...changes };
        return next;
      });

    try {
      for await (const item of sendMessage(apiUrl, message, session.current)) {
        switch (item.type) {
          case "start":
            // Both ids are kept so the next turn continues the same conversation
            // and resumes the same checkpointed thread.
            session.current = { sessionId: item.session_id, threadId: item.thread_id };
            break;
          case "recalled":
            patch({ recalled: item.count, hits: item.hits });
            break;
          case "answer":
            patch({ text: item.text });
            break;
          case "written":
            patch({
              created: item.created,
              superseded: item.superseded,
              duplicates: item.duplicates,
            });
            break;
          case "error":
            patch({ error: item.message, pending: false });
            break;
          case "done":
            patch({ text: item.answer || "", pending: false });
            break;
        }
      }
    } catch (caught) {
      patch({ error: caught instanceof Error ? caught.message : String(caught), pending: false });
    } finally {
      patch({ pending: false });
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
        {turns.length === 0 && (
          <div className="space-y-2 text-[11px] text-[var(--color-muted)]">
            <p>Tell me something and watch it become memory.</p>
            <p className="opacity-70">
              Try: &ldquo;Ana works at Acme in Munich.&rdquo; Then contradict it:
              &ldquo;actually she moved to Globex.&rdquo; The old fact dims — it is
              never deleted.
            </p>
          </div>
        )}

        {turns.map((turn, index) => (
          <div key={index} className="space-y-1">
            <p className="text-[10px] uppercase tracking-widest text-[var(--color-muted)]">
              {turn.role === "user" ? "you" : "cortex"}
            </p>
            <p className="leading-relaxed">
              {turn.text || (turn.pending ? <span className="opacity-50">thinking…</span> : "")}
            </p>

            {turn.error && (
              <p className="rounded-[var(--radius-control)] bg-[#fca5a5]/10 p-2 text-[11px] text-[#fca5a5]">
                {turn.error} — memory already written this turn is unaffected.
              </p>
            )}

            {turn.role === "assistant" && !turn.pending && turn.recalled !== undefined && (
              <>
                <p className="text-[11px] text-[var(--color-muted)]">
                  ⓘ {turn.recalled} recalled
                  {turn.created ? ` · ${turn.created} written` : ""}
                  {turn.superseded ? ` · ${turn.superseded} superseded` : ""}
                  {turn.duplicates ? ` · ${turn.duplicates} already known` : ""}
                </p>
                {turn.hits && turn.hits.length > 0 && (
                  <div className="flex flex-wrap gap-1 pt-1">
                    {turn.hits.map((hit) => (
                      <button
                        key={hit.fact_id}
                        onClick={() => onFocusFact(hit.fact_id)}
                        title={hit.text}
                        className="rounded-[var(--radius-control)] border px-1.5 py-0.5 text-[10px]"
                        style={{ borderColor: VIA_COLOUR[hit.via] ?? "#444" }}
                      >
                        <span style={{ color: VIA_COLOUR[hit.via] }}>{hit.via}</span>
                        <span className="text-[var(--color-muted)]">
                          {" "}
                          {hit.text.slice(0, 32)}
                          {hit.text.length > 32 ? "…" : ""}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        ))}
        <div ref={bottom} />
      </div>

      <form onSubmit={submit} className="border-t border-[var(--color-border)] p-3">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={busy}
          placeholder={busy ? "thinking…" : "tell me something…"}
          className="w-full rounded-[var(--radius-control)] border border-[var(--color-border)]
                     bg-[var(--color-surface)] px-3 py-2 text-[13px] outline-none
                     focus:border-[var(--color-primary)] disabled:opacity-50"
        />
      </form>
    </div>
  );
}
