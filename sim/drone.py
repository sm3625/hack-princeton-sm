"""Simulated drone: 10 m/s, battery %, no-fly avoidance, scripted camera observations.

Implements the drone half of contracts.Sim (plan_route / fly / drone_status). Position and
battery are pure functions of sim time and the current flight, so pausing or speeding up the
clock moves the drone consistently and the UI only has to draw what it is told.

Route semantics: Route.waypoints is the outbound path (current position -> target);
Route.distance_m is that outbound length; Route.battery_needed_pct covers out + hover + the
return leg to base. A route is feasible only if the battery left after it stays >= 15%.
"""

from __future__ import annotations

import heapq
import logging
import math
import threading
from dataclasses import dataclass
from itertools import pairwise

from common.contracts import DroneStatus, Route, World
from common.deployment import DroneSpec
from sim import world as worldmap
from sim.clock import SimClock

log = logging.getLogger(__name__)

SPEED_MPS = 10.0
BATTERY_PCT_PER_KM = 10.0
HOVER_S = 30.0  # time over the target taking pictures
HOVER_PCT = 2.0
RESERVE_PCT = 15.0
CHARGE_PCT_PER_MIN = 2.0  # on the pad at base
NO_FLY_MARGIN_M = 40.0
CAMERA_RADIUS_M = 300.0  # an observation matches if its sector is within this of the target
DEFAULT_OBSERVATION = "no activity seen"

Point = tuple[float, float]
Rect = worldmap.Rect
_EPS = 1e-3


@dataclass(frozen=True)
class Observation:
    """What the camera sees over a sector, optionally only between two sim times (unix s)."""

    sector: str
    text: str
    start: float | None = None
    end: float | None = None

    def active(self, t: float) -> bool:
        return (self.start is None or t >= self.start) and (self.end is None or t <= self.end)


@dataclass
class _Flight:
    out: list[Point]
    back: list[Point]
    out_m: float
    back_m: float
    depart: float
    battery_start: float
    observation: str
    gen: int
    speed_mps: float = SPEED_MPS
    pct_per_km: float = BATTERY_PCT_PER_KM

    @property
    def target(self) -> Point:
        return self.out[-1]

    @property
    def arrive(self) -> float:
        return self.depart + self.out_m / self.speed_mps

    @property
    def leave(self) -> float:
        return self.arrive + HOVER_S

    @property
    def land(self) -> float:
        return self.leave + self.back_m / self.speed_mps

    def phase(self, t: float) -> str:
        if t < self.arrive:
            return "out"
        if t < self.leave:
            return "hover"
        if t < self.land:
            return "return"
        return "landed"

    def position(self, t: float) -> Point:
        if t < self.arrive:
            return _along(self.out, (t - self.depart) * self.speed_mps)
        if t < self.leave:
            return self.target
        return _along(self.back, (t - self.leave) * self.speed_mps)

    def used_pct(self, t: float) -> float:
        flown = min(max(t - self.depart, 0.0) * self.speed_mps, self.out_m)
        hover = min(max(t - self.arrive, 0.0), HOVER_S) / HOVER_S if t > self.arrive else 0.0
        if t > self.leave:
            flown += min((t - self.leave) * self.speed_mps, self.back_m)
        return flown / 1000.0 * self.pct_per_km + hover * HOVER_PCT


# ---------- geometry ----------


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def path_length(path: list[Point]) -> float:
    return sum(_dist(a, b) for a, b in pairwise(path))


def _along(path: list[Point], d: float) -> Point:
    for a, b in pairwise(path):
        seg = _dist(a, b)
        if d <= seg:
            f = d / seg if seg else 1.0
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        d -= seg
    return path[-1]


def _inside(p: Point, r: Rect) -> bool:
    """Strictly inside (the boundary counts as outside)."""
    return r[0] < p[0] < r[2] and r[1] < p[1] < r[3]


def _inflate(r: Rect, m: float) -> Rect:
    return (r[0] - m, r[1] - m, r[2] + m, r[3] + m)


def segment_hits_rect(p: Point, q: Point, r: Rect) -> bool:
    """True if segment p-q passes through the open interior of r (Liang-Barsky clip)."""
    x0, y0, x1, y1 = r[0] + _EPS, r[1] + _EPS, r[2] - _EPS, r[3] - _EPS
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pk == 0:
            if qk < 0:
                return False
            continue
        t = qk / pk
        if pk < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return t1 - t0 > 1e-9


def rect_distance(p: Point, r: Rect) -> float:
    dx = max(r[0] - p[0], 0.0, p[0] - r[2])
    dy = max(r[1] - p[1], 0.0, p[1] - r[3])
    return math.hypot(dx, dy)


def shortest_path(
    start: Point,
    goal: Point,
    zones: list[Rect],
    bounds: Rect,
    margin: float = NO_FLY_MARGIN_M,
) -> list[Point] | None:
    """Shortest path around rectangular no-fly zones (visibility graph over inflated corners).

    Keeps `margin` metres clear of each zone, except where start or goal is already inside
    that margin (then the bare zone is used for that query).
    """
    obstacles = []
    for z in zones:
        inflated = _inflate(z, margin)
        near = _inside(start, inflated) or _inside(goal, inflated)
        obstacles.append(z if near else inflated)
    # A start inside a zone (should not happen) must still be able to leave it.
    obstacles = [o for o in obstacles if not _inside(start, o)]

    def visible(a: Point, b: Point) -> bool:
        return not any(segment_hits_rect(a, b, o) for o in obstacles)

    nodes: list[Point] = [start, goal]
    for o in obstacles:
        for c in ((o[0], o[1]), (o[2], o[1]), (o[2], o[3]), (o[0], o[3])):
            in_map = bounds[0] <= c[0] <= bounds[2] and bounds[1] <= c[1] <= bounds[3]
            if in_map and not any(_inside(c, other) for other in obstacles):
                nodes.append(c)

    best = {0: 0.0}
    prev: dict[int, int] = {}
    heap = [(0.0, 0)]
    done: set[int] = set()
    while heap:
        d, i = heapq.heappop(heap)
        if i in done:
            continue
        done.add(i)
        if i == 1:
            break
        for j, n in enumerate(nodes):
            if j in done or not visible(nodes[i], n):
                continue
            nd = d + _dist(nodes[i], n)
            if nd < best.get(j, math.inf):
                best[j] = nd
                prev[j] = i
                heapq.heappush(heap, (nd, j))
    if 1 not in done:
        return None
    path = [goal]
    i = 1
    while i != 0:
        i = prev[i]
        path.append(nodes[i])
    return path[::-1]


# ---------- drone ----------


class Drone:
    def __init__(self, world: World, clock: SimClock, spec: DroneSpec | None = None, drone_id: str = "drone-1") -> None:
        spec = spec or DroneSpec()
        self.world = world
        self.clock = clock
        self.id = drone_id
        self.speed_mps = spec.speed_mps
        self.pct_per_km = 100.0 / spec.range_km  # default spec: 10 km range = 10% per km
        self.reserve_pct = spec.reserve_pct
        bx = world.base_x_m if spec.base_x_m is None else spec.base_x_m
        by = world.base_y_m if spec.base_y_m is None else spec.base_y_m
        self.base: Point = (bx, by)
        self.zones: list[Rect] = [worldmap.sector_rect(world, s) for s in world.no_fly]
        self.bounds: Rect = (0.0, 0.0, world.width_m, world.height_m)
        self._lock = threading.RLock()
        self._observations: list[Observation] = []
        self._flight: _Flight | None = None
        self._gen = 0
        self.reset()

    # ----- setup -----

    def reset(self, battery_pct: float = 100.0) -> None:
        """Land at base with the given charge (cancels any flight in progress)."""
        with self._lock:
            self._gen += 1
            self._flight = None
            self._battery = float(battery_pct)
            self._battery_t = self.clock.now_f()

    def set_observations(self, observations: list[Observation]) -> None:
        with self._lock:
            self._observations = list(observations)

    # ----- state -----

    def _settle(self, t: float) -> None:
        f = self._flight
        if f is not None and t >= f.land:
            self._battery = max(0.0, f.battery_start - f.used_pct(f.land))
            self._battery_t = f.land
            self._flight = None

    def _battery_at(self, t: float) -> float:
        f = self._flight
        if f is not None:
            return max(0.0, f.battery_start - f.used_pct(t))
        charged = self._battery + CHARGE_PCT_PER_MIN * max(t - self._battery_t, 0.0) / 60.0
        return min(100.0, charged)

    def status(self) -> DroneStatus:
        with self._lock:
            t = self.clock.now_f()
            self._settle(t)
            x, y = self._flight.position(t) if self._flight else self.base
            return DroneStatus(
                x_m=round(x, 1),
                y_m=round(y, 1),
                battery_pct=round(self._battery_at(t), 1),
                flying=self._flight is not None,
                id=self.id,
            )

    def message(self) -> dict:
        """The {"type": "drone"} bus message for the current moment (extra keys for the map)."""
        with self._lock:
            s = self.status()
            msg: dict = {
                "type": "drone",
                "id": self.id,
                "x_m": s.x_m,
                "y_m": s.y_m,
                "battery_pct": s.battery_pct,
                "flying": s.flying,
                "phase": "idle",
            }
            f = self._flight
            if f is not None:
                phase = f.phase(self.clock.now_f())
                msg["phase"] = phase
                msg["target"] = list(f.target)
                msg["path"] = [list(p) for p in f.out + f.back[1:]]
                if phase in ("hover", "return"):
                    msg["observation"] = f.observation
            return msg

    # ----- planning -----

    def observation_at(self, target: Point, t: float) -> str:
        best: tuple[float, str] | None = None
        for o in self._observations:
            try:
                rect = worldmap.sector_rect(self.world, o.sector)
            except ValueError:
                continue
            d = rect_distance(target, rect)
            if o.active(t) and d <= CAMERA_RADIUS_M and (best is None or d < best[0]):
                best = (d, o.text)
        return best[1] if best else DEFAULT_OBSERVATION

    def plan_route(self, target_x_m: float, target_y_m: float) -> Route:
        target = (float(target_x_m), float(target_y_m))
        with self._lock:
            t = self.clock.now_f()
            self._settle(t)
            start = self._flight.position(t) if self._flight else self.base
            battery = self._battery_at(t)

            def refuse(reason: str, waypoints: list[Point] | None = None) -> Route:
                pts = waypoints or [start, target]
                return Route(
                    waypoints=[(round(x, 1), round(y, 1)) for x, y in pts],
                    distance_m=round(path_length(pts), 1),
                    battery_needed_pct=0.0,
                    feasible=False,
                    reason=reason,
                    drone_id=self.id,
                )

            if self._flight is not None:
                return refuse("drone is already flying")
            if not worldmap.in_bounds(self.world, *target):
                return refuse("target is outside the forest map")
            for name, zone in zip(self.world.no_fly, self.zones):
                if _inside(target, zone):
                    return refuse(f"target is inside no-fly sector {name}")
            out = shortest_path(start, target, self.zones, self.bounds)
            back = shortest_path(target, self.base, self.zones, self.bounds)
            if out is None or back is None:
                return refuse("no route around the no-fly sectors")
            out_m, back_m = path_length(out), path_length(back)
            needed = (out_m + back_m) / 1000.0 * self.pct_per_km + HOVER_PCT
            feasible = battery - needed >= self.reserve_pct
            reason = (
                ""
                if feasible
                else f"needs {needed:.0f}% plus {self.reserve_pct:.0f}% reserve; battery is "
                f"{battery:.0f}%"
            )
            return Route(
                waypoints=[(round(x, 1), round(y, 1)) for x, y in out],
                distance_m=round(out_m, 1),
                battery_needed_pct=round(needed, 1),
                feasible=feasible,
                reason=reason,
                drone_id=self.id,
            )

    # ----- flying -----

    def launch(self, route: Route) -> tuple[_Flight | None, str]:
        """Start a flight to the route's target. Returns (flight, "") or (None, why not)."""
        if not route.waypoints:
            return None, "route has no waypoints"
        target = route.waypoints[-1]
        with self._lock:
            fresh = self.plan_route(*target)  # never trust a stale or hand-made route
            if not fresh.feasible:
                return None, fresh.reason
            t = self.clock.now_f()
            out = [tuple(p) for p in fresh.waypoints]
            back = shortest_path(out[-1], self.base, self.zones, self.bounds) or [out[-1]]
            self._gen += 1
            flight = _Flight(
                out=out,
                back=back,
                out_m=path_length(out),
                back_m=path_length(back),
                depart=t,
                battery_start=self._battery_at(t),
                observation="",
                gen=self._gen,
                speed_mps=self.speed_mps,
                pct_per_km=self.pct_per_km,
            )
            flight.observation = self.observation_at(flight.target, flight.arrive)
            self._flight = flight
            log.info(
                "drone launched to %s (%.0f m out, %.0f%% battery)",
                self.world.sector_of(*flight.target),
                flight.out_m,
                fresh.battery_needed_pct,
            )
            return flight, ""

    def cancelled(self, flight: _Flight) -> bool:
        return self._gen != flight.gen
