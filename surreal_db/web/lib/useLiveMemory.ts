"use client";

/**
 * React binding for the live memory feed.
 *
 * Owns the three things that are miserable to retrofit and easy to skip:
 * reconnect with backoff, token refresh ahead of expiry, and coalescing events
 * onto an animation frame.
 */

import { useEffect, useReducer, useRef, useState } from "react";

import {
  type Connection,
  type GraphState,
  type LiveEvent,
  type LiveStatus,
  connect,
  disconnect,
  emptyGraph,
  fetchViewerToken,
  memoryReducer,
  resync,
} from "./live";

const MAX_BACKOFF_MS = 15_000;

/**
 * Refresh this far ahead of the token's expiry.
 *
 * It has to be *ahead*, not on expiry: re-authenticating ends every live query
 * on the session, so a refresh is always followed by re-registering and a full
 * resync. Doing that while the token is still valid keeps the gap to a blink
 * instead of a dropped connection.
 */
const REFRESH_MARGIN_MS = 60_000;

export type LiveMemory = {
  graph: GraphState;
  status: LiveStatus;
  error: string | null;
};

/**
 * Subscribe to the memory graph for the lifetime of the component.
 *
 * The returned graph updates as SurrealDB pushes changes. Nothing here polls and
 * nothing here talks to the API except to collect a token.
 */
export function useLiveMemory(surrealUrl: string, apiUrl: string): LiveMemory {
  const [graph, dispatch] = useReducer(memoryReducer, undefined, emptyGraph);
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const [error, setError] = useState<string | null>(null);

  // Events are buffered and flushed on an animation frame. One consolidate step
  // emits a dozen writes within a few milliseconds, and dispatching each one
  // separately re-renders the force graph a dozen times for a single logical
  // change.
  const queue = useRef<LiveEvent[]>([]);
  const frame = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    let connection: Connection | null = null;
    let attempt = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let refreshTimer: ReturnType<typeof setTimeout> | null = null;
    // Whether the socket has dropped since the last successful resync. Guards
    // against re-cycling on the very first `connected` event, which fires as
    // part of the initial handshake.
    let droppedRef = false;

    /** Queue an event and schedule a flush on the next frame. */
    function enqueue(event: LiveEvent): void {
      queue.current.push(event);
      if (frame.current !== null) return;
      frame.current = requestAnimationFrame(() => {
        frame.current = null;
        const batch = queue.current;
        queue.current = [];
        for (const item of batch) dispatch(item);
      });
    }

    /** Tear down the current socket without letting a failure stop the caller. */
    async function teardown(): Promise<void> {
      if (!connection) return;
      const current = connection;
      connection = null;
      await disconnect(current);
    }

    /**
     * Establish the connection, register subscriptions, and reconcile.
     *
     * Order matters: subscribe *first*, then read the full state. Reading first
     * would lose every write that landed between the read completing and the
     * subscription being registered -- silently, and only under load, which is
     * the worst way to find a bug.
     */
    async function open(): Promise<void> {
      try {
        setStatus(attempt === 0 ? "connecting" : "reconnecting");
        const auth = await fetchViewerToken(apiUrl);
        if (cancelled) return;

        // The API's answer wins; the prop is only a fallback for running the web
        // app outside compose against a database somewhere else.
        connection = await connect(auth.url || surrealUrl, auth, enqueue, (state) => {
          // The socket died and the SDK is bringing it back by itself. Our live
          // subscriptions do not survive that, so a transparent recovery has to
          // be turned into an explicit re-register and resync -- otherwise the
          // page goes quietly stale behind a green indicator.
          if (state === "disconnected" || state === "reconnecting") {
            droppedRef = true;
            setStatus("reconnecting");
          } else if (state === "connected" && droppedRef) {
            droppedRef = false;
            void cycle();
          }
        });
        if (cancelled) return void (await teardown());

        setStatus("resyncing");
        const snapshot = await resync(connection.db);
        if (cancelled) return void (await teardown());

        dispatch({ table: "__resync", action: "RESET", ...snapshot });
        setStatus("live");
        setError(null);
        attempt = 0;

        const untilRefresh = Math.max(
          10_000,
          auth.expires_at * 1000 - Date.now() - REFRESH_MARGIN_MS,
        );
        refreshTimer = setTimeout(() => void cycle(), untilRefresh);
      } catch (caught) {
        if (cancelled) return;
        setStatus("reconnecting");
        setError(caught instanceof Error ? caught.message : String(caught));
        // Exponential backoff with a ceiling. A tight retry loop against a
        // database that is still starting up is indistinguishable from an
        // outage, and it makes the logs useless.
        const delay = Math.min(MAX_BACKOFF_MS, 500 * 2 ** attempt);
        attempt += 1;
        reconnectTimer = setTimeout(() => void cycle(), delay);
      }
    }

    /** Drop whatever is open and connect again from scratch. */
    async function cycle(): Promise<void> {
      if (refreshTimer) clearTimeout(refreshTimer);
      await teardown();
      if (!cancelled) await open();
    }

    void open();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (refreshTimer) clearTimeout(refreshTimer);
      if (frame.current !== null) cancelAnimationFrame(frame.current);
      void teardown();
    };
  }, [surrealUrl, apiUrl]);

  return { graph, status, error };
}
