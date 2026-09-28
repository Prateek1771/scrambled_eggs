"""Pooled connections to Arm B's four stores. "Written the way a competent team
would actually write it: connection pooling, batched writes, indexes tuned"
(docs/06-benchmark.md) -- this module is the pooling half of that promise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class ArmBPools:
    pg: "asyncpg.Pool"
    neo4j: "neo4j.AsyncDriver"
    es: "elasticsearch.AsyncElasticsearch"
    redis: "redis.asyncio.Redis"

    async def close(self) -> None:
        await self.pg.close()
        await self.neo4j.close()
        await self.es.close()
        await self.redis.aclose()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


async def connect() -> ArmBPools:
    """Connect all four stores using ARM_B_* environment variables, matching
    bench/docker-compose.yml's published ports."""
    import asyncpg
    import redis.asyncio as redis
    from elasticsearch import AsyncElasticsearch
    from neo4j import AsyncGraphDatabase

    from pgvector.asyncpg import register_vector

    pg_dsn = _env("ARM_B_PG_DSN", "postgresql://bench:bench@localhost:5433/bench")
    neo4j_uri = _env("ARM_B_NEO4J_URI", "bolt://localhost:7688")
    neo4j_user = _env("ARM_B_NEO4J_USER", "neo4j")
    neo4j_pass = _env("ARM_B_NEO4J_PASS", "benchbench")
    es_url = _env("ARM_B_ES_URL", "http://localhost:9201")
    redis_url = _env("ARM_B_REDIS_URL", "redis://localhost:6380")

    # register_vector teaches asyncpg to bind a plain Python list of floats to a
    # `vector` column -- on every pooled connection, including COPY, not just
    # ad-hoc queries -- which is what lets bulk_load's copy_records_to_table and
    # consolidate_fact's INSERT both take a Python list directly.
    pg_pool = await asyncpg.create_pool(pg_dsn, min_size=4, max_size=16, init=register_vector)
    neo4j_driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))
    es_client = AsyncElasticsearch(es_url)
    redis_client = redis.from_url(redis_url)
    return ArmBPools(pg=pg_pool, neo4j=neo4j_driver, es=es_client, redis=redis_client)


async def apply_schema(pools: ArmBPools) -> None:
    """One-shot schema setup, mirroring CORTEX's `migrate` container. Idempotent:
    every statement here is IF NOT EXISTS or a plain CREATE TABLE IF NOT EXISTS."""
    import json
    from pathlib import Path

    here = Path(__file__).resolve().parent

    sql = (here / "schema.sql").read_text(encoding="utf-8")
    async with pools.pg.acquire() as conn:
        await conn.execute(sql)

    cypher_source = "\n".join(
        line for line in (here / "schema.cypher").read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("//")
    )
    cypher_statements = [s.strip() for s in cypher_source.split(";") if s.strip()]
    async with pools.neo4j.session() as session:
        for statement in cypher_statements:
            result = await session.run(statement)
            await result.consume()

    mapping = json.loads((here / "es_mapping.json").read_text(encoding="utf-8"))
    if not await pools.es.indices.exists(index="fact"):
        await pools.es.indices.create(index="fact", **mapping)
