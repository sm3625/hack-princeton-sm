"""Bounded adaptation checks: --mode mock or --mode ollama (never a paid provider).

Scripted fixtures test the four requested investigation behaviors. Real-model results
are behavioral smoke tests, not accuracy estimates. Each case gets a fresh station.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ("rain", "unavailable", "camera", "correction")


def worker(case, mode):
    sys.path.insert(0, str(ROOT / "tests"))
    from agent_fakes import Harness

    from agent.llm import ROLES, get_chat_model
    from common.contracts import SoundClass as S
    from station.config import load_settings

    os.environ["LLM_MODE"] = mode
    settings = load_settings()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp), models={r: get_chat_model(r, settings) for r in ROLES})
        if case == "rain":
            h.sim.rain = {n.sector for n in h.world.nodes}
        h.hear("Arroyo Recodo", 1, "02:14:05", S.CHAINSAW, .95)
        h.hear("Paso Musgo", 1, "02:14:09", S.CHAINSAW, .92)
        inc, = h.commander.incidents.all()
        if case == "unavailable":
            h.sim.battery = 5
        if case == "camera":
            h.sim.seen[inc.sector] = "Inconclusive: dense canopy; no activity seen in the visible clearing."
        began = time.monotonic()
        h.tick("02:14:30")
        had_proposal = bool(h.pending())
        if case == "correction":
            h.commander.message(inc.id, "Permit verified; this crew is authorized", "authorized_activity")
            h.tick("02:15:00")
        pending = h.pending()
        if case == "rain":
            passed = bool(inc.assessments and (h.sim.flights or pending or inc.next_check_at)) and inc.status != "dismissed"
        elif case == "unavailable":
            text = " ".join([inc.summary, *(a.note for a in pending), *(a["reason"] for a in inc.assessments)]).lower()
            passed = bool(inc.assessments) and not h.sim.flights and any(x in text for x in ("no aerial", "unavailable", "could not", "cannot", "battery"))
        elif case == "camera":
            passed = bool(inc.observations and inc.assessments) and not pending and inc.status == "watching" and inc.next_check_at is not None
        else:
            passed = had_proposal and bool(inc.assessments) and not pending and inc.status == "dismissed"
        return {"case": case, "passed": bool(passed and not inc.dispatches),
                "status": inc.status, "summary": inc.summary,
                "assessments": inc.assessments, "observations": inc.observations,
                "pending": [a.note for a in pending], "wall_s": round(time.monotonic() - began, 2)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("mock", "ollama"), default="mock")
    ap.add_argument("--case", choices=CASES)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()
    if args.case:
        result = worker(args.case, args.mode)
        out = args.out or ROOT / "eval" / f"investigation_{args.mode}_{args.case}.json"
        out.write_text(json.dumps(result, indent=2) + "\n")
        return
    results = []
    for case in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result.json"
            try:
                subprocess.run([sys.executable, "-m", "eval.investigation", "--mode", args.mode,
                                "--case", case, "--out", str(out)], cwd=ROOT,
                               capture_output=True, check=True, timeout=args.timeout)
                result = json.loads(out.read_text())
            except subprocess.TimeoutExpired:
                result = {"case": case, "passed": False, "error": "timeout", "limit_s": args.timeout}
            except subprocess.CalledProcessError as error:
                result = {"case": case, "passed": False, "error": error.stderr.decode()[-1000:]}
        results.append(result)
        print(f"{case}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        if not result["passed"]:
            print(json.dumps(result), flush=True)
    out = args.out or ROOT / "eval" / f"investigation_{args.mode}.json"
    out.write_text(json.dumps({"mode": args.mode, "model": os.getenv("OLLAMA_MODEL", "qwen3:8b") if args.mode == "ollama" else "scripted mock",
                               "cases": results}, indent=2) + "\n")
    print(f"Wrote {out}", flush=True)


if __name__ == "__main__":
    main()
