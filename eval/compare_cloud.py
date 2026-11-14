"""Bounded paired Deep Agents comparison, using real tools and isolated synthetic incidents.

    python -m eval.compare_cloud --modes haiku gemini --repeats 1

Cloud calls cost money. No real ranger messages or approvals are sent. Missing credentials
are recorded as blocked, never as a quality score. One pass is a smoke comparison, not a
statistically reliable ranking. Each case has a process timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from eval.quick import CASES, ROOT


def score(row):
    if row.get("status") != "completed" or row.get("agent_failures", 0):
        return False
    outcome = row.get("decision")
    correct = outcome == row["expected"] or (
        row["expected"] == "no_request" and outcome in ("watching", "dismissed")
    )
    return correct and row.get("dispatches") == 0 and not row.get("invented_names")


def summarize(rows, modes):
    summary = {}
    for mode in modes:
        selected = [r for r in rows if r["mode"] == mode]
        completed = [r for r in selected if r["status"] == "completed"]
        summary[mode] = {
            "attempted": len(selected),
            "completed": len(completed),
            "passed": sum(score(r) for r in selected),
            "median_decision_s": statistics.median(r["wall_s"] for r in completed)
            if completed
            else None,
        }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modes", nargs="+", choices=["haiku", "gemini", "mock"], default=["haiku", "gemini"]
    )
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--case-timeout", type=float, default=150)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/cloud_comparison.json")
    args = parser.parse_args()
    from agent.llm import HAIKU_MODEL, get_chat_model
    from station.config import load_settings

    settings = load_settings()
    blocked = {}
    models = {"haiku": HAIKU_MODEL, "gemini": settings.gemini_model, "mock": "mock"}
    for mode in args.modes:
        try:
            get_chat_model(settings=replace(settings, llm_mode=mode))
        except Exception as exc:  # noqa: BLE001 - report provider setup failures without secrets
            # ConfigError messages contain remediation, no credential material.
            from station.config import ConfigError

            blocked[mode] = str(exc) if isinstance(exc, ConfigError) else type(exc).__name__
            print(f"{mode}: BLOCKED ({blocked[mode]})", flush=True)
    rows = []
    started = time.time()

    def save():
        report = {
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "models": models,
            "blocked": blocked,
            "repeats": args.repeats,
            "limits": "Four synthetic incidents; real agent/tools. No real dispatch. "
            "Single passes are not a reliable quality ranking.",
            "rows": rows,
            "summary": summarize(rows, args.modes),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    save()
    with tempfile.TemporaryDirectory() as tmp:
        for repeat in range(args.repeats):
            for index, (cid, title, expected, _, _) in enumerate(CASES):
                # Alternate first provider to reduce systematic warm/order bias.
                modes = args.modes if (repeat + index) % 2 == 0 else list(reversed(args.modes))
                for mode in modes:
                    if mode in blocked:
                        continue
                    row = {
                        "mode": mode,
                        "repeat": repeat + 1,
                        "case": cid,
                        "title": title,
                        "expected": expected,
                    }
                    out = Path(tmp) / "case.json"
                    out.unlink(missing_ok=True)
                    start = time.perf_counter()
                    try:
                        subprocess.run(
                            [
                                sys.executable,
                                "-m",
                                "eval.quick",
                                "--mode",
                                mode,
                                "--case",
                                cid,
                                "--out",
                                str(out),
                            ],
                            cwd=ROOT,
                            env={**os.environ, "LLM_MODE": mode},
                            capture_output=True,
                            timeout=args.case_timeout,
                            check=True,
                        )
                        row.update(json.loads(out.read_text()), status="completed")
                    except subprocess.TimeoutExpired:
                        row.update(status="timeout")
                    except subprocess.CalledProcessError as exc:
                        row.update(status="error", exit_code=exc.returncode)
                    row["total_s"] = round(time.perf_counter() - start, 3)
                    row["passed"] = score(row)
                    rows.append(row)
                    save()
                    print(
                        f"{mode} {cid}: {row['status']} / {row.get('decision', 'no decision')} "
                        f"/ passed={row['passed']} / {row['total_s']:.1f}s",
                        flush=True,
                    )
    print(f"Report: {args.output}", flush=True)


if __name__ == "__main__":
    main()
