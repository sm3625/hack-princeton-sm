"""Check evidence honesty and the actual isolated inference path."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from emulators.run import inputs

ROOT = Path(__file__).resolve().parents[1]


def test_local_evidence_runs_real_model(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "emulators.run", "--output", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["execution"].startswith("LOCAL HOST")
    assert report["model_bytes"] == (ROOT / "models/classifier.tflite").stat().st_size
    assert len(report["windows"]) == 36
    assert report["input_kind"] == "Synthetic smoke-test audio"
    assert all(0 <= r["confidence"] <= 1 for r in report["windows"])
    assert all(r["latency_ms"] > 0 for r in report["windows"])
    assert "not a Raspberry Pi hardware benchmark" in (tmp_path / "report.html").read_text()


def test_empty_audio_directory_is_not_synthetic_fallback(tmp_path):
    with pytest.raises(ValueError, match="No WAV"):
        list(inputs(tmp_path))


def test_guest_refuses_non_arm_linux(monkeypatch, tmp_path):
    from argparse import Namespace

    from emulators import run

    monkeypatch.setattr(run.platform, "system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="requires Linux aarch64"):
        run.benchmark(Namespace(guest="pi-zero2", output=tmp_path))


def test_container_limits_and_allowlisted_context(monkeypatch, tmp_path):
    from argparse import Namespace

    from emulators import run

    calls = []
    monkeypatch.setattr(run.shutil, "which", lambda _: "/test/docker")

    def execute(command, **kwargs):
        calls.append(command)
        if command[1] == "build":
            context = Path(command[-1])
            assert (context / "node/classifier.py").read_bytes() == (
                ROOT / "node/classifier.py"
            ).read_bytes()
            assert not (context / ".env").exists()
            assert not (context / "keys").exists()
            assert not (context / "data").exists()

    monkeypatch.setattr(run.subprocess, "run", execute)
    run.container(Namespace(audio=None, output=tmp_path, profile="pi-zero2", live=True))
    command = calls[-1]
    assert "--platform=linux/arm64" in command
    assert "--memory=384m" in command and "--memory-swap=384m" in command
    assert "--cpus=1" in command and "--network=none" in command
    assert "--guest" in command and "--live" in command
