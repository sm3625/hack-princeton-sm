"""Deployment profile: what resources a site has (drones, ranger teams, link, limits).

One YAML file per site in deployments/, chosen with the DEPLOYMENT env var (file name without
.yaml, default laguna_del_tigre_pilot). Unknown keys and out-of-range values are rejected, so a
typo fails at start-up instead of silently changing behaviour. The human approval gate for
dispatch is not a setting: there is no key to turn it off.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from common.contracts import ROOT
from common.sites import DEFAULT_MAP, site_map

load_dotenv(ROOT / ".env")

DEPLOYMENT_DIR = ROOT / "deployments"
DEFAULT_DEPLOYMENT = "laguna_del_tigre_pilot"
MAX_DRONES = 12
NO_AERIAL = "No aerial verification available."
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class DeploymentError(ValueError):
    """The profile is missing, unreadable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Site(_Strict):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class DroneSpec(_Strict):
    """One drone. range_km is the out-and-back distance a full battery covers (10 km = 10%/km)."""

    speed_mps: float = Field(default=10.0, gt=0, le=60)
    range_km: float = Field(default=10.0, gt=0, le=500)
    reserve_pct: float = Field(default=15.0, ge=0, le=60)
    base_x_m: float | None = Field(default=None, ge=0)  # None: the map's base (scenarios/world.yaml)
    base_y_m: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _base_pair(self) -> DroneSpec:
        if (self.base_x_m is None) != (self.base_y_m is None):
            raise ValueError("base_x_m and base_y_m must be given together")
        return self


class FleetUnit(_Strict):
    """Per-drone override: any field left out comes from `spec`."""

    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,24}$")
    speed_mps: float | None = Field(default=None, gt=0, le=60)
    range_km: float | None = Field(default=None, gt=0, le=500)
    reserve_pct: float | None = Field(default=None, ge=0, le=60)
    base_x_m: float | None = Field(default=None, ge=0)
    base_y_m: float | None = Field(default=None, ge=0)


class Drones(_Strict):
    count: int = Field(default=1, ge=0, le=MAX_DRONES)
    spec: DroneSpec = DroneSpec()
    units: list[FleetUnit] = []  # optional overrides, matched to drones by position

    @model_validator(mode="after")
    def _units_fit(self) -> Drones:
        if len(self.units) > self.count:
            raise ValueError(f"{len(self.units)} units listed but count is {self.count}")
        ids = [u.id for u in self.units if u.id]
        if len(ids) != len(set(ids)):
            raise ValueError("drone ids must be unique")
        return self


class RangerTeams(_Strict):
    count: int = Field(default=1, ge=1, le=50)
    response_min: int = Field(default=45, ge=1, le=1440)  # typical time to reach a sector


class Approval(_Strict):
    """Who signs off on a dispatch. The gate itself is always on and is not configurable."""

    approver: str = Field(default="duty ranger", min_length=1, max_length=60)


class AgentLimits(_Strict):
    max_followup_checks: int | None = Field(default=None, ge=1, le=100)  # per incident; None: no cap
    check_interval_min: int = Field(default=30, ge=1, le=240)


class Deployment(_Strict):
    site: Site
    map: str = DEFAULT_MAP  # world, scenarios and OSM files (common/sites.py)
    drones: Drones = Drones()
    ranger_teams: RangerTeams = RangerTeams()
    connectivity: Literal["lora", "satellite", "cellular"] = "lora"  # informational
    approval: Approval = Approval()
    agent: AgentLimits = AgentLimits()
    name: str = Field(default=DEFAULT_DEPLOYMENT, exclude=True)  # file name, set by the loader

    @model_validator(mode="after")
    def _map_exists(self) -> Deployment:
        site_map(self.map)  # ValueError names the available maps
        return self

    @property
    def has_drones(self) -> bool:
        return self.drones.count > 0

    def drone_specs(self) -> list[tuple[str, DroneSpec]]:
        """(id, fully resolved spec) per drone, ids drone-1, drone-2, ... unless a unit names one."""
        out = []
        for i in range(self.drones.count):
            unit = self.drones.units[i] if i < len(self.drones.units) else FleetUnit()
            over = {k: v for k, v in unit.model_dump(exclude={"id"}).items() if v is not None}
            merged = DroneSpec.model_validate({**self.drones.spec.model_dump(), **over})
            out.append((unit.id or f"drone-{i + 1}", merged))
        return out

    def resources_line(self) -> str:
        """One short line for the commander's prompt."""
        teams = self.ranger_teams
        drones = f"{self.drones.count} drone(s)" if self.has_drones else "no drone"
        return (
            f"Site: {self.site.name}. Resources: {drones}; {teams.count} ranger team(s), "
            f"about {teams.response_min} min to reach a sector; link {self.connectivity}; "
            f"dispatch approved by {self.approval.approver}."
        )

    def public(self) -> dict:
        """Read-only view for GET /api/deployment. Nothing secret lives in a profile."""
        return {
            "name": self.name,
            "map": self.map,
            "site": self.site.model_dump(),
            "drones": {
                "count": self.drones.count,
                "units": [{"id": i, **spec.model_dump()} for i, spec in self.drone_specs()],
            },
            "ranger_teams": self.ranger_teams.model_dump(),
            "connectivity": self.connectivity,
            "approval": {"approver": self.approval.approver, "required": True},
            "agent": self.agent.model_dump(),
            "aerial_verification": self.has_drones,
        }


def load_deployment(name: str | None = None, directory: Path = DEPLOYMENT_DIR) -> Deployment:
    """Read and validate a profile. name defaults to $DEPLOYMENT, then the pilot profile."""
    name = (name or os.getenv("DEPLOYMENT") or DEFAULT_DEPLOYMENT).strip()
    if not _NAME.match(name):
        raise DeploymentError(f"DEPLOYMENT '{name}' must be a file name like laguna_del_tigre_pilot.")
    path = directory / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in directory.glob("*.yaml"))) or "none"
        raise DeploymentError(f"No deployment profile '{name}' in {directory.name}/. Available: {known}.")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise DeploymentError(f"{path.name}: not valid YAML ({e})") from e
    if not isinstance(data, dict):
        raise DeploymentError(f"{path.name}: expected a mapping at the top level.")
    try:
        return Deployment.model_validate({**data, "name": name})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise DeploymentError(f"{path.name}: {problems}") from e
