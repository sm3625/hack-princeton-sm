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
