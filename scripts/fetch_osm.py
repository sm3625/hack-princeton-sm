"""Fetch OpenStreetMap geography for the pilot block and the reserve inset.

Run by hand, never in tests:  uv run python scripts/fetch_osm.py [--raw-dir DIR] [--only block|reserve]

Makes two Overpass requests (block features, reserve + park boundaries), converts them to small static
files that the dashboard loads from its own origin:
  ui/geo/pilot_block.json   features in local metres (x east, y north of the world origin)
  ui/geo/reserve.json       simplified reserve and park outlines in lon/lat, plus the pilot block box

Data (c) OpenStreetMap contributors, ODbL: https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from itertools import pairwise
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORLD = ROOT / "scenarios" / "world.yaml"
OUT = ROOT / "ui" / "geo"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "forest-guardian-demo/0.1"
M_PER_DEG = 111320.0
MARGIN_M = 600  # keep geometry this far outside the block
RESERVE_ID = 556176
PARK_ID = 7330557
TOL_BLOCK_M = 4.0
TOL_RESERVE_DEG = 0.0025

LINE_CLASS = {
    "river": "river",
    "stream": "stream",
    "canal": "canal",
    "drain": "stream",
    "ditch": "stream",
    "motorway": "road",
    "trunk": "road",
    "primary": "road",
    "secondary": "road",
    "tertiary": "road",
    "unclassified": "road",
    "residential": "road",
    "service": "road",
    "living_street": "road",
    "track": "track",
    "path": "path",
    "footway": "path",
    "bridleway": "path",
    "steps": "path",
    "cycleway": "path",
}
AREA_CLASS = {
    "water": "water",
    "wetland": "wetland",
    "wood": "wood",
    "scrub": "scrub",
    "grassland": "grass",
    "forest": "wood",
    "meadow": "grass",
    "farmland": "farm",
    "farmyard": "farm",
    "orchard": "farm",
}


def overpass(query: str, cache: Path | None) -> dict:
    if cache and cache.exists():
        return json.loads(cache.read_text())
    req = urllib.request.Request(
        OVERPASS, data=urllib.parse.urlencode({"data": query}).encode(), headers={"User-Agent": UA}
    )
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            break
        except Exception as exc:  # noqa: BLE001 - a few polite retries, then give up
            last = exc
            time.sleep(10 * (attempt + 1))
    else:
        raise SystemExit(f"Overpass failed: {last}")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    return data


# ---------- geometry helpers ----------


def dp(points: list[list[float]], tol: float, closed: bool = False) -> list[list[float]]:
    """Douglas-Peucker, iterative."""
    n = len(points)
    if n < 3:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            d = (
                math.hypot(px - ax, py - ay)
                if norm == 0
                else abs(dx * (ay - py) - dy * (ax - px)) / norm
            )
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    out = [p for p, k in zip(points, keep, strict=True) if k]
    if closed and len(out) < 4:
        return points[:: max(1, n // 4)] if n >= 4 else points
    return out


def clip_line(
    pts: list[list[float]], box: tuple[float, float, float, float]
) -> list[list[list[float]]]:
    """Clip a polyline to a box; returns the pieces that lie inside (Liang-Barsky per segment)."""
    x0, y0, x1, y1 = box
    pieces: list[list[list[float]]] = []
    cur: list[list[float]] = []
    for a, b in pairwise(pts):
        t0, t1 = 0.0, 1.0
        dx, dy = b[0] - a[0], b[1] - a[1]
        ok = True
        for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
                if t0 > t1:
                    ok = False
                    break
        if not ok:
            if len(cur) > 1:
                pieces.append(cur)
            cur = []
            continue
        pa = [a[0] + t0 * dx, a[1] + t0 * dy]
        pb = [a[0] + t1 * dx, a[1] + t1 * dy]
        if not cur:
            cur = [pa]
        elif cur[-1] != pa:
            cur.append(pa)
        cur.append(pb)
        if t1 < 1.0:
            pieces.append(cur)
            cur = []
    if len(cur) > 1:
        pieces.append(cur)
    return pieces


def clip_poly(ring: list[list[float]], box: tuple[float, float, float, float]) -> list[list[float]]:
    """Sutherland-Hodgman against an axis-aligned box."""
    x0, y0, x1, y1 = box

    def run(pts, inside, inter):
        out = []
        for i, cur in enumerate(pts):
            prev = pts[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(inter(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(inter(prev, cur))
        return out

    def ix(x):
        return lambda p, q: [x, p[1] + (q[1] - p[1]) * (x - p[0]) / (q[0] - p[0])]

    def iy(y):
        return lambda p, q: [p[0] + (q[0] - p[0]) * (y - p[1]) / (q[1] - p[1]), y]

    pts = ring[:-1] if ring and ring[0] == ring[-1] else ring
    for inside, inter in (
        (lambda p: p[0] >= x0, ix(x0)),
        (lambda p: p[0] <= x1, ix(x1)),
        (lambda p: p[1] >= y0, iy(y0)),
        (lambda p: p[1] <= y1, iy(y1)),
    ):
        if not pts:
            return []
        pts = run(pts, inside, inter)
    return pts


def stitch(ways: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join way fragments end to end into rings."""
    ways = [list(w) for w in ways if len(w) > 1]
    rings = []
    while ways:
        ring = ways.pop()
        while ring[0] != ring[-1]:
            for i, w in enumerate(ways):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[::-1][1:]
                elif w[-1] == ring[0]:
                    ring = w[:-1] + ring
                elif w[0] == ring[0]:
                    ring = w[::-1][:-1] + ring
                else:
                    continue
                ways.pop(i)
                break
            else:
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def pip(pt: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in pairwise(ring):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def geom_pts(g: list[dict]) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in g]


def element_rings(e: dict) -> tuple[list, list]:
    """(outer rings, inner rings) in lon/lat for a closed way or a multipolygon relation."""
    if e["type"] == "way":
        pts = geom_pts(e.get("geometry", []))
        return ([pts] if len(pts) >= 4 and pts[0] == pts[-1] else []), []
    outer, inner = [], []
    for m in e.get("members", []):
        if m["type"] == "way" and m.get("geometry"):
            (inner if m.get("role") == "inner" else outer).append(geom_pts(m["geometry"]))
    return stitch(outer), stitch(inner)


# ---------- block ----------


def load_world() -> dict:
    return yaml.safe_load(WORLD.read_text())


def block(raw: Path | None, world: dict) -> dict:
    lat0, lon0 = world["origin_lat"], world["origin_lon"]
    w_m, h_m = world["width_m"], world["height_m"]
    kx = M_PER_DEG * math.cos(math.radians(lat0))
    s, n = lat0 - MARGIN_M / M_PER_DEG, lat0 + (h_m + MARGIN_M) / M_PER_DEG
    w, e = lon0 - MARGIN_M / kx, lon0 + (w_m + MARGIN_M) / kx
    bb = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    query = f"""[out:json][timeout:120];
(
  way["waterway"~"river|stream|canal|drain|ditch"]{bb};
  way["natural"~"water|wetland|wood|scrub|grassland"]{bb};
  relation["natural"~"water|wetland|wood|scrub|grassland"]{bb};
  way["landuse"]{bb};
  relation["landuse"]{bb};
  way["highway"]{bb};
  node["place"]["name"]{bb};
  node["natural"~"peak|spring"]["name"]{bb};
);
out tags geom;"""
    data = overpass(query, raw / "block.json" if raw else None)

    def proj(p):
        return [(p[0] - lon0) * kx, (p[1] - lat0) * M_PER_DEG]

    box = (-MARGIN_M, -MARGIN_M, w_m + MARGIN_M, h_m + MARGIN_M)
    feats: list[dict] = []
    places: list[dict] = []
    for el in data["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name")
        if el["type"] == "node":
            x, y = proj((el["lon"], el["lat"]))
            places.append(
                {
                    "n": name,
                    "k": tags.get("place") or tags.get("natural"),
                    "x": round(x),
                    "y": round(y),
                }
            )
            continue
        cls = LINE_CLASS.get(tags.get("waterway") or tags.get("highway") or "")
        if cls and el["type"] == "way":
            pts = [proj(p) for p in geom_pts(el.get("geometry", []))]
            for piece in clip_line(pts, box):
                piece = dp(piece, TOL_BLOCK_M)
                if len(piece) >= 2:
                    f = {"t": "line", "c": cls, "p": [[round(a), round(b)] for a, b in piece]}
                    if name:
                        f["n"] = name
                    if tags.get("bridge") == "yes":
                        f["b"] = 1
                    feats.append(f)
            continue
        acls = AREA_CLASS.get(tags.get("natural", "")) or AREA_CLASS.get(tags.get("landuse", ""))
        if acls:
            outer, inner = element_rings(el)
            rings = []
            for ring in outer + inner:
                pr = [proj(p) for p in ring]
                pr = clip_poly(pr, box)
                if len(pr) >= 3:
                    pr = dp(pr + [pr[0]], TOL_BLOCK_M, closed=True)[:-1]
                    if len(pr) >= 3:
                        rings.append([[round(a), round(b)] for a, b in pr])
            if rings:
                f = {"t": "area", "c": acls, "r": rings}
                if name:
                    f["n"] = name
                feats.append(f)
    return {
        "origin": [lat0, lon0],
        "size_m": [w_m, h_m],
        "margin_m": MARGIN_M,
        "attribution": "(c) OpenStreetMap contributors, ODbL",
        "features": feats,
        "places": places,
    }


# ---------- reserve ----------


def reserve(raw: Path | None, world: dict) -> dict:
    data = overpass(
        f"[out:json][timeout:180];\nrelation(id:{RESERVE_ID},{PARK_ID});\nout geom;",
        raw / "reserve.json" if raw else None,
    )
    lat0, lon0 = world["origin_lat"], world["origin_lon"]
    out: dict = {"attribution": "(c) OpenStreetMap contributors, ODbL"}
    for el in data["elements"]:
        key = "reserve" if el["id"] == RESERVE_ID else "park"
        outer, _ = element_rings(el)
        rings = []
        for ring in outer:
            pts = dp([list(p) for p in ring], TOL_RESERVE_DEG, closed=True)
            if len(pts) >= 4:
                rings.append([[round(x, 4), round(y, 4)] for x, y in pts])
        rings.sort(key=lambda r: -len(r))
        out[key] = {"name": el["tags"].get("name"), "rings": rings}
        out[key + "_contains_pilot"] = any(pip((lon0, lat0), [tuple(p) for p in r]) for r in rings)
    out["pilot"] = {
        "lat": lat0,
        "lon": lon0,
        "w_m": world["width_m"],
        "h_m": world["height_m"],
    }
    out["places"] = [{"n": "Tikal", "lat": 17.2220, "lon": -89.6233}]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, help="cache raw Overpass responses here and reuse them")
    ap.add_argument("--only", choices=["block", "reserve"])
    args = ap.parse_args()
    world = load_world()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.only != "reserve":
        b = block(args.raw_dir, world)
        (OUT / "pilot_block.json").write_text(
            json.dumps(b, separators=(",", ":"), ensure_ascii=False)
        )
        kinds: dict[str, int] = {}
        for f in b["features"]:
            kinds[f["c"]] = kinds.get(f["c"], 0) + 1
        print("block", kinds, "places", len(b["places"]))
    if args.only != "block":
        r = reserve(args.raw_dir, world)
        (OUT / "reserve.json").write_text(json.dumps(r, separators=(",", ":"), ensure_ascii=False))
        print(
            "reserve contains pilot:",
            r.get("reserve_contains_pilot"),
            "park:",
            r.get("park_contains_pilot"),
        )
    for f in OUT.glob("*.json"):
        print(f.name, f.stat().st_size, "bytes")


if __name__ == "__main__":
    sys.exit(main())
