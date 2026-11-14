"""Demo eval: about 2 minutes, sounds in quick succession, the model visibly working.

    uv run python -m eval.demo                 # fresh station on :8010, your LLM_MODE (.env)
    uv run python -m eval.demo --auto-ranger   # also play the ranger: approve real, reject false
    uv run python -m eval.demo --url http://127.0.0.1:8000   # use a station already running

The realistic evaluation is eval/run.py (whole nights, scored against a baseline). This one is
for showing the system: it plays scenarios/demo.yaml (three incidents and follow-up sounds over
~36 sim-minutes at 20x), prints a live timeline of what the agent does, and ends with a short
scorecard against the scenario's labels. Open the dashboard URL it prints to watch the map.

Approvals are left to a person (dashboard or Photon) unless --auto-ranger is given. Never calls a
paid model by itself: the station uses whatever LLM_MODE your environment sets.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "demo_last.json"
SCENARIO = "demo"
SPEED = 20.0
REAL_EXPECTS_REQUEST = {"real"}  # labels whose incident should reach a ranger


def http(method: str, url: str, body: dict | None = None, timeout: float = 10.0):
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as r:  # noqa: S310 - localhost station only
        return json.load(r)


def expected_groups() -> dict[str, dict]:
    """Ground truth from the scenario file: incident key -> label and the nodes that hear it."""
    from sim.scenario import load_scenario

    groups: dict[str, dict] = {}
    for e in load_scenario(SCENARIO).events:
        key = e.incident or f"{e.label}-{e.node}"
        g = groups.setdefault(key, {"label": e.label, "nodes": set(), "first_at": e.at})
        g["nodes"].add(e.node)
    return groups


@dataclass
class Tracker:
    """Turns polled station state into timeline lines and a scorecard."""

    t0: float
    seen: dict[str, str] = field(default_factory=dict)  # incident id -> last status
    first_decision_s: dict[str, float] = field(default_factory=dict)
    created_s: dict[str, float] = field(default_factory=dict)
    requested: set[str] = field(default_factory=set)
    nodes: dict[str, set[str]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    detections: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def say(self, text: str) -> None:
        print(f"[{self.elapsed():5.1f}s] {text}", flush=True)

    def update(self, incidents: list[dict]) -> None:
        for inc in incidents:
            iid, status = inc["id"], inc.get("status", "")
            self.nodes[iid] = set(inc.get("nodes") or [])
            self.titles[iid] = inc.get("title") or iid
            if iid not in self.seen:
                self.created_s[iid] = self.elapsed()
                self.say(f"incident opened: {self.titles[iid]} ({inc.get('sector', '?')})")
            if self.seen.get(iid) != status:
                if status != "open" and iid not in self.first_decision_s:
                    took = self.elapsed() - self.created_s.get(iid, self.elapsed())
                    self.first_decision_s[iid] = took
                    self.say(f"agent decided: {self.titles[iid]} -> {status} ({took:.0f}s after it opened)")
                elif iid in self.seen:
                    self.say(f"{self.titles[iid]}: {self.seen[iid]} -> {status}")
                if status == "awaiting_approval":
                    self.requested.add(iid)
                if status == "dispatched":
                    self.requested.add(iid)
                self.seen[iid] = status

    def score(self, groups: dict[str, dict]) -> list[dict]:
        rows = []
        for key, g in groups.items():
            matches = [i for i, n in self.nodes.items() if n & g["nodes"]]
            asked = any(i in self.requested for i in matches)
            want = g["label"] in REAL_EXPECTS_REQUEST
            decided = [self.first_decision_s[i] for i in matches if i in self.first_decision_s]
            if not matches:
                verdict = "not opened"
            elif not decided:
                verdict = "no decision yet"
            else:
                verdict = "correct" if asked == want else "wrong"
            rows.append({
                "incident": key,
                "truth": g["label"],
                "expected": "ask a ranger" if want else "no ranger request",
                "agent": ("asked a ranger" if asked else ", ".join(sorted({self.seen[i] for i in matches})) or "-"),
                "seconds_to_decide": round(min(decided), 1) if decided else None,
                "verdict": verdict,
            })
        return rows


def start_station(port: int, tmp: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "AGENT_FS_DIR": str(tmp / "agent_fs"),
        "AGENT_DB": str(tmp / "agent.sqlite"),
        "EVENTS_DB": str(tmp / "events.sqlite"),
        "NODE_DATA_DIR": str(tmp / "node_data"),
    }
    log = open(tmp / "station.log", "w")  # noqa: SIM115 - closed with the process
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "station.app:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def wait_ready(url: str, seconds: float = 60) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            http("GET", url + "/api/sim/state", timeout=2)
            return
        except (URLError, HTTPError, OSError, ValueError):
            time.sleep(0.5)
    raise SystemExit(f"station at {url} did not come up in {seconds:.0f}s")


def auto_ranger(url: str, tracker: Tracker, groups: dict[str, dict], decided: set[str]) -> None:
    """Approve requests for incidents whose nodes match a real group; reject the rest."""
    real_nodes = set().union(*(g["nodes"] for g in groups.values() if g["label"] == "real"))
    for iid, status in list(tracker.seen.items()):
        if status != "awaiting_approval":
            continue
        view = http("GET", f"{url}/api/incidents/{iid}")
        pending = view.get("pending_approval")
        if not pending or pending["approval_id"] in decided:
            continue
        real = bool(tracker.nodes.get(iid, set()) & real_nodes)
        decision = "approve" if real else "reject"
        body = {"decision": decision, "note": "" if real else "Rain, not a saw"}
        try:
            http("POST", f"{url}/api/approvals/{pending['approval_id']}", body)
            tracker.say(f"ranger (auto) {decision}d dispatch for {tracker.titles[iid]}")
        except HTTPError as e:
            tracker.say(f"ranger (auto) could not decide: HTTP {e.code}")
        decided.add(pending["approval_id"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="use a station already running here instead of starting one")
    ap.add_argument("--port", type=int, default=8010, help="port for the fresh station (default 8010)")
    ap.add_argument("--speed", type=float, default=SPEED, help="sim speed (default 20x, about 2 min)")
    ap.add_argument("--minutes", type=float, default=2.5, help="wall-time cap (default 2.5)")
    ap.add_argument("--auto-ranger", action="store_true", help="approve real incidents, reject others")
    args = ap.parse_args(argv)

    groups = expected_groups()
    proc, tmp = None, None
    url = (args.url or f"http://127.0.0.1:{args.port}").rstrip("/")
    try:
        if not args.url:
            tmp = Path(tempfile.mkdtemp(prefix="fg-demo-"))
            proc = start_station(args.port, tmp)
        wait_ready(url)
        mode = http("GET", url + "/api/health").get("model") or "unknown model"
        print(f"Forest Guardian demo: open {url} to watch. Model: {mode}. Speed {args.speed:g}x.")
        http("POST", url + "/api/sim/scenario", {"name": SCENARIO, "speed": args.speed})
        tracker = Tracker(t0=time.monotonic())
        tracker.say("scenario started: rain on the west side, then a chainsaw, then gunshots")
        decided: set[str] = set()
        cap = args.minutes * 60
        ended_at = None
        while tracker.elapsed() < cap:
            data = http("GET", url + "/api/incidents")
            tracker.update(data.get("incidents", []))
            if args.auto_ranger:
                auto_ranger(url, tracker, groups, decided)
            state = http("GET", url + "/api/sim/state")
            if not state.get("running") and ended_at is None and tracker.elapsed() > 5:
                ended_at = tracker.elapsed()
                tracker.say("all sounds played")
            # finish once every expected group has a decision and the agent is idle
            done = all(r["verdict"] in ("correct", "wrong") for r in tracker.score(groups))
            if done and ended_at is not None and not data.get("busy"):
                break
            time.sleep(1.0)
        rows = tracker.score(groups)
        print("\nScorecard")
        for r in rows:
            took = f"{r['seconds_to_decide']:.0f}s" if r["seconds_to_decide"] is not None else "-"
            print(f"  {r['incident']:<10} truth={r['truth']:<11} expected: {r['expected']:<18} "
                  f"agent: {r['agent']:<16} {took:>5}  {r['verdict'].upper()}")
        correct = sum(r["verdict"] == "correct" for r in rows)
        print(f"{correct}/{len(rows)} correct in {tracker.elapsed():.0f}s (model: {mode})")
        OUT.write_text(json.dumps({"model": mode, "speed": args.speed, "seconds": round(tracker.elapsed(), 1),
                                   "correct": correct, "of": len(rows), "rows": rows}, indent=2) + "\n")
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
