-- Hybrid recall: semantic, lexical and associative, in one statement.
--
-- This is the query the project exists to demonstrate. A split stack would need
-- three round trips and an application-side join to produce it, which is why
-- nobody writes it and everyone ships top-k cosine instead.
--
-- Single source of truth: read at runtime by api/cortex/retrieval.py and by
-- db/fake_writer.py, and stored verbatim on every `retrieval` record. The string
-- the query inspector shows is therefore literally the file that ran -- not a
-- reconstruction of it, and impossible to drift.
--
-- Parameters, bound by the caller:
--   $q     the embedding of the user's question
--   $text  the raw question, for BM25
--   $k     how many seeds per strategy
--
-- Leaves $vec, $kw and $assoc in scope for the caller to collect.

LET $vec = (
    -- knn() gives cosine *distance*, where 0 means identical. Everything
    -- downstream -- glow brightness, rank fusion -- reads score as "higher is
    -- better", so it is converted once here rather than inverted in three
    -- places later.
    SELECT id, text, confidence, 1 - vector::distance::knn() AS score, "vector" AS via
    FROM fact WHERE embedding <|12,64|> $q AND valid_to = NONE
);

LET $kw = (
    SELECT id, text, confidence, search::score(1) AS score,
           search::highlight("<em>", "</em>", 1) AS snippet, "text" AS via
    FROM fact WHERE text @1@ $text AND valid_to = NONE
    ORDER BY score DESC LIMIT $k
);

-- Expand outward from the entities those seeds mention.
LET $seeds = array::distinct(array::concat($vec.id, $kw.id));
LET $anchors = array::distinct(array::flatten(
    (SELECT VALUE ->mentions->entity FROM $seeds)
));

-- Depth 1 and depth 2, unioned explicitly.
--
-- `$anchors.{1..2}->relates->entity` alone looks equivalent and is not:
-- SurrealDB's recursion returns the nodes reached at the *deepest completed
-- depth*, so an anchor's immediate neighbour with no further outgoing edge is
-- dropped entirely. The clause parses, runs, and quietly under-recalls. Each
-- depth is flattened separately because array::flatten removes one level and the
-- two arms nest differently. See docs/03-data-model.md.
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
