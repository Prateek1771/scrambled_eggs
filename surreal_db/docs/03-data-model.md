# 03 — Data model

Everything below is written to become `schema.surql` verbatim. It is one file, idempotent, applied
by the migration container at startup.

## Shape

```mermaid
erDiagram
    session ||--o{ message : "contains"
    message ||--o{ fact : "derived_from"
    fact }o--o{ entity : "mentions"
    entity }o--o{ entity : "relates"
    fact ||--o| fact : "supersedes"
    session ||--o{ retrieval : "logs"
    episode }o--o{ message : "summarises"

    session {
        datetime started_at
        string title
    }
    message {
        record session
        string role
        string text
        datetime created_at
    }
    fact {
        string text
        array embedding "HNSW 1536"
        float confidence
        float salience
        datetime valid_from
        datetime valid_to "NONE = current"
        bool is_current "computed"
    }
    entity {
        string name
        string kind
        string summary
        float salience
        datetime last_seen
    }
    retrieval {
        string query
        string strategy
        object timing
        string surql "the query we ran"
    }
    episode {
        string summary
        datetime from_ts
        datetime to_ts
    }
```

Two node types carry the memory: **`fact`** (an atomic statement, embedded and searchable) and
**`entity`** (a thing facts are about). Everything else is scaffolding — provenance, grouping, and
the conversation itself.

`retrieval` is unusual and deliberate: **every recall is itself a record**, storing the SurrealQL
that ran and how long it took. That is what powers the query inspector in the UI, and it means the
system can answer "what did you look at before you said that?" — not just "what do you know".

## Namespace and database

```surql
DEFINE NAMESPACE IF NOT EXISTS cortex;
USE NS cortex;
DEFINE DATABASE IF NOT EXISTS main;
USE DB main;
```

## Tables

```surql
-- conversation ---------------------------------------------------------------

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

-- memory ---------------------------------------------------------------------

DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name      ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS kind      ON entity TYPE string
    ASSERT $value IN ["person", "place", "org", "concept", "artifact", "event"];
DEFINE FIELD IF NOT EXISTS summary   ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS salience  ON entity TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS last_seen ON entity TYPE datetime DEFAULT time::now();
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

-- computed field (3.0): current-ness is derived, never set by the application
DEFINE FIELD IF NOT EXISTS is_current ON fact COMPUTED valid_to = NONE;

DEFINE TABLE IF NOT EXISTS episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS summary ON episode TYPE string;
DEFINE FIELD IF NOT EXISTS from_ts ON episode TYPE datetime;
DEFINE FIELD IF NOT EXISTS to_ts   ON episode TYPE datetime;

-- provenance of recall -------------------------------------------------------

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
```

## Edges

```surql
DEFINE TABLE IF NOT EXISTS mentions     TYPE RELATION IN fact    OUT entity;
DEFINE TABLE IF NOT EXISTS derived_from TYPE RELATION IN fact    OUT message;
DEFINE TABLE IF NOT EXISTS supersedes   TYPE RELATION IN fact    OUT fact;
DEFINE TABLE IF NOT EXISTS summarises   TYPE RELATION IN episode OUT message;

DEFINE TABLE IF NOT EXISTS relates TYPE RELATION IN entity OUT entity;
DEFINE FIELD IF NOT EXISTS predicate ON relates TYPE string;    -- "works_at", "lives_in", ...
DEFINE FIELD IF NOT EXISTS strength  ON relates TYPE float DEFAULT 0.5;
DEFINE FIELD IF NOT EXISTS since     ON relates TYPE datetime DEFAULT time::now();
```

Edges are separate tables holding their own data, and SurrealDB removes an edge table once no
relationships remain — so there is no dangling-reference bookkeeping in application code. Where
3.0's schema-level bidirectional links apply (`relates`, which is symmetric for some predicates),
they keep both directions in sync without a second `RELATE`.

## Indexes

```surql
-- semantic recall - HNSW, concurrent-write capable in 3.0
DEFINE INDEX IF NOT EXISTS fact_hnsw ON fact FIELDS embedding
    HNSW DIMENSION 1536 DIST COSINE EFC 150 M 12;

-- lexical recall - BM25 with highlighting
DEFINE ANALYZER IF NOT EXISTS cortex_text
    TOKENIZERS class
    FILTERS lowercase, ascii, snowball(english);

DEFINE INDEX IF NOT EXISTS fact_fts ON fact FIELDS text
    FULLTEXT ANALYZER cortex_text BM25 HIGHLIGHTS;

-- ordinary lookups
DEFINE INDEX IF NOT EXISTS fact_recency ON fact FIELDS created_at;
DEFINE INDEX IF NOT EXISTS entity_seen  ON entity FIELDS last_seen;
```

> **`DIMENSION 1536` is load-bearing.** It must equal `EMBED_DIM` and the output width of
> `EMBED_MODEL`, forever — `DEFINE INDEX ... HNSW DIMENSION n` is fixed at definition time.
>
> The default stack already satisfies this: `text-embedding-3-small` emits 1536 dimensions natively.
> Nothing to configure.
>
> Upgrading to `text-embedding-3-large` (3072 native) has two paths. Pass `dimensions=1536` and it
> shrinks to fit this index with no schema change and no re-embedding of *future* facts — though
> existing vectors are from a different model and should be regenerated for consistent similarity.
> Or redefine the index at `DIMENSION 3072` and re-embed the whole corpus. Either way it is a
> deliberate migration, never an env-var edit.
>
> The migration asserts `EMBED_DIM` against the index rather than letting a mismatch surface as a
> query-time error on someone's first message.

## Hybrid retrieval — the one query

This is the query the project exists to show. Semantic, lexical and associative recall in one
statement, one transaction, no application-side join.

```surql
-- $q    : embedding of the user's question
-- $text : the raw question, for BM25
-- $k    : how many seeds per strategy

LET $vec = (
    SELECT id, text, confidence,
           vector::distance::knn() AS score,
           "vector" AS via
    FROM fact
    WHERE embedding <|12,64|> $q
      AND valid_to = NONE
);

LET $kw = (
    SELECT id, text, confidence,
           search::score(1) AS score,
           search::highlight("<em>", "</em>", 1) AS snippet,
           "text" AS via
    FROM fact
    WHERE text @1@ $text
      AND valid_to = NONE
    ORDER BY score DESC
    LIMIT $k
);

-- expand outward from the entities those seeds mention, 1..2 hops
LET $seeds   = array::distinct(array::concat($vec.id, $kw.id));
LET $anchors = array::distinct(array::flatten(
    (SELECT VALUE ->mentions->entity FROM $seeds)
));

LET $expanded = array::distinct(array::concat(
    array::flatten($anchors->relates->entity),
    array::flatten($anchors.{1..2}->relates->entity)
));

LET $assoc = (
    SELECT id, text, confidence, 0.4 AS score, "graph" AS via
    FROM fact
    WHERE ->mentions->entity CONTAINSANY $expanded
      AND valid_to = NONE
      AND id NOTINSIDE $seeds
    LIMIT $k
);

RETURN { vector: $vec, text: $kw, graph: $assoc, anchors: $anchors };
```

`<|12,64|>` is the KNN operator — 12 neighbours, EF search width 64. `@1@` is the full-text match
operator bound to reference `1`, which `search::score(1)` and `search::highlight(..., 1)` read.
`vector::distance::knn()` reads back the distance the KNN operator already computed, rather than
recomputing cosine similarity a second time.

> **Why the association step unions two depths instead of writing `.{1..2}`.**
> The obvious form — `CONTAINSANY $anchors.{1..2}->relates->entity` — parses, runs, and is wrong.
> SurrealDB's recursion returns the nodes reached at the *deepest completed depth*, not the union of
> every depth along the way, so an anchor's immediate neighbour that happens to have no further
> outgoing edge is dropped from the result entirely. Ana → Acme → Munich contributes Munich; Globex →
> Berlin, a dead end after one hop, contributes nothing, and every fact about Berlin becomes
> unreachable. There is no error and no warning — the query simply recalls less than it should.
>
> Unioning depth 1 and depth 2 explicitly fixes it. Each depth is flattened separately because
> `array::flatten` removes a single level and the two arms nest differently. `db/verify.py` runs both
> forms against the same fixture and asserts on the difference, so this cannot quietly regress.

Fusion (reciprocal rank fusion across the three lists) happens in Python. It is ranking policy, not
data access, and keeping it out of SurrealQL makes it unit-testable. The fused result plus the
SurrealQL string above are written to a `retrieval` record — which is what the inspector displays.

### Provenance walk

```surql
-- why do you believe fact:xyz?
SELECT
    text,
    ->derived_from->message.{ text, created_at, role } AS sources,
    ->supersedes->fact.{ text, valid_from, valid_to }  AS replaced
FROM fact:xyz;
```

### Recursive association

```surql
-- everything within 3 hops of Ana
SELECT VALUE entity:ana.{1..3}->relates->entity;
```

## Temporal model — nothing is deleted

```mermaid
flowchart LR
    subgraph t1["t1 - learned"]
        F1["fact:a<br/>'Ana works at Acme'<br/>valid_to = NONE<br/>is_current = true"]
    end
    subgraph t2["t2 - contradicted"]
        F2["fact:b<br/>'Ana works at Globex'<br/>valid_to = NONE<br/>is_current = true"]
        F1b["fact:a<br/>valid_to = t2<br/>is_current = false"]
        F2 -->|supersedes| F1b
    end
    t1 --> t2

    QN["query now"] -.-> F2
    QT["query 'as of t1'"] -.-> F1b

    style F1b stroke-dasharray: 4 4
```

Supersession is one transaction:

```surql
BEGIN TRANSACTION;
LET $old = type::record($old_id);
LET $new = (CREATE ONLY fact CONTENT { text: $text, embedding: $emb, confidence: $conf });
UPDATE $old SET valid_to = time::now();
RELATE $new->supersedes->$old;
COMMIT TRANSACTION;
```

Retrieval filters on `valid_to = NONE`. Time-travel is the same query with
`valid_from <= $t AND (valid_to = NONE OR valid_to > $t)` — which is why the temporal fields exist
even though v1 ships no time slider.

## Decay — ASYNC, off the write path

```surql
DEFINE EVENT IF NOT EXISTS decay_on_retrieval ON TABLE retrieval ASYNC
    WHEN $event = "CREATE" THEN {
    -- reinforce what was just used
    UPDATE $after.hits.*.fact SET salience = math::min([salience * 1.15, 3.0]);
    -- everything else drifts down
    UPDATE fact SET salience = salience * 0.999
        WHERE id NOTINSIDE $after.hits.*.fact AND salience > 0.05;
};
```

`ASYNC` runs after commit, so decay never delays the write that triggered it. And because the updates
are ordinary writes, the browser sees nodes brighten and fade through the same live feed as
everything else — no extra plumbing.

## What the browser subscribes to

```surql
LIVE SELECT * FROM fact WHERE valid_to = NONE;
LIVE SELECT * FROM entity;
LIVE SELECT * FROM relates;
LIVE SELECT * FROM mentions;
LIVE SELECT * FROM supersedes;
LIVE SELECT DIFF FROM fact;   -- salience/decay churn, as JSON Patch
```

Notes that matter:

- Each `LIVE SELECT` returns a UUID; the client tracks them and unsubscribes on unmount.
- `CREATE`/`UPDATE` push the full record; `DELETE` pushes only the record id.
- `DIFF` mode pushes JSON Patch instead of the whole record — used for high-frequency salience
  updates so decay doesn't ship a 1536-float embedding on every tick.
- Parameters are captured at registration time, and a bare parameter cannot name a table — use
  `type::table($param)`.
- **Signin/signup invalidates live queries on that session.** Token refresh must re-register.

## Verifying this file

Before any application code exists:

```bash
docker compose up -d --wait surrealdb   # starts the server
docker compose run --rm migrate         # applies this schema
python db/verify.py                     # proves it does what this document says
```

`db/verify.py` needs no virtualenv — standard library only, over the HTTP `/sql` endpoint. It checks
the two things that would invalidate the design if they were false:

1. **Supersession.** Two contradictory facts, the transaction above, then assert `is_current` flipped
   on its own, the old fact kept its `valid_to`, the `supersedes` edge points the right way, and
   **both versions are still retrievable**. Nothing may ever be deleted.
2. **The hybrid query.** Runs the associative arm both as this document originally wrote it and in
   the corrected depth-union form, against a fixture containing one fact that is reachable *only* by
   traversal — two hops from the query's anchors, sharing no keyword with it, embedded far away from
   it. The corrected form must find it. The naive form must not, which is what keeps that regression
   from returning.

## SurrealQL notes that cost an hour each

Found while making the above pass on 3.1.6. None of them are documented prominently, and all of them
fail in ways that look like something else:

| Behaviour | Consequence |
|---|---|
| Record ids serialise to plain strings (`"fact:abc"`) in JSON | They must be turned back with `type::record($id)` before use. Passing the string where a record is expected fails the whole transaction with a generic "failed transaction" message that names nothing |
| `RELATE` will not take an expression as an endpoint | `RELATE $a->edge->type::record($b)` is a parse error. Bind each endpoint to its own parameter first |
| `ORDER BY` fields must appear in the selection | `SELECT id, text FROM fact ORDER BY created_at` is rejected — "Missing order idiom" |
| `CREATE` returns an array | Use `CREATE ONLY` anywhere a single record is expected, or every downstream field access is off by one level of nesting |
| `type::thing` was renamed `type::record` | The 2.x name is gone |
| The KNN operator returns K neighbours regardless of distance | On a small table it returns the whole table. Anything that then excludes the seed set — as the associative arm does — silently has nothing left to work with |
