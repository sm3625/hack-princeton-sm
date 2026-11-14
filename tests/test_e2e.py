"""End to end against the real station process (opt-in: `make test-e2e`, marker e2e).

Starts uvicorn as a subprocess with LLM_MODE=mock and temp state, plays night1 from 02:10 at
60x over HTTP, and follows the WebSocket. No network beyond localhost, no paid API.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from common.contracts import ROOT

pytestmark = pytest.mark.e2e

websockets_sync = pytest.importorskip("websockets.sync.client")
from websockets.exceptions import ConnectionClosed


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Station:
    def __init__(self, tmp_path):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = {
            **os.environ,
            "LLM_MODE": "mock",
            "EVENTS_DB": str(tmp_path / "events.sqlite"),
            "KEYS_DIR": str(tmp_path / "keys"),
            "AGENT_FS_DIR": str(tmp_path / "agent_fs"),
            "AGENT_DB": str(tmp_path / "agent.sqlite"),
            "NODE_DATA_DIR": str(tmp_path / "node_data"),
            "LORA_UDP_PORT": "off",
            "LORA_LOSS": "0",
            "STATION_DEV": "1",
            "NODE_PIPELINE": "on",
        }
        self.log = (tmp_path / "station.log").open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "station.app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        self.messages: list[dict] = []

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def wait_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"station exited: {self.logs()}")
            try:
                if self.request("GET", "/api/health")[0] == 200:
                    return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError(f"station did not start: {self.logs()}")

    def follow_ws(self):
        ready = threading.Event()
        self._stop_ws = threading.Event()

        def pump():
            try:
                with websockets_sync.connect(
                    f"ws://127.0.0.1:{self.port}/ws", origin=self.url
                ) as ws:
                    ready.set()
                    while not self._stop_ws.is_set():
                        try:
                            self.messages.append(json.loads(ws.recv(timeout=0.5)))
                        except TimeoutError:
                            continue
            except (OSError, ConnectionClosed, ValueError) as e:
                self.ws_error = e
                ready.set()

        self.ws_error = None
        threading.Thread(target=pump, daemon=True).start()
        assert ready.wait(10) and self.ws_error is None, self.ws_error

    def wait_for(self, what, check, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {what}\n{self.logs()}")

    def logs(self):
        self.log.flush()
        return open(self.log.name).read()[-3000:]

    def stop(self):
        self._stop_ws.set()
        self.proc.terminate()
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


@pytest.fixture(scope="module")
def station(tmp_path_factory):
    s = Station(tmp_path_factory.mktemp("e2e"))
    try:
        s.wait_ready()
        s.follow_ws()
        yield s
    finally:
        s.stop()


def of_type(station, kind):
    return [m for m in list(station.messages) if m.get("type") == kind]


def test_night1_from_0210_detects_raises_incident_and_dispatches_after_approval(station):
    status, state = station.request("POST", "/api/sim/scenario",
                                    {"name": "night1", "speed": 60, "from": "02:10"})  # fmt: skip
    assert status == 200 and state["feed"] == "nodes"

    # 1. signed detections arrive, verified, from the real pipeline (not the stub)
    station.wait_for("verified detections", lambda: len(of_type(station, "detection")) >= 3)
    detections = of_type(station, "detection")
    assert all(m["detection"]["verified"] is True and not m.get("stub") for m in detections)
    assert {m["detection"]["sound"] for m in detections} & {"chainsaw"}
    stored = station.request("GET", "/api/detections?verified_only=true&limit=50")[1]["detections"]
    assert len(stored) >= 3 and all(d["verified"] for d in stored)

    # 2. an incident is created
    incidents = station.wait_for(
        "incident", lambda: station.request("GET", "/api/incidents")[1]["incidents"]
    )
    incident_id = incidents[0]["id"]

    # 3. an approval appears (WS message and incident view agree)
    approval = station.wait_for("approval", lambda: of_type(station, "approval"))[0]
    pending = station.wait_for(
        "pending approval",
        lambda: station.request("GET", f"/api/incidents/{incident_id}")[1]["pending_approval"],
    )
    assert pending["approval_id"] == approval["approval_id"]

    # 4. approving records a dispatch
    status, body = station.request(
        "POST", f"/api/approvals/{pending['approval_id']}", {"decision": "approve", "note": ""}
    )
    assert status == 200 and body["ok"]
    view = station.wait_for(
        "dispatch",
        lambda: (
            v if (v := station.request("GET", f"/api/incidents/{incident_id}")[1])["dispatches"]
            else None
        ),
    )
    assert view["dispatches"]
    assert station.request("POST", f"/api/approvals/{pending['approval_id']}",
                           {"decision": "approve"})[0] == 409  # decided: cannot be replayed  # fmt: skip

    # 5. the case file is sanitized html with intact records
    case = station.wait_for(
        "case file",
        lambda: station.request("GET", f"/api/incidents/{incident_id}/case_file")[1],
    )
    html = case["html"].lower()
    assert html and "<script" not in html and "onerror" not in html and "javascript:" not in html
    assert case["integrity"]["valid"] >= 1


def test_station_refuses_foreign_websocket_origins(station):
    from websockets.exceptions import InvalidStatus

    with pytest.raises(InvalidStatus) as refused, websockets_sync.connect(
        f"ws://127.0.0.1:{station.port}/ws", origin="https://evil.example"
    ):
        pass
    assert refused.value.response.status_code == 403
