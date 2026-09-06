-- CORTEX schema
--
-- Applied by the one-shot `migrate` container at startup. Every statement is
-- idempotent (IF NOT EXISTS / OVERWRITE) so re-running it on a live database is
-- safe -- a restart must never be a destructive operation.
--
-- This file is the implementation of docs/03-data-model.md. Where it departs
-- from that document it is because the document was written before the syntax
-- was verified against SurrealDB 3.2; each departure is commented inline.

-- ---------------------------------------------------------------------------
-- namespace and database
-- ---------------------------------------------------------------------------

DEFINE NAMESPACE IF NOT EXISTS cortex;
USE NS cortex;
DEFINE DATABASE IF NOT EXISTS main;
USE DB main;

-- ---------------------------------------------------------------------------
-- conversation
-- ---------------------------------------------------------------------------

DEFINE TABLE IF NOT EXISTS session SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS started_at ON session TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS title      ON session TYPE option<string>;

DEFINE TABLE IF NOT EXISTS message SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS session    ON message TYPE record<session>;
DEFINE FIELD IF NOT EXISTS role       ON message TYPE string
    ASSERT $value IN ["user", "assistant", "system"];
DEFINE FIELD IF NOT EXISTS text       ON message TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON message TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS message_session ON message FIELDS session, created_at;

-- ---------------------------------------------------------------------------
-- memory
--
-- Two tables carry the memory itself: `fact` (an atomic statement, embedded and
-- searchable) and `entity` (a thing that facts are about). Everything else in
-- this file is scaffolding for provenance and grouping.
-- ---------------------------------------------------------------------------

DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name      ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS kind      ON entity TYPE string
    ASSERT $value IN ["person", "place", "org", "concept", "artifact", "event"];
DEFINE FIELD IF NOT EXISTS summary   ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS salience  ON entity TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS last_seen ON entity TYPE datetime DEFAULT time::now();

-- The unique index is what makes entity upsert safe: two concurrent turns that
-- both mention Ana cannot create two `person:ana` records.
DEFINE INDEX IF NOT EXISTS entity_identity ON entity FIELDS name, kind UNIQUE;

DEFINE TABLE IF NOT EXISTS fact SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS text       ON fact TYPE string;
DEFINE FIELD IF NOT EXISTS embedding  ON fact TYPE array<float>;
DEFINE FIELD IF NOT EXISTS confidence ON fact TYPE float DEFAULT 0.5
    ASSERT $value >= 0.0 AND $value <= 1.0;
DEFINE FIELD IF NOT EXISTS salience   ON fact TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS valid_from ON fact TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS valid_to   ON fact TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at ON fact TYPE datetime DEFAULT time::now();

-- CORRECTION vs docs/03: the design document used `VALUE valid_to = NONE`, but
-- VALUE is only re-evaluated on write, which would let `is_current` go stale the
-- moment a fact is superseded by a transaction that touches a different record.
-- COMPUTED (SurrealDB 3.0) is evaluated at query time, which is what "current"
-- actually means here.
DEFINE FIELD IF NOT EXISTS is_current ON fact COMPUTED valid_to = NONE;

DEFINE TABLE IF NOT EXISTS episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS summary ON episode TYPE string;
DEFINE FIELD IF NOT EXISTS from_ts ON episode TYPE datetime;
DEFINE FIELD IF NOT EXISTS to_ts   ON episode TYPE datetime;

-- ---------------------------------------------------------------------------
-- provenance of recall
--
-- Every recall is itself a record. This is not logging: it stores the exact
-- SurrealQL that ran, so the UI's query inspector shows what the database
-- actually did rather than a reconstruction of it.
-- ---------------------------------------------------------------------------

DEFINE TABLE IF NOT EXISTS retrieval SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS session  ON retrieval TYPE record<session>;
DEFINE FIELD IF NOT EXISTS query    ON retrieval TYPE string;
DEFINE FIELD IF NOT EXISTS strategy ON retrieval TYPE string;        -- hybrid | vector | text | graph
DEFINE FIELD IF NOT EXISTS surql    ON retrieval TYPE string;        -- what actually ran
-- FLEXIBLE because the table is SCHEMAFULL: without it, every key inside a
-- nested object would have to be declared, and these two are payloads rather
-- than structure.
--
-- OVERWRITE rather than IF NOT EXISTS on the element field: declaring
-- `hits` as array<object> makes SurrealDB define `hits.*` implicitly, so
-- IF NOT EXISTS finds it already there and silently keeps the non-FLEXIBLE
-- version -- which then rejects every write with "no such field exists".
DEFINE FIELD IF NOT EXISTS hits     ON retrieval TYPE array<object>;  -- [{ fact, score, via }]
DEFINE FIELD OVERWRITE     hits.*   ON retrieval TYPE object FLEXIBLE;
DEFINE FIELD OVERWRITE     timing   ON retrieval TYPE object FLEXIBLE;  -- { total_ms, knn_ms, ... }

-- The hit shape, typed explicitly. `fact` matters most: record ids arrive from
-- any client as plain strings, and declaring the field as record<fact> makes
-- SurrealDB coerce them on write. Without this the decay event below silently
-- updates a list of strings, salience never moves, and nothing on screen ever
-- brightens or fades -- a failure with no error attached to it.
DEFINE FIELD OVERWRITE     hits.*.fact  ON retrieval TYPE record<fact>;
DEFINE FIELD OVERWRITE     hits.*.score ON retrieval TYPE float DEFAULT 0.0;
DEFINE FIELD OVERWRITE     hits.*.via   ON retrieval TYPE string DEFAULT "vector";
DEFINE FIELD IF NOT EXISTS at       ON retrieval TYPE datetime DEFAULT time::now();

-- ---------------------------------------------------------------------------
-- edges
--
-- Edge tables hold their own data. SurrealDB drops an edge table's rows with the
-- records they connect, so there is no dangling-reference bookkeeping in
-- application code.
-- ---------------------------------------------------------------------------

DEFINE TABLE IF NOT EXISTS mentions     TYPE RELATION IN fact    OUT entity;
DEFINE TABLE IF NOT EXISTS derived_from TYPE RELATION IN fact    OUT message;
DEFINE TABLE IF NOT EXISTS supersedes   TYPE RELATION IN fact    OUT fact;
DEFINE TABLE IF NOT EXISTS summarises   TYPE RELATION IN episode OUT message;

DEFINE TABLE IF NOT EXISTS relates TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS predicate ON relates TYPE string;    -- "works_at", "lives_in", ...
DEFINE FIELD IF NOT EXISTS strength  ON relates TYPE float DEFAULT 0.5;
DEFINE FIELD IF NOT EXISTS since     ON relates TYPE datetime DEFAULT time::now();

-- ---------------------------------------------------------------------------
-- indexes
-- ---------------------------------------------------------------------------

-- Semantic recall. DIMENSION is fixed at definition time and must equal
-- EMBED_DIM and the output width of EMBED_MODEL, forever. The default stack
-- already agrees: text-embedding-3-small emits 1536 dimensions natively.
DEFINE INDEX IF NOT EXISTS fact_hnsw ON fact FIELDS embedding
    HNSW DIMENSION 1536 DIST COSINE EFC 150 M 12;

-- Lexical recall. Names, identifiers and exact phrases are where embeddings
-- fail, which is the whole reason this index exists alongside the HNSW one.
DEFINE ANALYZER IF NOT EXISTS cortex_text
    TOKENIZERS class
    FILTERS lowercase, ascii, snowball(english);

DEFINE INDEX IF NOT EXISTS fact_fts ON fact FIELDS text
    FULLTEXT ANALYZER cortex_text BM25 HIGHLIGHTS;

-- Ordinary lookups.
DEFINE INDEX IF NOT EXISTS fact_recency ON fact FIELDS created_at;
DEFINE INDEX IF NOT EXISTS entity_seen  ON entity FIELDS last_seen;

-- ---------------------------------------------------------------------------
-- decay
--
-- Reinforcement and forgetting are properties of the memory, not steps in the
-- agent's reasoning, so they live in the database and hold no matter which
-- client writes.
--
-- CORRECTION vs docs/03: ASYNC is a clause on the event, positioned before
-- WHEN -- not a modifier on the THEN block. It runs the body outside the
-- triggering transaction, so decay never delays the write that caused it.
-- Because the updates are ordinary writes, the browser sees nodes brighten and
-- fade through the same live feed as everything else.
-- ---------------------------------------------------------------------------

DEFINE EVENT IF NOT EXISTS decay_on_retrieval ON TABLE retrieval ASYNC
    WHEN $event = "CREATE" THEN {
        -- Reinforce what was just used, capped so a hot fact cannot run away.
        UPDATE $after.hits.*.fact SET salience = math::min([salience * 1.15, 3.0]);
        -- Everything else drifts down, with a floor so nothing reaches zero and
        -- becomes permanently unreachable.
        UPDATE fact SET salience = salience * 0.999
            WHERE id NOTINSIDE $after.hits.*.fact AND salience > 0.05;
    };

-- ---------------------------------------------------------------------------
-- checkpointing
--
-- LangGraph persistence, per docs/04-agent.md. `parent_id` gives a checkpoint
-- tree rather than a list, which is what LangGraph's time-travel and branching
-- need.
--
-- `state` and `metadata` hold the serialiser's output rather than plain JSON:
-- LangGraph's JsonPlusSerializer is msgpack-backed and returns a (type_tag,
-- bytes) pair, so both halves are stored and handed back to loads_typed().
-- ---------------------------------------------------------------------------

DEFINE TABLE IF NOT EXISTS checkpoint SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS thread_id      ON checkpoint TYPE string;
DEFINE FIELD IF NOT EXISTS checkpoint_ns  ON checkpoint TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS checkpoint_id  ON checkpoint TYPE string;
DEFINE FIELD IF NOT EXISTS parent_id      ON checkpoint TYPE option<string>;
DEFINE FIELD IF NOT EXISTS type           ON checkpoint TYPE string;   -- serde type tag
DEFINE FIELD IF NOT EXISTS state          ON checkpoint TYPE bytes;
DEFINE FIELD IF NOT EXISTS metadata_type  ON checkpoint TYPE string;
DEFINE FIELD IF NOT EXISTS metadata       ON checkpoint TYPE bytes;
DEFINE FIELD IF NOT EXISTS created_at     ON checkpoint TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS ckpt_thread ON checkpoint FIELDS thread_id, checkpoint_ns, created_at;
DEFINE INDEX IF NOT EXISTS ckpt_ident  ON checkpoint FIELDS thread_id, checkpoint_ns, checkpoint_id UNIQUE;

DEFINE TABLE IF NOT EXISTS checkpoint_write SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS thread_id     ON checkpoint_write TYPE string;
DEFINE FIELD IF NOT EXISTS checkpoint_ns ON checkpoint_write TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS checkpoint_id ON checkpoint_write TYPE string;
DEFINE FIELD IF NOT EXISTS task_id       ON checkpoint_write TYPE string;
DEFINE FIELD IF NOT EXISTS task_path     ON checkpoint_write TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS idx           ON checkpoint_write TYPE int;
DEFINE FIELD IF NOT EXISTS channel       ON checkpoint_write TYPE string;
DEFINE FIELD IF NOT EXISTS type          ON checkpoint_write TYPE string;
DEFINE FIELD IF NOT EXISTS value         ON checkpoint_write TYPE bytes;
DEFINE INDEX IF NOT EXISTS ckptw_lookup ON checkpoint_write
    FIELDS thread_id, checkpoint_ns, checkpoint_id;

-- ---------------------------------------------------------------------------
-- the browser's credential
--
-- The UI holds a direct WebSocket to the database, so it gets an account that
-- can do exactly nothing but read. The password is substituted by the migration
-- container from SURREAL_VIEWER_PASS; OVERWRITE rather than IF NOT EXISTS so
-- rotating the password in .env actually takes effect.
-- ---------------------------------------------------------------------------

DEFINE USER OVERWRITE cortex_viewer ON DATABASE PASSWORD "__VIEWER_PASS__" ROLES VIEWER;
