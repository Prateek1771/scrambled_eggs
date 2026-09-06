"""Recall: one SurrealQL statement, three strategies, fused in Python.

The query itself lives in `db/recall.surql` and is read at import. Fusion stays
here rather than in SurrealQL because it is ranking *policy*, not data access --
and keeping it in Python makes it a pure function that can be tested exhaustively
without a database, which is exactly what you want from the code most likely to
silently rank things wrongly.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .db import Database, record_str
from .models import Hit, Via

logger = logging.getLogger("cortex.retrieval")

# Reciprocal rank fusion's damping constant. 60 is the value from the original
# paper and the de facto default; it flattens the difference between rank 1 and
# rank 2 enough that a single strategy cannot dominate the fused list.
RRF_K = 60


def _load_recall_query() -> str:
    """Read db/recall.surql, from the container mount or the repo checkout.

    Two locations because the same file has to be reachable from inside the api
    container (where db/ is mounted at /db) and from a test run on the host.
    """
    override = os.environ.get("RECALL_SURQL_PATH")
    candidates = [
        Path(override) if override else None,
        Path("/db/recall.surql"),
        Path(__file__).resolve().parents[2] / "db" / "recall.surql",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "db/recall.surql not found. Set RECALL_SURQL_PATH, or mount ./db into the "
        "api container."
    )


RECALL_SURQL = _load_recall_query()


def fuse(arms: dict[str, list[dict]], k: int = RRF_K, limit: int = 12) -> list[Hit]:
    """Combine ranked lists from several strategies by reciprocal rank fusion.

    Pure. No database, no clock, no configuration -- three fixed lists in, one
    ordered list out, which is what makes the ranking testable at all.

    A fact found by two strategies outranks one found by either alone, even if
    neither ranked it first. That is the whole point of fusing: agreement between
    a vector and a keyword match is stronger evidence than a high score from
    either, and raw scores from different strategies are not comparable in the
    first place -- cosine similarity and BM25 do not share a scale.

    The `via` recorded on a fused hit is the strategy that ranked it *highest*,
    since the browser colours a glowing node by exactly one strategy.
    """
    scores: dict[str, float] = {}
    best: dict[str, tuple[float, str]] = {}   # fact id -> (contribution, via)
    seen: dict[str, dict] = {}

    for via, rows in arms.items():
        for rank, row in enumerate(rows or []):
            fact_id = record_str(row["id"])
            contribution = 1.0 / (k + rank + 1)
            scores[fact_id] = scores.get(fact_id, 0.0) + contribution
            seen.setdefault(fact_id, row)
            if fact_id not in best or contribution > best[fact_id][0]:
                best[fact_id] = (contribution, via)

    # Ties are normal here -- two symmetric rankings tie every document -- and
    # `sorted` is stable, so without a second key the result would depend on
    # which arm happened to be iterated first. Falling back to the id keeps the
    # output identical run to run.
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    hits: list[Hit] = []
    for fact_id, score in ordered:
        row = seen[fact_id]
        hits.append(Hit(
            fact_id=fact_id,
            text=str(row.get("text", "")),
            score=round(score, 6),
            via=best[fact_id][1],  # type: ignore[arg-type]
            confidence=float(row.get("confidence") or 0.5),
            snippet=row.get("snippet"),
        ))
    return hits


async def recall(
    database: Database,
    embedding: list[float],
    question: str,
    session_id: str,
    k: int = 8,
) -> tuple[list[Hit], str]:
    """Run hybrid recall, record it, and return the fused hits.

    Returns the hits and the id of the `retrieval` record, which the caller
    attaches to the turn so the UI can link an answer to what produced it.

    The `retrieval` record is not logging. It is a product feature three times
    over: it drives the query inspector, it tells the browser which nodes to glow
    and in which colour, and creating it fires the ASYNC decay event -- so
    remembering something reinforces it, as a side effect of the write, with no
    application code involved.
    """
    started = time.perf_counter()

    # The hits array is built inside the same statement that runs the query.
    # Record ids that leave the database and come back have to be re-coerced, and
    # `hits.*.fact` is typed `record<fact>` with no implicit conversion -- so the
    # ids never leave in the first place.
    statement = RECALL_SURQL + """
        LET $sess = type::record($session_id);
        LET $rows = array::concat($vec, $kw, $assoc);
        LET $hits = array::map($rows, |$h| { fact: $h.id, score: $h.score, via: $h.via });
        LET $created = (CREATE ONLY retrieval SET
            session  = $sess,
            query    = $text,
            strategy = "hybrid",
            surql    = $surql,
            timing   = $timing,
            hits     = $hits);
        RETURN { vector: $vec, text: $kw, graph: $assoc, retrieval: $created.id };
    """

    result = await database.query(statement, {
        "q": embedding,
        "text": question,
        "k": k,
        "session_id": session_id,
        "surql": RECALL_SURQL.strip(),
        # Filled in after the round trip; the record is written inside the same
        # statement, so the total is attached by a follow-up update below.
        "timing": {"total_ms": 0, "knn_ms": 0, "fts_ms": 0, "graph_ms": 0},
    })

    payload = result[0] if isinstance(result, list) else result
    if not payload:
        return [], ""

    hits = fuse({
        "vector": payload.get("vector") or [],
        "text": payload.get("text") or [],
        "graph": payload.get("graph") or [],
    })

    retrieval_id = record_str(payload.get("retrieval"))
    total_ms = int((time.perf_counter() - started) * 1000)

    # Timing is written after the fact because the query cannot measure its own
    # wall clock. The inspector reads it, so it has to be real rather than zero.
    await database.query("""
        UPDATE type::record($id) SET timing.total_ms = $total;
    """, {"id": retrieval_id, "total": total_ms})

    logger.info("recall: %d vector, %d text, %d graph -> %d fused in %dms",
                len(payload.get("vector") or []), len(payload.get("text") or []),
                len(payload.get("graph") or []), len(hits), total_ms)

    return hits, retrieval_id
