"""M0 verification: prove the schema does what docs/03-data-model.md claims.

Runs against a live SurrealDB over the HTTP /sql endpoint using only the standard
library, so it needs no virtualenv and can run before any application code
exists.

Two things are checked, and they are the two that would invalidate the design if
they turned out to be false:

  1. Supersession. Contradicting a fact must mark the old one and leave it fully
     retrievable -- nothing is ever deleted. `is_current` must flip on its own.
  2. The hybrid query. Semantic, lexical and associative recall must compose in
     one statement. The associative clause uses a graph traversal inside
     CONTAINSANY, which is not documented; this is where that gets settled.

Usage: python db/verify.py [--endpoint http://localhost:8000]
"""

import argparse
import base64
import json
import random
import sys
import urllib.error
import urllib.request

DIM = 1536


def embedding(seed: int) -> list[float]:
    """Build a deterministic unit-length vector of the width the HNSW index wants.

    Real embeddings come from OpenAI, but these checks care only about
    dimensionality and about distinct seeds landing far apart, so a seeded PRNG
    keeps the script offline and reproducible. Gaussian coordinates in 1536
    dimensions are near-orthogonal between seeds, which is what makes "this fact
    is not semantically reachable" a property the fixture can actually rely on.
    """
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = sum(value * value for value in raw) ** 0.5 or 1.0
    return [value / norm for value in raw]


def near(seed: int, jitter: float = 0.15) -> list[float]:
    """Build a vector close to `embedding(seed)` but not identical to it.

    Used for filler facts that should crowd the query's neighbourhood, so the
    vector arm has real competition and does not simply return the whole table.
    """
    rng = random.Random(seed * 7919)
    base = embedding(seed)
    raw = [value + rng.gauss(0.0, jitter) for value in base]
    norm = sum(value * value for value in raw) ** 0.5 or 1.0
    return [value / norm for value in raw]


def query(endpoint: str, surql: str, user: str = "root", password: str = "root") -> list[dict]:
    """Execute SurrealQL and return the raw per-statement result envelopes.

    Returns the raw envelopes rather than unwrapping results because each
    statement carries its own status, and a batch can partially fail while the
    HTTP request itself succeeds with 200.
    """
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(
        f"{endpoint}/sql",
        data=surql.encode(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
            "surreal-ns": "cortex",
            "surreal-db": "main",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        print(error.read().decode(), file=sys.stderr)
        raise


def bind(params: dict) -> str:
    """Render parameters as leading LET statements.

    The HTTP /sql endpoint takes no separate variable payload, so parameters are
    prepended to the batch. Values go through json.dumps, which is valid
    SurrealQL for every type used here.
    """
    return "".join(f"LET ${name} = {json.dumps(value)};\n" for name, value in params.items())


def run(endpoint: str, label: str, surql: str, params: dict | None = None) -> list:
    """Execute a statement batch, exit loudly on any statement error, return results.

    Failing here rather than returning an error keeps each check in the caller
    down to an assertion about data instead of an assertion about plumbing.
    """
    envelopes = query(endpoint, bind(params or {}) + surql)
    for envelope in envelopes:
        if envelope.get("status") != "OK":
            print(f"FAIL [{label}] {envelope.get('result')}", file=sys.stderr)
            sys.exit(1)
    return [envelope["result"] for envelope in envelopes]


def check_supersession(endpoint: str) -> None:
    """Assert that contradicting a fact marks the old one without destroying it.

    This is the M0 done-when condition from docs/07-roadmap.md. The temporal
    model is the part of the design that silently degrades to last-write-wins if
    it is subtly wrong, so it is verified before anything is built on top of it.
    """
    run(endpoint, "reset", """
        DELETE supersedes; DELETE mentions; DELETE relates; DELETE derived_from;
        DELETE fact; DELETE entity; DELETE message; DELETE session;
    """)

    seeded = run(endpoint, "seed", """
        LET $s    = (CREATE ONLY session SET title = "verify");
        LET $m    = (CREATE ONLY message SET session = $s.id, role = "user",
                                             text = "Ana works at Acme in Munich");
        LET $ana  = (CREATE ONLY entity SET name = "Ana",    kind = "person");
        LET $acme = (CREATE ONLY entity SET name = "Acme",   kind = "org");
        LET $glob = (CREATE ONLY entity SET name = "Globex", kind = "org");
        LET $muc  = (CREATE ONLY entity SET name = "Munich", kind = "place");

        LET $ber  = (CREATE ONLY entity SET name = "Berlin", kind = "place");

        LET $old = (CREATE ONLY fact SET text = "Ana works at Acme",
                                         embedding = $e_old, confidence = 0.9);
        RELATE $old->mentions->$ana;
        RELATE $old->mentions->$acme;
        RELATE $old->derived_from->$m;
        RELATE $ana->relates->$acme SET predicate = "works_at";
        RELATE $acme->relates->$muc SET predicate = "located_in";

        -- The bait for the associative arm. This fact is two hops from the
        -- query's anchors (Ana -> Globex -> Berlin), shares no keyword with the
        -- query, and is embedded far away from it -- so neither the vector nor
        -- the lexical arm can reach it. Only the graph traversal can.
        LET $far = (CREATE ONLY fact SET text = "The coffee near the canal is excellent",
                                         embedding = $e_far, confidence = 0.6);
        RELATE $far->mentions->$ber;
        RELATE $glob->relates->$ber SET predicate = "located_in";

        -- Filler. Without these the fixture holds three facts, every one of
        -- them is inside any sane KNN neighbourhood, and the graph arm's
        -- `id NOTINSIDE $seeds` would exclude the very fact being tested.
        FOR $f IN $filler {
            CREATE fact SET text = $f.text, embedding = $f.embedding, confidence = 0.5;
        };

        RETURN { old: $old.id, ana: $ana.id, globex: $glob.id, far: $far.id,
                 session: $s.id, message: $m.id };
    """, {"e_old": embedding(1), "e_far": embedding(900), "filler": [
        {"text": f"Unrelated background fact number {n}", "embedding": near(2, 0.6)}
        for n in range(8)
    ]})[-1]

    # The supersession transaction from docs/03: one transaction, three writes,
    # zero deletes.
    run(endpoint, "supersede", """
        BEGIN TRANSACTION;
        -- Record ids round-trip through JSON as plain strings, so they have to
        -- be turned back into records. RELATE will not take an expression as an
        -- endpoint, so each one is bound to its own parameter first.
        LET $old    = type::record($old_id);
        LET $ana    = type::record($ana_id);
        LET $globex = type::record($globex_id);
        LET $new = (CREATE ONLY fact SET text = "Ana works at Globex",
                                         embedding = $e_new, confidence = 0.95);
        UPDATE $old SET valid_to = time::now();
        RELATE $new->supersedes->$old;
        RELATE $new->mentions->$ana;
        RELATE $new->mentions->$globex;
        COMMIT TRANSACTION;
    """, {"old_id": seeded["old"], "ana_id": seeded["ana"],
          "globex_id": seeded["globex"], "e_new": embedding(2)})

    facts = run(endpoint, "assert", """
        SELECT id, text, is_current, valid_to, created_at,
               ->supersedes->fact.text AS replaces
        FROM fact WHERE text CONTAINS "Ana works" ORDER BY created_at;
    """)[-1]

    assert len(facts) == 2, f"expected both versions to survive, got {len(facts)}"
    old, new = facts[0], facts[1]
    assert old["is_current"] is False, f"old fact still current: {old}"
    assert new["is_current"] is True, f"new fact not current: {new}"
    assert old["valid_to"] is not None, "old fact has no valid_to"
    assert new["replaces"] == ["Ana works at Acme"], f"supersedes edge missing: {new}"

    print("  ok  supersession: is_current flipped, both versions still retrievable")


def check_hybrid_query(endpoint: str) -> str:
    """Prove semantic + lexical + associative recall compose in a single statement.

    Returns the name of the associative form that worked. The traversal-inside-
    CONTAINSANY form written in docs/03 is undocumented; if it does not parse, the
    fallback computes the expanded anchor set in its own LET, which is still one
    round trip and one transaction, so the thesis is unaffected either way.
    """
    shared = """
        LET $vec = (
            SELECT id, text, confidence, vector::distance::knn() AS score, "vector" AS via
            -- K is 2 here, not the 12 the real query uses. The KNN operator
            -- returns K neighbours regardless of distance, and this fixture
            -- holds only three facts -- at K=12 the vector arm swallows the
            -- whole table, the graph arm's `id NOTINSIDE $seeds` excludes
            -- everything, and the check would pass or fail for the wrong
            -- reason.
            FROM fact WHERE embedding <|2,64|> $q AND valid_to = NONE
        );
        LET $kw = (
            SELECT id, text, confidence, search::score(1) AS score,
                   search::highlight("<em>", "</em>", 1) AS snippet, "text" AS via
            FROM fact WHERE text @1@ $text AND valid_to = NONE
            ORDER BY score DESC LIMIT 8
        );
        LET $seeds   = array::distinct(array::concat($vec.id, $kw.id));
        LET $anchors = array::distinct(array::flatten(
            (SELECT VALUE ->mentions->entity FROM $seeds)
        ));
    """
    tail = """
        RETURN { vector: $vec, text: $kw, graph: $assoc, anchors: $anchors };
    """

    # docs/03 expands with `$anchors.{1..2}->relates->entity`, reading it as
    # "everything within two hops". It is not. SurrealDB's recursion returns only
    # the nodes reached at the deepest completed depth, so a neighbour that has
    # no further outgoing edge is dropped from the result entirely. The clause
    # parses, runs, and quietly under-recalls -- the worst kind of wrong.
    as_written = shared + """
        LET $assoc = (
            SELECT id, text, confidence, 0.4 AS score, "graph" AS via
            FROM fact
            WHERE ->mentions->entity CONTAINSANY $anchors.{1..2}->relates->entity
              AND valid_to = NONE AND id NOTINSIDE $seeds
            LIMIT 8
        );
    """ + tail

    # The union of depth 1 and depth 2, flattened at each depth separately --
    # array::flatten only removes one level, and the two arms nest differently.
    corrected = shared + """
        LET $expanded = array::distinct(array::concat(
            array::flatten($anchors->relates->entity),
            array::flatten($anchors.{1..2}->relates->entity)
        ));
        LET $assoc = (
            SELECT id, text, confidence, 0.4 AS score, "graph" AS via
            FROM fact
            WHERE ->mentions->entity CONTAINSANY $expanded
              AND valid_to = NONE AND id NOTINSIDE $seeds
            LIMIT 8
        );
    """ + tail

    forms = {"depth-union (corrected)": corrected, "docs/03 as written": as_written}

    prelude = bind({"q": embedding(2), "text": "Globex"})
    recalled: dict[str, list[str]] = {}

    for name, surql in forms.items():
        envelopes = query(endpoint, prelude + surql)
        failed = [e for e in envelopes if e.get("status") != "OK"]
        if failed:
            print(f"  --  hybrid ({name}) rejected: {failed[0]['result']}")
            continue
        result = envelopes[-1]["result"]
        recalled[name] = [hit["text"] for hit in result["graph"]]
        print(f"  ok  hybrid ({name}) parsed: {len(result['vector'])} vector, "
              f"{len(result['text'])} text, {len(result['graph'])} graph, "
              f"{len(result['anchors'])} anchors")

    # Parsing is not the interesting part. The associative arm has to actually
    # surface the fact that the other two arms structurally cannot reach -- that
    # is the claim the whole project rests on.
    winner = "depth-union (corrected)"
    assert winner in recalled, "the corrected associative form did not run"
    assert any("coffee" in text for text in recalled[winner]), (
        "the associative arm parsed but recalled nothing reachable only by "
        f"traversal; got {recalled[winner]}"
    )
    print(f"  ok  graph-only recall reached: {recalled[winner]}")

    naive = recalled.get("docs/03 as written")
    if naive is not None and len(naive) < len(recalled[winner]):
        print(f"  !!  docs/03 as written under-recalls: {len(naive)} vs "
              f"{len(recalled[winner])} -- recursion returns only the deepest "
              f"completed depth, so one-hop dead ends are dropped")

    return winner

def main() -> None:
    """Run every M0 check against a live server and exit non-zero on the first failure."""
    parser = argparse.ArgumentParser(description="Verify the CORTEX schema.")
    parser.add_argument("--endpoint", default="http://localhost:8000")
    args = parser.parse_args()

    print(f"verifying schema against {args.endpoint}")
    check_supersession(args.endpoint)
    form = check_hybrid_query(args.endpoint)
    print(f"\nall M0 checks passed. associative form in use: {form}")


if __name__ == "__main__":
    main()
