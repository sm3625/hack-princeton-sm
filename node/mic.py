"""Run one live node: uv run python -m node.mic --node "Arroyo Recodo"

Listens on the default microphone (16 kHz mono via sounddevice), classifies with the real model
(node/classifier.py), and sends signed records over UDP to the station started by `make dev`.
--wav FILE feeds a recording instead of the mic (no mic permission needed; demo backup).
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import time
from pathlib import Path

import numpy as np

from common.contracts import (
    HOP_S,
    ROOT,
    SAMPLE_RATE,
    Classifier,
    Node,
    Prediction,
    World,
    load_world,
)
from node.keys import ensure_keys, load_signing_key
from node.runtime import NodeRuntime
from transport.udp import DEFAULT_HOST, DEFAULT_PORT, UdpLink

log = logging.getLogger("node.mic")


def find_node(world: World, name: str) -> Node:
    for node in world.nodes:
        if node.name.lower() == name.strip().lower():
            return node
    names = ", ".join(n.name for n in world.nodes)
    raise SystemExit(f'No node named "{name}". Nodes: {names}')


def load_classifier(model: Path | None) -> Classifier:
    from node.classifier import TFLiteClassifier  # lazy: built by the classifier track

    return TFLiteClassifier(model) if model else TFLiteClassifier()


def read_audio(path: Path) -> np.ndarray:
    import librosa

    audio, _ = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    return audio.astype(np.float32)


def run_mic(runtime: NodeRuntime, device: str | int | None) -> None:
    import sounddevice as sd

    blocks: queue.Queue[np.ndarray] = queue.Queue()

    def on_audio(indata, frames, time_info, status) -> None:
        if status:
            log.warning("mic: %s", status)
        blocks.put(indata[:, 0].copy())

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=int(SAMPLE_RATE * HOP_S),
        device=device,
        callback=on_audio,
    ):
        print(f"{runtime.node.name}: listening. Ctrl+C to stop.")
        while True:
            runtime.feed(blocks.get())  # the runtime logs each event


def run_wav(runtime: NodeRuntime, audio: np.ndarray, realtime: bool) -> None:
    if not realtime:
        runtime.feed(audio)
        return
    block = int(SAMPLE_RATE * HOP_S)
    for i in range(0, len(audio), block):
        runtime.feed(audio[i : i + block])
        time.sleep(HOP_S)


def _print_window(t: float, p: Prediction) -> None:
    print(
        f"  {time.strftime('%H:%M:%S', time.localtime(t))} {p.sound.name.lower():<10} "
        f"{p.confidence:.2f}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one live acoustic node.")
    parser.add_argument("--node", required=True, help="node name from scenarios/world.yaml")
    parser.add_argument("--wav", type=Path, help="feed this audio file instead of the mic")
    parser.add_argument("--realtime", action="store_true", help="with --wav: play at 1x speed")
    parser.add_argument("--model", type=Path, help="classifier .tflite (default: classifier's)")
    parser.add_argument("--device", help="input device name or index (see sounddevice)")
    parser.add_argument("--host", default=os.getenv("LORA_UDP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("LORA_UDP_PORT", DEFAULT_PORT)))
    parser.add_argument("--loss", type=float, default=0.05, help="simulated packet loss 0..1")
    parser.add_argument("--min-latency-ms", type=float, default=100.0)
    parser.add_argument("--max-latency-ms", type=float, default=2000.0)
    parser.add_argument("--seed", type=int, help="seed the loss/latency RNG")
    parser.add_argument("--clip-dir", type=Path, default=ROOT / "node_data")
    parser.add_argument("--keys-dir", type=Path)
    parser.add_argument("-v", "--verbose", action="store_true", help="print every window")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    world = load_world()
    node = find_node(world, args.node)
    ensure_keys(world, args.keys_dir)
    device = int(args.device) if args.device and args.device.isdigit() else args.device

    link = UdpLink(
        args.host,
        args.port,
        loss=args.loss,
        latency_ms=(args.min_latency_ms, args.max_latency_ms),
        seed=args.seed,
    )
    runtime = NodeRuntime(
        node,
        load_classifier(args.model),
        load_signing_key(node.id, args.keys_dir),
        args.clip_dir,
        link.send,
        time.time,
        on_window=_print_window if args.verbose else None,
    )
    try:
        if args.wav:
            run_wav(runtime, read_audio(args.wav), args.realtime)
        else:
            run_mic(runtime, device)
    except KeyboardInterrupt:
        pass
    finally:
        link.close(wait=True)
        s = link.stats
        print(
            f"{node.name}: {runtime.counters['events']} events; radio sent {s.sent}, "
            f"lost {s.dropped}, too large {s.rejected}"
        )


if __name__ == "__main__":
    main()
