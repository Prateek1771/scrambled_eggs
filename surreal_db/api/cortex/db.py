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
import re
from typing import Any

from surrealdb import AsyncSurreal, RecordID

from .config import Settings
from .telemetry import tracer

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
        # The span records the statement's first line and its retry count, not
        # its parameters: those carry embeddings and user text, and this table is
        # readable by anyone with a viewer token.
        with tracer().start_as_current_span("db.query") as span:
            span.set_attribute("db.system", "surrealdb")
            span.set_attribute("db.statement.head", surql.strip().splitlines()[0][:120])
            return await self._attempt(surql, params, span)

    async def _attempt(self, surql: str, params: dict[str, Any] | None, span) -> Any:
        """Execute with conflict retry, recording how many attempts it took."""
        import random

        for attempt in range(CONFLICT_RETRIES):
            span.set_attribute("db.retries", attempt)
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

    async def query_timed(self, surql: str,
                          params: dict[str, Any] | None = None) -> tuple[Any, list[float]]:
        """Run SurrealQL and return the last result *and* every statement's duration.

        SurrealDB reports how long each statement took, in the same envelope that
        carries its result -- `{"status": "OK", "time": "1.65ms", ...}`. Ordinary
        `query()` discards that.

        It is the only way to time the arms of the hybrid recall separately: all
        three run inside one statement batch, so a client-side stopwatch can
        measure the round trip and nothing finer. These are the engine's own
        numbers, which is both more accurate and a better answer to "how long did
        the KNN take" than anything measured from outside.
        """
        response = await self._require().query_raw(surql, params or {})
        return self._last(response), self._statement_times(response)

    @staticmethod
    def _statement_times(response: dict) -> list[float]:
        """Parse each statement's reported duration into milliseconds.

        Durations arrive as human strings -- `1.65ms`, `430µs`, `2s` -- rather than
        as numbers, so they are parsed rather than cast. An unrecognised unit
        yields 0.0: a timing that cannot be read must not take down the query that
        produced it.
        """
        # "µs" is the micro sign SurrealDB actually emits; "us" is accepted
        # too so the parser survives an encoding that mangles it.
        units = {"ns": 1e-6, "µs": 1e-3, "us": 1e-3, "ms": 1.0, "s": 1000.0}
        times: list[float] = []
        for envelope in response.get("result") or []:
            raw = str(envelope.get("time", "")).strip()
            match = re.fullmatch(r"([0-9.]+)\s*(ns|µs|us|ms|s)", raw)
            times.append(float(match.group(1)) * units[match.group(2)] if match else 0.0)
        return times

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
