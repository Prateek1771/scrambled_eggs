"""A tiny SurrealDB client over the HTTP /sql endpoint, standard library only.

Shared by db/verify.py and db/fake_writer.py. Both are meant to run before any
application code exists and without a virtualenv, so this deliberately avoids the
official SDK -- a schema check that needs `pip install` first is a schema check
people skip.

The API and the browser use real clients. This is for the scripts that stand up
and poke at the database.
"""

import base64
import json
import random
import sys
import urllib.error
import urllib.request

DIM = 1536


def embedding(seed: int) -> list[float]:
    """Build a deterministic unit-length vector of the width the HNSW index wants.

    Real embeddings come from OpenAI. These scripts care only about
    dimensionality and about distinct seeds landing far apart, so a seeded PRNG
    keeps them offline and reproducible. Gaussian coordinates in 1536 dimensions
    are near-orthogonal between seeds, which is what lets a fixture rely on "this
    fact is not semantically reachable" being true.
    """
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(DIM)]
    norm = sum(value * value for value in raw) ** 0.5 or 1.0
    return [value / norm for value in raw]


def near(seed: int, jitter: float = 0.15) -> list[float]:
    """Build a vector close to `embedding(seed)` but not identical to it.

    Used for facts that should sit in the same semantic neighbourhood, so vector
    recall has something to discriminate between rather than returning
    everything.
    """
    rng = random.Random(seed * 7919)
    base = embedding(seed)
    raw = [value + rng.gauss(0.0, jitter) for value in base]
    norm = sum(value * value for value in raw) ** 0.5 or 1.0
    return [value / norm for value in raw]


def query(
    surql: str,
    endpoint: str = "http://localhost:8000",
    namespace: str = "cortex",
    database: str = "main",
    user: str = "root",
    password: str = "root",
) -> list[dict]:
    """Execute SurrealQL and return the raw per-statement result envelopes.

    Returns the raw envelopes rather than unwrapping results because each
    statement carries its own status, and a batch can partially fail while the
    HTTP request itself succeeds with 200 -- trusting the status code alone hides
    real errors.
    """
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(
        f"{endpoint}/sql",
        data=surql.encode(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
            "surreal-ns": namespace,
            "surreal-db": database,
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        print(error.read().decode(), file=sys.stderr)
        raise


def bind(params: dict) -> str:
    """Render parameters as leading LET statements.

    The HTTP /sql endpoint takes no separate variable payload, so parameters are
    prepended to the batch. json.dumps produces valid SurrealQL for every type
    used here -- but note that a record id arrives as a plain string and has to be
    turned back with `type::record()` before it can be used as one.
    """
    return "".join(f"LET ${name} = {json.dumps(value)};\n" for name, value in params.items())


def run(label: str, surql: str, params: dict | None = None, attempts: int = 5,
        **connection) -> list:
    """Execute a statement batch, retrying conflicts, exiting loudly on real errors.

    Failing hard rather than returning an error keeps callers down to assertions
    about data instead of assertions about plumbing. `label` names the batch in
    the failure message, because "failed transaction" on its own identifies
    nothing.

    Transaction conflicts are the exception: SurrealDB reports them as retryable,
    and they occur whenever another test or the agent touches the same records.
    Exiting on one would turn ordinary contention into a failed corpus load.
    """
    import random
    import time

    statement = bind(params or {}) + surql
    for attempt in range(attempts):
        envelopes = query(statement, **connection)
        failures = [e for e in envelopes if e.get("status") != "OK"]
        if not failures:
            return [envelope["result"] for envelope in envelopes]

        text = " ".join(str(f.get("result", "")) for f in failures).lower()
        retryable = ("conflict" in text or "can be retried" in text
                     or "resource busy" in text)
        if not retryable or attempt == attempts - 1:
            print(f"FAIL [{label}] {failures[0].get('result')}", file=sys.stderr)
            sys.exit(1)
        time.sleep(0.02 * (2 ** attempt) * (0.5 + random.random()))
    return []
