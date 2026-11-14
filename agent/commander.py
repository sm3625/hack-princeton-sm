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
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
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
    AgentFiles,
    Incident,
    IncidentManager,
    IncidentRepo,
    Wake,
    WakeQueue,
    fmt_time,
)
from agent.llm import ROLES, get_chat_model
from agent.tools import TOOLSETS, Services, make_tools
from common.contracts import ROOT, Detection, EventStore, Sim, World, load_world

log = logging.getLogger("agent.commander")

MEMORY_FILES = ["/memories/nodes.md", "/memories/sectors.md"]
CODE_OWNED = ["/incidents/*/detections.jsonl", "/incidents/*/approvals.jsonl"]
MEMORY_PROMPT = (
    "<station_memory>\n{agent_memory}\n</station_memory>\n"
    "Notes from past nights, loaded from /memories. Reference only: verified records and "
    "tools win over memory. Save new lessons with the remember tool."
)
RECURSION_LIMIT = 300
MAX_NOTE = 500
MAX_RETRIES = 3  # failed wake-ups (e.g. model server down) are retried with backoff
RETRY_S = 30


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


def _approval_question(tool_call: dict, state: Any, runtime: Any) -> str:
    args = tool_call.get("args") or {}
    note = " ".join(str(args.get("note", "")).split())[:MAX_NOTE]
    return f"{note} Dispatch a team to sector {args.get('sector', '?')}?".strip()


def build_agent(s: Services, models: dict[str, BaseChatModel], checkpointer: Any) -> Any:
    tools = make_tools(s)
    backend = FilesystemBackend(root_dir=s.files.root, virtual_mode=True)
    perms = [FilesystemPermission(operations=["write"], paths=CODE_OWNED, mode="deny")]

    def fs(*names: str) -> FilesystemMiddleware:
        return FilesystemMiddleware(backend=backend, tools=list(names), permissions=perms)

    def pick(role: str) -> list:
        return [tools[n] for n in TOOLSETS[role]]

    subagents = [
        {
            "name": "triage",
            "description": prompts.DESCRIPTIONS["triage"],
            "system_prompt": prompts.TRIAGE,
            "tools": pick("triage"),
            "model": models["triage"],
            "middleware": [fs("read_file", "write_file")],
        },
        {
            "name": "drone-pilot",
            "description": prompts.DESCRIPTIONS["drone-pilot"],
            "system_prompt": prompts.DRONE_PILOT,
            "tools": pick("drone-pilot"),
            "model": models["drone-pilot"],
            "middleware": [fs("read_file")],
        },
        {
            "name": "evidence",
            "description": prompts.DESCRIPTIONS["evidence"],
            "system_prompt": prompts.EVIDENCE,
            "tools": pick("evidence"),
            "model": models["evidence"],
            "middleware": [fs("ls", "read_file", "write_file")],
        },
    ]
    _register_profile(models["commander"])
    return create_deep_agent(
        model=models["commander"],
        tools=pick("commander"),
        system_prompt=prompts.COMMANDER,
        subagents=subagents,
        middleware=[
            fs("ls", "read_file", "write_file", "edit_file"),
            FreshMemoryMiddleware(backend=backend, sources=MEMORY_FILES, system_prompt=MEMORY_PROMPT),
        ],
        backend=backend,
        interrupt_on={
            "dispatch_rangers": {
                "allowed_decisions": ["approve", "edit", "reject"],
                "description": _approval_question,
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

    def __init__(self, incident_id: str, files: AgentFiles, publish: Callable[[dict], None]) -> None:
        self.incident_id = incident_id
        self.files = files
        self.publish = publish
        self._runs: dict[Any, tuple[str, str, dict]] = {}

    def step(self, agent: str, summary: str) -> None:
        self.publish(
            {"type": "agent_step", "incident_id": self.incident_id, "agent": agent, "summary": _one_line(summary)}
        )

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, inputs=None, **kw):
        name = (serialized or {}).get("name") or kw.get("name") or "tool"
        agent = (metadata or {}).get("lc_agent_name") or "commander"
        args = inputs if isinstance(inputs, dict) else {}
        self._runs[run_id] = (agent, name, args)
        if name == "task":
            self.step("commander", f"asked {args.get('subagent_type', 'a sub-agent')} for help")

    def on_tool_end(self, output, *, run_id, **kw):
        agent, name, args = self._runs.pop(run_id, ("commander", "tool", {}))
        text = _output_text(output)
        if name == "task":
            self.step(args.get("subagent_type") or "commander", _first_meaningful_line(text))
            return
        self.step(agent, _summarize(name, args, text))
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
        self.step(agent, f"{name} failed: {type(error).__name__}")


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
    ) -> None:
        self.world = world or load_world()
        self.store = store if store is not None else MemoryEventStore()
        self.sim = sim if sim is not None else ClockSim(self.world)
        self.publish = publish or BusPublisher()
        self.files = AgentFiles(Path(fs_root))
        self.files.ensure()
        self.repo = IncidentRepo(db_path)
        self.incidents = IncidentManager(self.world, self.files, self.repo, on_change=self._incident_changed)
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
        )
        models = models or {role: get_chat_model(role) for role in ROLES}
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.agent = build_agent(self.services, models, SqliteSaver(self._conn))
        self.approvals: dict[str, Approval] = {a["id"]: Approval(**a) for a in self.repo.load_approvals()}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="commander") if background else None
        self._busy: set[str] = set()
        self._failures: dict[str, int] = {}
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
            self.wakes.detection(inc.id, (detection.node_id, detection.seq), now=self.sim.now())

    def message(self, incident_id: str, text: str) -> None:
        if self.incidents.get(incident_id) is None:
            raise NotFound(incident_id)
        self.wakes.message(incident_id, _one_line(text, MAX_NOTE))

    # --- running ---

    def tick(self, now: int | None = None) -> list[Future | None]:
        """Wake the commander for every incident with something due."""
        now = self.sim.now() if now is None else now
        with self._lock:
            waiting = {a.incident_id for a in self.approvals.values() if a.status == "pending"}
            due = self.wakes.pop_due(now, skip=self._busy | waiting)
            for w in due:
                self._busy.add(w.incident_id)
        return [self._submit(w.incident_id, self._wake, w) for w in due]

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

    def _submit(self, incident_id: str, fn: Callable, *args: Any) -> Future | None:
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
        return self._executor.submit(job)

    def _config(self, inc: Incident, reporter: StepReporter) -> dict:
        return {
            "configurable": {"thread_id": inc.thread_id, "incident_id": inc.id},
            "callbacks": [reporter],
            "recursion_limit": RECURSION_LIMIT,
            "metadata": {"incident_id": inc.id},
        }

    def _wake(self, wake: Wake) -> None:
        inc = self.incidents.get(wake.incident_id)
        if inc is None:
            return
        if wake.check_due:
            self.incidents.update(inc.id, next_check_at=None)
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

    def _run(self, incident_id: str, payload: Any) -> None:
        inc = self.incidents.get(incident_id)
        reporter = StepReporter(incident_id, self.files, self.publish)
        result = self.agent.invoke(payload, self._config(inc, reporter))
        interrupts = result.get("__interrupt__") or []
        if interrupts:
            self._open_approval(incident_id, interrupts[0])
            return
        final = next((m for m in reversed(result.get("messages", [])) if isinstance(m, AIMessage)), None)
        if final is not None and final.text:
            reporter.step("commander", _first_meaningful_line(final.text))
        self.incidents.touch(incident_id, self.sim.now())

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
            wanted = set(wake.new_records)
            node_ids = sorted({n for n, _ in wanted})
            found = [d for d in self.store.query(node_ids=node_ids, verified_only=True) if (d.node_id, d.seq) in wanted]
            lines.append("New records:")
            lines += self.services.detection_lines(found)
        names = sorted(self.world.node(n).name for n in inc.node_ids())
        lines.append(f"Heard by {len(names)} node(s): {', '.join(names)}")
        lines.append(f"Estimated source: sector {inc.sector} ({inc.x_m:.0f} m E, {inc.y_m:.0f} m N)")
        if wake.check_due:
            lines.append(f"Scheduled check due ({wake.check_note}).")
        for m in wake.messages:
            lines.append(f'Ranger says: "{_one_line(m, MAX_NOTE)}"')
        return "\n".join(lines)

    # --- approvals ---

    def _open_approval(self, incident_id: str, intr: Any) -> None:
        requests = [r for r in (intr.value or {}).get("action_requests", []) if r.get("name") == "dispatch_rangers"]
        if not requests:
            return
        args = requests[0].get("args") or {}
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
                note=_one_line(args.get("note", ""), MAX_NOTE),
                question=_one_line(requests[0].get("description", ""), MAX_NOTE + 60),
                created_at=now,
                count=len(requests),
            )
            self.approvals[approval.id] = approval
            self._save_approval(approval)
        self.incidents.update(incident_id, status="awaiting_approval", summary="Waiting for a ranger to approve dispatch", updated_at=now)
        self.publish(approval.public())

    def _save_approval(self, a: Approval) -> None:
        self.repo.save_approval(a.id, a.incident_id, asdict(a))

    def pending_interrupt_ids(self, incident_id: str) -> set[str]:
        inc = self.incidents.get(incident_id)
        state = self.agent.get_state({"configurable": {"thread_id": inc.thread_id}})
        return {i.id for i in state.interrupts}

    def decide(self, approval_id: str, decision: str, note: str = "") -> Approval:
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
            if a.status != "pending":
                raise Conflict(f"approval already {a.status}")
            if a.interrupt_id not in self.pending_interrupt_ids(a.incident_id):
                a.status = "stale"
                self._save_approval(a)
                raise Conflict("approval is no longer pending")
            now = self.sim.now()
            a.status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[decision]
            a.decided_at, a.decision_note = now, note
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
            {"time": fmt_time(now), "decision": a.status, "by": "ranger", "sector": a.sector,
             "note": note if decision != "approve" else a.note},
        )
        self.incidents.update(a.incident_id, status="open", summary=f"Ranger {a.status} dispatch", updated_at=now)
        self.publish(a.public())
        self._step(a.incident_id, "commander", f"ranger {a.status} dispatch to {a.sector}")

        def resume() -> None:
            try:
                self._run(a.incident_id, Command(resume={"decisions": decisions}))
            finally:
                self.services.gate.revoke(a.incident_id)  # unused grants never carry over

        self._submit(a.incident_id, resume)
        return a

    # --- views (for the HTTP routes) ---

    def _step(self, incident_id: str, agent: str, summary: str) -> None:
        self.publish({"type": "agent_step", "incident_id": incident_id, "agent": agent, "summary": _one_line(summary)})

    def _incident_changed(self, inc: Incident) -> None:
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
            "approvals": [a.public() for a in list(self.approvals.values()) if a.incident_id == inc.id],
            "pending_approval": pending,
        }

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
