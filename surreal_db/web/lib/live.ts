/**
 * The live memory feed.
 *
 * This file is the demo. The browser holds its own WebSocket to SurrealDB and
 * subscribes to the same records the agent writes, so a fact reaches the screen
 * because the database pushed it -- not because the API relayed it, and not
 * because anything polled. Routing this through the API would quietly
 * reintroduce the pub/sub tier the project claims to delete.
 */

import { Surreal, type LiveMessage, type LiveSubscription, type Uuid } from "surrealdb";

export type Via = "vector" | "text" | "graph";
export type EntityKind = "person" | "place" | "org" | "concept" | "artifact" | "event";
export type LinkKind = "mentions" | "relates" | "supersedes" | "derived_from";

/** A node in the memory graph: either an atomic fact or an entity facts are about. */
export type MemoryNode = {
  id: string;
  kind: "fact" | "entity";
  label: string;
  entityKind?: EntityKind;
  confidence: number;
  salience: number;
  /** False once a fact has been superseded. Superseded nodes dim; they are never removed. */
  current: boolean;
  /** Set when a retrieval touched this node, so the renderer can glow it and let it expire. */
  glow?: { via: Via; score: number; until: number };
  /** Wall-clock arrival, so the renderer can animate entry rather than popping. */
  bornAt: number;
};

/** An edge. `id` is the edge record's own id, because edges are records here. */
export type MemoryLink = {
  id: string;
  source: string;
  target: string;
  kind: LinkKind;
  strength: number;
  drawnAt: number;
};

/** One recall, as the database recorded it -- including the SurrealQL that ran. */
export type Retrieval = {
  id: string;
  query: string;
  strategy: string;
  surql: string;
  timing: Record<string, number>;
  hits: { fact: string; score: number; via: Via }[];
  at: string;
};

export type GraphState = {
  nodes: Map<string, MemoryNode>;
  links: Map<string, MemoryLink>;
  retrievals: Retrieval[];
  /**
   * Edges whose endpoints have not arrived yet.
   *
   * Live delivery is best-effort across clients and ordered only within a single
   * client's transaction, so an edge genuinely can arrive before its nodes. This
   * buffer is required, not defensive.
   */
  pending: MemoryLink[];
};

export type LiveEvent =
  | { table: "fact" | "entity"; action: "CREATE" | "UPDATE"; record: Record<string, unknown> }
  | { table: LinkKind; action: "CREATE" | "UPDATE"; record: Record<string, unknown> }
  | { table: "retrieval"; action: "CREATE" | "UPDATE"; record: Record<string, unknown> }
  | { table: string; action: "DELETE"; id: string }
  | { table: "__resync"; action: "RESET"; nodes: MemoryNode[]; links: MemoryLink[] };

export const GLOW_MS = 2500;

/** Start an empty graph. Separate function so a resync can reuse it. */
export function emptyGraph(): GraphState {
  return { nodes: new Map(), links: new Map(), retrievals: [], pending: [] };
}

/** Normalise a SurrealDB value that may be a RecordId object or an id string. */
function idOf(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "tb" in value && "id" in value) {
    const record = value as { tb: string; id: unknown };
    return `${record.tb}:${String(record.id)}`;
  }
  return String(value);
}

/** Build a graph node from a `fact` record pushed by the database. */
function factNode(record: Record<string, unknown>, existing?: MemoryNode): MemoryNode {
  return {
    id: idOf(record.id),
    kind: "fact",
    label: String(record.text ?? ""),
    confidence: Number(record.confidence ?? 0.5),
    salience: Number(record.salience ?? 1),
    // Derived here rather than read from the record's COMPUTED `is_current`,
    // because a projected live payload may not carry it and the meaning is
    // identical.
    current: record.valid_to === null || record.valid_to === undefined,
    glow: existing?.glow,
    bornAt: existing?.bornAt ?? Date.now(),
  };
}

/** Build a graph node from an `entity` record pushed by the database. */
function entityNode(record: Record<string, unknown>, existing?: MemoryNode): MemoryNode {
  return {
    id: idOf(record.id),
    kind: "entity",
    label: String(record.name ?? ""),
    entityKind: record.kind as EntityKind | undefined,
    confidence: 1,
    salience: Number(record.salience ?? 1),
    current: true,
    glow: existing?.glow,
    bornAt: existing?.bornAt ?? Date.now(),
  };
}

/** Build a link from an edge record. Edge records carry `in` and `out`. */
function edgeLink(kind: LinkKind, record: Record<string, unknown>): MemoryLink {
  return {
    id: idOf(record.id),
    source: idOf(record.in),
    target: idOf(record.out),
    kind,
    strength: Number(record.strength ?? 0.5),
    drawnAt: Date.now(),
  };
}

/** Move any buffered edges whose endpoints have now arrived into the live set. */
function flushPending(state: GraphState): void {
  if (state.pending.length === 0) return;
  const stillWaiting: MemoryLink[] = [];
  for (const link of state.pending) {
    if (state.nodes.has(link.source) && state.nodes.has(link.target)) {
      state.links.set(link.id, link);
    } else {
      stillWaiting.push(link);
    }
  }
  state.pending = stillWaiting;
}

/**
 * Fold one live event into the graph.
 *
 * Returns a new top-level object so React re-renders, while reusing the
 * underlying Maps -- a consolidate step can emit a dozen writes in milliseconds
 * and cloning the whole graph per event would thrash.
 */
export function memoryReducer(state: GraphState, event: LiveEvent): GraphState {
  const next: GraphState = { ...state };

  if (event.action === "RESET") {
    next.nodes = new Map(event.nodes.map((node) => [node.id, node]));
    next.links = new Map();
    next.pending = [];
    for (const link of event.links) {
      if (next.nodes.has(link.source) && next.nodes.has(link.target)) {
        next.links.set(link.id, link);
      } else {
        next.pending.push(link);
      }
    }
    return next;
  }

  if (event.action === "DELETE") {
    // DELETE carries only a record id, never the record -- the reducer has to
    // handle that asymmetry.
    next.nodes.delete(event.id);
    next.links.delete(event.id);
    for (const [key, link] of next.links) {
      if (link.source === event.id || link.target === event.id) next.links.delete(key);
    }
    next.pending = next.pending.filter(
      (link) => link.source !== event.id && link.target !== event.id && link.id !== event.id,
    );
    return next;
  }

  const { table, record } = event;

  if (table === "fact" || table === "entity") {
    const id = idOf(record.id);
    const existing = next.nodes.get(id);
    next.nodes.set(id, table === "fact" ? factNode(record, existing) : entityNode(record, existing));
    flushPending(next);
    return next;
  }

  if (table === "retrieval") {
    const retrieval: Retrieval = {
      id: idOf(record.id),
      query: String(record.query ?? ""),
      strategy: String(record.strategy ?? "hybrid"),
      surql: String(record.surql ?? ""),
      timing: (record.timing as Record<string, number>) ?? {},
      hits: ((record.hits as { fact: unknown; score: number; via: Via }[]) ?? []).map((hit) => ({
        fact: idOf(hit.fact),
        score: Number(hit.score ?? 0),
        via: hit.via,
      })),
      at: String(record.at ?? ""),
    };
    next.retrievals = [retrieval, ...state.retrievals].slice(0, 25);

    // Glow is what makes recall visible. It expires on its own so the graph
    // returns to rest rather than staying lit.
    const until = Date.now() + GLOW_MS;
    for (const hit of retrieval.hits) {
      const node = next.nodes.get(hit.fact);
      if (node) next.nodes.set(hit.fact, { ...node, glow: { via: hit.via, score: hit.score, until } });
    }
    return next;
  }

  // Anything else is an edge table.
  const link = edgeLink(table as LinkKind, record);
  if (next.nodes.has(link.source) && next.nodes.has(link.target)) {
    next.links.set(link.id, link);
  } else {
    next.pending = [...next.pending, link];
  }
  return next;
}

/** The tables the browser watches, and the projection it watches them with. */
const SUBSCRIPTIONS: { table: string; fields: string }[] = [
  // `embedding` is deliberately absent: 1536 floats per fact that the UI never
  // reads. Selecting it would ship a megabyte of vectors to the browser for
  // nothing.
  { table: "fact", fields: "id, text, confidence, salience, valid_to" },
  { table: "entity", fields: "id, name, kind, salience" },
  { table: "mentions", fields: "id, in, out" },
  { table: "relates", fields: "id, in, out, strength, predicate" },
  { table: "supersedes", fields: "id, in, out" },
  { table: "derived_from", fields: "id, in, out" },
  { table: "retrieval", fields: "id, query, strategy, surql, timing, hits, at" },
];

/**
 * Note that facts are watched unfiltered.
 *
 * docs/05 originally subscribed with `WHERE valid_to = NONE`. That is wrong in a
 * way that only shows up at the most important moment: when a fact is
 * superseded it stops matching the filter, and a live query says nothing about a
 * record that has left its result set. The old node would sit on screen looking
 * current forever -- precisely the frame the demo is built around. Watching the
 * whole table and deriving `current` from `valid_to` costs nothing and cannot
 * miss the transition.
 */
export type Connection = {
  db: Surreal;
  subscriptions: LiveSubscription[];
};

export type LiveStatus = "connecting" | "live" | "reconnecting" | "resyncing" | "error";

export type ViewerToken = {
  token: string;
  namespace: string;
  database: string;
  expires_at: number;
};

/** Fetch a short-lived read-only database token from the API. */
export async function fetchViewerToken(apiUrl: string): Promise<ViewerToken> {
  const response = await fetch(`${apiUrl}/viewer-token`);
  if (!response.ok) throw new Error(`viewer-token failed: ${response.status}`);
  return response.json();
}

/**
 * Read the whole current memory in one shot.
 *
 * Used on first load and again after every reconnect. Live queries can miss
 * events while the socket is down, and a graph showing stale memory is worse
 * than one that flickers -- so reconnecting always ends with a full read and a
 * reconcile, never with "resume where we left off".
 */
export async function resync(db: Surreal): Promise<{ nodes: MemoryNode[]; links: MemoryLink[] }> {
  const [facts, entities, mentions, relates, supersedes] = await db.query<
    [
      Record<string, unknown>[],
      Record<string, unknown>[],
      Record<string, unknown>[],
      Record<string, unknown>[],
      Record<string, unknown>[],
    ]
  >(`
    SELECT id, text, confidence, salience, valid_to FROM fact;
    SELECT id, name, kind, salience FROM entity;
    SELECT id, in, out FROM mentions;
    SELECT id, in, out, strength FROM relates;
    SELECT id, in, out FROM supersedes;
  `);

  return {
    nodes: [...facts.map((r) => factNode(r)), ...entities.map((r) => entityNode(r))],
    links: [
      ...mentions.map((r) => edgeLink("mentions", r)),
      ...relates.map((r) => edgeLink("relates", r)),
      ...supersedes.map((r) => edgeLink("supersedes", r)),
    ],
  };
}

/**
 * Open the socket, authenticate with the viewer token, and register every
 * subscription.
 *
 * Returns the handles so the caller can kill them on unmount. Subscriptions are
 * registered through raw `LIVE SELECT` rather than the SDK's `live()` helper
 * because the helper takes a table and no projection, and the projection is what
 * keeps embeddings off the wire.
 */
export async function connect(
  surrealUrl: string,
  auth: ViewerToken,
  onEvent: (event: LiveEvent) => void,
  onSocket?: (state: "disconnected" | "reconnecting" | "connected") => void,
): Promise<Connection> {
  const db = new Surreal();

  // The SDK reconnects its socket on its own, without telling the caller unless
  // the caller asks. That is a trap: the transport comes back, the application
  // never notices, and the live subscriptions registered on the *old* socket are
  // gone. The page then sits there showing a green "live" indicator and a graph
  // that stopped updating -- permanently stale, and indistinguishable from a
  // healthy one.
  //
  // Found by the chaos test: after restarting the database underneath a live
  // page, the browser settled one fact behind the database and stayed there.
  // Subscribing to these events is what turns a silent transport recovery into
  // a re-register and a resync.
  if (onSocket) {
    db.subscribe("disconnected", () => onSocket("disconnected"));
    db.subscribe("reconnecting", () => onSocket("reconnecting"));
    db.subscribe("connected", () => onSocket("connected"));
  }
  await db.connect(surrealUrl, {
    namespace: auth.namespace,
    database: auth.database,
  });
  await db.authenticate(auth.token);

  const subscriptions: LiveSubscription[] = [];
  for (const { table, fields } of SUBSCRIPTIONS) {
    const [id] = await db.query<[Uuid]>(`LIVE SELECT ${fields} FROM ${table}`);
    // liveOf() returns a promise for the subscription, not the subscription --
    // awaiting it is what attaches this client to the notification stream.
    const subscription = await db.liveOf(id);
    subscriptions.push(subscription);
    subscription.subscribe((message: LiveMessage) => {
      if (message.action === "KILLED") return;
      if (message.action === "DELETE") {
        // DELETE carries the record id and no record, which is why the reducer
        // treats it as a separate shape.
        onEvent({ table, action: "DELETE", id: idOf(message.recordId) });
        return;
      }
      onEvent({ table, action: message.action, record: message.value } as LiveEvent);
    });
  }

  return { db, subscriptions };
}

/** Unsubscribe and close. Called on unmount and before every reconnect. */
export async function disconnect(connection: Connection): Promise<void> {
  for (const subscription of connection.subscriptions) {
    try {
      await subscription.kill();
    } catch {
      // The socket is often already gone by the time we get here; killing a
      // subscription on a dead connection is not an error worth surfacing.
    }
  }
  try {
    await connection.db.close();
  } catch {
    // Same.
  }
}
