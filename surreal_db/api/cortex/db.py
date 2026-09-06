"""The database connection, and the SurrealQL idioms every module here needs.

One long-lived WebSocket held for the process lifetime. WebSocket rather than
HTTP because a single turn issues many small queries -- perceive writes, recall
reads, consolidate writes several times -- and a stateful connection avoids
paying a handshake for each one.

The three helpers at the bottom exist because SurrealDB's type coercion is
stricter than it looks, and each of these cost an hour the first time:

  * a record id sent as a JSON string stays a string, and fails whatever it is
    handed to, usually with a message that names nothing;
  * a datetime sent as an ISO string is not a datetime;
  * `CREATE` returns an array unless you ask it not to.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from surrealdb import AsyncSurreal, RecordID

from .config import Settings

logger = logging.getLogger("cortex.db")


# How many times a conflicted transaction is retried before the turn fails.
# Five is enough to survive the contention `tests/test_concurrency.py` measures
# at fifty concurrent writers, where 2.4 retries per commit is normal.
CONFLICT_RETRIES = 5


def _is_retryable(message: str) -> bool:
    """Whether a statement failure is a transient conflict rather than a real error.

    SurrealDB reports these as "Transaction conflict ... can be retried", and a
    batch reports a generic "not executed" for its *other* statements -- so the
    whole message is searched rather than just the first line.
    """
    lowered = message.lower()
    return ("conflict" in lowered or "can be retried" in lowered
            or "resource busy" in lowered)


class SurrealQueryError(RuntimeError):
    """A statement was rejected by the database.

    Distinct from a transport failure, because the two want opposite handling:
    reconnecting fixes a dead socket and cannot fix a bad cast.
    """


class Database:
    """A reconnecting SurrealDB connection shared by the whole process.

    Not a pool. SurrealDB multiplexes requests over one socket, and a single
    connection keeps live-query and transaction semantics simple to reason about.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncSurreal | None = None
        # Serialises reconnection, so a burst of failing queries triggers one
        # reconnect rather than one per caller.
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the socket, authenticate, and select the namespace and database.

        Called at startup so a bad credential or an unreachable database stops the
        container rather than surfacing on a user's first message.
        """
        async with self._lock:
            await self._open()

    async def _open(self) -> None:
        """Establish a fresh authenticated client. Caller must hold the lock."""
        client = AsyncSurreal(self._settings.surreal_url)
        await client.connect()
        await client.signin({
            "username": self._settings.root_user,
            "password": self._settings.root_pass,
        })
        await client.use(self._settings.namespace, self._settings.database)
        # A strong reference is kept deliberately: the SDK holds its reader task
        # weakly, so a client that goes out of scope can have its socket
        # collected out from under it.
        self._client = client
        logger.info("connected to %s", self._settings.surreal_url)

    async def close(self) -> None:
        """Close the socket, ignoring a connection that is already gone."""
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:  # noqa: BLE001 - shutdown errors are not actionable
                    pass
                self._client = None

    async def query(self, surql: str, params: dict[str, Any] | None = None) -> Any:
        """Run SurrealQL and return the **last** statement's result.

        Goes through `query_raw` rather than the SDK's `query()`. That helper
        returns `response["result"][0]` -- the result of the *first* statement --
        so a batch shaped `LET ...; LET ...; RETURN ...` yields the first LET's
        empty result and the RETURN is silently discarded. Every multi-statement
        query in CORTEX is shaped exactly that way, and the symptom is a `None`
        that only fails several calls later, somewhere else.

        Three failure modes, three responses:

        * **Transaction conflict** -- retried with backoff. SurrealDB says so
          itself ("can be retried"), and it happens whenever two turns touch the
          same records at once. Failing the user's turn over a conflict the
          database expected us to retry would be a bug.
        * **Any other query error** -- bad syntax, a failed cast, a constraint --
          raised immediately. Retrying a rejected write is how one bad statement
          becomes two.
        * **Transport failure** -- reconnect once, then retry.
        """
        import random

        for attempt in range(CONFLICT_RETRIES):
            try:
                return self._last(await self._require().query_raw(surql, params or {}))
            except SurrealQueryError as error:
                if not _is_retryable(str(error)) or attempt == CONFLICT_RETRIES - 1:
                    raise
                # Jittered backoff, so concurrent turns do not retry in lockstep
                # and collide again immediately.
                await asyncio.sleep(0.02 * (2 ** attempt) * (0.5 + random.random()))
            except Exception as first:  # noqa: BLE001 - transport failure; reconnect and retry
                if attempt == CONFLICT_RETRIES - 1:
                    raise
                logger.warning("connection failed (%s); reconnecting", type(first).__name__)
                async with self._lock:
                    await self.close_unlocked()
                    await self._open()
        raise RuntimeError("unreachable")

    @staticmethod
    def _last(response: dict) -> Any:
        """Return the final statement's result, raising if any statement failed.

        Every statement carries its own status, and a batch can partially fail
        while the transport call succeeds -- so trusting the envelope alone hides
        real errors.
        """
        statements = response.get("result") or []
        for index, envelope in enumerate(statements):
            if envelope.get("status") != "OK":
                raise SurrealQueryError(
                    f"statement {index + 1}/{len(statements)} failed: {envelope.get('result')}"
                )
        return statements[-1].get("result") if statements else None

    async def close_unlocked(self) -> None:
        """Close without taking the lock, for callers that already hold it."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _require(self) -> AsyncSurreal:
        """Return the live client, or explain that nobody called connect()."""
        if self._client is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._client


def record(value: Any) -> RecordID | None:
    """Turn an id into a RecordID, whatever shape it arrived in.

    Record ids come back from queries as `RecordID` objects but cross JSON, HTTP
    and LLM output as plain strings like `"fact:abc"`. Sending the string back
    where a record is expected fails the entire transaction with a generic
    message, so every boundary converts here instead of hoping.
    """
    if value is None:
        return None
    if isinstance(value, RecordID):
        return value
    text = str(value)
    if ":" not in text:
        return None
    table, _, identifier = text.partition(":")
    return RecordID(table, identifier)


def record_str(value: Any) -> str:
    """Render a record id as `table:id`, for comparison and for the UI."""
    if isinstance(value, RecordID):
        return f"{value.table_name}:{value.id}"
    return str(value)


def one(result: Any) -> Any:
    """Unwrap a result that may be a single record or a one-element list.

    `CREATE` returns an array unless it is `CREATE ONLY`, and `SELECT ... FROM
    ONLY` returns a bare record. Rather than remember which is which at every
    call site, callers pass the result through here.
    """
    if isinstance(result, list):
        return result[0] if result else None
    return result


def scalar(result: Any, default: int = 0) -> int:
    """Unwrap a `count() ... GROUP ALL`, which is an int or a row depending on version."""
    if not result:
        return default
    first = result[0] if isinstance(result, list) else result
    if isinstance(first, dict):
        return int(first.get("count", default))
    return int(first)
