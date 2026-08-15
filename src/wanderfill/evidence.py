"""Turning a photo library into a proof dossier.

WHY THIS EXISTS
---------------
NomadMania does not score on trust alone. Its top-ranked travellers are
*verified*: a committee picks a random sample of the regions you claim — sixty
of them — and asks you to prove you were there. At least forty-five of the
answers have to be what they call Class 1 evidence, and the clock is six months.
The same happens for countries at rank 100 and above, and again, harder, for
the Supreme badge.

Their Class 1 list reads like a description of a geotagged photo library:

    selfies with a prominent landmark
    serial photos within the region
    screenshots of diaries with dates and places
    hotel bills in the name of the traveller
    ATM withdrawals mentioning location and date

Class 2 — ordinary photos, a friend's word, a map of the journey — is filler
you are allowed for the remaining fifteen.

So somebody with a decade of geotagged photos already holds the evidence. What
they do not hold is an *index*: when the committee names sixty regions, the
work is finding five defensible photos for each one, by region, across a
hundred thousand assets, in six months, by hand. That is the entire problem
this module solves. It is retrieval, not proof generation.

WHAT IT PRODUCES, AND WHAT IT NEVER DOES
----------------------------------------
Local files only: a Markdown dossier, a JSON manifest, and — because most of a
modern library lives in iCloud rather than on the disk — a script that exports
the shortlisted originals out of Photos.

It never writes to NomadMania. There is no plan file and no ``--apply``,
because there is nothing to apply. It is the one part of this package that
reads your profile and answers back to *you*.

THE HONEST LIMITS, STATED UP FRONT
----------------------------------
- A photo proves a *camera* was somewhere. The committee knows this, which is
  why a selfie outranks an ordinary photo. This module can read Apple's
  front-camera flag; it cannot see whether a landmark is in the frame.
- Photos that somebody sent you, and screenshots, are excluded at the source —
  see :mod:`wanderfill.sources.photos_app`. They are evidence about other
  people's travel and about screens.
- A region graded ``none`` here is not a region you did not visit. Cameras run
  out of battery and film-era travel has no EXIF at all. It is a region whose
  proof is somewhere other than this library, and knowing which ones those are,
  before a committee asks, is the point.
- Non-photo Class 1 evidence — hotel bills, ATM slips, diaries — is stronger
  than anything here and lives in nobody's photo library. The dossier leaves a
  slot for it rather than pretending it does not exist.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .geo.tiles import haversine_km
from .sources.photos_app import PhotoAsset

# ---------------------------------------------------------------- thresholds

MIN_SERIAL_SHOTS = 3
"""Below this, "serial photos within the region" is not a phrase you can use."""

MIN_SERIAL_DAYS = 2
"""One day in a region is one day. Two is a stay, and a stay is harder to fake."""

MIN_SERIAL_SPREAD_KM = 1.0
"""Photos taken in one spot are one photo with variations.

Movement inside a region is what distinguishes a visit from a layover, and it
is the thing a verifier can check against a map.
"""

MIN_SERIAL_SPOTS = 3
"""Distinct places, at roughly a kilometre's resolution.

The escape hatch for the day trip: several separate spots in one day is
movement through a region, and it is the shape most of the difficult regions
get visited in.
"""

MIN_SINGLE_DAY_HOURS = 2.0
"""How long a single day's photos must span before that day counts as a visit.

Without this, three frames taken twenty minutes apart on a motorway satisfy
"serial photos within the region" on paper. They are a transit, and a verifier
reading the timestamps will say so. Two hours is a default, not a truth: raise
it if your travel is mostly stays, lower it if you genuinely see places in an
afternoon. It only ever applies to regions with a single day of photos — a
region visited across two days is judged on the days.
"""

MIN_SMALL_SPOTS = 2
"""Distinct spots that qualify a region too small to walk a kilometre across.

Vatican City is 500 m wide. Monaco, Gibraltar, Macau and a dozen island regions
are the same shape of problem: a hard kilometre threshold grades them ``thin``
no matter how long somebody stayed, because the distance does not exist to be
travelled. Two separate spots — about a hundred metres apart — on two separate
days is the alternative route, and it still refuses the balcony-for-three-days
case, which has one spot.

**This route only opens for regions that are actually small**, measured against
the polygon. Keying it on how clustered the photos are instead — the obvious
shortcut — grants it to somebody who spent two days in one hotel in Nairobi,
because that also produces a few coordinates a few hundred metres apart. The
two cases are indistinguishable in a photo library and only one of them is a
visit, so the region has to be measured, not inferred.
"""

SMALL_REGION_KM = 8.0
"""Across-the-diagonal size below which the small-region route may apply.

Generous on purpose: it is measured from a bounding box, so a long thin region
reads bigger than it is, and the route still demands two days and two separate
spots. Vatican City is 0.5, Monaco 3, Gibraltar 5, Macau 12 — Macau does not
qualify, and that is the honest answer, because you can walk further than a
kilometre in Macau.
"""

SMALL_SPOT_PRECISION = 3
"""~110 m. The resolution at which two places inside a tiny region differ."""

Grade = Literal["strong", "serial", "thin", "none"]

GRADE_MEANING = {
    "strong": "serial photos plus a front-camera shot — Class 1 if a landmark is in frame",
    "serial": (
        f"≥{MIN_SERIAL_SHOTS} photos, ≥{MIN_SERIAL_SPREAD_KM:g} km apart, over "
        f"≥{MIN_SERIAL_DAYS} days or ≥{MIN_SERIAL_SPOTS} spots in a day spanning "
        f"≥{MIN_SINGLE_DAY_HOURS:g}h; or ≥{MIN_SMALL_SPOTS} spots over "
        f"≥{MIN_SERIAL_DAYS} days in a region too small to cross"
    ),
    "thin": "photos exist but they are one day, one spot, or too few",
    "none": "no photo in this library falls in this region",
}


@dataclass(frozen=True)
class Rules:
    """The thresholds a dossier was graded with.

    Bundled into an object, and recorded in the output, because these are
    *judgement calls about somebody else's travel* and the right values depend
    on how that person travels. A dossier that does not say which numbers
    produced it cannot be argued with, and the whole point of the file is to be
    argued with before a committee does the arguing.
    """

    shots: int = MIN_SERIAL_SHOTS
    days: int = MIN_SERIAL_DAYS
    spread_km: float = MIN_SERIAL_SPREAD_KM
    spots: int = MIN_SERIAL_SPOTS
    single_day_hours: float = MIN_SINGLE_DAY_HOURS
    small_spots: int = MIN_SMALL_SPOTS

    def as_dict(self) -> dict:
        return {
            "min_shots": self.shots,
            "min_days": self.days,
            "min_spread_km": self.spread_km,
            "min_spots": self.spots,
            "min_single_day_hours": self.single_day_hours,
            "min_small_region_spots": self.small_spots,
        }


DEFAULT_RULES = Rules()


# --------------------------------------------------------------- the records


@dataclass(frozen=True)
class Shot:
    """One photo, attributed to one region.

    ``near_boundary`` is carried all the way to the dossier on purpose. A photo
    that fell outside every polygon and was attributed to the nearest one is a
    photo near a border, and offering it as proof of the region on the wrong
    side of that border is how an honest traveller ends up accused.
    """

    asset: PhotoAsset
    region: int
    near_boundary: bool = False
    distance_deg: float = 0.0

    @property
    def date(self) -> dt.date:
        return self.asset.date


@dataclass
class RegionEvidence:
    """Everything this library can say about one region."""

    region: int
    name: str
    claimed: bool
    shots: list[Shot] = field(default_factory=list)
    exhibits: list[Shot] = field(default_factory=list)
    visit_windows: list[tuple[dt.date, dt.date]] = field(default_factory=list)
    rules: Rules = DEFAULT_RULES
    small: bool = False
    """The region is genuinely too small to cross — measured, not inferred."""
    documents: list[str] = field(default_factory=list)
    """Non-photo evidence you filed yourself: bills, stamps, tickets, diaries.

    Never graded and never counted toward a grade — a file called ``hotel.pdf``
    could be anything, and a tool that scored it would be inventing proof. It
    is indexed so that the dossier is one place rather than two, and so a
    region with no photos but a hotel bill stops reading as a hole.
    """

    # -- derived ----------------------------------------------------------

    @property
    def days(self) -> list[dt.date]:
        return sorted({s.date for s in self.shots})

    @property
    def spots(self) -> int:
        return spots(self.shots)

    @property
    def small_spots(self) -> int:
        """Spots at ~110 m — the resolution that matters inside a tiny region."""
        return small_region_spots(self.shots)

    @property
    def selfies(self) -> int:
        return sum(1 for s in self.shots if s.asset.selfie)

    @property
    def near_boundary(self) -> int:
        return sum(1 for s in self.shots if s.near_boundary)

    @property
    def spread_km(self) -> float:
        """How far apart the two furthest photos in this region really are."""
        return diameter_km(self.shots)

    @property
    def places(self) -> list[str]:
        """Photos' own names for where these were taken, commonest first."""
        counts: dict[str, int] = {}
        for s in self.shots:
            if s.asset.place:
                counts[s.asset.place] = counts.get(s.asset.place, 0) + 1
        return [p for p, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    @property
    def hours(self) -> float:
        """Hours between first and last photo, on a region seen in one day."""
        return hours_spanned(self.shots) if len(self.days) == 1 else 0.0

    @property
    def grade(self) -> Grade:
        return grade(self.shots, self.rules, small=self.small)

    @property
    def days_outside(self) -> list[dt.date]:
        """Photo days that fall inside none of the recorded visits.

        Reported per day rather than as a single verdict, because the common
        case is *partial*: a region visited twice with only one visit recorded
        looks fine on the overlap and has a whole trip missing behind it. A
        summary that says "the dates match" because one day of five matched
        would hide exactly the record a committee would ask about.

        Undated visits are excluded upstream — they say nothing about when, so
        they cannot disagree with anything. This only ever reports; changing a
        visit is a write, and writes go through a plan.
        """
        if not self.visit_windows or not self.shots:
            return []
        return [d for d in self.days if not any(a <= d <= b for a, b in self.visit_windows)]

    @property
    def dates_disagree(self) -> bool:
        """True when *no* photo day falls inside any recorded visit.

        The hard case: the region is claimed for dates the photos flatly
        contradict. A traveller who looks like they are guessing is worse off
        than one with thin proof, and the committee explicitly looks for records
        that "don't correspond to the reality on the ground".
        """
        return bool(self.shots) and bool(self.visit_windows) and len(self.days_outside) == len(
            self.days
        )


@dataclass
class Dossier:
    """The whole audit: one entry per region, claimed or merely photographed."""

    account: int
    library: str
    regions: list[RegionEvidence] = field(default_factory=list)
    rules: Rules = DEFAULT_RULES
    generated: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    params: dict = field(default_factory=dict)
    assets_seen: int = 0
    assets_unplaced: int = 0

    def by_grade(self, *grades: str) -> list[RegionEvidence]:
        return [r for r in self.regions if r.grade in grades]

    @property
    def claimed(self) -> list[RegionEvidence]:
        return [r for r in self.regions if r.claimed]

    @property
    def unclaimed_with_evidence(self) -> list[RegionEvidence]:
        """Photographed but not on the profile — the importer's to-do list."""
        return [r for r in self.regions if not r.claimed and r.shots]

    def counts(self) -> dict[str, int]:
        out = {g: 0 for g in GRADE_MEANING}
        for r in self.claimed:
            out[r.grade] += 1
        return out

    def projection(self, sample: int = 60, class1_needed: int = 45) -> dict:
        """Two numbers for a 60-region draw, and deliberately no verdict.

        The obvious calculation — sample size times the share of regions with
        serial photos — assumes two things that are not true. It assumes the
        draw is uniform, when NomadMania say theirs "always includes some of
        the most difficult countries on the planet"; and it assumes a serial
        set *passes*, which is a committee's decision about landmarks and
        faces, not a property of coordinates.

        So this reports a range instead:

        ``even``
            a uniform draw over everything claimed. The optimistic end.
        ``weighted``
            the same draw restricted to the least-photographed half of the
            profile — a crude stand-in for "they ask about the hard places".
            The pessimistic end, and the more useful one.

        Regions whose photos contradict their recorded dates are excluded from
        both: a set of photos that argues with the profile is not an answer.
        There is no boolean here on purpose. A tool that printed "ready" would
        be making a promise on the committee's behalf, and somebody would stop
        collecting evidence on the strength of it.
        """
        claimed = self.claimed
        if not claimed:
            return {"sample": sample, "needed": class1_needed, "even": 0, "weighted": 0,
                    "provable": 0, "claimed": 0, "excluded_date_conflict": 0}

        def usable(r: RegionEvidence) -> bool:
            return r.grade in ("strong", "serial") and not r.dates_disagree

        conflicted = sum(1 for r in claimed if r.dates_disagree and r.grade in ("strong", "serial"))
        provable = sum(1 for r in claimed if usable(r))
        # "Hard" is approximated by thinness of evidence: sort by how much this
        # library holds, and take the leaner half. It is a proxy — a real
        # difficulty ranking would need their sampling weights, which nobody has.
        ranked = sorted(claimed, key=lambda r: (len(r.shots), len(r.days)))
        lean = ranked[: max(1, len(ranked) // 2)]
        return {
            "sample": sample,
            "needed": class1_needed,
            "claimed": len(claimed),
            "provable": provable,
            "excluded_date_conflict": conflicted,
            "even": round(sample * provable / len(claimed)),
            "weighted": round(sample * sum(1 for r in lean if usable(r)) / len(lean)),
        }


# ------------------------------------------------------------------- grading


def spots(shots: list[Shot]) -> int:
    """Distinct places inside the region, at about a kilometre's resolution."""
    return len({(round(s.asset.lat, 2), round(s.asset.lon, 2)) for s in shots})


def diameter_km(shots: list[Shot], cap: int = 200) -> float:
    """The furthest apart two of these photos actually are.

    The obvious shortcut — the diagonal of the bounding box — measures a corner
    no photo was taken at, and for photos strung along a coast it can be nearly
    double the real distance. Overstating movement pushes a region over the
    serial threshold on evidence that does not support it, and this is a tool
    whose errors should point the other way.

    So: the true maximum pairwise distance, over the extremes plus an evenly
    spaced sample when there are thousands of photos. A sample can only ever
    understate the diameter, which is the safe direction.
    """
    if len(shots) < 2:
        return 0.0
    pts = [(s.asset.lat, s.asset.lon) for s in shots]
    if len(pts) > cap:
        extremes = [
            min(pts), max(pts),
            min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1]),
        ]
        step = len(pts) / (cap - len(extremes))
        pts = extremes + [pts[int(i * step)] for i in range(cap - len(extremes))]
    return max(
        haversine_km(a[0], a[1], b[0], b[1])
        for i, a in enumerate(pts)
        for b in pts[i + 1 :]
    )


def gradable(shots: list[Shot]) -> list[Shot]:
    """Only photos that fell *inside* a polygon may earn a grade.

    A nearest-polygon hit can be up to the resolver's tolerance outside every
    region — a third of a degree by default. Letting those count would grade a
    region on photos taken on the wrong side of its border, which is the exact
    accusation this whole exercise exists to avoid. They stay in the dossier as
    exhibits, flagged, for a human to judge.
    """
    return [s for s in shots if not s.near_boundary]


def hours_spanned(shots: list[Shot]) -> float:
    """Wall-clock hours between the first and last photo of a single day."""
    if len(shots) < 2:
        return 0.0
    times = [s.asset.taken for s in shots]
    return (max(times) - min(times)).total_seconds() / 3600.0


def small_region_spots(shots: list[Shot]) -> int:
    """Distinct spots at ~110 m, for regions too small to walk a kilometre."""
    return len(
        {
            (round(s.asset.lat, SMALL_SPOT_PRECISION), round(s.asset.lon, SMALL_SPOT_PRECISION))
            for s in shots
        }
    )


def is_serial(shots: list[Shot], rules: Rules = DEFAULT_RULES, *, small: bool = False) -> bool:
    """Does this add up to "serial photos within the region"?

    Enough photos, and then one of three routes:

    **Several days apart.** The plain case. Distance plus a second day is a
    stay, and a stay is hard to counterfeit.

    **One day, several spots, several hours.** A day trip is how most difficult
    regions actually get visited, so refusing to grade one would be wrong. But
    three frames twenty minutes apart on a motorway satisfy "spots" too, and
    that is a transit — the timestamps say so, and a verifier reads timestamps.
    Hence the hours: movement *and* time in the region.

    **Too small to cross.** Vatican City is 500 m wide; no amount of staying
    produces a kilometre. Two separate spots on two separate days qualifies
    instead — but only when ``small`` says the *region* is small. Two days in
    one Nairobi hotel looks exactly the same in the data and is not the same
    thing.
    """
    inside = gradable(shots)
    if len(inside) < rules.shots:
        return False
    days = {s.date for s in inside}
    if diameter_km(inside) < rules.spread_km:
        # No distance available. Only a genuinely tiny region earns the exemption.
        return (
            small
            and len(days) >= rules.days
            and small_region_spots(inside) >= rules.small_spots
        )
    if len(days) >= rules.days:
        return True
    return spots(inside) >= rules.spots and hours_spanned(inside) >= rules.single_day_hours


def grade(shots: list[Shot], rules: Rules = DEFAULT_RULES, *, small: bool = False) -> Grade:
    """Grade one region's photos against NomadMania's own vocabulary.

    ``strong`` means a serial set that also contains a front-camera photo. That
    is as far as metadata reaches: their Class 1 wording is "selfies with a
    prominent landmark", and whether a landmark is in the frame is a question
    for the person who was standing there.
    """
    if not shots:
        return "none"
    if not is_serial(shots, rules, small=small):
        return "thin"
    return "strong" if any(s.asset.selfie for s in gradable(shots)) else "serial"


def choose_exhibits(shots: list[Shot], limit: int = 5, library: Path | None = None) -> list[Shot]:
    """Pick the few photos worth attaching to an email.

    The selection criterion is *spread*, not beauty. Five photos of the same
    square prove one afternoon in one square; five photos a day and a dozen
    kilometres apart are the "serial photos within the region" their Class 1
    list asks for. So: anchor on the strongest single shot — a selfie if there
    is one — then repeatedly take whichever remaining photo is furthest from
    everything already chosen, preferring a day not yet represented.

    Ties break on uuid so two runs of this produce the same dossier.
    """
    if not shots or limit < 1:
        return []
    pool = sorted(shots, key=lambda s: (s.date, s.asset.uuid))
    anchor = next(
        (s for s in pool if s.asset.selfie),
        next((s for s in pool if s.asset.favorite), pool[0]),
    )
    picked = [anchor]
    seen = {anchor.asset.uuid}
    while len(picked) < limit and len(picked) < len(pool):
        best, best_score = None, None
        for s in pool:
            if s.asset.uuid in seen:
                continue
            gap = min(
                haversine_km(s.asset.lat, s.asset.lon, p.asset.lat, p.asset.lon) for p in picked
            )
            new_day = s.date not in {p.date for p in picked}
            on_disk = bool(library and s.asset.on_disk(library))
            score = (new_day, round(gap, 3), s.asset.selfie, on_disk, s.asset.favorite,
                     s.asset.uuid)
            if best_score is None or score > best_score:
                best, best_score = s, score
        if best is None:
            break
        picked.append(best)
        seen.add(best.asset.uuid)
    return sorted(picked, key=lambda s: (s.date, s.asset.uuid))


def build(
    shots_by_region: dict[int, list[Shot]],
    catalogue: dict[int, dict],
    claimed: set[int],
    *,
    account: int,
    library: Path,
    exhibits: int = 5,
    visits: dict[int, list[tuple[dt.date, dt.date]]] | None = None,
    documents: dict[int, list[str]] | None = None,
    rules: Rules = DEFAULT_RULES,
    small: set[int] | None = None,
    assets_seen: int = 0,
    assets_unplaced: int = 0,
    params: dict | None = None,
) -> Dossier:
    """Assemble the dossier from attributed shots and live profile state.

    Every claimed region gets an entry even when it has no photos — the empty
    entries are the finding. Regions with photos but no claim get one too,
    pointing the other way.
    """
    ids = sorted(set(claimed) | set(shots_by_region) | set(documents or {}))
    out: list[RegionEvidence] = []
    for rid in ids:
        shots = shots_by_region.get(rid, [])
        entry = RegionEvidence(
            region=rid,
            name=str(catalogue.get(rid, {}).get("name", f"region {rid}")),
            claimed=rid in claimed,
            shots=shots,
            visit_windows=(visits or {}).get(rid, []),
            rules=rules,
            small=rid in (small or set()),
            documents=sorted((documents or {}).get(rid, [])),
        )
        entry.exhibits = choose_exhibits(shots, exhibits, library)
        out.append(entry)
    return Dossier(
        account=account,
        library=str(library),
        regions=out,
        rules=rules,
        assets_seen=assets_seen,
        assets_unplaced=assets_unplaced,
        params=params or {},
    )


# ----------------------------------------------------------------- rendering


def collect_documents(root: str | Path) -> dict[int, list[str]]:
    """Index whatever you filed by hand under ``<root>/<region-id>-anything/``.

    The folder names are the same ones ``--copy`` writes for photos, so the
    two halves of a dossier live side by side: ``0292-iceland-south`` holds the
    exhibits, and the folder of the same name under the documents root holds
    the hotel bill, the entry stamp and the ferry ticket. Anything whose name
    does not start with a region id is skipped rather than guessed at.
    """
    root = Path(root)
    if not root.is_dir():
        return {}
    out: dict[int, list[str]] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        head = re.match(r"(\d+)", child.name)
        if not head:
            continue
        files = [
            str(f.relative_to(root))
            for f in sorted(child.rglob("*"))
            if f.is_file() and not f.name.startswith(".")
        ]
        if files:
            out[int(head.group(1))] = files
    return out


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit] or "region"


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _exhibit_line(s: Shot, library: Path) -> str:
    bits = [
        f"`{s.asset.original_name}`",
        s.date.isoformat(),
        f"{s.asset.lat:.4f},{s.asset.lon:.4f}",
    ]
    if s.asset.place:
        bits.append(s.asset.place)
    if s.asset.selfie:
        bits.append("**front camera**")
    if s.asset.video:
        bits.append("video")
    if s.near_boundary:
        bits.append(f"⚠ OUTSIDE every polygon, nearest by {s.distance_deg:.3f}°")
    bits.append("on disk" if s.asset.on_disk(library) else "in iCloud — needs export")
    return " · ".join(bits) + f"  \n  `{s.asset.uuid}`"


def render_markdown(dossier: Dossier) -> str:
    """The dossier a human reads, and searches when the sample arrives."""
    library = Path(dossier.library)
    counts = dossier.counts()
    p = dossier.projection()
    claimed = dossier.claimed
    lines: list[str] = []
    add = lines.append

    add("# Evidence dossier")
    add("")
    add(f"NomadMania account **{dossier.account}** · generated {dossier.generated}")
    add(f"Library `{dossier.library}`")
    add(
        f"{dossier.assets_seen:,} photos read, "
        f"{dossier.assets_unplaced:,} in no region (flights, open water, bad fixes)"
    )
    add("")
    add("## Where you stand")
    add("")
    add("| grade | claimed regions | meaning |")
    add("|---|---:|---|")
    for g in ("strong", "serial", "thin", "none"):
        add(f"| {g} | {counts[g]} | {GRADE_MEANING[g]} |")
    add("")
    add(
        f"Of **{len(claimed)}** claimed regions, "
        f"**{counts['strong'] + counts['serial']}** can put serial photos in front "
        f"of a verifier out of this library alone. Whether any given set *passes* "
        f"is their call, not this file's: `strong` means a front-camera photo is in "
        f"the set, not that a landmark is in the frame."
    )
    add("")
    add(
        "Attribution is done at about a hundred metres' resolution, so a photo "
        "taken within roughly that distance of a border may be counted on the "
        "wrong side of it. Photos that fell outside every polygon are marked and "
        "never counted toward a grade."
    )
    add("")
    add(f"### If they drew {p['sample']} regions tomorrow")
    add("")
    add("| draw | photo-backed answers |")
    add("|---|---:|")
    add(f"| even — every claimed region equally likely | **{p['even']}** of {p['sample']} |")
    add(f"| leaning hard — drawn from your thinnest half | **{p['weighted']}** of {p['sample']} |")
    add("")
    add(
        f"They ask for {p['needed']} Class 1. **Neither number is a verdict, and "
        "this file does not offer one.** A serial photo set is evidence you can "
        "hand over; whether it is accepted turns on landmarks and faces, which is "
        "the committee's call. The second row exists because their sample "
        "\"always includes some of the most difficult countries on the planet\" — "
        "and difficult places are where photo libraries are thinnest. Treat the "
        "gap between the rows as the size of your exposure."
    )
    if p["excluded_date_conflict"]:
        add("")
        add(
            f"{_n(p['excluded_date_conflict'], 'region')} with serial photos "
            "counted toward neither number: their photos contradict the dates on "
            "the profile, and a photo set that argues with your own record is not "
            "an answer. They are listed below."
        )
    add("")
    add(
        "Photo evidence is only half of it. Hotel bills in your name, ATM "
        "withdrawals with a location and date, and dated diary entries are all "
        "Class 1 and none of them are in a photo library. The regions listed as "
        "`none` and `thin` below are exactly where digging those out pays."
    )
    add("")

    total = [r for r in claimed if r.dates_disagree]
    partial = [r for r in claimed if r.days_outside and not r.dates_disagree]
    if total or partial:
        add("## Dates that disagree with the photos")
        add("")
        add(
            "**Total** means every photo day in that region falls outside every "
            "visit on your profile: the visit is dated wrong, or those photos are "
            "from a trip that was never recorded. That is the one a committee "
            "would ask about, so it is listed in full."
        )
        add("")
        if total:
            add("| region | visit dates on file | photo dates |")
            add("|---|---|---|")
            for r in sorted(total, key=lambda r: r.name):
                windows = ", ".join(f"{a}→{b}" for a, b in r.visit_windows[:3])
                d = r.days
                add(f"| {r.name} ({r.region}) | {windows} | {d[0]} → {d[-1]} |")
            add("")
        else:
            add("No region's photos contradict its recorded dates outright.")
            add("")
        if partial:
            add(
                f"A further **{len(partial)}** regions have *some* photo days "
                "outside their recorded visits — almost always a later return "
                "trip that was never entered rather than a wrong date. Those are "
                "material for the importer, not for a verification, and the full "
                "list with the exact days is in `evidence.json` under "
                "`days_outside_visits`. The ten with the most unrecorded days:"
            )
            add("")
            add("| region | photo days outside a visit |")
            add("|---|---:|")
            for r in sorted(partial, key=lambda r: (-len(r.days_outside), r.name))[:10]:
                add(f"| {r.name} ({r.region}) | {len(r.days_outside)} of {len(r.days)} |")
            add("")
        add(
            "Either way, changing a visit is a write. It goes through a plan, not "
            "through this file."
        )
        add("")

    nothing = [r for r in claimed if r.grade == "none"]
    if nothing:
        add("## Claimed, with nothing in this library")
        add("")
        add(
            f"{len(nothing)} regions. This is the list to attack first: find the "
            "boarding pass, the hotel bill, the other camera, the friend who was "
            "there — or, if the memory does not survive contact with the "
            "evidence, take the region off the profile yourself. Doing that "
            "voluntarily is a different conversation from having it done for you."
        )
        add("")
        for r in nothing:
            filed = f" — {_n(len(r.documents), 'document')} filed" if r.documents else ""
            add(f"- {r.name} ({r.region}){filed}")
        add("")

    thin = [r for r in claimed if r.grade == "thin"]
    if thin:
        add("## Claimed, thin")
        add("")
        add(
            "Photos exist, but not the kind that answers a verification on its "
            "own. Usually one of these is a photo stop on a drive, and the fix "
            "is a bill or a stamp rather than another photo."
        )
        add("")
        add("| region | photos | days | spots | spread km | hours | why it is thin |")
        add("|---|---:|---:|---:|---:|---:|---|")
        rules = dossier.rules
        for r in sorted(thin, key=lambda r: (len(r.shots), r.name)):
            why = []
            if len(r.shots) < rules.shots:
                why.append("too few photos")
            elif r.spread_km < rules.spread_km:
                why.append(
                    "one spot"
                    if r.small_spots < rules.small_spots
                    else "too small to cross, and only one day"
                )
            elif len(r.days) < rules.days:
                if r.spots < rules.spots:
                    why.append("one day, barely moved")
                else:
                    why.append(f"one day, only {r.hours:.1f}h — reads as transit")
            hours = f"{r.hours:.1f}" if len(r.days) == 1 else "—"
            add(
                f"| {r.name} ({r.region}) | {len(r.shots)} | {len(r.days)} | "
                f"{r.spots} | {r.spread_km:.1f} | {hours} | {', '.join(why)} |"
            )
        add("")

    unclaimed = dossier.unclaimed_with_evidence
    if unclaimed:
        add("## Photographed but not claimed")
        add("")
        add(
            "The audit runs both ways. These regions have photos in them and no "
            "visit on your profile. Do not click them off this table — resolve "
            "them through the importer, which checks the polygon against the "
            "live catalogue rather than against a dossier."
        )
        add("")
        add("| region | photos | days | first | last |")
        add("|---|---:|---:|---|---|")
        for r in sorted(unclaimed, key=lambda r: -len(r.shots)):
            d = r.days
            add(f"| {r.name} ({r.region}) | {len(r.shots)} | {len(d)} | {d[0]} | {d[-1]} |")
        add("")

    add("## Region by region")
    add("")
    add(
        "Search this section by name when a verification request arrives. Each "
        "exhibit list is chosen for spread — different days, different places "
        "inside the region — because that is what "
        "*serial photos within the region* means."
    )
    add("")
    for r in sorted(dossier.regions, key=lambda r: r.name.lower()):
        if not r.shots and not r.documents:
            continue
        d = r.days
        head = f"### {r.name} ({r.region}) — {r.grade}"
        if not r.claimed:
            head += " · NOT ON PROFILE"
        add(head)
        add("")
        if r.documents:
            add("Filed by hand — usually the stronger evidence of the two:")
            add("")
            for doc in r.documents:
                add(f"- `{doc}`")
            add("")
        if not r.shots:
            continue
        add(
            f"{_n(len(r.shots), 'photo')} · {_n(len(d), 'day')} · "
            f"{_n(r.spots, 'spot')} · {d[0]} → {d[-1]} · "
            f"{r.spread_km:.1f} km across · {_n(r.selfies, 'selfie')}"
            + (f" · {r.near_boundary} near a border" if r.near_boundary else "")
        )
        if r.places:
            add("")
            add(f"Places: {', '.join(r.places[:6])}")
        add("")
        for s in r.exhibits:
            add(f"- {_exhibit_line(s, library)}")
        add("")

    add("## The thresholds this was graded with")
    add("")
    add(
        "These are judgement calls about how *you* travel, not facts about "
        "evidence. If a region you know well came out `thin`, the numbers are "
        "probably wrong for you rather than the memory being wrong — change them "
        "and run it again."
    )
    add("")
    add("| setting | value | flag |")
    add("|---|---:|---|")
    for label, value, flag in (
        ("photos in the region", dossier.rules.shots, "--min-shots"),
        ("days, for the plain route", dossier.rules.days, "--min-days"),
        ("km between the furthest two", f"{dossier.rules.spread_km:g}", "--min-spread-km"),
        ("spots, for a single-day visit", dossier.rules.spots, "--min-spots"),
        ("hours spanned, single day", f"{dossier.rules.single_day_hours:g}", "--min-hours"),
        ("spots, region too small to cross", dossier.rules.small_spots, "--min-small-spots"),
    ):
        add(f"| {label} | {value} | `{flag}` |")
    add("")
    tiny = [r for r in dossier.regions if r.small]
    if tiny:
        add(
            "Measured against the polygons, "
            + ", ".join(f"{r.name} ({r.region})" for r in sorted(tiny, key=lambda r: r.name))
            + " are too small to cross and were graded on the two-spot rule instead."
        )
    else:
        add(
            "No region here was small enough to need the two-spot exemption. That "
            "rule exists for the Vaticans and Monacos; it is granted by measuring "
            "the region, never by noticing that the photos are clustered — "
            "two days in one hotel looks identical in the data."
        )
    add("")
    return "\n".join(lines) + "\n"


def render_json(dossier: Dossier) -> dict:
    """The same dossier for machines — stable enough to diff between runs."""
    library = Path(dossier.library)
    return {
        "tool": "wanderfill evidence",
        "generated": dossier.generated,
        "account": dossier.account,
        "library": dossier.library,
        "params": dossier.params,
        "rules": dossier.rules.as_dict(),
        "projection": dossier.projection(),
        "assets_seen": dossier.assets_seen,
        "assets_unplaced": dossier.assets_unplaced,
        "counts": dossier.counts(),
        "regions": [
            {
                "region": r.region,
                "name": r.name,
                "claimed": r.claimed,
                "grade": r.grade,
                "photos": len(r.shots),
                "days": len(r.days),
                "first": r.days[0].isoformat() if r.shots else None,
                "last": r.days[-1].isoformat() if r.shots else None,
                "spread_km": round(r.spread_km, 3),
                "spots": r.spots,
                "hours_single_day": round(r.hours, 2),
                "selfies": r.selfies,
                "near_boundary": r.near_boundary,
                "places": r.places[:6],
                "documents": r.documents,
                "dates_disagree": r.dates_disagree,
                "days_outside_visits": [d.isoformat() for d in r.days_outside],
                "visit_windows": [[a.isoformat(), b.isoformat()] for a, b in r.visit_windows],
                "exhibits": [
                    {
                        "uuid": s.asset.uuid,
                        "applescript_id": s.asset.applescript_id,
                        "name": s.asset.original_name,
                        "taken": s.asset.taken.isoformat(),
                        "lat": s.asset.lat,
                        "lon": s.asset.lon,
                        "place": s.asset.place,
                        "selfie": s.asset.selfie,
                        "video": s.asset.video,
                        "near_boundary": s.near_boundary,
                        "on_disk": s.asset.on_disk(library),
                        "path": str(s.asset.original_path(library)),
                    }
                    for s in r.exhibits
                ],
            }
            for r in sorted(dossier.regions, key=lambda r: r.name.lower())
        ],
    }


def render_applescript(dossier: Dossier, destination: Path) -> str:
    """A script that pulls the shortlisted originals out of Photos.

    Needed because a modern library is mostly not on the disk: with iCloud
    optimisation on, the file behind a thumbnail is on Apple's servers and no
    amount of reading the SQLite database will produce it. Photos itself will
    download it on export, which is why this is AppleScript and not a copy.

    The id format is ``<UUID>/L0/001``. A bare UUID raises -1728, which is an
    hour of confusion the first time you meet it.
    """
    library = Path(dossier.library)
    wanted: list[tuple[str, str]] = []
    for r in sorted(dossier.regions, key=lambda r: r.name.lower()):
        if not r.exhibits:
            continue
        folder = f"{r.region:04d}-{slug(r.name)}"
        for s in r.exhibits:
            if not s.asset.on_disk(library):
                wanted.append((folder, s.asset.applescript_id))
    lines = [
        "-- Exports the dossier's exhibits out of Photos, downloading from",
        "-- iCloud as needed. Generated by wanderfill evidence; safe to re-run.",
        "-- Photos must be running and will ask for permission the first time.",
        f'set destRoot to "{destination}"',
        'do shell script "mkdir -p " & quoted form of destRoot',
        "",
    ]
    by_folder: dict[str, list[str]] = {}
    for folder, aid in wanted:
        by_folder.setdefault(folder, []).append(aid)
    for folder, ids in by_folder.items():
        joined = ", ".join(f'"{i}"' for i in ids)
        lines += [
            f'set folderPath to destRoot & "/{folder}"',
            'do shell script "mkdir -p " & quoted form of folderPath',
            f"set theIds to {{{joined}}}",
            "tell application \"Photos\"",
            "  set theItems to {}",
            "  repeat with anId in theIds",
            "    try",
            "      set end of theItems to media item id anId",
            "    end try",
            "  end repeat",
            "  if theItems is not {} then",
            "    export theItems to (POSIX file folderPath) with using originals",
            "  end if",
            "end tell",
            "",
        ]
    if not wanted:
        lines.append("-- Nothing to do: every exhibit is already on this disk.")
    return "\n".join(lines) + "\n"
