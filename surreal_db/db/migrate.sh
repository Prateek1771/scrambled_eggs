#!/bin/sh
# Applies db/schema.surql to a running SurrealDB over the HTTP /sql endpoint.
#
# Runs as a one-shot container before the API starts. The schema is idempotent,
# so this executes on every `docker compose up` and a restart is safe.
#
# Two things happen here that a plain `surreal import` cannot do:
#   1. the viewer password is substituted in from the environment, so no
#      credential is ever committed to the schema file;
#   2. EMBED_DIM is asserted against the dimension the HNSW index declares,
#      which turns a silent, query-time "vector is the wrong width" failure into
#      a loud startup failure.
set -eu

SCHEMA=/db/schema.surql
ENDPOINT="${SURREAL_HTTP}/sql"

# --- guard: the embedding width must match the index, forever -----------------
#
# DEFINE INDEX ... HNSW DIMENSION n is fixed at definition time. If EMBED_MODEL
# emits a different width than the index holds, every write fails later, on
# somebody's first message, long after the containers looked healthy.
INDEX_DIM=$(sed -n 's/.*HNSW DIMENSION \([0-9]*\).*/\1/p' "$SCHEMA" | head -n 1)
if [ "$INDEX_DIM" != "$EMBED_DIM" ]; then
    echo "migrate: EMBED_DIM=$EMBED_DIM does not match HNSW DIMENSION $INDEX_DIM in schema.surql." >&2
    echo "migrate: change EMBED_DIM back, or redefine the index and re-embed the corpus." >&2
    exit 1
fi

echo "migrate: applying schema (embedding dimension $INDEX_DIM)"

# --- apply --------------------------------------------------------------------
#
# The schema carries its own USE NS/DB statements, so no namespace headers are
# sent -- DEFINE NAMESPACE has to run before any namespace exists to be selected.
BODY=$(sed "s/__VIEWER_PASS__/${SURREAL_VIEWER_PASS}/" "$SCHEMA")

RESPONSE=$(curl --silent --show-error --fail-with-body \
    --user "${SURREAL_ROOT_USER}:${SURREAL_ROOT_PASS}" \
    --header "Accept: application/json" \
    --data-binary "$BODY" \
    "$ENDPOINT")

# --- report -------------------------------------------------------------------
#
# /sql answers 200 with a per-statement array even when individual statements
# failed, so the status of each one has to be inspected rather than trusting the
# HTTP code. Anything not "OK" is printed and the container exits non-zero, which
# stops the stack instead of letting the API start against a half-built schema.
if echo "$RESPONSE" | grep -q '"status":"ERR"'; then
    echo "migrate: one or more statements failed:" >&2
    echo "$RESPONSE" >&2
    exit 1
fi

echo "migrate: ok"
