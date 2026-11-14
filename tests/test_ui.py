"""Browser tests for the station UI (Playwright + headless Chromium, ~10 s).

Skipped when Chromium is not installed: `uv run playwright install chromium`.
"""

import json
import time
from copy import deepcopy

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

from tests.live_server import LiveServer

NODE_NAMES = {
    "Ridge North", "Ridge East", "Old Cedar", "Fern Hollow", "Creek Bend", "Stone Ford",
    "Low Marsh", "Mossy Gap", "Logging Road", "South Gate", "Kapok Stand", "River Mouth",
}  # fmt: skip


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import station.receiver as receiver_module
    from sim import engine as engine_mod

    tmp = tmp_path_factory.mktemp("station")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("EVENTS_DB", str(tmp / "events.sqlite"))
        mp.setenv("KEYS_DIR", str(tmp / "keys"))
        mp.setenv("NODE_DATA_DIR", str(tmp / "node_data"))
        mp.setenv("LORA_UDP_PORT", "off")
        mp.setattr(receiver_module, "_default", None)
        engine_mod.reset_engine(None)
        srv = LiveServer().start()
        yield srv
        srv.stop()
        engine_mod.reset_engine(None)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except playwright_api.Error as e:  # browser binaries missing
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


def open_page(browser, server, path="/", **context):
    ctx = browser.new_context(**{"viewport": {"width": 1280, "height": 800}, **context})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server.url + path)
    expect(page.locator("body[data-live]")).to_be_attached(timeout=10_000)
    page.errors = errors
    return page


def detection(node_id, sound="chainsaw", confidence=0.82, verified=True, seq=1):
    return {
        "type": "detection",
        "detection": {
            "node_id": node_id,
            "seq": seq,
            "timestamp": int(time.time()),
            "lat": 0,
            "lon": 0,
            "sound": sound,
            "confidence": confidence,
            "model_version": 1,
            "clip_hash": "ab" * 16,
            "signature": "cd" * 64,
            "verified": verified,
        },
    }


def approval(approval_id="ap-9", incident_id="inc-1"):
    return {
        "type": "approval",
        "incident_id": incident_id,
        "approval_id": approval_id,
        "question": "Dispatch a team to E4?",
        "action": {"name": "dispatch_rangers", "args": {"sector": "E4", "note": "Team B"}},
    }


def test_world_renders_twelve_named_nodes(browser, server):
    page = open_page(browser, server)
    nodes = page.locator("[data-node-name]")
    expect(nodes).to_have_count(12)
    names = {nodes.nth(i).get_attribute("data-node-name") for i in range(12)}
    assert names == NODE_NAMES
    expect(page.locator("#model")).to_have_text("Mock")
    expect(page.locator("#scenario option")).to_have_count(2)
    assert page.errors == []


def test_detection_shows_in_feed_and_lights_node(browser, server):
    page = open_page(browser, server)
    server.publish(detection(7, confidence=0.82))  # Low Marsh
    page.click("#tab-feed")
    item = page.locator(".feed-item").first
    expect(item).to_contain_text("Chainsaw")
    expect(item).to_contain_text("82%")
    expect(item).to_contain_text("Low Marsh")
    expect(page.locator('[data-node-name="Low Marsh"]')).to_have_class("node amber")
    assert "7" not in item.inner_text().split()  # names, never numeric ids


def test_feed_survives_a_page_refresh(browser, server):
    from common.contracts import Detection, SoundClass
    from node.keys import load_signing_key
    from node.record import pack
    from station.receiver import get_receiver

    det = Detection(
        node_id=7, seq=int(time.time()), timestamp=int(time.time()), lat=1.0, lon=2.0,
        sound=SoundClass.CHAINSAW, confidence=0.9, model_version=1, clip_hash="ab" * 16,
    )  # fmt: skip
    outcome = server.call(get_receiver().handle, pack(det, load_signing_key(7)))
    assert outcome.accepted
    page = open_page(browser, server)  # fresh page: nothing arrives live, history only
    page.click("#tab-feed")
    expect(page.locator(".feed-item")).to_have_count(1)
    expect(page.locator(".feed-item").first).to_contain_text("Low Marsh")
    page.reload()
    page.click("#tab-feed")
    expect(page.locator(".feed-item")).to_have_count(1)  # reload and resync do not duplicate
    assert page.errors == []


def test_rejected_record_turns_node_red(browser, server):
    page = open_page(browser, server)
    server.publish({"type": "rejected", "node": "Creek Bend", "reason": "bad_signature"})
    page.click("#tab-feed")
    expect(page.locator(".feed-item.rejected").first).to_contain_text("Bad signature")
    expect(page.locator('[data-node-name="Creek Bend"]')).to_have_class("node red")


def test_approval_card_posts_decisions(browser, server):
    page = open_page(browser, server)
    posted = []

    def handle(route):
        posted.append((route.request.url, json.loads(route.request.post_data)))
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/approvals/**", handle)
    server.publish(approval("ap-9"))
    card = page.locator('.approval[data-approval-id="ap-9"]')
    card.get_by_role("button", name="Approve").click()
    expect(card).to_contain_text("Approved")
    assert posted[0][0].endswith("/api/approvals/ap-9")
    assert posted[0][1] == {"decision": "approve", "note": ""}

    server.publish(approval("ap-10"))
    card = page.locator('.approval[data-approval-id="ap-10"]')
    card.get_by_role("button", name="Edit").click()
    card.locator("textarea").fill("Team A instead")
    card.get_by_role("button", name="Send").click()
    expect(card).to_contain_text("Sent")
    assert posted[1][1] == {"decision": "edit", "note": "Team A instead"}


def test_phone_shows_approval_card_without_horizontal_scroll(browser, server):
    page = open_page(
        browser,
        server,
        viewport={"width": 375, "height": 812},
        is_mobile=True,
        has_touch=True,
    )
    server.publish(approval("ap-11"))
    card = page.locator('.approval[data-approval-id="ap-11"]')
    expect(card).to_be_in_viewport(ratio=1)
    approve = card.get_by_role("button", name="Approve").bounding_box()
    assert approve["height"] >= 44 and approve["width"] >= 44
    assert approve["y"] + approve["height"] <= 812
    assert page.evaluate("document.documentElement.scrollWidth") <= 375


def test_deep_link_opens_incident(browser, server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.route(
        "**/api/incidents/inc-7",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "inc-7",
                    "title": "Chainsaw near Creek Bend",
                    "sector": "E5",
                    "status": "triage",
                    "plan": "- [ ] triage",
                }
            ),
        ),
    )
    page.goto(server.url + "/incident/inc-7")
    expect(page.locator(".detail-title")).to_have_text("Chainsaw near Creek Bend")
    expect(page.locator("pre.plan")).to_have_text("- [ ] triage")
    page.get_by_role("button", name="Incidents").first.click()
    expect(page).to_have_url(server.url + "/")


def test_text_from_the_station_is_escaped(browser, server):
    page = open_page(browser, server)
    evil = '<img src=x onerror="document.body.dataset.pwned=1">'
    server.publish(
        {"type": "incident", "incident": {"id": "x1", "title": evil, "sector": "B3",
                                          "status": "open", "updated_at": int(time.time())}}
    )  # fmt: skip
    row = page.locator(".incident-row").filter(has_text="onerror")
    expect(row).to_contain_text(evil)
    expect(page.locator("#incident-list img")).to_have_count(0)
    assert page.locator("body[data-pwned]").count() == 0


def test_case_file_shows_server_html(browser, server):
    page = open_page(browser, server)
    page.route(
        "**/api/incidents/cf-1/case_file",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"html": "<h2>Timeline</h2><p>Record 4 verified.</p>"}),
        ),
    )
    server.publish(
        {"type": "incident", "incident": {"id": "cf-1", "title": "Case test", "sector": "E4",
                                          "status": "dispatched", "updated_at": int(time.time())}}
    )  # fmt: skip
    page.locator(".incident-row").filter(has_text="Case test").click()
    page.get_by_role("button", name="Case file").click()
    expect(page.locator("#casefile-body h2")).to_have_text("Timeline")


def test_tab_choice_is_remembered(browser, server):
    page = open_page(browser, server)
    page.click("#tab-feed")
    page.reload()
    expect(page.locator("#tab-feed")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#view-feed")).to_be_visible()


RESULTS = {
    "runs": {
        "night1:mock:fresh": {
            "scenario": "night1", "mode": "mock", "memory": "fresh", "partial": None,
            "real_incidents": ["logging-e4", "gunshots-g2"],
            "baseline": {"alerts": 31, "real_caught": 2, "real_missed": 0, "false_alerts": 22},
            "commander": {"requests": 4, "real_caught": 2, "real_missed": 0, "false_requests": 1,
                          "wall_s_per_incident": 2.5, "noisy_node": {}},
        },
        "night2:mock:fresh": {
            "scenario": "night2", "mode": "mock", "memory": "fresh",
            "commander": {"triage_steps_total": 40},
        },
        "night2:mock:carried": {
            "scenario": "night2", "mode": "mock", "memory": "carried",
            "commander": {"triage_steps_total": 25},
        },
    }
}  # fmt: skip


def page_with_results(browser, server, status, body=None, **context):
    ctx = browser.new_context(**{"viewport": {"width": 1280, "height": 800}, **context})
    page = ctx.new_page()
    page.route(
        "**/api/eval/latest",
        lambda r: r.fulfill(status=status, content_type="application/json", body=json.dumps(body)),
    )
    page.goto(server.url)
    expect(page.locator("body[data-live]")).to_be_attached(timeout=10_000)
    return page


def test_results_tab_shows_baseline_against_commander(browser, server):
    page = page_with_results(browser, server, 200, RESULTS)
    page.click("#tab-results")
    card = page.locator(".res-card")
    expect(card).to_have_count(1)
    expect(card.locator("tr", has_text="Baseline")).to_contain_text("31")
    expect(card.locator("tr", has_text="Commander")).to_contain_text("4")
    expect(card).to_contain_text("2/2")
    expect(card).to_contain_text("2.5 s per incident")
    expect(card).to_contain_text("Memory: 40 → 25 triage steps")


def test_results_tab_is_hidden_without_results(browser, server):
    page = page_with_results(browser, server, 404, {"detail": "No evaluation yet."})
    expect(page.locator("#tab-results")).to_be_hidden()


def test_results_fit_a_phone(browser, server):
    page = page_with_results(
        browser, server, 200, RESULTS,
        viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True,
    )  # fmt: skip
    page.click("#tab-results")
    expect(page.locator(".res-card")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 375


def test_results_keep_live_and_waiting_memory_runs_separate(browser, server):
    results = deepcopy(RESULTS)
    for pace, suffix, steps in (("live", "", 31), ("waits", ":waits", 17)):
        for night, memory in (("night1", "fresh"), ("night2", "fresh"), ("night2", "carried")):
            run = deepcopy(RESULTS["runs"][f"{night}:mock:{memory}"])
            run.update(mode="ollama", pace=pace, speed=30)
            if night == "night2" and memory == "carried":
                run["commander"]["triage_steps_total"] = steps
            results["runs"][f"{night}:ollama:{memory}{suffix}"] = run
    page = page_with_results(browser, server, 200, results)
    page.click("#tab-results")
    live = page.locator(".res-card").filter(has=page.get_by_role("heading", name="Night 1 · Local · live 30×"))
    waits = page.locator(".res-card").filter(has=page.get_by_role("heading", name="Night 1 · Local · sim waits"))
    expect(live).to_contain_text("Memory: 40 → 31 triage steps")
    expect(waits).to_contain_text("Memory: 40 → 17 triage steps")
