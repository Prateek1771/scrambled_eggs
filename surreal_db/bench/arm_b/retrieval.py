"""Arm B's read path: three independent round trips -- pgvector KNN,
Elasticsearch BM25, Neo4j graph traversal -- fused client-side with the
*identical* RRF logic CORTEX uses (`api.cortex.retrieval.fuse`), imported
rather than re-implemented, so ranking policy can never be the variable that
explains a difference between the arms. Only the transport differs.

The associative arm's seeds are the union of the vector and lexical hit ids,
matching db/recall.surql's `$seeds = distinct(concat($vec.id, $kw.id))` --
Arm A's real query does the same thing, so this has to match it to be a fair
comparison rather than a different (and possibly easier or harder) retrieval
problem.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from cortex.retrieval import fuse  # noqa: E402

from .pool import ArmBPools


async def recall(pools: ArmBPools, embedding: list[float], question: str,
                  k: int = 12) -> tuple[list, int]:
    """Returns (fused hits, round_trips). Round trips is always 3: one call each
    to Postgres, Elasticsearch and Neo4j -- the number the doc's metric 3 exists
    to contrast against Arm A's 1."""
    vector_rows = await _vector_search(pools, embedding, k)
    text_rows = await _text_search(pools, question, k)
    seeds = list({row["id"] for row in vector_rows} | {row["id"] for row in text_rows})
    graph_rows = await _graph_search(pools, seeds, k)

    hits = fuse({"vector": vector_rows, "text": text_rows, "graph": graph_rows})
    return hits, 3


async def _vector_search(pools: ArmBPools, embedding: list[float], k: int) -> list[dict]:
    async with pools.pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, text, confidence, 1 - (embedding <=> $1) AS score "
            "FROM fact WHERE valid_to IS NULL ORDER BY embedding <=> $1 LIMIT $2",
            embedding, k,
        )
    return [{"id": row["id"], "text": row["text"], "confidence": row["confidence"]} for row in rows]


async def _text_search(pools: ArmBPools, question: str, k: int) -> list[dict]:
    response = await pools.es.search(
        index="fact", size=k,
        query={"bool": {"must": {"match": {"text": question}}, "filter": {"term": {"valid": True}}}},
    )
    return [{"id": hit["_source"]["fact_id"], "text": hit["_source"]["text"]}
            for hit in response["hits"]["hits"]]


async def _graph_search(pools: ArmBPools, seeds: list[str], k: int) -> list[dict]:
    if not seeds:
        return []
    async with pools.neo4j.session() as session:
        result = await session.run(
            """
            UNWIND $seeds AS sid
            MATCH (:Fact {id: sid})-[:MENTIONS]->(anchor:Entity)
            WITH collect(DISTINCT anchor) AS anchors
            UNWIND anchors AS a
            OPTIONAL MATCH (a)-[:RELATES*1..2]->(reached:Entity)
            WITH anchors, collect(DISTINCT reached) AS reached
            WITH anchors + reached AS pool
            UNWIND pool AS ent
            MATCH (f:Fact)-[:MENTIONS]->(ent)
            WHERE NOT f.id IN $seeds AND f.valid_to IS NULL
            RETURN DISTINCT f.id AS id, f.text AS text
            LIMIT $k
            """,
            seeds=seeds, k=k,
        )
        records = [record async for record in result]
    return [{"id": record["id"], "text": record["text"]} for record in records]
