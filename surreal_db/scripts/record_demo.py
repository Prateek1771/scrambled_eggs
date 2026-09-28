"""Record the README's animated demo: a memory graph building itself.

The project's pitch is *watch* a mind form, and five still screenshots cannot
carry that. This drives a real browser against a real stack, captures frames
while `db/fake_writer.py` writes, and assembles them into `docs/demo.gif`.

Nothing here is staged. The frames are of the actual application, fed by actual
writes arriving over the browser's own WebSocket to SurrealDB -- which is the
claim the GIF is there to make.

    python scripts/record_demo.py                    # default: the story, ~20s
    python scripts/record_demo.py --width 1000       # bigger, larger file
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))


def clear_memory(endpoint: str) -> None:
    """Empty the graph so the recording starts from nothing.

    The first frame has to be an empty canvas: the whole point of the animation
    is that structure appears where there was none.
    """
    from surreal_http import run

    run("demo-reset", """
        DELETE supersedes; DELETE mentions; DELETE relates; DELETE derived_from;
        DELETE retrieval; DELETE fact; DELETE entity; DELETE message; DELETE session;
    """, endpoint=endpoint)


def capture(web_url: str, endpoint: str, frames: int, interval: float,
            beat_delay: float, width: int) -> list[bytes]:
    """Drive the writer and screenshot the page while it runs.

    Returns raw PNG bytes per frame. The writer runs as a subprocess rather than
    in-process so the recording is of the same code path a person would run, and
    so its pacing is independent of the screenshot loop.
    """
    from playwright.sync_api import sync_playwright

    shots: list[bytes] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1680, "height": 900}).new_page()
        page.goto(web_url, wait_until="domcontentloaded")
        page.wait_for_function("() => document.body.innerText.includes('live')", timeout=60_000)

        writer = subprocess.Popen(
            [sys.executable, str(ROOT / "db" / "fake_writer.py"),
             "--delay", str(beat_delay), "--endpoint", endpoint],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        try:
            for _ in range(frames):
                shots.append(page.screenshot())
                time.sleep(interval)
        finally:
            writer.terminate()
            # A few frames after the last write, so the animation ends on the
            # settled graph rather than mid-motion.
            for _ in range(4):
                time.sleep(interval)
                shots.append(page.screenshot())
            browser.close()

    return shots


def assemble(shots: list[bytes], destination: Path, width: int, frame_ms: int) -> None:
    """Turn PNG frames into an optimised looping GIF.

    Downscaled and palette-reduced deliberately: a README GIF that takes ten
    seconds to load is a README GIF nobody sees. Quality is spent on legibility
    of the graph, not on colour depth.
    """
    from PIL import Image

    images = []
    for raw in shots:
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        height = round(image.height * width / image.width)
        image = image.resize((width, height), Image.LANCZOS)
        # An adaptive palette keeps the glow hues distinguishable, which is the
        # one thing in the frame that carries meaning.
        images.append(image.convert("P", palette=Image.ADAPTIVE, colors=128))

    destination.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        destination,
        save_all=True,
        append_images=images[1:],
        duration=frame_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )


def main() -> None:
    """Record the demo and report where it landed, and how big it is."""
    parser = argparse.ArgumentParser(description="Record docs/demo.gif")
    parser.add_argument("--web", default="http://localhost:3100")
    parser.add_argument("--endpoint", default="http://localhost:8000")
    parser.add_argument("--frames", type=int, default=44)
    parser.add_argument("--interval", type=float, default=0.55,
                        help="seconds between screenshots")
    parser.add_argument("--beat-delay", type=float, default=0.55,
                        help="seconds between writes in the story")
    parser.add_argument("--width", type=int, default=900)
    parser.add_argument("--frame-ms", type=int, default=140)
    parser.add_argument("--out", default=str(ROOT / "docs" / "demo.gif"))
    arguments = parser.parse_args()

    print("clearing memory")
    clear_memory(arguments.endpoint)

    print(f"recording {arguments.frames} frames from {arguments.web}")
    shots = capture(arguments.web, arguments.endpoint, arguments.frames,
                    arguments.interval, arguments.beat_delay, arguments.width)

    destination = Path(arguments.out)
    print(f"assembling {len(shots)} frames")
    assemble(shots, destination, arguments.width, arguments.frame_ms)

    size_mb = destination.stat().st_size / 1e6
    print(f"wrote {destination.relative_to(ROOT)} -- {size_mb:.1f} MB, {len(shots)} frames")
    if size_mb > 8:
        print("  that is large for a README; try --width 700 or fewer --frames")


if __name__ == "__main__":
    main()
