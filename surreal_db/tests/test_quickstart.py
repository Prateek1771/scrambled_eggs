"""L10 - the README's quickstart, executed.

For three milestones the README promised "Three containers" and
`http://localhost:3000` while `docker-compose.yml` defined neither a `web`
service nor a seed. Anyone who cloned the repo and followed the instructions got
nothing, and no test noticed, because every other test runs against a stack that
was already up.

This one starts from an empty volume and does exactly what the README says. It is
slow and destructive by nature -- it drops the database -- so it is marked
`chaos` and excluded from ordinary runs:

    pytest tests/test_quickstart.py -m chaos
"""

from __future__ import annotations

import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.slow, pytest.mark.chaos]


def _compose(*arguments: str, timeout: int = 900) -> subprocess.CompletedProcess:
    """Run a docker compose command from the project root."""
    return subprocess.run(["docker", "compose", *arguments], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout, check=True)


def _documented_port() -> int:
    """Read the port the quickstart actually publishes.

    From `.env` if it overrides it, otherwise the documented default. The test
    follows the same resolution the user's machine does -- hard-coding 3000 here
    would fail on any machine that already uses it, which is exactly the
    situation the override exists for.
    """
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*WEB_PORT\s*=\s*(\d+)", line)
            if match:
                return int(match.group(1))
    return 3000


def _wait_for_http(url: str, timeout: float = 180.0) -> int:
    """Poll a URL until it answers, returning the status code."""
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status
        except Exception as error:  # noqa: BLE001 - still starting
            last = error
            time.sleep(2.0)
    raise AssertionError(f"{url} never answered within {timeout:.0f}s: {last}")


@pytest.fixture(scope="module")
def clean_stack(corpus, connection):
    """Destroy everything, bring it up as the README instructs, then put it back.

    `down -v` removes the named volume, so this genuinely starts from nothing --
    the state a person cloning the repo is in. Anything that only works because a
    previous run left data behind fails here.

    The restore afterwards is not tidiness, it is correctness. This module
    deletes the corpus that every other module asserts against, so without
    reloading it the suite would pass or fail depending on which file pytest
    happened to collect first. Depending on `corpus` also guarantees the fixture
    exists before the volume is destroyed.
    """
    import corpus as corpus_module

    _compose("down", "-v")
    _compose("up", "-d", "--build", timeout=1800)
    yield

    # The stack stays up -- other modules need it -- but the seed's small corpus
    # is replaced with the one the rest of the suite was built against.
    corpus_module.reset(**connection)
    corpus_module.load(corpus, **connection)


def test_the_quickstart_brings_up_three_containers(clean_stack):
    """`docker compose up` leaves surrealdb, api and web running.

    The README says three. Two one-shot jobs (migrate, seed) run and exit, which
    is why the wording is "three long-running containers" -- this asserts the
    claim as written.
    """
    result = _compose("ps", "--format", "{{.Service}} {{.State}}")
    running = {
        line.split()[0]
        for line in result.stdout.strip().splitlines()
        if line.strip().endswith("running")
    }
    assert {"surrealdb", "api", "web"} <= running, (
        f"the README promises surrealdb, api and web; running: {sorted(running)}"
    )


def test_the_migration_and_seed_both_succeeded(clean_stack):
    """Both one-shot jobs exited cleanly.

    A migration that failed leaves the API talking to a schemaless database, and
    a seed that failed leaves the first frame empty. Neither shows up as a
    stopped container, so both exit codes are checked.
    """
    result = _compose("ps", "-a", "--format", "{{.Service}} {{.ExitCode}}")
    codes = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            codes[parts[0]] = int(parts[1])

    assert codes.get("migrate") == 0, f"migration exited {codes.get('migrate')}"
    assert codes.get("seed") == 0, f"seed exited {codes.get('seed')}"


def test_the_documented_url_serves_the_app(clean_stack):
    """The address in the README answers.

    This is the assertion the quickstart never had. It failed for three
    milestones and nothing caught it, because no test ever opened the documented
    port.
    """
    port = _documented_port()
    status = _wait_for_http(f"http://localhost:{port}")
    assert status == 200, f"http://localhost:{port} returned {status}"


def test_the_api_hands_the_browser_a_database_url(clean_stack):
    """`/viewer-token` tells the browser where to connect.

    The web image is built once and must run anywhere, so it cannot have a
    database address compiled into it -- `NEXT_PUBLIC_*` values are inlined at
    build time. The API supplies it at runtime instead, and this is the contract
    that makes the image portable.
    """
    with urllib.request.urlopen("http://localhost:8080/viewer-token", timeout=15) as response:
        import json

        payload = json.load(response)

    assert payload.get("url", "").startswith(("ws://", "wss://")), payload.get("url")
    assert payload.get("namespace") and payload.get("database")
    body = json.dumps(payload).lower()
    assert "sk-" not in body and "openai" not in body, "the token response leaks a credential"


def test_a_fresh_clone_opens_on_a_populated_graph(clean_stack, endpoint):
    """The first frame is not empty.

    A visualisation that opens on a blank canvas is the worst possible
    introduction to a project selling a visualisation. The seed replays
    `tests/corpus.py`, so the graph arrives with supersession chains and
    multi-hop associations already in it.
    """
    import sys

    sys.path.insert(0, str(ROOT / "db"))
    from surreal_http import query

    envelopes = query("""
        SELECT VALUE count() FROM fact GROUP ALL;
        SELECT VALUE count() FROM entity GROUP ALL;
        SELECT VALUE count() FROM supersedes GROUP ALL;
    """, endpoint=endpoint)

    def scalar(result) -> int:
        """Unwrap a GROUP ALL count."""
        if not result:
            return 0
        return result[0]["count"] if isinstance(result[0], dict) else result[0]

    facts, entities, superseded = (scalar(e["result"]) for e in envelopes)

    assert facts > 100, f"only {facts} facts were seeded"
    assert entities > 50, f"only {entities} entities were seeded"
    assert superseded > 10, (
        f"only {superseded} supersessions -- the first frame should already show "
        f"history, since that is the behaviour the graph exists to display"
    )
    print(f"\n  first frame: {facts} facts, {entities} entities, {superseded} superseded")
