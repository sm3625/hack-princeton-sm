"""Incident commander HTTP API.

GET  /api/incidents                      list (names only, no node ids) and whether the commander is busy
GET  /api/incidents/{id}                 plan, files, records, approvals (markdown rendered sanitized)
GET  /api/incidents/{id}/case_file       {"html": sanitized, "citations", "integrity"} re-checked on open
POST /api/incidents/{id}/messages        {"text"} ranger message -> wakes the commander
POST /api/approvals/{approval_id}        {"decision": "approve"|"edit"|"reject", "note"}
     Acts only on a currently pending interrupt: 404 unknown id, 409 decided or stale.
     There is no other way to dispatch through the API.

Startup (router lifespan, merged into the app by include_router): builds the Commander from
app.state.event_store / app.state.sim / app.state.verify_record when the other tracks set them,
registers Commander.on_verified on app.state.receiver.on_verified (or app.state.on_verified),
and starts the wake-up ticker. AGENT_FS_DIR / AGENT_DB override where files and state live.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from agent.commander import Commander, Conflict, NotFound
from common.contracts import ROOT
from station.config import ConfigError

log = logging.getLogger("station.routes_agent")

_commander: Commander | None = None


def configure(commander: Commander | None) -> None:
    global _commander
    _commander = commander


def get_commander() -> Commander:
    if _commander is None:
        raise HTTPException(503, "Incident commander is not running.")
    return _commander


def _hooks(app: FastAPI) -> list | None:
    receiver = getattr(app.state, "receiver", None)
    hooks = getattr(receiver, "on_verified", None)
    if hooks is None:
        hooks = getattr(app.state, "on_verified", None)
    return hooks if isinstance(hooks, list) else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sim.engine import SimEngine

    started = None
    session = None
    if (_commander is None and os.getenv("DEMO_RESET_RUNS", "1") == "1"
            and isinstance(getattr(app.state, "sim", None), SimEngine)):
        from station.demo_session import DemoSession

        session = DemoSession(app, configure, _hooks(app))
        await session.reset()
        app.state.sim.on_reset = session.reset
    if _commander is None:
        try:
            started = Commander(
                store=getattr(app.state, "event_store", None),
                sim=getattr(app.state, "sim", None),
                verify=getattr(app.state, "verify_record", None),
                fs_root=Path(os.getenv("AGENT_FS_DIR", ROOT / "agent_fs")),
                db_path=Path(os.getenv("AGENT_DB", ROOT / "agent_state.sqlite")),
                deployment=getattr(app.state, "deployment", None),
            )
            configure(started)
        except Exception:
            log.exception("incident commander failed to start")
    commander = _commander
    if commander is not None:
        hooks = _hooks(app)
        if hooks is not None and commander.on_verified not in hooks:
            hooks.append(commander.on_verified)
        commander.start(asyncio.get_running_loop())
    try:
        yield
    finally:
        if session is not None:
            app.state.sim.on_reset = None
            session.retire()
            configure(None)
        elif started is not None:
            started.stop()
            configure(None)


router = APIRouter(lifespan=lifespan)
CommanderDep = Annotated[Commander, Depends(get_commander)]


class ModelSelection(BaseModel):
    mode: Literal["haiku", "gemini"]


@router.put("/api/commander/model")
async def set_model(body: ModelSelection, request: Request, c: CommanderDep) -> dict:
    settings = request.app.state.settings
    if settings.llm_mode != body.mode:
        settings = replace(settings, llm_mode=body.mode)
        try:
            c.switch_model(settings)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from None
        request.app.state.settings = settings
    return {"mode": settings.llm_mode, "model": settings.model_label}


class Decision(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    note: str = Field("", max_length=500)
    via: Literal["dashboard", "imessage"] = "dashboard"  # shown on the dashboard; not a permission


class RangerMessage(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    outcome: Literal["confirmed_threat", "authorized_activity", "false_alarm", "unresolved"] | None = None


@router.get("/api/incidents")
def list_incidents(c: CommanderDep) -> dict:
    return {"incidents": c.list_incidents(), "busy": c.busy()}


@router.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str, c: CommanderDep) -> dict:
    try:
        return c.incident_view(incident_id)
    except NotFound:
        raise HTTPException(404, "No such incident.") from None


@router.get("/api/incidents/{incident_id}/research")
def get_research(incident_id: str, c: CommanderDep) -> dict:
    try:
        return c.research_view(incident_id)
    except NotFound:
        raise HTTPException(404, "No such incident.") from None


@router.get("/api/incidents/{incident_id}/case_file")
def get_case_file(incident_id: str, c: CommanderDep) -> dict:
    try:
        return c.case_file(incident_id)
    except NotFound:
        raise HTTPException(404, "No case file yet.") from None


@router.post("/api/incidents/{incident_id}/messages", status_code=202)
def post_message(incident_id: str, body: RangerMessage, c: CommanderDep) -> dict:
    try:
        c.message(incident_id, body.text, body.outcome)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except NotFound:
        raise HTTPException(404, "No such incident.") from None
    return {"ok": True}


@router.post("/api/approvals/{approval_id}")
def post_approval(approval_id: str, body: Decision, c: CommanderDep) -> dict:
    try:
        approval = c.decide(approval_id, body.decision, body.note, body.via)
    except NotFound:
        raise HTTPException(404, "No such approval.") from None
    except Conflict as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"ok": True, "status": approval.status, "incident_id": approval.incident_id}
