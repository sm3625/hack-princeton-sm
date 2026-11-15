"""deploy/watchdog.sh restarts the station after repeated failed health checks."""

import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("curl") is None,
                                reason="needs bash and curl")


def _run(tmp_path: Path, health_url: str, **extra: str) -> str:
    log = tmp_path / "systemctl.log"
    stub = tmp_path / "systemctl"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    stub.chmod(0o755)
    env = {**os.environ, "HEALTH_URL": health_url, "STATE_DIR": str(tmp_path / "state"),
           "SYSTEMCTL": str(stub), "TLS_HOST": "", "FAILS_BEFORE_RESTART": "3", **extra}
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True, timeout=30)
    return log.read_text() if log.exists() else ""


def test_restarts_only_after_three_consecutive_failures(tmp_path):
    dead = "http://127.0.0.1:9/api/health"
    assert _run(tmp_path, dead) == ""
    assert _run(tmp_path, dead) == ""
    assert _run(tmp_path, dead).strip() == "restart forest-guardian-judges"
    # Counter resets after a restart.
    assert _run(tmp_path, dead).strip() == "restart forest-guardian-judges"


def test_healthy_check_resets_failure_count(tmp_path):
    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dead = "http://127.0.0.1:9/api/health"
        alive = f"http://127.0.0.1:{server.server_port}/api/health"
        _run(tmp_path, dead)
        _run(tmp_path, dead)
        _run(tmp_path, alive)
        assert _run(tmp_path, dead) == ""
        assert (tmp_path / "state" / "fails").read_text().strip() == "1"
    finally:
        server.shutdown()
