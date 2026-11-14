"""Quick judgment eval on the real local model, under 10 minutes.

    make eval-quick            # or: uv run python -m eval.quick

Four hand-picked Night 1 incidents, each fed to the commander on its own in-process test
station (real qwen3:8b, real tools, signed records; fake clock, instant drone). Each case stops
at the decision: a dispatch request, or dismissed/watching. No approval, follow-up or case file.

Each case runs in its own process with a time limit, so a stuck model cannot eat the budget.
It does NOT test the classifier, the radio, night-long grouping or 60x time pressure: the full
replay (eval.run) does. Never calls a paid model.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASE_LIMIT_S = 150
TOTAL_LIMIT_S = 600
OUT_JSON = ROOT / "eval" / "quick_results.json"
OUT_MD = ROOT / "eval" / "quick_report.md"
RAIN_WEST = {f"{c}{r}" for c in "ABC" for r in range(1, 7)}  # night1 rain area A1:C6

# id, title, expected, rain sectors, records (node, seq, time, sound, confidence)
CASES = [
    ("A", "Logging: chainsaw at 3 dry nodes", "dispatch_requested", set(),
     [("Creek Bend", 41, "02:14:05", "CHAINSAW", 0.91), ("Mossy Gap", 17, "02:14:31", "CHAINSAW", 0.84),
      ("Logging Road", 23, "02:14:44", "CHAINSAW", 0.86)]),
    ("C", "Rain cluster: chainsaw at 2 nodes in rain", "no_request", RAIN_WEST,
     [("Low Marsh", 3, "00:16:46", "CHAINSAW", 0.83), ("Fern Hollow", 1, "00:21:02", "CHAINSAW", 0.82),
      ("Low Marsh", 4, "00:24:27", "CHAINSAW", 0.85)]),
    ("B", "Gunshots: 2 dry nodes", "dispatch_requested", set(),
     [("River Mouth", 5, "04:37:12", "GUNSHOT", 0.94), ("Logging Road", 30, "04:37:14", "GUNSHOT", 0.88)]),
    ("D", "Single node in rain", "no_request", RAIN_WEST,
     [("South Gate", 2, "00:40:10", "CHAINSAW", 0.81)]),
]
_TAG = re.compile(r"\[([A-Z][A-Za-z ]+?) #\d+\]")


def run_case(case_id: str, out: Path) -> None:
    """Worker: one case on a fresh station, result written as JSON."""
    sys.path.insert(0, str(ROOT / "tests"))
    from agent_fakes import Harness, at  # the same in-process station the agent tests use

    from agent.llm import ROLES, get_chat_model
    from common.contracts import SoundClass
    from station.config import load_settings

    _, _title, _expected, rain, records = next(c for c in CASES if c[0] == case_id)
    settings = load_settings()
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp), models={r: get_chat_model(r, settings) for r in ROLES})
        h.sim.rain = set(rain)
        for node, seq, hhmmss, sound, conf in records:
            h.hear(node, seq, hhmmss, SoundClass[sound], conf)
        last = max(at(r[2]) for r in records) + 15
        h.sim.t = last
        began = time.monotonic()
        h.commander.tick(last)
        wall = time.monotonic() - began
        (inc,) = h.commander.incidents.all()
        decision = "dispatch_requested" if h.pending() else inc.status
        text = " ".join(h.file(inc.id, n) or "" for n in ("triage.md", "plan.md"))
        names = {n.name for n in h.world.nodes}
        invented = sorted({m for m in _TAG.findall(text) if m not in names})
        stats = h.commander._stats.get(inc.id, {})
        out.write_text(json.dumps({
            "decision": decision,
            "summary": inc.summary,
            "wall_s": round(wall, 1),
            "model_steps": sum(stats.get("steps", {}).values()),
            "invented_names": invented,
            "dispatches": len(inc.dispatches),
        }))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--case", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.case:
        run_case(args.case, Path(args.out))
        return

    from station.config import load_settings

    settings = load_settings()
    if settings.llm_mode != "ollama":
        sys.exit(f"eval.quick runs on the local model only; LLM_MODE is {settings.llm_mode}")
    _warm_up(settings.ollama_model)
    start, rows = time.monotonic(), []
    for cid, title, expected, _rain, _recs in CASES:
        left = TOTAL_LIMIT_S - (time.monotonic() - start)
        row = {"case": cid, "title": title, "expected": expected}
        if left < 30:
            row.update(decision="skipped (10 min budget used)", correct=False)
            rows.append(row)
            continue
        limit = min(CASE_LIMIT_S, left)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out = Path(f.name)
        t0 = time.monotonic()
        try:
            subprocess.run([sys.executable, "-m", "eval.quick", "--case", cid, "--out", str(out)],
                           cwd=ROOT, timeout=limit, check=True, capture_output=True)
            row.update(json.loads(out.read_text()))
        except subprocess.TimeoutExpired:
            row.update(decision=f"no decision in {limit:.0f} s", wall_s=round(limit))
        except subprocess.CalledProcessError as e:
            row.update(decision="error", error=e.stderr.decode()[-300:])
        finally:
            out.unlink(missing_ok=True)
        got = row.get("decision", "")
        row["correct"] = got == expected or (expected == "no_request" and got in ("dismissed", "watching"))
        row["case_wall_s"] = round(time.monotonic() - t0, 1)
        rows.append(row)
        print(f"[{cid}] {title}: {got} ({'ok' if row['correct'] else 'WRONG'}, {row['case_wall_s']} s)", flush=True)
    total = round(time.monotonic() - start, 1)
    result = {"model": settings.ollama_model, "total_s": total, "cases": rows,
              "correct": sum(r["correct"] for r in rows), "of": len(rows)}
    OUT_JSON.write_text(json.dumps(result, indent=2) + "\n")
    OUT_MD.write_text(_report(result))
    print(f"{result['correct']}/{result['of']} correct in {total} s; wrote {OUT_MD.relative_to(ROOT)}")


def _warm_up(model: str) -> None:
    """Load the model into memory so case A does not pay for it."""
    from langchain_ollama import ChatOllama

    ChatOllama(model=model, num_predict=1, reasoning=False).invoke("hi")


def _report(r: dict) -> str:
    lines = [
        f"# Quick judgment eval ({r['model']})", "",
        f"{r['correct']} of {r['of']} correct in {r['total_s']:.0f} s. Each case is one Night 1 incident on its own,",
        "stopped at the decision. Not a full-night score: see report.md for that.", "",
        "| Case | Expected | Decision | OK | Time | Model steps | Invented names |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in r["cases"]:
        lines.append(
            f"| {c['case']}: {c['title']} | {c['expected']} | {c.get('decision', '')} | "
            f"{'yes' if c['correct'] else 'no'} | {c.get('case_wall_s', '')} s | {c.get('model_steps', '')} | "
            f"{', '.join(c.get('invented_names', [])) or 'none'} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
