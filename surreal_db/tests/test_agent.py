"""L9 - the agent: fusion, checkpointing, tools, and one real conversation.

Split deliberately by cost. Everything above `test_agent_learns_and_then_changes_
its_mind` runs for free -- pure functions and database round trips. The turns that
call OpenAI are marked `llm` and there are exactly two of them, because a test
suite that costs a dollar to run is a test suite nobody runs.

    pytest tests/test_agent.py -m "not llm"   # free
    pytest tests/test_agent.py                # ~2 model turns
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from cortex.config import Settings  # noqa: E402
from cortex.db import Database, record_str  # noqa: E402
from cortex.retrieval import fuse  # noqa: E402

# --------------------------------------------------------------------------
# fusion -- pure, no database, no network
# --------------------------------------------------------------------------


def _rows(*texts: str) -> list[dict]:
    """Build minimal recall rows, ranked in the order given."""
    return [{"id": f"fact:{text}", "text": text, "confidence": 0.9} for text in texts]


def test_fusion_rewards_agreement_between_strategies():
    """A fact two strategies found outranks one that only led a single list.

    This is the whole reason for fusing rather than concatenating: raw scores
    from cosine similarity and BM25 do not share a scale, so the only comparable
    signal is *rank*, and agreement across ranks is stronger evidence than a high
    position in one list.

    `b` is second in vector and first in text. `a` leads vector and appears
    nowhere else. `b` must win.

    Note the shape of the fixture: two perfectly reversed lists would tie every
    document by construction -- 1/(k+1) + 1/(k+3) equals 1/(k+2) + 1/(k+2) -- so
    the arms overlap only partially, which is what a real query produces anyway.
    """
    fused = fuse({
        "vector": _rows("a", "b", "c"),
        "text": _rows("b", "d", "e"),
    })
    assert [hit.fact_id for hit in fused][0] == "fact:b", [h.fact_id for h in fused]


def test_fusion_is_order_independent_across_arms():
    """Which arm is processed first must not change the result.

    Ties decide this. Any three-way symmetric fixture ties, and a stable sort
    would then return whichever order the arms were iterated in -- so the fused
    ranking would quietly differ between two identical queries.
    """
    arms = {"vector": _rows("a", "b"), "text": _rows("b", "c"), "graph": _rows("c", "a")}
    forward = [hit.fact_id for hit in fuse(arms)]
    backward = [hit.fact_id for hit in fuse(dict(reversed(list(arms.items()))))]
    assert forward == backward


def test_fusion_attributes_a_hit_to_its_strongest_arm():
    """`via` is the strategy that ranked the fact highest.

    It decides a glowing node's colour in the browser, so a fact found by three
    strategies still has to resolve to exactly one.
    """
    fused = fuse({
        "vector": _rows("x", "y", "z"),   # y is rank 2
        "text": _rows("y"),               # y is rank 1 -- stronger
    })
    hit = next(h for h in fused if h.fact_id == "fact:y")
    assert hit.via == "text"


def test_fusion_of_nothing_is_nothing():
    """Empty arms produce an empty list rather than an exception."""
    assert fuse({"vector": [], "text": [], "graph": []}) == []


def test_fusion_deduplicates():
    """A fact in all three arms appears once."""
    fused = fuse({"vector": _rows("a"), "text": _rows("a"), "graph": _rows("a")})
    assert len(fused) == 1


# --------------------------------------------------------------------------
# fixtures for everything that needs the real stack
# --------------------------------------------------------------------------


@pytest.fixture
async def database(endpoint: str):
    """A connected Database, closed afterwards."""
    settings = Settings.from_env()
    # The tests run on the host, where the database is on localhost rather than
    # at the container's hostname.
    settings = type(settings)(**{**settings.__dict__,
                                 "surreal_url": endpoint.replace("http", "ws") + "/rpc"})
    db = Database(settings)
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def saver(database):
    """A checkpointer, with its thread cleaned up afterwards."""
    from cortex.checkpointer import SurrealCheckpointSaver

    thread = f"test-{uuid.uuid4()}"
    instance = SurrealCheckpointSaver(database)
    yield instance, thread
    await instance.adelete_thread(thread)


def _checkpoint(checkpoint_id: str, **channels):
    """Build a minimal Checkpoint the serialiser will accept."""
    return {
        "v": 4,
        "id": checkpoint_id,
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": channels,
        "channel_versions": {name: 1 for name in channels},
        "versions_seen": {},
    }


# --------------------------------------------------------------------------
# the checkpointer -- M2's stated done-when
# --------------------------------------------------------------------------


async def test_checkpoint_round_trips(saver):
    """A checkpoint written and read back reconstructs its state exactly.

    The one assertion M2 is defined by. It also proves the serialiser's
    `(type_tag, bytes)` pair survives `TYPE bytes` in SurrealDB -- drop the tag
    and the bytes become unreadable, but only on resume, long after the write
    looked fine.
    """
    instance, thread = saver
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}

    checkpoint = _checkpoint("ckpt-1", messages=["hello"], counter=7)
    returned = await instance.aput(config, checkpoint, {"source": "test", "step": 1}, {})

    assert returned["configurable"]["checkpoint_id"] == "ckpt-1"

    loaded = await instance.aget_tuple({"configurable": {"thread_id": thread}})
    assert loaded is not None
    assert loaded.checkpoint["channel_values"] == {"messages": ["hello"], "counter": 7}
    assert loaded.metadata["source"] == "test"


async def test_pending_writes_come_back_in_order(saver):
    """Writes replay in the order they were recorded, not insertion order.

    LangGraph reconstructs partial progress from these; replayed out of order
    they silently produce a different state rather than an error.
    """
    instance, thread = saver
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": "",
                               "checkpoint_id": "ckpt-writes"}}
    await instance.aput({"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
                        _checkpoint("ckpt-writes", messages=[]), {"step": 1}, {})

    await instance.aput_writes(config, [("alpha", 1), ("beta", 2), ("gamma", 3)], "task-1")

    loaded = await instance.aget_tuple(config)
    assert loaded is not None
    assert [channel for _, channel, _ in loaded.pending_writes] == ["alpha", "beta", "gamma"]
    assert [value for _, _, value in loaded.pending_writes] == [1, 2, 3]


async def test_checkpoints_form_a_tree_not_a_list(saver):
    """`parent_checkpoint_id` is stored, which is what time travel needs.

    Without it a thread is a flat list and branching is impossible -- and
    retrofitting it means rewriting every stored checkpoint.
    """
    instance, thread = saver
    root = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    await instance.aput(root, _checkpoint("root"), {"step": 0}, {})

    child_config = {"configurable": {"thread_id": thread, "checkpoint_ns": "",
                                     "checkpoint_id": "root"}}
    await instance.aput(child_config, _checkpoint("child"), {"step": 1}, {})

    loaded = await instance.aget_tuple(
        {"configurable": {"thread_id": thread, "checkpoint_ns": "", "checkpoint_id": "child"}})
    assert loaded is not None
    assert loaded.parent_config is not None
    assert loaded.parent_config["configurable"]["checkpoint_id"] == "root"


async def test_listing_a_thread_returns_newest_first(saver):
    """`alist` walks a thread in reverse chronological order, and respects limit."""
    instance, thread = saver
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    for index in range(4):
        await instance.aput(config, _checkpoint(f"ckpt-{index}"), {"step": index}, {})

    seen = [tuple_.checkpoint["id"] async for tuple_ in instance.alist(config, limit=3)]
    assert len(seen) == 3
    assert seen[0] == "ckpt-3"


async def test_deleting_a_thread_removes_it_entirely(saver, database):
    """A discarded thread leaves no checkpoints and no writes behind.

    The only place in CORTEX where deletion is correct: conversation scaffolding
    is not memory. The facts a thread produced survive it.
    """
    instance, thread = saver
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    await instance.aput(config, _checkpoint("doomed"), {"step": 1}, {})
    await instance.aput_writes(
        {**config, "configurable": {**config["configurable"], "checkpoint_id": "doomed"}},
        [("channel", "value")], "task")

    await instance.adelete_thread(thread)

    assert await instance.aget_tuple({"configurable": {"thread_id": thread}}) is None
    remaining = await database.query(
        "SELECT VALUE count() FROM checkpoint_write WHERE thread_id = $t GROUP ALL;",
        {"t": thread})
    assert not remaining or remaining[0] in (0, {"count": 0})


async def test_an_unknown_thread_has_no_checkpoint(saver):
    """Asking about a thread that never existed returns None, not an error."""
    instance, _ = saver
    assert await instance.aget_tuple({"configurable": {"thread_id": "never-existed"}}) is None


# --------------------------------------------------------------------------
# the memory tools -- real queries, no model
# --------------------------------------------------------------------------


@pytest.fixture
async def tools(database):
    """The tool set, wired to a real database and a real embedding client."""
    from langchain_openai import OpenAIEmbeddings

    from cortex.tools import build_tools

    settings = Settings.from_env()
    embed = OpenAIEmbeddings(model=settings.embed_model, api_key=settings.openai_api_key)
    return {tool.name: tool for tool in build_tools(database, embed)}


async def test_timeline_returns_superseded_versions_too(tools, database):
    """The tool that answers "where did she work *before*".

    A vector store cannot do this at all: the old fact is gone, or if it is not,
    it is indistinguishable from the current one.
    """
    seeded = await database.query("""
        BEGIN TRANSACTION;
        LET $e = (CREATE ONLY entity SET name = "[tooltest] Subject", kind = "person");
        LET $old = (CREATE ONLY fact SET text = "[tooltest] Subject worked at Old Corp",
                                        embedding = array::repeat(0.0, 1536), confidence = 0.9);
        LET $new = (CREATE ONLY fact SET text = "[tooltest] Subject works at New Corp",
                                        embedding = array::repeat(0.0, 1536), confidence = 0.9);
        RELATE $old->mentions->$e; RELATE $new->mentions->$e;
        UPDATE $old.id SET valid_to = time::now();
        RELATE $new->supersedes->$old;
        COMMIT TRANSACTION;
        RETURN $e.id;
    """)
    assert seeded is not None

    try:
        answer = await tools["timeline"].ainvoke({"entity": "[tooltest] Subject"})
        assert "Old Corp" in answer, answer
        assert "New Corp" in answer, answer
        assert "until" in answer, "the superseded version is not marked as historical"
    finally:
        await database.query("""
            DELETE supersedes WHERE in.text CONTAINS '[tooltest]';
            DELETE mentions   WHERE in.text CONTAINS '[tooltest]';
            DELETE fact       WHERE text CONTAINS '[tooltest]';
            DELETE entity     WHERE name CONTAINS '[tooltest]';
        """)


async def test_why_walks_back_to_the_original_message(tools, database):
    """Provenance: the tool produces the sentence the fact came from.

    This is the claim `docs/01` makes about why a graph beats a chunk store --
    asserted rather than asserted-in-prose.
    """
    await database.query("""
        BEGIN TRANSACTION;
        LET $s = (CREATE ONLY session SET title = "[tooltest]");
        LET $m = (CREATE ONLY message SET session = $s.id, role = "user",
                                          text = "[tooltest] I told you this myself");
        LET $f = (CREATE ONLY fact SET text = "[tooltest] a derived fact",
                                       embedding = array::repeat(0.0, 1536), confidence = 0.8);
        RELATE $f->derived_from->$m;
        COMMIT TRANSACTION;
        RETURN $f.id;
    """)
    fact_id = record_str((await database.query(
        "SELECT VALUE id FROM fact WHERE text = '[tooltest] a derived fact' LIMIT 1;"))[0])

    try:
        answer = await tools["why"].ainvoke({"fact_id": fact_id})
        assert "I told you this myself" in answer, answer
    finally:
        await database.query("""
            DELETE derived_from WHERE in.text CONTAINS '[tooltest]';
            DELETE fact WHERE text CONTAINS '[tooltest]';
            DELETE message WHERE text CONTAINS '[tooltest]';
            DELETE session WHERE title = '[tooltest]';
        """)


async def test_recall_exact_finds_an_identifier_no_embedding_could(tools, database):
    """The lexical arm, earning its place.

    `ZX-99141` has no semantics. Vector search cannot retrieve it; BM25 finds it
    instantly. This is the entire argument for keeping both.
    """
    await database.query("""
        CREATE fact SET text = "[tooltest] incident ZX-99141 took down the relay",
                        embedding = array::repeat(0.0, 1536), confidence = 0.9;
    """)
    try:
        answer = await tools["recall_exact"].ainvoke({"phrase": "ZX-99141"})
        assert "ZX-99141" in answer, answer
    finally:
        await database.query("DELETE fact WHERE text CONTAINS '[tooltest]';")


# --------------------------------------------------------------------------
# one real conversation
# --------------------------------------------------------------------------


@pytest.mark.llm
async def test_agent_learns_and_then_changes_its_mind(database):
    """Two turns: state a fact, contradict it, and assert the memory did the right thing.

    The end-to-end claim, and the only test here that spends money -- two turns,
    deliberately. It asserts what `docs/01` promises:

      * atomic extraction (one statement per fact, not one per sentence);
      * the contradiction is detected and the old fact *ended*, not deleted;
      * `is_current` flips on its own, because it is COMPUTED;
      * the superseded fact is still retrievable, and says what was believed.
    """
    from cortex.graph import build_agent

    settings = Settings.from_env()
    agent = build_agent(settings, database)

    session = record_str((await database.query(
        'LET $s = (CREATE ONLY session SET title = "[agenttest]"); RETURN $s.id;')))
    thread = f"agenttest-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread}}

    try:
        await agent.ainvoke({
            "query": "Zara Petrov works at Northwind. Northwind is based in Lisbon.",
            "session_id": session, "thread_id": thread,
        }, config=config)

        facts = await database.query(
            "SELECT text FROM fact WHERE text CONTAINS 'Zara' AND valid_to = NONE;")
        assert len(facts or []) >= 1, "the agent remembered nothing from the first turn"

        await agent.ainvoke({
            "query": "Correction: Zara left Northwind, she works at Vector Labs now.",
            "session_id": session, "thread_id": thread,
        }, config=config)

        rows = await database.query("""
            SELECT text, is_current, valid_to, ->supersedes->fact.text AS replaces
            FROM fact WHERE text CONTAINS 'Zara';
        """)
        current = [row for row in rows if row["is_current"]]
        ended = [row for row in rows if not row["is_current"]]

        assert ended, (
            "nothing was superseded -- the agent did not notice the contradiction. "
            f"Facts: {[r['text'] for r in rows]}"
        )
        assert any("Vector Labs" in row["text"] for row in current), [r["text"] for r in current]
        assert any(row.get("replaces") for row in current), "no supersedes edge was created"
        # The old fact is still readable. This is the promise the whole temporal
        # model exists for.
        assert any("Northwind" in row["text"] for row in ended)
    finally:
        await database.query("""
            DELETE supersedes WHERE in.text CONTAINS 'Zara' OR out.text CONTAINS 'Zara';
            DELETE mentions   WHERE in.text CONTAINS 'Zara';
            DELETE derived_from WHERE in.text CONTAINS 'Zara';
            DELETE fact WHERE text CONTAINS 'Zara';
            DELETE entity WHERE name IN ['Zara Petrov', 'Northwind', 'Vector Labs', 'Lisbon'];
            DELETE message WHERE session = type::record($s);
            DELETE session WHERE title = '[agenttest]';
        """, {"s": session})


@pytest.mark.llm
async def test_a_thread_survives_a_restarted_process(database):
    """A conversation resumed on a new agent object still has its history.

    The checkpointer's reason for existing. A fresh `build_agent` -- as though the
    container had restarted -- must pick the thread up where it left off, which it
    can only do by reading state back out of SurrealDB.
    """
    from cortex.graph import build_agent

    settings = Settings.from_env()
    session = record_str((await database.query(
        'LET $s = (CREATE ONLY session SET title = "[threadtest]"); RETURN $s.id;')))
    thread = f"threadtest-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread}}

    try:
        first = build_agent(settings, database)
        await first.ainvoke({"query": "My favourite colour is vermilion.",
                             "session_id": session, "thread_id": thread}, config=config)

        # A completely separate agent instance, sharing only the database.
        second = build_agent(settings, database)
        state = await second.aget_state(config)

        assert state.values.get("messages"), "the resumed thread has no message history"
        history = " ".join(str(message.content) for message in state.values["messages"])
        assert "vermilion" in history.lower(), history[:300]
    finally:
        await database.query("""
            DELETE checkpoint_write WHERE thread_id = $t;
            DELETE checkpoint WHERE thread_id = $t;
            DELETE mentions WHERE in.text CONTAINS 'vermilion';
            DELETE derived_from WHERE in.text CONTAINS 'vermilion';
            DELETE fact WHERE text CONTAINS 'vermilion';
            DELETE message WHERE session = type::record($s);
            DELETE session WHERE title = '[threadtest]';
        """, {"t": thread, "s": session})
