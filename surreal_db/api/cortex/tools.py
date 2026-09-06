"""The five memory tools the model can call while reasoning.

Each maps to one SurrealQL statement. Two of them -- `why` and `timeline` -- are
the ones no vector store can offer, and they are what make the demo land: you can
ask the agent to justify itself and it produces the actual sentence you said,
or the whole history of what it used to believe.
"""

from __future__ import annotations

import logging

from langchain_core.tools import StructuredTool

from .db import Database, record_str

logger = logging.getLogger("cortex.tools")


def build_tools(database: Database, embed) -> list[StructuredTool]:
    """Create the tool set, closed over the database and embedding client.

    A factory rather than module-level tools because each needs a live connection,
    and passing that through LangChain's tool arguments would expose it to the
    model.
    """

    async def recall_semantic(query: str, k: int = 8) -> str:
        """Find facts that mean something similar to the query.

        Use when you need things that are *about* a topic, and the exact words do
        not matter.
        """
        vector = (await embed.aembed_documents([query]))[0]
        rows = await database.query("""
            SELECT text, 1 - vector::distance::knn() AS score
            FROM fact WHERE embedding <|$k,64|> $q AND valid_to = NONE;
        """.replace("$k", str(int(k))), {"q": vector})
        return _format(rows, "nothing semantically similar is remembered")

    async def recall_exact(phrase: str, k: int = 8) -> str:
        """Find facts containing an exact word or identifier.

        Use for names, ticket numbers, error codes, versions -- anything where the
        precise string matters and a paraphrase would be wrong.
        """
        rows = await database.query("""
            SELECT text, search::score(1) AS score
            FROM fact WHERE text @1@ $phrase AND valid_to = NONE
            ORDER BY score DESC LIMIT $k;
        """, {"phrase": phrase, "k": int(k)})
        return _format(rows, f"no remembered fact contains {phrase!r}")

    async def explore(entity: str, hops: int = 2) -> str:
        """Walk outward from an entity to what it is connected to.

        Use to answer questions about relationships -- who works with whom, what
        depends on what -- where the answer is a path rather than a document.
        """
        depth = max(1, min(int(hops), 3))
        # Each depth is unioned explicitly. `.{1..n}` alone returns only the
        # nodes at the deepest completed depth, silently dropping every
        # intermediate neighbour -- see docs/03-data-model.md.
        rows = await database.query("""
            LET $start = (SELECT VALUE id FROM entity WHERE name = $entity LIMIT 1);
            LET $reached = array::distinct(array::concat(
                array::flatten($start->relates->entity),
                array::flatten($start.{1..DEPTH}->relates->entity)
            ));
            SELECT name, kind FROM $reached;
        """.replace("DEPTH", str(depth)), {"entity": entity})
        if not rows:
            return f"{entity!r} is not connected to anything remembered"
        listing = ", ".join(f"{row['name']} ({row['kind']})" for row in rows)
        return f"within {depth} hop(s) of {entity}: {listing}"

    async def why(fact_id: str) -> str:
        """Show where a remembered fact came from, and what it replaced.

        Use when asked to justify something, or when a fact seems wrong and you
        need to see the source.
        """
        rows = await database.query("""
            SELECT text,
                   ->derived_from->message.{ text, created_at, role } AS sources,
                   ->supersedes->fact.{ text, valid_from, valid_to } AS replaced
            FROM type::record($id);
        """, {"id": fact_id})
        if not rows:
            return f"no fact with id {fact_id}"

        row = rows[0]
        parts = [f"fact: {row['text']}"]
        for source in row.get("sources") or []:
            parts.append(f"  said by {source.get('role')}: {source.get('text')!r}")
        for replaced in row.get("replaced") or []:
            parts.append(f"  replaced: {replaced.get('text')!r}")
        if len(parts) == 1:
            parts.append("  no recorded provenance")
        return "\n".join(parts)

    async def timeline(entity: str) -> str:
        """Show everything believed about an entity over time, current and past.

        Use for questions about the past -- "where did she work before?" -- where
        the superseded version is the correct answer, not a stale one.
        """
        rows = await database.query("""
            LET $e = (SELECT VALUE id FROM entity WHERE name = $entity LIMIT 1);
            SELECT text, valid_from, valid_to, is_current
            FROM fact WHERE ->mentions->entity CONTAINSANY $e
            ORDER BY valid_from;
        """, {"entity": entity})
        if not rows:
            return f"nothing is remembered about {entity!r}"

        lines = []
        for row in rows:
            marker = "now" if row.get("is_current") else f"until {row.get('valid_to')}"
            lines.append(f"  [{marker}] {row['text']}")
        return f"timeline for {entity}:\n" + "\n".join(lines)

    def _format(rows, empty: str) -> str:
        """Render recall results, or say plainly that there were none.

        Returning prose rather than JSON: the model reads this, and an empty
        result stated in words is far less likely to be hallucinated over than an
        empty array.
        """
        if not rows:
            return empty
        return "\n".join(f"  ({row.get('score', 0):.2f}) {row['text']}" for row in rows)

    return [
        StructuredTool.from_function(coroutine=recall_semantic, name="recall_semantic",
                                     description=recall_semantic.__doc__),
        StructuredTool.from_function(coroutine=recall_exact, name="recall_exact",
                                     description=recall_exact.__doc__),
        StructuredTool.from_function(coroutine=explore, name="explore",
                                     description=explore.__doc__),
        StructuredTool.from_function(coroutine=why, name="why",
                                     description=why.__doc__),
        StructuredTool.from_function(coroutine=timeline, name="timeline",
                                     description=timeline.__doc__),
    ]
