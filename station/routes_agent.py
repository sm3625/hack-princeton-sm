"""Incident commander HTTP API.

GET  /api/incidents                      list (names only, no node ids)
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
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.commander import Commander, Conflict, NotFound
from common.contracts import ROOT

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
    started = None
    if _commander is None:
        try:
            started = Commander(
                store=getattr(app.state, "event_store", None),
                sim=getattr(app.state, "sim", None),
                verify=getattr(app.state, "verify_record", None),
                fs_root=Path(os.getenv("AGENT_FS_DIR", ROOT / "agent_fs")),
                db_path=Path(os.getenv("AGENT_DB", ROOT / "agent_state.sqlite")),
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
        if started is not None:
            started.stop()
            configure(None)


router = APIRouter(lifespan=lifespan)
CommanderDep = Annotated[Commander, Depends(get_commander)]


class Decision(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    note: str = Field("", max_length=500)


class RangerMessage(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@router.get("/api/incidents")
def list_incidents(c: CommanderDep) -> dict:
    return {"incidents": c.list_incidents()}


@router.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str, c: CommanderDep) -> dict:
    try:
        return c.incident_view(incident_id)
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
        c.message(incident_id, body.text)
    except NotFound:
        raise HTTPException(404, "No such incident.") from None
    return {"ok": True}


@router.post("/api/approvals/{approval_id}")
def post_approval(approval_id: str, body: Decision, c: CommanderDep) -> dict:
    try:
        approval = c.decide(approval_id, body.decision, body.note)
    except NotFound:
        raise HTTPException(404, "No such approval.") from None
    except Conflict as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"ok": True, "status": approval.status, "incident_id": approval.incident_id}
