"""LLM_MODE=mock: a deterministic scripted chat model that emits real tool calls.

Each role (commander, triage, drone-pilot, evidence) has a small policy: a pure function of the
conversation so far that returns the next tool calls or a final answer. It follows the
02:14 -> 06:00 walkthrough, so the whole deepagents flow (sub-agents, files, interrupts,
checkpointed wake-ups) runs offline and costs nothing. It is not a judge of anything: the
real judgment comes from Ollama or Claude.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agent.prompts import INVESTIGATION_MARKER, NO_DRONE_MARKER
from common.deployment import NO_AERIAL

THREATS = ("chainsaw", "axe", "gunshot")
_REC = re.compile(r"\[([^\[\]#]+?) #(\d+)\] (\d\d:\d\d:\d\d) (\w+) ([\d.]+)")


# ---------- reading the conversation ----------


@dataclass
class Call:
    key: str  # tool name, or "task:<subagent>"
    name: str
    args: dict
    result: str | None


@dataclass
class Brief:
    """Fields parsed from the wake-up message or a task description (both are code-written)."""

    text: str
    fields: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key.lower(), default)

    @property
    def kinds(self) -> str:
        return self.get("wake-up")

    @property
    def records(self) -> list[dict]:
        return parse_records(self.text)


def parse_brief(text: str) -> Brief:
    fields = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z][A-Za-z -]{1,20}):\s*(.*)$", line.strip())
        m = m or re.match(r"^(Wake-up):\s*(.*)$", line.strip())
        if m:
            fields.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    return Brief(text=text, fields=fields)


def parse_records(text: str) -> list[dict]:
    return [
        {"node": m.group(1).strip(), "seq": int(m.group(2)), "time": m.group(3), "sound": m.group(4),
         "confidence": float(m.group(5))}
        for m in _REC.finditer(text or "")
    ]


def _text(msg: BaseMessage) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)


def last_human(messages: list[BaseMessage]) -> tuple[int, Brief]:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return i, parse_brief(_text(messages[i]))
    return -1, parse_brief("")


def calls_since(messages: list[BaseMessage], start: int) -> list[Call]:
    results = {m.tool_call_id: _text(m) for m in messages[start:] if isinstance(m, ToolMessage)}
    out = []
    for m in messages[start:]:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                key = tc["name"]
                if key == "task":
                    key = f"task:{tc['args'].get('subagent_type')}"
                out.append(Call(key, tc["name"], tc["args"], results.get(tc["id"])))
    return out


class Done:
    def __init__(self, calls: list[Call]) -> None:
        self.calls = calls

    assumed: frozenset[str] = frozenset()  # steps the current mode leaves out, treated as done

    def has(self, key: str) -> bool:
        return key in self.assumed or any(c.key == key for c in self.calls)

    def count(self, key: str) -> int:
        return sum(c.key == key for c in self.calls)

    def result(self, key: str) -> str:
        found = [c.result for c in self.calls if c.key == key and c.result is not None]
        return found[-1] if found else ""

    def results(self, key: str) -> list[Call]:
        return [c for c in self.calls if c.key == key]


def tc(name: str, **args: Any) -> tuple[str, dict]:
    return name, args


def task(subagent: str, description: str) -> tuple[str, dict]:
    return "task", {"description": description, "subagent_type": subagent}


# ---------- commander ----------


def _plan(w: Brief, steps: list[tuple[bool, str]], note: str = "") -> tuple[str, dict]:
    lines = [f"# Plan: {w.get('incident')}", "", f"Sector {w.get('sector')} · {w.get('family')}", ""]
    lines += [f"- [{'x' if done else ' '}] {step}" for done, step in steps]
    if note:
        lines += ["", note]
    return tc("write_file", file_path=w.get("folder") + "plan.md", content="\n".join(lines) + "\n")


def _brief_for(w: Brief, ask: str) -> str:
    recs = "\n".join(
        f"- [{r['node']} #{r['seq']}] {r['time']} {r['sound']} {r['confidence']:.2f}" for r in w.records
    )
    return (
        f"Incident: {w.get('incident')}\nSector: {w.get('sector')}\nFamily: {w.get('family')}\n"
        f"Folder: {w.get('folder')}\nNew records:\n{recs}\n\n{ask}"
    )


@dataclass
class Verdict:
    verdict: str = "uncertain"
    confidence: float = 0.5
    reasons: list[str] = field(default_factory=list)


def parse_verdict(text: str) -> Verdict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    return Verdict(
        verdict=str(data.get("verdict", "uncertain")),
        confidence=float(data.get("confidence", 0.5)),
        reasons=[str(r) for r in data.get("reasons", [])],
    )


def _no_drone(messages: list[BaseMessage]) -> bool:
    return any(isinstance(m, SystemMessage) and NO_DRONE_MARKER in str(m.content) for m in messages)


def commander(messages: list[BaseMessage]) -> list[tuple[str, dict]] | str:
    start, w = last_human(messages)
    d = Done(calls_since(messages, start + 1))
    if not any(isinstance(m, SystemMessage) and INVESTIGATION_MARKER in str(m.content) for m in messages):
        d.assumed = frozenset({"compare_response_options", "record_assessment"})  # light mode
    kinds, status = w.kinds, w.get("status")
    if "ranger message" in kinds:
        return _ranger_update(w, d, _no_drone(messages))
    if "new detections" in kinds and any(x in w.get("latest ranger update") for x in ('"authorized_activity"', '"false_alarm"')):
        return _ranger_update(w, d, _no_drone(messages))
    if "new detections" in kinds:
        return _follow_up(w, d) if status == "dispatched" else _triage_flow(w, d, _no_drone(messages))
    if "scheduled check" in kinds:
        if status == "dispatched":
            return _dispatched_check(w, d)
        if status in ("watching", "open"):
            return _watch_check(w, d)
        return f"Check on {w.get('incident')}: nothing to do (status {status})."
    if "ranger message" in kinds:
        return f"Noted the ranger's message for {w.get('incident')}."
    return "No action needed."


def _triage_flow(w: Brief, d: Done, no_drone: bool = False, ranger_confirmed: bool = False) -> list[tuple[str, dict]] | str:
    inc, sector, family = w.get("incident"), w.get("sector"), w.get("family")
    if d.count("write_file") == 0:
        return [_plan(w, [(False, "Triage: real or false alarm?"), (False, "Follow up if real")])]
    if not d.has("task:triage"):
        return [task("triage", _brief_for(w, "Is this real? Write triage.md and reply with the JSON verdict."))]
    v = parse_verdict(d.result("task:triage"))
    if ranger_confirmed:
        v = Verdict("real", .9, ["Ranger reports confirmed threat; independent human report"])
    reason = "; ".join(v.reasons[:2]) or v.verdict
    if v.verdict == "false_alarm":
        if not d.has("set_status"):
            calls = [tc("set_status", status="dismissed", reason=f"False alarm: {reason}")]
            nodes = {r["node"] for r in w.records}
            if len(nodes) == 1 and not any("history" in r for r in v.reasons):
                calls.append(tc("remember", topic="nodes", note=f"{nodes.pop()}: false alarms during rain"))
            calls.append(_plan(w, [(True, "Triage"), (True, "Dismissed")], f"Dismissed: {reason}"))
            return calls
        return f"Dismissed {inc}: {reason}"
    if v.verdict in ("real", "investigate") and family in ("logging", "poaching"):
        if not d.has("compare_response_options"):
            return [tc("compare_response_options")]
        if no_drone and v.verdict == "investigate":
            return _uncertain_response(w, d, "Rain interference or real activity; no aerial verification available")
        if no_drone:
            if not d.has("record_assessment"):
                return [_assessment("Strong acoustic threat", "Authorized activity or classification error",
                                    "propose_dispatch", "Request human verification; no aerial verification available")]
            if not d.has("dispatch_rangers"):
                nodes = len({r["node"] for r in w.records})
                what = "logging activity requiring ranger assessment" if family == "logging" else "possible gunshot activity"
                note = f"{NO_AERIAL} Likely {what} in sector {sector}. {nodes} nodes agree."
                return [
                    _plan(w, [(True, "Triage: likely real"), (False, "Ask ranger to verify or dispatch")]),
                    tc("dispatch_rangers", sector=sector, note=note[:480]),
                ]
        elif not d.has("task:drone-pilot"):
            return [
                _plan(w, [(True, "Triage: likely real"), (False, "Drone check"), (False, "Ask ranger to dispatch")]),
                task("drone-pilot", f"Incident: {inc}\nFly to sector {sector} and report what the camera sees."),
            ]
        seen = d.result("task:drone-pilot")
        lower = seen.lower()
        unavailable = any(x in lower for x in ("could not fly", "not flown", "not completed"))
        contradiction = any(x in lower for x in ("no activity", "no people", "no evidence", "inconclusive", "empty", "authorized", "only wildlife", "nothing seen"))
        if contradiction or (unavailable and v.verdict == "investigate"):
            return _uncertain_response(w, d, "Camera did not corroborate the acoustic reports: " + seen)
        if not d.has("record_assessment"):
            reason = ("No aerial verification available; request ranger assessment based on strong acoustic reports. " + seen
                      if unavailable else "Camera report and acoustic evidence warrant human review. " + seen)
            return [_assessment("Possible threat", "Authorized activity or classification error", "propose_dispatch", reason)]
        if not d.has("dispatch_rangers"):
            seen = d.result("task:drone-pilot")
            m = re.search(r"Camera saw: (.*?)\.? Flew", seen)
            drone = ("no aerial verification; " + seen if unavailable else
                     f"drone saw {m.group(1).rstrip('.').lower()}" if m else f"drone: {seen}")
            nodes = len({r["node"] for r in w.records})
            what = "logging activity requiring ranger assessment" if family == "logging" else "possible gunshot activity"
            note = f"Likely {what} in sector {sector}. {nodes} nodes agree; {drone}."
            return [tc("dispatch_rangers", sector=sector, note=note[:480])]
        res = d.result("dispatch_rangers")
        if "Rangers dispatched" in res:
            if not d.has("schedule_check"):
                return [
                    tc("schedule_check", incident_id=inc, minutes=30),
                    _plan(w, [(True, "Triage"), (True, "Drone check"), (True, "Rangers dispatched"),
                              (False, "Re-check in 30 min"), (False, "Case file")]),
                ]
            return f"Rangers dispatched to {sector}. Re-check in 30 min."
        if not d.has("set_status"):
            return [
                tc("set_status", status="watching", reason="Ranger did not approve dispatch"),
                tc("schedule_check", incident_id=inc, minutes=30),
            ]
        return "Dispatch not approved. Watching; re-check in 30 min."
    if not d.has("set_status"):
        minutes = 20 if family == "vehicle" else 10
        why = "Vehicle only, no logging sounds" if family == "vehicle" else f"Uncertain: {reason}"
        return [
            tc("set_status", status="watching", reason=why),
            tc("schedule_check", incident_id=inc, minutes=minutes),
            _plan(w, [(True, "Triage"), (False, f"Re-check in {minutes} min")], why),
        ]
    return f"Watching {inc}."


def _assessment(explanation, alternative, action, reason):
    return tc("record_assessment", explanation=explanation, alternative=alternative,
              next_action=action, reason=reason)


def _uncertain_response(w, d, reason):
    limitation = reason + ". Limited camera coverage does not prove absence of activity."
    if not d.has("record_assessment"):
        return [_assessment("Threat remains uncertain", "Weather interference or missed camera coverage",
                            "request_ranger_assessment", limitation)]
    if not d.has("set_status"):
        return [tc("set_status", status="watching", reason=limitation),
                tc("schedule_check", incident_id=w.get("incident"), minutes=10),
                _plan(w, [(True, "Reassessed conflicting or unavailable evidence"),
                          (False, "Request ranger assessment; recheck in 10 min")], limitation)]
    return "Watching; ranger assessment requested and recheck scheduled. " + limitation


def _ranger_update(w, d, no_drone=False):
    try:
        update = json.loads(w.get("latest ranger update", "{}"))
    except ValueError:
        update = {}
    outcome = update.get("outcome")
    if not d.has("compare_response_options"):
        return [tc("compare_response_options")]
    if outcome in ("authorized_activity", "false_alarm"):
        reason = "Ranger reports " + outcome.replace("_", " ") + ": " + update.get("text", "")
        if not d.has("record_assessment"):
            return [_assessment(reason, "Earlier acoustic threat interpretation", "dismiss", "Ranger correction supersedes the earlier interpretation; prior dispatches cannot be recalled.")]
        if not d.has("set_status"):
            context = json.loads(d.result("compare_response_options") or "{}")
            return [tc("set_status", status="watching" if context.get("already_dispatched") else "dismissed", reason=reason),
                    _plan(w, [(True, "Revised assessment from ranger correction")], reason + ". Any prior dispatch remains recorded; no automatic recall.")]
        return reason
    if outcome == "confirmed_threat":
        return _triage_flow(w, d, no_drone, ranger_confirmed=True)
    return _uncertain_response(w, d, "Ranger supplied new information requiring verification: " + update.get("text", ""))


def _follow_up(w: Brief, d: Done) -> list[tuple[str, dict]] | str:
    if not d.has("get_detections"):
        return [tc("get_detections", sector=w.get("sector"), window_s=1800)]
    if d.count("write_file") == 0:
        heard = ", ".join(f"[{r['node']} #{r['seq']}] {r['sound']} {r['time']}" for r in w.records)
        note = f"Follow-up after dispatch: {heard}. Suspects probably leaving."
        return [
            _plan(w, [(True, "Triage"), (True, "Drone check"), (True, "Rangers dispatched"),
                      (False, "Re-check in 30 min"), (False, "Case file")], note),
            tc("schedule_check", incident_id=w.get("incident"), minutes=30),
        ]
    return "Logged follow-up sounds; re-check in 30 min."


def _dispatched_check(w: Brief, d: Done) -> list[tuple[str, dict]] | str:
    inc = w.get("incident")
    if not d.has("get_detections"):
        return [tc("get_detections", sector=w.get("sector"), window_s=1200)]
    if parse_records(d.result("get_detections")):
        if not d.has("schedule_check"):
            return [tc("schedule_check", incident_id=inc, minutes=30)]
        return "Area still active; re-check in 30 min."
    if not d.has("task:evidence"):
        return [task("evidence", f"Incident: {inc}\nSector: {w.get('sector')}\nFolder: {w.get('folder')}\n"
                                 "Area is quiet. Verify every record and write case_file.md.")]
    if not d.has("set_status"):
        return [
            tc("set_status", status="closed", reason="Area quiet; case file written"),
            _plan(w, [(True, "Triage"), (True, "Drone check"), (True, "Rangers dispatched"),
                      (True, "Area quiet"), (True, "Case file")]),
        ]
    return f"Closed {inc}: {d.result('task:evidence')}"


def _watch_check(w: Brief, d: Done) -> list[tuple[str, dict]] | str:
    if not d.has("get_detections"):
        return [tc("get_detections", sector=w.get("sector"), window_s=1800)]
    threats = [r for r in parse_records(d.result("get_detections")) if r["sound"] in THREATS]
    if threats:
        if not d.has("schedule_check"):
            return [tc("schedule_check", incident_id=w.get("incident"), minutes=15)]
        return "Threat sounds continue; still watching."
    if not d.has("set_status"):
        return [tc("set_status", status="dismissed", reason="No follow-up sounds; nothing to act on")]
    return "Dismissed: no follow-up sounds."


# ---------- triage ----------


def _judge(sector: str, dets: list[dict], weather: str, infos: list[dict]) -> dict:
    nodes = sorted({r["node"] for r in dets})
    rain = "raining" in weather
    noisy = [i["node"] for i in infos if i.get("memory") or i.get("dismissed_incidents")]
    sound = dets[0]["sound"] if dets else "sound"
    tags = " ".join(f"[{r['node']} #{r['seq']}]" for r in dets[:6])
    if rain:
        strong = [r for r in dets if r["sound"] in THREATS and r["confidence"] >= .85]
        def seconds(r):
            h, m, sec = map(int, r["time"].split(":"))
            return h * 3600 + m * 60 + sec
        compatible = any(a["node"] != b["node"] and a["sound"] == b["sound"]
                         and abs(seconds(a) - seconds(b)) <= 120 for a in strong for b in strong)
        if compatible:
            return {"verdict": "investigate", "confidence": .65, "location_estimate": sector,
                    "reasons": ["Strong compatible reports from independent nodes during rain " + tags,
                                "Rain interference and real activity remain plausible; seek camera or ranger assessment"]}
    if rain and (len(nodes) <= 1 or set(nodes) <= set(noisy)):
        reasons = [f"raining in {sector}", f"only {', '.join(nodes) or 'no node'} heard it {tags}".strip()]
        if noisy:
            reasons.append(f"{', '.join(noisy)} has a false-alarm history")
        return {"verdict": "false_alarm", "confidence": 0.85, "location_estimate": sector, "reasons": reasons}
    if len(nodes) >= 2:
        reasons = [f"{len(nodes)} nodes heard {sound}: {', '.join(nodes)} {tags}", "no rain" if not rain else "raining"]
        if not noisy:
            reasons.append("no false-alarm history")
        conf = min(0.95, 0.6 + 0.1 * len(nodes))
        return {"verdict": "real", "confidence": round(conf, 2), "location_estimate": sector, "reasons": reasons}
    return {"verdict": "uncertain", "confidence": 0.5, "location_estimate": sector,
            "reasons": [f"single node, {'rain' if rain else 'dry'} {tags}".strip()]}


def triage(messages: list[BaseMessage]) -> list[tuple[str, dict]] | str:
    start, b = last_human(messages)
    d = Done(calls_since(messages, start + 1))
    sector = b.get("sector")
    if not d.has("get_detections"):
        return [tc("get_detections", sector=sector, window_s=1800), tc("get_weather", sector=sector)]
    dets = parse_records(d.result("get_detections")) or b.records
    nodes = sorted({r["node"] for r in dets})
    if not d.has("get_node_info") and nodes:
        return [tc("get_node_info", node_name=n) for n in nodes]
    infos = []
    for c in d.results("get_node_info"):
        try:
            infos.append(json.loads(c.result or "{}"))
        except json.JSONDecodeError:
            pass
    verdict = _judge(sector, dets, d.result("get_weather"), infos)
    if not d.has("write_file"):
        head = f"**Verdict:** {verdict['verdict']} ({verdict['confidence']:.0%})"
        md = [f"# Triage: {b.get('incident')}", "", head, "", "## Reasons"]
        md += [f"- {r}" for r in verdict["reasons"]]
        return [tc("write_file", file_path=b.get("folder") + "triage.md", content="\n".join(md) + "\n")]
    return json.dumps(verdict)


# ---------- drone pilot ----------


def drone_pilot(messages: list[BaseMessage]) -> list[tuple[str, dict]] | str:
    start, b = last_human(messages)
    d = Done(calls_since(messages, start + 1))
    m = re.search(r"sector ([A-Z]\d+)", b.text)
    sector = m.group(1) if m else b.get("sector")
    if not d.has("plan_route"):
        return [tc("get_drone_status"), tc("plan_route", sector=sector)]
    try:
        route = json.loads(d.result("plan_route") or "{}")
    except json.JSONDecodeError:
        route = {}
    if route.get("feasible") and not d.has("fly_drone"):
        return [tc("fly_drone", sector=sector)]
    if d.has("fly_drone"):
        flown = d.result("fly_drone")
        m = re.search(r"Flew ([\d.]+) km to \w+\. Camera: (.*?) Battery now (\d+)%", flown)
        if not m:
            return flown
        return f"Camera saw: {m.group(2)} Flew {m.group(1)} km; battery now {m.group(3)}%."
    return f"Could not fly to {sector}: {route.get('reason') or 'route not feasible'}."


# ---------- evidence ----------


def _jsonl(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _case_file(b: Brief, rows: list[dict], valid: set[str], triage_md: str, drone_md: str,
               approvals: list[dict]) -> str:
    good = [r for r in rows if r["record"] in valid]
    bad = [r for r in rows if r["record"] not in valid]
    nodes = sorted({r["node"] for r in good})
    first = good[0] if good else None
    out = [f"# Case file: {b.get('incident')}", "", "## Summary"]
    if first:
        cites = " ".join(r["record"] for r in good)
        out.append(f"{first['sound'].capitalize()} heard by {len(nodes)} node(s) near sector "
                   f"{b.get('sector')} on {first['time'][:10]}. Records: {cites}")
    out += ["", "## Timeline"]
    out += [f"- {r['time'][11:19]} {r['record']} {r['sound']}, confidence {r['confidence']:.2f}" for r in good]
    out += ["", "## Nodes"]
    for n in nodes:
        recs = " ".join(r["record"] for r in good if r["node"] == n)
        sector = next(r["sector"] for r in good if r["node"] == n)
        out.append(f"- {n}, sector {sector}: {recs}")
    out += ["", "## Audio clip hashes"]
    out += [f"- {r['record']} {r['clip_hash']}" for r in good]
    out += ["", "## Drone observations"]
    obs = [ln.strip("-# ").strip() for ln in drone_md.splitlines() if ln.strip().startswith(("- camera", "## "))]
    out += [f"- {o}" for o in obs] or ["- No drone flight."]
    out.append("- Camera coverage is limited; observations alone do not establish absence or legality.")
    out += ["", "## Approval record"]
    if approvals:
        out += [f"- {a.get('time')} {a.get('decision')} by {a.get('by', 'ranger')}: {a.get('note', '')}".rstrip(": ")
                for a in approvals]
    else:
        out.append("- No dispatch approval on file.")
    verified = f"{len(good)} of {len(rows)} records re-verified: Ed25519 signature valid, record unaltered."
    out += ["", "## Integrity", verified]
    if bad:
        out.append("Failed verification, not used as evidence: " + ", ".join(r["record"].strip("[]") for r in bad))
    if "Verdict" in triage_md:
        out += ["", "Triage verdict on file in triage.md."]
    return "\n".join(out) + "\n"


def evidence(messages: list[BaseMessage]) -> list[tuple[str, dict]] | str:
    start, b = last_human(messages)
    d = Done(calls_since(messages, start + 1))
    folder = b.get("folder")
    reads = d.results("read_file")
    if not reads:
        return [tc("read_file", file_path=folder + "detections.jsonl")]
    rows = _jsonl(reads[0].result or "")
    if not d.has("verify_signature") and rows:
        return [tc("verify_signature", node=r["node"], seq=r["seq"]) for r in rows]
    if len(reads) == 1:
        return [tc("read_file", file_path=folder + f) for f in ("triage.md", "drone_log.md", "approvals.jsonl")]
    valid = set()
    for c in d.results("verify_signature"):
        if c.result and "signature valid" in c.result:
            valid.add(f"[{c.args['node']} #{c.args['seq']}]")
    by_path = {c.args.get("file_path", ""): c.result or "" for c in reads}
    if not d.has("write_file"):
        md = _case_file(
            b, rows, valid,
            by_path.get(folder + "triage.md", ""),
            by_path.get(folder + "drone_log.md", ""),
            _jsonl(by_path.get(folder + "approvals.jsonl", "")),
        )
        return [tc("write_file", file_path=folder + "case_file.md", content=md)]
    if not d.has("remember"):
        return [tc("remember", topic="sectors", note=f"{b.get('sector')}: incident {b.get('incident')} closed "
                                                      "with a verified case file")]
    return f"{len(valid)} of {len(rows)} records verified; case_file.md written."


POLICIES = {"commander": commander, "triage": triage, "drone-pilot": drone_pilot, "evidence": evidence}


# ---------- the chat model ----------


class MockChatModel(BaseChatModel):
    """Scripted model for one role. Raises if its script calls a tool it was not given."""

    role: str
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "forest-mock"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> dict:
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = "forest-mock"
        params["ls_model_name"] = f"mock-{self.role}"
        return params

    def bind_tools(self, tools: Any, **kwargs: Any) -> MockChatModel:
        names = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in tools]
        return self.model_copy(update={"bound_tools": [n for n in names if n]})

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None,
                  **kwargs: Any) -> ChatResult:
        out = POLICIES[self.role](messages)
        if isinstance(out, str):
            msg = AIMessage(content=out)
        else:
            turn = sum(isinstance(m, AIMessage) for m in messages)
            calls = []
            for i, (name, args) in enumerate(out):
                if self.bound_tools and name not in self.bound_tools:
                    raise ValueError(f"mock {self.role} script called unbound tool {name}")
                calls.append({"name": name, "args": args, "id": f"{self.role}-{turn}-{i}", "type": "tool_call"})
            msg = AIMessage(content="", tool_calls=calls)
        return ChatResult(generations=[ChatGeneration(message=msg)])
