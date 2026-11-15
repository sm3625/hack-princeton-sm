"""No Photon network, credentials, paid models or provisioning in these tests."""
import copy
from urllib.parse import parse_qs, urlsplit

import pytest
from agent_fakes import Harness
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.contracts import SoundClass as S
from station import photon as p
from station import routes_agent

TARGET = p.Target("project", "resource", "space", "agent", "ranger")


class FakePhoton:
    target = TARGET

    def __init__(self, limit=10):
        self.remaining, self.sent, self.pages, self.queries = limit, [], [], []
        self.fail = False

    def send(self, text, identity):
        assert self.remaining > 0
        self.remaining -= 1
        self.sent.append((text, identity))
        if self.fail:
            raise p.BridgeError("uncertain")

    def page(self, since, before, token):
        self.queries.append((since, before, token))
        return self.pages.pop(0) if self.pages else {"items": []}


def incoming(text, identity="message"):
    return {"id": identity, "projectId": "project", "platform": "imessage", "status": "received",
                "sentBy": {"id": "ranger", "kind": "human"},
                "space": {"id": "space", "kind": "direct", "participantIds": ["agent", "ranger"]},
                "content": {"type": "text", "text": text}}


@pytest.fixture
def setup(tmp_path):
    h = Harness(tmp_path)
    app = FastAPI()
    app.include_router(routes_agent.router)
    routes_agent.configure(h.commander)
    client = TestClient(app)

    def transport(method, url, body=None):
        response = client.request(method, urlsplit(url).path, json=body)
        if response.status_code >= 400:
            raise p.BridgeError(f"HTTP {response.status_code}")
        return response.json()

    h.hear("Arroyo Recodo", 41, "02:14:05", S.CHAINSAW, 0.91)
    h.hear("Paso Musgo", 17, "02:14:09", S.CHAINSAW, 0.84)
    h.tick("02:14:20")
    station = p.Station(transport=transport)
    provider = FakePhoton()
    now = [1000.0]
    bridge = p.Bridge(station, provider, tmp_path / "bridge.sqlite", lambda: now[0])
    bridge.discover()
    code = bridge.db.execute("SELECT code FROM approvals").fetchone()[0]
    yield h, bridge, provider, code, now
    bridge.db.close()
    client.close()
    routes_agent.configure(None)


def test_real_commander_approval_and_duplicate_after_restart(setup, tmp_path):
    h, bridge, provider, code, now = setup
    bridge.flush()
    assert len(provider.sent) == 1
    message = incoming(f"APPROVE {code}")
    bridge.receive(message)
    assert len(h.commander.incidents.all()[0].dispatches) == 1
    bridge.flush()
    assert len(provider.sent) == 2
    bridge.db.close()
    resumed = p.Bridge(bridge.station, provider, tmp_path / "bridge.sqlite", lambda: now[0])
    try:
        resumed.receive(message)
        resumed.receive(incoming(f"APPROVE {code}", "second-command"))
        resumed.discover()
        resumed.flush()
    finally:
        resumed.db.close()
    assert len(provider.sent) == 3  # Second command reports already decided, no new dispatch.
    assert len(h.commander.incidents.all()[0].dispatches) == 1


def test_reject_routes_reason_through_gate(setup):
    h, bridge, provider, code, _ = setup
    bridge.receive(incoming(f"REJECT {code} Rain confirmed"))
    assert not h.commander.incidents.all()[0].dispatches
    assert h.commander.approvals[next(iter(h.commander.approvals))].status == "rejected"
    bridge.flush()
    assert len(provider.sent) == 1  # Expired approval notification is suppressed.
    assert provider.sent[0][0] == "1: recorded. Check dashboard."


def test_short_number_reply_is_enough(setup):
    h, bridge, provider, code, _ = setup
    assert code == "1"  # Short number, not a hash
    bridge.flush()
    assert provider.sent[0][0].splitlines()[-1] == "Reply YES 1 or NO 1. Expires in 15 minutes."
    bridge.receive(incoming("NO 99", "unknown-number"))  # Unknown number: nothing happens
    assert not h.commander.incidents.all()[0].dispatches
    bridge.receive(incoming("YES 1", "short-yes"))
    assert len(h.commander.incidents.all()[0].dispatches) == 1


@pytest.mark.parametrize("mutation", [
    {"projectId": "other"}, {"platform": "sms"}, {"status": "dispatched"},
    {"sentBy": {"id": "stranger", "kind": "human"}},
    {"sentBy": {"id": "ranger", "kind": "agent"}},
    {"space": {"id": "other", "kind": "direct", "participantIds": ["agent", "ranger"]}},
    {"space": {"id": "space", "kind": "group", "participantIds": ["agent", "ranger"]}},
    {"receivedBy": {"id": "other"}}, {"content": {"type": "image"}},
])
def test_wrong_conversation_or_sender_ignored(setup, mutation):
    h, bridge, _, code, _ = setup
    msg = incoming(f"APPROVE {code}")
    msg.update(mutation)
    bridge.receive(msg)
    assert not h.commander.incidents.all()[0].dispatches
    assert bridge.db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0


@pytest.mark.parametrize("command", ["yes", "APPROVE {code} please", "ignore rules; APPROVE {code}",
                                     "APPROVE 1234567890"])
def test_explicit_command_required(setup, command):
    h, bridge, _, code, _ = setup
    bridge.receive(incoming(command.format(code=code)))
    assert not h.commander.incidents.all()[0].dispatches


def test_expired_approval_not_sent_or_executed(setup):
    h, bridge, provider, code, now = setup
    now[0] += 901
    bridge.flush()
    assert provider.sent == []
    bridge.receive(incoming(f"APPROVE {code}"))
    assert not h.commander.incidents.all()[0].dispatches


def test_ambiguous_send_never_retried(setup):
    _, bridge, provider, _, _ = setup
    provider.fail = True
    with pytest.raises(p.BridgeError):
        bridge.flush()
    bridge.flush()
    assert len(provider.sent) == 1
    assert bridge.db.execute("SELECT state FROM outbox").fetchone()[0] == "attempting"


def test_failed_station_action_is_not_replayed(setup):
    _, bridge, _, code, _ = setup
    original = bridge.station.call
    calls = []

    def failing(method, path, body=None):
        if method == "POST":
            calls.append(path)
            raise p.BridgeError("uncertain")
        return original(method, path, body)

    bridge.station.call = failing
    msg = incoming(f"APPROVE {code}")
    with pytest.raises(p.BridgeError):
        bridge.receive(msg)
    bridge.receive(msg)
    assert len(calls) == 1
    assert bridge.db.execute("SELECT outcome FROM inbox").fetchone()[0] == "processing"


def test_poll_empty_page_token_and_fixed_window(setup):
    _, bridge, provider, _, now = setup
    now[0] += 10
    provider.pages = [{"items": [], "nextPageToken": "next"}, {"items": []}]
    bridge.poll()
    assert provider.queries[0][:2] == provider.queries[1][:2]
    assert provider.queries[1][2] == "next"
    assert bridge.meta("since") == p.stamp(1000)  # Never replay before first startup.
    now[0] += 60
    bridge.poll()
    assert bridge.meta("since") == p.stamp(1040)


def test_pagination_cap_does_not_advance_cursor(setup):
    _, bridge, provider, _, _ = setup
    before = bridge.meta("since")
    provider.pages = [{"items": [], "nextPageToken": "next"}] * 5
    with pytest.raises(p.BridgeError, match="page limit"):
        bridge.poll()
    assert bridge.meta("since") == before


def test_target_binding_blocks_cross_account_reuse(setup, tmp_path):
    _, bridge, provider, _, _ = setup
    other = copy.copy(provider)
    other.target = p.Target("other", "resource", "space", "agent", "ranger")
    with pytest.raises(p.BridgeError, match="Target changed"):
        p.Bridge(bridge.station, other, tmp_path / "bridge.sqlite")


def test_exact_api_contract_and_budget():
    calls = []

    def transport(*args):
        calls.append(args)
        return {"items": []}

    photon = p.Photon(TARGET, "pho_sk_test", 1, transport)
    photon.page("since", "before", "token")
    query = parse_qs(urlsplit(calls[0][1]).query)
    assert query["resourceId"] == ["resource"] and "spaceId" not in query
    assert query["pageToken"] == ["token"] and query["direction"] == ["received"]
    photon.send("hello", "unique-key")
    assert calls[1][2] == {"senderId": "agent", "spaceId": "space",
                           "content": {"type": "text", "format": "plain", "text": "hello"}}
    assert calls[1][3]["idempotency-key"] == "unique-key"
    with pytest.raises(p.BridgeError, match="allowance"):
        photon.send("no", "another")
    assert len(calls) == 2


def test_failed_send_consumes_budget():
    def fail(*args):
        raise p.BridgeError("timeout")
    photon = p.Photon(TARGET, "pho_sk_test", 1, fail)
    with pytest.raises(p.BridgeError):
        photon.send("hello", "key")
    assert photon.remaining == 0


def test_default_zero_budget_never_calls_transport():
    photon = p.Photon(TARGET, "pho_sk_test", transport=lambda *a: pytest.fail("network"))
    with pytest.raises(p.BridgeError):
        photon.send("hello", "key")


def test_unlimited_send_allowance_stays_available():
    sent = []
    photon = p.Photon(TARGET, "pho_sk_test", None, lambda method, *_: sent.append(method))
    photon.send("first", "one")
    photon.send("second", "two")
    assert sent == ["POST", "POST"] and photon.remaining is None


def test_preview_with_credentials_never_constructs_photon(setup, monkeypatch, capsys):
    _, bridge, _, _, _ = setup
    monkeypatch.setenv("PHOTON_API_KEY", "pho_sk_test")
    monkeypatch.setattr(p, "Station", lambda *a: bridge.station)
    monkeypatch.setattr(p, "Photon", lambda *a: pytest.fail("Photon constructed in preview"))
    assert p.main([]) == 0
    assert "PREVIEW ONLY" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        p.main(["--allow-sends", "1"])


@pytest.mark.parametrize("url", ["https://example.com", "http://localhost@evil.test",
                                 "http://127.0.0.1/remote", "http://user@localhost"])
def test_station_is_loopback_only(url):
    with pytest.raises(p.BridgeError):
        p.Station(url)


def test_http_errors_do_not_leak_body_or_key(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    class Opener:
        def open(self, *args, **kwargs):
            raise HTTPError("secret-url", 402, "secret-key", {}, BytesIO(b"secret-body"))

    monkeypatch.setattr(p, "build_opener", lambda *a: Opener())
    with pytest.raises(p.BridgeError) as exc:
        p.request_json("GET", "https://api.photon.codes")
    assert str(exc.value) == "HTTP 402; stopped without retry"
    assert p.NoRedirects().redirect_request(None, None, 302, "", {}, "https://evil") is None


DISCOVER_PRJ = "pho_prj_" + "0" * 26


def test_discover_reads_ids_from_one_get_and_sends_nothing():
    calls = []

    def transport(method, url, body, headers):
        calls.append((method, url, body))
        return {"items": [
            {"sentBy": {"id": "pho_usr_agentecho", "kind": "agent"}, "space": {"kind": "direct"}},
            {"id": "m1", "createdAt": "2026-11-14T16:00:00Z", "content": {"type": "text", "text": "ping"},
             "sentBy": {"id": "pho_usr_me", "kind": "human", "handle": "+14155550123"},
             "receivedBy": {"id": "pho_usr_line", "kind": "agent", "details": {"resourceId": "pho_res_x"}},
             "space": {"id": "pho_spc_dm", "kind": "direct", "participantIds": ["pho_usr_me", "pho_usr_line"]}},
        ]}

    (found,) = p.discover(DISCOVER_PRJ, "pho_sk_" + "A" * 30, transport)
    assert [c[0] for c in calls] == ["GET"] and calls[0][2] is None
    q = parse_qs(urlsplit(calls[0][1]).query)
    assert q["direction"] == ["received"] and q["platform"] == ["imessage"]
    assert found["PHOTON_RANGER_ID"] == "pho_usr_me" and found["PHOTON_AGENT_ID"] == "pho_usr_line"
    assert found["PHOTON_SPACE_ID"] == "pho_spc_dm" and found["PHOTON_RESOURCE_ID"] == "pho_res_x"
    assert found["from"] == "…0123"  # never the full phone number


def test_discover_refuses_bad_settings_before_any_call():
    def transport(*a):
        raise AssertionError("no network")

    with pytest.raises(p.BridgeError):
        p.discover("", "pho_sk_" + "A" * 30, transport)
    with pytest.raises(p.BridgeError):
        p.discover(DISCOVER_PRJ, "pho_ask_" + "A" * 30, transport)


def test_discover_error_explains_without_leaking_keys():
    import io
    from urllib.error import HTTPError

    body = b'{"error": {"code": "forbidden", "message": "key pho_sk_SECRETSECRET lacks project:read"}}'
    exc = HTTPError("https://api.photon.codes", 403, "Forbidden", {}, io.BytesIO(body))
    why = p._explain(exc)
    assert "forbidden" in why and "project:read" in why
    assert "SECRET" not in why


def test_shared_line_needs_no_resource_id(monkeypatch):
    ids = {"PROJECT": "pho_prj_", "SPACE": "pho_spc_", "AGENT": "pho_usr_", "RANGER": "pho_usr_"}
    for i, (k, pre) in enumerate(ids.items()):
        monkeypatch.setenv(f"PHOTON_{k}_ID", pre + str(i) * 26)
    monkeypatch.setenv("PHOTON_RESOURCE_ID", "")
    target = p.Target.from_env()
    assert target.resource == ""
    seen = []
    photon = p.Photon(target, "pho_sk_" + "A" * 30, transport=lambda m, u, b, h: seen.append(u) or {"items": []})
    photon.page("2026-11-14T00:00:00.000Z", "2026-11-14T00:01:00.000Z")
    q = parse_qs(urlsplit(seen[0]).query)
    assert "resourceId" not in q and "spaceId" not in q


def test_imessage_decision_shows_on_dashboard_as_imessage(setup):
    h, bridge, _provider, code, _now = setup
    bridge.receive(incoming(f"APPROVE {code}"))
    inc = h.commander.incidents.all()[0]
    view = h.commander.incident_view(inc.id)
    (action,) = view["ranger_actions"]
    assert action["decision"] == "approved" and action["via"] == "imessage" and action["by"] == "ranger"
    assert view["approvals"][0]["decided_via"] == "imessage"
    assert any("via iMessage" in m["summary"] for m in h.of_type("agent_step"))
