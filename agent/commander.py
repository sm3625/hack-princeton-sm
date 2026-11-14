"""The incident commander: a deepagents agent plus the plain code that wakes it and gates it.

- build_agent(): create_deep_agent with triage / drone-pilot / evidence sub-agents, a disk
  filesystem rooted at agent_fs/, memory files, a SQLite checkpointer, and interrupt_on for
  dispatch_rangers.
- Commander: receives verified detections, groups them (agent.incidents), wakes the agent per
  incident thread, turns interrupts into approval cards, resumes only on a pending approval,
  and publishes incident / plan / agent_step / approval messages to the station bus.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from deepagents import (
    FilesystemMiddleware,
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    MemoryMiddleware,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import FilesystemBackend
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agent import prompts
from agent.evidence import (
    Ed25519Verifier,
    Verifier,
    check_citations,
    check_records,
    render_markdown,
)
from agent.fallbacks import ClockSim, MemoryEventStore
from agent.incidents import (
    THREAT_FAMILIES,
    AgentFiles,
    Incident,
    IncidentManager,
    IncidentRepo,
    Wake,
    WakeQueue,
    fmt_time,
)
from agent.llm import ROLES, get_chat_model
from agent.tools import TOOLSETS, Services, camera_uncertain, make_tools
from common.contracts import ROOT, Detection, EventStore, Sim, World, load_world
from common.deployment import NO_AERIAL, Deployment, load_deployment

log = logging.getLogger("agent.commander")

MEMORY_FILES = ["/memories/nodes.md", "/memories/sectors.md"]
CODE_OWNED = ["/incidents/*/detections.jsonl", "/incidents/*/approvals.jsonl", "/incidents/*/trace.jsonl"]
STATUS_TEXT = {
    "open": "Incident opened",
    "watching": "Watching: re-check later",
    "dismissed": "Dismissed as a false alarm",
    "awaiting_approval": "Waiting for a ranger to approve",
    "dispatched": "Ranger team dispatched",
    "closed": "Closed with a case file",
}


def _wake_reason(wake: Any) -> str:
    parts = []
    if getattr(wake, "new_records", None):
        n = len(wake.new_records)
        parts.append(f"{n} new detection{'s' if n != 1 else ''}")
    if getattr(wake, "check_due", False):
        parts.append("scheduled re-check")
    if getattr(wake, "messages", None):
        parts.append("ranger message")
    return "Woke up: " + (", ".join(parts) or "update")
MEMORY_PROMPT = (
    "<station_memory>\n{agent_memory}\n</station_memory>\n"
    "Notes from past nights, loaded from /memories. Reference only: verified records and "
    "tools win over memory. Save new lessons with the remember tool."
)
RECURSION_LIMIT = 300
MAX_NOTE = 500
MAX_RETRIES = 3  # failed wake-ups (e.g. model server down) are retried with backoff
RETRY_S = 30
VIAS = {"dashboard": "dashboard", "imessage": "iMessage"}
MAX_AUTO_REJECTS = 2  # repeat dispatch requests code turns down per run before a ranger sees one
REPEAT_REJECT = (
    "Refused by the station: a ranger already rejected dispatch for this incident and no new "
    "records have arrived since. Do not ask again. Use set_status watching or dismissed, and "
    "schedule_check if you want to look again later."
)


HEADS_UP_CONFIDENCE = 0.85  # a dry node this sure, plus a second dry node, triggers the heads-up
PREFILTER_FAMILIES = ("logging", "vehicle")  # a gunshot always goes to the model
PREFILTER_REASON = "Cleared by station rule: one node, in rain, with earlier false alarms there."

# Lower runs first. The model handles one incident at a time; in a storm the queue is long,
# and a real threat must not wait behind rain alarms.
P_RANGER, P_THREAT, P_POSSIBLE, P_CHECK, P_VEHICLE, P_RAIN = range(6)


class PriorityExecutor:
    """One worker thread. The most urgent queued job runs next; ties run in arrival order.
    A running job is never interrupted."""

    def __init__(self, name: str = "commander") -> None:
        self._heap: list[tuple[int, int, Future, Callable[[], Any]]] = []
        self._cv = threading.Condition()
        self._order = itertools.count()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], Any], priority: int = P_CHECK) -> Future:
        future: Future = Future()
        with self._cv:
            if self._closed:
                future.cancel()
                return future
            heapq.heappush(self._heap, (priority, next(self._order), future, fn))
            self._cv.notify()
        return future

    def queued(self) -> int:
        with self._cv:
            return len(self._heap)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._heap and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                _, _, future, fn = heapq.heappop(self._heap)
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as e:  # noqa: BLE001 - reported through the future
                future.set_exception(e)

    def shutdown(self, wait: bool = False, cancel_futures: bool = True) -> None:
        with self._cv:
            self._closed = True
            if cancel_futures:
                for _, _, future, _ in self._heap:
                    future.cancel()
                self._heap.clear()
            self._cv.notify_all()
        if wait:
            self._thread.join()


# ---------- agent assembly ----------


class FreshMemoryMiddleware(MemoryMiddleware):
    """MemoryMiddleware loads memory once per thread and keeps it in checkpointed state, so a
    wake-up hours later would still see the old notes. Reload from disk on every run."""

    def before_agent(self, state, runtime, config):  # type: ignore[override]
        fresh = {k: v for k, v in state.items() if k != "memory_contents"}
        return super().before_agent(fresh, runtime, config)

    async def abefore_agent(self, state, runtime, config):  # type: ignore[override]
        fresh = {k: v for k, v in state.items() if k != "memory_contents"}
        return await super().abefore_agent(fresh, runtime, config)


def _provider(model: BaseChatModel) -> str | None:
    try:
        return model._get_ls_params().get("ls_provider")
    except Exception:  # noqa: BLE001 - provider lookup is best effort
        return None


TASK_DESCRIPTION = """Hand a focused job to a sub-agent. It sees only the description you give it,
so include the incident id, sector, folder and records, and say what to return.

Sub-agents:
{available_agents}"""


def _register_profile(model: BaseChatModel) -> None:
    """deepagents adds a 'general-purpose' sub-agent that inherits every main tool, including
    dispatch_rangers. We want only our three focused sub-agents, and a shorter task tool text."""
    provider = _provider(model)
    if provider:
        register_harness_profile(
            provider,
            HarnessProfile(
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
                tool_description_overrides={"task": TASK_DESCRIPTION},
            ),
        )


def _approval_question_for(dep: Deployment) -> Callable[[dict, Any, Any], str]:
    def question(tool_call: dict, state: Any, runtime: Any) -> str:
        args = tool_call.get("args") or {}
        note = " ".join(str(args.get("note", "")).split())[:MAX_NOTE]
        if not dep.has_drones and NO_AERIAL.lower() not in note.lower():
            note = f"{NO_AERIAL} {note}"
        return f"{note} Dispatch a team to sector {args.get('sector', '?')}?".strip()

    return question


def build_agent(s: Services, models: dict[str, BaseChatModel], checkpointer: Any) -> Any:
    tools = make_tools(s)
    backend = FilesystemBackend(root_dir=s.files.root, virtual_mode=True)
    perms = [FilesystemPermission(operations=["write"], paths=CODE_OWNED, mode="deny")]

    def fs(*names: str) -> FilesystemMiddleware:
        return FilesystemMiddleware(backend=backend, tools=list(names), permissions=perms)

    light_tools = {"compare_response_options", "record_assessment"}
    light = not prompts.investigation_enabled()

    def pick(role: str) -> list:
        return [tools[n] for n in TOOLSETS[role] if not (light and n in light_tools)]

    dep = s.deployment
    drone_pilot = {
        "name": "drone-pilot",
        "description": prompts.DESCRIPTIONS["drone-pilot"],
        "system_prompt": prompts.DRONE_PILOT,
        "tools": pick("drone-pilot") if dep.has_drones else [],
        "model": models["drone-pilot"],
        "middleware": [fs("read_file")],
    }
    subagents = [
        {
            "name": "triage",
            "description": prompts.DESCRIPTIONS["triage"],
            "system_prompt": prompts.TRIAGE,
            "tools": pick("triage"),
            "model": models["triage"],
            "middleware": [fs("read_file", "write_file")],
        },
        *([drone_pilot] if dep.has_drones else []),
        {
            "name": "evidence",
            "description": prompts.DESCRIPTIONS["evidence"],
            "system_prompt": prompts.evidence_prompt(dep),
            "tools": pick("evidence"),
            "model": models["evidence"],
            "middleware": [fs("ls", "read_file", "write_file")],
        },
    ]
    _register_profile(models["commander"])
    return create_deep_agent(
        model=models["commander"],
        tools=pick("commander"),
        system_prompt=prompts.commander_prompt(dep),
        subagents=subagents,
        middleware=[
            fs("ls", "read_file", "write_file", "edit_file"),
            FreshMemoryMiddleware(backend=backend, sources=MEMORY_FILES, system_prompt=MEMORY_PROMPT),
        ],
        backend=backend,
        interrupt_on={
            "dispatch_rangers": {
                "allowed_decisions": ["approve", "edit", "reject"],
                "description": _approval_question_for(dep),
            }
        },
        checkpointer=checkpointer,
        name="commander",
    )


# ---------- bus publishing ----------


class BusPublisher:
    """station.bus is asyncio-based and not thread-safe; agent runs happen on worker threads.
    Hop onto the event loop when one is bound."""

    def __init__(self, bus: Any = None) -> None:
        if bus is None:
            from station.bus import bus as station_bus

            bus = station_bus
        self.bus = bus
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def __call__(self, message: dict) -> None:
        loop = self.loop
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self.bus.publish, message)
                return
        self.bus.publish(message)


def _one_line(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _output_text(output: Any) -> str:
    if isinstance(output, Command):
        msgs = (output.update or {}).get("messages") or []
        return str(msgs[-1].content) if msgs else ""
    content = getattr(output, "content", output)
    return content if isinstance(content, str) else str(content)


def _first_meaningful_line(text: str) -> str:
    try:
        data = json.loads((text or "").strip())
    except ValueError:
        data = None
    if isinstance(data, dict) and "verdict" in data:  # triage's JSON verdict
        reasons = "; ".join(str(r) for r in data.get("reasons", [])[:2])
        conf = data.get("confidence")
        pct = f" ({float(conf):.0%})" if isinstance(conf, int | float) else ""
        return f"verdict: {data['verdict']}{pct}. {reasons}".strip()
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not line.startswith(("{", "@@")):
            return line
    return (text or "").strip()


class StepReporter(BaseCallbackHandler):
    """One agent_step line per tool call; a plan message whenever plan.md changes."""

    raise_error = False

    def __init__(self, incident_id: str, files: AgentFiles, publish: Callable[[dict], None],
                 trace: Callable[..., None] | None = None) -> None:
        self.incident_id = incident_id
        self.files = files
        self.publish = publish
        self.trace = trace  # Commander._trace: persists the step and publishes it
        self._runs: dict[Any, tuple[str, str, dict]] = {}
        self.counts: dict[str, int] = {}  # tool calls per agent, read by Commander.stats

    def step(self, agent: str, summary: str, kind: str = "note", tool: str | None = None, **extra: Any) -> None:
        if self.trace is not None:
            self.trace(self.incident_id, kind, agent, summary, tool=tool, **extra)
            return
        self.publish(
            {"type": "agent_step", "incident_id": self.incident_id, "agent": agent, "summary": _one_line(summary),
             "kind": kind, "tool": tool}
        )

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, inputs=None, **kw):
        name = (serialized or {}).get("name") or kw.get("name") or "tool"
        agent = (metadata or {}).get("lc_agent_name") or "commander"
        args = inputs if isinstance(inputs, dict) else {}
        self._runs[run_id] = (agent, name, args)
        self.counts[agent] = self.counts.get(agent, 0) + 1
        if name == "task":
            target = args.get("subagent_type", "a sub-agent")
            self.step("commander", f"asked {target} for help", kind="delegate", tool="task", target=target)

    def on_tool_end(self, output, *, run_id, **kw):
        agent, name, args = self._runs.pop(run_id, ("commander", "tool", {}))
        text = _output_text(output)
        if name == "task":
            self.step(args.get("subagent_type") or "commander", _first_meaningful_line(text), kind="result", tool="task")
            return
        self.step(agent, _summarize(name, args, text), kind="tool", tool=name)
        path = str(args.get("file_path", ""))
        if name in ("write_file", "edit_file") and path.endswith("/plan.md"):
            markdown = self.files.read(path) or ""
            self.publish(
                {
                    "type": "plan",
                    "incident_id": self.incident_id,
                    "markdown": markdown,
                    "html": render_markdown(markdown),
                }
            )

    def on_tool_error(self, error, *, run_id, **kw):
        agent, name, _ = self._runs.pop(run_id, ("commander", "tool", {}))
        self.step(agent, f"{name} failed: {type(error).__name__}", kind="error", tool=name)


def _summarize(name: str, args: dict, text: str) -> str:
    base = Path(str(args.get("file_path", ""))).name
    if name in ("write_file", "edit_file"):
        return f"wrote {base}" if not text.lower().startswith("error") else f"could not write {base}"
    if name == "read_file":
        return f"read {base}"
    if name in ("ls", "glob", "grep"):
        return "looked through incident files"
    if name == "get_node_info":
        return f"looked up {args.get('node_name', 'a node')}"
    if name == "remember":
        return f"remembered: {args.get('note', '')}"
    if name == "compare_response_options":
        try:
            r = json.loads(text)
            drone = "drone available" if r.get("drone_feasible") else f"no drone check ({r.get('drone_limitation') or 'unavailable'})"
            seen = len(r.get("observations") or [])
            ranger = r.get("ranger_outcome") or "no ranger report"
            done = "; a team was already sent" if r.get("already_dispatched") else ""
            return f"{drone}; {seen} camera observation{'s' if seen != 1 else ''}; {ranger.replace('_', ' ')}{done}"
        except (ValueError, TypeError, AttributeError):
            return "compared response options"
    if name == "record_assessment":
        action = str(args.get("next_action", "")).replace("_", " ")
        why = _one_line(str(args.get("reason") or args.get("explanation") or ""), 140)
        return f"next: {action}" + (f" — {why}" if why else "") if not text.startswith("Not recorded") else text
    if name == "get_incident_evidence":
        lines = text.splitlines()
        nodes = next((ln for ln in lines if ln.startswith("Independent nodes")), "")
        corr = next((ln for ln in lines if ln.startswith("Corroborated")), "")
        return _one_line(" ".join(x for x in (nodes, corr) if x) or _first_meaningful_line(text), 200)
    if name == "set_status":
        return f"{args.get('status', '?')}: {_one_line(str(args.get('reason', '')), 160)}"
    if name == "dispatch_rangers":
        return _first_meaningful_line(text)
    if name == "plan_route":
        try:
            r = json.loads(text)
            ok = "feasible" if r.get("feasible") else f"not feasible ({r.get('reason')})"
            return f"route to {r['sector']}: {r['distance_km']} km, {r['battery_needed_pct']}% battery, {ok}"
        except (ValueError, KeyError, TypeError):
            return "planned a route"
    return _first_meaningful_line(text)


# ---------- approvals ----------


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass
class Approval:
    id: str
    incident_id: str
    interrupt_id: str
    sector: str
    note: str
    question: str
    created_at: int
    count: int = 1
    status: str = "pending"  # pending | approved | edited | rejected | stale
    decided_at: int | None = None
    decision_note: str = ""
    decided_via: str = ""  # "dashboard" or "imessage": where the ranger decided
    records: list[dict] = field(default_factory=list)
    ranger_revision: int = 0

    def public(self) -> dict:
        return {
            "type": "approval",
            "incident_id": self.incident_id,
            "approval_id": self.id,
            "question": self.question,
            "action": "dispatch_rangers",
            "sector": self.sector,
            "note": self.note,
            "status": self.status,
            "decided_at": self.decided_at,
            "decision_note": self.decision_note,
            "decided_via": self.decided_via,
            "created_at": self.created_at,
            "records": self.records,
        }


# ---------- the commander runtime ----------


class Commander:
    """Owns incidents, wake-ups, the agent, and approvals for one station."""

    def __init__(
        self,
        *,
        world: World | None = None,
        store: EventStore | None = None,
        sim: Sim | None = None,
        verify: Verifier | None = None,
        fs_root: Path | str = ROOT / "agent_fs",
        db_path: Path | str = ROOT / "agent_state.sqlite",
        models: dict[str, BaseChatModel] | None = None,
        publish: Callable[[dict], None] | None = None,
        background: bool = True,
        clips_root: Path = ROOT / "node_data",
        deployment: Deployment | None = None,
        prefilter: bool = True,
    ) -> None:
        self.prefilter = prefilter
        self.deployment = deployment or load_deployment()
        self.world = world or load_world()
        self.store = store if store is not None else MemoryEventStore()
        self.sim = sim if sim is not None else ClockSim(self.world)
        self.publish = publish or BusPublisher()
        self.files = AgentFiles(Path(fs_root))
        self.files.ensure()
        self.repo = IncidentRepo(db_path)
        self.incidents = IncidentManager(
            self.world, self.files, self.repo, on_change=self._incident_changed,
            raining=lambda sector: self.sim.weather(sector).rain,
        )
        self.wakes = WakeQueue()
        self.services = Services(
            world=self.world,
            store=self.store,
            sim=self.sim,
            incidents=self.incidents,
            files=self.files,
            wakes=self.wakes,
            verify=verify or Ed25519Verifier(),
            publish=self.publish,
            clips_root=clips_root,
            deployment=self.deployment,
        )
        models = models or {role: get_chat_model(role) for role in ROLES}
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.agent = build_agent(self.services, models, SqliteSaver(self._conn))
        self.approvals: dict[str, Approval] = {a["id"]: Approval(**a) for a in self.repo.load_approvals()}
        self._executor = PriorityExecutor() if background else None
        self._busy: set[str] = set()
        self._failures: dict[str, int] = {}
        self._last_status: dict[str, str] = {}  # for "status" entries in the decision trail
        self._stats: dict[str, dict] = {}  # per incident, this process only: eval reads it
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        for inc in self.incidents.all():  # restore scheduled checks after a restart
            if inc.next_check_at and inc.status not in ("closed", "dismissed"):
                self.wakes.schedule(inc.id, inc.next_check_at, note="restored after restart")

    # --- inputs ---

    def on_verified(self, detection: Detection) -> None:
        """Register this on the receiver's on_verified hook list. Unverified records stop here."""
        if not detection.verified:
            return
        inc = self.incidents.add(detection)
        if inc is not None:
            self._heads_up(inc)
            self.wakes.detection(inc.id, (detection.node_id, detection.seq), now=self.sim.now())

    def _heads_up(self, inc: Incident) -> None:
        """Plain code, no model: as soon as two dry nodes agree on a chainsaw, axe or gunshot,
        tell the rangers a review has started. The model then confirms or dismisses; however
        long its queue is, the first warning never waits for it. Not a dispatch request."""
        if inc.heads_up_at is not None or inc.family not in THREAT_FAMILIES:
            return
        if inc.status not in ("open", "watching"):
            return
        dry, _ = self.services.rain_split(inc)
        if len(dry) < 2 or max(inc.weights.get(str(n), 0.0) for n in dry) < HEADS_UP_CONFIDENCE:
            return
        names = sorted(self.world.node(n).name for n in dry)
        sound = next((s for s in inc.sounds if s != "vehicle"), inc.family)
        text = f"Possible {sound} in sector {inc.sector}, heard by {len(names)} nodes. Reviewing."
        now = self.sim.now()
        self.incidents.update(inc.id, heads_up_at=now, heads_up=text)
        self.publish({"type": "heads_up", "incident_id": inc.id, "sector": inc.sector,
                      "sound": sound, "nodes": names, "text": text, "at": now})
        self._step(inc.id, "station", text)

    def priority(self, inc: Incident | None, wake: Wake) -> int:
        if inc is None or wake.messages:
            return P_RANGER
        dry, wet = self.services.rain_split(inc)
        if inc.family == "poaching":
            return P_THREAT
        if not dry and wet:
            return P_RAIN
        if inc.family in THREAT_FAMILIES:
            return P_THREAT if inc.family == "poaching" or len(dry) >= 2 else P_POSSIBLE
        if wake.check_due and not wake.new_records:
            return P_CHECK
        return P_VEHICLE

    def message(self, incident_id: str, text: str, outcome: str | None = None) -> None:
        if outcome not in (None, "confirmed_threat", "authorized_activity", "false_alarm", "unresolved"):
            raise ValueError("Unknown ranger outcome")
        with self._lock:
            inc = self.incidents.get(incident_id)
            if inc is None:
                raise NotFound(incident_id)
            text = _one_line(text, MAX_NOTE)
            if not text:
                raise ValueError("A ranger note is required")
            update = {"source": "ranger", "timestamp": self.sim.now(), "text": text,
                      "outcome": outcome, "records_at_update": len(inc.records)}
            self.incidents.update(inc.id, ranger_updates=[*inc.ranger_updates, update],
                                  updated_at=self.sim.now())
            pending = [a for a in self.approvals.values()
                       if a.incident_id == incident_id and a.status == "pending"]
            # Invalidate immediately. Do not make a ranger wait for inference to clear an
            # obsolete interrupt; a later investigative wake resumes it with rejection.
            for approval in pending:
                approval.status = "stale"
                approval.decision_note = "Superseded by ranger information: " + text
                approval.decided_at = self.sim.now()
                self._save_approval(approval)
                self.publish(approval.public())
            self.services.gate.revoke(incident_id)
            if inc.ranger_outcome() in ("authorized_activity", "false_alarm"):
                self._apply_ranger_clearance(inc)
            else:
                self.wakes.message(incident_id, text)
        self._step(incident_id, "station", "Ranger update received; reassessing. Already executed dispatches are unchanged.")

    def _apply_ranger_clearance(self, inc: Incident) -> None:
        update = inc.ranger_updates[-1]
        reason = "Ranger reports " + inc.ranger_outcome().replace("_", " ") + ": " + update["text"]
        assessment = {"time": fmt_time(self.sim.now()), "source": "ranger",
                      "explanation": reason, "alternative": "Earlier acoustic threat interpretation",
                      "next_action": "wait_recheck" if inc.dispatches else "dismiss",
                      "reason": "Explicit ranger correction supersedes the earlier interpretation. Prior dispatches cannot be recalled here.",
                      "records": [{"node": self.world.node(n).name, "seq": seq} for n, seq in inc.records],
                      "observation_count": len(inc.observations), "ranger_update_count": len(inc.ranger_updates)}
        self.incidents.update(inc.id, status="watching" if inc.dispatches else "dismissed",
                              summary=reason[:200], next_check_at=None,
                              assessments=[*inc.assessments, assessment], updated_at=self.sim.now())
        plan = "# Ranger correction\n\n" + reason + "\n\n" + assessment["reason"] + "\n"
        self.files.write(inc.folder + "plan.md", plan)
        self.publish({"type": "plan", "incident_id": inc.id, "markdown": plan, "html": render_markdown(plan)})

    # --- running ---

    def tick(self, now: int | None = None) -> list[Future | None]:
        """Wake the commander for every incident with something due."""
        now = self.sim.now() if now is None else now
        # A correction can arrive while a model is still constructing an approval.
        for a in list(self.approvals.values()):
            if a.status == "pending" and a.ranger_revision != len(self.incidents.get(a.incident_id).ranger_updates):
                a.status = "stale"
                a.decision_note = "Ranger information changed during review; reassess."
                self._save_approval(a)
                self.services.gate.revoke(a.incident_id)
                self.publish(a.public())
                self.wakes.message(a.incident_id, a.decision_note)
        with self._lock:
            waiting = {a.incident_id for a in self.approvals.values() if a.status == "pending"}
            due = self.wakes.pop_due(now, skip=self._busy | waiting)
            for w in due:
                self._busy.add(w.incident_id)
        ranked = sorted(((self.priority(self.incidents.get(w.incident_id), w), w) for w in due), key=lambda pw: pw[0])
        return [self._submit(w.incident_id, self._wake, w, priority=p) for p, w in ranked]

    def start(self, loop: asyncio.AbstractEventLoop | None = None, interval_s: float = 0.5) -> None:
        if isinstance(self.publish, BusPublisher) and loop is not None:
            self.publish.bind(loop)
        if self._ticker is not None:
            return

        def run() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.tick()
                except Exception:
                    log.exception("commander tick failed")

        self._ticker = threading.Thread(target=run, name="commander-ticker", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(self, incident_id: str, fn: Callable, *args: Any, priority: int = P_RANGER) -> Future | None:
        def job() -> None:
            try:
                fn(*args)
            except Exception as e:
                log.exception("commander run failed for %s", incident_id)
                self._step(incident_id, "commander", f"agent unavailable ({type(e).__name__})")
            finally:
                with self._lock:
                    self._busy.discard(incident_id)

        if self._executor is None:
            job()
            return None
        return self._executor.submit(job, priority)

    def _config(self, inc: Incident, reporter: StepReporter) -> dict:
        return {
            "configurable": {"thread_id": inc.thread_id, "incident_id": inc.id,
                             "ranger_revision": len(inc.ranger_updates)},
            "callbacks": [reporter],
            "recursion_limit": RECURSION_LIMIT,
            "metadata": {"incident_id": inc.id},
        }

    def _wake(self, wake: Wake) -> None:
        inc = self.incidents.get(wake.incident_id)
        if inc is None:
            return
        if inc.ranger_outcome() in ("authorized_activity", "false_alarm"):
            if inc.status != ("watching" if inc.dispatches else "dismissed"):
                self._apply_ranger_clearance(inc)
            return  # authoritative human clearance needs no model turn
        # A superseded proposal remains checkpointed but is never approved. Clear it
        # with a rejection only when there is a new investigation to perform.
        if self.pending_interrupt_ids(inc.id):
            self._run(inc.id, Command(resume={"decisions": [{"type": "reject", "message":
                "This proposal is obsolete. Do not propose dispatch again in this turn; await the new ranger/evidence message."}]}))
        if wake.check_due:
            self.incidents.update(inc.id, next_check_at=None)
        if wake.messages and inc.ranger_outcome() == "confirmed_threat":
            # An explicit new confirmed outcome is evidence even without new sensor sequences.
            self.incidents.update(inc.id, rejected_at_records=None)
        if self._prefiltered(inc, wake):
            return
        self._ensure_plan(inc)
        self._trace(inc.id, "wake", "station", _wake_reason(wake))
        text = self.wake_text(inc, wake)
        try:
            self._run(inc.id, {"messages": [{"role": "user", "content": text}]})
        except Exception as e:
            log.exception("commander run failed for %s", inc.id)
            tries = self._failures.get(inc.id, 0) + 1
            self._failures[inc.id] = tries
            if tries <= MAX_RETRIES:
                self.wakes.requeue(wake, delay_s=RETRY_S * tries)
                self._step(inc.id, "commander", f"agent unavailable ({type(e).__name__}); retrying")
            else:
                self._failures.pop(inc.id, None)
                self._step(inc.id, "commander", f"agent unavailable ({type(e).__name__}); gave up on this wake-up")
            return
        self._failures.pop(inc.id, None)

    def _prefiltered(self, inc: Incident, wake: Wake) -> bool:
        """Clear the most obvious rain alarm without the model: every record from one node, it
        is raining there, and that node already has dismissed false alarms. Anything else (a
        second node, dry weather, a gunshot, a ranger message, an earlier dispatch request)
        goes to the model. If the picture changes later, the next wake-up is judged afresh."""
        if not self.prefilter or not wake.new_records or wake.messages or wake.check_due:
            return False
        if inc.family not in PREFILTER_FAMILIES or inc.status not in ("open", "dismissed"):
            return False
        if inc.ranger_updates or inc.dispatches or inc.rejected_at_records is not None or inc.heads_up_at is not None:
            return False
        nodes = inc.node_ids()
        if len(nodes) != 1:
            return False
        dry, _ = self.services.rain_split(inc)
        history = self.services.false_alarm_history(nodes[0], exclude=inc.id)
        if dry or (history < 1 and inc.status != "dismissed"):
            return False
        now = self.sim.now()
        name = self.world.node(nodes[0]).name
        self.files.append(
            inc.folder + "triage.md",
            f"- {fmt_time(now)} station rule, no model: {len(inc.records)} record(s), all from "
            f"{name}, raining there, false-alarm history: {history} earlier dismissed incident(s) "
            "at this node (or this incident was already dismissed). "
            "Dismissed as a rain false alarm.\n",
        )
        self.incidents.update(inc.id, status="dismissed", summary=PREFILTER_REASON, updated_at=now)
        self._step(inc.id, "station", PREFILTER_REASON)
        with self._lock:
            s = self._stats.setdefault(inc.id, {"runs": 0, "wall_s": 0.0, "steps": {}})
            s["prefiltered"] = s.get("prefiltered", 0) + 1
        return True

    def _ensure_plan(self, inc: Incident) -> None:
        """Small local models often skip bookkeeping, so code seeds plan.md; the agent edits it."""
        path = inc.folder + "plan.md"
        if self.files.read(path):
            return
        drone_item = (
            "- [ ] If real: drone check of the estimated location\n"
            if self.deployment.has_drones
            else "- [ ] If real: ask a ranger to verify, or approve dispatch (no aerial verification)\n"
        )
        markdown = (
            f"# {inc.title}\n\n"
            "- [ ] Triage: real threat or false alarm?\n"
            f"{drone_item}"
            "- [ ] If confirmed: ask a ranger to approve dispatch\n"
            "- [ ] Follow up, then write the case file\n"
        )
        self.files.write(path, markdown)
        self.publish(
            {"type": "plan", "incident_id": inc.id, "markdown": markdown, "html": render_markdown(markdown)}
        )

    def _run(self, incident_id: str, payload: Any) -> None:
        inc = self.incidents.get(incident_id)
        ranger_revision = len(inc.ranger_updates)
        reporter = StepReporter(incident_id, self.files, self.publish, trace=self._trace)
        began = time.monotonic()
        config = self._config(inc, reporter)
        config["configurable"]["ranger_revision"] = ranger_revision
        try:
            result = self.agent.invoke(payload, config)
            # Small models re-propose a rejected dispatch over and over. Code turns the repeat
            # down itself unless new evidence arrived since the ranger said no.
            for _ in range(MAX_AUTO_REJECTS):
                interrupts = result.get("__interrupt__") or []
                problem = self._proposal_problem(inc)
                if not interrupts or problem.startswith("Camera report is explicitly") or not (self._repeat_after_reject(incident_id) or problem):
                    break
                requests = (interrupts[0].value or {}).get("action_requests", [])
                n = len(requests) or 1
                args = (requests[0].get("args") or {}) if requests else {}
                self.files.append_jsonl(  # audit trail: the ranger never saw this request
                    inc.folder + "approvals.jsonl",
                    {"time": fmt_time(self.sim.now()), "decision": "auto_rejected", "by": "station",
                     "sector": str(args.get("sector", ""))[:8], "note": REPEAT_REJECT},
                )
                reporter.step("commander", "repeat dispatch request blocked: a ranger rejected it and nothing new has arrived")
                result = self.agent.invoke(
                    Command(resume={"decisions": [{"type": "reject", "message": REPEAT_REJECT if self._repeat_after_reject(incident_id) else problem}] * n}), config
                )
        finally:
            self._record_stats(incident_id, time.monotonic() - began, reporter.counts)
        interrupts = result.get("__interrupt__") or []
        if inc.ranger_outcome() in ("authorized_activity", "false_alarm"):
            if inc.status != ("watching" if inc.dispatches else "dismissed"):
                self._apply_ranger_clearance(inc)
            return
        if interrupts:
            problem = self._proposal_problem(inc)
            if problem:
                camera_review = problem.startswith("Camera report is explicitly")
                reason = ("Ranger review required: " if camera_review else "Assessment unavailable: ") + problem
                if camera_review:
                    self.incidents.update(inc.id, assessments=[*inc.assessments, {
                        "time": fmt_time(self.sim.now()), "source": "station_policy",
                        "explanation": "Camera did not corroborate the acoustic reports; activity remains unresolved.",
                        "alternative": "Activity outside camera coverage or a false acoustic alarm",
                        "next_action": "request_ranger_assessment", "reason": reason,
                        "observation_count": len(inc.observations), "ranger_update_count": len(inc.ranger_updates),
                        "records": [{"node": self.world.node(n).name, "seq": seq} for n, seq in inc.records],
                    }])
                self.incidents.update(inc.id, status="watching", summary=reason[:200],
                                      next_check_at=self.sim.now() + 600)
                self.wakes.schedule(inc.id, self.sim.now() + 600, note=reason)
                self._step(inc.id, "station", reason)
            else:
                self._open_approval(incident_id, interrupts[0], ranger_revision)
            return
        final = next((m for m in reversed(result.get("messages", [])) if isinstance(m, AIMessage)), None)
        if final is not None and final.text:
            reporter.step("commander", _first_meaningful_line(final.text))
        self.incidents.touch(incident_id, self.sim.now())

    def _proposal_problem(self, inc: Incident) -> str:
        if inc.ranger_outcome() in ("authorized_activity", "false_alarm"):
            return "Ranger clearance forbids dispatch. Do not repeat this proposal."
        if inc.observations:
            latest = inc.observations[-1]
            if (latest["availability"] == "observed" and camera_uncertain(latest["content"])
                    and inc.ranger_outcome() != "confirmed_threat"):
                return ("Camera report is explicitly inconclusive or contradictory. Ask for ranger input, "
                        "record_assessment, set_status watching and schedule_check. "
                        "A new confirming observation or explicit ranger confirmed_threat outcome is needed before dispatch.")
            if not prompts.investigation_enabled():
                return ""  # light mode: no assessment step, the camera check above still applies
            assessment = inc.assessments[-1] if inc.assessments else {}
            if (assessment.get("observation_count") != len(inc.observations)
                    or assessment.get("ranger_update_count") != len(inc.ranger_updates)
                    or assessment.get("next_action") != "propose_dispatch"):
                return ("Camera result requires a current record_assessment before dispatch. "
                        "For an inconclusive/contradictory view, set_status watching and schedule_check; "
                        "do not automatically dispatch. Compare alternatives and limited coverage.")
        return ""

    def _repeat_after_reject(self, incident_id: str) -> bool:
        inc = self.incidents.get(incident_id)
        return inc is not None and inc.rejected_at_records is not None and len(inc.records) <= inc.rejected_at_records

    def wake_text(self, inc: Incident, wake: Wake) -> str:
        kinds = [k for k, on in (("new detections", wake.new_records), ("scheduled check", wake.check_due),
                                 ("ranger message", wake.messages)) if on]
        now = self.sim.now()
        lines = [
            f"Wake-up: {', '.join(kinds)}",
            f"Incident: {inc.id}",
            f"Sector: {inc.sector}",
            f"Family: {inc.family}",
            f"Status: {inc.status}",
            f"Folder: {inc.folder}",
            f"Time: {fmt_time(now)}",
        ]
        if wake.new_records:
            refs = [f"[{self.world.node(n).name} #{seq}]" for n, seq in wake.new_records]
            lines.append("New records (details below): " + ", ".join(refs))
        lines.append(f"Estimated source: sector {inc.sector} ({inc.x_m:.0f} m E, {inc.y_m:.0f} m N)")
        lines.append("All evidence so far (pass this to triage):")
        lines.append(self.services.incident_evidence(inc))
        if inc.ranger_updates:
            lines.append("Latest ranger update: " + json.dumps({**inc.ranger_updates[-1], "outcome": inc.ranger_outcome()}))
        if wake.check_due:
            lines.append(f"Scheduled check due ({wake.check_note}).")
        for m in wake.messages:
            lines.append(f'Ranger says: "{_one_line(m, MAX_NOTE)}"')
        return "\n".join(lines)

    # --- approvals ---

    def _record_stats(self, incident_id: str, wall_s: float, counts: dict[str, int]) -> None:
        """Wall time and tool calls the agent spent on this incident (approval waits excluded)."""
        with self._lock:
            s = self._stats.setdefault(incident_id, {"runs": 0, "wall_s": 0.0, "steps": {}})
            s["runs"] += 1
            s["wall_s"] = round(s["wall_s"] + wall_s, 2)
            for agent, n in counts.items():
                s["steps"][agent] = s["steps"].get(agent, 0) + n

    def _open_approval(self, incident_id: str, intr: Any, ranger_revision: int | None = None) -> None:
        requests = [r for r in (intr.value or {}).get("action_requests", []) if r.get("name") == "dispatch_rangers"]
        if not requests:
            return
        args = requests[0].get("args") or {}
        limitation = self.services.response_limitations(self.incidents.get(incident_id))
        now = self.sim.now()
        with self._lock:
            for a in self.approvals.values():
                if a.incident_id == incident_id and a.status == "pending":
                    a.status = "stale"
                    self._save_approval(a)
            approval = Approval(
                id=secrets.token_urlsafe(12),
                incident_id=incident_id,
                interrupt_id=intr.id,
                sector=str(args.get("sector", ""))[:8],
                note=_one_line(limitation + " " + args.get("note", ""), MAX_NOTE),
                question=_one_line(limitation + " " + requests[0].get("description", ""), MAX_NOTE + 60),
                created_at=now,
                ranger_revision=len(self.incidents.get(incident_id).ranger_updates) if ranger_revision is None else ranger_revision,
                count=len(requests),
                records=[{"node": self.world.node(n).name, "seq": seq} for n, seq in self.incidents.get(incident_id).records],
            )
            self.approvals[approval.id] = approval
            self._save_approval(approval)
        self.incidents.update(incident_id, status="awaiting_approval", summary="Waiting for a ranger to approve dispatch", updated_at=now)
        self._trace(incident_id, "approval", "station", f"Asked a ranger to approve sending a team to {approval.sector}")
        self.publish(approval.public())

    def _save_approval(self, a: Approval) -> None:
        self.repo.save_approval(a.id, a.incident_id, asdict(a))

    def pending_interrupt_ids(self, incident_id: str) -> set[str]:
        inc = self.incidents.get(incident_id)
        state = self.agent.get_state({"configurable": {"thread_id": inc.thread_id}})
        return {i.id for i in state.interrupts}

    def decide(self, approval_id: str, decision: str, note: str = "", via: str = "dashboard") -> Approval:
        """Act on a ranger's decision. Only a currently pending interrupt can be resumed."""
        if decision not in ("approve", "edit", "reject"):
            raise ValueError("decision must be approve, edit or reject")
        note = _one_line(note, MAX_NOTE)
        if decision == "edit" and not note:
            raise ValueError("edit needs a note")
        with self._lock:
            a = self.approvals.get(approval_id)
            if a is None:
                raise NotFound(approval_id)
            if decision != "reject" and a.ranger_revision != len(self.incidents.get(a.incident_id).ranger_updates):
                raise Conflict("Ranger information changed; reassess before dispatch")
            if decision != "reject" and (problem := self._proposal_problem(self.incidents.get(a.incident_id))):
                raise Conflict(problem)
            if a.status != "pending":
                raise Conflict(f"approval already {a.status}")
            if a.interrupt_id not in self.pending_interrupt_ids(a.incident_id):
                a.status = "stale"
                self._save_approval(a)
                raise Conflict("approval is no longer pending")
            now = self.sim.now()
            a.status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[decision]
            a.decided_at, a.decision_note = now, note
            a.decided_via = via if via in VIAS else "dashboard"
            self._save_approval(a)
            self._busy.add(a.incident_id)
        if decision == "reject":
            decisions = [{"type": "reject", "message": note or "Ranger rejected the dispatch."}] * a.count
        elif decision == "edit":
            edited = {"name": "dispatch_rangers", "args": {"sector": a.sector, "note": note}}
            decisions = [{"type": "edit", "edited_action": edited}] * a.count
        else:
            decisions = [{"type": "approve"}] * a.count
        if decision != "reject":
            self.services.gate.grant(a.incident_id, a.count)
        inc = self.incidents.get(a.incident_id)
        self.files.append_jsonl(
            inc.folder + "approvals.jsonl",
            {"time": fmt_time(now), "decision": a.status, "by": "ranger", "via": a.decided_via,
             "sector": a.sector, "note": note if decision != "approve" else a.note},
        )
        self.incidents.update(
            a.incident_id, status="open", summary=f"Ranger {a.status} dispatch", updated_at=now,
            rejected_at_records=len(inc.records) if decision == "reject" else None,
        )
        self.publish(a.public())
        self._trace(a.incident_id, "ranger", "ranger", f"{a.status.capitalize()} the team for {a.sector} (via {VIAS[a.decided_via]})",
                    decision=a.status, via=a.decided_via)

        def resume() -> None:
            try:
                self._run(a.incident_id, Command(resume={"decisions": decisions}))
            finally:
                self.services.gate.revoke(a.incident_id)  # unused grants never carry over

        self._submit(a.incident_id, resume)
        return a

    def busy(self) -> bool:
        """Is the commander working, or about to? Eval uses it to hold the sim while the model thinks."""
        with self._lock:
            return bool(self._busy) or self.wakes.has_pending()

    # --- views (for the HTTP routes) ---

    def _step(self, incident_id: str, agent: str, summary: str) -> None:
        self._trace(incident_id, "note", agent, summary)

    def _trace(self, incident_id: str, kind: str, agent: str, summary: str, **extra: Any) -> None:
        """One entry in the incident's decision trail: saved to trace.jsonl and sent to the UI.
        kind: wake | delegate | result | tool | error | status | approval | ranger | note."""
        inc = self.incidents.get(incident_id)
        if inc is None:
            return
        entry = {"t": int(self.sim.now()), "kind": kind, "agent": agent, "summary": _one_line(summary),
                 **{k: v for k, v in extra.items() if v is not None}}
        try:
            self.files.append_jsonl(inc.folder + "trace.jsonl", entry)
        except OSError:
            log.warning("could not write trace for %s", incident_id)
        self.publish({"type": "agent_step", "incident_id": incident_id, **entry})

    def _incident_changed(self, inc: Incident) -> None:
        seen = self.__dict__.setdefault("_last_status", {})  # may run during __init__
        last = seen.get(inc.id)
        if last != inc.status:
            seen[inc.id] = inc.status
            if last is not None or inc.status == "open":
                self._trace(inc.id, "status", "station", STATUS_TEXT.get(inc.status, inc.status), status=inc.status)
        self.publish({"type": "incident", "incident": inc.public(self.world)})

    def list_incidents(self) -> list[dict]:
        return [i.public(self.world) for i in self.incidents.all()]

    def incident_view(self, incident_id: str) -> dict:
        inc = self.incidents.get(incident_id)
        if inc is None:
            raise NotFound(incident_id)
        plan = self.files.read(inc.folder + "plan.md") or ""
        rows = []
        for line in (self.files.read(inc.folder + "detections.jsonl") or "").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append({k: r.get(k) for k in ("record", "node", "seq", "time", "sound", "confidence", "sector")})
        pending = next((a.public() for a in list(self.approvals.values()) if a.incident_id == inc.id and a.status == "pending"), None)
        names = ("plan.md", "detections.jsonl", "triage.md", "drone_log.md", "case_file.md", "approvals.jsonl")
        return {
            "incident": inc.public(self.world),
            "plan": {"markdown": plan, "html": render_markdown(plan)},
            "triage_html": render_markdown(self.files.read(inc.folder + "triage.md") or ""),
            "drone_log_html": render_markdown(self.files.read(inc.folder + "drone_log.md") or ""),
            "files": [n for n in names if self.files.read(inc.folder + n) is not None],
            "records": rows,
            "dispatches": inc.dispatches,
            "trace": self._read_trace(inc),
            "ranger_actions": self._ranger_actions(inc),
            "approvals": [a.public() for a in list(self.approvals.values()) if a.incident_id == inc.id],
            "pending_approval": pending,
            "stats": self._stats.get(inc.id, {"runs": 0, "wall_s": 0.0, "steps": {}}),
        }

    def _read_trace(self, inc: Incident, limit: int = 400) -> list[dict]:
        out = []
        for line in (self.files.read(inc.folder + "trace.jsonl") or "").splitlines()[-limit:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def _ranger_actions(self, inc: Incident) -> list[dict]:
        """Every decision on a dispatch request, in order: the ranger's (dashboard or iMessage)
        and the station's own refusals of repeat requests. From the code-owned approvals.jsonl."""
        out = []
        for line in (self.files.read(inc.folder + "approvals.jsonl") or "").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                out.append({k: _one_line(str(r.get(k) or ""), MAX_NOTE) for k in ("time", "decision", "by", "via", "sector", "note")})
        return out

    def case_file(self, incident_id: str) -> dict:
        """Render the case file sanitized and re-check it: signatures, clips, citations."""
        inc = self.incidents.get(incident_id)
        if inc is None:
            raise NotFound(incident_id)
        text = self.files.read(inc.folder + "case_file.md")
        if text is None:
            raise NotFound(f"{incident_id} has no case file yet")
        integrity = check_records(self.world, self.store, self.services.verify, inc.records, self.services.clips_root)
        known = set()
        for (node_id, seq), rec in zip(inc.records, integrity.records, strict=True):
            if rec.valid:
                known.add((self.world.node(node_id).name, seq))
        return {
            "incident_id": inc.id,
            "html": render_markdown(text),
            "citations": check_citations(text, known),
            "integrity": integrity.to_json(),
            "checked_at": int(time.time()),
        }
