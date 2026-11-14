"""View site resources and adjust them for the next simulator run."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from common.deployment import MAX_DRONES

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
