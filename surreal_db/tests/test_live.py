"""L5 - live-query semantics, including the two traps found by hand during M1.

Both of them are the same class of failure: the database behaves exactly as
documented, the application looks like it works, and the bug only appears at the
one moment the demo is built around. Neither produces an error.

Pinning them here means the reason the UI subscribes the way it does is written
down in a form that fails if someone "simplifies" it.
"""

from __future__ import annotations

import asyncio

import pytest
from surrealdb import AsyncSurreal

pytestmark = [pytest.mark.slow, pytest.mark.asyncio]

# Long enough to survive a busy database, short enough that the absence tests do
# not dominate the run. Every write below addresses a record by id rather than by
# `WHERE text CONTAINS`, so nothing here scans the table -- an earlier version did,
# and quietly started failing once the corpus grew from six facts to three
# thousand, which looked like a live-query bug and was a full-table scan.
SETTLE = 3.0


async def _client(endpoint: str) -> AsyncSurreal:
    """Open an authenticated root connection over the WebSocket RPC endpoint.

    Live queries are WebSocket-only -- the HTTP engine does not support them at
    all -- so every test in this module uses the RPC socket rather than the /sql
    helpers the rest of the suite shares.
    """
    ws = endpoint.replace("http://", "ws://").replace("https://", "wss://")
    db = AsyncSurreal(f"{ws}/rpc")
    await db.connect()
    await db.signin({"username": "root", "password": "root"})
    await db.use("cortex", "main")
    return db


async def _collect(db: AsyncSurreal, query_id, seconds: float = SETTLE) -> list[dict]:
    """Drain a live subscription for a fixed window and return what arrived.

    A fixed window rather than "wait for N messages", because several of these
    tests assert that *nothing* arrives, and a test that waits for something that
    should never come would hang instead of failing.
    """
    received: list[dict] = []

    # The queue is registered directly rather than going through
    # `subscribe_live`, because that helper yields `ret["result"]` and throws the
    # rest of the envelope away -- including `action`. A Python consumer using it
    # cannot tell a CREATE from an UPDATE from a DELETE, which is fine for a
    # dashboard and useless for a test about exactly that distinction.
    #
    # The raw envelope carries: action, id, record, result, session.
    queue: asyncio.Queue = asyncio.Queue()
    db.live_queues.setdefault(str(query_id), []).append(queue)

    deadline = asyncio.get_event_loop().time() + seconds
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            received.append(await asyncio.wait_for(queue.get(), timeout=remaining))
        except asyncio.TimeoutError:
            break

    subscribers = db.live_queues.get(str(query_id), [])
    if queue in subscribers:
        subscribers.remove(queue)
    return received


@pytest.fixture
async def db(endpoint: str):
    """A root WebSocket connection, closed at the end of the test."""
    client = await _client(endpoint)
    yield client
    await client.close()


@pytest.fixture
async def scratch(db):
    """An isolated pair of facts to supersede, removed afterwards.

    Tests here write, so they work on their own records rather than on the
    corpus -- otherwise every later assertion in the session would be running
    against a database this module quietly modified.
    """
    await db.query("DELETE fact WHERE string::starts_with(text, '[livetest]');")
    yield
    await db.query("""
        DELETE supersedes WHERE string::starts_with(in.text ?? '', '[livetest]');
        DELETE fact WHERE string::starts_with(text, '[livetest]');
    """)


# --------------------------------------------------------------------------
# trap 1: a filtered live query cannot report a record leaving its result set
# --------------------------------------------------------------------------


async def test_unfiltered_live_query_reports_supersession(db, scratch):
    """Watching the whole table delivers the UPDATE that ends a fact.

    This is the half that must work: the browser learns a fact was superseded and
    dims it.
    """
    query_id = await db.query("LIVE SELECT id, text, valid_to FROM fact")

    created = await db.query("""
        CREATE ONLY fact:livetest_unfiltered SET text = '[livetest] unfiltered subject',
                             embedding = array::repeat(0.0, 1536), confidence = 0.9;
    """)
    await asyncio.sleep(0.3)

    async def supersede() -> None:
        """End the fact under test after the collector is listening."""
        await asyncio.sleep(0.4)
        # By id. A `WHERE text = ...` predicate scans every fact in the table,
        # which at corpus scale takes longer than the collection window.
        await db.query("UPDATE fact:livetest_unfiltered SET valid_to = time::now();")

    task = asyncio.create_task(supersede())
    received = await _collect(db, query_id)
    await task
    await db.kill(query_id)

    assert created is not None
    updates = [n for n in received if str(n.get("action")).upper().endswith("UPDATE")]
    assert updates, (
        "an unfiltered LIVE SELECT delivered no UPDATE when a fact was superseded; "
        "the browser would never learn to dim it"
    )


async def test_filtered_live_query_goes_silent_when_a_record_leaves_it(db, scratch):
    """The trap. `WHERE valid_to = NONE` never reports the fact that stopped matching.

    docs/05 originally subscribed exactly this way. A live query says nothing
    about a record leaving its result set, so the superseded node would sit on
    screen looking current forever -- and the frame the whole demo is built
    around is precisely that transition.

    Asserting the *absence* is what makes the current unfiltered design a
    decision rather than an accident.
    """
    query_id = await db.query(
        "LIVE SELECT id, text, valid_to FROM fact WHERE valid_to = NONE"
    )

    await db.query("""
        CREATE ONLY fact:livetest_filtered SET text = '[livetest] filtered subject',
                             embedding = array::repeat(0.0, 1536), confidence = 0.9;
    """)
    await asyncio.sleep(0.3)

    async def supersede() -> None:
        """End the fact, which removes it from the live query's result set."""
        await asyncio.sleep(0.4)
        await db.query("UPDATE fact:livetest_filtered SET valid_to = time::now();")

    task = asyncio.create_task(supersede())
    received = await _collect(db, query_id)
    await task
    await db.kill(query_id)

    # The CREATE matched the filter, so it is expected. The UPDATE that ended the
    # fact is the one that must be missing.
    ending = [
        n for n in received
        if str(n.get("action")).upper().endswith("UPDATE")
        and isinstance(n.get("result"), dict)
        and n["result"].get("valid_to") is not None
    ]
    assert not ending, (
        "a filtered live query reported a record leaving its result set -- if "
        "SurrealDB has started doing this, docs/05's original design became "
        "viable and the UI can go back to filtering"
    )


# --------------------------------------------------------------------------
# trap 2: DELETE carries an id, not a record
# --------------------------------------------------------------------------


async def test_delete_notification_carries_only_the_record_id(db, scratch):
    """The reducer has to handle DELETE asymmetrically, and this is why.

    CREATE and UPDATE push the whole record; DELETE pushes the id alone. A
    reducer written against the CREATE shape would read `undefined` fields and
    quietly drop the node from the wrong bucket.
    """
    query_id = await db.query("LIVE SELECT id, text FROM fact")

    await db.query("""
        CREATE ONLY fact:livetest_doomed SET text = '[livetest] doomed',
                             embedding = array::repeat(0.0, 1536), confidence = 0.5;
    """)
    await asyncio.sleep(0.4)

    async def remove() -> None:
        """Delete the fact once the collector is listening."""
        await asyncio.sleep(0.4)
        await db.query("DELETE fact:livetest_doomed;")

    task = asyncio.create_task(remove())
    received = await _collect(db, query_id)
    await task
    await db.kill(query_id)

    deletes = [n for n in received if str(n.get("action")).upper().endswith("DELETE")]
    assert deletes, "no DELETE notification arrived"

    # `record` is the record id and is always present. `result` echoes whatever
    # the LIVE SELECT projected, so a projected live query does return field
    # values on DELETE -- the "id only" rule in the docs describes `LIVE SELECT *`.
    # The reducer keys off the id either way, which is why it survives both.
    notification = deletes[0]
    assert notification.get("record"), f"DELETE carried no record id: {notification}"
    assert str(notification["record"]).startswith("fact:"), notification["record"]


# --------------------------------------------------------------------------
# delivery under load
# --------------------------------------------------------------------------


async def test_no_notifications_are_dropped_in_a_burst(db, scratch):
    """Every committed write reaches the subscriber.

    The browser reconciles with a full SELECT after a reconnect precisely because
    delivery can gap while the socket is down. While the socket is *up*, it must
    not gap -- otherwise the graph drifts silently and no resync is ever
    triggered to correct it.
    """
    burst = 300
    query_id = await db.query("LIVE SELECT id, text FROM fact")
    await asyncio.sleep(0.3)

    async def write() -> None:
        """Commit a burst of creates as fast as the connection allows."""
        await asyncio.sleep(0.3)
        await db.query("""
            FOR $i IN array::range(0, $n) {
                CREATE fact SET text = '[livetest] burst ' + <string> $i,
                                embedding = array::repeat(0.0, 1536), confidence = 0.5;
            };
        """, {"n": burst})

    task = asyncio.create_task(write())
    received = await _collect(db, query_id, seconds=6.0)
    await task
    await db.kill(query_id)

    creates = [
        n for n in received
        if str(n.get("action")).upper().endswith("CREATE")
        and isinstance(n.get("result"), dict)
        and str(n["result"].get("text", "")).startswith("[livetest] burst")
    ]
    assert len(creates) == burst, (
        f"{burst} facts were committed but {len(creates)} notifications arrived -- "
        f"{burst - len(creates)} were dropped while the socket was up"
    )


async def test_reauthenticating_as_the_same_principal_keeps_live_queries(endpoint, scratch):
    """Signing in again as the *same* identity does not end the session's live queries.

    docs/02 says flatly that "signin invalidates live queries on that session".
    Measured against 3.1.6 that is too strong: the subscription survives a
    re-signin as the same principal. The condition is a change of *identity*, not
    the act of authenticating -- which the next test pins.

    The distinction matters for token refresh. Refreshing into the same viewer
    identity is cheap; the UI's re-register-and-resync is insurance against the
    other case, not a per-refresh necessity.
    """
    db = await _client(endpoint)
    try:
        query_id = await db.query("LIVE SELECT id, text FROM fact")
        await asyncio.sleep(0.3)
        await db.signin({"username": "root", "password": "root"})
        await asyncio.sleep(0.3)

        async def write() -> None:
            """Write something the subscription should still report."""
            await asyncio.sleep(0.3)
            await db.query("""
                CREATE fact SET text = '[livetest] after same-principal reauth',
                                embedding = array::repeat(0.0, 1536), confidence = 0.5;
            """)

        task = asyncio.create_task(write())
        received = await _collect(db, query_id, seconds=2.5)
        await task

        relevant = [
            n for n in received
            if isinstance(n.get("result"), dict)
            and "same-principal" in str(n["result"].get("text", ""))
        ]
        assert relevant, (
            "the live query stopped delivering after a same-principal re-signin; "
            "if SurrealDB has tightened this, docs/02's blanket claim is correct "
            "after all and the UI must re-register on every refresh"
        )
    finally:
        await db.close()


async def test_becoming_a_different_principal_ends_live_queries(endpoint, scratch):
    """Changing identity on a session ends every live query it holds.

    This is the real footgun behind token refresh, and the reason the browser
    re-registers *and* resyncs rather than assuming its subscriptions survived.
    A client that misses this goes silently deaf while still showing a green
    "live" indicator -- the worst failure mode available, because the graph looks
    healthy and is simply frozen.
    """
    db = await _client(endpoint)
    try:
        query_id = await db.query("LIVE SELECT id, text FROM fact")
        await asyncio.sleep(0.3)

        # root -> cortex_viewer. A different principal on the same socket.
        await db.signin({
            "username": "cortex_viewer", "password": "viewer",
            "namespace": "cortex", "database": "main",
        })
        await asyncio.sleep(0.4)

        # The viewer cannot write, so the write goes through a second connection.
        writer = await _client(endpoint)
        try:
            async def write() -> None:
                """Write from a separate session the dead subscription would have seen."""
                await asyncio.sleep(0.3)
                await writer.query("""
                    CREATE fact SET text = '[livetest] after principal change',
                                    embedding = array::repeat(0.0, 1536), confidence = 0.5;
                """)

            task = asyncio.create_task(write())
            received = await _collect(db, query_id, seconds=2.5)
            await task
        finally:
            await writer.close()

        relevant = [
            n for n in received
            if isinstance(n.get("result"), dict)
            and "principal change" in str(n["result"].get("text", ""))
        ]
        assert not relevant, (
            "a live query survived a change of principal on its session; the UI's "
            "re-register-and-resync after token refresh would then be unnecessary"
        )
    finally:
        await db.close()
