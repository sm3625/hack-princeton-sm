"""Map sites and the local geography the commander reasons with.

A deployment profile names a map (`map:`). Each map has a world file (nodes, sectors, no-fly,
ranger posts, terrain, activity zones), its own scenarios and its own OpenStreetMap files:

  laguna_del_tigre (default)  scenarios/world.yaml, scenarios/*.yaml, ui/geo/*.json
  <other>                     sites/<map>/world.yaml, sites/<map>/scenarios/*.yaml, ui/geo/<map>/*.json

Terrain, zones and posts are optional; a world without them gives no site notes, so the
original map behaves exactly as before.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from common.contracts import ROOT, World

DEFAULT_MAP = "laguna_del_tigre"
SITES_DIR = ROOT / "sites"
MAP_NAME = re.compile(r"^[a-z0-9_]{1,40}$")
_SECTOR = re.compile(r"^([A-Z])(\d{1,2})$")
MUSTER_MIN = 10  # time for a team to leave its post


@dataclass(frozen=True)
class SiteMap:
    key: str
    world_file: Path
    scenario_dir: Path
    geo_url: str  # served prefix for pilot_block.json and reserve.json


def site_map(key: str = DEFAULT_MAP) -> SiteMap:
    if key == DEFAULT_MAP:
        return SiteMap(key, ROOT / "scenarios" / "world.yaml", ROOT / "scenarios", "/ui/geo/")
    if not MAP_NAME.match(key or ""):
        raise ValueError(f"map '{key}' must be lower-case letters, digits or _")
    folder = SITES_DIR / key
    if not (folder / "world.yaml").is_file():
        known = ", ".join(available_maps())
        raise ValueError(f"no map '{key}' (sites/{key}/world.yaml). Available: {known}")
    return SiteMap(key, folder / "world.yaml", folder / "scenarios", f"/ui/geo/{key}/")


def available_maps() -> list[str]:
    others = sorted(p.parent.name for p in SITES_DIR.glob("*/world.yaml")) if SITES_DIR.is_dir() else []
    return [DEFAULT_MAP, *others]


def map_profile(key: str) -> str:
    """Deployment profile a map opens with when picked in the dashboard."""
    from common.deployment import DEFAULT_DEPLOYMENT

    return DEFAULT_DEPLOYMENT if key == DEFAULT_MAP else key


def map_summaries() -> list[dict]:
    """{map, title, region} for every map, for the dashboard's site picker."""
    import yaml

    out = []
    for key in available_maps():
        site = (yaml.safe_load(site_map(key).world_file.read_text()) or {}).get("site") or {}
        out.append({"map": key, "title": site.get("title", key), "region": site.get("region", "")})
    return out


def active_map() -> SiteMap:
    """The map of the profile named by $DEPLOYMENT."""
    from common.deployment import load_deployment  # deployment imports contracts; avoid a cycle

    return site_map(load_deployment().map)


# ---------- sector specs ----------


def _parse(name: str) -> tuple[int, int]:
    m = _SECTOR.match(name.strip().upper())
    if not m:
        raise ValueError(f"not a sector name: {name!r}")
    return ord(m.group(1)) - ord("A"), int(m.group(2)) - 1


def expand_sectors(spec: str | list[str]) -> set[str]:
    """'E4', 'A1:C6' (rectangle) or a list of either."""
    items = [spec] if isinstance(spec, str) else list(spec)
    out: set[str] = set()
    for item in items:
        item = str(item).strip().upper()
        if ":" in item:
            a, b = item.split(":", 1)
            (c0, r0), (c1, r1) = _parse(a), _parse(b)
            for r in range(min(r0, r1), max(r0, r1) + 1):
                for c in range(min(c0, c1), max(c0, c1) + 1):
                    out.add(f"{chr(ord('A') + c)}{r + 1}")
        else:
            _parse(item)
            out.add(item)
    return out


def _clock_min(text: str) -> int:
    h, m = text.strip().split(":")
    return int(h) * 60 + int(m)


def in_hours(hours: str | None, minute_of_day: int) -> bool:
    """'06:00-18:00' (may wrap midnight). None means all day."""
    if not hours:
        return True
    a, b = (_clock_min(t) for t in hours.split("-", 1))
    return a <= minute_of_day < b if a <= b else minute_of_day >= a or minute_of_day < b


# ---------- what the commander reads ----------


def expected_hours(zone: dict) -> dict[str, str | None]:
    """{sound: hours or None}. `expect` is a list (all share `hours`) or a {sound: hours} map."""
    expect = zone.get("expect") or {}
    if isinstance(expect, dict):
        return {str(k): (str(v) if v else None) for k, v in expect.items()}
    return {str(k): zone.get("hours") for k in expect}


def terrain_at(world: World, sector: str) -> list[dict]:
    return [t for t in world.terrain if sector in expand_sectors(t["sectors"])]


def zones_at(world: World, sector: str) -> list[dict]:
    return [z for z in world.zones if sector in expand_sectors(z["sectors"])]


def ranger_eta(world: World, x_m: float, y_m: float, sector: str, response_min: int) -> dict | None:
    """Nearest post and a terrain-adjusted travel estimate. None when the map has no posts."""
    if not world.ranger_posts:
        return None
    factor = 1.0
    for t in terrain_at(world, sector):
        factor *= float(t.get("ranger_factor", 1.0))
    best = None
    for p in world.ranger_posts:
        km = math.hypot(p["x_m"] - x_m, p["y_m"] - y_m) / 1000
        speed = float(p.get("speed_kmh", world.ranger_speed_kmh))
        mins = MUSTER_MIN + km / speed * 60 * factor
        if best is None or mins < best["eta_min"]:
            best = {"post": p["name"], "mode": p.get("mode", "foot"), "km": round(km, 1),
                    "eta_min": round(mins)}
    if best is not None:
        best["terrain_factor"] = round(factor, 2)
        best["profile_response_min"] = response_min
    return best


def site_notes(world: World, sector: str, x_m: float, y_m: float, sounds: set[str],
               minute_of_day: int, response_min: int,
               heard: dict[str, tuple[str, set[str]]] | None = None) -> list[str]:
    """Map facts for one incident. Empty for a map with no terrain, zones or posts.

    `heard` maps node name -> (its sector, sounds it reported). Zones are checked at the incident's
    estimated sector and at every node, since the estimate can fall just across a zone edge.
    """
    out = []
    for t in terrain_at(world, sector):
        line = f"Terrain in {sector}: {t['note']}"
        if float(t.get("ranger_factor", 1.0)) != 1.0:
            line += f" (ground travel x{float(t['ranger_factor']):g})"
        out.append(line + ".")
    spots = {"": (sector, sounds)} | (heard or {})
    for z in world.zones:
        cells = expand_sectors(z["sectors"])
        where = [(name, sec, snd) for name, (sec, snd) in spots.items() if sec in cells]
        if not where:
            continue
        places = sorted({f"{name} ({sec})" if name else sec for name, sec, _ in where})
        zone_sounds = set().union(*(snd for _, _, snd in where))
        line = f"Zone at {', '.join(places)}: {z['name']}. {z['note']}"
        hours = expected_hours(z)
        if hours:
            normal = ", ".join(f"{k} {v}" if v else f"{k} any time" for k, v in sorted(hours.items()))
            line += f" Normal here: {normal}."
            lawful = sorted(k for k in zone_sounds & hours.keys() if in_hours(hours[k], minute_of_day))
            odd = sorted(k for k in zone_sounds & hours.keys() if k not in lawful)
            if lawful:
                line += (f" Heard {', '.join(lawful)} is consistent with lawful activity at this hour; "
                         "look for other classes or a move off the zone.")
            if odd:
                line += f" Heard {', '.join(odd)} OUTSIDE its normal hours: not routine."
        outside = sorted(name for name, (sec, _) in spots.items() if name and sec not in cells)
        if outside and any(name for name, _, _ in where):
            line += f" Also heard outside this zone by {', '.join(outside)}: activity may cross its edge."
        out.append(line)
    eta = ranger_eta(world, x_m, y_m, sector, response_min)
    if eta:
        out.append(
            f"Nearest ranger post: {eta['post']} ({eta['mode']}), {eta['km']} km, "
            f"about {eta['eta_min']} min to reach {sector}."
        )
    return out
