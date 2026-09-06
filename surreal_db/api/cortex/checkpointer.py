"""LangGraph persistence backed by SurrealDB.

LangGraph users reach for Postgres by default. There is no SurrealDB
checkpointer, which is a gap worth closing on its own: this file has no
dependency on the rest of CORTEX and is the piece most likely to be useful to
somebody else.

Two things it gets right that are easy to get wrong:

  * `parent_checkpoint_id` is stored, so threads form a *tree* rather than a
    list. That is what LangGraph's time travel and branching need, and
    retrofitting it later means rewriting every stored checkpoint.
  * The serialiser is msgpack-backed despite its name, and `dumps_typed` returns
    a `(type_tag, bytes)` pair. Both halves are stored -- dropping the tag makes
    the bytes unreadable, and the failure appears only on resume.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from .db import Database, one

logger = logging.getLogger("cortex.checkpointer")


class SurrealCheckpointSaver(BaseCheckpointSaver):
    """Store LangGraph checkpoints and pending writes in SurrealDB.

    Uses the `checkpoint` and `checkpoint_write` tables from db/schema.surql.
    Bytes round-trip natively through the Python SDK into `TYPE bytes`, so no
    base64 layer is needed -- verified before this was written, because the
    fallback would have been a schema change.
    """

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.db = database

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _identity(config: RunnableConfig) -> tuple[str, str, str | None]:
        """Pull thread id, checkpoint namespace and checkpoint id out of a config.

        The namespace defaults to the empty string rather than None: LangGraph
        uses "" for the top-level graph, and a NULL here would make every lookup
        in a subgraph miss.
        """
        configurable = config.get("configurable", {})
        return (
            str(configurable["thread_id"]),
            str(configurable.get("checkpoint_ns", "")),
            configurable.get("checkpoint_id"),
        )

    def _to_tuple(self, row: dict, pending: list[tuple[str, str, Any]]) -> CheckpointTuple:
        """Rebuild a CheckpointTuple from a stored row and its pending writes."""
        config: RunnableConfig = {"configurable": {
            "thread_id": row["thread_id"],
            "checkpoint_ns": row.get("checkpoint_ns", ""),
            "checkpoint_id": row["checkpoint_id"],
        }}
        parent_config: RunnableConfig | None = None
        if row.get("parent_id"):
            parent_config = {"configurable": {
                "thread_id": row["thread_id"],
                "checkpoint_ns": row.get("checkpoint_ns", ""),
                "checkpoint_id": row["parent_id"],
            }}

        return CheckpointTuple(
            config=config,
            checkpoint=self.serde.loads_typed((row["type"], bytes(row["state"]))),
            metadata=self.serde.loads_typed((row["metadata_type"], bytes(row["metadata"]))),
            parent_config=parent_config,
            pending_writes=pending,
        )

    async def _pending_writes(self, thread_id: str, namespace: str,
                              checkpoint_id: str) -> list[tuple[str, str, Any]]:
        """Load the writes recorded against one checkpoint, in their original order.

        Ordering is by `idx`, not by insertion: LangGraph replays these to
        reconstruct partial progress, and out-of-order replay silently produces a
        different state.
        """
        rows = await self.db.query("""
            SELECT task_id, channel, type, value, idx FROM checkpoint_write
            WHERE thread_id = $thread AND checkpoint_ns = $ns AND checkpoint_id = $ckpt
            ORDER BY idx;
        """, {"thread": thread_id, "ns": namespace, "ckpt": checkpoint_id})

        return [
            (row["task_id"], row["channel"],
             self.serde.loads_typed((row["type"], bytes(row["value"]))))
            for row in (rows or [])
        ]

    # -- the interface ----------------------------------------------------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Fetch one checkpoint: the named one, or the latest for the thread."""
        thread_id, namespace, checkpoint_id = self._identity(config)

        if checkpoint_id:
            rows = await self.db.query("""
                SELECT * FROM checkpoint
                WHERE thread_id = $thread AND checkpoint_ns = $ns
                  AND checkpoint_id = $ckpt
                LIMIT 1;
            """, {"thread": thread_id, "ns": namespace, "ckpt": checkpoint_id})
        else:
            rows = await self.db.query("""
                SELECT * FROM checkpoint
                WHERE thread_id = $thread AND checkpoint_ns = $ns
                ORDER BY created_at DESC LIMIT 1;
            """, {"thread": thread_id, "ns": namespace})

        row = one(rows)
        if not row:
            return None
        pending = await self._pending_writes(thread_id, namespace, row["checkpoint_id"])
        return self._to_tuple(row, pending)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 - name fixed by the interface
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """Walk a thread's checkpoints, newest first.

        `before` is resolved by `created_at` rather than by id, because
        checkpoint ids are UUIDs and carry no order.
        """
        thread_id, namespace, _ = self._identity(config or {"configurable": {}})

        clauses = ["thread_id = $thread", "checkpoint_ns = $ns"]
        params: dict[str, Any] = {"thread": thread_id, "ns": namespace}

        if before is not None:
            _, _, before_id = self._identity(before)
            if before_id:
                params["before"] = before_id
                clauses.append(
                    "created_at < (SELECT VALUE created_at FROM ONLY checkpoint "
                    "WHERE thread_id = $thread AND checkpoint_ns = $ns "
                    "AND checkpoint_id = $before LIMIT 1)"
                )

        statement = (f"SELECT * FROM checkpoint WHERE {' AND '.join(clauses)} "
                     f"ORDER BY created_at DESC")
        if limit:
            statement += f" LIMIT {int(limit)}"

        rows = await self.db.query(statement + ";", params) or []
        for row in rows:
            # `filter` matches against checkpoint metadata, which is stored
            # serialised -- so it is deserialised and compared here rather than
            # pushed into the query.
            metadata = self.serde.loads_typed((row["metadata_type"], bytes(row["metadata"])))
            if filter and not all(metadata.get(key) == value for key, value in filter.items()):
                continue
            pending = await self._pending_writes(thread_id, namespace, row["checkpoint_id"])
            yield self._to_tuple(row, pending)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Write a checkpoint and return the config that identifies it.

        `UPSERT` keyed on (thread, namespace, checkpoint id) rather than `CREATE`:
        LangGraph can write the same checkpoint id twice within a superstep, and a
        plain create would collide with the unique index.
        """
        thread_id, namespace, parent_id = self._identity(config)
        checkpoint_id = checkpoint["id"]

        state_type, state_bytes = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_bytes = self.serde.dumps_typed(dict(metadata))

        await self.db.query("""
            UPSERT checkpoint
            SET thread_id = $thread, checkpoint_ns = $ns, checkpoint_id = $ckpt,
                parent_id = $parent, type = $state_type, state = $state,
                metadata_type = $metadata_type, metadata = $metadata,
                created_at = time::now()
            WHERE thread_id = $thread AND checkpoint_ns = $ns AND checkpoint_id = $ckpt;
        """, {
            "thread": thread_id, "ns": namespace, "ckpt": checkpoint_id,
            "parent": parent_id, "state_type": state_type, "state": state_bytes,
            "metadata_type": metadata_type, "metadata": metadata_bytes,
        })

        return {"configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": namespace,
            "checkpoint_id": checkpoint_id,
        }}

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Record the writes a task produced against the current checkpoint.

        Written in one statement rather than one per write: a task can emit
        dozens, and a round trip each would make every superstep slower than the
        model call it is bookkeeping for.
        """
        thread_id, namespace, checkpoint_id = self._identity(config)
        if not writes:
            return

        rows = []
        for index, (channel, value) in enumerate(writes):
            value_type, value_bytes = self.serde.dumps_typed(value)
            rows.append({
                "thread_id": thread_id, "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint_id, "task_id": task_id,
                "task_path": task_path, "idx": index, "channel": channel,
                "type": value_type, "value": value_bytes,
            })

        await self.db.query("""
            FOR $w IN $rows {
                CREATE checkpoint_write SET
                    thread_id = $w.thread_id, checkpoint_ns = $w.checkpoint_ns,
                    checkpoint_id = $w.checkpoint_id, task_id = $w.task_id,
                    task_path = $w.task_path, idx = $w.idx, channel = $w.channel,
                    type = $w.type, value = $w.value;
            };
        """, {"rows": rows})

    async def adelete_thread(self, thread_id: str) -> None:
        """Remove a thread's checkpoints and their writes.

        The only place in CORTEX where deletion is correct: conversation
        scaffolding is not memory, and a thread the user discarded should leave
        nothing behind. The facts it produced remain -- those are memory, and
        memory is never deleted.
        """
        await self.db.query("""
            DELETE checkpoint_write WHERE thread_id = $thread;
            DELETE checkpoint WHERE thread_id = $thread;
        """, {"thread": thread_id})
