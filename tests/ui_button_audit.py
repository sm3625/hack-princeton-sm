"""Browser regression audit against localhost; mutations and websocket events are mocked.
Run: .venv/bin/python tests/ui_button_audit.py
"""

import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
INC = {
    "id": "audit-closed",
    "title": "Completed audit incident",
    "sector": "E5",
    "status": "closed",
    "updated_at": 1794690000,
}
DETAIL = {
    "incident": INC,
    "plan": {"html": "<p>Verified audit plan</p>"},
    "trace": [
        {"kind": "wake", "agent": "station", "summary": "Review evidence", "t": 1794690000},
        {"kind": "status", "agent": "commander", "summary": "Incident closed", "t": 1794690010},
    ],
    "records": [],
    "approvals": [],
}


def main():
    checks = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width in (1440, 390):
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            sockets = []
            page.route_web_socket("**/ws", lambda ws, sockets=sockets: sockets.append(ws))
            page.route(
                "**/api/incidents", lambda r: r.fulfill(json={"incidents": [INC], "busy": False})
            )
            page.route("**/api/incidents/audit-closed", lambda r: r.fulfill(json=DETAIL))
            page.route(
                "**/api/incidents/audit-closed/case_file",
                lambda r: r.fulfill(status=404, json={"detail": "No case file yet."}),
            )
            page.goto("http://127.0.0.1:8000")
            page.locator(".incident-row").click()
            expect(page.get_by_role("button", name="Back to incidents")).to_be_visible()
            # Hold the real mouse down while an agent event rerenders detail content.
            back = page.get_by_role("button", name="Back to incidents")
            box = back.bounding_box()
            page.mouse.move(box["x"] + 20, box["y"] + 20)
            page.mouse.down()
            sockets[0].send(
                json.dumps(
                    {
                        "type": "agent_step",
                        "incident_id": INC["id"],
                        "agent": "commander",
                        "summary": "Live update",
                        "kind": "tool",
                    }
                )
            )
            page.wait_for_timeout(100)
            page.mouse.up()
            expect(page.locator("#view-incidents")).to_be_visible()
            checks.append(f"{width}: back survives live update")
            page.locator(".incident-row").click()
            page.get_by_role("button", name="Decision trail", exact=True).click()
            expect(page.locator("#trail-dialog")).to_be_visible()
            expect(page.locator("#trail-body")).to_contain_text("Incident closed")
            page.locator("#trail-close").click()
            expect(page.locator("#trail-dialog")).not_to_be_visible()
            page.get_by_role("button", name="Decision trail", exact=True).click()
            page.keyboard.press("Escape")
            checks.append(f"{width}: completed trail opens, closes and reopens")
            page.get_by_role("button", name="Case file & evidence").click()
            expect(page.locator("#casefile-body")).to_contain_text("No authored case file")
            expect(page.locator("#casefile-body")).to_contain_text("Verified audit plan")
            page.locator("#casefile-close").click()
            page.unroute("**/api/incidents/audit-closed/case_file")
            page.route(
                "**/api/incidents/audit-closed/case_file",
                lambda r: r.fulfill(
                    json={"html": "<h2>Saved case report</h2><p>Completed evidence.</p>"}
                ),
            )
            page.get_by_role("button", name="Case file & evidence").click()
            expect(page.locator("#casefile-body")).to_contain_text("Saved case report")
            page.keyboard.press("Escape")
            checks.append(f"{width}: missing and authored case files handled")
            page.locator("#tab-incidents").click()
            expect(page.locator("#view-incidents")).to_be_visible()
            page.locator("#tab-feed").click()
            expect(page.locator("#view-feed")).to_be_visible()
            page.locator("#tab-results").click()
            expect(page.locator("#view-results")).to_be_visible()
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#view-results")).to_contain_text("Cloud agent comparison")
            page.get_by_text("Case-by-case decisions", exact=True).click()
            expect(page.locator("#view-results")).to_contain_text("rain")
            page.screenshot(path=str(ROOT / f"emulators/evidence/audit-results-{width}.png"))
            checks.append(f"{width}: tabs, results refresh and detail disclosure")
            assert not errors, errors
            page.close()
        browser.close()
    print("\n".join(checks))


if __name__ == "__main__":
    main()
