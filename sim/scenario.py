"""Scenario files (scenarios/<name>.yaml) and the runner that plays them in sim time.

Plug-in point for the pipeline track:

    runner = ScenarioRunner(world, feed=node_runtime.feed, scenario=load_scenario("night1"),
                            clock=clock, weather=weather_model)
    await runner.run()

`feed(node_id, audio)` is called on the event-loop thread at each event's sim time with the
event's audio (float32 mono, 16 kHz, -1..1): the real clip from data/ when present, otherwise
a synthetic placeholder (logged as synthetic). Keep feed fast or hand work to a thread.
`on_event(event)` additionally receives the event itself; sim/demo_feed.py uses it with
feed=None to publish stub detections without audio.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from common.contracts import ROOT, SoundClass, World
from sim import audio as audiolib
from sim import world as worldmap
from sim.weather import WeatherChange, WeatherModel

log = logging.getLogger(__name__)

SCENARIO_DIR = ROOT / "scenarios"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SOUNDS = tuple(s.name.lower() for s in SoundClass)
LABELS = ("real", "false_alarm", "benign")
_DAY = 86_400

# YAML 1.1 reads unquoted 02:14 as the base-60 integer 134. Resolve clock times as strings.
_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


class _Loader(yaml.SafeLoader):
    pass


_Loader.yaml_implicit_resolvers = {
    k: list(v) for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _ch in "0123456789":
    _Loader.yaml_implicit_resolvers.setdefault(_ch, []).insert(
        0, ("tag:yaml.org,2002:str", _CLOCK_RE)
    )


class ScenarioError(ValueError):
    pass


class UnknownScenario(ScenarioError):
    pass


@dataclass(frozen=True)
class ScenarioEvent:
    at: float  # seconds after scenario start
    node: str  # node name
    node_id: int
    sound: str  # class the node is expected to report: chainsaw|gunshot|vehicle|axe|background
    duration_s: float = 5.0
    clip: str | None = None  # path under data/
    confidence: float = 0.9  # only used by the stub feed
    source: str = ""  # what really made the sound (truth for eval): chainsaw, rain, truck...
    label: str = "real"  # real | false_alarm | benign
    incident: str | None = None  # groups the records of one real incident
    note: str = ""


@dataclass(frozen=True)
class SightingSpec:
    sector: str
    text: str
    start: float | None = None  # seconds after scenario start
    end: float | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    start: float  # unix seconds (station local time)
    duration_s: float
    speed: float
    events: list[ScenarioEvent]
    weather: list[WeatherChange] = field(default_factory=list)
    sightings: list[SightingSpec] = field(default_factory=list)
    description: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration_s

    def summary(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "hours": round(self.duration_s / 3600, 1),
            "events": len(self.events),
        }


# ---------- parsing ----------


def _clock_seconds(text: str) -> int:
    parts = [int(p) for p in text.split(":")]
    h, m, s = (parts + [0])[:3]
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ScenarioError(f"bad clock time {text!r}")
    return h * 3600 + m * 60 + s


def _offset(value, start_tod: int, what: str) -> float:
    """'02:14:05' (clock time, wraps past midnight), '+1:30' (offset) or seconds -> offset."""
    if isinstance(value, bool):
        raise ScenarioError(f"{what}: bad time {value!r}")
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if text.startswith("+"):
        return float(_clock_seconds(text[1:]) if ":" in text else float(text[1:]))
    if not _CLOCK_RE.match(text):
        raise ScenarioError(f"{what}: bad time {value!r} (use 'HH:MM[:SS]')")
    return float((_clock_seconds(text) - start_tod) % _DAY)


def scenario_path(name: str, directory: Path = SCENARIO_DIR) -> Path:
    if not NAME_RE.match(name) or name == "world":
        raise UnknownScenario(f"unknown scenario {name!r}")
    path = directory / f"{name}.yaml"
    if not path.is_file():
        raise UnknownScenario(f"unknown scenario {name!r}")
    return path


def list_scenarios(directory: Path = SCENARIO_DIR, world: World | None = None) -> list[dict]:
    out = []
    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "world" or not NAME_RE.match(path.stem):
            continue
        try:
            out.append(load_scenario(path.stem, directory, world).summary())
        except ScenarioError as e:
            log.warning("skipping scenario %s: %s", path.name, e)
    return out


def load_scenario(
    name: str, directory: Path = SCENARIO_DIR, world: World | None = None
) -> Scenario:
    world = world or worldmap.load()
    data = yaml.load(scenario_path(name, directory).read_text(), Loader=_Loader) or {}
    try:
        return _parse(name, data, world)
    except (KeyError, TypeError) as e:
        raise ScenarioError(f"{name}: {e}") from e


def _parse(name: str, data: dict, world: World) -> Scenario:
    start_text = str(data.get("start", "22:00"))
    if not _CLOCK_RE.match(start_text):
        raise ScenarioError(f"{name}: start must be 'HH:MM'")
    start_tod = _clock_seconds(start_text)
    day = data.get("date", dt.date(2026, 11, 14))
    if isinstance(day, str):
        day = dt.date.fromisoformat(day)
    start_dt = dt.datetime.combine(day, dt.time()) + dt.timedelta(seconds=start_tod)
    duration = _offset(data.get("end", "06:00"), start_tod, "end") or _DAY

    events = []
    for i, raw in enumerate(data.get("events") or []):
        where = f"{name} event {i + 1}"
        try:
            node = worldmap.node_by_name(world, raw["node"])
        except KeyError as e:
            raise ScenarioError(f"{where}: {e}") from e
        sound = str(raw["sound"]).lower()
        label = str(raw.get("label", "real"))
        conf = float(raw.get("confidence", 0.9))
        dur = float(raw.get("duration", 5.0))
        if sound not in SOUNDS:
            raise ScenarioError(f"{where}: sound must be one of {', '.join(SOUNDS)}")
        if label not in LABELS:
            raise ScenarioError(f"{where}: label must be one of {', '.join(LABELS)}")
        if not (0 <= conf <= 1) or not (0 < dur <= 30):
            raise ScenarioError(f"{where}: confidence 0..1 and duration 0..30 s")
        at = _offset(raw["at"], start_tod, where)
        if at > duration:
            raise ScenarioError(f"{where}: after the scenario ends")
        events.append(
            ScenarioEvent(
                at=at,
                node=node.name,
                node_id=node.id,
                sound=sound,
                duration_s=dur,
                clip=raw.get("clip"),
                confidence=conf,
                source=str(raw.get("source", sound)),
                label=label,
                incident=raw.get("incident"),
                note=str(raw.get("note", "")),
            )
        )
    events.sort(key=lambda e: e.at)

    weather = []
    for i, raw in enumerate(data.get("weather") or []):
        where = f"{name} weather {i + 1}"
        try:
            sectors = tuple(worldmap.sectors_in(world, raw.get("sectors", "all")))
        except ValueError as e:
            raise ScenarioError(f"{where}: {e}") from e
        weather.append(
            WeatherChange(
                at=_offset(raw["at"], start_tod, where),
                sectors=sectors,
                rain=raw.get("rain"),
                wind_kph=raw.get("wind_kph"),
            )
        )
    weather.sort(key=lambda w: w.at)

    sightings = []
    for i, raw in enumerate(data.get("drone") or []):
        where = f"{name} drone {i + 1}"
        sector = str(raw["sector"]).upper()
        try:
            worldmap.parse_sector(world, sector)
        except ValueError as e:
            raise ScenarioError(f"{where}: {e}") from e
        sightings.append(
            SightingSpec(
                sector=sector,
                text=str(raw["see"]),
                start=_offset(raw["from"], start_tod, where) if "from" in raw else None,
                end=_offset(raw["until"], start_tod, where) if "until" in raw else None,
            )
        )

    speed = float(data.get("speed", 30))
    return Scenario(
        name=name,
        title=str(data.get("title", name)),
        start=start_dt.timestamp(),
        duration_s=duration,
        speed=speed,
        events=events,
        weather=weather,
        sightings=sightings,
        description=str(data.get("description", "")).strip(),
    )


# ---------- running ----------


class ScenarioRunner:
    def __init__(
        self,
        world: World,
        feed: Callable[[int, np.ndarray], None] | None,
        *,
        scenario: Scenario,
        clock,
        weather: WeatherModel | None = None,
        on_event: Callable[[ScenarioEvent], None] | None = None,
        data_dir: Path = audiolib.DATA_DIR,
    ) -> None:
        self.world = world
        self.feed = feed
        self.scenario = scenario
        self.clock = clock
        self.weather = weather
        self.on_event = on_event
        self.data_dir = data_dir
        self.fired = 0
        self.synthetic = 0
        self.finished = False

    def timeline(self) -> list[tuple[float, str, object]]:
        """(sim time, kind, item) in firing order; weather before events at the same instant."""
        start = self.scenario.start
        items: list[tuple[float, int, int, str, object]] = []
        for i, w in enumerate(self.scenario.weather):
            items.append((start + w.at, 0, i, "weather", w))
        for i, e in enumerate(self.scenario.events):
            items.append((start + e.at, 1, i, "event", e))
        items.sort(key=lambda x: x[:3])
        return [(t, kind, item) for t, _, _, kind, item in items]

    def audio_for(self, event: ScenarioEvent, index: int = 0) -> np.ndarray:
        path = audiolib.resolve_clip(event.clip, self.data_dir)
        if path is not None:
            try:
                return audiolib.load_clip(path, event.duration_s)
            except (OSError, RuntimeError, ValueError) as e:  # unreadable: fall back
                log.warning("could not read clip %s (%s); using synthetic audio", path.name, e)
        self.synthetic += 1
        node = self.world.node(event.node_id)
        rain = event.source == "rain" or bool(self.weather and self.weather.get(node.sector).rain)
        log.info(
            "synthetic audio: %s at %s (%s)",
            event.sound,
            event.node,
            f"clip {event.clip} not in data/" if event.clip else "no clip named",
        )
        return audiolib.synthesize(event.sound, event.duration_s, seed=index, rain=rain)

    def fire(self, kind: str, item, index: int = 0) -> None:
        if kind == "weather":
            if self.weather is not None:
                self.weather.apply(item)
            return
        event: ScenarioEvent = item
        self.fired += 1
        if self.feed is not None:
            try:
                self.feed(event.node_id, self.audio_for(event, index))
            except Exception:
                log.exception("feed failed for %s at %s", event.sound, event.node)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                log.exception("on_event failed for %s at %s", event.sound, event.node)

    async def run(self) -> None:
        for i, (t, kind, item) in enumerate(self.timeline()):
            await self.clock.sleep_until(t)
            self.fire(kind, item, i)
        await self.clock.sleep_until(self.scenario.end)
        self.finished = True
