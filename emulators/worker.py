"""Persistent inference worker. Only JSON goes to stdout; diagnostics go to stderr."""

import argparse
import base64
import json
import platform
import sys
import time

import numpy as np

from emulators.run import limits, sha
from node.classifier import TFLiteClassifier


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm64", action="store_true")
    args = parser.parse_args()
    if args.arm64 and (platform.system(), platform.machine()) != ("Linux", "aarch64"):
        raise RuntimeError("ARM64 worker requires Linux aarch64")
    clf = TFLiteClassifier(num_threads=1)
    clf.load()
    print(
        json.dumps(
            {
                "ready": True,
                "platform": f"{platform.system()} {platform.machine()}",
                "model_bytes": clf.model_bytes,
                "model_version": clf.model_version,
                "model_sha256": sha(clf.model_path),
                "limits": limits(),
                "thresholds": {str(int(k)): v for k, v in clf.thresholds.items()},
            }
        ),
        flush=True,
    )
    for line in sys.stdin:
        try:
            request = json.loads(line)
            window = np.frombuffer(base64.b64decode(request["audio"], validate=True), dtype="<f4")
            start = time.perf_counter()
            prediction = clf.classify(window)
            result = {
                "sound": int(prediction.sound),
                "confidence": prediction.confidence,
                "latency_ms": (time.perf_counter() - start) * 1000,
            }
        except Exception as exc:  # noqa: BLE001 - report inference failures to the parent
            result = {"error": str(exc)}
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
