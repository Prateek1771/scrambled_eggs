"""Arm B's write path: the same memory semantics as CORTEX's `consolidate_fact`
(api/cortex/consolidate.py), spread across four stores with no shared
transaction. That absence is exactly what docs/06-benchmark.md's consistency
test (metric 7) measures -- Arm A commits one SurrealQL transaction; Arm B
commits four independent ones.
"""

from __future__ import annotations

import json

from .pool import ArmBPools


async def _run(session, query: str, **params) -> None:
    """`session.run` dispatches a query but does not guarantee it has finished
    executing (or surface its errors) until the result is consumed -- so every
    write-only Cypher call in this module goes through here rather than being
    awaited and dropped."""
    result = await session.run(query, **params)
    await result.consume()


async def consolidate_fact(
    pools: ArmBPools,
    fact_id: str,
    text: str,
    embedding: list[float],
    confidence: float,
    entities: list[tuple[str, str, str]],  # (entity_id, name, kind)
    relations: list[tuple[str, str, str]] = (),  # (source_id, target_id, predicate)
    superseded_id: str | None = None,
) -> None:
    """Write one fact. Each numbered step below is an independently committed
    round trip -- there is no cross-store transaction, unlike Arm A's single
    SurrealQL BEGIN/COMMIT block (api/cortex/consolidate.py:194-226)."""
    # 1. Postgres: the fact row, its entities, and supersession bookkeeping.
    async with pools.pg.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO fact (id, text, embedding, confidence) VALUES ($1, $2, $3, $4)",
                fact_id, text, embedding, confidence,
            )
            for entity_id, name, kind in entities:
                await conn.execute(
                    "INSERT INTO entity (id, name, kind) VALUES ($1, $2, $3) "
                    "ON CONFLICT (id) DO NOTHING", entity_id, name, kind,
                )
                await conn.execute(
                    "INSERT INTO mentions (fact_id, entity_id) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING", fact_id, entity_id,
                )
            for source_id, target_id, predicate in relations:
                await conn.execute(
                    "INSERT INTO relates (source_id, target_id, predicate) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    source_id, target_id, predicate,
                )
            if superseded_id:
                await conn.execute("UPDATE fact SET valid_to = now() WHERE id = $1", superseded_id)
                await conn.execute(
                    "INSERT INTO supersedes (new_id, old_id) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING", fact_id, superseded_id,
                )

    # 2. Elasticsearch: lexical index, with its own `valid` flag -- ES has no
    # notion of CORTEX's `valid_to`, so validity is tracked redundantly here,
    # one more independent commit that can lag behind Postgres.
    await pools.es.index(index="fact", id=fact_id,
                          document={"fact_id": fact_id, "text": text, "valid": True})
    if superseded_id:
        await pools.es.update(index="fact", id=superseded_id, doc={"valid": False})

    # 3. Neo4j: graph nodes/edges for associative traversal, plus its own
    # validity flag on :Fact. `text` is denormalized onto :Fact so the graph arm
    # never needs a fourth round trip. This and step 2 are what actually produce
    # the torn-read window metric 7 measures -- step 1's Postgres transaction is
    # atomic by itself, but nothing ties it to steps 2 and 3.
    async with pools.neo4j.session() as session:
        await _run(session, "MERGE (f:Fact {id: $fact_id}) SET f.text = $text, f.valid_to = null",
                   fact_id=fact_id, text=text)
        if superseded_id:
            await _run(session, "MATCH (f:Fact {id: $fact_id}) SET f.valid_to = timestamp()",
                       fact_id=superseded_id)
        for entity_id, name, kind in entities:
            await _run(
                session,
                "MERGE (e:Entity {id: $eid}) SET e.name = $name, e.kind = $kind "
                "WITH e MATCH (f:Fact {id: $fact_id}) MERGE (f)-[:MENTIONS]->(e)",
                eid=entity_id, name=name, kind=kind, fact_id=fact_id,
            )
        for source_id, target_id, predicate in relations:
            await _run(
                session,
                "MERGE (a:Entity {id: $sid}) MERGE (b:Entity {id: $tid}) "
                "MERGE (a)-[:RELATES {predicate: $predicate}]->(b)",
                sid=source_id, tid=target_id, predicate=predicate,
            )

    # 4. Redis: pub/sub, matching the reference architecture's fifth store
    # (docs/06-benchmark.md). Nothing subscribes during the benchmark; this
    # exists only for the footprint/ops comparison, not the read path.
    await pools.redis.publish("cortex-writes", json.dumps({"fact_id": fact_id}))


async def bulk_load(pools: ArmBPools, corpus, embeddings: dict[str, list[float]],
                     progress: bool = False) -> dict[str, float]:
    """Load a whole corpus, batched -- the "batched writes" half of "written the
    way a competent team would actually write it" (docs/06-benchmark.md). Not
    what metric 2 measures; that is `consolidate_fact` above, timed once per
    synthetic turn by bench/arm_a.py's Arm-A-equivalent in bench/run.py."""
    import time

    timings: dict[str, float] = {}

    started = time.perf_counter()
    async with pools.pg.acquire() as conn:
        await conn.copy_records_to_table(
            "entity", records=[(e.slug, e.name, e.kind) for e in corpus.entities],
            columns=["id", "name", "kind"],
        )
    timings["entities_pg_s"] = time.perf_counter() - started

    started = time.perf_counter()
    async with pools.neo4j.session() as session:
        await _run(
            session,
            "UNWIND $rows AS row MERGE (e:Entity {id: row.id}) "
            "SET e.name = row.name, e.kind = row.kind",
            rows=[{"id": e.slug, "name": e.name, "kind": e.kind} for e in corpus.entities],
        )
    timings["entities_neo4j_s"] = time.perf_counter() - started

    started = time.perf_counter()
    async with pools.pg.acquire() as conn:
        await conn.copy_records_to_table(
            "fact",
            records=[(f.slug, f.text, embeddings[f.slug], f.confidence) for f in corpus.facts],
            columns=["id", "text", "embedding", "confidence"],
        )
        mention_rows = [(f.slug, m) for f in corpus.facts for m in f.mentions]
        await conn.copy_records_to_table("mentions", records=mention_rows,
                                          columns=["fact_id", "entity_id"])
    timings["facts_pg_s"] = time.perf_counter() - started

    started = time.perf_counter()
    await _es_bulk_index(pools, corpus.facts, progress=progress)
    timings["facts_es_s"] = time.perf_counter() - started

    started = time.perf_counter()
    async with pools.neo4j.session() as session:
        await _run(
            session,
            "UNWIND $rows AS row MERGE (f:Fact {id: row.id}) SET f.text = row.text, f.valid_to = null",
            rows=[{"id": f.slug, "text": f.text} for f in corpus.facts],
        )
        await _run(
            session,
            "UNWIND $rows AS row MATCH (f:Fact {id: row.fact}), (e:Entity {id: row.entity}) "
            "MERGE (f)-[:MENTIONS]->(e)",
            rows=[{"fact": f.slug, "entity": m} for f in corpus.facts for m in f.mentions],
        )
    timings["facts_neo4j_s"] = time.perf_counter() - started

    started = time.perf_counter()
    superseded = [(f.superseded_by, f.slug) for f in corpus.facts if f.superseded_by]
    if superseded:
        old_ids = [old for _, old in superseded]
        async with pools.pg.acquire() as conn:
            await conn.executemany(
                "UPDATE fact SET valid_to = now() WHERE id = $1", [(old,) for old in old_ids])
            await conn.copy_records_to_table("supersedes", records=superseded,
                                              columns=["new_id", "old_id"])
        # Same three-store validity split consolidate_fact writes one at a time:
        # ES and Neo4j each get their own "no longer current" flag.
        from elasticsearch.helpers import async_bulk

        async def _invalidate_actions():
            for old in old_ids:
                yield {"_op_type": "update", "_index": "fact", "_id": old, "doc": {"valid": False}}

        await async_bulk(pools.es, _invalidate_actions())
        async with pools.neo4j.session() as session:
            await _run(
                session,
                "UNWIND $ids AS oid MATCH (f:Fact {id: oid}) SET f.valid_to = timestamp()",
                ids=old_ids,
            )
    timings["supersedes_s"] = time.perf_counter() - started

    started = time.perf_counter()
    async with pools.pg.acquire() as conn:
        await conn.copy_records_to_table(
            "relates", records=[(e.source, e.target, e.predicate) for e in corpus.edges],
            columns=["source_id", "target_id", "predicate"],
        )
    async with pools.neo4j.session() as session:
        await _run(
            session,
            "UNWIND $rows AS row MATCH (a:Entity {id: row.source}), (b:Entity {id: row.target}) "
            "MERGE (a)-[:RELATES {predicate: row.predicate}]->(b)",
            rows=[{"source": e.source, "target": e.target, "predicate": e.predicate}
                  for e in corpus.edges],
        )
    timings["relates_s"] = time.perf_counter() - started

    timings["total_s"] = sum(timings.values())
    return timings


async def _es_bulk_index(pools: ArmBPools, facts, progress: bool = False, batch_size: int = 2000) -> None:
    from elasticsearch.helpers import async_bulk

    async def actions():
        for fact in facts:
            yield {"_index": "fact", "_id": fact.slug,
                   "_source": {"fact_id": fact.slug, "text": fact.text, "valid": True}}

    await async_bulk(pools.es, actions(), chunk_size=batch_size)
    if progress:
        print(f"  es indexed {len(facts)} facts")
