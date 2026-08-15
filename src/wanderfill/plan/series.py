"""Matching a track against NomadMania's 69 series — and knowing when not to.

MEASURED, NOT ASSUMED
---------------------
The obvious approach is to tick every series object within a few kilometres of
somewhere the user has been. Run against a real 4,868-day track and scored
against 392 objects the user had already ticked by hand, that approach performs
like this:

    within  1 km   found  33% of them, while offering 2,770 candidates
    within  2 km   found  50%                          4,721
    within  5 km   found  67%                          7,320
    within 10 km   found  84%                          9,839

There is no threshold that is both honest and useful. The reason is structural.
Series such as *Art Museums*, *Markets* and *Malls/Department Stores* pack
hundreds of objects into the same few square kilometres of a city centre, while
a day-level track carries one point per city. Standing four hundred metres from
a museum is not evidence of going inside it.

So this module refuses to be an auto-marker for those series. It produces a
graded shortlist and prints the recall curve alongside it, so whoever decides
can see the quality of the evidence first.

WHERE PRESENCE REALLY IS THE VISIT
----------------------------------
A handful of series are about *being somewhere* rather than *entering
something*: World Capitals, European Cities, Cities of the Americas, African
Cities, Cities of Asia and Oceania. For those the inference is sound — but the
right evidence turned out not to be distance.

Cities match by NAME. The track carries a reverse-geocoded place name for every
day; if it says Bratislava and the series has an object called Bratislava, that
is direct evidence. Distance fails in both directions here: a metro area's
marker can sit fifteen kilometres from anywhere anyone stays, and passing three
kilometres from a town on a motorway is not visiting it.

Airports are the mirror image. Their objects are named after their city —
*Kyiv – Zhuliany (IEV)* — so a name rule would tick the airport of every city
the user ever reached by train. There the coordinate is the evidence and the
name is noise.

THE SELF-CHECK THAT MATTERS
---------------------------
Before a rule may add anything to a series, it is scored against what is already
ticked there. A rule that cannot re-find what a human already recorded has not
earned the right to add more. On the run this package was built from, that gate
fired on its own and stopped the Airports rule at 25% — no human noticed first.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..geo.tiles import SeriesPoint, haversine_km
from ..sources.base import Track
from ..sources.normalize import components, fold

PRESENCE_SERIES = {
    1: "World Capitals",
    3: "European Cities",
    4: "Cities of the Americas",
    6: "African Cities",
    7: "Cities of Asia and Oceania",
}
AIRPORT_SERIES = {5: "Airports"}

CITY_MAX_KM = 25.0
AIRPORT_MAX_KM = 2.0
MIN_RECALL = 0.30

RADII_KM = (0.5, 1, 2, 3, 5, 10)


@dataclass
class Candidate:
    item: int
    series: int
    name: str
    km: float
    date: dt.date | None
    why: str


@dataclass
class SeriesVerdict:
    """What a rule proposes for one series, and whether it may be trusted."""

    series: int
    title: str
    score: int
    total: int
    rule: str
    recall: float
    located_ticked: int
    candidates: list[Candidate] = field(default_factory=list)
    trusted: bool = True
    note: str = ""


def nearest_days(points: dict[int, SeriesPoint], track: Track) -> dict[int, tuple[float, dt.date]]:
    """For each harvested object, the closest photo-day and its distance.

    A coarse grid keeps this from being a full cross product; series harvests
    run to tens of thousands of objects and tracks to thousands of days.
    """
    grid_size = 0.15  # roughly 16 km cells
    grid: dict[tuple[int, int], list] = {}
    for p in track.points:
        grid.setdefault((int(p.lat / grid_size), int(p.lon / grid_size)), []).append(p)

    out: dict[int, tuple[float, dt.date]] = {}
    for oid, obj in points.items():
        gy, gx = int(obj.lat / grid_size), int(obj.lon / grid_size)
        best_km, best_day = float("inf"), None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for p in grid.get((gy + dy, gx + dx), ()):
                    km = haversine_km(obj.lat, obj.lon, p.lat, p.lon)
                    if km < best_km:
                        best_km, best_day = km, p.date
        if best_day is not None:
            out[oid] = (best_km, best_day)
    return out


def recall_curve(
    nearest: dict[int, tuple[float, dt.date]], already_ticked: set[int]
) -> list[dict]:
    """The table that should be printed before anybody chooses a radius."""
    located = [o for o in already_ticked if o in nearest]
    rows = []
    for r in RADII_KM:
        offered = sum(1 for v in nearest.values() if v[0] <= r)
        found = sum(1 for o in located if nearest[o][0] <= r)
        rows.append(
            {
                "radius_km": r,
                "candidates": offered,
                "already_ticked_found": found,
                "recall": (found / len(located)) if located else 0.0,
            }
        )
    return rows


def match_series(
    series_id: int,
    title: str,
    catalogue_ids: set[int],
    already_ticked: set[int],
    points: dict[int, SeriesPoint],
    nearest: dict[int, tuple[float, dt.date]],
    track: Track,
    *,
    min_recall: float = MIN_RECALL,
) -> SeriesVerdict:
    """Apply the right rule for this series, validate it, then propose."""
    is_airport = series_id in AIRPORT_SERIES
    is_city = series_id in PRESENCE_SERIES
    rule = (
        f"distance <= {AIRPORT_MAX_KM} km"
        if is_airport
        else f"name match, <= {CITY_MAX_KM} km"
        if is_city
        else "shortlist only — proximity is weak evidence for this series"
    )
    places = track.places()

    def evidence(oid: int) -> tuple[bool, str]:
        hit = nearest.get(oid)
        if not hit:
            return False, ""
        km, day = hit
        if is_airport:
            return km <= AIRPORT_MAX_KM, f"{km:.2f} km on {day}"
        if is_city:
            if km > CITY_MAX_KM:
                return False, ""
            for part in components(points[oid].name):
                if part in places:
                    days = places[part]
                    return True, f"name match, {len(days)} photo-day(s), first {min(days)}"
            return False, ""
        return False, ""

    located = [o for o in already_ticked if o in nearest]
    refound = [o for o in located if evidence(o)[0]] if (is_city or is_airport) else []
    recall = len(refound) / len(located) if located else 0.0

    verdict = SeriesVerdict(
        series=series_id,
        title=title,
        score=len(already_ticked),
        total=len(catalogue_ids),
        rule=rule,
        recall=recall,
        located_ticked=len(located),
    )

    if not (is_city or is_airport):
        verdict.trusted = False
        verdict.note = "not a presence-based series; review the shortlist by hand"
        verdict.candidates = [
            Candidate(oid, series_id, points[oid].name, nearest[oid][0], nearest[oid][1], "nearby")
            for oid in nearest
            if oid in catalogue_ids and oid not in already_ticked and nearest[oid][0] <= 3.0
        ]
        verdict.candidates.sort(key=lambda c: c.km)
        return verdict

    if located and recall < min_recall:
        verdict.trusted = False
        verdict.note = (
            f"rule re-finds only {recall:.0%} of what is already ticked here — "
            "not trustworthy enough to add anything"
        )
        return verdict

    for oid in nearest:
        if oid in already_ticked or oid not in catalogue_ids:
            continue
        ok, why = evidence(oid)
        if ok:
            km, day = nearest[oid]
            verdict.candidates.append(Candidate(oid, series_id, points[oid].name, km, day, why))
    verdict.candidates.sort(key=lambda c: c.km)
    return verdict


__all__ = [
    "AIRPORT_SERIES",
    "PRESENCE_SERIES",
    "Candidate",
    "SeriesVerdict",
    "fold",
    "match_series",
    "nearest_days",
    "recall_curve",
]
