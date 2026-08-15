"""Cutting a location track into trips.

This problem has no correct answer and the module says so rather than pretending
otherwise.

Trip detection normally assumes a home to leave and return to. For a
continuously nomadic traveller there are years with no home at all, and naive
segmentation produces a single "trip" of 465 days covering 101 regions — which
is a period of someone's life, not a journey.

What works in practice is three deliberate, arbitrary choices, each of which the
user should see and be able to change:

  * home is whichever region dominates that calendar year
  * a silence longer than ``gap_days`` ends a trip
  * no trip may run longer than ``cap_days``

The cap is the honest part. Without it the runs are unbounded; with it, trips
are cut at a length that has no meaning beyond "a person would call this a
trip". :func:`sweep` exists so the numbers for several settings can be put in
front of somebody before one is chosen.
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
    declared_home: Iterable[int] | None = None,
) -> list[Journey]:
    """Cut away-days into journeys.

    ``declared_home`` comes from ``client.home_regions()`` — the regions the
    user has actually told NomadMania they live in. When it is available it
    beats the modal-region inference outright, because it is a statement rather
    than a guess. The inference stays as the fallback for years the declared
    home does not cover.
    """
    if not day_regions:
        return []
    inferred = home_by_year(day_regions)
    fixed = set(declared_home or ())

    def is_away(day: dt.date, regions: set[int]) -> bool:
        home_today = fixed | {inferred.get(day.year)}
        return bool(regions - home_today)

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
) -> list[dict]:
    """Show what each parameter pair produces, so a human can choose.

    Printing this table is not optional politeness. The difference between a
    30-day cap and no cap is the difference between 214 trips and one trip, and
    the tool has no business making that choice silently.
    """
    rows = []
    for gap in gaps:
        for cap in caps:
            js = segment(day_regions, gap_days=gap, cap_days=cap)
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
