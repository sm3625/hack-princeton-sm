"""Per-sector weather (rain, wind) driven by scenario changes. Swappable for a real API later."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from common.contracts import Weather, World
from sim import world as worldmap

DEFAULT = Weather(rain=False, wind_kph=5.0)


@dataclass(frozen=True)
class WeatherChange:
    at: float  # seconds after scenario start
    sectors: tuple[str, ...]
    rain: bool | None = None  # None keeps the current value
    wind_kph: float | None = None


class WeatherModel:
    def __init__(
        self,
        world: World,
        publish: Callable[[dict], None] | None = None,
        default: Weather = DEFAULT,
    ) -> None:
        self.world = world
        self.default = default
        self._publish = publish or (lambda _msg: None)
        self._state: dict[str, Weather] = {}

    def reset(self) -> None:
        changed = [s for s, w in self._state.items() if w != self.default]
        self._state.clear()
        for sector in changed:
            self._emit(sector)

    def get(self, sector: str) -> Weather:
        return self._state.get(sector.strip().upper(), self.default)

    def apply(self, change: WeatherChange) -> list[str]:
        """Apply a change; publish one weather message per sector that actually changed."""
        changed = []
        for sector in change.sectors:
            old = self.get(sector)
            new = Weather(
                rain=old.rain if change.rain is None else bool(change.rain),
                wind_kph=old.wind_kph if change.wind_kph is None else float(change.wind_kph),
            )
            if new != old:
                self._state[sector] = new
                changed.append(sector)
                self._emit(sector)
        return changed

    def snapshot(self) -> list[dict]:
        """Sectors that differ from the default (for the UI on page load)."""
        return [
            self._message(s)
            for s in worldmap.sector_names(self.world)
            if self.get(s) != self.default
        ]

    def raining_sectors(self) -> list[str]:
        return [s for s in worldmap.sector_names(self.world) if self.get(s).rain]

    def _message(self, sector: str) -> dict:
        w = self.get(sector)
        return {"type": "weather", "sector": sector, "rain": w.rain, "wind_kph": w.wind_kph}

    def _emit(self, sector: str) -> None:
        self._publish(self._message(sector))
