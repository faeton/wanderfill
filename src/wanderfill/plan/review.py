"""Which unticked series objects may be shown to the owner as a question.

A series object, a museum, a capital or an airport, is offered only when a photo
taken **on the ground** is near it. Being near is not having been inside, so what
comes out of here is a question for the owner and never an op; but the owner
answers hundreds of them, and one wrong offer is one tired click from a false
tick on a public, ranked profile. So the rule of the module is that nothing is
offered which no ground photo shows.

A photo has four ways of not being one:

  * **It is somebody else's.** Not visible here; the caller reads the library with
    ``origins``, and every candidate names the origins of its proof.
  * **It was taken from an aircraft**, or carries the last coordinate of the ground
    for hours into the flight. The speed around it says so.
  * **It is impossible.** No aircraft makes the leg, so one end of it is wrong, and
    nothing says which.
  * **The owner says so**: a spoofed stretch, a layover, a stay that is not over.

Everything is a function of its arguments: photos, tiles and ticked lists in,
candidates out, in one order whatever order the photos came in. Nothing is cached,
and nothing here can write.

Objects that no photo is near do not leave this module, not as a list and not as
the nearest one: a page that shows them has proposed them.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, get_args

from ..api.errors import PlanInvalid
from ..geo.tiles import SeriesPoint, haversine_km, neighbourhood
from ..sources.photos_app import Origin, PhotoAsset
from .model import _r, parse_when
from .series import AIRPORT_SERIES

SKIP_SERIES = frozenset({-2, 12, 17, 60})

RADIUS_KM = 2.0
THIN_KM = 1.0

Kind = Literal["ground", "fast", "conflict", "untimed"]
Use = Literal["all", "airports", "none"]
SpanUse = Literal["none", "airports"]

_KM_PER_DEGREE = haversine_km(0.0, 0.0, 1.0, 0.0)
_MERCATOR_LAT = 85.05  # a web map has no tile beyond, and no y to compute at the pole


# -- what the owner says about stretches of photos -----------------------------


@dataclass(frozen=True)
class Span:
    """Dates and a box in which photos prove less than they seem to, or nothing.

    ``none`` is for a spoofed coordinate or a photo that is not theirs. ``airports``
    is for a layover: they were in the airport and nowhere else. ``west > east`` is
    a box across the antimeridian.
    """

    first: dt.date
    last: dt.date
    west: float
    south: float
    east: float
    north: float
    why: str
    use: SpanUse

    def covers(self, day: dt.date, lat: float, lon: float) -> bool:
        """Both dates and all four edges are inside."""
        if not (self.first <= day <= self.last and self.south <= lat <= self.north):
            return False
        east = self.east + 360 if self.west > self.east else self.east
        # 180 and -180 are one meridian, and a photo may be written either way.
        return any(self.west <= x <= east for x in (lon, lon - 360, lon + 360))


_SPAN_FIELDS = ("from", "to", "bbox", "why", "use")


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:  # json keeps the last, and the owner may have read the first
            raise ValueError(f"an object gives {_r(key)} twice")
        out[key] = value
    return out


def load_spans(path: str | Path) -> list[Span]:
    """The owner's spans file, or :class:`PlanInvalid` saying what is wrong with it.

    Strict, because a span that is skipped turns a spoofed photo back into proof:
    a misspelt field, a repeated one or a bare year is refused, never guessed at.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_no_repeats)
    except OSError as exc:
        raise PlanInvalid(f"{path}: cannot be read ({exc.strerror or exc})") from None
    except (ValueError, RecursionError) as exc:  # not JSON, not UTF-8, nested without end
        raise PlanInvalid(f"{path}: not a readable spans file ({exc})") from None
    if not isinstance(raw, list):
        raise PlanInvalid(f"{path}: a spans file is a JSON list, not {type(raw).__name__}")
    out = []
    for i, row in enumerate(raw):
        try:
            out.append(_span_of(row))
        except ValueError as exc:
            raise PlanInvalid(f"{path}: [{i}]: {exc}") from None
    return out


def _span_of(row: object) -> Span:
    if not isinstance(row, dict):
        raise ValueError(f"{_r(row)} is not an object")
    for name in _SPAN_FIELDS:
        if name not in row:
            raise ValueError(f"has no {name!r}")
    for name in row:
        if name not in _SPAN_FIELDS:
            raise ValueError(f"has a field {_r(name)}; a span has only {', '.join(_SPAN_FIELDS)}")
    first, last = _day(row, "from"), _day(row, "to")
    if first > last:
        raise ValueError(f"starts {first} after it ends {last}")
    box = row["bbox"]
    if not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) for v in box):
        raise ValueError(f"bbox must be [west, south, east, north] in degrees, not {_r(box)}")
    west, south, east, north = box
    # Written so that a NaN, which compares false with everything, is refused.
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= north <= 90):
        raise ValueError(f"bbox {_r(box)} is not [west, south, east, north] on this planet")
    why, use = row["why"], row["use"]
    if not isinstance(why, str) or not why.strip():
        raise ValueError(f"why must say something, not {_r(why)}")
    if not isinstance(use, str) or use not in get_args(SpanUse):
        raise ValueError(f"use must be one of {', '.join(get_args(SpanUse))}, not {_r(use)}")
    return Span(first, last, float(west), float(south), float(east), float(north), why, use)


def _day(row: dict, name: str) -> dt.date:
    try:
        day = parse_when(row[name])
    except ValueError as exc:
        raise ValueError(f"{name}: {exc}") from None
    if not isinstance(day, dt.date):  # absent, or a bare year
        raise ValueError(f"{name}: {_r(row[name])} is not YYYY-MM-DD")
    return day


# -- which photos are on the ground --------------------------------------------


@dataclass(frozen=True)
class Shot:
    """One photo, with what it may be used for.

    ``taken`` is the photo's own clock and dates it; ``taken_utc`` orders it.
    ``kind`` is what the speed around it says, ``use`` what the owner says about its
    place and day. Only a ``ground`` shot is proof, and only as far as ``use`` lets
    it; one with ``use`` ``none`` takes no part in anything. ``untimed`` is a photo
    with no UTC time, which cannot be shown to be slow or fast; its ``use`` is
    always ``none``. ``late`` says that ``use`` is ``none`` because the photo is
    after ``until``.
    """

    taken: dt.datetime
    taken_utc: dt.datetime | None
    lat: float
    lon: float
    origin: Origin
    kind: Kind
    use: Use
    late: bool = False


def grade_shots(
    assets: Iterable[PhotoAsset],
    spans: Sequence[Span],
    *,
    until: dt.date | None = None,
    fast_kmh: float = 120.0,
    min_leg_km: float = 1.0,
    impossible_kmh: float = 1200.0,
    impossible_km: float = 100.0,
) -> list[Shot]:
    """Every photo given, in UTC order and the untimed ones last, with its kind and use.

    Pass one camera's whole sequence. Speeds are taken over everything given, before
    ``spans`` and ``until`` are looked at, because a flight with its other end filtered
    away looks like a walk; and over one camera, because a photo somebody sent, between
    two of the owner's, makes speeds nobody travelled at, and hides ones somebody did.
    Grade each origin on its own and put the lists together.

    Consecutive photos at one coordinate are one place, entered at its first photo and
    left from its last, as in :func:`wanderfill.grade.looks_airborne`: a phone can repeat
    the departure lounge all the way through a flight, and the jump that ends it is
    evidence about every photo of the repeat.

    A place's speed is measured both ways in time, to the nearest place at least
    ``min_leg_km`` away. Nearer than that is the jitter of two photos seconds apart, but
    measuring only to the next photo would also call a burst from a window jitter, whose
    every leg is a few hundred metres. Over ``fast_kmh`` either way is fast. Over
    ``impossible_kmh`` across at least ``impossible_km`` is a conflict of both places,
    and so is any such pair with no time between them: one camera was not in both, and
    nothing says in which. A conflict outranks fast.

    In a stay that never leaves ``min_leg_km`` the photos near the landing, and near the
    departure, have only the flight to be measured to, and are fast until it is slow on
    average: hours, and after a long flight days. That errs toward offering less.

    What it cannot see: a gap long enough to hide the flight that is in it. A photo taken
    in the air hours after the one before it and hours before the next is slow both ways.
    That is why what comes out is a question for the owner and not a tick.
    """
    timed: list[PhotoAsset] = []
    untimed: list[PhotoAsset] = []
    for a in assets:
        (untimed if a.taken_utc is None else timed).append(a)

    def rest(a: PhotoAsset) -> tuple:
        return (a.lat, a.lon, a.origin, a.uuid)

    timed.sort(key=lambda a: (a.taken_utc, *rest(a)))
    untimed.sort(key=lambda a: (a.taken, *rest(a)))

    # Not `grade._runs`: it leaves coordinates of four decimals or fewer apart, because
    # there the question is whether a repeat counts as another photo. Here it is whether
    # the phone was still reporting, and a frozen coordinate can be a rounded one.
    places = [list(g) for _, g in groupby(timed, key=lambda a: (a.lat, a.lon))]
    at = [(p[0].lat, p[0].lon) for p in places]
    last = len(places) - 1
    # (the place measured, earlier, later): once forward in time, and once back.
    sides = [(n, n, m) for n, m in enumerate(_next_far(at, min_leg_km)) if m is not None]
    sides += [(last - n, last - m, last - n)
              for n, m in enumerate(_next_far(at[::-1], min_leg_km)) if m is not None]
    fast: set[int] = set()
    conflict: set[int] = set()
    for n, earlier, later in sides:
        a, b = places[earlier][-1], places[later][0]
        km = haversine_km(a.lat, a.lon, b.lat, b.lon)
        hours = (b.taken_utc - a.taken_utc).total_seconds() / 3600
        if hours <= 0 or (km >= impossible_km and km / hours > impossible_kmh):
            conflict |= {earlier, later}
        elif km / hours > fast_kmh:
            fast.add(n)

    out: list[Shot] = []
    for n, place in enumerate(places):
        kind: Kind = "conflict" if n in conflict else "fast" if n in fast else "ground"
        out += [_shot(a, kind, spans, until) for a in place]
    for a in untimed:
        out.append(replace(_shot(a, "untimed", spans, until), use="none"))
    return out


def _next_far(at: Sequence[tuple[float, float]], min_km: float) -> list[int | None]:
    """For each point, the index of the first later one at least ``min_km`` from it.

    A long stay is thousands of points with no such one among them, and looking at each
    from each is quadratic. So the points are also kept as blocks of 2, 4, 8… in a circle,
    and a block whose whole circle is too near is stepped over.
    """
    levels: list[list[tuple[float, float, float] | None]] = [[]]  # by k: blocks of 2**k
    while len(at) >> len(levels):
        k = len(levels)
        levels.append([
            _circle(at[j << k:(j + 1) << k], min_km)
            # A half that is too wide for a circle makes a block that is.
            if k == 1 or (levels[-1][2 * j] and levels[-1][2 * j + 1]) else None
            for j in range(len(at) >> k)
        ])
    out: list[int | None] = []
    for n, (lat, lon) in enumerate(at):
        i, k, found = n + 1, 1, None
        while i < len(at):
            if k:
                circle = None
                if k < len(levels) and not i & ((1 << k) - 1) and i >> k < len(levels[k]):
                    circle = levels[k][i >> k]
                if circle is None or haversine_km(lat, lon, *circle[:2]) + circle[2] >= min_km:
                    k -= 1
                else:
                    i += 1 << k
                    k += 1
            elif haversine_km(lat, lon, *at[i]) >= min_km:
                found = i
                break
            else:
                i, k = i + 1, 1
        out.append(found)
    return out


def _circle(points: Sequence[tuple[float, float]], under: float
            ) -> tuple[float, float, float] | None:
    """A centre and a radius around all the points; None where that is ``under`` or wider.

    The radius has a little added, so that no rounding puts a point outside.
    """
    first = points[0][1]
    lats = [lat for lat, _ in points]
    lons = [(lon - first + 180.0) % 360.0 - 180.0 for _, lon in points]  # over the meridian
    lat = (min(lats) + max(lats)) / 2
    lon = first + (min(lons) + max(lons)) / 2
    radius = 0.0
    for p in points:
        radius = max(radius, haversine_km(lat, lon, *p) + 1e-6)
        if radius >= under:
            return None
    return lat, lon, radius


def _shot(a: PhotoAsset, kind: Kind, spans: Sequence[Span], until: dt.date | None) -> Shot:
    late = until is not None and a.date > until
    uses = {s.use for s in spans if s.covers(a.date, a.lat, a.lon)}
    use: Use = "none" if late or "none" in uses else "airports" if uses else "all"
    return Shot(a.taken, a.taken_utc, a.lat, a.lon, a.origin, kind, use, late)


def left_out(shots: Iterable[Shot]) -> dict[str, int]:
    """How many of these photos are no proof, by reason, for the page to say.

    Each photo has one reason, the first that holds: ``no_utc``, ``after_until``,
    ``span_none``, ``conflict``, ``fast``; and ``span_airports``, a ground photo that
    is left out of every series but the airports. Every reason is always there. A
    photo of another origin was never read, so counting those is the caller's.
    """
    out = dict.fromkeys(
        ("fast", "conflict", "span_none", "span_airports", "after_until", "no_utc"), 0
    )
    for s in shots:
        if s.taken_utc is None:
            out["no_utc"] += 1
        elif s.late:
            out["after_until"] += 1
        elif s.use == "none":
            out["span_none"] += 1
        elif s.kind == "conflict":
            out["conflict"] += 1
        elif s.kind == "fast":
            out["fast"] += 1
        elif s.use == "airports":
            out["span_airports"] += 1
    return out


# -- which objects there are ---------------------------------------------------


def harvest(
    shots: Iterable[Shot],
    reader: Any,
    *,
    radius_km: float,
    zoom: int = 9,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[tuple[int, int], SeriesPoint], int]:
    """Objects by ``(series, item)`` around the photos, and how many tiles were not read.

    ``reader`` is a :class:`wanderfill.geo.tiles.TileReader`. Zoom 9 is the coarsest
    shown to be complete. Each photo's tile is read with the ring around it, and with
    more rings where tiles are narrower than the radius, which they are near the poles.
    A tile that could not be read is not "nothing there": the count goes to the owner.
    """
    _check_radius(radius_km)
    n = 2**zoom
    tiles: set[tuple[int, int]] = set()
    for s in shots:
        if s.use == "none":
            continue
        lat = max(-_MERCATOR_LAT, min(_MERCATOR_LAT, s.lat))
        poleward = min(_MERCATOR_LAT, abs(lat) + radius_km / _KM_PER_DEGREE)
        tile_km = 360.0 / n * _KM_PER_DEGREE * math.cos(math.radians(poleward))
        ring = max(1, math.ceil(min(radius_km / tile_km, n)))
        tiles |= {(x % n, y) for x, y in neighbourhood(lat, s.lon, zoom, ring) if 0 <= y < n}
    objects: dict[tuple[int, int], SeriesPoint] = {}
    unread = 0
    for done, (x, y) in enumerate(sorted(tiles), 1):
        for p in reader.series_points(zoom, x, y):
            objects.setdefault((p.series_id, p.id), p)
        unread += ("series2", zoom, x, y) in reader.failed
        if on_progress:
            on_progress(done, len(tiles))
    return objects, unread


# -- which of them a photo is near ---------------------------------------------


def _check_radius(radius_km: float) -> None:
    if not (math.isfinite(radius_km) and radius_km >= 0):
        raise ValueError(f"radius_km={radius_km!r} is not a distance in kilometres")


class _Near:
    """The shots within a radius of a point, without measuring to all of them.

    Rows of latitude as tall as the radius, each sorted by longitude. Cells of
    longitude would need a width, and there is none: a degree of it is 111 km at
    the equator and 38 km at 70° north. So the width is worked out for each point,
    and wraps at the antimeridian.
    """

    def __init__(self, shots: Iterable[Shot], radius_km: float):
        _check_radius(radius_km)
        self.radius_km = radius_km
        self.reach = radius_km / _KM_PER_DEGREE + 1e-7  # floats: the window may be wider
        self.height = max(self.reach, 0.01)
        rows: dict[int, list[tuple[float, Shot]]] = {}
        for s in shots:
            if s.use != "none":
                rows.setdefault(self._row(s.lat), []).append((_lon(s.lon), s))
        self.rows: dict[int, tuple[list[float], list[Shot]]] = {}
        for r, row in rows.items():
            row.sort(key=lambda e: e[0])
            self.rows[r] = ([x for x, _ in row], [s for _, s in row])

    def _row(self, lat: float) -> int:
        return math.floor(lat / self.height)  # int() makes one row of the two at 0.0

    def _width(self, lat: float) -> float:
        """Half the longitudes that can be in reach of ``lat``: all of them near a pole."""
        if abs(lat) + self.reach >= 90:
            return 180.0
        sine = math.sin(math.radians(self.reach)) / math.cos(math.radians(lat))
        return math.degrees(math.asin(min(1.0, sine))) + 1e-7

    def within(self, lat: float, lon: float) -> list[tuple[float, Shot]]:
        """``(km, shot)`` for each shot in the radius, the edge included, in no order."""
        x, width = _lon(lon), self._width(lat)
        # Three windows a turn apart, which touch once around and never overlap.
        windows = [(x - width + turn, x + width + turn) for turn in (-360.0, 0.0, 360.0)]
        if width >= 180.0:
            windows = [(-180.0, 180.0)]
        out = []
        for r in range(self._row(lat - self.reach), self._row(lat + self.reach) + 1):
            if r not in self.rows:
                continue
            lons, shots = self.rows[r]
            for lo, hi in windows:
                for s in shots[bisect_left(lons, lo):bisect_right(lons, hi)]:
                    km = haversine_km(lat, lon, s.lat, s.lon)
                    if km <= self.radius_km:
                        out.append((km, s))
        return out


def _lon(lon: float) -> float:
    return (lon + 180.0) % 360.0 - 180.0


def _proves(s: Shot, series: int) -> bool:
    """The one rule: a photo on the ground, and in a layover only for the airports."""
    if s.kind != "ground" or s.taken_utc is None:
        return False
    return s.use == "all" or (s.use == "airports" and series in AIRPORT_SERIES)


@dataclass(frozen=True)
class SeriesInfo:
    """One series as the live catalogue lists it: its title, and its objects by id."""

    name: str
    items: dict[int, str]


@dataclass(frozen=True)
class Candidate:
    """An unticked object with a ground photo near it: a question, not an answer.

    ``km`` and ``at``, the photo's own clock, are of the nearest ground photo.
    ``photos`` and ``days`` count the ground photos in the radius and ``origins`` is
    where those came from. ``fast_near`` counts fast photos in the radius, as a fact
    only. ``conflict`` means a photo of one of those days, here too, cannot be where
    it says. ``thin`` means one photo, or none within a kilometre.
    """

    series: int
    item: int
    series_name: str
    name: str
    lat: float
    lon: float
    km: float
    at: dt.datetime
    photos: int
    days: int
    origins: tuple[str, ...]
    fast_near: int
    conflict: bool
    thin: bool


def _ids(
    ticked: Mapping[Any, Iterable[Any]], catalogue: Iterable[int] = ()
) -> dict[int, set[int]]:
    """Ticked ids as numbers: the site answers them as text too, and text equals none.

    Every series of the catalogue must be there. One that is not was not read, and as an
    empty list it would have everything in it offered again.
    """
    out = {int(series): {int(i) for i in items} for series, items in ticked.items()}
    if unread := sorted(set(catalogue) - out.keys()):
        raise PlanInvalid("no ticked list for series " + ", ".join(map(str, unread))
                          + "; read it first, for a list that was not read is not an empty one")
    return out


def find_candidates(
    shots: Iterable[Shot],
    objects: Mapping[tuple[int, int], SeriesPoint],
    catalogue: Mapping[int, SeriesInfo],
    ticked: Mapping[int, Collection[int]],
    *,
    radius_km: float = RADIUS_KM,
    skip: Collection[int] = SKIP_SERIES,
) -> list[Candidate]:
    """The unticked catalogue objects with a ground photo in the radius.

    Sorted by series, distance and id. An object the catalogue does not list is not
    offered, whatever the tile says: a tick on it could not be read back. A catalogue
    series absent from ``ticked`` is one whose list was not read, and raises
    :class:`PlanInvalid`; a series with nothing ticked has an empty entry.
    """
    near, done = _Near(shots, radius_km), _ids(ticked, catalogue)
    out = []
    for (series, item), o in objects.items():
        info = catalogue.get(series)
        if series in skip or info is None or item not in info.items:
            continue
        if item in done.get(series, ()):
            continue
        close = near.within(o.lat, o.lon)
        proof = [(km, s) for km, s in close if _proves(s, series)]
        if not proof:
            continue
        km, at = min((km, s.taken_utc, s.taken) for km, s in proof)[::2]
        days = {s.taken.date() for _, s in proof}
        out.append(Candidate(
            series=series, item=item, series_name=info.name, name=info.items[item],
            lat=o.lat, lon=o.lon, km=km, at=at, photos=len(proof), days=len(days),
            origins=tuple(sorted({s.origin for _, s in proof})),
            fast_near=sum(s.kind == "fast" for _, s in close),
            conflict=any(s.kind == "conflict" and s.taken.date() in days for _, s in close),
            thin=len(proof) == 1 or km > THIN_KM,
        ))
    return sorted(out, key=lambda c: (c.series, c.km, c.item))


def count_skipped(
    shots: Iterable[Shot],
    objects: Mapping[tuple[int, int], SeriesPoint],
    ticked: Mapping[int, Collection[int]],
    *,
    radius_km: float = RADIUS_KM,
    skip: Collection[int] = SKIP_SERIES,
) -> dict[int, int]:
    """How many unticked objects of each skipped series a ground photo is near.

    So that "not offered" can be said with a number and not read as "nothing there".
    Numbers only, and without the catalogue, which nobody reads for a skipped series. A
    series absent from ``ticked`` is one whose list was not read: callers must treat it as
    that, for its count then has the ticked ones in it.
    """
    near, done = _Near(shots, radius_km), _ids(ticked)
    out: dict[int, int] = {}
    for (series, item), o in objects.items():
        if series not in skip or item in done.get(series, ()):
            continue
        if any(_proves(s, series) for _, s in near.within(o.lat, o.lon)):
            out[series] = out.get(series, 0) + 1
    return dict(sorted(out.items()))


def recall(
    shots: Iterable[Shot],
    objects: Mapping[tuple[int, int], SeriesPoint],
    ticked: Mapping[int, Collection[int]],
    radii: Sequence[float] = (0.2, 0.5, 1.0, 2.0),
    *,
    catalogue: Mapping[int, SeriesInfo],
) -> dict[int, dict]:
    """How much of what is already ticked the same photos find, per series.

    ``ticked`` is how many are ticked and ``located`` how many of those the harvest has
    an object for; ``found`` is, for each radius, how many of them a ground photo is
    near, by the rule the offers use. ``no_photo`` is the ids of the ticked items not
    found at the widest radius, the ones without an object included: those were in no
    tile that was read, which is no photo near or a tile that failed.

    A catalogue series absent from ``ticked`` is one whose list was not read, and raises
    :class:`PlanInvalid`: a recall printed without it would look complete.

    Recall, not precision: it says what the page will miss, not how often it is right.
    """
    near = _Near(shots, max(radii))
    out: dict[int, dict] = {}
    for series, items in sorted(_ids(ticked, catalogue).items()):
        nearest: dict[int, float] = {}
        located = [i for i in sorted(items) if (series, i) in objects]
        for i in located:
            o = objects[series, i]
            kms = [km for km, s in near.within(o.lat, o.lon) if _proves(s, series)]
            if kms:
                nearest[i] = min(kms)
        out[series] = {
            "ticked": len(items),
            "located": len(located),
            "found": {r: sum(km <= r for km in nearest.values()) for r in radii},
            "no_photo": [i for i in sorted(items) if i not in nearest],
        }
    return out


__all__ = [
    "RADIUS_KM",
    "SKIP_SERIES",
    "THIN_KM",
    "Candidate",
    "SeriesInfo",
    "Shot",
    "Span",
    "count_skipped",
    "find_candidates",
    "grade_shots",
    "harvest",
    "left_out",
    "load_spans",
    "recall",
]
