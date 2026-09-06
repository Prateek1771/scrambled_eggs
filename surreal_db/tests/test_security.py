"""L2 - containment of the browser's credential, and credential leakage.

CORTEX does something unusual: the browser holds its own WebSocket to the
database. That is the whole demo, and it is also the reason this layer exists --
it is entirely reasonable for a reader to ask what else the browser is holding.

The answer has to be "a short-lived read-only token, and nothing else", and it
has to be asserted rather than stated.

One trap runs through all of it: a VIEWER's writes are **accepted and
discarded**, not rejected. Every containment check therefore verifies by
counting rows, never by expecting an error.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))
from surreal_http import query  # noqa: E402


def _token(api_url: str) -> dict:
    """Fetch a viewer token from the API, as the browser does."""
    try:
        with urllib.request.urlopen(f"{api_url}/viewer-token", timeout=10) as response:
            return json.load(response)
    except urllib.error.URLError as error:
        pytest.skip(f"CORTEX API not reachable at {api_url}: {error}")


def _as_viewer(endpoint: str, token: str, statement: str) -> list[dict]:
    """Run SurrealQL authenticated as the browser's viewer token.

    Returns the raw envelopes. A statement the viewer may not perform can come
    back either as an error *or* as a successful no-op, and both need to be
    visible to the caller.
    """
    request = urllib.request.Request(
        f"{endpoint}/sql",
        data=statement.encode(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "surreal-ns": "cortex",
            "surreal-db": "main",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return [{"status": "ERR", "result": error.read().decode()}]


@pytest.fixture(scope="module")
def viewer(api_url: str) -> dict:
    """The browser's credential, minted the same way the browser mints it."""
    return _token(api_url)


# --------------------------------------------------------------------------
# what the token is
# --------------------------------------------------------------------------


def test_viewer_token_is_short_lived(viewer):
    """The browser's credential expires, and soon enough to matter.

    A token that never expires is a password with extra steps.
    """
    assert viewer["token"], "no token was issued"
    lifetime = viewer["expires_at"] - time.time()
    assert 0 < lifetime <= 24 * 3600, f"token lifetime is {lifetime / 3600:.1f}h"


def test_viewer_token_carries_no_secret_beyond_its_own_scope(viewer):
    """The JWT names a namespace, a database and a viewer identity -- nothing else.

    Decoded without verification on purpose: the point is what a *reader* of the
    token can see, and anyone holding it can read its payload.
    """
    payload = viewer["token"].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))

    assert claims.get("NS") == "cortex"
    assert claims.get("DB") == "main"
    assert claims.get("ID") == "cortex_viewer", claims
    blob = json.dumps(claims).lower()
    for forbidden in ("sk-", "openai", "root", "password", "secret"):
        assert forbidden not in blob, f"token payload leaks {forbidden!r}: {claims}"


# --------------------------------------------------------------------------
# what the token can do
# --------------------------------------------------------------------------


def test_viewer_can_read(viewer, endpoint, counts):
    """The viewer must be able to read, or the whole live-graph design collapses."""
    envelopes = _as_viewer(endpoint, viewer["token"],
                           "SELECT VALUE count() FROM fact "
                           "WHERE !string::starts_with(text, '[') GROUP ALL;")
    assert envelopes[0]["status"] == "OK", envelopes
    result = envelopes[0]["result"]
    value = result[0]["count"] if isinstance(result[0], dict) else result[0]
    assert value == counts["facts"]


def test_viewer_writes_land_nowhere(viewer, endpoint):
    """A write issued with the viewer token must change nothing.

    The trap: SurrealDB answers this with `status: OK` and an empty result rather
    than an error. A containment test that asserts on the error would pass while
    the write succeeded -- so this counts rows before and after instead.
    """
    marker = f"[security-probe-{int(time.time())}]"

    before = query("SELECT VALUE count() FROM fact GROUP ALL;",
                   endpoint=endpoint)[0]["result"]  # unfiltered: this one counts its own probe
    envelopes = _as_viewer(endpoint, viewer["token"], f"""
        CREATE fact SET text = '{marker}', embedding = array::repeat(0.0, 1536),
                        confidence = 0.5;
    """)
    after = query("SELECT VALUE count() FROM fact GROUP ALL;", endpoint=endpoint)[0]["result"]

    def scalar(result):
        """Unwrap a GROUP ALL count, which may be an int or a row."""
        return result[0]["count"] if isinstance(result[0], dict) else result[0]

    assert scalar(before) == scalar(after), (
        f"the viewer token created a record. Response was: {envelopes}"
    )

    landed = query("SELECT VALUE count() FROM fact WHERE text = $m GROUP ALL;",
                   endpoint=endpoint)  # noqa: F841 - shape check below
    probe = query(f"SELECT VALUE count() FROM fact WHERE text = '{marker}' GROUP ALL;",
                  endpoint=endpoint)[0]["result"]
    assert not probe or scalar(probe) == 0, "the viewer's write is present in the table"
    assert landed is not None


def test_viewer_cannot_change_the_schema(viewer, endpoint):
    """DEFINE is the escalation that matters, so it gets its own assertion.

    A viewer that can define a table can define one with permissive
    `PERMISSIONS`, which would make every other containment check meaningless.
    """
    _as_viewer(endpoint, viewer["token"],
               "DEFINE TABLE security_probe SCHEMALESS PERMISSIONS FULL;")

    tables = query("INFO FOR DB;", endpoint=endpoint)[0]["result"]["tables"]
    assert "security_probe" not in tables, "the viewer token defined a table"


def test_viewer_cannot_delete(viewer, endpoint, counts):
    """Deletion is the one irreversible thing in a system built on never deleting."""
    _as_viewer(endpoint, viewer["token"], "DELETE fact WHERE confidence < 2.0;")
    after = query("SELECT VALUE count() FROM fact "
                  "WHERE !string::starts_with(text, '[') GROUP ALL;",
                  endpoint=endpoint)[0]["result"]
    value = after[0]["count"] if isinstance(after[0], dict) else after[0]
    assert value == counts["facts"], "the viewer token deleted facts"


# --------------------------------------------------------------------------
# what reaches the browser
# --------------------------------------------------------------------------


def test_no_public_env_var_carries_a_credential():
    """The standing rule from docs/02, asserted mechanically rather than promised.

    `NEXT_PUBLIC_*` is compiled into the JavaScript bundle and is readable by
    anyone who opens the page. The rule is that none of them may carry an OpenAI
    or database credential -- and if one ever appears in a diff, that is the bug.
    """
    for name in (".env.example", ".env"):
        path = ROOT / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped.startswith("NEXT_PUBLIC_"):
                continue
            key, _, value = stripped.partition("=")
            lowered = f"{key}={value}".lower()
            for forbidden in ("sk-", "openai", "api_key", "apikey", "password", "pass=",
                              "secret", "token"):
                assert forbidden not in lowered, (
                    f"{name}:{number} exposes a credential to the browser: {key}"
                )


def test_the_api_never_returns_the_openai_key(api_url, viewer):
    """No API response may contain the OpenAI key, however the endpoint is shaped.

    `/viewer-token` is the only endpoint the browser calls before the agent
    exists, so it is the one checked here. The assertion is deliberately blunt:
    scan the whole response body.
    """
    body = json.dumps(viewer).lower()
    assert "sk-" not in body, "an OpenAI key appeared in the viewer-token response"
    assert "openai" not in body


@pytest.mark.skipif(not (ROOT / "web" / ".next").exists(),
                    reason="web app has not been built; run `npm run build` in web/")
def test_no_credential_is_compiled_into_the_javascript_bundle():
    """Grep the built client bundle for anything that should never have left the server.

    This is the assertion that would actually catch the mistake. A key added to a
    `NEXT_PUBLIC_` variable is inlined into the JavaScript at build time, and no
    amount of care in the source prevents that once the variable is named wrong.
    """
    suspicious: list[str] = []
    for path in (ROOT / "web" / ".next").rglob("*.js"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "sk-proj-" in text or "sk-ant-" in text:
            suspicious.append(str(path.relative_to(ROOT)))
        # The root database password must never be inlined either.
        if "SURREAL_ROOT_PASS" in text:
            suspicious.append(f"{path.relative_to(ROOT)} (root password name)")

    assert not suspicious, f"credentials found in the client bundle: {suspicious[:5]}"
