"""Classifier protocol adapter to a persistent local or ARM64 Linux worker."""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

import numpy as np

from common.contracts import Prediction, SoundClass
from emulators.run import PROFILES, ROOT


class EdgeClassifier:
    def __init__(self, backend="local", profile="pi-zero2", timeout=30):
        if backend not in ("local", "arm64") or profile not in PROFILES:
            raise ValueError("Unknown edge backend or profile")
        self.backend, self.profile, self.timeout = backend, profile, timeout
        self._lock = threading.Lock()
        self._responses = queue.Queue()
        self._recent = deque(maxlen=12)
        self._count = 0
        self._error = None
        self._container = None
        command = [sys.executable, "-u", "-m", "emulators.worker"]
        if backend == "arm64":
            cpus, memory = PROFILES[profile]
            self._container = f"forest-edge-{uuid.uuid4().hex[:12]}"
            command = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--name",
                self._container,
                "--platform=linux/arm64",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=64",
                "--tmpfs",
                "/tmp:rw,size=32m",
                f"--cpus={cpus}",
                f"--memory={memory}m",
                f"--memory-swap={memory}m",
                "--entrypoint",
                "python",
                "forest-guardian-edge:arm64",
                "-u",
                "-m",
                "emulators.worker",
                "--arm64",
            ]
        self._process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self.info = self._receive()
            if not self.info.get("ready"):
                raise RuntimeError("Edge worker did not complete startup")
            self.model_version = self.info["model_version"]
            self.model_bytes = self.info["model_bytes"]
            self.thresholds = {SoundClass(int(k)): v for k, v in self.info["thresholds"].items()}
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self._process.stdout:
            self._responses.put(line)
        self._responses.put(None)

    def _receive(self):
        try:
            line = self._responses.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise RuntimeError("Edge worker timed out; restart the demo") from exc
        if line is None:
            raise RuntimeError("Edge worker exited; see station logs")
        result = json.loads(line)
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def classify(self, window):
        with self._lock:
            if self._error:
                raise RuntimeError(self._error)
            try:
                start = time.perf_counter()
                encoded = base64.b64encode(np.asarray(window, dtype="<f4").tobytes()).decode()
                self._process.stdin.write(json.dumps({"audio": encoded}) + "\n")
                self._process.stdin.flush()
                result = self._receive()
                pred = Prediction(SoundClass(result["sound"]), float(result["confidence"]))
                self._recent.append(
                    {
                        "sound": pred.sound.name.lower(),
                        "confidence": pred.confidence,
                        "latency_ms": result["latency_ms"],
                        "roundtrip_ms": (time.perf_counter() - start) * 1000,
                    }
                )
                self._count += 1
                return pred
            except Exception as exc:
                self._error = str(exc)
                self.close()
                raise

    def status(self):
        # Snapshot on the inference lock so worker updates cannot race deque iteration.
        with self._lock:
            return {
                **self.info,
                "backend": self.backend,
                "profile": self.profile,
                "connected": self._process.poll() is None and not self._error,
                "windows": self._count,
                "recent": list(self._recent),
                "error": self._error,
                "label": (
                    "ARM64 Linux resource envelope"
                    if self.backend == "arm64"
                    else "Local edge worker · no Pi limits enforced"
                ),
            }

    def close(self):
        if self._container:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", self._container],
                    timeout=5,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        self._reader.join(timeout=1)
        self._process.stdin.close()
        self._process.stdout.close()
