"""Shared fixtures: the database connection, the corpus, and scale selection.

The corpus is expensive to build and load -- a hundred seconds at `medium` --
so it is built once per session and reused. Tests that mutate it say so, and get
a reset first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "tests"))

import corpus as corpus_module  # noqa: E402
from surreal_http import query, run  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    """Expose corpus scale, seed and endpoints as command-line options.

    Scale is an option rather than an environment variable because it belongs in
    the command that reproduces a failure, next to the seed.
    """
    parser.addoption("--scale", default="small", choices=sorted(corpus_module.SCALES),
                     help="corpus size: small (fast loop), medium (default full run), huge")
    parser.addoption("--seed", type=int, default=7, help="corpus seed; a failure reproduces from it")
    parser.addoption("--endpoint", default="http://localhost:8000", help="SurrealDB HTTP endpoint")
    parser.addoption("--api", default="http://localhost:8080", help="CORTEX API base URL")
    parser.addoption("--web", default="http://localhost:3100", help="CORTEX web app base URL")
    parser.addoption("--reuse-corpus", action="store_true",
                     help="assume the corpus is already loaded and skip the load step")
    # Browser options live here rather than in tests/e2e/conftest.py: pytest only
    # calls pytest_addoption from the rootdir conftest, so options declared in a
    # subdirectory exist when that directory is the target and disappear when the
    # whole suite runs.
    parser.addoption("--headless", action="store_true",
                     help="run the browser hidden; the default is headed so the run can be watched")
    parser.addoption("--slowmo", type=int, default=0,
                     help="milliseconds to pause between browser actions, for demoing")


@pytest.fixture(scope="session")
def scale(request: pytest.FixtureRequest) -> str:
    """The corpus size this run is exercising."""
    return request.config.getoption("--scale")


@pytest.fixture(scope="session")
def seed(request: pytest.FixtureRequest) -> int:
    """The corpus seed. Printed on failure so the run can be reproduced exactly."""
    return request.config.getoption("--seed")


@pytest.fixture(scope="session")
def endpoint(request: pytest.FixtureRequest) -> str:
    """SurrealDB's HTTP endpoint."""
    return request.config.getoption("--endpoint")


@pytest.fixture(scope="session")
def api_url(request: pytest.FixtureRequest) -> str:
    """The CORTEX API base URL, used for viewer tokens."""
    return request.config.getoption("--api")


@pytest.fixture(scope="session")
def web_url(request: pytest.FixtureRequest) -> str:
    """The CORTEX web app base URL, used by the browser tests."""
    return request.config.getoption("--web")


@pytest.fixture(scope="session")
def connection(endpoint: str) -> dict:
    """Connection kwargs for the `db/surreal_http.py` helpers.

    Fails the session immediately if the database is unreachable, rather than
    letting every test fail separately with the same connection error.
    """
    try:
        query("RETURN 1;", endpoint=endpoint)
    except Exception as error:  # noqa: BLE001 - any failure here is fatal and the cause matters
        pytest.exit(f"SurrealDB unreachable at {endpoint}: {error}\n"
                    f"Start it with: docker compose up -d --wait surrealdb", returncode=3)
    return {"endpoint": endpoint}


@pytest.fixture(scope="session")
def corpus(scale: str, seed: int, connection: dict, request: pytest.FixtureRequest):
    """Build and load the corpus once for the whole session.

    Loading dominates the runtime of the suite, so it happens once. Tests that
    need a pristine database ask for `clean_corpus` instead, which reloads.
    """
    built = corpus_module.build(scale, seed)
    print(f"\ncorpus: {built.summary()}")

    if request.config.getoption("--reuse-corpus"):
        # Guard against the mismatch that produces the most confusing possible
        # failure: a corpus object describing `small` while the database holds
        # `medium`, so every count assertion fails by hundreds and points at the
        # loader rather than at the flag that was forgotten.
        loaded = query("SELECT VALUE count() FROM entity GROUP ALL;", **connection)[0]["result"]
        actual = (loaded[0]["count"] if loaded and isinstance(loaded[0], dict)
                  else (loaded or [0])[0])
        if actual != len(built.entities):
            pytest.exit(
                f"--reuse-corpus was given, but the database holds {actual} entities "
                f"and scale={scale!r} seed={seed} describes {len(built.entities)}. "
                f"Either pass the matching --scale, or drop --reuse-corpus to reload.",
                returncode=4,
            )
        print("corpus: reusing what is already loaded (--reuse-corpus)")
        return built

    corpus_module.reset(**connection)
    timings = corpus_module.load(built, **connection)
    rate = len(built.facts) / max(timings["facts_s"], 1e-6)
    print(f"corpus: loaded in {timings['total_s']:.1f}s ({rate:.0f} facts/s)")
    built.timings = timings  # type: ignore[attr-defined]
    return built


@pytest.fixture
def clean_corpus(corpus, connection: dict):
    """A freshly reloaded corpus, for tests that mutate the database.

    Expensive, so it is opt-in. A test that writes and does not ask for this is
    a test that will corrupt every test after it.
    """
    corpus_module.reset(**connection)
    corpus_module.load(corpus, **connection)
    return corpus


# Scratch records written by the suite itself are all tagged with a bracketed
# prefix -- '[contended]', '[livetest]', '[chaos]'. Corpus assertions exclude
# them, because a module that leaks one record would otherwise fail a completely
# unrelated test in a different file, and only when run in a particular order.
NOT_SCRATCH = "!string::starts_with(text, '[')"

# Only the `in` side is checked. On a `mentions` edge the `out` side is an
# entity, which has a `name` and no `text` at all -- and string::starts_with on
# NONE is a hard error, not a false. Every scratch edge has a scratch fact as its
# source, so the `in` side is sufficient as well as safe.
EDGE_NOT_SCRATCH = "!string::starts_with(in.text ?? '', '[')"


@pytest.fixture
def counts(corpus, connection: dict) -> dict[str, int]:
    """Corpus record counts, read fresh from the database for each test.

    Assertions compare against these rather than against the generator's own
    numbers, so a load that silently dropped records cannot pass.

    Function-scoped deliberately. A session-scoped version caches whatever the
    database happened to hold when the first test asked -- which makes every
    count assertion depend on module execution order, and produces failures that
    only appear in a full run and never when the file is run alone.
    """
    envelopes = query(f"""
        SELECT VALUE count() FROM fact WHERE {NOT_SCRATCH} GROUP ALL;
        SELECT VALUE count() FROM entity GROUP ALL;
        SELECT VALUE count() FROM mentions WHERE {EDGE_NOT_SCRATCH} GROUP ALL;
        SELECT VALUE count() FROM relates GROUP ALL;
        SELECT VALUE count() FROM supersedes WHERE {EDGE_NOT_SCRATCH} GROUP ALL;
        SELECT VALUE count() FROM fact WHERE valid_to = NONE AND {NOT_SCRATCH} GROUP ALL;
    """, **connection)

    def scalar(envelope: dict) -> int:
        """Unwrap a GROUP ALL count, which comes back as either an int or a row."""
        result = envelope["result"]
        if not result:
            return 0
        first = result[0]
        return int(first["count"]) if isinstance(first, dict) else int(first)

    keys = ["facts", "entities", "mentions", "relates", "supersedes", "current_facts"]
    return dict(zip(keys, (scalar(envelope) for envelope in envelopes)))


@pytest.fixture(scope="session")
def sql(connection: dict):
    """A terse helper for one-off statements inside a test.

    Wraps `run` so every test does not repeat the connection kwargs, and so a
    failing statement names the test that issued it.
    """
    def execute(label: str, statement: str, params: dict | None = None):
        return run(label, statement, params, **connection)

    return execute
