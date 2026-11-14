"""Wiring: simulator -> node runtimes -> LoRa link -> receiver -> store/bus/commander.

One NodeRuntime per world node. The sim hands each scenario clip to feed(node_id, audio); the
audio is classified on a worker thread (CPU work), the signed record goes through an
InProcessLink that delivers on the event loop to receiver.handle. Records are stamped with
simulated time. Started by the router lifespan below (needs the running loop).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import APIRouter, FastAPI

from common.contracts import ROOT, Classifier, World
from node.keys import load_signing_key
from node.runtime import NodeRuntime
from sim.engine import SimEngine, get_engine
from station.bus import bus
from station.receiver import Receiver, get_receiver
from transport.base import DEFAULT_LOSS
from transport.inprocess import InProcessLink

log = logging.getLogger("station.integration")

MODEL_FILE = ROOT / "models" / "classifier.tflite"


def pick_classifier(model_file: Path = MODEL_FILE) -> Classifier:
    """The real TFLite model when models/classifier.tflite exists, else the labelled fallback."""
    backend = os.getenv("EDGE_BACKEND", "off").strip().lower()
    if backend != "off":
        from emulators.bridge import EdgeClassifier

        return EdgeClassifier(backend, os.getenv("EDGE_PROFILE", "pi-zero2"))
    if model_file.exists():
        try:
            from node.classifier import TFLiteClassifier  # built by the classifier track

            return TFLiteClassifier(model_file)
        except ImportError:
            log.warning("%s exists but node.classifier is not importable", model_file)
    from node.fallback_classifier import LABEL, ScenarioHintClassifier

    log.warning("classifier: %s. Train models/classifier.tflite for the real one.", LABEL)
    return ScenarioHintClassifier()


class Pipeline:
    def __init__(
        self,
        world: World,
        receiver: Receiver,
        engine: SimEngine,
        classifier: Classifier,
        loop: asyncio.AbstractEventLoop,
        clip_dir: Path,
        loss: float = DEFAULT_LOSS,
    ) -> None:
        self.engine = engine
        self.classifier = classifier
        self._loop = loop
        self._now = 0.0  # sim time of the clip being processed (read by the runtimes' clocks)
        # One worker: classification is CPU work, runtimes and the TFLite interpreter are not
        # shared across threads, and clips of one node stay in order.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="node-runtime")
        self.runtimes: dict[int, NodeRuntime] = {}
        # What each node heard most recently (sim time, audio), for "listen live" in the UI.
        self.heard: dict[int, deque[tuple[float, np.ndarray]]] = {n.id: deque(maxlen=4) for n in world.nodes}
        for node in world.nodes:
            link = InProcessLink(receiver.handle, loss=loss)  # delivers on the running loop
            self.runtimes[node.id] = NodeRuntime(
                node,
                classifier,
                load_signing_key(node.id),
                clip_dir,
                link.send,
                lambda: self._now,
            )

    def feed(self, node_id: int, audio: np.ndarray) -> asyncio.Future | None:
        """Sim callback (event-loop thread): hand the clip to the worker, return at once."""
        runtime = self.runtimes.get(node_id)
        if runtime is None:
            log.warning("feed for unknown node %s ignored", node_id)
            return
        stamp = float(self.engine.now())  # sim time the clip arrives, even if work queues up
        self.heard.setdefault(node_id, deque(maxlen=4)).append((stamp, audio))
        return self._loop.run_in_executor(self._pool, self._process, runtime, audio, stamp)

    def _process(self, runtime: NodeRuntime, audio: np.ndarray, stamp: float) -> dict:
        try:
            self._now = stamp
            runtime.reset()  # each scenario clip is a separate recording
            detections = runtime.feed(audio)
            return {"emitted": len(detections), "node_id": runtime.node.id}
        except Exception as exc:
            log.exception("node %s failed to process a clip", runtime.node.name)
            return {"error": str(exc)}

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        close = getattr(self.classifier, "close", None)
        if close is not None:
            close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    bus.bind_loop(loop)
    pipeline = None
    if os.getenv("NODE_PIPELINE", "on").strip().lower() == "off":
        log.warning("NODE_PIPELINE=off: scenarios use stub detections (no node runtimes)")
    else:
        engine = get_engine()
        pipeline = Pipeline(
            engine.world,
            get_receiver(),
            engine,
            pick_classifier(),
            loop,
            Path(os.getenv("NODE_DATA_DIR") or ROOT / "node_data"),
            float(os.getenv("LORA_LOSS", DEFAULT_LOSS)),
        )
        app.state.pipeline = pipeline
        engine.set_feed(pipeline.feed)
    try:
        yield
    finally:
        if pipeline is not None:
            get_engine().set_feed(None)
            pipeline.close()
        bus.bind_loop(None)


router = APIRouter(lifespan=lifespan)


def setup(app: FastAPI) -> None:
    """Called from create_app: share the real sim, then mount the pipeline's lifespan."""
    app.state.sim = get_engine()
    app.include_router(router)
