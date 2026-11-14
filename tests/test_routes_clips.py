"""Node clips can be played from the dashboard, with the hash the node signed."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from node.record import clip_hash
from station import routes_clips


def client():
    app = FastAPI()
    app.include_router(routes_clips.router)
    return TestClient(app)


def test_serves_the_stored_clip_with_its_signed_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path))
    (tmp_path / "5").mkdir()
    wav = b"RIFF....WAVEfmt fake clip bytes"
    (tmp_path / "5" / "7.wav").write_bytes(wav)
    res = client().get("/api/nodes/5/clips/7.wav")
    assert res.status_code == 200 and res.content == wav
    assert res.headers["content-type"] == "audio/wav"
    assert res.headers["x-clip-hash"] == clip_hash(wav).hex()


def test_missing_or_bad_clip_ids_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path))
    c = client()
    assert c.get("/api/nodes/5/clips/8.wav").status_code == 404
    assert c.get("/api/nodes/0/clips/1.wav").status_code == 404
    assert c.get("/api/nodes/..%2F..%2Fetc/clips/1.wav").status_code in (404, 422)


def test_lists_recordings_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path))
    (tmp_path / "5").mkdir()
    for seq in (1, 3, 2):
        (tmp_path / "5" / f"{seq}.wav").write_bytes(b"x")
    data = client().get("/api/nodes/5/clips").json()
    assert [c["seq"] for c in data["clips"]] == [3, 2, 1]
    assert data["clips"][0]["url"] == "/api/nodes/5/clips/3.wav"


def test_live_plays_new_sounds_in_order_then_ambient(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import numpy as np
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    heard = {5: [(100.0, np.zeros(16000, dtype=np.float32)), (200.0, np.zeros(8000, dtype=np.float32))]}

    app = FastAPI()
    app.include_router(routes_clips.router)
    app.state.pipeline = SimpleNamespace(heard=heard)
    c = TestClient(app)
    first = c.get("/api/nodes/5/live.wav?after=0")
    assert first.headers["x-live-kind"] == "heard" and first.headers["x-live-at"] == "100.0"
    second = c.get("/api/nodes/5/live.wav?after=100")
    assert second.headers["x-live-at"] == "200.0"
    idle = c.get("/api/nodes/5/live.wav?after=200")
    assert idle.headers["x-live-kind"].startswith("ambient") and idle.content[:4] == b"RIFF"
