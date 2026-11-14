import socket
import time

import pytest
from fastapi.testclient import TestClient

import station.receiver as receiver_module
from node.keys import load_signing_key
from node.record import pack
from station.receiver import Receiver, get_receiver
from station.store import SqliteEventStore
from tests.pipeline_fakes import WORLD, detection, keys_for
from transport import UdpLink

KEYS = keys_for(*(n.id for n in WORLD.nodes))
PUBLIC = {i: k.verify_key for i, k in KEYS.items()}


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("EVENTS_DB", str(tmp_path / "events.sqlite"))
    monkeypatch.setenv("KEYS_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("LORA_UDP_PORT", "0")
    monkeypatch.setattr(receiver_module, "_default", None)
    from station.app import create_app

    return create_app()


@pytest.fixture
def client(app_env):
    messages: list[dict] = []
    rx = Receiver(SqliteEventStore(), PUBLIC, WORLD, publish=messages.append)
    app_env.dependency_overrides[get_receiver] = lambda: rx
    c = TestClient(app_env)  # no `with`: lifespan (UDP) not started
    c.rx, c.messages = rx, messages
    return c


def test_detections_lists_recent_first_with_names(client):
    for seq in (1, 2, 3):
        client.rx.handle(pack(detection(seq=seq), KEYS[5]))
    forged = bytearray(pack(detection(node_id=4, seq=1), KEYS[4]))
    forged[25] ^= 1
    client.rx.handle(bytes(forged))

    body = client.get("/api/detections", params={"limit": 3}).json()
    rows = body["detections"]
    assert [(r["node"], r["seq"], r["verified"]) for r in rows] == [
        ("Fern Hollow", 1, False),
        ("Creek Bend", 3, True),
        ("Creek Bend", 2, True),
    ]
    assert rows[1]["sound"] == "chainsaw" and "received_at" in rows[1]

    only = client.get("/api/detections", params={"verified_only": True}).json()["detections"]
    assert all(r["verified"] for r in only) and len(only) == 3
    assert client.get("/api/detections", params={"limit": 0}).status_code == 422


def test_tamper_endpoint_flips_a_byte_and_reports_failure(client):
    client.rx.handle(pack(detection(seq=8), KEYS[5]))
    res = client.post("/api/dev/tamper/5/8")
    assert res.status_code == 200
    assert res.json() == {
        "node": "Creek Bend",
        "seq": 8,
        "byte": 20,
        "verified_before": True,
        "verified": False,
    }
    assert client.rx.store.query() == []
    assert client.messages[-1] == {
        "type": "rejected",
        "node": "Creek Bend",
        "reason": "bad_signature",
        "seq": 8,
    }
    rows = client.get("/api/detections").json()["detections"]
    assert rows[0]["verified"] is False


def test_tamper_endpoint_validates_input(client):
    assert client.post("/api/dev/tamper/5/1").status_code == 404
    client.rx.handle(pack(detection(seq=1), KEYS[5]))
    assert client.post("/api/dev/tamper/5/1", params={"byte": 108}).status_code == 422
    assert client.post("/api/dev/tamper/5/1", params={"byte": 107}).json()["verified"] is False


def _wait_for(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_app_lifespan_starts_udp_listener_end_to_end(app_env, tmp_path):
    """UDP packet from a node -> station listener -> verify -> store -> API and WebSocket."""
    with TestClient(app_env) as client:
        port = client.app.state.lora_udp.get_extra_info("sockname")[1]
        key = load_signing_key(5, tmp_path / "keys")  # provisioned by the station on startup
        link = UdpLink("127.0.0.1", port, loss=0.0, latency_ms=(0, 0))
        with client.websocket_connect("/ws") as ws:
            link.send(pack(detection(seq=1), key))
            tampered = bytearray(pack(detection(seq=2), key))
            tampered[22] ^= 0x10
            link.send(bytes(tampered))
            rows = _wait_for(
                lambda: len(d := client.get("/api/detections").json()["detections"]) == 2 and d
            )
            assert [(r["seq"], r["verified"]) for r in rows] == [(2, False), (1, True)]
            first, second = ws.receive_json(), ws.receive_json()
        link.close()
    assert first["type"] == "detection" and first["node"] == "Creek Bend"
    assert second == {"type": "rejected", "node": "Creek Bend", "reason": "bad_signature", "seq": 2}


def test_busy_udp_port_does_not_stop_the_app(app_env, monkeypatch):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("127.0.0.1", 0))
    monkeypatch.setenv("LORA_UDP_PORT", str(blocker.getsockname()[1]))
    try:
        with TestClient(app_env) as client:
            assert client.app.state.lora_udp is None
            assert client.get("/api/health").status_code == 200
    finally:
        blocker.close()


def test_udp_can_be_turned_off(app_env, monkeypatch):
    monkeypatch.setenv("LORA_UDP_PORT", "off")
    with TestClient(app_env) as client:
        assert client.app.state.lora_udp is None
