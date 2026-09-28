-- Arm B / Postgres: state + vector KNN only. Lexical (Elasticsearch) and
-- associative (Neo4j) live elsewhere -- see docs/06-benchmark.md's "What is
-- compared" diagram. Applied idempotently, same rule as db/schema.surql.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS entity (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    embedding  vector(1536) NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS mentions (
    fact_id   TEXT NOT NULL REFERENCES fact(id),
    entity_id TEXT NOT NULL REFERENCES entity(id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE TABLE IF NOT EXISTS relates (
    source_id TEXT NOT NULL REFERENCES entity(id),
    target_id TEXT NOT NULL REFERENCES entity(id),
    predicate TEXT NOT NULL,
    strength  REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY (source_id, target_id, predicate)
);

CREATE TABLE IF NOT EXISTS supersedes (
    new_id TEXT NOT NULL REFERENCES fact(id),
    old_id TEXT NOT NULL REFERENCES fact(id),
    PRIMARY KEY (new_id, old_id)
);

-- Tuned, not default: HNSW with cosine ops, matching CORTEX's fact_hnsw index
-- (db/schema.surql) so the two arms are not compared on mismatched recall
-- quality due to an untuned baseline.
CREATE INDEX IF NOT EXISTS fact_hnsw ON fact
    USING hnsw (embedding vector_cosine_ops) WITH (ef_construction = 150, m = 12);
CREATE INDEX IF NOT EXISTS fact_current ON fact (valid_to);
