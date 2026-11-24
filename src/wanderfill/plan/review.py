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

Everything but the last part is a function of its arguments: photos, tiles and ticked
lists in, candidates out, in one order whatever order the photos came in. The last part
reads the profile live. Nothing is cached, and nothing here can write to the profile.

A :class:`Manifest` is one such list with everything said beside it. The owner answers
it on a local page, and the answers name its hash, so that they are never read as
answers to another list. :func:`plan_for_series` makes the plan for one series from the
two, and :func:`plan_for_named` one for a thing the owner named without being asked.

Objects that no photo is near do not leave this module, not as a list and not as
the nearest one: a page that shows them has proposed them. One kind does, for it is
ticked already: the list names the ticked airports that no photo is near, and the page
shows those names, so that nobody takes its airports for all of them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from importlib.resources import files
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, get_args

from ..api.client import NomadMania, series_ticks
from ..api.client import _whole as _live_id
from ..api.errors import AccountMismatch, DriftError, PlanInvalid, VerificationFailed
from ..geo.tiles import SeriesPoint, haversine_km, neighbourhood
from ..sources.photos_app import ORIGINS, Origin, PhotoAsset
from .model import Op, Plan, _r, basis_of, fingerprint, parse_when, shown, validate
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
    burst_minutes: float = 10.0,
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

    A place's speed is measured each way in time, to the nearest place at least
    ``min_leg_km`` away. Nearer than that is the jitter of two photos seconds apart, and
    is looked past; measuring only to the next photo would call a burst from a window
    jitter, whose every leg is a few hundred metres. But it is looked past only while all
    its photos are within ``burst_minutes``. At a nearby photo farther off in time there
    is no speed that way, or a whole stay would be measured to the flight that ended in
    it. So after a flight the landing is fast and the minutes after it are, and an hour
    later in the same place is the ground; before a flight the same. The minutes are
    counted from the place's last photo forward, and from its first back: a run is graded
    whole, however long it lasted, by what it was left for and entered from.

    Over ``fast_kmh`` either way is fast. Over ``impossible_kmh`` across at least
    ``impossible_km`` is a conflict of both places, and so is any such pair with no time
    between them: one camera was not in both, and nothing says in which. A conflict
    outranks fast.

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
    fast: set[int] = set()
    conflict: set[int] = set()
    for n, earlier, later in _legs(places, min_leg_km, dt.timedelta(minutes=burst_minutes)):
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


def _legs(
    places: Sequence[Sequence[PhotoAsset]], min_km: float, burst: dt.timedelta
) -> Iterator[tuple[int, int, int]]:
    """``(place, earlier, later)`` for each place and each way there is a speed to take.

    Only the minutes of a burst are ever looked through, so a long stay costs what its
    densest minutes do, and not every photo of it for every other.
    """
    for n, here in enumerate(places):
        for step in (1, -1):
            m = n + step
            while 0 <= m < len(places):
                there = places[m]
                if haversine_km(here[0].lat, here[0].lon, there[0].lat, there[0].lon) >= min_km:
                    yield n, min(n, m), max(n, m)
                    break
                # To its far end: one nearby photo out of reach is enough.
                end = -1 if step == 1 else 0
                if abs(there[end].taken_utc - here[end].taken_utc) > burst:
                    break
                m += step


def _shot(a: PhotoAsset, kind: Kind, spans: Sequence[Span], until: dt.date | None) -> Shot:
    late = until is not None and a.date > until
    uses = {s.use for s in spans if s.covers(a.date, a.lat, a.lon)}
    use: Use = "none" if late or "none" in uses else "airports" if uses else "all"
    return Shot(a.taken, a.taken_utc, a.lat, a.lon, a.origin, kind, use, late)


def default_speeds() -> dict[str, float]:
    """What :func:`grade_shots` grades by unless told, by name, for the list to say."""
    return {name: grade_shots.__kwdefaults__[name] for name in _SPEEDS}


def grade_by_origin(
    assets: Iterable[PhotoAsset],
    spans: Sequence[Span],
    origins: Collection[str],
    *,
    since: dt.date,
    until: dt.date | None = None,
    **speeds: float,
) -> tuple[list[Shot], int]:
    """The shots of ``origins`` from ``since`` on, and how many photos were of another one.

    Each origin is graded on its own: the photos of two devices, one between the other's,
    are legs nobody travelled. Give it the whole library, earlier photos too: ``since``
    is taken off after the grading, for a landing on the first day is seen to be one only
    by the photo from before it.
    """
    mine: dict[str, list[PhotoAsset]] = {o: [] for o in sorted(origins)}
    other = 0
    for a in assets:
        if a.origin in mine:
            mine[a.origin].append(a)
        else:
            other += a.date >= since
    shots = [s for photos in mine.values()
             for s in grade_shots(photos, spans, until=until, **speeds)
             if s.taken.date() >= since]
    return shots, other


_LEFT_OUT = ("fast", "conflict", "span_none", "span_airports", "after_until", "no_utc")


def left_out(shots: Iterable[Shot]) -> dict[str, int]:
    """How many of these photos are no proof, by reason, for the page to say.

    Each photo has one reason, the first that holds: ``no_utc``, ``after_until``,
    ``span_none``, ``conflict``, ``fast``; and ``span_airports``, a ground photo that
    is left out of every series but the airports. Every reason is always there. A
    photo of another origin is no shot at all: :func:`grade_by_origin` counts those.
    """
    out = dict.fromkeys(_LEFT_OUT, 0)
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

    ``one_spot`` means the ground photos are of two days or more and all carry one
    identical raw coordinate. A spoofed receiver and a stale fix do that, and so does a
    phone on one desk, so it is a flag and an order and leaves nothing out. A source
    that rounds its coordinates repeats them anyway, and is flagged more.
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
    one_spot: bool


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


def _rank(c: Candidate) -> tuple:
    """The order of a list: see :func:`find_candidates`. The page has it by day as well."""
    return (c.series, c.conflict or c.one_spot, c.thin and c.series in AIRPORT_SERIES, c.km,
            c.item)


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

    Sorted by series, then the ones with no ``conflict`` and no ``one_spot`` before the
    ones with, then distance and id. Of the airports the ``thin`` ones come last in each of
    the two, for a single photo near one is what a transfer leaves. An object the catalogue
    does not list is not offered, whatever the tile says: a tick on it could not be read
    back. A catalogue series absent from ``ticked`` is one whose list was not read, and
    raises :class:`PlanInvalid`; a series with nothing ticked has an empty entry.
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
            one_spot=len(days) > 1 and len({(s.lat, s.lon) for _, s in proof}) == 1,
        ))
    return sorted(out, key=_rank)


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
    tile that was read, which is no photo near or a tile that failed. The airports also
    have ``no_photo_names``, the catalogue's names of those, so that nobody reads the list
    as complete. They are ticked: an unticked thing without a photo is named nowhere.

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
        if series in AIRPORT_SERIES and series in catalogue:
            names = catalogue[series].items
            out[series]["no_photo_names"] = {
                i: names[i] for i in out[series]["no_photo"] if i in names}
    return out


# -- what the owner was shown --------------------------------------------------

FORMAT = 1
ANSWERS_FORMAT = 1
MAX_ANSWERS_BYTES = 5_000_000

Answer = Literal["y", "n", "q"]

_PARAMS = ("since", "until", "radius_km", "origins", "skip", "speeds")
_SPEEDS = ("fast_kmh", "min_leg_km", "impossible_kmh", "impossible_km", "burst_minutes")
_RECALL = ("ticked", "located", "found", "no_photo")


@dataclass(frozen=True)
class Manifest:
    """One list of questions, with everything the owner was told beside it.

    ``sha256`` is what an answer is bound to. It is of the canonical JSON of all the
    fields but ``created``: the parameters, the coverage and the counts are part of the
    question, and a list rebuilt with nothing changed is the same list.

    ``params`` has ``since``, ``until`` (a date or ``None``), ``radius_km``, ``origins``,
    ``skip`` and ``speeds``, the arguments of :func:`grade_shots` by name. ``coverage``
    has ``tiles_read``, ``tiles_unread`` and ``recall``, which is what :func:`recall`
    returns, where a row may also have a ``name``, and ``no_photo_names`` may hold names of
    ids in ``no_photo`` of the airports and no others; and may have ``skipped``, from
    :func:`count_skipped`. ``left_out`` is what :func:`left_out` returns, and may have
    ``other_origin``, the photos of another origin, which were not used.

    Everything is checked here and put into the one form that is hashed, saved, loaded
    and shown: ids as text where they are keys, times as ISO text with no zone, sets
    sorted, coordinates to 5 decimals and distances to 3, so that no hash hangs on the
    last bit of a float. What does not fit raises :class:`PlanInvalid`.
    """

    account: int
    created: str
    params: dict
    coverage: dict
    left_out: dict
    candidates: list[Candidate]
    format: int = FORMAT

    def __post_init__(self) -> None:
        try:
            if type(self.format) is not int or self.format != FORMAT:
                raise ValueError(f"format {_r(self.format)} is not {FORMAT}, the one this reads")
            _whole(self.account, "account")
            if not isinstance(self.candidates, (list, tuple)):
                raise ValueError(f"candidates must be a list, not {_r(self.candidates)}")
            clean = {
                "created": _text(self.created, "created"),
                "params": _params_of(self.params),
                "coverage": _coverage_of(self.coverage),
                "left_out": _left_out_of(self.left_out),
                "candidates": [_candidate_of(_row_of(c), f"candidates[{i}]")
                               for i, c in enumerate(self.candidates)],
            }
            seen: set[str] = set()
            for c in clean["candidates"]:
                if (key := _key(c)) in seen:  # one answer would stand for two questions
                    raise ValueError(f"candidates: {key} is there twice")
                seen.add(key)
        except ValueError as exc:
            raise PlanInvalid(str(exc)) from None
        for name, value in clean.items():
            object.__setattr__(self, name, value)

    def _content(self) -> dict:
        return {
            "format": self.format, "account": self.account, "params": self.params,
            "coverage": self.coverage, "left_out": self.left_out,
            "candidates": [_row_of(c) for c in self.candidates],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical(self._content()).encode("utf-8")).hexdigest()

    def _stored(self) -> dict:
        """The file, and the page's data: the content, and what is not hashed beside it."""
        return {"sha256": self.sha256, "created": self.created, **self._content()}

    def to_json(self) -> str:
        return json.dumps(self._stored(), indent=1, ensure_ascii=False, allow_nan=False)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> Manifest:
        """Read what :meth:`save` wrote, or :class:`PlanInvalid` saying what is wrong with it.

        Anything else is refused, a number spelt another way included: the list on disk
        is the one the owner answered, or it is not.
        """
        raw = _read_json(path, "manifest")
        try:
            if not isinstance(raw, dict):
                raise ValueError(f"a manifest is a JSON object, not {type(raw).__name__}")
            # First, because a later format may have other fields and another hash.
            if "format" in raw and (type(raw["format"]) is not int or raw["format"] != FORMAT):
                raise ValueError(f"format {_r(raw['format'])} is not {FORMAT}, the one this reads")
            _fields(raw, ("sha256", "created", "format", "account", "params", "coverage",
                          "left_out", "candidates"), "the manifest")
            rows = raw["candidates"]
            if not isinstance(rows, list):
                raise ValueError(f"candidates must be a list, not {_r(rows)}")
            rest = {k: v for k, v in raw.items() if k not in ("sha256", "candidates")}
            m = cls(**rest, candidates=[_candidate_of(row, f"candidates[{i}]")
                                        for i, row in enumerate(rows)])
            if _canonical(raw) != _canonical(m._stored()):
                raise ValueError("its content is not what its sha256 was taken of; it was changed "
                                 "after it was written, so build the list again")
        except ValueError as exc:
            raise PlanInvalid(f"{path}: {exc}") from None
        return m


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _key(c: Candidate) -> str:
    """A candidate's name in the answers file, and in the page."""
    return f"{c.series}:{c.item}"


def _row_of(c: object) -> dict:
    """A candidate as JSON has it. Checks nothing; :func:`_candidate_of` does."""
    if not isinstance(c, Candidate):
        raise PlanInvalid(f"candidates: {_r(c)} is not a candidate")
    row = {f.name: getattr(c, f.name) for f in fields(c)}
    if isinstance(c.at, dt.datetime):
        row["at"] = c.at.isoformat()
    if isinstance(c.origins, tuple):
        row["origins"] = sorted(c.origins, key=str)
    return row


def _candidate_of(row: object, what: str) -> Candidate:
    """The one reading of a candidate, whichever way it came in."""
    _fields(row, [f.name for f in fields(Candidate)], what)
    out = {}
    for name in ("series", "item"):
        out[name] = _whole(row[name], f"{what}.{name}")
    for name in ("series_name", "name"):
        out[name] = _text(row[name], f"{what}.{name}")
    # Adding 0.0 makes -0.0 the zero it is.
    out["lat"] = round(_number(row["lat"], f"{what}.lat", low=-90.0, high=90.0), 5) + 0.0
    lon = round(_number(row["lon"], f"{what}.lon", low=-360.0, high=360.0), 5)
    out["lon"] = round(_lon(lon), 5)  # a tile's edge reaches past the antimeridian
    out["km"] = round(_number(row["km"], f"{what}.km"), 3)
    out["at"] = _at(row["at"], f"{what}.at")
    for name in ("photos", "days"):
        out[name] = _whole(row[name], f"{what}.{name}", low=1)
    if out["days"] > out["photos"]:
        raise ValueError(f"{what}.days: {out['days']} days of {out['photos']} photos")
    out["origins"] = tuple(_origins(row["origins"], f"{what}.origins"))
    out["fast_near"] = _whole(row["fast_near"], f"{what}.fast_near", low=0)
    for name in ("conflict", "thin", "one_spot"):
        if type(row[name]) is not bool:
            raise ValueError(f"{what}.{name} must be true or false, not {_r(row[name])}")
        out[name] = row[name]
    return Candidate(**out)


def _params_of(raw: object) -> dict:
    _fields(raw, _PARAMS, "params")
    speeds = raw["speeds"]
    _fields(speeds, _SPEEDS, "params.speeds")
    return {
        "since": _date(raw["since"], "params.since"),
        "until": None if raw["until"] is None else _date(raw["until"], "params.until"),
        "radius_km": _number(raw["radius_km"], "params.radius_km"),
        "origins": _origins(raw["origins"], "params.origins"),
        "skip": _whole_list(raw["skip"], "params.skip"),
        "speeds": {k: _number(speeds[k], f"params.speeds.{k}") for k in _SPEEDS},
    }


def _coverage_of(raw: object) -> dict:
    _fields(raw, ("tiles_read", "tiles_unread", "recall"), "coverage", ("skipped",))
    out = {name: _whole(raw[name], f"coverage.{name}", low=0)
           for name in ("tiles_read", "tiles_unread")}
    out["recall"] = {series: _recall_of(row, f"coverage.recall[{series}]")
                     for series, row in _by_id(raw["recall"], "coverage.recall").items()}
    for series, row in out["recall"].items():
        if "no_photo_names" in row and int(series) not in AIRPORT_SERIES:
            raise ValueError(f"coverage.recall[{series}].no_photo_names: only airports have them")
    if "skipped" in raw:
        out["skipped"] = {series: _whole(n, f"coverage.skipped[{series}]", low=0) for series, n
                          in _by_id(raw["skipped"], "coverage.skipped").items()}
    return out


def _recall_of(row: object, what: str) -> dict:
    _fields(row, _RECALL, what, ("name", "no_photo_names"))
    if not isinstance(row["found"], Mapping):
        raise ValueError(f"{what}.found must be an object, not {_r(row['found'])}")
    found: dict[str, int] = {}
    for radius, n in row["found"].items():
        km = radius
        if isinstance(radius, str):
            try:
                km = float(radius)
            except ValueError:
                pass
        key = str(_number(km, f"{what}.found, a radius"))
        if key in found or (isinstance(radius, str) and radius != key):
            raise ValueError(f"{what}.found: {_r(radius)} is not a radius, or is there twice")
        found[key] = _whole(n, f"{what}.found[{key}]", low=0)
    out = {name: _whole(row[name], f"{what}.{name}", low=0) for name in ("ticked", "located")}
    out |= {"found": found, "no_photo": _whole_list(row["no_photo"], f"{what}.no_photo")}
    if "name" in row:
        out["name"] = _text(row["name"], f"{what}.name")
    if "no_photo_names" in row:
        at = f"{what}.no_photo_names"
        out["no_photo_names"] = names = {
            i: _text(name, f"{at}[{i}]") for i, name in _by_id(row["no_photo_names"], at).items()}
        # A name of anything else would be of a thing that nobody has a photo near.
        if others := sorted(set(names) - set(map(str, out["no_photo"]))):
            raise ValueError(f"{at}: {', '.join(others)} not in no_photo")
    return out


def _left_out_of(raw: object) -> dict:
    _fields(raw, _LEFT_OUT, "left_out", ("other_origin",))
    return {name: _whole(n, f"left_out.{name}", low=0) for name, n in raw.items()}


def _fields(row: object, wanted: Sequence[str], what: str, optional: Sequence[str] = ()) -> None:
    if not isinstance(row, Mapping):
        raise ValueError(f"{what} must be an object, not {_r(row)}")
    for name in wanted:
        if name not in row:
            raise ValueError(f"{what} has no {name!r}")
    for name in row:
        if name not in wanted and name not in optional:
            raise ValueError(f"{what} has a field {_r(name)}, which this version does not know")


def _whole(value: object, what: str, *, low: int | None = None) -> int:
    # Within what JavaScript counts exactly: the page reads ids and writes them back.
    if type(value) is not int or abs(value) >= 2**53 or (low is not None and value < low):
        least = "" if low is None else f" of {low} or more"
        raise ValueError(f"{what} must be a whole number{least}, not {_r(value)}")
    return value


def _number(value: object, what: str, *, low: float = 0.0, high: float = math.inf) -> float:
    if type(value) in (int, float):
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if math.isfinite(number) and low <= number <= high:
            return number + 0.0
    raise ValueError(f"{what} must be a number from {low:g} to {high:g}, not {_r(value)}")


def _text(value: object, what: str) -> str:
    """``value`` with what UTF-8 cannot hold replaced: half a surrogate pair is no reason
    to refuse a list, and would be one to the file and to the hash."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, not {_r(value)}")
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _date(value: object, what: str) -> str:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value.isoformat()
    try:
        day = parse_when(value)
    except ValueError as exc:
        raise ValueError(f"{what}: {exc}") from None
    if not isinstance(day, dt.date):
        raise ValueError(f"{what}: {_r(value)} is not YYYY-MM-DD")
    return day.isoformat()


def _at(value: object, what: str) -> dt.datetime:
    """A photo's own clock, which has no zone, in the one spelling it is hashed in."""
    try:
        at = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        at = None
    if at is None or at.tzinfo is not None or at.isoformat() != value:
        raise ValueError(f"{what} must be YYYY-MM-DDTHH:MM:SS with no zone, not {_r(value)}")
    return at


def _listed(value: object, what: str) -> list:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{what} must be a list, not {_r(value)}")
    return list(value)


def _origins(value: object, what: str) -> list[str]:
    out = _listed(value, what)
    if not out or any(not isinstance(o, str) or o not in ORIGINS for o in out):
        raise ValueError(f"{what} must be some of {', '.join(sorted(ORIGINS))}, not {_r(value)}")
    if len(set(out)) != len(out):
        raise ValueError(f"{what}: {_r(value)} has one twice")
    return sorted(out)


def _whole_list(value: object, what: str) -> list[int]:
    out = [_whole(v, what) for v in _listed(value, what)]
    if len(set(out)) != len(out):
        raise ValueError(f"{what}: {_r(value)} has one twice")
    return sorted(out)


def _by_id(value: object, what: str) -> dict[str, Any]:
    """An object keyed by id, its keys as JSON has them."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be an object, not {_r(value)}")
    out: dict[str, Any] = {}
    for key, v in value.items():
        try:
            number = int(key) if isinstance(key, str) else key
        except ValueError:
            number = None
        name = str(_whole(number, f"{what}, the id {_r(key)}"))
        if name in out or (isinstance(key, str) and key != name):
            raise ValueError(f"{what}: {_r(key)} is not an id, or is there twice")
        out[name] = v
    return out


# -- the page, and what came back from it --------------------------------------

_DATA = "__DATA__"
_ESCAPES = {ord(c): f"\\u{ord(c):04x}" for c in "<>&\u2028\u2029"}


def render_page(manifest: Manifest) -> str:
    """The review page: one file that loads nothing, sends nothing and links to nothing.

    The manifest goes in as JSON, in which those five characters occur only inside
    strings, so escaping them changes no value and nothing in a name can end the script
    element it is in. The page writes every text with ``textContent``.
    """
    template = files(__package__).joinpath("review_page.html").read_text(encoding="utf-8")
    before, after = template.split(_DATA)  # exactly one, or this is another template
    return before + _canonical(manifest._stored()).translate(_ESCAPES) + after


def load_answers(path: str | Path, manifest: Manifest) -> dict[tuple[int, int], Answer]:
    """What the owner answered about ``manifest``, by ``(series, item)``.

    An item that is not in the file was not answered, and is not here: blank never
    becomes "no". A file that answers another list, or is for another account, or holds
    anything the page does not write, raises :class:`PlanInvalid`.
    """
    return read_answers(path, manifest)[0]


def read_answers(
    path: str | Path, manifest: Manifest
) -> tuple[dict[tuple[int, int], Answer], str]:
    """:func:`load_answers`, and the sha256 of the bytes that were read."""
    data = _read_bytes(path, "answers file", limit=MAX_ANSWERS_BYTES)
    raw = _json_of(data, path, "answers file")
    known = {_key(c): (c.series, c.item) for c in manifest.candidates}
    try:
        _fields(raw, ("format", "account", "manifest", "answers"), "the answers file")
        if type(raw["format"]) is not int or raw["format"] != ANSWERS_FORMAT:
            raise ValueError(f"format {_r(raw['format'])} is not {ANSWERS_FORMAT}, "
                             "the one this reads")
        if type(raw["account"]) is not int or raw["account"] != manifest.account:
            raise ValueError(f"these are answers for account {_r(raw['account'])}, and the list "
                             f"is for account {manifest.account}")
        if raw["manifest"] != manifest.sha256:
            raise ValueError("these are answers to another list than this one; open the page that "
                             "was made with this list, and answer that")
        answers = raw["answers"]
        if not isinstance(answers, dict):
            raise ValueError(f"answers must be an object, not {_r(answers)}")
        out: dict[tuple[int, int], Answer] = {}
        for key, value in answers.items():
            if key not in known:
                raise ValueError(f"answers: {_r(key)} is not on this list")
            if not isinstance(value, str) or value not in get_args(Answer):
                raise ValueError(f"answers: {key} must be one of {', '.join(get_args(Answer))}, "
                                 f"not {_r(value)}")
            out[known[key]] = value
    except ValueError as exc:
        raise PlanInvalid(f"{path}: {exc}") from None
    return out, hashlib.sha256(data).hexdigest()


def _read_json(path: str | Path, what: str) -> Any:
    return _json_of(_read_bytes(path, what), path, what)


def _read_bytes(path: str | Path, what: str, *, limit: int | None = None) -> bytes:
    try:
        with Path(path).open("rb") as f:
            data = f.read() if limit is None else f.read(limit + 1)
    except OSError as exc:
        raise PlanInvalid(f"{path}: cannot be read ({exc.strerror or exc})") from None
    if limit is not None and len(data) > limit:
        raise PlanInvalid(f"{path}: over {limit // 1_000_000} MB, which is no {what}")
    return data


def _json_of(data: bytes, path: str | Path, what: str) -> Any:
    def no_number(text: str) -> None:
        raise ValueError(f"{text} is not a number")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_repeats,
                          parse_constant=no_number)
    except (ValueError, RecursionError) as exc:  # not JSON, not UTF-8, nested without end
        raise PlanInvalid(f"{path}: not a readable {what} ({exc})") from None


# -- the series as it is now, read live -----------------------------------------


def read_series(client: NomadMania, series_id: int) -> tuple[SeriesInfo, set[int]]:
    """One series from one request: its catalogue, and the ids ticked in it.

    Strict, because a list that was not read must not come out as an empty one: with
    nothing ticked everything is offered again, and with no items nothing is there. What
    is missing or of another shape raises :class:`VerificationFailed`. Its text quotes
    nothing of the answer, which is full of names the owner may have no photo near.
    """
    action = f"V2/ series {series_id}"
    data = client.series(series_id)
    if not isinstance(data, dict):
        raise VerificationFailed(action, "the answer is not an object")
    title, rows = data.get("title"), data.get("items2")
    if not isinstance(title, str):
        raise VerificationFailed(action, "the answer has no title in text")
    if not isinstance(rows, list) or not rows:
        raise VerificationFailed(action, "the answer has no list of items in 'items2'")
    items: dict[int, str] = {}
    for n, row in enumerate(rows):
        item, name = (row.get("id"), row.get("name")) if isinstance(row, dict) else (None, None)
        if (number := _live_id(item)) is None or not isinstance(name, str):
            raise VerificationFailed(
                action, f"items2[{n}] is not an item with an 'id' that is a number and a 'name' "
                        "in text")
        name = _text(name, "name")
        if items.setdefault(number, name) != name:
            raise VerificationFailed(action, f"items2 has item {number} twice, with two names")
    return SeriesInfo(_text(title, "title"), items), series_ticks(action, data)


# -- from answers to a plan ------------------------------------------------------

def _km_text(km: float) -> str:
    return f"{round(km * 1000)} m" if km < 1 else f"{km:.2f}".rstrip("0").rstrip(".") + " km"


def _count(n: int, one: str) -> str:
    return f"{n} {one}" + ("" if n == 1 else "s")


def _photo_proof(c: Candidate) -> dict:
    """The facts of the page about one item, its flags with them, and nothing else."""
    detail = (f"nearest ground photo {_km_text(c.km)} away, {c.at:%Y-%m-%d %H:%M} by its own "
              f"clock; {_count(c.photos, 'photo')} on {_count(c.days, 'day')}, from "
              f"{', '.join(c.origins)}")
    flags = [text for flag, text in (
        (c.conflict, "conflict: a photo that day has an impossible jump"),
        (c.one_spot, "one spot: every photo, on several days, has one coordinate"),
        (c.thin, "thin: one photo, or over 1 km away"),
        (c.fast_near, f"{_count(c.fast_near, 'fast photo')} near, not counted")) if flag]
    return {"kind": "ground_photo", "detail": "; ".join([detail, *flags])}


def _tick(series: int, item: int, name: str, **kw: Any) -> Op:
    return Op("tick_series", bucket="apply", series=series, item=item,
              label=shown(name, f"item {item}"), confidence=0.0, **kw)


def _plan_of(
    client: NomadMania, account: int, series_id: int, ticked: set[int], ops: list[Op], **kw: Any
) -> Plan:
    snap = client.snapshot([])
    plan = Plan(account=account, ops=ops, basis={
        "fingerprint": fingerprint(basis_of(snap, [])),
        "series_ticked": {str(series_id): sorted(ticked)},
        "taken_at": snap["taken_at"],  # gives a plan built again a journal of its own
    }, **kw)
    if problems := validate(plan):
        raise PlanInvalid("the plan would not be valid: " + "; ".join(problems))
    return plan


def yes_items(
    manifest: Manifest, answers: Mapping[tuple[int, int], Answer], series_id: int
) -> list[Candidate]:
    """What the owner said yes to in one series. ``n``, ``q`` and no answer are not yes."""
    return [c for c in manifest.candidates
            if c.series == series_id and answers.get((c.series, c.item)) == "y"]


def plan_for_series(
    manifest: Manifest,
    answers: Mapping[tuple[int, int], Answer],
    series_id: int,
    client: NomadMania,
    *,
    answers_sha256: str | None = None,
) -> Plan:
    """The plan that ticks what the owner said yes to in one series of ``manifest``.

    The account and the series are read live. An item that is ticked by now is left out,
    and a plan may come back with no ops. A yes to an item that is gone, or whose id has
    another name than the one that was shown, refuses the whole plan with
    :class:`DriftError`: the series is not the one that was asked about.
    """
    if series_id in SKIP_SERIES or all(c.series != series_id for c in manifest.candidates):
        raise PlanInvalid(f"series {series_id} is not on this list")
    if (account := client.account_id()) != manifest.account:
        raise AccountMismatch(
            f"the list was made for account {manifest.account}, logged in as {account}")
    info, ticked = read_series(client, series_id)
    yes = yes_items(manifest, answers, series_id)
    for what, bad in (
        ("not in the series any more", [c for c in yes if c.item not in info.items]),
        ("under another name in the series now", [c for c in yes if c.item not in ticked
                                                  and info.items.get(c.item) != c.name]),
    ):
        if bad:
            names = ", ".join(f"{shown(c.name, '(no name)')} ({c.item})" for c in bad)
            raise DriftError(f"series {series_id}: {what}: {names}. No plan was made; run "
                             "`series review` again.")
    list_name = manifest.sha256[:12]
    ops = [_tick(c.series, c.item, c.name, method="series review: ground photos, owner's answer",
                 evidence=[f"The owner answered \"I was there\" on list {list_name}."],
                 proof=[_photo_proof(c)])
           for c in yes if c.item not in ticked]
    sources: list[dict] = [{"kind": "manifest", "sha256": manifest.sha256}]
    if answers_sha256:
        sources.append({"kind": "answers", "sha256": answers_sha256})
    sources.append({"kind": "photos", "origins": list(manifest.params["origins"])})
    return _plan_of(client, account, series_id, ticked, ops, sources=sources,
                    params=manifest.params)


def plan_for_named(series_id: int, name: str, said: str, client: NomadMania) -> Plan:
    """The plan that ticks the one item of a series whose name is exactly ``name``.

    ``said`` is the owner's own words, kept as they are. No ops when it is ticked already.
    A name that is not there, or is there twice, is refused with a text that has no name
    in it and nothing about how near it was: the nearest name is a proposal.
    """
    if series_id <= 0:
        raise PlanInvalid(f"{series_id} is not the id of a series")
    if not any(ch.isprintable() and not ch.isspace() for ch in said):
        raise PlanInvalid("what the owner said is empty, and it is the only evidence there is")
    if not name.strip():
        raise PlanInvalid("the name is empty")
    try:
        said.encode("utf-8")
    except UnicodeEncodeError:
        raise PlanInvalid("what the owner said is not text that a file can keep") from None
    account = client.account_id()
    info, ticked = read_series(client, series_id)
    items = [item for item, n in info.items.items() if n == name]
    if not items:
        raise PlanInvalid(f"series {series_id} has no item with exactly that name, letter for "
                          "letter. Nothing near it is looked for")
    if len(items) > 1:
        raise PlanInvalid(f"series {series_id} has more than one item with exactly that name, so "
                          "the name does not say which")
    ops = [_tick(series_id, item, name, method="named by the owner",
                 evidence=["The owner named this item themselves."],
                 proof=[{"kind": "memory", "detail": said}])
           for item in items if item not in ticked]
    return _plan_of(client, account, series_id, ticked, ops, sources=[{"kind": "memory"}])


__all__ = [
    "RADIUS_KM",
    "SKIP_SERIES",
    "THIN_KM",
    "Candidate",
    "Manifest",
    "SeriesInfo",
    "Shot",
    "Span",
    "count_skipped",
    "default_speeds",
    "find_candidates",
    "grade_by_origin",
    "grade_shots",
    "harvest",
    "left_out",
    "load_answers",
    "load_spans",
    "plan_for_named",
    "plan_for_series",
    "read_answers",
    "read_series",
    "recall",
    "render_page",
    "shown",
    "yes_items",
]
