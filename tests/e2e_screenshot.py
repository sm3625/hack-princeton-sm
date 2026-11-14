"""Screenshot of the live UI mid-incident, from the REAL station process (mock LLM).

    uv run python tests/e2e_screenshot.py [out.png]     # default: docs/screenshots/integrated.png

Starts the station like tests/test_e2e.py, plays night1 from 02:10, waits for the commander's
approval request, then captures the page. Nothing is staged.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright

from tests.test_e2e import Station


def main(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        station = Station(Path(tmp))
        try:
            station.wait_ready()
            station.follow_ws()
            station.request("POST", "/api/sim/scenario", {"name": "night1", "speed": 60, "from": "02:10"})
            station.wait_for("approval", lambda: [m for m in station.messages if m["type"] == "approval"])
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(station.url)
                expect(page.locator("body[data-live]")).to_be_attached(timeout=10_000)
                page.wait_for_timeout(1500)
                out.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out))
                browser.close()
        finally:
            station.stop()
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots" / "integrated.png")
