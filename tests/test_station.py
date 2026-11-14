import importlib

import pytest
from fastapi.testclient import TestClient


def make_client(monkeypatch, **env):
    for k in ("LLM_MODE", "OLLAMA_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import station.app

    importlib.reload(station.app)
    return TestClient(station.app.create_app())


def test_index_serves_ui(monkeypatch):
    client = make_client(monkeypatch, LLM_MODE="mock")
    res = client.get("/")
    assert res.status_code == 200
    assert "Forest Guardian" in res.text


def test_health_reports_model(monkeypatch):
    client = make_client(monkeypatch, LLM_MODE="ollama", OLLAMA_MODEL="qwen3:8b")
    assert client.get("/api/health").json() == {
        "ok": True,
        "model": "Local · qwen3:8b",
        "mode": "ollama",
    }


def test_haiku_without_key_fails_clearly(monkeypatch):
    from station.config import ConfigError

    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make_client(monkeypatch, LLM_MODE="haiku")


def test_unknown_mode_rejected(monkeypatch):
    from station.config import ConfigError

    with pytest.raises(ConfigError, match="LLM_MODE"):
        make_client(monkeypatch, LLM_MODE="gpt")


def test_ws_forwards_bus_messages(monkeypatch):
    from station.bus import bus

    client = make_client(monkeypatch, LLM_MODE="mock")
    with client.websocket_connect("/ws") as ws:
        bus.publish({"type": "clock", "sim_time": 123})
        assert ws.receive_json() == {"type": "clock", "sim_time": 123}



def test_bus_publish_from_another_thread_reaches_subscribers():
    import asyncio
    import threading

    from station.bus import Bus

    async def scenario():
        b = Bus()
        b.bind_loop(asyncio.get_running_loop())
        q = b.subscribe()
        t = threading.Thread(target=b.publish, args=({"type": "clock", "sim_time": 1},))
        t.start()
        t.join()
        # queued via call_soon_threadsafe: not yet visible until the loop runs
        return await asyncio.wait_for(q.get(), 2)

    assert asyncio.run(scenario()) == {"type": "clock", "sim_time": 1}


def test_bus_publish_without_loop_or_on_loop_is_direct():
    import asyncio

    from station.bus import Bus

    b = Bus()
    q = b.subscribe()
    b.publish({"n": 1})  # no loop bound
    assert q.get_nowait() == {"n": 1}

    async def on_loop():
        b.bind_loop(asyncio.get_running_loop())
        b.publish({"n": 2})
        return q.get_nowait()  # delivered synchronously on the loop thread

    assert asyncio.run(on_loop()) == {"n": 2}


def test_ws_rejects_foreign_origins_and_accepts_own_or_none(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from station.bus import bus

    client = make_client(monkeypatch, LLM_MODE="mock")
    for origin in ("https://evil.example", "http://localhost:9999", "http://127.0.0.1:81", "null"):
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws", headers={"origin": origin}),
        ):
            pass
    # TestClient serves on port 80
    for headers in ({}, {"origin": "http://localhost"}, {"origin": "http://127.0.0.1:80"}):
        with client.websocket_connect("/ws", headers=headers) as ws:
            bus.publish({"type": "clock", "sim_time": 5})
            assert ws.receive_json()["type"] == "clock"
