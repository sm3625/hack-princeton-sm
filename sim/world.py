"""World map helpers on top of common.contracts.World (data in scenarios/world.yaml)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from common.contracts import Node, World, load_world
from common.sites import active_map, expand_sectors

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 in metres

SITE_KEYS = ("title", "region", "block", "inset_country", "inset_zone")
_SECTOR_RE = re.compile(r"^([A-Z])(\d{1,2})$")


def load(path: Path | None = None) -> World:
    """World for a file; with no path, the active deployment's map."""
    return _load(path or active_map().world_file)


@lru_cache(maxsize=8)
def _load(path: Path) -> World:
    return load_world(path)


def cols(world: World) -> int:
    return round(world.width_m / world.sector_m)


def rows(world: World) -> int:
    return round(world.height_m / world.sector_m)


def sector_names(world: World) -> list[str]:
    return [f"{chr(ord('A') + c)}{r + 1}" for r in range(rows(world)) for c in range(cols(world))]


def parse_sector(world: World, name: str) -> tuple[int, int]:
    """'B3' -> (col 1, row 2). Raises ValueError for names outside the grid."""
    m = _SECTOR_RE.match(name.strip().upper())
    if not m:
        raise ValueError(f"not a sector name: {name!r}")
    col = ord(m.group(1)) - ord("A")
    row = int(m.group(2)) - 1
    if not (0 <= col < cols(world) and 0 <= row < rows(world)):
        raise ValueError(f"sector outside the map: {name!r}")
    return col, row


def sector_rect(world: World, name: str) -> Rect:
    col, row = parse_sector(world, name)
    s = world.sector_m
    return (col * s, row * s, (col + 1) * s, (row + 1) * s)


def sector_center(world: World, name: str) -> tuple[float, float]:
    x0, y0, x1, y1 = sector_rect(world, name)
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def sectors_in(world: World, spec: str | list[str]) -> list[str]:
    """Expand 'all', 'A2:C5' (rectangle) or a list of names/ranges into sector names."""
    if isinstance(spec, str):
        spec = [spec]
    out: list[str] = []
    for item in spec:
        item = str(item).strip().upper()
        if item == "ALL":
            out.extend(sector_names(world))
        elif ":" in item:
            a, b = item.split(":", 1)
            (c0, r0), (c1, r1) = parse_sector(world, a), parse_sector(world, b)
            for r in range(min(r0, r1), max(r0, r1) + 1):
                for c in range(min(c0, c1), max(c0, c1) + 1):
                    out.append(f"{chr(ord('A') + c)}{r + 1}")
        else:
            parse_sector(world, item)
            out.append(item)
    return list(dict.fromkeys(out))


def node_by_name(world: World, name: str) -> Node:
    for n in world.nodes:
        if n.name.lower() == name.strip().lower():
            return n
    raise KeyError(f"no node named {name!r}")


def in_bounds(world: World, x: float, y: float) -> bool:
    return 0 <= x <= world.width_m and 0 <= y <= world.height_m


def to_json(world: World) -> dict:
    """Shape served at GET /api/world. ids are included for mapping only; the UI shows names."""
    return {
        "width_m": world.width_m,
        "height_m": world.height_m,
        "sector_m": world.sector_m,
        "cols": cols(world),
        "rows": rows(world),
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "x_m": n.x_m,
                "y_m": n.y_m,
                "lat": n.lat,
                "lon": n.lon,
                "sector": n.sector,
            }
            for n in world.nodes
        ],
        "no_fly": list(world.no_fly),
        "base": {"x_m": world.base_x_m, "y_m": world.base_y_m},
        "site": {k: v for k, v in world.site.items() if k in SITE_KEYS},
        "ranger_posts": [
            {k: p[k] for k in ("name", "x_m", "y_m", "mode") if k in p} for p in world.ranger_posts
        ],
        "terrain": [
            {"kind": t["kind"], "note": t["note"], "sectors": sorted(expand_sectors(t["sectors"]))}
            for t in world.terrain
        ],
        "zones": [
            {"name": z["name"], "kind": z["kind"], "sectors": sorted(expand_sectors(z["sectors"]))}
            for z in world.zones
        ],
    }
