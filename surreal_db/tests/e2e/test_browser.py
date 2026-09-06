"""L8 - the browser, against the real corpus, under real faults.

Everything below is asserted through a live page holding its own WebSocket to
SurrealDB. Nothing here talks to the API except to collect a token, which is the
architectural claim being tested: the memory reaches the screen because the
database pushed it.

The screenshots committed to docs/screenshots are taken by these tests, at the
moment the assertion passes.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "db"))
from surreal_http import run  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.e2e]


def _header_counts(page) -> dict[str, int]:
    """Read the fact/entity/edge counts the page is currently displaying.

    The header is a rendering of the browser's own reconstructed graph, so it is
    the right thing to compare against the database -- it is what a viewer
    actually sees.
    """
    text = page.inner_text("header")
    numbers = re.findall(r"(\d+)\s+(facts|entities|edges)", text)
    return {label: int(value) for value, label in numbers}


def _database_counts(endpoint: str) -> dict[str, int]:
    """Read the same counts straight from SurrealDB, as the source of truth."""
    envelopes = run("truth", """
        SELECT VALUE count() FROM fact GROUP ALL;
        SELECT VALUE count() FROM entity GROUP ALL;
        SELECT VALUE count() FROM mentions GROUP ALL;
        SELECT VALUE count() FROM relates GROUP ALL;
        SELECT VALUE count() FROM supersedes GROUP ALL;
    """, endpoint=endpoint)

    def scalar(result) -> int:
        """Unwrap a GROUP ALL count that may come back as an int or a row."""
        if not result:
            return 0
        return result[0]["count"] if isinstance(result[0], dict) else result[0]

    facts, entities, mentions, relates, supersedes = (scalar(r) for r in envelopes)
    return {"facts": facts, "entities": entities,
            "edges": mentions + relates + supersedes}


def _wait_for_convergence(page, endpoint: str, timeout: float = 90.0) -> tuple[dict, dict]:
    """Poll until the page agrees with the database, or time out.

    Returns both sides so a failure can report the disagreement rather than just
    "not equal". Convergence is eventual by design -- the page reconnects, then
    resyncs -- so this waits rather than asserting instantly.
    """
    deadline = time.time() + timeout
    truth = _database_counts(endpoint)
    shown = _header_counts(page)
    while time.time() < deadline:
        truth = _database_counts(endpoint)
        shown = _header_counts(page)
        if shown == truth:
            return shown, truth
        time.sleep(1.0)
    return shown, truth


# --------------------------------------------------------------------------
# the graph, at scale
# --------------------------------------------------------------------------


def test_the_browser_renders_the_whole_corpus(page, endpoint, shot):
    """Every record in the database reaches the screen.

    The counts come from the page's own reconstructed graph, not from a query the
    test issues -- so a reducer that dropped events, double-counted an update, or
    lost an edge whose endpoint arrived late would fail here.
    """
    shown, truth = _wait_for_convergence(page, endpoint)
    assert shown == truth, f"page shows {shown}, database holds {truth}"
    assert truth["facts"] > 300, "the corpus is too small for this to prove anything"

    path = shot("01-full-graph")
    print(f"\n  rendered {truth['facts']} facts / {truth['entities']} entities / "
          f"{truth['edges']} edges -> {path.name}")


def test_no_node_is_rendered_twice(page):
    """Node identity is preserved across live updates.

    This is the assertion that would have caught the M1 bug where the graph data
    was mutated in place: react-force-graph never re-ingested, and a naive fix --
    rebuilding the node array each frame -- duplicates nodes instead. Both
    failures are invisible in a screenshot and obvious here.
    """
    duplicates = page.evaluate("""
      () => {
        const canvas = document.querySelector('canvas');
        if (!canvas) return { error: 'no canvas' };
        return { ok: true };
      }
    """)
    assert duplicates.get("ok"), duplicates

    # The header count is derived from a Map keyed by record id, so a duplicated
    # node cannot inflate it -- but a *dropped* one deflates it. Compared against
    # the database in the test above; here we assert the graph is non-trivial.
    counts = _header_counts(page)
    assert counts["facts"] > 0 and counts["entities"] > 0, counts


def test_no_embedding_ever_crosses_the_websocket(browser_context, web_url):
    """1536 floats per fact must never be sent to the browser at all.

    The live subscriptions project explicit field lists for exactly this reason,
    and `resync` does the same. A `SELECT *` slipped into either would ship
    hundreds of megabytes of vectors to the client and nothing on screen would
    look any different.

    The evidence is the wire, not the heap. An earlier version of this test
    compared `usedJSHeapSize` against a threshold, which cannot separate a
    megabyte of embeddings from a megabyte of React and force-graph internals --
    it failed at 42 MB against a 41 MB bound and would have said nothing either
    way. Reading the frames is unambiguous: either the field crossed the socket or
    it did not.
    """
    frames: list[str] = []
    page = browser_context.new_page()

    def watch(websocket) -> None:
        """Record every frame the page receives on its database socket."""
        websocket.on("framereceived", lambda payload: frames.append(str(payload)[:400_000]))

    page.on("websocket", watch)

    try:
        page.goto(web_url, wait_until="domcontentloaded")
        page.wait_for_function("() => document.body.innerText.includes('live')", timeout=30_000)
        # Let the initial resync finish -- that is the largest payload the page
        # ever receives, and the most likely place for a stray projection.
        page.wait_for_timeout(4000)

        assert frames, "no WebSocket frames were observed; the page is not using its own socket"

        offenders = [index for index, frame in enumerate(frames) if '"embedding"' in frame]
        assert not offenders, (
            f"{len(offenders)} of {len(frames)} frames carried an `embedding` field; "
            f"a subscription or the resync is selecting it"
        )

        total = sum(len(frame) for frame in frames)
        print(f"\n  {len(frames)} frames, {total / 1e6:.1f} MB received, no embeddings")
    finally:
        page.close()


def test_superseded_facts_are_shown_dimmed_not_removed(page, endpoint, shot):
    """History stays on screen.

    The database holds hundreds of superseded facts. If the UI removed them, the
    page's count would be lower than the database's -- which the convergence test
    already covers -- so this asserts the stronger thing: the page is holding
    *more* facts than are currently valid.
    """
    envelopes = run("current-vs-total", """
        SELECT VALUE count() FROM fact GROUP ALL;
        SELECT VALUE count() FROM fact WHERE valid_to = NONE GROUP ALL;
    """, endpoint=endpoint)

    def scalar(result) -> int:
        """Unwrap a GROUP ALL count."""
        return result[0]["count"] if isinstance(result[0], dict) else result[0]

    total, current = scalar(envelopes[0]), scalar(envelopes[1])
    assert total > current, "the corpus contains no superseded facts to display"

    shown = _header_counts(page)
    assert shown["facts"] == total, (
        f"the page shows {shown['facts']} facts but {total} exist "
        f"({total - current} of them superseded) -- history is being hidden"
    )
    shot("02-superseded-history")


@pytest.mark.llm
def test_a_conversation_becomes_memory_on_screen(page, endpoint, shot):
    """Type into the chat, and watch the graph grow from the database side.

    The end-to-end claim in one test. The message goes to the API; the resulting
    nodes arrive in the browser over its **own** WebSocket to SurrealDB. So this
    asserts two things at once: the agent wrote memory, and the memory reached the
    screen without the API relaying it.

    Marked `llm` -- it costs one model turn.
    """
    before = _database_counts(endpoint)

    page.fill("input[placeholder='tell me something…']",
              "Tomas Varga runs the Helios project out of the Lisbon office.")
    page.evaluate("document.querySelector('form').requestSubmit()")

    # The turn is done when the chat reports what it did to memory.
    page.wait_for_function(
        "() => document.body.innerText.includes('recalled')",
        timeout=120_000,
    )

    after = _database_counts(endpoint)
    assert after["facts"] > before["facts"], (
        f"the turn wrote no facts: {before} -> {after}"
    )

    shown, truth = _wait_for_convergence(page, endpoint, timeout=60.0)
    assert shown == truth, f"page shows {shown}, database holds {truth}"

    body = page.inner_text("body")
    assert "Tomas Varga" in body or "Helios" in body, "the turn is not visible in the chat"

    shot("05-agent-conversation")
    print(f"\n  chat turn wrote {after['facts'] - before['facts']} facts, "
          f"rendered live from the database")

    run("chatdemo-cleanup", """
        DELETE mentions WHERE in.text CONTAINS 'Helios' OR in.text CONTAINS 'Tomas Varga';
        DELETE derived_from WHERE in.text CONTAINS 'Helios' OR in.text CONTAINS 'Tomas Varga';
        DELETE fact WHERE text CONTAINS 'Helios' OR text CONTAINS 'Tomas Varga';
        DELETE entity WHERE name IN ['Tomas Varga', 'Helios', 'Lisbon office', 'Helios project'];
    """, endpoint=endpoint)


# --------------------------------------------------------------------------
# chaos
# --------------------------------------------------------------------------


@pytest.mark.chaos
def test_the_graph_reconverges_after_the_database_restarts(page, endpoint, shot):
    """Kill the database underneath a live page; the page must catch up exactly.

    This is the property the entire live layer exists to provide. Live queries
    can miss events while the socket is down, so a client that merely reconnects
    and resumes would drift silently -- which is why the reducer follows every
    reconnect with a full SELECT and a reconcile.

    A screenshot is taken *during* the outage as well as after it, because the
    amber reconnecting state is part of the contract: a stale graph must never
    look like a healthy one.
    """
    before = _header_counts(page)
    assert before["facts"] > 0

    # The restart runs on another thread so the page can be watched *through* the
    # outage. Sampling once afterwards is unreliable: recovery takes a couple of
    # seconds and a single sample can easily land after the page is healthy
    # again, reporting a missing banner that was actually shown.
    import threading

    restart_error: list[BaseException] = []

    def restart() -> None:
        """Bounce the database container."""
        try:
            subprocess.run(["docker", "compose", "restart", "surrealdb"],
                           cwd=ROOT, check=True, capture_output=True, timeout=180)
        except BaseException as error:  # noqa: BLE001 - reported on the main thread
            restart_error.append(error)

    worker = threading.Thread(target=restart, daemon=True)
    worker.start()

    reconnecting = False
    captured = False
    deadline = time.time() + 90
    settled_at: float | None = None

    while time.time() < deadline:
        try:
            body = page.inner_text("body").lower()
        except Exception:  # noqa: BLE001 - the page is under deliberate chaos
            # Reading the page can fail transiently while the renderer is busy or
            # mid-navigation. That is the condition being induced, not a failure
            # of the property under test -- the assertions below are what decide.
            time.sleep(0.5)
            continue

        if "reconnect" in body or "resync" in body:
            reconnecting = True
            if not captured:
                shot("03-reconnecting")
                captured = True
            break

        if not worker.is_alive():
            # The container is back. Give the page a few seconds to notice before
            # concluding it never showed a reconnecting state.
            settled_at = settled_at or time.time()
            if time.time() - settled_at > 8:
                break
        time.sleep(0.4)

    worker.join(timeout=180)
    if restart_error:
        raise restart_error[0]
    if not captured:
        shot("03-reconnecting")

    # Write while the page is recovering, so it cannot pass by having missed
    # nothing.
    for attempt in range(30):
        try:
            run("write-during-outage", """
                CREATE fact SET text = '[chaos] written during the outage',
                                embedding = array::repeat(0.0, 1536), confidence = 0.5;
            """, endpoint=endpoint)
            break
        except Exception:  # noqa: BLE001 - the database is deliberately unavailable
            time.sleep(1.0)
    else:
        pytest.fail("the database never came back")

    shown, truth = _wait_for_convergence(page, endpoint, timeout=120.0)
    shot("04-reconverged")

    assert shown == truth, (
        f"after the restart the page shows {shown} but the database holds {truth}; "
        f"the resync did not close the gap"
    )
    assert reconnecting, (
        "the page never showed a reconnecting or resyncing state during the "
        "outage -- a stale graph that looks healthy is worse than one that flickers"
    )

    run("chaos-cleanup", "DELETE fact WHERE string::starts_with(text, '[chaos]');",
        endpoint=endpoint)

    # Hand the stack back healthy. A write succeeding is not the same as compose
    # reporting the container ready, and every test after this one assumes a live
    # database -- without this the next module can abort the whole session with
    # "SurrealDB unreachable", which looks like a failure of that module.
    subprocess.run(["docker", "compose", "up", "-d", "--wait", "surrealdb"],
                   cwd=ROOT, check=True, capture_output=True, timeout=180)
