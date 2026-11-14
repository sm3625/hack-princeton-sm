import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emulators.bridge import EdgeClassifier
from node.classifier import TFLiteClassifier
from station.routes_edge import router


def test_worker_matches_production_and_closes():
    edge = EdgeClassifier()
    production = TFLiteClassifier()
    try:
        for seed in range(3):
            audio = np.random.default_rng(seed).normal(0, 0.1, 16000).astype(np.float32)
            assert edge.classify(audio) == production.classify(audio)
        state = edge.status()
        assert state["connected"] and state["windows"] == 3
        assert state["backend"] == "local"
        assert state["recent"][-1]["roundtrip_ms"] >= state["recent"][-1]["latency_ms"]
        assert edge.thresholds == production.thresholds
    finally:
        edge.close()
    assert edge._process.poll() is not None


def test_dead_worker_never_falls_back():
    edge = EdgeClassifier(timeout=2)
    edge._process.kill()
    edge._process.wait()
    with pytest.raises((RuntimeError, BrokenPipeError)):
        edge.classify(np.zeros(16000, dtype=np.float32))
    assert not edge.status()["connected"]


def test_disconnected_routes():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        assert not c.get("/api/edge/status").json()["connected"]
        assert c.post("/api/edge/play", json={"sound": "gunshot"}).status_code == 409
        assert c.post("/api/edge/play", json={"sound": "../secret"}).status_code == 422


@pytest.mark.asyncio
async def test_real_edge_predictions_enter_signed_pipeline(tmp_path, monkeypatch):
    from nacl.signing import SigningKey

    from common.contracts import load_world
    from station import integration
    from station.receiver import Receiver
    from station.store import SqliteEventStore

    node = load_world().node(5)
    key = SigningKey.generate()
    monkeypatch.setattr(integration, "load_signing_key", lambda _: key)
    receiver = Receiver(SqliteEventStore(tmp_path / "events.sqlite"), {node.id: key.verify_key})
    edge = EdgeClassifier()
    pipeline = integration.Pipeline(
        SimpleNamespace(nodes=[node]),
        receiver,
        SimpleNamespace(now=lambda: 1763000000),
        edge,
        asyncio.get_running_loop(),
        tmp_path / "clips",
        loss=0,
    )
    try:
        # Exact waveform and real model; compare downstream counters to returned predictions.
        audio = np.random.default_rng(4).normal(0, 0.1, 80000).astype(np.float32)
        result = await pipeline.feed(node.id, audio)
        await asyncio.sleep(0.05)
        assert "error" not in result
        assert edge.status()["windows"] == 9
        assert pipeline.runtimes[node.id].stats()["windows"] == 9
        assert result["emitted"] == len(receiver.store.query())
        assert all(d.verified for d in receiver.store.query())
    finally:
        pipeline.close()
