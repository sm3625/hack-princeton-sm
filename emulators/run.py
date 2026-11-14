"""Real inference, recorded evidence, and a portable ARM64 deployment demo."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import importlib.metadata
import json
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {"pi-zero2": (1, 384), "pi4": (4, 1024)}
NOTICE = (
    "Compatibility experiment, not a Raspberry Pi hardware benchmark. "
    "CPU quotas do not reproduce Pi clock speed, peripherals, thermals or power draw."
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(destination):
    """Select first held-out recordings by filename, never by model prediction."""
    from sim.audio import load_clip

    destination.mkdir(parents=True, exist_ok=True)
    esc = ROOT / "data/ESC-50-master"
    rows = list(csv.DictReader((esc / "meta/esc50.csv").open()))
    sources = []
    for category, label in [
        ("chirping_birds", "background"),
        ("chainsaw", "chainsaw"),
        ("engine", "vehicle"),
    ]:
        row = min(
            (r for r in rows if r["fold"] == "5" and r["category"] == category),
            key=lambda r: r["filename"],
        )
        sources.append((label, esc / "audio" / row["filename"]))
    urban = ROOT / "data/UrbanSound8K"
    rows = list(csv.DictReader((urban / "metadata/UrbanSound8K.csv").open()))
    row = min(
        (r for r in rows if r["fold"] == "10" and r["class"] == "gun_shot"),
        key=lambda r: r["slice_file_name"],
    )
    sources.append(("gunshot", urban / "audio/fold10" / row["slice_file_name"]))
    manifest = []
    for label, source in sources:
        audio = load_clip(source, 5)
        target = destination / f"{label}.wav"
        with wave.open(str(target), "wb") as w:
            w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            w.writeframes((audio * 32767).astype("<i2").tobytes())
        manifest.append(
            {
                "file": target.name,
                "source": str(source.relative_to(ROOT)),
                "source_sha256": sha(source),
                "prepared_sha256": sha(target),
            }
        )
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Prepared {len(sources)} held-out recordings in {destination}")


def inputs(directory):
    import numpy as np

    if directory:
        paths = sorted(directory.glob("*.wav"))
        if not paths:
            raise ValueError(f"No WAV files in {directory}; run --prepare first")
        for path in paths:
            with wave.open(str(path), "rb") as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
                    raise ValueError(f"{path}: expected mono 16-bit PCM at 16000 Hz")
                audio = (
                    np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
                    / 32768
                )
            if len(audio) < 16000:
                raise ValueError(f"{path}: needs at least one second")
            yield path.stem, audio, sha(path)
    else:
        from sim.audio import synthesize

        for label in ["background", "chainsaw", "gunshot", "vehicle"]:
            audio = synthesize(label, 5, seed=42)
            yield label, audio, hashlib.sha256(audio.tobytes()).hexdigest()


def limits():
    result = {}
    for name in ["memory.max", "cpu.max", "memory.swap.max"]:
        path = Path("/sys/fs/cgroup") / name
        result[name] = path.read_text().strip() if path.exists() else None
    return result


def report_html(report):
    esc = html.escape
    rows = "".join(
        f"<tr><td>{esc(r['input'])}</td><td>{r['offset_s']:.1f}s</td>"
        f"<td>{esc(r['prediction'])}</td><td>{r['confidence']:.0%}</td>"
        f"<td>{r['latency_ms']:.2f} ms</td></tr>"
        for r in report["windows"]
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>Forest Guardian · Edge evidence</title><style>
body{{background:#0b1716;color:#e9f3ef;font:18px system-ui;margin:50px auto;max-width:1000px}}
h1{{font-size:48px}}small,p{{color:#acc4b9}}.cards{{display:flex;gap:20px}}
article{{background:#17302b;padding:24px;border-radius:16px;flex:1}}b{{font-size:30px}}
table{{width:100%;border-collapse:collapse;margin-top:30px}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #284139}}
code{{overflow-wrap:anywhere}}summary{{cursor:pointer;margin:24px 0}}pre{{white-space:pre-wrap;font-size:13px}}
</style><small>FOREST GUARDIAN / EDGE DEPLOYMENT LAB</small><h1>Small model. Real inference.</h1>
<p>{esc(report["execution"])} · {esc(report["platform"])}</p>
<div class="cards"><article><small>MODEL SIZE</small><br><b>{report["model_bytes"] / 1024:.1f} KiB</b></article>
<article><small>OBSERVED P95</small><br><b>{report["p95_ms"]:.2f} ms</b></article>
<article><small>PROCESS PEAK RSS</small><br><b>{report["peak_rss_mib"]:.1f} MiB</b></article></div>
<p>{esc(NOTICE)}</p><p>{esc(report["input_kind"])}. Predictions are actual outputs, not an accuracy evaluation.</p>
<p>500 ms scheduling budget · {report["deadline_misses"]} measured windows over budget · one inference thread</p>
<details><summary>Inspect reproducible evidence</summary><pre>{esc(json.dumps(report, indent=2))}</pre></details>
<table><thead><tr><th>Input</th><th>Offset</th><th>Prediction</th><th>Confidence</th><th>Latency</th></tr></thead><tbody>{rows}</tbody></table></html>"""


def benchmark(args):
    import numpy as np

    from node.classifier import TFLiteClassifier

    if args.guest and (platform.system() != "Linux" or platform.machine() != "aarch64"):
        raise RuntimeError("ARM64 proof requires Linux aarch64; refusing to label this run ARM64")
    clf = TFLiteClassifier(num_threads=1)
    start = time.perf_counter()
    clf.load()
    load_ms = (time.perf_counter() - start) * 1000
    samples = list(inputs(args.audio))
    # Warm the actual inference path with non-silent audio, separately from measured windows.
    clf.classify(np.random.default_rng(0).normal(0, 0.1, 16000).astype(np.float32))
    execution = (
        f"ARM64 Linux container / {args.guest} resource envelope"
        if args.guest
        else "LOCAL HOST rehearsal / no Pi limits enforced"
    )
    print(f"\nFOREST GUARDIAN · EDGE DEPLOYMENT LAB\n{execution}\n{NOTICE}", flush=True)
    print(f"Model: {clf.model_bytes / 1024:.1f} KiB · SHA256 {sha(clf.model_path)[:16]}…")
    print("Input            Time   Real prediction   Confidence   Inference / 500ms", flush=True)
    windows = []
    for label, audio, digest in samples:
        for offset in range(0, len(audio) - 16000 + 1, 8000):
            start = time.perf_counter()
            pred = clf.classify(audio[offset : offset + 16000])
            latency = (time.perf_counter() - start) * 1000
            row = {
                "input": label,
                "input_sha256": digest,
                "offset_s": offset / 16000,
                "prediction": pred.sound.name.lower(),
                "confidence": pred.confidence,
                "latency_ms": latency,
            }
            windows.append(row)
            print(
                f"{label:16} {offset / 16000:4.1f}s  {row['prediction']:16} "
                f"{pred.confidence:6.1%}       {latency:7.2f} ms",
                flush=True,
            )
            if args.live:
                time.sleep(max(0, 0.5 - latency / 1000))
    times = [r["latency_ms"] for r in windows]
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "execution": execution,
        "platform": f"{platform.system()} {platform.machine()}",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notice": NOTICE,
        "cgroup_limits": limits(),
        "python": platform.python_version(),
        "packages": {p: importlib.metadata.version(p) for p in ["numpy", "ai-edge-litert"]},
        "input_kind": "WAV recordings" if args.audio else "Synthetic smoke-test audio",
        "model_sha256": sha(clf.model_path),
        "model_bytes": clf.model_bytes,
        "classifier_sha256": sha(ROOT / "node/classifier.py"),
        "model_version": clf.model_version,
        "load_ms": load_ms,
        "p50_ms": float(np.median(times)),
        "p95_ms": float(np.percentile(times, 95)),
        "max_ms": max(times),
        "deadline_misses": sum(t > 500 for t in times),
        "peak_rss_mib": rss / (1024**2 if sys.platform == "darwin" else 1024),
        "windows": windows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    (args.output / "report.html").write_text(report_html(report))
    print(
        f"\nP95 {report['p95_ms']:.2f} ms · peak RSS {report['peak_rss_mib']:.1f} MiB"
        f" · {report['deadline_misses']} deadline misses\nEvidence: {args.output.resolve()}"
    )


def build_image():
    if not shutil.which("docker"):
        raise RuntimeError(
            "Docker is not installed. Install/start Docker Desktop, then rerun. "
            "Use --backend local for an explicitly labeled rehearsal now."
        )
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
    # Allowlist context: never send .env, keys, databases, or the dataset to Docker.
    with tempfile.TemporaryDirectory() as temp:
        context = Path(temp)
        files = [
            "node/__init__.py",
            "node/classifier.py",
            "common/__init__.py",
            "common/contracts.py",
            "sim/__init__.py",
            "sim/audio.py",
            "models/classifier.tflite",
            "models/classifier.json",
            "emulators/__init__.py",
            "emulators/run.py",
            "emulators/worker.py",
        ]
        for name in files:
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        shutil.copy2(ROOT / "emulators/Dockerfile", context / "Dockerfile")
        subprocess.run(
            [
                "docker",
                "build",
                "--platform=linux/arm64",
                "-t",
                "forest-guardian-edge:arm64",
                str(context),
            ],
            check=True,
        )


def container(args):
    if args.audio and (not args.audio.is_dir() or not any(args.audio.glob("*.wav"))):
        raise ValueError("Audio directory is missing or empty; run --prepare first")
    build_image()
    cpus, memory = PROFILES[args.profile]
    args.output.mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "run",
        "--rm",
        "--platform=linux/arm64",
        "--network=none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=32m",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--cpus={cpus}",
        f"--memory={memory}m",
        f"--memory-swap={memory}m",
        "--pids-limit=64",
        "-v",
        f"{args.output.resolve()}:/evidence",
    ]
    if args.audio:
        command += ["-v", f"{args.audio.resolve()}:/audio:ro"]
    command += ["forest-guardian-edge:arm64", "--guest", args.profile, "--output", "/evidence"]
    if args.audio:
        command += ["--audio", "/audio"]
    if args.live:
        command += ["--live"]
    subprocess.run(command, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", choices=["local", "arm64"], default="local")
    p.add_argument("--profile", choices=PROFILES, default="pi-zero2")
    p.add_argument("--audio", type=Path, help="Directory of mono 16kHz PCM16 WAV files")
    p.add_argument("--prepare", type=Path, help="Prepare four held-out recordings, then exit")
    p.add_argument("--output", type=Path)
    p.add_argument("--live", action="store_true", help="Pace output at the real 500ms audio hop")
    p.add_argument("--guest", choices=PROFILES, help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.output is None:
        name = args.profile if args.backend == "arm64" else "local"
        args.output = ROOT / "emulators/evidence" / name
    try:
        if args.prepare:
            prepare(args.prepare)
        elif args.backend == "arm64":
            container(args)
        else:
            benchmark(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        p.exit(1, f"Edge demo stopped: {exc}\n")


if __name__ == "__main__":
    main()
