"""Drive the UI through night 1's logging incident and save light/dark, desktop/phone screenshots.

    uv run python tests/ui_screenshots.py [out_dir]     # default: docs/screenshots

Detections come from the stub feed (sim/demo_feed.py) and the drone really flies in the sim.
The commander's messages (incident, plan, steps, approval) and one rejected record are STAGED
here, since the agent and pipeline tracks are built separately.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright

from sim.engine import get_engine
from sim.scenario import offset_of
from tests.live_server import LiveServer

INCIDENT = "2026-11-15_E4"
VIEWS = {
    "desktop-light": {"viewport": {"width": 1440, "height": 900}, "color_scheme": "light"},
    "desktop-dark": {"viewport": {"width": 1440, "height": 900}, "color_scheme": "dark"},
    "phone-light": {
        "viewport": {"width": 375, "height": 812},
        "color_scheme": "light",
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 2,
    },
    "phone-dark": {
        "viewport": {"width": 375, "height": 812},
        "color_scheme": "dark",
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 2,
    },
}

PLAN = """# E4 chainsaw
- [x] Open incident: Arroyo Recodo, chainsaw 91% (record 1)
- [x] Triage: 3 nodes within 40 s, no rain -> likely real
- [{drone}] Drone over E4
- [ ] Ask ranger to dispatch
- [ ] Re-check in 30 min
"""


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    srv = LiveServer().start()
    engine = get_engine()

    def wait_until(clock_time: str) -> None:
        target = engine.scenario.start + offset_of(engine.scenario, clock_time)
        while engine.clock.now_f() < target:
            time.sleep(0.05)

    def incident(status: str, title: str) -> None:
        srv.publish(
            {
                "type": "incident",
                "incident": {
                    "id": INCIDENT,
                    "sector": "E4",
                    "status": status,
                    "title": title,
                    "updated_at": engine.now(),
                },
            }
        )

    def step(agent: str, summary: str) -> None:
        srv.publish(
            {"type": "agent_step", "incident_id": INCIDENT, "agent": agent, "summary": summary}
        )

    def plan(drone_done: bool) -> None:
        text = PLAN.format(drone="x" if drone_done else " ")
        srv.publish({"type": "plan", "incident_id": INCIDENT, "markdown": text})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pages = {}
        for name, opts in VIEWS.items():
            page = browser.new_context(**opts).new_page()
            page.goto(srv.url)
            page.wait_for_selector("[data-node-name]")
            expect(page.locator("#clock-time")).not_to_have_text("--:--")
            pages[name] = page

        page = pages["desktop-light"]
        page.request.post(
            f"{srv.url}/api/sim/scenario", data={"name": "night1", "speed": 60, "from": "01:24"}
        )

        wait_until("02:14:12")
        incident("triage", "Chainsaw near Arroyo Recodo")
        plan(False)
        step("commander", "Opened from Arroyo Recodo: chainsaw 91% (record 1).")
        wait_until("02:14:50")
        step("triage", "Paso Musgo and Brecha Vieja agree within 40 s. No rain in E4. Likely real.")
        wait_until("02:15:30")
        route = engine.plan_route(2167, 1700)
        engine.fly(route, wait=False)
        incident("investigating", "Chainsaw near Arroyo Recodo")
        step(
            "drone-pilot",
            f"Flying to E4: {route.distance_m / 1000:.1f} km, "
            f"{route.battery_needed_pct:.0f}% battery.",
        )
        wait_until("02:20:20")  # drone is over the target
        srv.call(engine.set_paused, True)
        step("drone-pilot", "Camera over E4: fresh clearing, one vehicle.")
        plan(True)
        incident("awaiting_approval", "Likely illegal logging")
        srv.publish(
            {
                "type": "approval",
                "incident_id": INCIDENT,
                "approval_id": "ap-1",
                "question": "3 nodes agree and the drone saw a fresh clearing with one vehicle. "
                "Dispatch a team to E4?",
                "action": {
                    "name": "dispatch_rangers",
                    "args": {"sector": "E4", "note": "Team B via the south track."},
                },
            }
        )
        srv.publish({"type": "rejected", "node": "El Bajo", "reason": "bad_signature"})
        time.sleep(1.5)

        pages["desktop-light"].screenshot(path=out / "desktop-light.png")

        dark = pages["desktop-dark"]
        dark.click(".incident-row")
        time.sleep(1.0)
        dark.screenshot(path=out / "desktop-dark.png")

        pages["phone-light"].screenshot(path=out / "phone-light.png")

        phone = pages["phone-dark"]
        phone.click("#tab-feed")
        time.sleep(0.5)
        phone.screenshot(path=out / "phone-dark.png")

        browser.close()
    srv.stop()
    print(f"saved {', '.join(f'{n}.png' for n in VIEWS)} to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots")
