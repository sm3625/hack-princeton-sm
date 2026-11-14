"""eval/report.md from eval/results.json: a short table and a few plain sentences."""

from __future__ import annotations

from pathlib import Path

MODE_NAMES = {"mock": "Mock model", "ollama": "Local model"}


def _run(data: dict, scenario: str, mode: str, memory: str = "fresh") -> dict | None:
    return data["runs"].get(f"{scenario}:{mode}:{memory}")


def _secs(s: float) -> str:
    return f"{s:.1f} s"


def night1_table(data: dict) -> list[str]:
    lines = [
        "| Night 1 | Alerts to rangers | Real caught | Real missed | False alerts | Time per incident |",
        "|---|---|---|---|---|---|",
    ]
    base_done = False
    for mode in ("mock", "ollama"):
        r = _run(data, "night1", mode)
        if r is None:
            continue
        total = len(r["real_incidents"])
        b, c = r["baseline"], r["commander"]
        if not base_done:
            lines.append(f"| Rules baseline | {b['alerts']} | {b['real_caught']}/{total} | {b['real_missed']} | {b['false_alerts']} | none |")
            base_done = True
        name = MODE_NAMES[mode] + (f" ({r['model']})" if mode == "ollama" else "")
        note = " (partial)" if r.get("partial") else ""
        lines.append(
            f"| Commander, {name}{note} | {c['requests']} | {c['real_caught']}/{total} | "
            f"{c['real_missed']} | {c['false_requests']} | {_secs(c['wall_s_per_incident'])} |"
        )
    return lines


def memory_table(data: dict) -> list[str]:
    rows = []
    for mode in ("mock", "ollama"):
        for label, memory in (("no memory", "fresh"), ("after night 1", "carried")):
            r = _run(data, "night2", mode, memory)
            if r is None:
                continue
            c = r["commander"]
            rows.append(
                f"| {MODE_NAMES[mode]}, {label} | {c['requests']} | {c['triage_steps_total']} | "
                f"{c['steps_total']} | {_secs(c['wall_s_total'])} | {_marsh(c['noisy_node'])} |"
            )
    if not rows:
        return []
    head = [
        "| Night 2 | Requests | Triage steps | All steps | Commander time | Low Marsh triage each |",
        "|---|---|---|---|---|---|",
    ]
    return head + rows


def _marsh(n: dict) -> str:
    return f"{n['triage_steps']} ({n['incidents']})" if n["incidents"] else "none"


def sentences(data: dict) -> list[str]:
    out = []
    for mode in ("mock", "ollama"):
        r = _run(data, "night1", mode)
        if r is None:
            continue
        b, c, total = r["baseline"], r["commander"], len(r["real_incidents"])
        name = MODE_NAMES[mode].lower()
        cut = b["alerts"] - c["requests"]
        verdict = "fewer" if cut > 0 else "no fewer"
        out.append(
            f"On night 1 the {name} commander put {c['requests']} requests in front of rangers where the baseline "
            f"sent {b['alerts']} alerts ({verdict}), and caught {c['real_caught']} of {total} real incidents "
            f"against the baseline's {b['real_caught']}; {c['false_requests']} of its requests were false "
            f"against {b['false_alerts']} false baseline alerts."
        )
        if r.get("partial"):
            out.append(f"The {name} run was cut short ({r['partial']}), so its numbers are partial.")
    for mode in ("mock", "ollama"):
        fresh, carried = _run(data, "night2", mode), _run(data, "night2", mode, "carried")
        if fresh and carried:
            f, c = fresh["commander"], carried["commander"]
            out.append(
                f"On night 2 the {MODE_NAMES[mode].lower()} commander took {c['triage_steps_total']} triage steps "
                f"with night 1's memory against {f['triage_steps_total']} without it."
            )
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
        "time, approval waits excluded; the baseline has no analysis step."
    )
    parts += [note, ""]
    path.write_text("\n".join(parts))
