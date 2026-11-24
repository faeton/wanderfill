"""Which days of photos may be shown to the owner as a stay, and which stays as a trip.

A run is the days of photos inside one region. It is offered only where nothing speaks
against it: a run that is not over, that a visit on the profile may already be, or that may
be one half of a longer stay is **held**, with its reason. The dangerous error is claiming
too much: a region, a day or a visit that did not happen, or one that is there already. A
missed visit can be added later; a wrong one only the owner can remove.

Three words are kept apart. An *observation* is one photo. A *run* is days of photos inside
one region. An *episode* is one stay, and nothing here knows one: only the profile and the
owner do. So two runs with nothing seen elsewhere between them are not two visits, however
long the camera was silent, and a visit that nobody dated may be any of them.

Days are the photo's own local dates, because those are what a visit has. Order is by UTC,
because over the date line the later photo can have the earlier date.

Everything is a function of its arguments, in one order whatever order the photos came in.
``obs`` is always the whole library, graded as one camera: a run is seen to be over by a
photo from after the dates that were asked for.

The numbers in the docstrings are the decisions of the trip planner's design.
"""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from itertools import pairwise
from typing import Literal, Protocol, TypedDict

from .. import evidence
from ..api.client import Visit, When, YearOnly
from ..api.errors import PlanInvalid
from ..geo.resolve import Resolution
from ..geo.tiles import haversine_km
from ..sources.photos_app import PhotoAsset, zone_fits
from .model import _r
from .review import Shot, default_speeds

GAP_DAYS = 2
SWEEP_GAPS = (1, 2, 3, 5)

State = Literal["inside", "edge", "nearest", "nowhere", "unread", "stale"]
Status = Literal["offered", "held", "fixed", "there"]

# Reasons and flags are text that starts with one of these and, mostly, a colon. Words of
# the owner's release a run whose reasons are all of the first kind, and nothing releases one
# with a reason of the second. An overlap also cuts the trip it is in.
RELEASED_BY_WORDS = frozenset({"open", "brief", "layover?", "undated"})
NEVER_RELEASED = frozenset({"overlap", "continuation", "not read", "stale map", "across",
                            "clock", "not judged"})

TrackRow = tuple[dt.date, int | None]
"""A day-level row and its region, or ``None`` where it was not placed in one."""

_DAY = dt.timedelta(days=1)
_AROUND_KM = 0.1
_KM_PER_DEGREE = haversine_km(0.0, 0.0, 1.0, 0.0)
_BRIEF = dt.timedelta(minutes=15)
_LAYOVER = dt.timedelta(hours=24)
_FAST_NEAR = dt.timedelta(hours=6)
_CLOCKS_APART = 12 * 3600
_NIGHT = dt.timedelta(hours=2)


def kind_of(text: str) -> str:
    """The kind of a reason, a flag or a suggestion."""
    return text.partition(":")[0]


# -- where a photo is ------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    """The region a coordinate is in, and how surely.

    ``inside`` is strictly inside; ``edge`` is inside with another region, or a tile that
    was not read, within a hundred metres; ``nearest`` is in no region, beside this one.
    The other three have no region: no land near, a tile that was not read, and a polygon
    that the catalogue does not have.
    """

    region: int | None
    state: State


def place(resolve: Callable[[float, float], Resolution], lat: float, lon: float) -> Place:
    """Where a raw coordinate is (2a). ``resolve`` is ``RegionResolver.from_tiles``.

    Only a point inside a polygon of the live tiles is asked about further: eight points a
    hundred metres around it must each be in the same region or in none. A point in none
    that is nearest to another region is taken as that region's, for a border can be drawn
    with a gap in it. Ask ``resolve`` about raw coordinates only, not rounded or saved ones.
    """
    here = resolve(lat, lon)
    if here.unread:
        return Place(None, "unread")
    if here.stale:
        return Place(None, "stale")
    if here.region is None:
        return Place(None, "nowhere")
    if here.method != "tile_interior":
        return Place(here.region, "nearest")
    for y, x in _around(lat, lon):
        r = resolve(y, x)
        if r.unread or r.stale or r.region not in (here.region, None):
            return Place(here.region, "edge")
    return Place(here.region, "inside")


def _around(lat: float, lon: float) -> Iterator[tuple[float, float]]:
    """Eight points on the sphere, as vectors: at a pole too there are eight ways to go."""
    far, y, x = math.radians(_AROUND_KM / _KM_PER_DEGREE), math.radians(lat), math.radians(lon)
    here = (math.cos(y) * math.cos(x), math.cos(y) * math.sin(x), math.sin(y))
    north = (-math.sin(y) * math.cos(x), -math.sin(y) * math.sin(x), math.cos(y))
    east = (-math.sin(x), math.cos(x), 0.0)
    for step in range(8):
        turn = math.radians(45 * step)
        a, b, c = (math.cos(far) * h + math.sin(far) * (math.cos(turn) * n + math.sin(turn) * e)
                   for h, n, e in zip(here, north, east, strict=True))
        yield math.degrees(math.atan2(c, math.hypot(a, b))), math.degrees(math.atan2(b, a))


@dataclass(frozen=True)
class Obs:
    """One photo of the owner's camera, and where it is."""

    shot: Shot
    place: Place

    @property
    def proof(self) -> bool:
        """Whether it shows a day in its region (2, 2b).

        A ground photo of the owner's own camera, not set aside by a span, strictly inside,
        with a clock that can be trusted to date it.
        """
        s = self.shot
        return (s.kind == "ground" and s.use == "all" and s.taken_utc is not None
                and s.origin == "own-camera" and self.place.state == "inside"
                and zone_fits(s.zone, s.lon))

    @property
    def day(self) -> dt.date:
        return self.shot.taken.date()


def _own(obs: Iterable[Obs]) -> Iterator[Obs]:
    return (o for o in obs if o.shot.origin == "own-camera")


# -- runs --------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """Days of photos inside one region.

    ``days`` are the days with proof and ``first`` and ``last`` the ends of those, never of
    anything else. ``silent`` are the days between with no photo at all. ``shots`` is the
    proof, in UTC order. ``held`` is released by the owner's words and ``fixed`` by nothing;
    ``there`` is on the profile already, with these dates.
    """

    region: int
    first: dt.date
    last: dt.date
    days: tuple[dt.date, ...]
    silent: tuple[dt.date, ...]
    shots: tuple[Obs, ...]
    reasons: tuple[str, ...]
    flags: tuple[str, ...]
    status: Status

    @property
    def id(self) -> str:
        return f"{self.region}:{self.first}:{self.last}"

    @property
    def start(self) -> dt.datetime:
        """The first proof, in UTC."""
        return self.shots[0].shot.taken_utc

    @property
    def end(self) -> dt.datetime:
        """The last proof, in UTC."""
        return self.shots[-1].shot.taken_utc


class AnyRun(Protocol):
    """What :func:`group` asks of a run: a :class:`Run`, or one that a list read back."""

    @property
    def region(self) -> int: ...
    @property
    def first(self) -> dt.date: ...
    @property
    def last(self) -> dt.date: ...
    @property
    def reasons(self) -> tuple[str, ...]: ...
    @property
    def status(self) -> Status: ...
    @property
    def id(self) -> str: ...
    @property
    def start(self) -> dt.datetime: ...
    @property
    def end(self) -> dt.datetime: ...


def _utc(o: Obs) -> tuple:
    s = o.shot
    return (s.taken_utc, s.lat, s.lon, s.uuid)


def _in_order(runs: Iterable[AnyRun]) -> list:
    return sorted(runs, key=lambda r: (r.start, r.end, r.id))


def find_runs(
    obs: Iterable[Obs], *, gap_days: int = GAP_DAYS, track: Iterable[TrackRow] = ()
) -> list[Run]:
    """The runs of every region, in time order, not judged (3, 3a).

    The proof days of one region, by the photos' local dates. Two of them, one after the
    other, join when each day strictly between is compatible or silent, with at most
    ``gap_days`` - 1 silent days in a row. Compatible: every photo of the day is on the
    ground, not set aside, and placed in this region, on its edge or beside it. Silent: no
    photo at all, and no ``track`` row in another region. Any other day is a barrier. What
    else there is on a proof day does not matter: a day trip and a transfer leave proof of
    two regions on one day.

    What comes back is ``fixed``, as ``not judged``: :func:`judge` says what each may be.
    """
    if type(gap_days) is not int or gap_days < 1:
        raise ValueError(f"gap_days={gap_days!r} is not a whole number of days of 1 or more")
    only: dict[dt.date, int | None] = {}  # the one region of a day's photos, or None
    proof: dict[int, dict[dt.date, list[Obs]]] = defaultdict(lambda: defaultdict(list))
    for o in _own(obs):
        fit = o.place.region if o.shot.kind == "ground" and o.shot.use == "all" else None
        if only.setdefault(o.day, fit) != fit:
            only[o.day] = None
        if o.proof:
            proof[o.place.region][o.day].append(o)
    rows: dict[dt.date, set[int | None]] = defaultdict(set)
    for day, region in track:
        rows[day].add(region)

    def between(region: int, a: dt.date, b: dt.date) -> list[dt.date] | None:
        """The silent days between two proof days that join, or ``None``."""
        silent, in_a_row = [], 0
        for n in range(1, (b - a).days):
            day = a + n * _DAY
            if day in only:
                if only[day] != region:
                    return None
                in_a_row = 0
            elif rows.get(day, {region}) != {region}:
                return None
            else:
                silent.append(day)
                in_a_row += 1
                if in_a_row >= gap_days:
                    return None
        return silent

    out = []
    for region, by_day in proof.items():
        days = sorted(by_day)
        stretch, silent = [days[0]], []
        stretches = [(stretch, silent)]
        for a, b in pairwise(days):
            joins = between(region, a, b)
            if joins is None:
                stretch, silent = [], []
                stretches.append((stretch, silent))
            silent += joins or []
            stretch.append(b)
        for stretch, silent in stretches:
            shots = sorted((o for d in stretch for o in by_day[d]), key=_utc)
            out.append(Run(region, stretch[0], stretch[-1], tuple(stretch), tuple(silent),
                           tuple(shots), ("not judged",), (), "fixed"))
    return _in_order(out)


# -- what is known of all the photos ---------------------------------------------------


class _Index:
    """What :func:`judge` asks of the whole library, without reading it once per run."""

    def __init__(self, obs: Iterable[Obs]):
        obs = list(_own(obs))
        self.proofs = sorted((o for o in obs if o.proof), key=_utc)
        self.utcs = [o.shot.taken_utc for o in self.proofs]
        self.fast = sorted(o.shot.taken_utc for o in obs
                           if o.shot.kind == "fast" and o.shot.taken_utc)
        self.unread = {o.day for o in obs if o.place.state == "unread"}
        self.stale = {o.day for o in obs if o.place.state == "stale"}
        self.conflict = {(o.day, o.place.region) for o in obs if o.shot.kind == "conflict"}
        self.seen = {(o.day, o.place.region) for o in obs}

    @cached_property
    def _change(self) -> list[int]:
        """For each proof, the next one that is in another region."""
        n = len(self.proofs)
        out = [n] * n
        for i in range(n - 2, -1, -1):
            same = self.proofs[i].place.region == self.proofs[i + 1].place.region
            out[i] = out[i + 1] if same else i + 1
        return out

    def elsewhere(self, region: int, i: int, j: int) -> Iterator[Obs]:
        """The proofs from ``i`` up to ``j`` that are in another region."""
        while i < j:
            if self.proofs[i].place.region == region:
                i = self._change[i]
            else:
                yield self.proofs[i]
                i += 1

    @cached_property
    def _latest(self) -> list[tuple]:
        """For each proof, the last local day from it on, its region, and the last day of
        any other region."""
        none = dt.date.min
        day, region, other = none, None, none
        out: list[tuple] = [(day, region, other)]
        for o in reversed(self.proofs):
            if o.place.region == region:
                day = max(day, o.day)
            elif o.day > day:
                day, region, other = o.day, o.place.region, day
            else:
                other = max(other, o.day)
            out.append((day, region, other))
        return out[::-1]

    def finished(self, run: Run) -> bool:
        """A proof elsewhere, later in UTC and on a later local day (4)."""
        day, region, other = self._latest[bisect_right(self.utcs, run.end)]
        return (day if region != run.region else other) > run.last

    def left_between(self, a: Run, b: Run) -> bool:
        """A proof elsewhere strictly between the photos of two runs of one region (5)."""
        i, j = bisect_right(self.utcs, a.end), bisect_left(self.utcs, b.start)
        return any(True for _ in self.elsewhere(a.region, i, j))

    def fast_within(self, start: dt.datetime, end: dt.datetime) -> bool:
        return bisect_left(self.fast, start) < bisect_right(self.fast, end)


# -- a run against the visits that are there ---------------------------------------------


def _ends(when: When | None) -> tuple[dt.date, dt.date]:
    """The first and the last day that one end of a visit can be."""
    if when is None:
        return dt.date.min, dt.date.max
    if isinstance(when, YearOnly):
        return dt.date(when.year, 1, 1), dt.date(when.year, 12, 31)
    return when, when


@dataclass(frozen=True)
class _Dates:
    """What a visit's dates say: the days it can have, and those it must have.

    ``sure`` is ``None`` where no day is certain. An end that is not there is open, and is
    never taken for the other one; but the end that is there is a day of the visit.
    """

    lo: dt.date
    hi: dt.date
    sure: tuple[dt.date, dt.date] | None

    @classmethod
    def of(cls, v: Visit) -> _Dates:
        (a, b), (c, d) = _ends(v.date_from), _ends(v.date_to)
        if a == b and c == d:
            lo, hi = min(a, c), max(a, c)
            return cls(lo, hi, (lo, hi))
        if a > d:  # the wrong way round, so all that is between is taken for its own
            return cls(c, b, (d, a))
        b, c = min(b, d), max(c, a)  # neither end is past the other
        return cls(a, d, (b, c) if b <= c else None)


def _said(v: Visit) -> str:
    """A visit as a reason names it."""
    a, b = (w.isoformat() if w else "no date" for w in (v.date_from, v.date_to))
    if a == b == "no date":
        return f"visit {v.id}, with no dates"
    return f"visit {v.id}, {a} to {b}"


def _midnight(day: dt.date, o: Obs) -> dt.datetime:
    """When ``day`` began, by the clock of this photo."""
    start = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
    return start - dt.timedelta(seconds=o.shot.zone)


def _apart(ix: _Index, run: Run, dates: _Dates) -> bool:
    """Whether a photo shows that the owner left between a visit and the run (5).

    The visit shares no day with the run and cannot: it is before it or after it.
    A visit has dates and no times. So the photo must be a proof in another region, and by
    its own local date on a day strictly between the visit's nearest end and the run's, or
    on the run's own edge day, before its first proof or after its last. On the visit's
    edge day no photo says which came first, so there it counts for nothing. The nearest
    end is the nearest day it can be, if the visit is dated only in part.

    It must also be between them in UTC: before the run's first proof, and two hours or more
    after the visit's last day was over by the clock of that first proof; the same the other
    way about. Two hours, because nobody knows the visit's own clock and a region can have
    two. Over the date line a later date can be an earlier time. Where the two do not agree
    the answer is no: doubtful is a continuation.
    """
    if dates.hi < run.first:
        over = _midnight(dates.hi + _DAY, run.shots[0]) + _NIGHT
        i, j = bisect_left(ix.utcs, over), bisect_left(ix.utcs, run.start)
        return any(dates.hi < o.day <= run.first for o in ix.elsewhere(run.region, i, j))
    begun = _midnight(dates.lo, run.shots[-1]) - _NIGHT
    i, j = bisect_right(ix.utcs, run.end), bisect_right(ix.utcs, begun)
    return any(run.last <= o.day < dates.lo for o in ix.elsewhere(run.region, i, j))


def _against_visit(ix: _Index, run: Run, v: Visit) -> str | None:
    """The reason that one row of the region's visits gives (6), or ``None``.

    Dated only in part, a visit that has a day of the run for certain is an overlap, and one
    that can have a day of it is undated. One that can have none is a continuation unless
    the owner was seen elsewhere between.
    """
    dates = _Dates.of(v)
    if (v.date_from, v.date_to) == (run.first, run.last):
        return f"there: {_said(v)}"
    if dates.sure and dates.sure[0] <= run.last and run.first <= dates.sure[1]:
        return f"overlap: shares a day with {_said(v)}"
    if dates.lo <= run.last and run.first <= dates.hi:
        return f"undated: may be {_said(v)}"
    if not _apart(ix, run, dates):
        return f"continuation: no photo shows that you left between it and {_said(v)}"
    return None


# -- judging ------------------------------------------------------------------------------


def _touches(run: Run, since: dt.date | None, until: dt.date | None) -> bool:
    return (since is None or run.last >= since) and (until is None or run.first <= until)


def regions_to_read(
    obs: Collection[Obs],
    *,
    since: dt.date | None,
    until: dt.date | None,
    track: Collection[TrackRow] = (),
    gaps: Iterable[int] = SWEEP_GAPS,
) -> set[int]:
    """The regions whose visits :func:`judge` and :func:`sweep` need, at each of ``gaps``."""
    return {r.region for gap in gaps for r in find_runs(obs, gap_days=gap, track=track)
            if _touches(r, since, until)}


def evidence_grade(run: Run, assets: Mapping[str, PhotoAsset]) -> evidence.Grade:
    """The grade ``evidence`` gives the run's proof. A photo not in ``assets`` adds nothing."""
    shots = [evidence.Shot(assets[o.shot.uuid], run.region) for o in run.shots
             if o.shot.uuid in assets]
    return evidence.grade(shots)


def status_of(reasons: Iterable[str]) -> Status:
    """What the reasons of a judged run make it."""
    found = {kind_of(s) for s in reasons}
    return ("there" if "there" in found else
            "offered" if not found else
            "held" if found <= RELEASED_BY_WORDS else "fixed")


def judge(
    runs: Iterable[Run],
    obs: Iterable[Obs],
    visits: Mapping[int, Sequence[Visit]],
    *,
    since: dt.date | None,
    until: dt.date | None,
    track: Iterable[TrackRow] = (),
    assets: Mapping[str, PhotoAsset] | None = None,
) -> list[Run]:
    """The runs that touch ``since`` to ``until``, with reasons, flags and status (4-7b).

    ``runs`` is all of them, as :func:`find_runs` gives them, with the same ``obs`` and
    ``track``: an earlier stay that is not on the list may still be the first half of one
    that is. ``visits`` has every row of every region that comes back, trip-owned ones and
    transits too, and an empty list for a region without one. A region that is absent is one
    whose list was not read, and raises :class:`PlanInvalid`.

    A run with a visit of exactly its dates is ``there``, whatever else is true of it.
    Otherwise one reason that nothing releases makes it ``fixed``, any other makes it
    ``held``, and none makes it ``offered``. Two runs of one region with no proof elsewhere
    between them are both ``fixed``: nothing says that the first one ended.
    """
    obs = list(obs)
    runs = _in_order(runs)
    chosen = [r for r in runs if _touches(r, since, until)]
    if unread := sorted({r.region for r in chosen} - set(visits)):
        raise PlanInvalid("no list of visits for region " + ", ".join(map(str, unread))
                          + "; read it first, for a list that was not read is not an empty one")
    ix = _Index(obs)
    rows: dict[dt.date, set[int | None]] = defaultdict(set)
    for day, region in track:
        rows[day].add(region)
    half_of: dict[str, list[Run]] = defaultdict(list)
    by_region: dict[int, list[Run]] = defaultdict(list)
    for r in runs:
        by_region[r.region].append(r)
    for same in by_region.values():
        for a, b in pairwise(sorted(same, key=lambda r: r.first)):
            if not ix.left_between(a, b):
                half_of[a.id].append(b)
                half_of[b.id].append(a)

    out = []
    for run in chosen:
        said = [_against_visit(ix, run, v) for v in visits[run.region]]
        reasons = [s for s in said if s and kind_of(s) == "there"]
        if not reasons:
            reasons = [s for s in said if s]
            reasons += [f"continuation: no photo shows that you left between it and run {r.id}"
                        for r in half_of[run.id]]
            reasons += _own_reasons(ix, run, since, until)
        out.append(replace(run, reasons=tuple(reasons), status=status_of(reasons),
                           flags=tuple(_flags(ix, run, rows, assets or {}))))
    return out


def _own_reasons(
    ix: _Index, run: Run, since: dt.date | None, until: dt.date | None
) -> Iterator[str]:
    """What holds a run whatever the profile says (4, 7)."""
    if since is not None and run.first < since:
        yield f"across: it starts before --since {since}"
    if until is not None and run.last > until:
        yield f"across: it ends after --until {until}"
    if not ix.finished(run):
        yield "open: no photo on a later day shows you in another region"
    took = run.end - run.start
    if len(run.shots) == 1:
        yield "brief: one photo"
    elif took <= _BRIEF:
        yield "brief: every photo is within 15 minutes"
    elif len({(o.shot.lat, o.shot.lon) for o in run.shots}) == 1:
        yield "brief: every photo has one and the same coordinate"
    if (took < _LAYOVER and ix.fast_within(run.start - _FAST_NEAR, run.start)
            and ix.fast_within(run.end, run.end + _FAST_NEAR)):
        yield "layover?: under 24 hours, with a fast photo before it and after it"
    near = {run.first + n * _DAY for n in range(-1, (run.last - run.first).days + 2)}
    if days := sorted(near & ix.unread):
        yield "not read: a map tile on " + ", ".join(map(str, days))
    if days := sorted(near & ix.stale):
        yield "stale map: a region not in the catalogue on " + ", ".join(map(str, days))
    zones = [o.shot.zone for o in run.shots]
    if max(zones) - min(zones) > _CLOCKS_APART:
        # Both clocks fit the place, a day being taken off, and only one shows its date.
        yield "clock: its photos have two clocks over 12 hours apart"


def _flags(
    ix: _Index, run: Run, rows: Mapping[dt.date, set[int | None]],
    assets: Mapping[str, PhotoAsset],
) -> Iterator[str]:
    """What the owner should see and holds nothing (7b)."""
    days = [run.first + n * _DAY for n in range((run.last - run.first).days + 1)]
    if any((d, run.region) in ix.conflict for d in days):
        yield "conflict"
    if evidence_grade(run, assets) in ("thin", "none"):
        yield "evidence thin"
    if ix.fast_within(run.start - _FAST_NEAR, run.end + _FAST_NEAR):
        yield "fast near"
    other = [(d, r) for d in days for r in sorted(rows.get(d, ()), key=str)
             if r != run.region and (r is None or (d, r) not in ix.seen)]
    if other:
        yield "track: " + ", ".join(
            f"{d} in {'no known region' if r is None else f'region {r}'}" for d, r in other)


# -- between two runs ------------------------------------------------------------------


@dataclass(frozen=True)
class Boundary:
    """The place between two runs that are next to each other in time order.

    ``suggestions`` say why a new trip may start here, and have to be answered. Where it is
    ``forced`` a new trip starts here whatever is answered.
    """

    before: str
    after: str
    suggestions: tuple[str, ...]
    forced: bool

    @property
    def id(self) -> str:
        return f"{self.before}|{self.after}"


def _cuts_itself_off(run: AnyRun) -> bool:
    """On the profile already, for certain: an overlap can only be left out (8a)."""
    return run.status == "there" or "overlap" in map(kind_of, run.reasons)


def boundaries(
    runs: Iterable[Run], *, gap_days: int, obs: Iterable[Obs], fast_kmh: float | None = None
) -> list[Boundary]:
    """One boundary between every two of the judged runs, in time order (8, 8a).

    Every run is in the order, also one that is there and one that nothing releases, so a
    trip is what is between two cuts. Suggested where a fast or impossible photo, or a leg
    faster than ``fast_kmh`` (what :func:`~wanderfill.plan.review.grade_shots` takes), lies
    between; where the next run's first day is more than ``gap_days`` after this one's last;
    and beside a run that is on the profile, where it is forced too. Between is from this
    run's last proof before the next one starts: a day trip is inside the stay it was
    made from.
    """
    fast_kmh = default_speeds()["fast_kmh"] if fast_kmh is None else fast_kmh
    moving = sorted(o.shot.taken_utc for o in _own(obs)
                    if o.shot.kind in ("fast", "conflict") and o.shot.taken_utc)
    out = []
    for a, b in pairwise(_in_order(runs)):
        times = [o.shot.taken_utc for o in a.shots]
        leave = a.shots[max(0, bisect_right(times, b.start) - 1)].shot
        arrive = b.shots[0].shot
        why = []
        if bisect_left(moving, leave.taken_utc) < bisect_right(moving, arrive.taken_utc):
            why.append("fast: a photo between them was taken at speed")
        elif _speed(leave, arrive) > fast_kmh:
            why.append(f"fast: over {fast_kmh:g} km/h from one to the other")
        if (b.first - a.last).days > gap_days:
            why.append(f"gap: nothing from {a.last} to {b.first}")
        forced = _cuts_itself_off(a) or _cuts_itself_off(b)
        if forced:
            why.append("profile: one of them is on the profile already")
        out.append(Boundary(a.id, b.id, tuple(why), forced))
    return out


def _speed(a: Shot, b: Shot) -> float:
    km = haversine_km(a.lat, a.lon, b.lat, b.lon)
    hours = (b.taken_utc - a.taken_utc).total_seconds() / 3600
    return km / hours if hours > 0 else math.inf if km else 0.0


# -- from answers to trips ---------------------------------------------------------------


class RunAnswer(TypedDict, total=False):
    """One run's answer: ``a`` is a quality ``"1"`` to ``"6"``, ``"no"`` or ``"?"``."""

    a: str
    words: str


class Answers(TypedDict):
    """The owner's answers by run id and by boundary id (``"cut"`` or ``"same"``).

    What is not there was not answered, and is never taken for anything.
    """

    runs: Mapping[str, RunAnswer]
    cuts: Mapping[str, str]


@dataclass(frozen=True)
class TripRow:
    """One region of a trip, with its quality and, for a held run, the owner's own words."""

    run: AnyRun
    quality: int
    words: str


@dataclass(frozen=True)
class TripDraft:
    """A trip to plan. Its dates are of the rows that are in it (10)."""

    rows: tuple[TripRow, ...]
    date_from: dt.date
    date_to: dt.date


@dataclass(frozen=True)
class NotMade:
    """Runs that make no trip, by id, and why not."""

    runs: tuple[str, ...]
    reasons: tuple[str, ...]


_QUALITIES = frozenset("123456")


# Characters that show nothing: spaces, controls and formats, what is drawn as blank or not
# at all, half pairs, private use, and the last three planes. Written down, because the
# page must read words as this does, and two runtimes know two versions of Unicode.
SAYS_NOTHING = (
    (0x0000, 0x0020), (0x007F, 0x00A0), (0x00AD, 0x00AD), (0x034F, 0x034F), (0x0600, 0x0605),
    (0x061C, 0x061C), (0x06DD, 0x06DD), (0x070F, 0x070F), (0x0890, 0x0891), (0x08E2, 0x08E2),
    (0x115F, 0x1160), (0x1680, 0x1680), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x2000, 0x200F),
    (0x2028, 0x202F), (0x205F, 0x206F), (0x2800, 0x2800), (0x3000, 0x3000), (0x3164, 0x3164),
    (0xD800, 0xF8FF), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFFB),
    (0xFFFE, 0xFFFF), (0x110BD, 0x110BD), (0x110CD, 0x110CD), (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0x10FFFF),
)


def _says(text: str) -> bool:
    return any(not any(low <= ord(ch) <= high for low, high in SAYS_NOTHING) for ch in text)


def _check(runs: Sequence[AnyRun], bounds: Sequence[Boundary], answers: Answers) -> None:
    """Nothing in ``answers`` that the page could not have written."""
    if [(a.id, b.id) for a, b in pairwise(runs)] != [(b.before, b.after) for b in bounds]:
        raise PlanInvalid("these are not the boundaries of these runs")
    known = {r.id: r for r in runs}
    for key, row in answers["runs"].items():
        if key not in known:
            raise PlanInvalid(f"runs: {_r(key)} is not on this list")
        a, words, status = row.get("a"), row.get("words", ""), known[key].status
        if a is not None and a not in ("no", "?") and (
                not isinstance(a, str) or a not in _QUALITIES):
            raise PlanInvalid(f"runs: {key} is answered {_r(a)}")
        if not isinstance(words, str):
            raise PlanInvalid(f"runs: the words of {key} are not text")
        if words and status != "held":
            raise PlanInvalid(f"runs: {key} takes no words")
        if a in _QUALITIES and status not in ("offered", "held"):
            raise PlanInvalid(f"runs: {key} takes no quality")
    cuts = {b.id for b in bounds}
    for key, cut in answers["cuts"].items():
        if key not in cuts:
            raise PlanInvalid(f"cuts: {_r(key)} is not on this list")
        if cut not in ("cut", "same"):
            raise PlanInvalid(f"cuts: {key} is answered {_r(cut)}")


def group(
    runs: Iterable[AnyRun], boundaries: Sequence[Boundary], answers: Answers
) -> tuple[list[TripDraft], list[NotMade]]:
    """The trips that the answers make, and the runs that make none, with why (8-10).

    ``runs`` and ``boundaries`` are the judged runs and all their boundaries, or those of a
    :class:`~wanderfill.plan.trip_list.TripList`, which come to the same. A trip is
    what is between two cuts. There is a cut where one is forced, where the owner cut, and
    before an open run that the owner put no words to. A boundary with a suggestion and no
    answer is no cut and holds what is on both sides of it.

    Runs answered "no" are left out, and the rest is the trip. A run with no answer or
    with "?", a held run with a quality and no words, and a region that is in it twice, each
    hold the whole trip. A stretch with nothing in it and nothing that holds it is no trip,
    and is in neither list, and neither is a run that is there.

    An answer that the page could not have written raises :class:`PlanInvalid`.
    """
    runs = _in_order(runs)
    _check(runs, boundaries, answers)
    words = {key: row.get("words", "") for key, row in answers["runs"].items()}

    stretches: list[tuple[list[AnyRun], list[str]]] = []
    for n, run in enumerate(runs):
        open_end = "open" in map(kind_of, run.reasons) and not _says(words.get(run.id, ""))
        edge = boundaries[n - 1] if n else None
        said = answers["cuts"].get(edge.id) if edge else None
        if edge is None or edge.forced or open_end or said == "cut":
            stretches.append(([], []))
        elif edge.suggestions and said is None:
            stretches[-1][1].append(
                f"between {edge.before} and {edge.after} it is not answered whether a new trip "
                "starts")
        stretches[-1][0].append(run)

    drafts, not_made = [], []
    for members, holds in stretches:
        rows: list[TripRow] = []
        for run in members:
            answer = answers["runs"].get(run.id, {}).get("a")
            if run.status == "there" or answer == "no":
                continue
            if answer is None:
                holds.append(f"{run.id} is not answered")
            elif answer == "?":
                holds.append(f"{run.id} is answered with a question mark")
            elif run.status == "held" and not _says(words.get(run.id, "")):
                holds.append(f"{run.id} needs words of yours")
            else:
                rows.append(TripRow(run, int(answer), words.get(run.id, "")))
        once: dict[int, AnyRun] = {}
        for row in rows:
            first = once.setdefault(row.run.region, row.run)
            if first is not row.run:
                holds.append(f"a trip has a region once, and {first.id} and {row.run.id} are of "
                             "one region; cut between them")
        if holds:
            not_made.append(NotMade(tuple(r.id for r in members), tuple(holds)))
        elif rows:
            drafts.append(TripDraft(tuple(rows), min(r.run.first for r in rows),
                                    max(r.run.last for r in rows)))
    return drafts, not_made


# -- at other gaps -------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepRow:
    """One line of the gap table: how many runs, suggested cuts, and runs that are held."""

    gap_days: int
    runs: int
    suggested: int
    held: int


def sweep(
    obs: Collection[Obs],
    visits: Mapping[int, Sequence[Visit]],
    *,
    since: dt.date | None,
    until: dt.date | None,
    track: Collection[TrackRow] = (),
    gaps: Iterable[int] = SWEEP_GAPS,
    fast_kmh: float | None = None,
) -> list[SweepRow]:
    """What the same photos come to at other settings of ``gap_days`` (13).

    ``held`` counts those that words release and those that nothing does, and ``suggested``
    the cuts that would be the owner's to answer, so not the forced ones.
    """
    out = []
    for gap in gaps:
        runs = judge(find_runs(obs, gap_days=gap, track=track), obs, visits,
                     since=since, until=until, track=track)
        cuts = boundaries(runs, gap_days=gap, obs=obs, fast_kmh=fast_kmh)
        out.append(SweepRow(gap, len(runs),
                            sum(bool(b.suggestions) and not b.forced for b in cuts),
                            sum(r.status in ("held", "fixed") for r in runs)))
    return out


__all__ = [
    "GAP_DAYS",
    "NEVER_RELEASED",
    "RELEASED_BY_WORDS",
    "SAYS_NOTHING",
    "SWEEP_GAPS",
    "Answers",
    "AnyRun",
    "Boundary",
    "NotMade",
    "Obs",
    "Place",
    "Run",
    "RunAnswer",
    "SweepRow",
    "TripDraft",
    "TripRow",
    "boundaries",
    "evidence_grade",
    "find_runs",
    "group",
    "judge",
    "kind_of",
    "place",
    "regions_to_read",
    "status_of",
    "sweep",
]
