"""Replay a whole night through the real station and score it (SPEC 3.9).

    uv run python -m eval.run --scenario night1 --mode mock
    uv run python -m eval.run --all --mode mock                 # night1, night2, night2 after night1
    uv run python -m eval.run --scenario night1 --mode ollama

Each run starts a station process (real classifier, signing, receiver, commander) with temp
database, keys and agent folders, plays the night at 60x over HTTP and plays the ranger: it
approves approvals for real incidents and rejects the rest, so the night finishes unattended.
Results are merged into eval/results.json and eval/report.md. Cloud mode is opt-in via --mode haiku and requires credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from websockets.exceptions import ConnectionClosed

from agent.llm import HAIKU_MODEL
from common.contracts import ROOT, load_world
from eval import metrics
from eval.report import write_report
from sim.scenario import load_scenario

RESULTS = ROOT / "eval" / "results.json"
MODES = ("mock", "ollama", "haiku", "gemini")
QUIET_S = {"mock": 20.0, "ollama": 150.0, "haiku": 60.0, "gemini": 60.0}  # no agent activity for this long = night is done
DEFAULT_MAX_MIN = {"mock": 30.0, "ollama": 20.0, "haiku": 20.0, "gemini": 20.0}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pace_clock(station: Station, sim: dict, busy: bool, sim_waits: bool) -> None:
    """Hold inference time, but let a blocking drone flight advance and finish."""
    flying = any(d["flying"] for d in sim.get("drones", []) or [sim.get("drone") or {}] if d)
    if not sim["running"]:
        paused = not flying  # a flight can outlast the scenario's automatic pause
    elif sim_waits:
        paused = busy and not flying
    else:
        return
    if paused != sim["paused"]:
        status, _ = station.request("POST", "/api/sim/pause", {"paused": paused})
        if status != 200:
            raise RuntimeError(f"could not {'pause' if paused else 'resume'} simulation: {status}")


class Station:
    """A station subprocess with throwaway state, driven over HTTP and watched over /ws."""

    def __init__(self, workdir: Path, mode: str, memory_from: Path | None = None) -> None:
        from websockets.sync.client import connect

        self._connect = connect
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.agent_fs = workdir / "agent_fs"
        if memory_from is not None:  # what the commander learned on an earlier night
            shutil.copytree(memory_from / "memories", self.agent_fs / "memories")
        env = {
            **os.environ,
            "LLM_MODE": mode,
            "EVENTS_DB": str(workdir / "events.sqlite"),
            "KEYS_DIR": str(workdir / "keys"),
            "AGENT_FS_DIR": str(self.agent_fs),
            "AGENT_DB": str(workdir / "agent.sqlite"),
            "NODE_DATA_DIR": str(workdir / "node_data"),
            "LORA_UDP_PORT": "off",
            "LORA_LOSS": "0",
            "STATION_DEV": "0",
            "NODE_PIPELINE": "on",
        }
        self.log_path = workdir / "station.log"
        self.log = self.log_path.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "station.app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        self.messages: list[dict] = []
        self.last_activity = time.monotonic()
        self._stop = threading.Event()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"station exited:\n{self.logs()}")
            try:
                if self.request("GET", "/api/health")[0] == 200:
                    return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError(f"station did not start:\n{self.logs()}")

    def follow(self) -> None:
        ready = threading.Event()

        def pump() -> None:
            try:
                with self._connect(f"ws://127.0.0.1:{self.port}/ws") as ws:
                    ready.set()
                    while not self._stop.is_set():
                        try:
                            msg = json.loads(ws.recv(timeout=0.5))
                        except TimeoutError:
                            continue
                        self.messages.append(msg)
                        if msg.get("type") in ("agent_step", "incident", "approval", "plan"):
                            self.last_activity = time.monotonic()
            except (OSError, ValueError, ConnectionClosed):  # socket closed or a bad frame: stop following
                ready.set()

        threading.Thread(target=pump, daemon=True).start()
        ready.wait(10)

    def logs(self) -> str:
        self.log.flush()
        return self.log_path.read_text()[-3000:]

    def stop(self) -> None:
        self._stop.set()
        self.proc.terminate()
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def run_night(
    scenario_name: str, mode: str, *, memory_from: Path | None = None, speed: float = 60.0,
    max_wall_min: float | None = None, from_time: str | None = None, sim_waits: bool = False, log=print,
) -> tuple[dict[str, Any], Path]:  # fmt: skip
    """Play one night. Returns (results, agent folder to carry memory forward)."""
    world = load_world()
    scenario = load_scenario(scenario_name, world=world)
    max_wall = 60 * (max_wall_min or DEFAULT_MAX_MIN[mode])
    workdir = Path(tempfile.mkdtemp(prefix=f"eval-{scenario_name}-{mode}-"))
    station = Station(workdir, mode, memory_from)
    tag = f"{scenario_name}:{mode}" + (":carried" if memory_from else "") + (":waits" if sim_waits else "")
    began = time.monotonic()
    partial = None
    decided: dict[str, str] = {}
    try:
        station.wait_ready()
        station.follow()
        body = {"name": scenario_name, "speed": speed, **({"from": from_time} if from_time else {})}
        status, state = station.request("POST", "/api/sim/scenario", body)
        if status != 200 or state.get("feed") != "nodes":
            raise RuntimeError(f"scenario did not start with the real pipeline: {status} {state}")
        truth: dict[tuple[str, int], str | None] = {}
        while True:
            time.sleep(0.2 if sim_waits else 0.5)
            if station.proc.poll() is not None:
                raise RuntimeError(f"station died:\n{station.logs()}")
            elapsed = time.monotonic() - began
            truth.update(_truth(station, scenario))
            for msg in list(station.messages):
                if msg.get("type") == "approval" and msg["status"] == "pending":
                    aid = msg["approval_id"]
                    if aid not in decided:
                        decided[aid] = _decide(station, msg, truth)
                        log(f"[{tag}] {elapsed:5.0f}s approval {msg['sector']}: {decided[aid]}")
            if elapsed > max_wall:
                partial = f"stopped at the {max_wall / 60:.0f} min wall-time cap"
                break
            sim = station.request("GET", "/api/sim/state")[1]
            busy = bool(station.request("GET", "/api/incidents")[1]["busy"])
            pace_clock(station, sim, busy, sim_waits)
            quiet = time.monotonic() - station.last_activity
            pending = any(
                m["status"] == "pending" and m["approval_id"] not in decided
                for m in station.messages
                if m.get("type") == "approval"
            )
            if (not sim["running"] and not any(d["flying"] for d in sim.get("drones", []) or [sim.get("drone") or {}] if d) and not busy
                    and quiet > QUIET_S[mode] and not pending):
                break
        wall = time.monotonic() - began
        rows = station.request("GET", "/api/detections?limit=500&verified_only=true")[1]["detections"]
        truth.update(_truth(station, scenario, rows))
        incidents = station.request("GET", "/api/incidents")[1]["incidents"]
        views = [station.request("GET", f"/api/incidents/{i['id']}")[1] for i in incidents]
        result = {
            "scenario": scenario_name,
            "mode": mode,
            "memory": "carried" if memory_from else "fresh",
            "model": os.getenv("OLLAMA_MODEL", "qwen3:8b") if mode == "ollama" else HAIKU_MODEL if mode == "haiku" else os.getenv("GEMINI_MODEL", "gemini-3.5-flash") if mode == "gemini" else "mock",
            "speed": speed,
            "pace": "waits" if sim_waits else "live",
            "from": from_time,
            "wall_s": round(wall),
            "partial": partial,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "real_incidents": metrics.real_groups(scenario.events),
            "detections": {"verified": len(rows), "events": len(scenario.events)},
            "baseline": metrics.score_baseline(rows, scenario.events, scenario.start, world),
            "commander": metrics.score_commander(views, rows, scenario.events, scenario.start),
        }
        log(f"[{tag}] done in {wall:.0f}s" + (f" ({partial})" if partial else ""))
        return result, station.agent_fs
    finally:
        station.stop()


def _truth(station: Station, scenario, rows: list[dict] | None = None) -> dict[tuple[str, int], str | None]:
    if rows is None:
        rows = station.request("GET", "/api/detections?limit=500&verified_only=true")[1]["detections"]
    return {
        (r["node"], r["seq"]): metrics.group_of(
            metrics.match_event(scenario.events, scenario.start, r["node"], r["timestamp"])
        )
        for r in rows
    }


def _decide(station: Station, msg: dict, truth: dict) -> str:
    view = station.request("GET", f"/api/incidents/{msg['incident_id']}")[1]
    decision = "approve" if metrics.want_dispatch(msg if "records" in msg else view, truth) else "reject"
    note = "" if decision == "approve" else "Not a real incident."
    station.request("POST", f"/api/approvals/{msg['approval_id']}", {"decision": decision, "note": note})
    return decision


def load_results(path: Path = RESULTS) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"runs": {}}


def save(result: dict, path: Path = RESULTS) -> None:
    data = load_results(path)
    key = f"{result['scenario']}:{result['mode']}:{result['memory']}" + (f":{result['pace']}" if result.get("pace") in ("waits", "lockstep") else "")
    data["runs"][key] = result
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(data, indent=2) + "\n")
    write_report(data, path.with_name("report.md"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="night1")
    ap.add_argument("--mode", choices=MODES, default="mock")
    ap.add_argument("--after", help="play this night first and carry its memory into --scenario")
    ap.add_argument("--all", action="store_true", help="night1, then night2 fresh and night2 after night1")
    ap.add_argument("--sim-waits", action="store_true", help="hold the sim during agent work, except while the drone flies")
    ap.add_argument("--lockstep", action="store_true", help="deterministic synchronous pipeline replay")
    ap.add_argument("--speed", type=float, default=60.0)
    ap.add_argument("--from", dest="from_time", help="skip ahead to this clock time, e.g. 02:00")
    ap.add_argument("--max-minutes", type=float, help="wall-time cap per night")
    args = ap.parse_args(argv)
    kw = {"speed": args.speed, "max_wall_min": args.max_minutes, "from_time": args.from_time, "sim_waits": args.sim_waits}

    lock = threading.Lock()

    def play(name: str, memory: Path | None = None) -> Path:
        if args.lockstep:
            if args.from_time or args.sim_waits:
                ap.error("--lockstep cannot be combined with --from or --sim-waits")
            from eval.lockstep import run_lockstep
            result, folder = run_lockstep(name, args.mode, memory_from=memory,
                                         max_wall_min=args.max_minutes or 30)
        else:
            result, folder = run_night(name, args.mode, memory_from=memory, **kw)
        with lock:
            save(result)
        return folder

    if args.all:
        folder = play("night1")
        # both second nights side by side, so they share the machine's load equally
        with ThreadPoolExecutor(max_workers=2) as pool:
            for f in [pool.submit(play, "night2"), pool.submit(play, "night2", folder)]:
                f.result()
    elif args.after:
        play(args.scenario, memory=play(args.after))
    else:
        play(args.scenario)
    print(f"wrote {RESULTS} and report.md")


if __name__ == "__main__":
    main()
