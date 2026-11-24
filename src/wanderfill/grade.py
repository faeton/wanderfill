"""Deciding whether a coordinate represents a visit.

This module exists because the rule lived in prose. A previous run graded KYE
quadrants with a speed test written in a throwaway script and described in
markdown, which is a rule that drifts the moment somebody re-implements it from
the paragraph. It is code now so it can be argued with, tested, and changed in
one place.

WHAT IT IS FOR

A coordinate inside a boundary proves a camera was there. It does not prove a
person was. The four ways that goes wrong, in the order they were met:

  * **Aircraft.** A photo from seat 27A lands in a cell exactly like a week on
    the ground. Two mid-Pacific quadrants 1,100 km apart 2.5 hours apart were
    one flight.
  * **Airside transit.** Twenty-six minutes in Houston Terminal E is slow, so
    speed says nothing. Time spread is what catches it.
  * **A single stray point.** One undated day-level row on the far side of an
    island is not evidence of the capital.
  * **A frozen fix.** A phone can stamp one stale coordinate on every photo for
    hours in flight. Twelve of them over 2.75 hours look like a day on the ground
    at 0 km/h, and two of them either side of midnight look like two days. So a
    run of photos with exactly one coordinate counts once, at the first, and the
    speed out of it is timed from the last. A phone lying on a table repeats
    itself too, and the numbers cannot tell the two apart: that is a hold, and
    ``Verdict.repeated`` says how many photos were counted with another.

Somebody else's photo, or one stamped by glasses with a coordinate from another
country, is not among them: nothing here can see it. ``PhotoAsset.origin`` can.

WHAT IT IS NOT

Not a classifier, and the thresholds are not laws. **High-speed rail runs at
200–350 km/h**, so the aircraft test will flag a Shinkansen day as airborne;
that is why the verdict is *hold for a human*, never *reject*. Anyone applying
this to a rail-heavy archive should raise ``fast_kmh`` and say so.

Sources differ in what they can answer. A per-photo library has timestamps to
the second and supports all of this. A day-level track has one row per city and
**no intra-day speed at all**, so speed is simply unavailable for those points —
they are graded on day count alone, and this module does not pretend otherwise.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

# Above this, consecutive timestamped points are travel no person makes on the
# ground — except by high-speed rail, which is the documented false positive.
FAST_KMH = 200.0
# A single day needs this many points over this many hours to stand alone.
LONE_DAY_POINTS = 10
LONE_DAY_HOURS = 2.0
# A coordinate with no more decimals than this was rounded on the way here, and
# rounded coordinates repeat whenever somebody stays within ten metres.
ROUNDED_DECIMALS = 4


@dataclass(frozen=True)
class Fix:
    """One observation: where, when, and whether the time is real."""

    lat: float
    lon: float
    day: dt.date
    at: dt.datetime | None = None  # None for day-level rows — no intra-day clock


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    days: int
    points: int
    span_hours: float
    fast_legs: int
    timed_legs: int
    repeated: int = 0  # photos counted with the first of their run, not on their own


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance. Local enough for a leg between two photos."""
    r, p = 6371.0, math.pi / 180
    return 2 * r * math.asin(
        math.sqrt(
            math.sin((b[0] - a[0]) * p / 2) ** 2
            + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin((b[1] - a[1]) * p / 2) ** 2
        )
    )


def _decimals(x: float) -> int:
    """Decimals in the float's shortest repr, which is how the source wrote it.

    Not a comparison within a tolerance: 48.1234 is stored as a binary fraction
    near 48.1234, and repr is what gives back the four digits it was written with.
    Through Decimal because beside the equator the repr is ``2e-05``.
    """
    return -Decimal(repr(float(x))).as_tuple().exponent


def _runs(fixes: Iterable[Fix]) -> list[list[Fix]]:
    """The timed fixes in order, with a coordinate that repeats gathered into one run.

    Only consecutive, exactly equal, unrounded coordinates make a run. Two GPS
    readings of one place nearly always differ past the fourth decimal; two copies
    of one reading never do. Untimed fixes are not here, so they neither join a run nor break one.
    """
    runs: list[list[Fix]] = []
    for f in sorted((f for f in fixes if f.at), key=lambda f: f.at):
        if runs:
            last = runs[-1][-1]
            if (f.lat, f.lon) == (last.lat, last.lon) and (
                max(_decimals(f.lat), _decimals(f.lon)) > ROUNDED_DECIMALS
            ):
                runs[-1].append(f)
                continue
        runs.append([f])
    return runs


def looks_airborne(fixes: Sequence[Fix], *, fast_kmh: float = FAST_KMH) -> tuple[int, int]:
    """``(fast_legs, timed_legs)`` between consecutive *timestamped* fixes.

    Untimed fixes are skipped rather than assumed simultaneous — pairing two
    day-level rows would imply a speed the data never measured. Inside a run of
    one repeated coordinate there is no leg: 0 km/h between two copies of a stale
    fix was not measured either, and eleven of them outvote the jump that shows
    the aircraft. The leg out of a run is timed from its last fix.
    """
    fast = legs = 0
    for before, after in pairwise(_runs(fixes)):
        a, b = before[-1], after[0]
        seconds = (b.at - a.at).total_seconds()
        if seconds <= 0:
            continue
        legs += 1
        if km((a.lat, a.lon), (b.lat, b.lon)) / (seconds / 3600) > fast_kmh:
            fast += 1
    return fast, legs


def grade(
    fixes: Iterable[Fix],
    *,
    fast_kmh: float = FAST_KMH,
    lone_day_points: int = LONE_DAY_POINTS,
    lone_day_hours: float = LONE_DAY_HOURS,
) -> Verdict:
    """Was somebody *there*, on this evidence?

    ``ok`` means the evidence stands on its own. ``not ok`` means **hold it for
    the user**, which is not the same as rejecting it: on one profile the held
    pile included St Petersburg, sitting genuinely inside its quadrant by
    0.002° of latitude on a single undated row.

    Days, points and span are counted with each run of one repeated coordinate
    taken once, at its first photo. If what is left still stands, so does the
    verdict: a burst in one spot does not hold a day that moves.
    """
    fixes = list(fixes)
    if not fixes:
        return Verdict(False, "no coordinates", 0, 0, 0.0, 0, 0)

    timed = [run[0] for run in _runs(fixes)]
    kept = [*timed, *(f for f in fixes if not f.at)]
    repeated = len(fixes) - len(kept)
    days = {f.day for f in kept}
    span = (timed[-1].at - timed[0].at).total_seconds() / 3600 if timed else 0.0
    fast, legs = looks_airborne(fixes, fast_kmh=fast_kmh)
    flying = legs > 0 and fast / legs >= 0.5
    counts = (len(days), len(kept), span, fast, legs, repeated)
    note = ""
    if repeated:
        note = (
            f"; one coordinate repeated, so {repeated} more photo(s) counted only with the "
            "first of their run — it may be a frozen fix, or a phone lying still"
        )

    if flying:
        detail = f"{fast}/{legs} legs above {fast_kmh:.0f} km/h — airborne, or high-speed rail"
        return Verdict(False, detail + note, *counts)
    if len(days) >= 2:
        return Verdict(True, f"{len(days)} distinct days", *counts)
    if len(kept) >= lone_day_points and span >= lone_day_hours:
        return Verdict(
            True, f"one day, {len(kept)} points over {span:.1f}h at ground speed", *counts
        )
    detail = f"single day, {len(kept)} point(s)"
    if span:
        detail += f", span {span * 60:.0f} min"
    return Verdict(False, detail + note, *counts)
