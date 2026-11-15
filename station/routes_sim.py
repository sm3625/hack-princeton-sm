"""Simulator routes: world map, scenario control, and deep links into the UI."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sim import world as worldmap
from sim.clock import MAX_SPEED, MIN_SPEED
from sim.engine import get_engine
from sim.scenario import ScenarioError, UnknownScenario
from station.config import ROOT

INDEX = ROOT / "ui" / "index.html"


@asynccontextmanager
async def lifespan(_app):
    engine = get_engine()
    engine.attach()
    try:
        yield
    finally:
        await engine.shutdown()


router = APIRouter(lifespan=lifespan)


class ScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    speed: float | None = Field(default=None, ge=MIN_SPEED, le=MAX_SPEED)
    from_time: str | None = Field(default=None, alias="from", pattern=r"^\d{1,2}:\d{2}$")


class PauseRequest(BaseModel):
    paused: bool | None = None  # omitted: toggle


class SpeedRequest(BaseModel):
    speed: float = Field(ge=MIN_SPEED, le=MAX_SPEED)


@router.get("/api/world")
def world() -> dict:
    engine = get_engine()
    site = getattr(engine, "site", None)
    out = worldmap.to_json(engine.world)
    if site is not None:
        out["map"] = site.key
        out["geo_url"] = site.geo_url
    return out


@router.get("/api/sim/state")
async def sim_state() -> dict:
    engine = get_engine()
    engine.attach()
    return engine.state()


@router.post("/api/sim/scenario")
async def start_scenario(req: ScenarioRequest) -> dict:
    try:
        return await get_engine().start_scenario(req.name, req.speed, req.from_time)
    except UnknownScenario as e:
        raise HTTPException(status_code=404, detail="Unknown scenario") from e
    except ScenarioError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/api/sim/pause")
async def pause(req: PauseRequest | None = None) -> dict:
    engine = get_engine()
    engine.attach()
    return engine.set_paused(req.paused if req else None)


@router.post("/api/sim/speed")
async def speed(req: SpeedRequest) -> dict:
    engine = get_engine()
    engine.attach()
    return engine.set_speed(req.speed)


@router.get("/incident/{incident_id}", include_in_schema=False)
def incident_page(incident_id: str) -> FileResponse:
    return FileResponse(INDEX)
