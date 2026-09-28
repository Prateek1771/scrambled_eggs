"""Arm A: CORTEX itself. Seeds and queries SurrealDB through the real production
code path -- `api/cortex/retrieval.py`'s `recall()` and
`api/cortex/consolidate.py`'s `consolidate_fact()` -- rather than reimplementing
either, so "same hybrid retrieval, same RRF fusion, same supersession rules"
(docs/06-benchmark.md) is true by construction, not by parallel maintenance.

Seeding reuses `tests/corpus.py`'s HTTP loader unchanged, pointed at a dedicated
namespace/database so a benchmark run never touches dev data.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import corpus as corpus_module  # noqa: E402
from surreal_http import embedding as fake_embedding  # noqa: E402

from cortex.config import Settings  # noqa: E402
from cortex.consolidate import consolidate_fact  # noqa: E402
from cortex.db import Database, record_str  # noqa: E402
from cortex.models import ContradictionCheck, EntityRef, Extracted  # noqa: E402
from cortex.retrieval import recall  # noqa: E402

BENCH_SESSION_ID = "session:bench_arm_a"


class NoContradictionLLM:
    """Stand-in for consolidate_fact's `llm_fast`.

    docs/06-benchmark.md is explicit that no model is called during measurement
    -- LLM latency would swamp every difference between the arms. This keeps
    consolidate_fact's real transaction code exercised (entity upsert,
    candidate lookup, the write transaction itself) without a network call.
    Always says "no contradiction", which is the dominant real-world case.
    """

    def with_structured_output(self, _response_model):
        return self

    async def ainvoke(self, _prompt):
        return ContradictionCheck(contradicts=False)


def apply_bench_schema(endpoint: str, root_user: str = "root", root_pass: str = "root") -> None:
    """Apply db/schema.surql to a dedicated `cortex_bench`/`bench` namespace, so
    a benchmark run never touches dev data in `cortex`/`main`.

    db/schema.surql hardcodes its own `DEFINE NAMESPACE cortex ... USE DB main`
    (see db/migrate.sh) rather than taking it from connection headers, so this
    applies a copy with just those two lines swapped -- same schema, same
    indexes, different namespace. Mirrors migrate.sh's approach exactly: no
    namespace/database HTTP headers, since DEFINE NAMESPACE has to run before
    any namespace exists to be selected.
    """
    schema_path = REPO_ROOT / "db" / "schema.surql"
    body = schema_path.read_text(encoding="utf-8")
    body = (body
            .replace("DEFINE NAMESPACE IF NOT EXISTS cortex;", "DEFINE NAMESPACE IF NOT EXISTS cortex_bench;")
            .replace("USE NS cortex;", "USE NS cortex_bench;")
            .replace("DEFINE DATABASE IF NOT EXISTS main;", "DEFINE DATABASE IF NOT EXISTS bench;")
            .replace("USE DB main;", "USE DB bench;")
            .replace("__VIEWER_PASS__", "unused"))

    token = base64.b64encode(f"{root_user}:{root_pass}".encode()).decode()
    request = urllib.request.Request(
        f"{endpoint}/sql", data=body.encode("utf-8"),
        headers={"Accept": "application/json", "Authorization": f"Basic {token}"},
    )
    with urllib.request.urlopen(request) as response:
        result = json.load(response)
    failures = [envelope for envelope in result if envelope.get("status") != "OK"]
    if failures:
        raise RuntimeError(f"bench schema apply failed: {failures[:3]}")


def seed_database(corpus: corpus_module.Corpus, fact_embeddings: dict[str, list[float]],
                   **connection) -> dict[str, float]:
    """Clear and load the corpus into SurrealDB, with real embeddings. Synchronous:
    `tests/corpus.py`'s loader is plain stdlib HTTP, no event loop needed."""
    corpus_module.reset(**connection)
    return corpus_module.load(corpus, embeddings=fact_embeddings, **connection)


async def _ensure_session_and_message(database: Database) -> str:
    """One throwaway session+message pair, reused by every query and write in the
    run -- retrieval and consolidation both need somewhere to attach provenance."""
    await database.query("""
        UPSERT type::record($sid) SET title = "bench";
    """, {"sid": BENCH_SESSION_ID})
    rows = await database.query("""
        CREATE ONLY message SET session = type::record($sid), role = "user", text = "bench";
    """, {"sid": BENCH_SESSION_ID})
    return record_str(rows["id"])


async def query_all(corpus: corpus_module.Corpus, query_embeddings: list[list[float]],
                     database: Database) -> list[dict]:
    """Run every ground-truth query through the real hybrid recall path.

    Returns one dict per query: `{fact_ids, latency_ms, round_trips}`. Round
    trips is always 1 -- vector, text and graph all run inside one SurrealQL
    batch, which is the number the whole architecture argument rests on.
    """
    results = []
    for query, embedding in zip(corpus.queries, query_embeddings):
        started = time.perf_counter()
        hits, _retrieval_id = await recall(database, embedding, query.question, BENCH_SESSION_ID)
        latency_ms = (time.perf_counter() - started) * 1000
        results.append({
            "fact_ids": [hit.fact_id for hit in hits],
            "latency_ms": latency_ms,
            "round_trips": 1,
        })
    return results


async def benchmark_writes(database: Database, corpus: corpus_module.Corpus,
                            message_id: str, n: int = 200, seed: int = 7) -> list[float]:
    """Time `n` single-fact consolidate transactions, the doc's "write latency for
    one consolidate step" (metric 2). Each write mentions 1-2 real entities from
    the seeded corpus, so the entity-upsert and candidate-lookup queries do real
    work, same as in production."""
    import random

    rng = random.Random(seed)
    llm = NoContradictionLLM()
    latencies = []
    for i in range(n):
        sample = rng.sample(corpus.entities, k=min(2, len(corpus.entities)))
        fact = Extracted(
            text=f"bench synthetic fact {i}: {sample[0].name} did something noteworthy",
            confidence=0.8,
            entities=[EntityRef(name=e.name, kind=e.kind) for e in sample],
        )
        embedding = fake_embedding(900_000 + i)  # far from every real cluster -> never a duplicate
        started = time.perf_counter()
        await consolidate_fact(database, llm, fact, embedding, message_id)
        latencies.append((time.perf_counter() - started) * 1000)
    return latencies


async def run(corpus: corpus_module.Corpus, query_embeddings: list[list[float]],
              write_samples: int = 200) -> dict:
    """Connect, run the query set and the write-latency sample, return both."""
    settings = Settings.from_env()
    database = Database(settings)
    await database.connect()
    try:
        message_id = await _ensure_session_and_message(database)
        query_results = await query_all(corpus, query_embeddings, database)
        write_latencies = await benchmark_writes(database, corpus, message_id, n=write_samples)
        return {"queries": query_results, "write_latencies_ms": write_latencies}
    finally:
        await database.close()


# --------------------------------------------------------------------------
# consistency test (docs/06-benchmark.md metric 7) -- see bench/consistency.py
# for the generic orchestration this plugs into.
# --------------------------------------------------------------------------


async def create_seed_fact(database: Database, slug: str) -> None:
    """Create the one fact the consistency test's supersession chain starts from."""
    await database.query("""
        CREATE ONLY type::record($id) SET text = "consistency seed",
                                          embedding = $embedding, confidence = 0.9;
    """, {"id": f"fact:{slug}", "embedding": fake_embedding(1)})


async def supersede_probe(database: Database, old_slug: str, new_slug: str) -> None:
    """One minimal supersession transaction -- same shape as
    `consolidate_fact`'s write (create, end the old fact, RELATE supersedes),
    without the entity/message bookkeeping the real turn also does, since the
    consistency test only cares about this one transaction's atomicity."""
    await database.query("""
        BEGIN TRANSACTION;
        LET $new = (CREATE ONLY type::record($new_id) SET text = "consistency probe",
                                                           embedding = $embedding, confidence = 0.9);
        LET $old = type::record($old_id);
        UPDATE $old SET valid_to = time::now();
        RELATE $new->supersedes->$old;
        COMMIT TRANSACTION;
    """, {
        "new_id": f"fact:{new_slug}", "old_id": f"fact:{old_slug}",
        "embedding": fake_embedding(hash(new_slug) % 10_000_000),
    })


async def check_torn(database: Database, old_slug: str, new_slug: str) -> bool:
    """Torn iff both facts are current at once, or neither is -- the only two
    valid states are (old current, new absent) before the write and (old ended,
    new current) after it. One transaction, so this should always read 0."""
    result = await database.query("""
        LET $old = (SELECT valid_to FROM fact WHERE id = type::record($old_id));
        LET $new = (SELECT valid_to FROM fact WHERE id = type::record($new_id));
        RETURN { old: $old, new: $new };
    """, {"old_id": f"fact:{old_slug}", "new_id": f"fact:{new_slug}"})
    old_rows = result.get("old") or []
    new_rows = result.get("new") or []
    old_current = bool(old_rows) and old_rows[0].get("valid_to") is None
    new_current = bool(new_rows)  # a probe fact is always current the moment it exists
    return old_current == new_current
