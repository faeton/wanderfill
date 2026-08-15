"""Cutting a location track into trips.

This problem has no correct answer and the module says so rather than pretending
otherwise.

Trip detection normally assumes a home to leave and return to. That assumption
is doing a lot of unexamined work, and for a lot of the people this tool is for
it is simply false.

There are four real cases, and :func:`segment` takes ``home`` to name which one
applies rather than guessing:

``home="infer"``
    Home is whichever region dominates each calendar year. A guess, and a worse
    one than it looks: **modal inference cannot tell "lived there" from "kept
    coming back"**. On a real 17-year track it named a country home for a year
    in which the traveller had spent 69 days there and had no home at all. Use
    it to get a first look at a track, not to decide anything.

``home=[region_id, ...]``
    Home is stated. ``client.home_regions()`` reads what the user told
    NomadMania — but treat that as a *hint*, not as truth. Profile settings go
    stale: somebody who set a home five years ago and has not had one since will
    still have it sitting in their account. Ask before trusting it.

``home=[HomeWindow(...), ...]``
    Home is stated *and dated*. Most long tracks need this one, because most
    people's homes start and stop. A traveller who lived in one city until 2022,
    kept a base somewhere else for eight months in 2024-25, and had no home
    either side of that is not describable by any single set of regions — and
    both other models get them wrong in opposite directions.

``home=None``
    No home at all. Every day is travel. The correct setting for a genuinely
    nomadic person, and getting it wrong is not cosmetic: any other setting
    silently deletes their most-visited region from their own trips.

Then the cutting itself. A silence longer than ``gap_days`` ends a trip, a hard
``cap_days`` ceiling bounds it, and ``split_on_jump`` ends it when a day shares
no region with the day before.

That last one has a precondition, and it is worth stating plainly because both
settings have produced garbage on real data.

Jump-cutting works because a travel day carries photographs from *both* ends: a
drive or a connecting flight keeps a journey whole, while stepping off a plane
somewhere unrelated starts a new one. Without it, a densely photographed month
never breaks on a gap and the cap alone does the cutting — a cap cuts at a
number of days, which is not a fact about the journey. One real month came out
as a single "trip" spanning a home city, a nine-country drive and a flight to
the Arctic; jump-cutting gave the four journeys a person would name.

But it only works if the track keeps every point of a day. Run it against a
track already collapsed to one region per day and *every* move is a jump: the
same person's 17-year history went from 326 journeys to 1,155, of which 38% were
a single day. Fragmenting a two-week trip into fourteen daily ones is not more
accurate, it is unusable.

So ``split_on_jump`` defaults to ``"auto"``, which measures the track before
deciding, and the two explicit settings are there for when the measurement is
wrong. See :func:`multi_region_share`.

:func:`sweep` exists so the numbers for several settings can be put in front of
somebody before one is chosen.
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


@dataclass(frozen=True)
class HomeWindow:
    """Somewhere that was home, for as long as it was.

    ``start`` and ``end`` are inclusive, and either may be ``None`` for an open
    end — ``HomeWindow(None, date(2022, 2, 1), {123})`` reads as "home until
    February 2022, and we are not saying when it began".

    Days falling in no window have no home, which is the point: the gaps between
    the windows are the periods of having nowhere to come back to, and they are
    stated rather than inferred away.

    A window does not claim the person was *there* the whole time. It claims
    that while it was open, days in those regions were ordinary life rather than
    travel. Somebody based in one place for eight months while flying in and out
    constantly is described exactly by one window; they are described by nothing
    else.
    """

    start: dt.date | None
    end: dt.date | None
    regions: frozenset[int]

    def __init__(self, start: dt.date | None, end: dt.date | None, regions: Iterable[int]):
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "regions", frozenset(regions))
        if start and end and end < start:
            raise ValueError(f"home window ends before it starts: {start} .. {end}")
        if not self.regions:
            raise ValueError(
                "a HomeWindow with no regions says nothing; leave the period "
                "uncovered instead, which is how 'no home then' is expressed"
            )

    def covers(self, day: dt.date) -> bool:
        return (self.start is None or day >= self.start) and (
            self.end is None or day <= self.end
        )


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


def away_test(
    day_regions: dict[dt.date, set[int]],
    home: str | Iterable[int] | Iterable[HomeWindow] | None,
):
    """Build the "was this day travel?" predicate for one home model.

    Separated from :func:`segment` because it is the part worth inspecting. Two
    home models can disagree about a third of a person's life, and a caller that
    wants to show somebody *which* days a model is about to discard needs the
    test itself, not the journeys that come out the far end.
    """
    if home is None:
        return lambda day, regions: bool(regions)

    if home == "infer":
        inferred = home_by_year(day_regions)
        return lambda day, regions: bool(regions - {inferred.get(day.year)})

    items = list(home)
    if not items:
        raise ValueError(
            "home=[] is ambiguous; pass home=None for a traveller with no home"
        )

    if all(isinstance(i, HomeWindow) for i in items):
        windows: list[HomeWindow] = items

        def is_away(day: dt.date, regions: set[int]) -> bool:
            # Windows may overlap — a move takes time, and for a while two
            # places are both home. Everything covering the day counts.
            at_home: set[int] = set()
            for w in windows:
                if w.covers(day):
                    at_home |= w.regions
            return bool(regions - at_home)

        return is_away

    if any(isinstance(i, HomeWindow) for i in items):
        raise TypeError(
            "home mixes HomeWindow with bare region ids; a bare id means "
            "'home for the whole track', which is almost certainly not what "
            "was meant alongside a dated window"
        )

    fixed = {int(i) for i in items}
    return lambda day, regions: bool(regions - fixed)


MULTI_REGION_FLOOR = 0.10


def multi_region_share(day_regions: dict[dt.date, set[int]]) -> float:
    """How much of the track keeps more than one region per day.

    This is the question "does a travel day in this track carry both ends of the
    journey?", asked of the data instead of assumed. A track built from a photo
    library with its points intact runs well above the floor. A track already
    collapsed to one place per day sits at exactly zero — and on such a track
    jump-cutting turns every single move into a new journey.
    """
    if not day_regions:
        return 0.0
    return sum(1 for rs in day_regions.values() if len(rs) > 1) / len(day_regions)


def segment(
    day_regions: dict[dt.date, set[int]],
    *,
    gap_days: int = 2,
    cap_days: int = 30,
    home: str | Iterable[int] | Iterable[HomeWindow] | None = "infer",
    split_on_jump: bool | str = "auto",
) -> list[Journey]:
    """Cut away-days into journeys.

    ``home`` picks the model rather than assuming one. ``split_on_jump`` ends a
    journey when a day shares no region with the previous one; ``"auto"`` turns
    it on only when the track is detailed enough for the rule to mean anything
    — see the module docstring.
    """
    if not day_regions:
        return []

    if split_on_jump == "auto":
        split_on_jump = multi_region_share(day_regions) >= MULTI_REGION_FLOOR
    elif not isinstance(split_on_jump, bool):
        raise ValueError(f"split_on_jump must be True, False or 'auto', not {split_on_jump!r}")

    is_away = away_test(day_regions, home)
    away = sorted(d for d, rs in day_regions.items() if is_away(d, rs))

    journeys: list[Journey] = []
    previous: dt.date | None = None
    for day in away:
        jump = (
            split_on_jump
            and previous is not None
            and not (day_regions[day] & day_regions.get(previous, set()))
        )
        if (
            journeys
            and not jump
            and (day - journeys[-1].end).days <= gap_days
            and (day - journeys[-1].start).days < cap_days
        ):
            journeys[-1].end = day
        else:
            journeys.append(Journey(start=day, end=day))
        previous = day

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
    home: str | Iterable[int] | Iterable[HomeWindow] | None = "infer",
    split_on_jump: bool | str = "auto",
) -> list[dict]:
    """Show what each parameter pair produces, so a human can choose.

    Printing this table is not optional politeness. The difference between a
    30-day cap and no cap is the difference between 214 trips and one trip, and
    the tool has no business making that choice silently.
    """
    rows = []
    for gap in gaps:
        for cap in caps:
            js = segment(
                day_regions,
                gap_days=gap,
                cap_days=cap,
                home=home,
                split_on_jump=split_on_jump,
            )
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


def compare_homes(
    day_regions: dict[dt.date, set[int]],
    models: dict[str, str | Iterable[int] | Iterable[HomeWindow] | None],
    **kwargs,
) -> list[dict]:
    """Put the home models side by side, in days rather than in trips.

    The number that matters when choosing a home model is not how many journeys
    fall out — it is **how many days the model throws away**. Those are the days
    it has decided were ordinary life, and if it is wrong they are travel that
    will never reach the profile. On one real track the difference between two
    defensible models was 261 visits.
    """
    total = len(day_regions)
    rows = []
    for name, home in models.items():
        is_away = away_test(day_regions, home)
        away = sum(1 for d, rs in day_regions.items() if is_away(d, rs))
        js = segment(day_regions, home=home, **kwargs)
        rows.append(
            {
                "model": name,
                "days_travel": away,
                "days_home": total - away,
                "share_home": round((total - away) / total, 3) if total else 0.0,
                "trips": len(js),
                "region_visits": sum(len(j.regions) for j in js),
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
