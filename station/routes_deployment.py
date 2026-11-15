"""View site resources and adjust them for the next simulator run."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from common.deployment import MAX_DRONES, DeploymentError, load_deployment
from common.sites import available_maps, map_profile, map_summaries

router = APIRouter()


@router.get("/api/deployment")
async def deployment(request: Request) -> dict:
    engine = getattr(request.app.state, "sim", None)
    pending = getattr(engine, "next_deployment", None)
    editable = bool(getattr(engine, "on_reset", None))
    current = engine.deployment if editable else request.app.state.deployment
    return {
        **(pending or current).public(),
        "editable": editable,
        "pending": pending is not None,
    }


class Resources(BaseModel):
    drones: int = Field(ge=0, le=MAX_DRONES)
    ranger_teams: int = Field(ge=1, le=50)
    response_min: int = Field(ge=1, le=1440)
    connectivity: Literal["lora", "satellite", "cellular"]


@router.put("/api/deployment/resources")
async def set_resources(body: Resources, request: Request) -> dict:
    engine = request.app.state.sim
    if engine.on_reset is None:
        raise HTTPException(409, "Resource editing requires simulator run resets.")
    baseline = request.app.state.deployment
    engine.next_deployment = baseline.model_copy(update={
        "drones": baseline.drones.model_copy(update={
            "count": body.drones, "units": baseline.drones.units[:body.drones],
        }),
        "ranger_teams": baseline.ranger_teams.model_copy(update={
            "count": body.ranger_teams, "response_min": body.response_min,
        }),
        "connectivity": body.connectivity,
    })
    return await deployment(request)


@router.get("/api/sites")
async def sites(request: Request) -> dict:
    engine = getattr(request.app.state, "sim", None)
    return {
        "current": request.app.state.deployment.map,
        "editable": bool(getattr(engine, "on_reset", None)),
        "sites": map_summaries(),
    }


class SiteChoice(BaseModel):
    map: str = Field(pattern=r"^[a-z0-9_]{1,40}$")


@router.put("/api/sites")
async def switch_site(body: SiteChoice, request: Request) -> dict:
    """Open another map: the current run ends, a fresh run starts on the new map."""
    app = request.app
    engine = app.state.sim
    if engine.on_reset is None:
        raise HTTPException(409, "Switching maps requires simulator run resets.")
    if body.map not in available_maps():
        raise HTTPException(404, "Unknown map.")
    if body.map == app.state.deployment.map:
        return await sites(request)
    try:
        deployment = load_deployment(map_profile(body.map))
    except DeploymentError as e:
        raise HTTPException(500, str(e)) from e

    async def repoint(world) -> None:
        pipeline = getattr(app.state, "pipeline", None)
        if pipeline is not None:
            await pipeline.drain()
            await pipeline.set_world(world)
        app.state.receiver.set_world(world)

    await engine.switch_site(deployment, before_reset=repoint)
    app.state.deployment = deployment  # baseline for resource edits on this map
    return await sites(request)
