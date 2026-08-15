"""Location-history sources.

Everything reduces to the same tiny shape: a date, a coordinate, and a note of
where the evidence came from. A photo library, a GPX file, a Google Timeline
export and a CSV somebody typed by hand all become a list of ``DayPoint``.

The rule for contributors is deliberately strict: a new source is a parser that
emits ``DayPoint`` objects. It must not add an HTTP client for a third party's
private API. Reverse-engineering one undocumented service is a maintenance
burden worth carrying; two is how projects die.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DayPoint:
    """One day, one coordinate, and where it came from.

    ``place`` matters more than it looks. For series that are about *being in a
    city*, a reverse-geocoded place name is far stronger evidence than the
    distance between two coordinates: a metro area's marker can sit fifteen
    kilometres from anywhere a person stays, and passing three kilometres from a
    town on a motorway is not visiting it.
    """

    date: dt.date
    lat: float
    lon: float
    place: str = ""
    country: str = ""
    source: str = ""
    weight: int = 1  # e.g. how many photos back this point up

    @property
    def key(self) -> tuple[float, float]:
        """Coordinates rounded for deduplication before geocoding.

        A 4,868-day track is usually only about two thousand distinct points.
        Geocoding the duplicates is the difference between minutes and an hour.
        """
        return (round(self.lat, 3), round(self.lon, 3))


@runtime_checkable
class Source(Protocol):
    """Anything that can produce a track."""

    name: str

    def points(self) -> Iterable[DayPoint]:  # pragma: no cover - protocol
        ...


@dataclass
class Track:
    """A whole location history, plus the provenance needed for a plan file."""

    points: list[DayPoint] = field(default_factory=list)
    origin: str = ""

    def __len__(self) -> int:
        return len(self.points)

    @property
    def span(self) -> tuple[dt.date, dt.date] | None:
        if not self.points:
            return None
        ds = [p.date for p in self.points]
        return min(ds), max(ds)

    def distinct_coords(self) -> dict[tuple[float, float], list[DayPoint]]:
        out: dict[tuple[float, float], list[DayPoint]] = {}
        for p in self.points:
            out.setdefault(p.key, []).append(p)
        return out

    def places(self) -> dict[str, list[dt.date]]:
        """Normalised place name -> the days it was seen. Used for city series."""
        from .normalize import fold

        out: dict[str, list[dt.date]] = {}
        for p in self.points:
            if p.place:
                out.setdefault(fold(p.place), []).append(p.date)
        return out
