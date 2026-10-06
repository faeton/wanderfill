"""Where was I, and when — for the questions other people ask.

A visa form wants "every country visited in the last ten years, excluding the
EU/EEA, with the date of the first trip". ESTA wants a yes or no on Iran since
2011. A tax authority wants days per country per tax year; a citizenship
application wants every absence from one country. All of them are queries over
the same thing: a **ledger** of which country (and region) each day was spent
in, built from every source that can see the day.

This module is pure. It takes points that are already resolved to regions,
profile visits that are already read, and a :class:`CountryMap`, and returns
plain results. No network, no filesystem — the CLI does those, and the tests
hit this directly.

WHY THE RULES ARE THE WAY THEY ARE

On NomadMania the dangerous error is claiming too much. On a visa form it is
the reverse: **omission is "deception"**, and a UK refusal for deception can
carry a ten-year ban. So nothing here quietly drops what it cannot place:

* A profile visit with only a year, or no date at all, cannot be turned into
  days. It becomes an :class:`UndatedClaim` and every answer it could belong to
  shows it as **CHECK**.
* A country seen only at aircraft speed is not presence, and it is not nothing
  either. It goes on an "overflight — you decide" list.
* "No" is never bare. It is "no evidence in sources covering <dates>", because a
  photo library that begins in 2018 has nothing to say about 2014.

And nothing is invented — AGENTS.md rules 6 and 7 hold:

* **Bridging.** A gap of up to ``bridge`` days with the *same* country on both
  edges, nothing else observed in between, and neither edge a border-crossing
  day, is filled — as ``bridged``, never as ``observed``. A day-level photo
  track goes quiet on quiet days; it does not go quiet because somebody popped
  abroad and back unphotographed, and when they did, the other country usually
  shows up. A gap between two *different* countries is never filled: the trip
  ends with an open edge ("left between Mar 3 and Mar 9").
* **Speed.** Consecutive timestamped points faster than ``fast_kmh`` mark a
  point airborne; a country-day with only airborne points is ``airborne``.
  Day-level rows have no clock, so they are never airborne — and never
  provably on the ground either; that is stated in every header.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from .api.client import Visit, YearOnly
from .countries import GROUPS, CountryMap, Place, name_of
from .grade import FAST_KMH, km

BRIDGE_DAYS = 7
OBSERVED, BRIDGED, AIRBORNE = "observed", "bridged", "airborne"
DEFAULT_TERRITORIES = {"days": "sovereign", "absences": "sovereign"}

TAX_YEARS = {
    # name -> (month, day) the tax year starts on
    "calendar": (1, 1),
    "uk": (4, 6),
    "au": (7, 1),
    "nz": (4, 1),
    "in": (4, 1),
    "za": (3, 1),
    "us": (1, 1),
    "es": (1, 1),
    "pt": (1, 1),
    "de": (1, 1),
    "fr": (1, 1),
    "it": (1, 1),
    "nl": (1, 1),
}


# ------------------------------------------------------------------ inputs


@dataclass(frozen=True)
class Point:
    """One resolved observation. ``at`` is None for a day-level row."""

    date: dt.date
    lat: float
    lon: float
    region: int | None
    source: str  # "photos" | "track"
    ref: str = ""  # asset uuid, or "track:<line>"
    at: dt.datetime | None = None


@dataclass(frozen=True)
class UndatedClaim:
    """A profile visit that cannot become days: year-only, or nothing at all."""

    region: int
    place: Place
    region_name: str
    year: int | None
    visit_id: int

    def describe(self) -> str:
        when = f"some time in {self.year}" if self.year else "no date at all"
        return f"NomadMania visit #{self.visit_id} to {self.region_name}: {when}"


@dataclass(frozen=True)
class Coverage:
    source: str
    first: dt.date | None
    last: dt.date | None
    continuous: bool  # a day-by-day record, or a list of claims

    def sees(self, day: dt.date) -> bool:
        return bool(self.first and self.last and self.first <= day <= self.last)

    def describe(self) -> str:
        if not self.first:
            return f"{self.source}: nothing"
        kind = "" if self.continuous else " (claimed visits, not a day-by-day record)"
        return f"{self.source}: {self.first} → {self.last}{kind}"


# ------------------------------------------------------------------ ledger


@dataclass
class Cell:
    """Everything known about one (date, region)."""

    sources: set[str] = field(default_factory=set)
    refs: list[str] = field(default_factory=list)
    ground: bool = False  # at least one point (or a profile day) not airborne


@dataclass(frozen=True)
class Day:
    """One (date, country key) in the ledger."""

    date: dt.date
    key: str
    label: str
    iso: str
    territory: bool
    kind: str
    sources: frozenset[str]
    regions: frozenset[int]


@dataclass
class Ledger:
    cells: dict[tuple[dt.date, int], Cell]
    undated: list[UndatedClaim]
    coverage: list[Coverage]
    cmap: CountryMap
    unresolved_points: int = 0
    airborne_points: int = 0
    bridge: int = BRIDGE_DAYS
    fast_kmh: float = FAST_KMH

    # -- country days, per territory mode, with bridging applied --------------

    def days(self, territories: str = "separate") -> list[Day]:
        per: dict[tuple[dt.date, str], dict[str, Any]] = {}
        for (date, region), cell in self.cells.items():
            place = self.cmap.place(region)
            k = (date, place.key(territories))
            slot = per.setdefault(
                k,
                {
                    "place": place,
                    "ground": False,
                    "sources": set(),
                    "regions": set(),
                },
            )
            slot["ground"] |= cell.ground
            slot["sources"] |= cell.sources
            slot["regions"].add(region)

        out: list[Day] = []
        for (date, key), s in per.items():
            p: Place = s["place"]
            out.append(
                Day(
                    date,
                    key,
                    p.label(territories),
                    p.iso,
                    bool(p.territory) and territories == "separate",
                    OBSERVED if s["ground"] else AIRBORNE,
                    frozenset(s["sources"]),
                    frozenset(s["regions"]),
                )
            )
        out.extend(self._bridges(out))
        out.sort(key=lambda d: (d.date, d.key))
        return out

    def _bridges(self, days: list[Day]) -> list[Day]:
        """Same-country gaps of at most ``bridge`` days, and only those.

        A gap is bridged when both edge days are that country *alone* (an edge
        that also has another country is a border-crossing day, and the gap
        after it may well be the other country), and nothing at all — not even
        an overflight — is recorded strictly inside the gap.
        """
        if self.bridge <= 0:
            return []
        by_date: dict[dt.date, list[Day]] = defaultdict(list)
        for d in days:
            by_date[d.date].append(d)
        seen = sorted(by_date)
        out = []
        for a, b in pairwise(seen):
            gap = (b - a).days - 1
            if not 1 <= gap <= self.bridge:
                continue
            left, right = by_date[a], by_date[b]
            if len(left) != 1 or len(right) != 1:
                continue
            lo, hi = left[0], right[0]
            if lo.key != hi.key or lo.kind != OBSERVED or hi.kind != OBSERVED:
                continue
            for i in range(1, gap + 1):
                out.append(
                    Day(
                        a + dt.timedelta(days=i),
                        lo.key,
                        lo.label,
                        lo.iso,
                        lo.territory,
                        BRIDGED,
                        frozenset(),
                        frozenset(),
                    )
                )
        return out

    def coverage_text(self) -> str:
        return "; ".join(c.describe() for c in self.coverage)

    def continuous_cover(self, day: dt.date) -> list[str]:
        return [c.source for c in self.coverage if c.continuous and c.sees(day)]


def _airborne_flags(points: Sequence[Point], fast_kmh: float) -> list[bool]:
    """Per point: is every timed leg touching it faster than ``fast_kmh``?

    Untimed points are never airborne — a day-level row has no clock, and
    pairing two of them would imply a speed nobody measured. Legs are only
    taken between points of the same source, ordered by time.
    """
    flags = [False] * len(points)
    by_source: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(points):
        if p.at is not None:
            by_source[p.source].append(i)
    for idx in by_source.values():
        idx.sort(key=lambda i: points[i].at)
        fast_legs: dict[int, list[bool]] = defaultdict(list)
        for i, j in pairwise(idx):
            a, b = points[i], points[j]
            secs = (b.at - a.at).total_seconds()
            if secs <= 0:
                continue
            fast = km((a.lat, a.lon), (b.lat, b.lon)) / (secs / 3600) > fast_kmh
            fast_legs[i].append(fast)
            fast_legs[j].append(fast)
        for i, legs in fast_legs.items():
            flags[i] = bool(legs) and all(legs)
    return flags


def _visit_days(v: Visit) -> list[dt.date] | None:
    """The days a dated visit covers, or None if it has no day-precise start."""
    start = v.date_from
    if not isinstance(start, dt.date) or isinstance(start, YearOnly):
        return None
    end = v.date_to if isinstance(v.date_to, dt.date) else start
    if end < start:
        end = start
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def build_ledger(
    points: Iterable[Point],
    visits: Iterable[Visit],
    cmap: CountryMap,
    *,
    bridge: int = BRIDGE_DAYS,
    fast_kmh: float = FAST_KMH,
) -> Ledger:
    """Every source, merged per (date, region). Nothing is dropped.

    ``visits`` should be *every* visit record — standalone and trip-owned. A
    ``trip_id is None`` filter once created fifty duplicate visits; here it
    would silently lose every trip.
    """
    points = list(points)
    flags = _airborne_flags(points, fast_kmh)
    cells: dict[tuple[dt.date, int], Cell] = {}
    unresolved = airborne = 0
    spans: dict[str, list[dt.date]] = defaultdict(list)

    for p, fast in zip(points, flags, strict=True):
        spans[p.source].append(p.date)
        if p.region is None:
            unresolved += 1
            continue
        airborne += fast
        c = cells.setdefault((p.date, p.region), Cell())
        c.sources.add(p.source)
        c.ground |= not fast
        if p.ref and len(c.refs) < 20:
            c.refs.append(p.ref)

    undated: list[UndatedClaim] = []
    for v in visits:
        days = _visit_days(v)
        if days is None:
            year = v.date_from.year if isinstance(v.date_from, YearOnly) else None
            undated.append(
                UndatedClaim(
                    v.region,
                    cmap.place(v.region),
                    cmap.region_name(v.region),
                    year,
                    v.id,
                )
            )
            continue
        for d in days:
            spans["profile"].append(d)
            c = cells.setdefault((d, v.region), Cell())
            c.sources.add("profile")
            c.ground = True
            if len(c.refs) < 20:
                c.refs.append(f"visit:{v.id}")

    coverage = [
        Coverage(src, min(ds) if ds else None, max(ds) if ds else None, src != "profile")
        for src, ds in sorted(spans.items())
    ]
    return Ledger(cells, undated, coverage, cmap, unresolved, airborne, bridge, fast_kmh)


# ------------------------------------------------------------------ trips


@dataclass
class Trip:
    key: str
    label: str
    iso: str
    territory: bool
    entry: dt.date
    exit: dt.date
    observed: int
    bridged: int
    sources: dict[str, tuple[dt.date, dt.date]]  # source -> (first, last) inside trip
    entry_earliest: dt.date | None  # None: no data at all before it
    exit_latest: dt.date | None

    @property
    def days(self) -> int:
        return (self.exit - self.entry).days + 1

    @property
    def entry_open(self) -> bool:
        return self.entry_earliest != self.entry

    @property
    def exit_open(self) -> bool:
        return self.exit_latest != self.exit

    def agreement(self) -> str:
        if len(self.sources) < 2:
            return "one source"
        firsts = {s[0] for s in self.sources.values()}
        lasts = {s[1] for s in self.sources.values()}
        return "agree" if len(firsts) == 1 and len(lasts) == 1 else "disagree"


def trips(ledger: Ledger, territories: str = "separate") -> list[Trip]:
    """Maximal runs of observed+bridged days per country key."""
    days = ledger.days(territories)
    grounded = [d for d in days if d.kind in (OBSERVED, BRIDGED)]
    any_ground = sorted({d.date for d in grounded})
    by_key: dict[str, list[Day]] = defaultdict(list)
    for d in grounded:
        by_key[d.key].append(d)

    import bisect

    def prev_known(day: dt.date) -> dt.date | None:
        i = bisect.bisect_left(any_ground, day)
        return any_ground[i - 1] if i > 0 else None

    def next_known(day: dt.date) -> dt.date | None:
        i = bisect.bisect_right(any_ground, day)
        return any_ground[i] if i < len(any_ground) else None

    out: list[Trip] = []
    for _key, ds in by_key.items():
        ds.sort(key=lambda d: d.date)
        run: list[Day] = []
        for d in [*ds, None]:
            if d is not None and run and (d.date - run[-1].date).days == 1:
                run.append(d)
                continue
            if run:
                out.append(_trip(run, prev_known, next_known))
            run = [d] if d is not None else []
    out.sort(key=lambda t: (t.entry, t.key))
    return out


def _trip(run: list[Day], prev_known, next_known) -> Trip:
    first, last = run[0], run[-1]
    sources: dict[str, list[dt.date]] = defaultdict(list)
    for d in run:
        for s in d.sources:
            sources[s].append(d.date)
    before = prev_known(first.date)
    after = next_known(last.date)
    return Trip(
        key=first.key,
        label=first.label,
        iso=first.iso,
        territory=first.territory,
        entry=first.date,
        exit=last.date,
        observed=sum(d.kind == OBSERVED for d in run),
        bridged=sum(d.kind == BRIDGED for d in run),
        sources={s: (min(v), max(v)) for s, v in sorted(sources.items())},
        entry_earliest=(before + dt.timedelta(days=1)) if before else None,
        exit_latest=(after - dt.timedelta(days=1)) if after else None,
    )


# ----------------------------------------------------------------- report


@dataclass
class Section:
    title: str
    columns: list[str]
    rows: list[dict[str, Any]]
    note: str = ""


@dataclass
class Report:
    """A query's answer: header facts, tables, and the things to say plainly."""

    query: str
    header: dict[str, str]
    sections: list[Section]
    notes: list[str] = field(default_factory=list)


def header(
    ledger: Ledger,
    *,
    command: str,
    since: dt.date | None,
    until: dt.date | None,
    territories: str,
    groups: dict[str, str] | None = None,
    today: dt.date | None = None,
) -> dict[str, str]:
    today = today or dt.date.today()
    h = {
        "command": command,
        "generated": today.isoformat(),
        "window": f"{since or 'beginning'} → {until or today}",
        "sources": ledger.coverage_text() or "none",
        "bridge": f"same-country gaps of up to {ledger.bridge} days are filled as 'bridged'",
        "airborne": f"consecutive timed points faster than {ledger.fast_kmh:.0f} km/h; "
        "day-level rows have no clock and are never airborne",
        "territories": territories,
        "unresolved points": f"{ledger.unresolved_points} (open water, flights, or outside every "
        "polygon — left unplaced)",
        "undated profile visits": f"{len(ledger.undated)} (year only, or no date — shown as CHECK, "
        "never turned into days)",
    }
    for name, expansion in (groups or {}).items():
        h[f"group {name}"] = expansion
    return h


def describe_groups(names: Iterable[str]) -> dict[str, str]:
    out = {}
    for n in names:
        g = GROUPS[n]
        out[n] = f"{', '.join(sorted(g.members))} — {g.note} (as of {g.as_of})"
    return out


def _in_window(start: dt.date, end: dt.date, since: dt.date | None, until: dt.date | None) -> bool:
    return (since is None or end >= since) and (until is None or start <= until)


def _wanted(iso: str, territory: bool, include: set[str] | None, exclude: set[str]) -> bool:
    """Include-list by sovereign; exclude never removes a territory.

    Greenland is Danish and *not* in the EU or EEA. In ``separate`` mode a
    territory is therefore kept even when its sovereign is excluded, and the
    output says why. Over-listing is the safe direction on a declaration.
    """
    if include is not None and iso not in include:
        return False
    return not (iso in exclude and not territory)


def _edge(day: dt.date, open_: bool, bound: dt.date | None, side: str) -> str:
    """``2024-03-09``, or the range the sources leave it in — never a guess inside it."""
    if not open_:
        return str(day)
    if bound is None:
        return f"{day} (no data {'earlier' if side == 'entry' else 'later'} in any source)"
    lo, hi = (bound, day) if side == "entry" else (day, bound)
    return f"{day} ({'entered' if side == 'entry' else 'left'} between {lo} and {hi})"


def _trip_row(t: Trip, ledger: Ledger, since, until) -> dict[str, Any]:
    entry = _edge(t.entry, t.entry_open, t.entry_earliest, "entry")
    exit_ = _edge(t.exit, t.exit_open, t.exit_latest, "exit")
    flags = []
    if since and t.entry < since:
        flags.append("began before the window")
    if until and t.exit > until:
        flags.append("ended after the window")
    if t.territory:
        flags.append(f"territory of {name_of(t.iso)} — check whether the form counts it")
    silent = [
        c.source
        for c in ledger.coverage
        if c.continuous and c.source not in t.sources and c.sees(t.entry)
    ]
    blind = [
        c.source
        for c in ledger.coverage
        if c.continuous and not c.sees(t.entry) and c.source not in t.sources
    ]
    return {
        "country": t.label,
        "iso": t.iso,
        "entry": entry,
        "exit": exit_,
        "days": t.days,
        "observed": t.observed,
        "bridged": t.bridged,
        "sources": ", ".join(f"{s} {a}→{b}" for s, (a, b) in t.sources.items()),
        "agreement": t.agreement(),
        "not seen by": ", ".join(silent),
        "cannot see": ", ".join(blind),
        "flags": "; ".join(flags),
    }


TRIP_COLUMNS = [
    "country",
    "iso",
    "entry",
    "exit",
    "days",
    "observed",
    "bridged",
    "sources",
    "agreement",
    "not seen by",
    "cannot see",
    "flags",
]


def _undated_in(ledger: Ledger, since, until, include, exclude, territories) -> list[UndatedClaim]:
    out = []
    for u in ledger.undated:
        territory = bool(u.place.territory) and territories == "separate"
        if not _wanted(u.place.iso, territory, include, exclude):
            continue
        if u.year is not None:
            if since and u.year < since.year:
                continue
            if until and u.year > until.year:
                continue
        out.append(u)
    return out


def _overflights(ledger: Ledger, days: list[Day], since, until, include, exclude) -> list[dict]:
    grounded = {
        d.key for d in days if d.kind != AIRBORNE and _in_window(d.date, d.date, since, until)
    }
    air: dict[str, list[Day]] = defaultdict(list)
    for d in days:
        if (
            d.kind == AIRBORNE
            and d.key not in grounded
            and _in_window(d.date, d.date, since, until)
            and _wanted(d.iso, d.territory, include, exclude)
        ):
            air[d.key].append(d)
    rows = []
    for _key, ds in sorted(air.items(), key=lambda kv: kv[1][0].date):
        rows.append(
            {
                "country": ds[0].label,
                "iso": ds[0].iso,
                "dates": ", ".join(str(d.date) for d in ds[:12]) + (" …" if len(ds) > 12 else ""),
                "days": len(ds),
                "why": f"every timed point here moved faster than {ledger.fast_kmh:.0f} km/h — "
                "an aircraft, or high-speed rail",
            }
        )
    return rows


def _undated_row(u: UndatedClaim, territories: str) -> dict[str, Any]:
    return {
        "country": u.place.label(territories),
        "iso": u.place.iso,
        "entry": "CHECK",
        "exit": "",
        "days": "",
        "observed": "",
        "bridged": "",
        "sources": "profile",
        "agreement": "",
        "not seen by": "",
        "cannot see": "",
        "flags": u.describe(),
    }


def query_trips(
    ledger: Ledger,
    *,
    since: dt.date | None,
    until: dt.date | None,
    include: set[str] | None = None,
    exclude: set[str] | frozenset[str] = frozenset(),
    first: bool = False,
    territories: str = "separate",
    hdr: dict[str, str] | None = None,
    title: str = "trips",
) -> Report:
    exclude = set(exclude)
    all_trips = [
        t
        for t in trips(ledger, territories)
        if _in_window(t.entry, t.exit, since, until)
        and _wanted(t.iso, t.territory, include, exclude)
    ]
    rows: list[dict[str, Any]] = []
    if first:
        by_key: dict[str, list[Trip]] = defaultdict(list)
        for t in all_trips:
            by_key[t.key].append(t)
        for ts in sorted(by_key.values(), key=lambda ts: ts[0].entry):
            row = _trip_row(ts[0], ledger, since, until)
            row["trips in window"] = len(ts)
            rows.append(row)
    else:
        rows = [_trip_row(t, ledger, since, until) for t in all_trips]

    undated = _undated_in(ledger, since, until, include, exclude, territories)
    dated_keys = {r["iso"] for r in rows}
    check_rows, also = [], []
    for u in undated:
        (also if u.place.iso in dated_keys else check_rows).append(u)
    rows.extend(_undated_row(u, territories) for u in check_rows)

    columns = TRIP_COLUMNS + (["trips in window"] if first else [])
    sections = [Section(title, columns, rows)]
    if also:
        sections.append(
            Section(
                "undated profile visits to countries already listed",
                ["country", "visit"],
                [{"country": u.place.label(territories), "visit": u.describe()} for u in also],
                note="Each may be a separate trip — and with --first, possibly an earlier one.",
            )
        )
    days = ledger.days(territories)
    air = _overflights(ledger, days, since, until, include, exclude)
    if air:
        sections.append(
            Section(
                "probably overflight or transit — you decide",
                ["country", "iso", "dates", "days", "why"],
                air,
                note="Not counted above. Not dropped either: an airside layover may still be a "
                "visit on some forms.",
            )
        )
    notes = [
        "CHECK rows are profile visits that carry no day; they are listed, not guessed.",
        "Open edges ('entered between …') mean the sources have no day in between — the tool "
        "does not pick a date inside that range, and neither should anyone helping you.",
    ]
    if not rows and not air:
        notes.insert(0, f"Nothing in the window. Sources: {ledger.coverage_text() or 'none'}.")
    return Report(title, hdr or {}, sections, notes)


def query_list(ledger: Ledger, **kw) -> Report:
    kw.setdefault("title", "every trip")
    return query_trips(ledger, **kw)


def query_check(
    ledger: Ledger,
    *,
    countries: set[str],
    since: dt.date | None,
    until: dt.date | None,
    territories: str = "separate",
    hdr: dict[str, str] | None = None,
) -> Report:
    ts = [t for t in trips(ledger, territories) if _in_window(t.entry, t.exit, since, until)]
    days = ledger.days(territories)
    rows = []
    for iso in sorted(countries):
        mine = [t for t in ts if t.iso == iso]
        air = [
            d
            for d in days
            if d.iso == iso and d.kind == AIRBORNE and _in_window(d.date, d.date, since, until)
        ]
        und = [u for u in _undated_in(ledger, since, until, {iso}, set(), territories)]
        if mine:
            answer = "YES"
            detail = "; ".join(f"{t.label} {t.entry} → {t.exit}" for t in mine[:6])
            if len(mine) > 6:
                detail += f"; … {len(mine) - 6} more"
        elif air or und:
            answer = "CHECK"
            parts = []
            if air:
                parts.append(f"overflight only: {', '.join(str(d.date) for d in air[:6])}")
            parts.extend(u.describe() for u in und)
            detail = "; ".join(parts)
        else:
            answer = "no evidence"
            detail = f"in sources covering: {ledger.coverage_text() or 'nothing'}"
        rows.append({"country": name_of(iso), "iso": iso, "answer": answer, "evidence": detail})
    return Report(
        "check",
        hdr or {},
        [Section("check", ["country", "iso", "answer", "evidence"], rows)],
        ["'no evidence' is not 'no': it says what the sources could see, and nothing more."],
    )


def tax_year_bounds(
    year: int, scheme: str = "calendar", start: tuple[int, int] | None = None
) -> tuple[dt.date, dt.date, str]:
    m, d = start or TAX_YEARS[scheme]
    begin = dt.date(year, m, d)
    end = dt.date(year + 1, m, d) - dt.timedelta(days=1)
    label = str(year) if (m, d) == (1, 1) else f"{year}/{str(year + 1)[-2:]}"
    return begin, end, label


def query_days(
    ledger: Ledger,
    *,
    year: int,
    scheme: str = "calendar",
    start: tuple[int, int] | None = None,
    territories: str = "sovereign",
    threshold: int = 183,
    today: dt.date | None = None,
    hdr: dict[str, str] | None = None,
) -> Report:
    today = today or dt.date.today()
    begin, end, label = tax_year_bounds(year, scheme, start)
    last = min(end, today)
    days = [d for d in ledger.days(territories) if begin <= d.date <= last]
    counts: dict[str, dict[str, Any]] = {}
    present: set[dt.date] = set()
    for d in days:
        c = counts.setdefault(
            d.key, {"country": d.label, "iso": d.iso, OBSERVED: 0, BRIDGED: 0, AIRBORNE: 0}
        )
        c[d.kind] += 1
        if d.kind != AIRBORNE:
            present.add(d.date)
    span = (last - begin).days + 1 if last >= begin else 0
    unobserved = span - len(present)
    rows = []
    for c in sorted(counts.values(), key=lambda c: -(c[OBSERVED] + c[BRIDGED])):
        n = c[OBSERVED] + c[BRIDGED]
        if n >= threshold:
            status = f"{threshold}+ on this evidence"
        elif n + unobserved >= threshold:
            status = f"could reach {threshold}: {n} counted + {unobserved} days with no data"
        else:
            status = f"under {threshold} even if every day with no data was here"
        rows.append({**c, "present": n, f"vs {threshold}": status})
    notes = [
        f"Tax year {label}: {begin} → {end}"
        + (f" (counted to {last}, today)" if last < end else ""),
        "A day in two countries counts in both — the usual convention for presence tests. "
        "Some regimes count midnights instead; this table does not.",
        f"{unobserved} day(s) in this period have no data in any source. They are neither "
        "here nor there.",
        "Residency is decided by law and the tax authority, not by a day count. These are "
        "counts and margins only.",
    ]
    cols = ["country", "iso", OBSERVED, BRIDGED, "present", AIRBORNE, f"vs {threshold}"]
    return Report(
        "days", hdr or {}, [Section(f"days present, tax year {label}", cols, rows)], notes
    )


def query_absences(
    ledger: Ledger,
    *,
    home: set[str],
    since: dt.date,
    until: dt.date,
    territories: str = "sovereign",
    hdr: dict[str, str] | None = None,
) -> Report:
    """Absences from ``home``: runs of days with no home presence.

    A day with any home presence is a home day — departure and return days
    count as present, which is how most residence rules count them. A day with
    no data at all is **not proven home**: it is its own status, reported in
    its own column, and only an upper bound treats it as away.
    """
    status: dict[dt.date, str] = {}
    abroad: dict[dt.date, set[str]] = defaultdict(set)
    for d in ledger.days(territories):
        if not since <= d.date <= until or d.kind == AIRBORNE:
            continue
        if d.iso in home:
            status[d.date] = "home"
        else:
            abroad[d.date].add(d.label)
            status.setdefault(d.date, "away")
    total = (until - since).days + 1
    seq = []
    for i in range(total):
        day = since + dt.timedelta(days=i)
        seq.append((day, status.get(day, "nodata")))

    rows, nodata_rows = [], []
    run: list[tuple[dt.date, str]] = []
    for day, st in [*seq, (None, "home")]:
        if st != "home":
            run.append((day, st))
            continue
        if run:
            away = [d for d, s in run if s == "away"]
            nod = [d for d, s in run if s == "nodata"]
            first, last = run[0][0], run[-1][0]
            open_start = first == since
            open_end = last == until
            if away:
                where = sorted({c for d in away for c in abroad[d]})
                rows.append(
                    {
                        "from": str(first) + (" (window start)" if open_start else ""),
                        "to": str(last) + (" (window end)" if open_end else ""),
                        "away": len(away),
                        "no data": len(nod),
                        "countries": ", ".join(where),
                    }
                )
            else:
                nodata_rows.append({"from": str(first), "to": str(last), "days": len(nod)})
        run = []

    away_days = [d for d, s in seq if s == "away"]
    nodata_days = [d for d, s in seq if s == "nodata"]
    roll_away = _max_rolling(away_days, 365)
    roll_upper = _max_rolling(sorted(away_days + nodata_days), 365)
    per_year: dict[int, dict[str, int]] = defaultdict(lambda: {"away": 0, "no data": 0, "home": 0})
    for d, s in seq:
        per_year[d.year][{"away": "away", "nodata": "no data", "home": "home"}[s]] += 1

    sections = [
        Section("absences", ["from", "to", "away", "no data", "countries"], rows),
        Section(
            "totals per calendar year",
            ["year", "home", "away", "no data"],
            [{"year": y, **v} for y, v in sorted(per_year.items())],
        ),
    ]
    if nodata_rows:
        sections.append(
            Section(
                "stretches with no data at all",
                ["from", "to", "days"],
                nodata_rows,
                note="Not home and not away — the sources are silent.",
            )
        )
    notes = [
        f"Home: {', '.join(name_of(h) for h in sorted(home))}.",
        f"Most days away in any 365-day window: {roll_away} observed; up to {roll_upper} if every "
        "no-data day was also away.",
        "Departure and return days are counted as home days.",
        "Limits for residence and citizenship are set by law and the authority; this is a count, "
        "not a verdict.",
    ]
    return Report("absences", hdr or {}, sections, notes)


def _max_rolling(days: Sequence[dt.date], window: int) -> int:
    best = i = 0
    for j, d in enumerate(days):
        while (d - days[i]).days >= window:
            i += 1
        best = max(best, j - i + 1)
    return best


def query_where(
    ledger: Ledger,
    *,
    since: dt.date,
    until: dt.date,
    by: str = "region",
    territories: str = "separate",
    hdr: dict[str, str] | None = None,
) -> Report:
    """A day-by-day timeline of the period, collapsed into runs."""
    labels: dict[dt.date, list[str]] = defaultdict(list)
    sources: dict[dt.date, set[str]] = defaultdict(set)
    for d in ledger.days(territories):
        if not since <= d.date <= until:
            continue
        if d.kind == BRIDGED:
            labels[d.date].append(f"{d.label} (bridged)")
        elif d.kind == AIRBORNE:
            labels[d.date].append(f"{d.label} (airborne)")
        elif by == "country":
            labels[d.date].append(d.label)
        else:
            names = sorted(ledger.cmap.region_name(r) for r in d.regions)
            labels[d.date].append(f"{d.label}: {', '.join(names)}")
        sources[d.date] |= d.sources

    rows = []
    cur: tuple[str, ...] | None = None
    start = prev = None
    srcs: set[str] = set()
    total = (until - since).days + 1
    for i in range(total + 1):
        day = since + dt.timedelta(days=i) if i < total else None
        if day is None:
            key = None
        else:
            key = tuple(sorted(labels.get(day, []))) or ("no data",)
        if day is not None and key == cur:
            prev = day
            srcs |= sources.get(day, set())
            continue
        if cur is not None:
            rows.append(
                {
                    "from": str(start),
                    "to": str(prev),
                    "days": (prev - start).days + 1,
                    "where": " · ".join(cur),
                    "sources": ", ".join(sorted(srcs)),
                }
            )
        cur, start, prev, srcs = key, day, day, set(sources.get(day, set())) if day else set()

    und = _undated_in(ledger, since, until, None, set(), territories)
    sections = [
        Section(f"where, {since} → {until}", ["from", "to", "days", "where", "sources"], rows)
    ]
    if und:
        sections.append(
            Section(
                "undated profile visits that may fall in this period",
                ["country", "visit"],
                [{"country": u.place.label(territories), "visit": u.describe()} for u in und],
            )
        )
    return Report(
        "where",
        hdr or {},
        sections,
        ["'no data' means no source has a point that day — not that you were at home."],
    )


# ----------------------------------------------------------------- render


def render_markdown(report: Report) -> str:
    out = [f"# Travel history — {report.query}", ""]
    for k, v in report.header.items():
        out.append(f"- **{k}:** {v}")
    out.append("")
    for s in report.sections:
        out += [f"## {s.title}", ""]
        if s.note:
            out += [s.note, ""]
        if not s.rows:
            out += ["_nothing_", ""]
            continue
        out.append("| " + " | ".join(s.columns) + " |")
        out.append("|" + "---|" * len(s.columns))
        for r in s.rows:
            out.append(
                "| " + " | ".join(str(r.get(c, "")).replace("|", "/") for c in s.columns) + " |"
            )
        out.append("")
    if report.notes:
        out += ["## Read this before copying anything", ""]
        out += [f"- {n}" for n in report.notes]
        out.append("")
    return "\n".join(out)


def render_csv(report: Report) -> str:
    """The first section only — the one a form is filled in from."""
    buf = io.StringIO()
    if not report.sections:
        return ""
    s = report.sections[0]
    w = csv.DictWriter(buf, fieldnames=s.columns, extrasaction="ignore")
    w.writeheader()
    for r in s.rows:
        w.writerow(r)
    return buf.getvalue()


def render_json(report: Report) -> dict:
    return {
        "query": report.query,
        "header": report.header,
        "sections": [
            {"title": s.title, "note": s.note, "columns": s.columns, "rows": s.rows}
            for s in report.sections
        ],
        "notes": report.notes,
    }


def render_text(report: Report, limit: int = 60) -> str:
    """Terminal view: each section as aligned columns, header elided to the essentials."""
    out = []
    for s in report.sections:
        out.append(f"\n{s.title}  ({len(s.rows)})")
        if s.note:
            out.append(f"  {s.note}")
        cols = [c for c in s.columns if any(str(r.get(c, "")) for r in s.rows)] or s.columns
        widths = {
            c: min(48, max([len(c)] + [len(str(r.get(c, ""))) for r in s.rows[:limit]]))
            for c in cols
        }
        out.append("  " + "  ".join(c.ljust(widths[c]) for c in cols))
        for r in s.rows[:limit]:
            out.append(
                "  " + "  ".join(str(r.get(c, ""))[: widths[c]].ljust(widths[c]) for c in cols)
            )
        if len(s.rows) > limit:
            out.append(f"  … {len(s.rows) - limit} more in the files")
    out.append("")
    out += [f"* {n}" for n in report.notes]
    return "\n".join(out)


# ------------------------------------------------------------------ dates


def parse_when(text: str | None, today: dt.date | None = None) -> dt.date | None:
    """ISO date, or ``10y`` / ``18m`` / ``90d`` back from today."""
    if not text:
        return None
    today = today or dt.date.today()
    t = text.strip().lower()
    if t[:-1].isdigit() and t[-1] in "ymd":
        n = int(t[:-1])
        if t[-1] == "d":
            return today - dt.timedelta(days=n)
        months = n * 12 if t[-1] == "y" else n
        y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
        m += 1
        day = today.day
        while True:
            try:
                return dt.date(y, m, day)
            except ValueError:
                day -= 1
    return dt.date.fromisoformat(text)


def parse_period(text: str, today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """``2024-03-01..2024-04-15``, ``2024-03`` (a month), or ``2024`` (a year)."""
    if ".." in text:
        a, b = text.split("..", 1)
        return parse_when(a, today), parse_when(b, today) if b else (today or dt.date.today())
    if len(text) == 4 and text.isdigit():
        y = int(text)
        return dt.date(y, 1, 1), dt.date(y, 12, 31)
    if len(text) == 7 and text[4] == "-":
        y, m = int(text[:4]), int(text[5:])
        nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
        return dt.date(y, m, 1), nxt - dt.timedelta(days=1)
    d = dt.date.fromisoformat(text)
    return d, d
