"""eval/report.md from eval/results.json: a short table and a few plain sentences."""

from __future__ import annotations

from pathlib import Path

VARIANTS = tuple((m, p) for m in ("mock", "ollama", "haiku") for p in ("live", "waits", "lockstep"))


def label(mode: str, pace: str, model: str = "", speed: float | None = None) -> str:
    if mode == "mock":
        name = "Mock model"
    elif mode == "haiku":
        name = f"Cloud model ({model})"
    else:
        name = f"Local model ({model})" if model else "Local model"
    if pace == "lockstep":
        suffix = ", lockstep"
    elif pace == "waits":
        suffix = ", sim waits"
    elif speed is not None:
        suffix = f", live {speed:g}x"
    else:
        suffix = ", live"
    return name + suffix


def _run(data: dict, scenario: str, mode: str, memory: str = "fresh", pace: str = "live") -> dict | None:
    return data["runs"].get(f"{scenario}:{mode}:{memory}" + (f":{pace}" if pace in ("waits", "lockstep") else ""))


def _secs(s: float) -> str:
    return f"{s:.1f} s"


def _label_run(r: dict, mode: str, pace: str) -> str:
    # Older result files have no pace/speed fields; the run key still identifies waits.
    recorded_pace = r.get("pace", pace)
    return label(mode, recorded_pace, r.get("model", ""), r.get("speed"))


def night1_table(data: dict) -> list[str]:
    lines = [
        "| Night 1 | Alerts to rangers | Real caught | Real missed | False alerts | Time per incident |",
        "|---|---|---|---|---|---|",
    ]
    runs = [(mode, pace, _run(data, "night1", mode, pace=pace)) for mode, pace in VARIANTS]
    runs = [(mode, pace, r) for mode, pace, r in runs if r is not None]
    baselines_differ = bool(runs) and any(r["baseline"] != runs[0][2]["baseline"] for _, _, r in runs[1:])
    base_done = False
    for mode, pace, r in runs:
        total = len(r["real_incidents"])
        b, c = r["baseline"], r["commander"]
        if baselines_differ:
            lines.append(
                f"| Rules baseline ({_label_run(r, mode, pace)}) | {b['alerts']} | {b['real_caught']}/{total} | "
                f"{b['real_missed']} | {b['false_alerts']} | none |"
            )
        elif not base_done:
            lines.append(
                f"| Rules baseline | {b['alerts']} | {b['real_caught']}/{total} | {b['real_missed']} | "
                f"{b['false_alerts']} | none |"
            )
            base_done = True
        note = " (partial)" if r.get("partial") else ""
        lines.append(
            f"| Commander, {_label_run(r, mode, pace)}{note} | {c['requests']} | "
            f"{c['real_caught']}/{total} | {c['real_missed']} | {c['false_requests']} | "
            f"{_secs(c['wall_s_per_incident'])} |"
        )
    return lines


def _marsh(n: dict) -> str:
    return f"{n['triage_steps']} ({n['incidents']})" if n["incidents"] else "none"


def memory_table(data: dict) -> list[str]:
    rows = []
    for mode, pace in VARIANTS:
        for name, memory in (("no memory", "fresh"), ("after night 1", "carried")):
            r = _run(data, "night2", mode, memory, pace)
            if r is None:
                continue
            c = r["commander"]
            rows.append(
                f"| {_label_run(r, mode, pace)}, {name} | {c['requests']} | {c['triage_steps_total']} | "
                f"{c['steps_total']} | {_secs(c['wall_s_total'])} | {_marsh(c['noisy_node'])} |"
            )
    if not rows:
        return []
    head = [
        "| Night 2 | Requests | Triage steps | All steps | Commander time | El Bajo triage each |",
        "|---|---|---|---|---|---|",
    ]
    return head + rows


def sentences(data: dict) -> list[str]:
    out = []
    for mode, pace in VARIANTS:
        r = _run(data, "night1", mode, pace=pace)
        if r is None:
            continue
        b, c, total = r["baseline"], r["commander"], len(r["real_incidents"])
        name = _label_run(r, mode, pace).lower()
        verdict = "fewer" if c["requests"] < b["alerts"] else "no fewer"
        out.append(
            f"On night 1 the commander ({name}) put {c['requests']} requests in front of rangers where the "
            f"baseline sent {b['alerts']} alerts ({verdict}), and caught {c['real_caught']} of {total} real "
            f"incidents against the baseline's {b['real_caught']}; {c['false_requests']} of its requests were "
            f"false against {b['false_alerts']} false baseline alerts."
        )
        if c["requests"] < b["alerts"] and c["real_caught"] < b["real_caught"]:
            out.append("Fewer requests came with worse recall, a regression against the baseline.")
        if r.get("partial"):
            out.append(f"That run was cut short ({r['partial']}), so its numbers are partial.")
    for mode, pace in VARIANTS:
        fresh, carried = _run(data, "night2", mode, pace=pace), _run(data, "night2", mode, "carried", pace)
        if fresh and carried:
            f, c = fresh["commander"], carried["commander"]
            out.append(
                f"On night 2 the commander ({_label_run(carried, mode, pace).lower()}) took {c['triage_steps_total']} triage "
                f"steps with night 1's memory against {f['triage_steps_total']} without it."
            )
            if c["triage_steps_total"] < f["triage_steps_total"]:
                out.append("Night 1's memory reduced triage steps.")
            elif c["triage_steps_total"] == f["triage_steps_total"]:
                out.append("Night 1's memory did not reduce triage steps.")
            else:
                out.append("Night 1's memory increased triage steps.")
    return out


def write_report(data: dict, path: Path) -> None:
    parts = ["# Evaluation", "", "Rules baseline against the incident commander, replayed through the real pipeline.", ""]
    parts += night1_table(data) + [""]
    mem = memory_table(data)
    if mem:
        parts += mem + [""]
    parts += [" ".join(sentences(data)), ""]
    note = (
        "Real incidents come from the scenario files. The baseline alerts on every verified detection at 0.8 or "
        "above. The commander's requests are approved when the incident is real and rejected otherwise, so a "
        "false alert is a request a ranger had to turn down. Time per incident is the commander's own working "
        "time, approval waits excluded; the baseline has no analysis step. Live runs use the recorded simulation "
        "speed. Sim waits holds the clock during agent work, but the clock advances during drone flights. "
        "This is pacing assistance, not a strict lockstep replay with identical observations. Older runs without speed metadata are "
        "labeled simply live. Scoring v2 requires an executed dispatch with a snapshot of real evidence; "
        "approval alone earns no catch. Legacy results retain their original approval-based scoring. "
        "Lockstep runs deliver events and complete inference synchronously, advancing through intervening "
        "events during drone travel; real model responses can still vary. Synthetic clips are reported in JSON."
    )
    parts += [note, ""]
    for key, run in data["runs"].items():
        if run.get("pace") == "lockstep":
            parts.append(f"- {key}: scoring v{run['commander'].get('scoring_version', 1)}, "
                         f"{run.get('synthetic_clips', 0)}/{run.get('events_processed', 0)} synthetic clips. "
                         "Mock decisions validate the harness, not real-model judgment.")

    path.write_text("\n".join(parts))
