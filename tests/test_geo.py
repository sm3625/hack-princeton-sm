"""The committed OpenStreetMap files match the world and stay small (made by scripts/fetch_osm.py)."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GEO = ROOT / "ui" / "geo"


def test_geo_files_match_world_origin_and_stay_small():
    world = yaml.safe_load((ROOT / "scenarios" / "world.yaml").read_text())
    block = json.loads((GEO / "pilot_block.json").read_text())
    reserve = json.loads((GEO / "reserve.json").read_text())
    assert block["origin"] == [world["origin_lat"], world["origin_lon"]]
    assert block["features"]
    assert reserve["pilot"]["lat"] == world["origin_lat"]
    assert reserve["pilot"]["lon"] == world["origin_lon"]
    assert reserve["reserve"]["rings"] and reserve["reserve_contains_pilot"]
    assert sum(f.stat().st_size for f in GEO.glob("*.json")) < 300_000
