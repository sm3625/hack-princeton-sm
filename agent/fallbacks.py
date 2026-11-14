"""Stand-ins used when the real event store or simulator is not wired in (dev, tests).

They implement the protocols in common/contracts.py and nothing more.
"""

from __future__ import annotations

import math
import threading
import time

from common.contracts import Detection, DroneStatus, Route, Weather, World


class MemoryEventStore:
    """In-memory EventStore. Keeps raw bytes so signatures can be re-checked."""

    def __init__(self) -> None:
        self._rows: dict[tuple[int, int], tuple[Detection, bytes]] = {}
        self._lock = threading.Lock()

    def add(self, detection: Detection, raw: bytes) -> bool:
        key = (detection.node_id, detection.seq)
        with self._lock:
            if key in self._rows:
                return False
            self._rows[key] = (detection, raw)
            return True

    def query(
        self,
        node_ids: list[int] | None = None,
        since: int | None = None,
        until: int | None = None,
        verified_only: bool = True,
    ) -> list[Detection]:
        with self._lock:
            rows = [d for d, _ in self._rows.values()]
        out = [
            d
            for d in rows
            if (node_ids is None or d.node_id in node_ids)
            and (since is None or d.timestamp >= since)
            and (until is None or d.timestamp <= until)
            and (d.verified or not verified_only)
        ]
        return sorted(out, key=lambda d: (d.timestamp, d.node_id, d.seq))

    def raw(self, node_id: int, seq: int) -> bytes | None:
        with self._lock:
            row = self._rows.get((node_id, seq))
        return row[1] if row else None


class ClockSim:
    """Wall clock, dry weather, straight-line drone with no camera. Dev fallback only."""

    SPEED_MPS = 10.0
    PCT_PER_KM = 8.0

    def __init__(self, world: World) -> None:
        self.world = world
        self._x, self._y = world.base_x_m, world.base_y_m
        self._battery = 100.0

    def now(self) -> int:
        return int(time.time())

    def weather(self, sector: str) -> Weather:
        return Weather(rain=False, wind_kph=0.0)

    def plan_route(self, target_x_m: float, target_y_m: float) -> Route:
        dist = 2 * math.hypot(target_x_m - self._x, target_y_m - self._y)
        need = dist / 1000 * self.PCT_PER_KM
        sector = self.world.sector_of(target_x_m, target_y_m)
        if sector in self.world.no_fly:
            return Route([], dist, need, False, f"{sector} is a no-fly zone")
        if need > self._battery - 10:
            return Route([], dist, need, False, "not enough battery to return")
        return Route([(self._x, self._y), (target_x_m, target_y_m)], dist, need, True)

    def fly(self, route: Route) -> str:
        if route.feasible:
            self._battery -= route.battery_needed_pct
        return "No camera feed: simulator not connected."

    def drone_status(self) -> DroneStatus:
        return DroneStatus(self._x, self._y, self._battery, False)
