"""Scoring for the evaluation (SPEC 3.9). Pure functions: no station, no network.

Ground truth comes from the scenario file: an event with label "real" belongs to a real incident
(its `incident` name); everything else (false_alarm, benign) is not worth sending rangers for.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from statistics import mean
from typing import Any

from agent.baseline import baseline_alerts
from common.contracts import Detection, SoundClass, World
from sim.scenario import ScenarioEvent

MATCH_WINDOW_S = 120  # a record belongs to the nearest scenario event on its node within this
NOISY_NODE = "Low Marsh"  # rains false alarms all night in both scenarios
DISPATCHED = ("approved", "edited")


def real_groups(events: Iterable[ScenarioEvent]) -> list[str]:
    return sorted({e.incident or e.source for e in events if e.label == "real"})


def match_event(
    events: Sequence[ScenarioEvent], start: float, node: str, timestamp: float
) -> ScenarioEvent | None:
    """The scenario event that produced a record: same node, nearest in time."""
    near = [(abs(start + e.at - timestamp), e) for e in events if e.node == node]
    near = [(gap, e) for gap, e in near if gap <= MATCH_WINDOW_S]
    return min(near, key=lambda p: p[0])[1] if near else None


def group_of(event: ScenarioEvent | None) -> str | None:
    return (event.incident or event.source) if event is not None and event.label == "real" else None


def to_detection(row: dict[str, Any]) -> Detection:
    """A row of GET /api/detections back into a Detection."""
    return Detection(
        node_id=row["node_id"], seq=row["seq"], timestamp=row["timestamp"], lat=row["lat"],
        lon=row["lon"], sound=SoundClass[row["sound"].upper()], confidence=row["confidence"],
        model_version=row["model_version"], clip_hash=row["clip_hash"],
        signature=row.get("signature", ""), verified=bool(row["verified"]),
    )  # fmt: skip


def score_baseline(
    rows: Sequence[dict[str, Any]], events: Sequence[ScenarioEvent], start: float, world: World
) -> dict[str, Any]:
    """One alert per verified detection >= 0.8, each judged against the scenario truth."""
    alerts = baseline_alerts([to_detection(r) for r in rows], world)
    groups = real_groups(events)
    seen: set[str] = set()
    false_alerts = 0
    for a in alerts:
        group = group_of(match_event(events, start, a.node, a.timestamp))
        if group:
            seen.add(group)
        else:
            false_alerts += 1
    return {
        "alerts": len(alerts),
        "real_caught": len(seen),
        "real_missed": len(groups) - len(seen),
        "false_alerts": false_alerts,
        "caught": sorted(seen),
    }


def incident_groups(view: dict, truth: dict[tuple[str, int], str | None]) -> set[str]:
    """Real incidents (scenario names) that an incident's records belong to."""
    return {g for r in view["records"] if (g := truth.get((r["node"], r["seq"])))}


def score_commander(
    views: Sequence[dict], rows: Sequence[dict[str, Any]], events: Sequence[ScenarioEvent], start: float
) -> dict[str, Any]:
    """Incidents the commander opened and the approvals it put in front of rangers.

    A request is false when the stand-in ranger (eval.run) rejected it: nothing real in the incident then."""
    truth = {(r["node"], r["seq"]): group_of(match_event(events, start, r["node"], r["timestamp"])) for r in rows}
    groups = real_groups(events)
    detected = {g for g in truth.values() if g}
    caught: set[str] = set()
    requests = false_requests = false_dispatches = 0
    per_incident = []
    for v in views:
        g = incident_groups(v, truth)
        approvals = [a for a in v["approvals"] if a["status"] != "stale"]  # stale: superseded, never shown
        sent = [a for a in approvals if a["status"] in DISPATCHED]
        requests += len(approvals)
        false_requests += sum(a["status"] == "rejected" for a in approvals)  # the stand-in ranger said no
        if not g:
            false_dispatches += len(sent)
        if sent:
            caught |= g
        stats = v.get("stats") or {}
        steps = stats.get("steps", {})
        per_incident.append({
            "id": v["incident"]["id"],
            "nodes": v["incident"]["nodes"],
            "real": sorted(g),
            "status": v["incident"]["status"],
            "approvals": len(approvals),
            "dispatched": bool(sent),
            "wall_s": stats.get("wall_s", 0.0),
            "steps": sum(steps.values()),
            "triage_steps": steps.get("triage", 0),
        })  # fmt: skip
    walls = [p["wall_s"] for p in per_incident]
    real_walls = [p["wall_s"] for p in per_incident if p["real"]]
    return {
        "incidents": len(per_incident),
        "requests": requests,
        "real_detected": len(detected),
        "real_caught": len(caught),
        "real_missed": len(groups) - len(caught),
        "false_requests": false_requests,
        "false_dispatches": false_dispatches,
        "caught": sorted(caught),
        "triage_steps_total": sum(p["triage_steps"] for p in per_incident),
        "steps_total": sum(p["steps"] for p in per_incident),
        "wall_s_total": round(sum(walls), 1),
        "wall_s_per_incident": round(mean(walls), 1) if walls else 0.0,
        "wall_s_per_real_incident": round(mean(real_walls), 1) if real_walls else 0.0,
        "noisy_node": noisy_node(per_incident),
        "per_incident": per_incident,
    }


def noisy_node(per_incident: Sequence[dict], node: str = NOISY_NODE) -> dict[str, Any]:
    """Effort on incidents only the noisy node heard (the memory test)."""
    mine = [p for p in per_incident if p["nodes"] == [node]]
    if not mine:
        return {"node": node, "incidents": 0, "triage_steps": 0.0, "steps": 0.0, "wall_s": 0.0}
    return {
        "node": node,
        "incidents": len(mine),
        "triage_steps": round(mean(p["triage_steps"] for p in mine), 2),
        "steps": round(mean(p["steps"] for p in mine), 2),
        "wall_s": round(mean(p["wall_s"] for p in mine), 1),
    }


def want_dispatch(view: dict, truth: dict[tuple[str, int], str | None]) -> bool:
    """Eval's stand-in for a ranger: approve real incidents, reject the rest."""
    return bool(incident_groups(view, truth))
