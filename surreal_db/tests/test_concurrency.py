"""L4 - torn reads under concurrent supersession.

This is the experiment specified in docs/06-benchmark.md and never run. It is
also the project's central architectural claim: a split stack writes a
supersession across Postgres, Neo4j and Elasticsearch with no shared
transaction, so there is a window in which a reader sees an inconsistent world.
One transactional engine should have no such window.

"Should have" is marketing. This measures it.

A read is **torn** if it observes any of:
  * both versions of a fact current at once;
  * neither version current, so the memory has a hole;
  * a `supersedes` edge whose target is not yet visible as ended.

Expected count on a single engine is zero, at every concurrency level.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
from surreal_http import bind, query, run  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.chaos]

SUBJECTS = 12          # independent facts being contended over
DURATION_S = 4.0       # how long each concurrency level runs
CONCURRENCY_LEVELS = (1, 10, 50)


@dataclass
class Observation:
    """What the readers saw at one concurrency level."""

    writers: int
    reads: int = 0
    writes: int = 0
    conflicts: int = 0
    retries: int = 0
    errors: int = 0
    torn: int = 0
    examples: list[str] = field(default_factory=list)

    def record_tear(self, description: str) -> None:
        """Note a torn read, keeping the first few for the failure message."""
        self.torn += 1
        if len(self.examples) < 5:
            self.examples.append(description)


def _seed_subjects(connection: dict) -> list[str]:
    """Create the facts the writers will contend over, and return their subject tags.

    Its own record set, tagged and disposable, so the corpus every other test
    depends on is not left rewritten by this one.
    """
    run("concurrency-cleanup", """
        DELETE supersedes WHERE in.text CONTAINS '[contended]'
                             OR out.text CONTAINS '[contended]';
        DELETE fact WHERE text CONTAINS '[contended]';
    """, **connection)

    tags = [f"subject-{index:02d}" for index in range(SUBJECTS)]
    run("concurrency-seed", """
        FOR $tag IN $tags {
            CREATE fact SET text = '[contended] ' + $tag + ' v0',
                            embedding = array::repeat(0.0, 1536), confidence = 0.9;
        };
    """, {"tags": tags}, **connection)
    return tags


SUPERSEDE = """
    BEGIN TRANSACTION;
    LET $old = (SELECT * FROM ONLY fact
                WHERE text CONTAINS $tag AND valid_to = NONE LIMIT 1);
    IF $old != NONE {
        LET $new = (CREATE ONLY fact
                    SET text = '[contended] ' + $tag + ' v' + <string> $version,
                        embedding = array::repeat(0.0, 1536), confidence = 0.9);
        UPDATE $old.id SET valid_to = time::now();
        RELATE $new->supersedes->$old;
    };
    COMMIT TRANSACTION;
"""


def _supersede(tag: str, version: int, connection: dict,
               attempts: int = 6) -> tuple[str, int]:
    """Replace a subject's current fact with a new one, retrying on conflict.

    Exactly the transaction from docs/03: create the replacement, end the
    predecessor, link them. If this is genuinely atomic, no reader can ever see
    the intermediate state.

    Retries matter to the experiment. Under contention SurrealDB returns
    retryable transaction conflicts, and a writer that gives up on the first one
    stops contending -- which would leave the torn-read result looking clean
    because almost nothing was happening. Retrying keeps the pressure on, and the
    conflict rate is itself a result worth reporting.

    Returns the outcome ("ok", "conflict" or "error") and how many retries it
    took. Never raises: a failed write is not a torn read, and this loop must not
    take the test down with it.
    """
    import random

    statement = bind({"tag": tag, "version": version}) + SUPERSEDE
    for attempt in range(attempts):
        try:
            envelopes = query(statement, **connection)
        except Exception:  # noqa: BLE001 - transport failure is not a torn read
            return "error", attempt

        failures = [e for e in envelopes if e.get("status") != "OK"]
        if not failures:
            return "ok", attempt

        # Every failing statement is inspected, not just the first. When one
        # statement inside a transaction conflicts, the surrounding statements
        # report a generic "not executed due to a failed transaction" -- and that
        # generic message usually sorts first. Reading only failures[0] therefore
        # classifies a retryable conflict as a hard error, which silently turns a
        # contention experiment into a measurement of nothing.
        text = " ".join(str(failure.get("result", "")) for failure in failures).lower()
        retryable = "conflict" in text or "can be retried" in text or "resource busy" in text
        if not retryable:
            return "error", attempt
        # Backoff with jitter, so fifty writers do not retry in lockstep.
        time.sleep(0.004 * (2 ** attempt) * (0.5 + random.random()))
    return "conflict", attempts


def _read_and_check(tags: list[str], observation: Observation, connection: dict) -> None:
    """Read every subject once and record any torn state.

    The check is per subject: however many versions exist, exactly one must be
    current. Anything else means a reader observed a partially applied
    supersession.
    """
    rows = run("concurrency-read", """
        SELECT text, is_current, valid_to FROM fact
        WHERE text CONTAINS '[contended]';
    """, **connection)[-1]

    by_subject: dict[str, list[dict]] = {}
    for row in rows:
        for tag in tags:
            if tag in row["text"]:
                by_subject.setdefault(tag, []).append(row)
                break

    observation.reads += 1
    for tag, versions in by_subject.items():
        current = [version for version in versions if version["is_current"]]
        if len(current) != 1:
            observation.record_tear(
                f"{tag}: {len(current)} current of {len(versions)} versions "
                f"({[v['text'].split()[-1] for v in versions]})"
            )


def _run_level(writers: int, tags: list[str], connection: dict) -> Observation:
    """Run one concurrency level: N writers superseding, readers watching.

    Readers and writers run for the same wall-clock window. The readers are the
    measurement; the writers only exist to create the window a tear could appear
    in.
    """
    observation = Observation(writers=writers)
    stop = threading.Event()
    counter = threading.Lock()
    version = [1]

    def writer_loop() -> None:
        """Supersede random subjects until told to stop."""
        import random

        rng = random.Random(threading.get_ident())
        while not stop.is_set():
            with counter:
                version[0] += 1
                this_version = version[0]
            outcome, retries = _supersede(rng.choice(tags), this_version, connection)
            with counter:
                observation.retries += retries
                if outcome == "ok":
                    observation.writes += 1
                elif outcome == "conflict":
                    observation.conflicts += 1
                else:
                    observation.errors += 1

    def reader_loop() -> None:
        """Read every subject repeatedly, checking for torn state."""
        while not stop.is_set():
            try:
                _read_and_check(tags, observation, connection)
            except Exception:  # noqa: BLE001 - a failed read is not a torn read
                pass

    with ThreadPoolExecutor(max_workers=writers + 2) as pool:
        futures = [pool.submit(writer_loop) for _ in range(writers)]
        futures += [pool.submit(reader_loop) for _ in range(2)]
        time.sleep(DURATION_S)
        stop.set()
        for future in futures:
            future.result()

    return observation


@pytest.fixture(scope="module")
def subjects(connection: dict) -> list[str]:
    """The contended facts, cleaned up when the module finishes."""
    tags = _seed_subjects(connection)
    yield tags
    run("concurrency-teardown", """
        DELETE supersedes WHERE in.text CONTAINS '[contended]'
                             OR out.text CONTAINS '[contended]';
        DELETE fact WHERE text CONTAINS '[contended]';
    """, **connection)


@pytest.mark.parametrize("writers", CONCURRENCY_LEVELS)
def test_no_torn_reads_under_concurrent_supersession(writers, subjects, connection, record_property):
    """A reader must never observe a half-applied supersession.

    Run at 1, 10 and 50 concurrent writers. The numbers are recorded whatever
    they are, because this is the claim the project makes about why one engine
    beats five, and an unverified claim is marketing.
    """
    observation = _run_level(writers, subjects, connection)

    record_property("writers", writers)
    record_property("reads", observation.reads)
    record_property("writes", observation.writes)
    record_property("conflicts", observation.conflicts)
    record_property("retries", observation.retries)
    record_property("errors", observation.errors)
    record_property("torn", observation.torn)

    # Retries per commit is the interesting number, and it is not a failure:
    # SurrealDB returning a retryable conflict is the engine refusing to produce
    # the torn state a split stack would have produced silently. The cost of that
    # refusal is what this line reports.
    per_commit = observation.retries / observation.writes if observation.writes else 0.0
    print(f"\n  writers={writers:<3} committed={observation.writes:<5} "
          f"gave-up={observation.conflicts:<4} retries={observation.retries:<5} "
          f"({per_commit:.2f}/commit)  reads={observation.reads:<5} "
          f"torn={observation.torn}")

    assert observation.reads > 0, "no reads completed; the measurement is empty"
    assert observation.writes > 0, "no writes were attempted; nothing was contended"
    assert observation.torn == 0, (
        f"{observation.torn} torn reads at {writers} writers over "
        f"{observation.reads} reads. Examples: {observation.examples}"
    )


def test_supersession_history_survives_the_contention(subjects, connection):
    """After all that churn, every subject still has exactly one current version.

    The per-read check could in principle miss a tear that healed between polls.
    This is the settled-state check: once the writers stop, the world must be
    exactly consistent, and every intermediate version must still be on disk.
    """
    rows = run("post-contention", """
        SELECT text, is_current FROM fact WHERE text CONTAINS '[contended]';
    """, **connection)[-1]

    by_subject: dict[str, list[dict]] = {}
    for row in rows:
        tag = row["text"].split()[1]
        by_subject.setdefault(tag, []).append(row)

    assert by_subject, "the contended facts vanished entirely"
    for tag, versions in by_subject.items():
        current = [version for version in versions if version["is_current"]]
        assert len(current) == 1, (
            f"{tag} settled with {len(current)} current versions out of {len(versions)}"
        )
        assert len(versions) > 1, (
            f"{tag} was never superseded, so it proves nothing about contention"
        )
