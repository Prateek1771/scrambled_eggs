/**
 * The chat client.
 *
 * Talks to `/chat` over Server-Sent Events. This is the *only* stream that goes
 * through the API — the memory graph gets its own WebSocket straight to
 * SurrealDB, and the two are deliberately independent. The graph keeps animating
 * during a slow model call because it is not waiting on this at all.
 */

export type ChatEvent =
  | { type: "start"; session_id: string; thread_id: string }
  | { type: "stage"; node: string }
  | { type: "recalled"; count: number; hits: RecalledHit[]; retrieval_id: string }
  | { type: "answer"; text: string }
  | { type: "written"; created: number; superseded: number; duplicates: number }
  | { type: "error"; message: string }
  | { type: "done"; answer: string };

export type RecalledHit = {
  fact_id: string;
  text: string;
  score: number;
  via: "vector" | "text" | "graph";
};

export type Turn = {
  role: "user" | "assistant";
  text: string;
  /** What the turn did to memory. Rendered under the assistant's reply. */
  recalled?: number;
  created?: number;
  superseded?: number;
  duplicates?: number;
  hits?: RecalledHit[];
  pending?: boolean;
  error?: string;
};

/**
 * Send one message and yield each event as it arrives.
 *
 * An async generator rather than a callback so the caller can drive React state
 * with a plain `for await`, and so cancelling is just breaking the loop.
 *
 * `fetch` with a manual reader rather than `EventSource`: EventSource cannot
 * issue a POST, and the turn's text has to go in a body rather than a query
 * string.
 */
export async function* sendMessage(
  apiUrl: string,
  message: string,
  session: { sessionId?: string; threadId?: string },
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const response = await fetch(`${apiUrl}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      session_id: session.sessionId ?? null,
      thread_id: session.threadId ?? null,
    }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`chat failed: ${response.status} ${response.statusText}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. A frame can arrive split across
    // reads, so anything after the last separator stays in the buffer.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const event = parseFrame(frame);
      if (event) yield event;
    }
  }
}

/** Turn one raw SSE frame into a typed event, or null if it carries nothing. */
function parseFrame(frame: string): ChatEvent | null {
  let name = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return null;

  try {
    return { type: name, ...JSON.parse(dataLines.join("\n")) } as ChatEvent;
  } catch {
    // A frame that does not parse is dropped rather than throwing: one bad
    // frame must not end a turn that is otherwise working.
    return null;
  }
}
