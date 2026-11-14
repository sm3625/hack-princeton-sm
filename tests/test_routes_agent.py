"""Incident commander HTTP API: approvals only act on a pending interrupt; output is sanitized."""

import importlib
from types import SimpleNamespace

import pytest
from agent_fakes import FakeSim, Harness, Nodes, at
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.evidence import Ed25519Verifier
from agent.fallbacks import MemoryEventStore
from common.contracts import SoundClass as S
from common.contracts import load_world
from station import routes_agent


@pytest.fixture
def api(tmp_path):
    h = Harness(tmp_path)
    app = FastAPI()
    app.include_router(routes_agent.router)
    routes_agent.configure(h.commander)
    yield h, TestClient(app)  # no `with`: skip the router lifespan, use the configured commander
    routes_agent.configure(None)


def raise_incident(h):
    h.hear("Creek Bend", 41, "02:14:05", S.CHAINSAW, 0.91)
    h.hear("Mossy Gap", 17, "02:14:09", S.CHAINSAW, 0.84)
    h.tick("02:14:20")
    return h.commander.incidents.all()[0].id


def test_list_and_get_incident(api):
    h, client = api
    assert client.get("/api/incidents").json() == {"incidents": [], "busy": False}
    inc_id = raise_incident(h)
    (row,) = client.get("/api/incidents").json()["incidents"]
    assert row["id"] == inc_id and row["status"] == "awaiting_approval"
    view = client.get(f"/api/incidents/{inc_id}").json()
    assert view["pending_approval"]["action"] == "dispatch_rangers"
    assert "plan.md" in view["files"] and view["plan"]["html"].startswith("<h1>")
    assert [r["record"] for r in view["records"]] == ["[Creek Bend #41]", "[Mossy Gap #17]"]
    assert "node_id" not in str(view)
    assert client.get("/api/incidents/nope").status_code == 404


def test_approval_route_approves_once(api):
    h, client = api
    inc_id = raise_incident(h)
    approval_id = client.get(f"/api/incidents/{inc_id}").json()["pending_approval"]["approval_id"]
    res = client.post(f"/api/approvals/{approval_id}", json={"decision": "approve", "note": ""})
    assert res.status_code == 200 and res.json()["status"] == "approved"
    assert len(h.commander.incidents.get(inc_id).dispatches) == 1
    again = client.post(f"/api/approvals/{approval_id}", json={"decision": "approve"})
    assert again.status_code == 409
    assert len(h.commander.incidents.get(inc_id).dispatches) == 1


def test_approval_route_rejects_unknown_stale_and_bad_input(api):
    h, client = api
    assert client.post("/api/approvals/made-up", json={"decision": "approve"}).status_code == 404
    inc_id = raise_incident(h)
    approval_id = h.pending()[0].id
    assert client.post(f"/api/approvals/{approval_id}", json={"decision": "send_everyone"}).status_code == 422
    assert client.post(f"/api/approvals/{approval_id}", json={"decision": "edit", "note": " "}).status_code == 422
    assert client.post(f"/api/approvals/{approval_id}", json={"decision": "edit", "note": "x" * 501}).status_code == 422
    # a newer interrupt replaced this one (e.g. the agent asked again): old card is stale
    h.commander.approvals[approval_id].interrupt_id = "an-old-interrupt"
    assert client.post(f"/api/approvals/{approval_id}", json={"decision": "approve"}).status_code == 409
    assert h.commander.incidents.get(inc_id).dispatches == []


def test_case_file_html_is_sanitized(api):
    h, client = api
    inc_id = raise_incident(h)
    assert client.get(f"/api/incidents/{inc_id}/case_file").status_code == 404
    h.commander.files.write(
        f"/incidents/{inc_id}/case_file.md",
        "# Case\n\n<script>fetch('/api/approvals/x')</script>\n\n<img src=x onerror=alert(1)>\n\n"
        "[Creek Bend #41] heard a chainsaw. [click](javascript:alert(1))\n",
    )
    body = client.get(f"/api/incidents/{inc_id}/case_file").json()
    assert "<script" not in body["html"] and "onerror" not in body["html"] and "javascript:" not in body["html"]
    assert "[Creek Bend #41] heard a chainsaw." in body["html"]
    assert body["citations"]["ok"] and body["integrity"]["valid"] == 2


def test_ranger_message_route(api):
    h, client = api
    inc_id = raise_incident(h)
    assert client.post(f"/api/incidents/{inc_id}/messages", json={"text": "On site"}).status_code == 202
    assert client.post("/api/incidents/nope/messages", json={"text": "hi"}).status_code == 404
    assert client.post(f"/api/incidents/{inc_id}/messages", json={"text": ""}).status_code == 422


def test_routes_503_when_commander_not_running():
    app = FastAPI()
    app.include_router(routes_agent.router)
    routes_agent.configure(None)
    assert TestClient(app).get("/api/incidents").status_code == 503


def test_station_startup_wires_commander_into_receiver(monkeypatch, tmp_path):
    """The real station app: lifespan builds the commander and registers the on_verified hook."""
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("AGENT_FS_DIR", str(tmp_path / "agent_fs"))
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "state.sqlite"))
    import station.app

    importlib.reload(station.app)
    app = station.app.create_app()
    world = load_world()
    nodes = Nodes(world)
    app.state.receiver = SimpleNamespace(on_verified=[])
    app.state.event_store = MemoryEventStore()
    app.state.sim = FakeSim(world, start=at("02:14:00"))
    app.state.verify_record = Ed25519Verifier(nodes.public)
    with TestClient(app) as client:
        (hook,) = app.state.receiver.on_verified
        det, raw = nodes.record("Creek Bend", 41, at("02:14:05"), S.CHAINSAW, 0.91)
        app.state.event_store.add(det, raw)
        hook(det)
        (row,) = client.get("/api/incidents").json()["incidents"]
        assert row["nodes"] == ["Creek Bend"]
    assert routes_agent._commander is None  # stopped and cleared on shutdown
    assert (tmp_path / "agent_fs" / "memories" / "nodes.md").exists()


def test_dashboard_decision_is_recorded_as_dashboard_and_via_is_validated(api):
    h, client = api
    inc_id = raise_incident(h)
    approval_id = h.pending()[0].id
    assert client.post(f"/api/approvals/{approval_id}", json={"decision": "approve", "via": "fax"}).status_code == 422
    assert client.post(f"/api/approvals/{approval_id}", json={"decision": "reject", "note": "rain"}).status_code == 200
    actions = client.get(f"/api/incidents/{inc_id}").json()["ranger_actions"]
    assert actions[0] == {"time": actions[0]["time"], "decision": "rejected", "by": "ranger", "via": "dashboard",
                          "sector": actions[0]["sector"], "note": "rain"}
