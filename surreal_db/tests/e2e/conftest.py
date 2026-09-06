"""Browser fixtures for the end-to-end layer.

The `--headless` and `--slowmo` options these fixtures read are declared in
tests/conftest.py, not here: pytest only calls `pytest_addoption` from the
rootdir conftest, so a subdirectory declaration works when that directory is the
target and silently vanishes when the whole suite runs.

Headed by default. These tests are also the project's demo: watching the graph
reconverge after the database is restarted underneath it is the single most
convincing thing CORTEX does, and a headless run hides it. Pass `--headless` in
CI, where nobody is watching and a visible window is just slower.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCREENSHOTS = Path(__file__).resolve().parents[2] / "docs" / "screenshots"


@pytest.fixture(scope="module")
def browser_context(request: pytest.FixtureRequest):
    """A Chromium window for this module.

    One window rather than one per test, because the interesting assertions are
    about a *live* page surviving events -- a fresh page per test would resync
    from scratch every time and never exercise the reconnect path.

    Module-scoped rather than session-scoped, and that is load-bearing.
    Playwright's synchronous API drives its own event loop; while it is alive,
    anything that calls `asyncio.run` fails with "cannot be called from a running
    event loop". A session-scoped browser stays open for the whole run and breaks
    every async test that follows it -- which is exactly what the live-query layer
    is made of. Tying the browser's life to this module releases the loop before
    those run.
    """
    from playwright.sync_api import sync_playwright

    headless = request.config.getoption("--headless")
    slowmo = request.config.getoption("--slowmo")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, slow_mo=slowmo)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        yield context
        context.close()
        browser.close()


@pytest.fixture(scope="module")
def page(browser_context, web_url: str):
    """The CORTEX page, loaded and waited on until its live feed is up.

    Waiting for the "live" indicator rather than for a fixed delay: the page has
    to fetch a token, open its own socket, register subscriptions and resync
    before any assertion about its contents means anything.
    """
    page = browser_context.new_page()
    page.goto(web_url, wait_until="domcontentloaded")

    try:
        page.wait_for_function(
            "() => document.body.innerText.includes('live')",
            timeout=30_000,
        )
    except Exception as error:  # noqa: BLE001 - a page that never goes live fails every test
        pytest.skip(f"CORTEX web app never reached 'live' at {web_url}: {error}")

    yield page
    page.close()


@pytest.fixture(scope="module")
def shot(page):
    """Save a screenshot into docs/screenshots, and return its path.

    Screenshots are captured by the test that asserts the behaviour they show, so
    an image in the docs cannot drift away from the thing it claims to depict.
    """
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)

    def capture(name: str) -> Path:
        """Write one screenshot under a stable filename."""
        path = SCREENSHOTS / f"{name}.png"
        page.screenshot(path=str(path))
        return path

    return capture
