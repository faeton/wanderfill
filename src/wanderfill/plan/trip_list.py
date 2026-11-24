"""The list of runs the owner is asked about, the page it is on, and the answers to it.

A :class:`TripList` is everything the owner was shown: the runs and the boundaries as
:mod:`wanderfill.plan.trips` judged them, the visits and the trips that were on the profile,
the sources, and what was left out. The answers name its hash, so they are never read as
answers to another list, and ``trips plan`` can read the profile again and refuse where it
is no longer what was shown.

It has no coordinates and no photos: the page needs neither to ask. Nothing here reads the
profile or a library. :func:`visits_rows` and :func:`trips_rows` put what was read into the
one form that is hashed, for the list and for the later reading alike.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from importlib.resources import files
from itertools import pairwise
from pathlib import Path
from typing import Any, get_args

from .. import evidence
from ..api.client import MARKS_REGION_FROM, QUALITY, Visit, When
from ..api.client import _whole as _live_id
from ..api.errors import PlanInvalid
from ..sources.photos_app import PhotoAsset
from .manifest import canonical, date, escape_data, json_of, read_bytes, read_json, sha256_of
from .manifest import fields as _fields
from .manifest import number as _number
from .manifest import text as _text
from .manifest import whole as _whole
from .model import _CONTROLS, _r, parse_when
from .review import _SPEEDS, _at
from .trips import (
    NEVER_RELEASED,
    RELEASED_BY_WORDS,
    SAYS_NOTHING,
    Answers,
    AnyRun,
    Boundary,
    Obs,
    Run,
    RunAnswer,
    Status,
    SweepRow,
    _cuts_itself_off,
    _ends,
    _in_order,
    _says,
    evidence_grade,
    kind_of,
    status_of,
)

FORMAT = 1
ANSWERS_FORMAT = 1
MAX_ANSWERS_BYTES = 5_000_000
MAX_WORDS = 500

_REASONS = (RELEASED_BY_WORDS | NEVER_RELEASED | {"there"}) - {"not judged"}
_FLAGS = frozenset({"conflict", "evidence thin", "fast near", "track"})
_SUGGESTIONS = frozenset({"fast", "gap", "profile"})
_KINDS = _REASONS | _FLAGS | _SUGGESTIONS

_PARAMS = ("since", "until", "gap_days", "speeds", "spans")
_SOURCES = ("photos", "track", "spans")
_COVERAGE = ("tiles_read", "tiles_unread", "unread_photos")
_LEFT_OUT = ("other_origin", "no_utc", "span_none", "conflict", "fast", "span_airports",
             "unread", "stale", "nowhere", "nearest", "edge", "clock")
_VISIT = ("id", "date_from", "date_to", "quality", "trip_id")
_TRIP = ("id", "date_from", "date_to", "description")
_LISTS = ("sources", "sweep", "runs", "boundaries", "trips")

# In the owner's words: what the pages and the terminal take out of a name, for a name can be
# shown without it and their words are kept as they are; every line break; and half pairs,
# which no file can hold.
_NOT_IN_WORDS = ((0x00, 0x1F), (0x7F, 0x9F), (0x61C, 0x61C), (0x200E, 0x200F),
                 (0x2028, 0x202E), (0x2066, 0x2069), (0xD800, 0xDFFF))


# -- what was shown ------------------------------------------------------------------------


@dataclass(frozen=True)
class PhotoTime:
    """When a photo was taken by its own clock, ``zone`` seconds ahead of UTC."""

    at: dt.datetime
    zone: int


@dataclass(frozen=True)
class ListedRun:
    """A judged :class:`~wanderfill.plan.trips.Run` as the list keeps it.

    ``photos`` counts its proof. ``start`` and ``end`` are the first and the last of it in
    UTC, which is the order of the list, and ``first_photo`` and ``last_photo`` the same two
    by their own clocks. ``evidence`` is the grade that ``wanderfill evidence`` would give.
    """

    region: int
    name: str
    first: dt.date
    last: dt.date
    days: tuple[dt.date, ...]
    silent: tuple[dt.date, ...]
    photos: int
    first_photo: PhotoTime
    last_photo: PhotoTime
    start: dt.datetime
    end: dt.datetime
    evidence: evidence.Grade
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    status: Status

    @property
    def id(self) -> str:
        return f"{self.region}:{self.first}:{self.last}"


@dataclass(frozen=True)
class TripList:
    """One list of runs, with everything the owner was told beside it.

    ``sha256`` is what an answer is bound to. It is of the canonical JSON of all the fields
    but ``created``: a list rebuilt with nothing changed is the same list.

    ``params`` has ``since``, ``until`` (a date or ``None``), ``gap_days``, ``speeds``, the
    arguments of :func:`~wanderfill.plan.review.grade_shots` by name, and ``spans``, whether
    a spans file was given. ``sources`` has the photos once, every track and the spans file,
    each with ``kind``, ``label`` (no directory), ``sha256`` or ``None``, ``first``, ``last``
    and ``count``; ``no_track`` is the owner's word that there is no track for these dates,
    and is true exactly when there is none. ``coverage`` has ``tiles_read``,
    ``tiles_unread`` and ``unread_photos``. ``left_out`` is what :func:`left_out` returns.
    ``visits`` has every visit of every region on the list, as :func:`visits_rows` has them,
    and ``trips`` the trips over the same days, as :func:`trips_rows` has them.

    Everything is checked here and put into the one form that is hashed, saved, loaded and
    shown. What does not fit, or does not follow from the rest, raises :class:`PlanInvalid`.
    """

    account: int
    created: str
    params: dict
    sources: list[dict]
    no_track: bool
    coverage: dict
    left_out: dict
    sweep: list[SweepRow]
    runs: list[ListedRun]
    boundaries: list[Boundary]
    visits: dict[str, list[dict]]
    trips: list[dict]
    format: int = FORMAT

    def __post_init__(self) -> None:
        try:
            _check_format(self.format)
            _whole(self.account, "account")
            for name in _LISTS:
                if not isinstance(getattr(self, name), (list, tuple)):
                    raise ValueError(f"{name} must be a list, not {_r(getattr(self, name))}")
            clean: dict[str, Any] = {
                "created": _text(self.created, "created"),
                "params": _params_of(self.params),
                "sources": _sources_of(self.sources),
                "no_track": _flag(self.no_track, "no_track"),
                "coverage": _counts(self.coverage, _COVERAGE, "coverage"),
                "left_out": _counts(self.left_out, _LEFT_OUT, "left_out"),
            }
            for name, kind, read in (("sweep", SweepRow, _sweep_of), ("runs", ListedRun, _run_of),
                                     ("boundaries", Boundary, _boundary_of)):
                clean[name] = [read(_row_of(v, kind, f"{name}[{i}]"), f"{name}[{i}]")
                               for i, v in enumerate(getattr(self, name))]
            runs = clean["runs"]
            clean["visits"] = _visits_of(self.visits, runs)
            clean["trips"] = _trips_of(self.trips, runs)
            _check_sources(clean)
            _check_sweep(clean["sweep"])
            _check_runs(runs)
            _check_boundaries(runs, clean["boundaries"], clean["params"]["gap_days"])
        except ValueError as exc:
            raise PlanInvalid(str(exc)) from None
        for name, value in clean.items():
            object.__setattr__(self, name, value)

    def _content(self) -> dict:
        return {
            "format": self.format, "account": self.account, "params": self.params,
            "sources": self.sources, "no_track": self.no_track, "coverage": self.coverage,
            "left_out": self.left_out, "visits": self.visits, "trips": self.trips,
            "sweep": [_row_of(r, SweepRow, "sweep") for r in self.sweep],
            "runs": [_row_of(r, ListedRun, "runs") for r in self.runs],
            "boundaries": [_row_of(b, Boundary, "boundaries") for b in self.boundaries],
        }

    @property
    def sha256(self) -> str:
        return sha256_of(self._content())

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
    def load(cls, path: str | Path) -> TripList:
        """Read what :meth:`save` wrote, or :class:`PlanInvalid` saying what is wrong with it.

        Anything else is refused, a number spelt another way included: the list on disk is
        the one the owner answered, or it is not.
        """
        raw = read_json(path, "trip list")
        try:
            if not isinstance(raw, dict):
                raise ValueError(f"a trip list is a JSON object, not {type(raw).__name__}")
            if "format" in raw:  # first, because a later format may have other fields
                _check_format(raw["format"])
            _fields(raw, ("sha256", *(f.name for f in fields(cls))), "the trip list")
            for name in _LISTS:
                if not isinstance(raw[name], list):
                    raise ValueError(f"{name} must be a list, not {_r(raw[name])}")
            args = {k: v for k, v in raw.items() if k != "sha256"}
            for name, read in (("sweep", _sweep_of), ("runs", _run_of),
                               ("boundaries", _boundary_of)):
                args[name] = [read(row, f"{name}[{i}]") for i, row in enumerate(raw[name])]
            out = cls(**args)
            if canonical(raw) != canonical(out._stored()):
                raise ValueError("its content is not what its sha256 was taken of; it was changed "
                                 "after it was written, so build the list again")
        except ValueError as exc:
            raise PlanInvalid(f"{path}: {exc}") from None
        except PlanInvalid as exc:
            raise PlanInvalid(f"{path}: {exc}") from None
        return out


def _check_format(value: object) -> None:
    if type(value) is not int or value != FORMAT:
        raise ValueError(f"format {_r(value)} is not {FORMAT}, the one this reads")


# -- one reading of every part ---------------------------------------------------------------


def _row_of(value: object, kind: type, what: str) -> dict:
    """A run, a boundary or a sweep row as JSON has it. The readers below check it."""
    if type(value) is not kind:
        raise ValueError(f"{what}: {_r(value)} is not a {kind.__name__}")
    return {f.name: _plain(getattr(value, f.name)) for f in fields(kind)}


def _plain(value: object) -> object:
    if isinstance(value, PhotoTime):
        return {"at": _plain(value.at), "zone": value.zone}
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _listed(value: object, what: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list, not {_r(value)}")
    return value


def _flag(value: object, what: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{what} must be true or false, not {_r(value)}")
    return value


def _counts(raw: object, names: Sequence[str], what: str) -> dict[str, int]:
    _fields(raw, names, what)
    return {name: _whole(raw[name], f"{what}.{name}", low=0) for name in names}


def _day(value: object, what: str) -> dt.date:
    return dt.date.fromisoformat(date(value, what))


def _days(value: object, what: str) -> tuple[dt.date, ...]:
    out = tuple(_day(v, what) for v in _listed(value, what))
    if any(a >= b for a, b in pairwise(out)):
        raise ValueError(f"{what} must be in order and each of them once")
    return out


def _utc(value: object, what: str) -> dt.datetime:
    """A UTC time in the one spelling it is hashed in, which also sorts as text."""
    try:
        at = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        at = None
    if at is None or at.tzinfo is None or at.utcoffset() or at.isoformat() != value:
        raise ValueError(f"{what} must be YYYY-MM-DDTHH:MM:SS+00:00, not {_r(value)}")
    return at.replace(tzinfo=dt.timezone.utc)


def _texts(value: object, kinds: Iterable[str], what: str) -> tuple[str, ...]:
    out = tuple(_text(v, what) for v in _listed(value, what))
    for text in out:
        if kind_of(text) not in kinds:
            raise ValueError(f"{what}: {_r(text)} is of no kind that this version knows")
    return out


def _photo_time(raw: object, what: str) -> PhotoTime:
    _fields(raw, ("at", "zone"), what)
    zone = _whole(raw["zone"], f"{what}.zone")
    if not -12 * 3600 <= zone <= 14 * 3600:
        raise ValueError(f"{what}.zone: {zone} seconds is no clock's")
    return PhotoTime(_at(raw["at"], f"{what}.at"), zone)


def _run_of(raw: object, what: str) -> ListedRun:
    _fields(raw, [f.name for f in fields(ListedRun)], what)
    grade = raw["evidence"]
    if not isinstance(grade, str) or grade not in get_args(evidence.Grade):
        raise ValueError(f"{what}.evidence must be one of "
                         f"{', '.join(get_args(evidence.Grade))}, not {_r(grade)}")
    run = ListedRun(
        region=_whole(raw["region"], f"{what}.region"),
        name=_text(raw["name"], f"{what}.name"),
        first=_day(raw["first"], f"{what}.first"), last=_day(raw["last"], f"{what}.last"),
        days=_days(raw["days"], f"{what}.days"), silent=_days(raw["silent"], f"{what}.silent"),
        photos=_whole(raw["photos"], f"{what}.photos", low=1),
        first_photo=_photo_time(raw["first_photo"], f"{what}.first_photo"),
        last_photo=_photo_time(raw["last_photo"], f"{what}.last_photo"),
        start=_utc(raw["start"], f"{what}.start"), end=_utc(raw["end"], f"{what}.end"),
        evidence=grade, flags=_texts(raw["flags"], _FLAGS, f"{what}.flags"),
        reasons=_texts(raw["reasons"], _REASONS, f"{what}.reasons"), status=raw["status"])
    if not run.days or (run.days[0], run.days[-1]) != (run.first, run.last):
        raise ValueError(f"{what}.days must be from first to last")
    if run.photos < len(run.days):
        raise ValueError(f"{what}.photos: {run.photos} photos on {len(run.days)} days")
    if any(d in run.days or not run.first < d < run.last for d in run.silent):
        raise ValueError(f"{what}.silent must be days between, with no photo")
    for photo, name in ((run.first_photo, "first_photo"), (run.last_photo, "last_photo")):
        if photo.at.date() not in run.days:
            raise ValueError(f"{what}.{name} is on none of its days")
    if run.start > run.end:
        raise ValueError(f"{what}.start is after its end")
    if run.status != (wanted := status_of(run.reasons)):
        raise ValueError(f"{what}.status: {_r(run.status)} does not follow from its reasons, "
                         f"which make it {wanted}")
    if wanted == "there" and {kind_of(r) for r in run.reasons} != {"there"}:
        raise ValueError(f"{what}.reasons: a run that is there has no other reason")
    if ("evidence thin" in run.flags) is not (grade in ("thin", "none")):
        raise ValueError(f"{what}.evidence: {grade} and the flag 'evidence thin' do not agree")
    return run


def _boundary_of(raw: object, what: str) -> Boundary:
    _fields(raw, [f.name for f in fields(Boundary)], what)
    return Boundary(_text(raw["before"], f"{what}.before"), _text(raw["after"], f"{what}.after"),
                    _texts(raw["suggestions"], _SUGGESTIONS, f"{what}.suggestions"),
                    _flag(raw["forced"], f"{what}.forced"))


def _sweep_of(raw: object, what: str) -> SweepRow:
    _fields(raw, [f.name for f in fields(SweepRow)], what)
    return SweepRow(_whole(raw["gap_days"], f"{what}.gap_days", low=1),
                    *(_whole(raw[k], f"{what}.{k}", low=0) for k in ("runs", "suggested", "held")))


def _params_of(raw: object) -> dict:
    _fields(raw, _PARAMS, "params")
    speeds = raw["speeds"]
    _fields(speeds, _SPEEDS, "params.speeds")
    return {
        "since": date(raw["since"], "params.since"),
        "until": None if raw["until"] is None else date(raw["until"], "params.until"),
        "gap_days": _whole(raw["gap_days"], "params.gap_days", low=1),
        "speeds": {k: _number(speeds[k], f"params.speeds.{k}") for k in _SPEEDS},
        "spans": _flag(raw["spans"], "params.spans"),
    }


def _sources_of(rows: Sequence) -> list[dict]:
    out = []
    for i, row in enumerate(rows):
        what = f"sources[{i}]"
        _fields(row, ("kind", "label", "sha256", "first", "last", "count"), what)
        kind, label, sha = row["kind"], _text(row["label"], f"{what}.label"), row["sha256"]
        if not isinstance(kind, str) or kind not in _SOURCES:
            raise ValueError(f"{what}.kind must be one of {', '.join(_SOURCES)}, not {_r(kind)}")
        if not label or "/" in label or "\\" in label:
            raise ValueError(f"{what}.label must be a name without a directory, not {_r(label)}")
        if sha is not None and not (isinstance(sha, str) and len(sha) == 64
                                    and set(sha) <= set("0123456789abcdef")):
            raise ValueError(f"{what}.sha256 must be 64 hex digits or null, not {_r(sha)}")
        if sha is None and kind != "photos":
            raise ValueError(f"{what}.sha256: a {kind} file has one")
        count = _whole(row["count"], f"{what}.count", low=0)
        ends = [None if row[k] is None else date(row[k], f"{what}.{k}") for k in ("first", "last")]
        if (ends == [None, None]) is not (count == 0) or (None in ends and any(ends)):
            raise ValueError(f"{what}.first and last are its days, where there is anything in it")
        if count and ends[0] > ends[1]:
            raise ValueError(f"{what}.first is after its last")
        out.append({"kind": kind, "label": label, "sha256": sha, "first": ends[0],
                    "last": ends[1], "count": count})
    return sorted(out, key=lambda s: (_SOURCES.index(s["kind"]), s["label"], canonical(s)))


def _check_sources(clean: Mapping) -> None:
    n = {kind: sum(s["kind"] == kind for s in clean["sources"]) for kind in _SOURCES}
    if n["photos"] != 1:
        raise ValueError("sources: the photos are there once")
    if clean["no_track"] is not (n["track"] == 0):
        raise ValueError("no_track: there is a track, or the owner said that there is none")
    if n["spans"] != int(clean["params"]["spans"]):
        raise ValueError("params.spans and the spans file in sources do not agree")


def _check_sweep(rows: Sequence[SweepRow]) -> None:
    if any(a.gap_days >= b.gap_days for a, b in pairwise(rows)):
        raise ValueError("sweep: the gaps must be in order and each of them once")


def _check_runs(runs: Sequence[ListedRun]) -> None:
    seen: set[str] = set()
    for run in runs:
        if run.id in seen:  # one answer would stand for two questions
            raise ValueError(f"runs: {run.id} is there twice")
        seen.add(run.id)
    if list(runs) != _in_order(runs):
        raise ValueError("runs must be in the order of their photos")


def _check_boundaries(
    runs: Sequence[ListedRun], bounds: Sequence[Boundary], gap_days: int
) -> None:
    if [(a.id, b.id) for a, b in pairwise(runs)] != [(b.before, b.after) for b in bounds]:
        raise ValueError("boundaries must be one between every two runs, in their order")
    for (a, b), edge in zip(pairwise(runs), bounds, strict=True):
        kinds = {kind_of(s) for s in edge.suggestions}
        forced = _cuts_itself_off(a) or _cuts_itself_off(b)
        if edge.forced is not forced or ("profile" in kinds) is not forced:
            raise ValueError(f"boundaries: {edge.id} is forced, and says so, exactly where a run "
                             "beside it is on the profile")
        if ("gap" in kinds) is not ((b.first - a.last).days > gap_days):
            raise ValueError(f"boundaries: {edge.id} and its gap do not agree with gap_days")


# -- the profile, in the form that is hashed ---------------------------------------------------


def _when_text(value: object, what: str) -> str | None:
    """An end of a visit: a day, a year, or ``None``, in the API's own spelling."""
    try:
        when = value if value is None or not isinstance(value, str) else parse_when(value)
    except ValueError as exc:
        raise ValueError(f"{what}: {exc}") from None
    if when is not None and not hasattr(when, "isoformat"):
        raise ValueError(f"{what}: {_r(value)} is no date")
    return None if when is None else when.isoformat()


def _visit_of(row: object, what: str) -> dict:
    _fields(row, _VISIT, what)
    trip = row["trip_id"]
    return {
        "id": _whole(row["id"], f"{what}.id"),
        "date_from": _when_text(row["date_from"], f"{what}.date_from"),
        "date_to": _when_text(row["date_to"], f"{what}.date_to"),
        "quality": _whole(row["quality"], f"{what}.quality", low=0),
        "trip_id": None if trip is None else _whole(trip, f"{what}.trip_id"),
    }


def visits_rows(visits: Mapping, regions: Iterable[int]) -> dict[str, list[dict]]:
    """Every visit of ``regions``, in the form and the order of :attr:`TripList.visits`.

    ``visits`` is what :func:`~wanderfill.plan.trips.judge` takes, or the ``visits`` of a
    :meth:`~wanderfill.api.client.NomadMania.snapshot`. A region that is not in it was not
    read, and raises :class:`PlanInvalid`: that is not a region without visits.
    """
    out: dict[str, list[dict]] = {}
    try:
        for region in sorted(set(regions)):
            rows = visits.get(region, visits.get(str(region)))
            if rows is None:
                raise ValueError(f"no list of visits for region {region}; read it first, for a "
                                 "list that was not read is not an empty one")
            out[str(region)] = _sorted(_live_visit(v, region) for v in rows)
    except ValueError as exc:
        raise PlanInvalid(str(exc)) from None
    return out


def _live_visit(v: object, region: int) -> dict:
    what = f"a visit of region {region}"
    row = {f.name: getattr(v, f.name) for f in fields(Visit)} if isinstance(v, Visit) else v
    _fields(row, ("region", *_VISIT), what)
    if row["region"] != region:
        raise ValueError(f"{what} is of region {_r(row['region'])}")
    return _visit_of({k: row[k] for k in _VISIT}, what)


def _sorted(rows: Iterable[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (row["id"], canonical(row)))


def _visits_of(raw: object, runs: Sequence[ListedRun]) -> dict[str, list[dict]]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"visits must be an object, not {_r(raw)}")
    wanted = {str(r.region) for r in runs}
    if set(raw) != wanted:
        raise ValueError("visits must have the regions of the runs and no others, not "
                         + _r(sorted(map(str, raw))))
    return {region: _sorted(_visit_of(row, f"visits[{region}]")
                            for row in _listed(raw[region], f"visits[{region}]"))
            for region in sorted(wanted, key=int)}


def _trip_of(row: object, what: str) -> dict:
    _fields(row, _TRIP, what)
    out: dict[str, Any] = {"id": _whole(row["id"], f"{what}.id")}
    for name in _TRIP[1:]:
        out[name] = None if row[name] is None else _text(row[name], f"{what}.{name}")
    return out


def _trip_end(text: str | None) -> When | None:
    """An end of a trip's header; one that cannot be read is none."""
    try:
        return parse_when(text)
    except ValueError:
        return None


def _over(trip: Mapping, run: AnyRun) -> bool:
    """Whether a trip can have a day of the run.

    An end that does not say is taken for open, but a header with no date that can be read is
    over no day: there are many, and they would be over every run.
    """
    ends = [_trip_end(trip[k]) for k in ("date_from", "date_to")]
    if ends == [None, None]:
        return False
    (a, b), (c, d) = map(_ends, ends)
    lo, hi = (a, d) if a <= d else (c, b)
    return lo <= run.last and run.first <= hi


def trips_rows(trips: Iterable[Mapping], runs: Iterable[AnyRun]) -> list[dict]:
    """The headers of the trips over the days of ``runs``, as :attr:`TripList.trips` has them.

    ``trips`` are rows of ``trips/get-trips-for-year-app``, of all the years that the runs
    are in. One that is listed under two years is there once, and one with no dates is not
    there: the visits in it are seen with those of their regions.
    """
    runs = list(runs)
    out: dict[int, dict] = {}
    try:
        for raw in trips:
            if not isinstance(raw, Mapping):
                raise ValueError(f"a trip must be an object, not {_r(raw)}")
            row = _trip_of({"id": _live_id(raw.get("id"))} | {
                k: raw.get(k) for k in _TRIP[1:]}, "a trip")
            if any(_over(row, run) for run in runs) and out.setdefault(row["id"], row) != row:
                raise ValueError(f"trip {row['id']} is listed twice, and not the same")
    except ValueError as exc:
        raise PlanInvalid(str(exc)) from None
    return _sorted(out.values())


def _trips_of(rows: Sequence, runs: Sequence[ListedRun]) -> list[dict]:
    out = [_trip_of(row, f"trips[{i}]") for i, row in enumerate(rows)]
    if len({t["id"] for t in out}) != len(out):
        raise ValueError("trips: one is there twice")
    if apart := [t["id"] for t in out if not any(_over(t, run) for run in runs)]:
        raise ValueError(f"trips: {_r(apart)} over none of these days")
    return _sorted(out)


# -- building one -------------------------------------------------------------------------------


def left_out(
    obs: Iterable[Obs], *, since: dt.date | None, until: dt.date | None, other_origin: int = 0
) -> dict[str, int]:
    """How many photos of these dates are no proof, by reason, for the page to say.

    Each photo has one reason, the first that holds, in the order of the result.
    ``other_origin`` is how many of another origin were not given at all.
    """
    out = dict.fromkeys(_LEFT_OUT, 0)
    out["other_origin"] = other_origin
    for o in obs:
        if (since is None or o.day >= since) and (until is None or o.day <= until) and (
                why := _why_not(o)):
            out[why] += 1
    return out


def _why_not(o: Obs) -> str | None:
    s = o.shot
    if s.origin != "own-camera":
        return "other_origin"
    if s.taken_utc is None:
        return "no_utc"
    if s.use == "none":
        return "span_none"
    if s.kind in ("conflict", "fast"):
        return s.kind
    if s.use == "airports":
        return "span_airports"
    if o.place.state != "inside":
        return o.place.state
    return None if o.proof else "clock"


def build_list(
    runs: Iterable[Run],
    boundaries: Sequence[Boundary],
    *,
    account: int,
    created: str,
    names: Mapping[int, str],
    visits: Mapping,
    trips: Iterable[Mapping],
    params: dict,
    sources: Sequence[dict],
    no_track: bool,
    coverage: dict,
    left_out: dict,
    sweep: Sequence[SweepRow],
    assets: Mapping[str, PhotoAsset],
) -> TripList:
    """The list of these judged runs and of their boundaries.

    ``names`` is the region catalogue, ``visits`` what :func:`visits_rows` takes and
    ``trips`` what :func:`trips_rows` takes. ``assets`` are the ones the runs were judged
    with: the grade of each run must agree with its flag.
    """
    runs = _in_order(runs)
    if missing := sorted({r.region for r in runs} - set(names)):
        raise PlanInvalid("no name for region " + ", ".join(map(str, missing)))
    return TripList(
        account=account, created=created, params=params, sources=list(sources),
        no_track=no_track, coverage=coverage, left_out=left_out, sweep=list(sweep),
        runs=[_listed_run(r, names[r.region], evidence_grade(r, assets)) for r in runs],
        boundaries=list(boundaries), visits=visits_rows(visits, [r.region for r in runs]),
        trips=trips_rows(trips, runs))


def _listed_run(run: Run, name: str, grade: evidence.Grade) -> ListedRun:
    first, last = (PhotoTime(o.shot.taken, o.shot.zone) for o in (run.shots[0], run.shots[-1]))
    utc = dt.timezone.utc
    return ListedRun(run.region, name, run.first, run.last, run.days, run.silent, len(run.shots),
                     first, last, run.start.astimezone(utc), run.end.astimezone(utc), grade,
                     run.flags, run.reasons, run.status)


# -- the page, and what came back from it ----------------------------------------------------

_DATA = "__DATA__"


def render_page(trip_list: TripList) -> str:
    """The page to answer: one file that loads nothing, sends nothing and links to nothing.

    The list goes in with :func:`~wanderfill.plan.manifest.escape_data`, so nothing in a
    name can end the script element it is in, and beside it what the page must not have a
    second copy of. The page writes every text with ``textContent``.
    """
    page = {
        "quality": {str(q): word for q, word in QUALITY.items() if q},
        "no_visit": QUALITY[0],
        "marks_from": MARKS_REGION_FROM,
        "max_words": MAX_WORDS,
        "controls": sorted(_CONTROLS),
        "refused": _NOT_IN_WORDS,
        "blank": SAYS_NOTHING,
        "over": {run.id: [n for n, trip in enumerate(trip_list.trips) if _over(trip, run)]
                 for run in trip_list.runs},
    }
    template = files(__package__).joinpath("trips_page.html").read_text(encoding="utf-8")
    before, after = template.split(_DATA)  # exactly one, or this is another template
    return before + escape_data({**trip_list._stored(), "page": page}) + after


def words_problem(text: str) -> str | None:
    """Why these words of the owner cannot be taken, or ``None``. Nothing is mended."""
    if len(text) > MAX_WORDS:
        return f"too long: {len(text)} of {MAX_WORDS} characters"
    if any(low <= ord(ch) <= high for ch in text for low, high in _NOT_IN_WORDS):
        return "they have a line break or another control character in them"
    if not _says(text):
        return "there is nothing that can be read in them"
    return None


_TAKES: dict[str, frozenset[str]] = {
    "offered": frozenset({"1", "2", "3", "4", "5", "6", "no", "?"}),
    "held": frozenset({"1", "2", "3", "4", "5", "6", "no", "?"}),
    "fixed": frozenset({"no"}),
    "there": frozenset(),
}


def load_answers(path: str | Path, trip_list: TripList) -> Answers:
    """What the owner answered about ``trip_list``, for :func:`~wanderfill.plan.trips.group`.

    What is not in the file was not answered and is not here. A file that answers another
    list, or is for another account, or holds anything the page has no control for, raises
    :class:`PlanInvalid`.
    """
    return read_answers(path, trip_list)[0]


def read_answers(path: str | Path, trip_list: TripList) -> tuple[Answers, str]:
    """:func:`load_answers`, and the sha256 of the bytes that were read."""
    data = read_bytes(path, "answers file", limit=MAX_ANSWERS_BYTES)
    raw = json_of(data, path, "answers file")
    try:
        _fields(raw, ("format", "account", "manifest", "runs", "cuts"), "the answers file")
        if type(raw["format"]) is not int or raw["format"] != ANSWERS_FORMAT:
            raise ValueError(f"format {_r(raw['format'])} is not {ANSWERS_FORMAT}, "
                             "the one this reads")
        if type(raw["account"]) is not int or raw["account"] != trip_list.account:
            raise ValueError(f"these are answers for account {_r(raw['account'])}, and the list "
                             f"is for account {trip_list.account}")
        if raw["manifest"] != trip_list.sha256:
            raise ValueError("these are answers to another list than this one; open the page that "
                             "was made with this list, and answer that")
        runs = _read_each(raw["runs"], "runs", {r.id: r for r in trip_list.runs}, _run_answer)
        cuts = _read_each(raw["cuts"], "cuts", {b.id: b for b in trip_list.boundaries}, _cut)
    except ValueError as exc:
        raise PlanInvalid(f"{path}: {exc}") from None
    return {"runs": runs, "cuts": cuts}, hashlib.sha256(data).hexdigest()


def _read_each(
    raw: object, what: str, known: Mapping[str, Any], read: Callable[[Any, object, str], Any]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be an object, not {_r(raw)}")
    for key in raw:
        if key not in known:
            raise ValueError(f"{what}: {_r(key)} is not on this list")
    return {key: read(known[key], value, f"{what}: {key}") for key, value in raw.items()}


def _run_answer(run: ListedRun, row: object, what: str) -> RunAnswer:
    _fields(row, (), what, ("a", "words"))
    takes = _TAKES[run.status]
    if not row:
        raise ValueError(f"{what} says nothing; an answer that was not given is not there")
    if not takes:
        raise ValueError(f"{what} takes no answer, for it is on the profile already")
    if "a" in row and (not isinstance(row["a"], str) or row["a"] not in takes):
        raise ValueError(f"{what} must be one of {', '.join(sorted(takes))}, not {_r(row['a'])}")
    if "words" in row:
        if run.status != "held":
            raise ValueError(f"{what} takes no words")
        if not isinstance(row["words"], str):
            raise ValueError(f"{what}: the words are not text")
        if problem := words_problem(row["words"]):
            raise ValueError(f"{what}: the words cannot be taken: {problem}")
    return dict(row)  # type: ignore[return-value]


def _cut(edge: Boundary, value: object, what: str) -> str:
    if edge.forced:
        raise ValueError(f"{what} is forced, and takes no answer")
    if not isinstance(value, str) or value not in ("cut", "same"):
        raise ValueError(f"{what} must be cut or same, not {_r(value)}")
    return value


__all__ = [
    "ListedRun",
    "PhotoTime",
    "TripList",
    "build_list",
    "left_out",
    "load_answers",
    "read_answers",
    "render_page",
    "trips_rows",
    "visits_rows",
    "words_problem",
]
