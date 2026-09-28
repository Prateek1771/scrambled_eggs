"""Arm B's half of the consistency test (docs/06-benchmark.md metric 7): unlike
Arm A, "current" is tracked independently in three stores (Postgres,
Elasticsearch, Neo4j -- see writer.py), so a torn read here means those stores
disagree about the same fact, not "both current or neither" inside one engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
from surreal_http import embedding as fake_embedding  # noqa: E402

from . import writer
from .pool import ArmBPools

_DIM_SEED = 1


async def create_seed_fact(pools: ArmBPools, fact_id: str) -> None:
    """Create the one fact the consistency test's supersession chain starts from."""
    await writer.consolidate_fact(
        pools, fact_id=fact_id, text="consistency seed", embedding=fake_embedding(_DIM_SEED),
        confidence=0.9, entities=[],
    )


async def supersede(pools: ArmBPools, old_id: str, new_id: str) -> None:
    """One supersession: the same four independent writes `writer.consolidate_fact`
    always does, which is the point -- there is no transaction spanning them."""
    await writer.consolidate_fact(
        pools, fact_id=new_id, text="consistency probe",
        embedding=fake_embedding(hash(new_id) % 10_000_000),
        confidence=0.9, entities=[], superseded_id=old_id,
    )


async def check_torn(pools: ArmBPools, old_id: str, new_id: str) -> bool:
    """Torn iff Postgres, Elasticsearch and Neo4j disagree about whether the
    *same* fact (old or new) is current -- the propagation lag between
    writer.consolidate_fact's three independently-committed validity flags."""
    async with pools.pg.acquire() as conn:
        pg_rows = await conn.fetch(
            "SELECT id, valid_to FROM fact WHERE id = ANY($1)", [old_id, new_id])
    pg_current = {row["id"]: row["valid_to"] is None for row in pg_rows}

    es_response = await pools.es.mget(index="fact", ids=[old_id, new_id])
    es_current = {
        doc["_id"]: doc["_source"].get("valid", False)
        for doc in es_response["docs"] if doc.get("found")
    }

    async with pools.neo4j.session() as session:
        result = await session.run(
            "MATCH (f:Fact) WHERE f.id IN $ids RETURN f.id AS id, f.valid_to AS valid_to",
            ids=[old_id, new_id],
        )
        records = [record async for record in result]
    neo_current = {record["id"]: record["valid_to"] is None for record in records}

    def disagrees(fact_id: str) -> bool:
        seen = [store[fact_id] for store in (pg_current, es_current, neo_current) if fact_id in store]
        return len(set(seen)) > 1

    return disagrees(old_id) or disagrees(new_id)
