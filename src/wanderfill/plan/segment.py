"""Cutting a location track into trips.

This problem has no correct answer and the module says so rather than pretending
otherwise.

Trip detection normally assumes a home to leave and return to. That assumption
is doing a lot of unexamined work, and for a lot of the people this tool is for
it is simply false.

There are three real cases, and :func:`segment` takes ``home`` to name which one
applies rather than guessing:

``home="infer"``
    Home is whichever region dominates each calendar year. A guess, but usually
    a decent one, and it adapts when somebody moves.

``home=[region_id, ...]``
    Home is stated. ``client.home_regions()`` reads what the user told
    NomadMania — but treat that as a *hint*, not as truth. Profile settings go
    stale: somebody who set a home five years ago and has not had one since will
    still have it sitting in their account. Ask before trusting it.

``home=None``
    No home at all. Every day is travel. This is the correct setting for a
    genuinely nomadic person, and getting it wrong is not cosmetic: any other
    setting silently deletes their most-visited region from their own trips.

The other two knobs are a silence longer than ``gap_days`` ending a trip, and a
hard ``cap_days`` ceiling. The cap is the honest part — without it the runs are
unbounded, and with it trips are cut at a length that means nothing beyond "a
person would call this a trip". :func:`sweep` exists so the numbers for several
settings can be put in front of somebody before one is chosen.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass
class Journey:
    start: dt.date
    end: dt.date
    regions: dict[int, tuple[dt.date, dt.date]] = field(default_factory=dict)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def home_by_year(day_regions: dict[dt.date, set[int]]) -> dict[int, int]:
    """The modal region of each calendar year.

    Not a stated home — an inferred one. In 2022-2023 this picks up whichever
    place happened to accumulate the most days, which is the best available
    proxy and is stated as such in the plan.
    """
    per_year: dict[int, Counter] = defaultdict(Counter)
    for day, regions in day_regions.items():
        for r in regions:
            per_year[day.year][r] += 1
    return {year: counts.most_common(1)[0][0] for year, counts in per_year.items() if counts}


def segment(
    day_regions: dict[dt.date, set[int]],
    *,
    gap_days: int = 2,
    cap_days: int = 30,
    home: str | Iterable[int] | None = "infer",
) -> list[Journey]:
    """Cut away-days into journeys.

    ``home`` picks the model rather than assuming one — see the module
    docstring. ``"infer"`` uses the modal region per year, an iterable of ids
    uses those, and ``None`` means the traveller has no home and every day
    counts as travel.
    """
    if not day_regions:
        return []

    if home is None:
        def is_away(day: dt.date, regions: set[int]) -> bool:
            return bool(regions)
    elif home == "infer":
        inferred = home_by_year(day_regions)

        def is_away(day: dt.date, regions: set[int]) -> bool:
            return bool(regions - {inferred.get(day.year)})
    else:
        fixed = set(home)
        if not fixed:
            raise ValueError(
                "home=[] is ambiguous; pass home=None for a traveller with no home"
            )

        def is_away(day: dt.date, regions: set[int]) -> bool:
            return bool(regions - fixed)

    away = sorted(d for d, rs in day_regions.items() if is_away(d, rs))

    journeys: list[Journey] = []
    for day in away:
        if (
            journeys
            and (day - journeys[-1].end).days <= gap_days
            and (day - journeys[-1].start).days < cap_days
        ):
            journeys[-1].end = day
        else:
            journeys.append(Journey(start=day, end=day))

    for j in journeys:
        seen: dict[int, list[dt.date]] = defaultdict(list)
        day = j.start
        while day <= j.end:
            for r in day_regions.get(day, ()):
                seen[r].append(day)
            day += dt.timedelta(days=1)
        j.regions = {r: (min(v), max(v)) for r, v in seen.items()}
    return journeys


def sweep(
    day_regions: dict[dt.date, set[int]],
    gaps=(1, 2, 3, 5),
    caps=(14, 30, 60, 9999),
    home: str | Iterable[int] | None = "infer",
) -> list[dict]:
    """Show what each parameter pair produces, so a human can choose.

    Printing this table is not optional politeness. The difference between a
    30-day cap and no cap is the difference between 214 trips and one trip, and
    the tool has no business making that choice silently.
    """
    rows = []
    for gap in gaps:
        for cap in caps:
            js = segment(day_regions, gap_days=gap, cap_days=cap, home=home)
            rows.append(
                {
                    "gap_days": gap,
                    "cap_days": cap,
                    "trips": len(js),
                    "longest_days": max((j.days for j in js), default=0),
                    "most_regions": max((len(j.regions) for j in js), default=0),
                }
            )
    return rows


def split_first_and_repeat(
    journeys: list[Journey],
) -> tuple[dict[int, tuple[dt.date, dt.date]], list[Journey]]:
    """Separate each region's first journey from its later ones.

    This exists because of a counting trap in the API: a visit counts whether it
    is standalone or owned by a trip. If every region already has a standalone
    visit and you then create trips covering every journey, the totals inflate
    by the number of first visits — 729 real visits become 999.

    With deletions off the table, the way out is to let each region's *first*
    journey stay a standalone record and put only the *repeats* into trips.

    The cost, stated rather than hidden: a trip lists only the regions for which
    it is not the first visit, so some trips show fewer regions than the journey
    really covered, and journeys made entirely of first visits vanish.
    """
    first: dict[int, tuple[dt.date, dt.date]] = {}
    repeats: list[Journey] = []
    seen: set[int] = set()
    for j in journeys:
        rep: dict[int, tuple[dt.date, dt.date]] = {}
        for region, span in sorted(j.regions.items()):
            if region in seen:
                rep[region] = span
            else:
                first[region] = span
                seen.add(region)
        if rep:
            repeats.append(Journey(start=j.start, end=j.end, regions=rep))
    return first, repeats
