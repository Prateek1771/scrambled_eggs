"""Write memory to SurrealDB on a timer, with no agent and no LLM involved.

This exists for M1, whose only question is: does LIVE SELECT -> browser -> force
graph actually feel alive? Answering that with the real agent in the loop would
confuse two risks -- an unresponsive graph and a slow model look identical on
screen. So the agent is removed entirely and the database is driven directly.

It writes a small scripted story rather than random noise, because a graph of
unrelated nodes cannot show association, supersession, or recall, which are the
three things the visualisation exists to make visible.

Usage:
    python db/fake_writer.py                  # clear, then play once at ~1s per beat
    python db/fake_writer.py --delay 0.25     # faster, for recording
    python db/fake_writer.py --loop           # play it forever
    python db/fake_writer.py --keep           # append instead of clearing
"""

import argparse
import pathlib
import time

from surreal_http import embedding, near, run

# The story. Each beat is one write, and the order matters: entities appear
# before the facts that mention them, associations form before the traversal that
# uses them, and the contradiction lands late so there is something to supersede.
#
# `kind` drives which writer runs. `seed` picks the embedding neighbourhood --
# facts sharing a seed family are semantically close, which is what makes vector
# recall behave like recall rather than like a lookup.
STORY: list[dict] = [
    {"kind": "entity", "name": "Ana", "type": "person"},
    {"kind": "entity", "name": "Acme", "type": "org"},
    {"kind": "fact", "text": "Ana works at Acme", "seed": 10,
     "mentions": ["Ana", "Acme"], "confidence": 0.9},
    {"kind": "entity", "name": "Munich", "type": "place"},
    {"kind": "relate", "from": "Acme", "to": "Munich", "predicate": "located_in"},
    {"kind": "fact", "text": "Acme's head office is in Munich", "seed": 11,
     "mentions": ["Acme", "Munich"], "confidence": 0.85},
    {"kind": "entity", "name": "distributed systems", "type": "concept"},
    {"kind": "fact", "text": "Ana is deep into distributed systems", "seed": 30,
     "mentions": ["Ana", "distributed systems"], "confidence": 0.8},
    {"kind": "relate", "from": "Ana", "to": "distributed systems", "predicate": "interested_in"},
    {"kind": "entity", "name": "Bruno", "type": "person"},
    {"kind": "fact", "text": "Bruno reviews Ana's design documents", "seed": 31,
     "mentions": ["Bruno", "Ana"], "confidence": 0.7},
    {"kind": "relate", "from": "Bruno", "to": "Ana", "predicate": "works_with"},
    {"kind": "recall", "query": "what is Ana working on?", "seed": 30},
    {"kind": "entity", "name": "Globex", "type": "org"},
    {"kind": "entity", "name": "Berlin", "type": "place"},
    {"kind": "relate", "from": "Globex", "to": "Berlin", "predicate": "located_in"},
    {"kind": "fact", "text": "The coffee near the Landwehr canal is excellent", "seed": 900,
     "mentions": ["Berlin"], "confidence": 0.4},
    # The contradiction. Everything above exists so that this beat has weight:
    # the old fact dims rather than disappearing, and the edge between them is
    # visible on screen.
    {"kind": "supersede", "old": "Ana works at Acme",
     "text": "Ana works at Globex", "seed": 12,
     "mentions": ["Ana", "Globex"], "confidence": 0.95},
    {"kind": "relate", "from": "Ana", "to": "Globex", "predicate": "works_at"},
    {"kind": "recall", "query": "where does Ana work now?", "seed": 12},
    {"kind": "recall", "query": "anything to do around Ana's office?", "seed": 900},
]


def reset(**connection) -> None:
    """Delete every memory record, leaving the schema in place.

    Edges are deleted before the records they connect. SurrealDB does not require
    this, but doing it in dependency order keeps the live feed's DELETE events in
    an order the browser's reducer can apply without a pending buffer.
    """
    run("reset", """
        DELETE supersedes; DELETE mentions; DELETE relates; DELETE derived_from;
        DELETE retrieval; DELETE fact; DELETE entity; DELETE message; DELETE session;
    """, **connection)


def ensure_session(**connection) -> str:
    """Create the session every write in this run belongs to, and return its id.

    Facts are attributed to a session the same way the real agent attributes
    them, so nothing downstream has to special-case data the fake writer
    produced.
    """
    return run("session", """
        LET $s = (CREATE ONLY session SET title = "fake writer");
        RETURN $s.id;
    """, **connection)[-1]


def write_entity(name: str, kind: str, **connection) -> None:
    """Create an entity, or leave it alone if it already exists.

    UPSERT against the unique (name, kind) index rather than SELECT-then-CREATE,
    so replaying the story twice does not produce a duplicate and does not race
    with itself.
    """
    run("entity", """
        UPSERT entity SET name = $name, kind = $kind, last_seen = time::now()
        WHERE name = $name AND kind = $kind;
    """, {"name": name, "kind": kind}, **connection)


def write_fact(text: str, seed: int, mentions: list[str], confidence: float,
               session: str, **connection) -> None:
    """Create a fact, link it to the entities it names, and record where it came from.

    One transaction, so the browser never sees a fact that has no edges yet --
    a node that appears alone and acquires its edges a moment later reads as a
    rendering glitch rather than as memory forming.
    """
    run("fact", """
        BEGIN TRANSACTION;
        LET $sess = type::record($session_id);   -- $session is reserved
        LET $m = (CREATE ONLY message SET session = $sess, role = "user", text = $text);
        LET $f = (CREATE ONLY fact SET text = $text, embedding = $embedding,
                                       confidence = $confidence);
        RELATE $f->derived_from->$m;
        FOR $name IN $mentions {
            LET $e = (SELECT * FROM ONLY entity WHERE name = $name LIMIT 1);
            IF $e != NONE { RELATE $f->mentions->$e; };
        };
        COMMIT TRANSACTION;
    """, {"text": text, "embedding": near(seed), "confidence": confidence,
          "mentions": mentions, "session_id": session}, **connection)


def write_relation(source: str, target: str, predicate: str, **connection) -> None:
    """Associate two entities.

    Skips silently if either endpoint is missing rather than failing the run: the
    story is ordered so this cannot happen, but a half-written graph is a better
    outcome for a demo script than a crash.
    """
    run("relate", """
        LET $a = (SELECT * FROM ONLY entity WHERE name = $source LIMIT 1);
        LET $b = (SELECT * FROM ONLY entity WHERE name = $target LIMIT 1);
        IF $a != NONE AND $b != NONE {
            RELATE $a->relates->$b SET predicate = $predicate, strength = 0.7;
        };
    """, {"source": source, "target": target, "predicate": predicate}, **connection)


def write_supersession(old_text: str, text: str, seed: int, mentions: list[str],
                       confidence: float, session: str, **connection) -> None:
    """Replace a fact with a contradicting one without deleting anything.

    The old fact keeps its row, gains a `valid_to`, and is pointed at by a
    `supersedes` edge from its replacement. `is_current` is COMPUTED, so it flips
    on its own -- no application code sets it, and no second write is needed.
    """
    run("supersede", """
        BEGIN TRANSACTION;
        LET $sess = type::record($session_id);   -- $session is reserved
        LET $old = (SELECT * FROM ONLY fact WHERE text = $old_text AND valid_to = NONE LIMIT 1);
        LET $m = (CREATE ONLY message SET session = $sess, role = "user", text = $text);
        LET $new = (CREATE ONLY fact SET text = $text, embedding = $embedding,
                                         confidence = $confidence);
        RELATE $new->derived_from->$m;
        IF $old != NONE {
            UPDATE $old.id SET valid_to = time::now();
            RELATE $new->supersedes->$old;
        };
        FOR $name IN $mentions {
            LET $e = (SELECT * FROM ONLY entity WHERE name = $name LIMIT 1);
            IF $e != NONE { RELATE $new->mentions->$e; };
        };
        COMMIT TRANSACTION;
    """, {"old_text": old_text, "text": text, "embedding": near(seed),
          "confidence": confidence, "mentions": mentions, "session_id": session},
        **connection)


# The hybrid query lives in db/recall.surql, read once at import. It used to be a
# constant here; moving it to a file means the agent in api/cortex/retrieval.py
# runs the identical text, and the string stored on every retrieval record is the
# file itself. Two copies of a query this important is one copy too many.
RECALL_SURQL = (pathlib.Path(__file__).parent / "recall.surql").read_text(encoding="utf-8")


def write_recall(question: str, seed: int, session: str, **connection) -> None:
    """Run a real hybrid retrieval and record it, which is what makes the graph glow.

    The query here is the one from docs/03 -- semantic, lexical and associative
    recall in a single statement. Writing the `retrieval` record is not logging:
    the browser subscribes to that table, so this is what tells the UI which nodes
    to light up and in which colour. It also fires the ASYNC decay event, so
    salience moves as a side effect of remembering, exactly as it does for the
    real agent.
    """
    # RECALL_SURQL is executed, not paraphrased: the same string that runs is the
    # string stored on the record and shown in the inspector. The tail below only
    # persists the result -- it is not part of the retrieval itself, which is why
    # it is appended here rather than living in the constant.
    persist = """
        -- Built and written in the same batch on purpose. Record ids that leave
        -- the database over HTTP come back as plain strings, and `hits.*.fact`
        -- is typed `record<fact>` with no implicit coercion -- so a round trip
        -- through the client would have to re-derive a type the database already
        -- knew. Keeping it server-side leaves the ids as records throughout.
        LET $sess = type::record($session_id);   -- $session is reserved
        LET $hits = array::map(
            array::concat($vec, $kw, $assoc),
            |$h| { fact: $h.id, score: $h.score, via: $h.via }
        );
        CREATE retrieval SET
            session  = $sess,
            query    = $text,
            strategy = "hybrid",
            surql    = $surql,
            timing   = { total_ms: 0, knn_ms: 0, fts_ms: 0, graph_ms: 0 },
            hits     = $hits;
    """

    run("recall", RECALL_SURQL + persist,
        {"q": near(seed), "text": question, "k": 6, "session_id": session,
         "surql": RECALL_SURQL.strip()},
        **connection)


def play(story: list[dict], delay: float, session: str, **connection) -> None:
    """Write every beat of the story in order, pausing between them.

    The pause is the point. These writes take milliseconds; without a delay the
    entire graph appears in one frame and there is nothing to watch. The delay is
    pacing for a human eye, not backpressure.
    """
    for index, beat in enumerate(story, start=1):
        kind = beat["kind"]
        if kind == "entity":
            write_entity(beat["name"], beat["type"], **connection)
            label = f"entity {beat['name']}"
        elif kind == "fact":
            write_fact(beat["text"], beat["seed"], beat["mentions"],
                       beat["confidence"], session, **connection)
            label = f"fact \"{beat['text']}\""
        elif kind == "relate":
            write_relation(beat["from"], beat["to"], beat["predicate"], **connection)
            label = f"relate {beat['from']} -{beat['predicate']}-> {beat['to']}"
        elif kind == "supersede":
            write_supersession(beat["old"], beat["text"], beat["seed"],
                               beat["mentions"], beat["confidence"], session,
                               **connection)
            label = f"supersede \"{beat['old']}\" -> \"{beat['text']}\""
        elif kind == "recall":
            write_recall(beat["query"], beat["seed"], session, **connection)
            label = f"recall \"{beat['query']}\""
        else:
            raise ValueError(f"unknown story beat: {kind}")

        print(f"  {index:>2}/{len(story)}  {label}")
        time.sleep(delay)


def main() -> None:
    """Parse arguments and play the story, once or forever."""
    parser = argparse.ArgumentParser(description="Drive the CORTEX memory graph without an agent.")
    parser.add_argument("--endpoint", default="http://localhost:8000")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="seconds between writes (pacing for a human eye)")
    # Clearing first is the default. Replaying the story on top of itself
    # produces a graph with two of every fact, which looks like a bug in the
    # renderer rather than what it is -- the writer has no dedupe, because
    # dedupe is the agent's job in M4.
    parser.add_argument("--keep", action="store_true",
                        help="append to existing memory instead of clearing it")
    parser.add_argument("--loop", action="store_true", help="replay forever")
    args = parser.parse_args()

    connection = {"endpoint": args.endpoint}

    if not args.keep:
        print("clearing memory")
        reset(**connection)

    while True:
        session = ensure_session(**connection)
        print(f"playing {len(STORY)} beats at {args.delay}s")
        play(STORY, args.delay, session, **connection)
        if not args.loop:
            break
        print("looping -- clearing and replaying")
        time.sleep(2.0)
        reset(**connection)


if __name__ == "__main__":
    main()
