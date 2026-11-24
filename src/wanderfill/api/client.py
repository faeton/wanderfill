"""A client for NomadMania's private API.

Design rule for this module: every quirk of the real server is encoded as
*behaviour*, not as a comment telling you to be careful. If a caller can get it
wrong by writing the obvious thing, the method is wrong, not the caller.

Three quirks are handled here rather than left to the user:

  * ``update_visit`` refuses to run without a quality, because the server
    replaces the whole record and a missing quality silently downgrades it.
  * ``create_trip`` sends the junk ``regions`` field the server insists on
    seeing before it will read ``regions_json``.
  * ``visits_for_region`` returns every visit, standalone and trip-owned alike,
    because both count and filtering to one kind is how duplicates get made.
  * the five undo methods — ``delete_visit``, ``delete_trip``, ``clear_region``,
    ``unmark_dare``, ``unmark_kye`` — each take one id, read the thing before
    and after, refuse to act on a record they cannot see, and return what was
    removed so it can be journalled and put back.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, TypeVar

from .cache import ReadCache, summary_of
from .errors import ApiError, PrecisionLoss, UnknownWriteOutcome, VerificationFailed
from .transport import Transport

T = TypeVar("T")

# Bulk reads run this many at a time. On a slow link the cost is round-trip
# latency, not bandwidth, so four in flight is roughly four times faster.
# Writes never go through this path: see ``_many``.
READ_WORKERS = 4

# What the server scores a country that is marked visited but carries no year.
# The published rule only covers "never visited" (your age); this value was
# measured on one profile across a batch run. It is a constant here so that the
# next person who learns better changes one line rather than hunting prose.
UNDATED_YES = 8

def yes_delta(current: int, year: int, *, today: dt.date | None = None) -> int:
    """Points YES would move if a country currently scoring ``current`` were dated to ``year``.

    Negative is a gain. **Compute this; do not apply a rule of thumb.** "Dating
    an old visit makes YES worse" is true for a 2013 visit and false for a 2025
    one, and the break-even moves every January. With an undated country at 8,
    today, a visit in the current or previous calendar year is worth −8, and
    every year further back is worth one point more until it turns positive.

    It is also worth nothing at all if some other region of that country already
    carries a later year — YES reads the country's most recent visit, not this
    one. Check ``yes_scores()[cid]["last_year"]`` before proposing a date.
    """
    today = today or dt.date.today()
    would = 0 if year >= today.year - 1 else today.year - year
    return would - current


QUALITY = {
    0: "no visit",
    1: "transit",
    2: "minimal visit",
    3: "good visit",
    4: "worked here",
    5: "lived here",
    6: "travelguru",
}

MARKS_REGION_FROM = 2
"""The lowest quality that makes a region count as visited.

A region whose only visit is a transit is absent from
``maps/get-visited-regions-ids-simple`` and from the region total, although the
visit itself is stored and returned. Measured in October 2026: two ``add-visit``
writes, at quality 2 and quality 1, moved the region count by one.
:func:`wanderfill.plan.apply.verify` reports such a visit as ``not_counted``
rather than letting a plan summary promise a region the profile will not show.
"""


@dataclass(frozen=True)
class YearOnly:
    """A visit somebody remembers the year of, but not the day.

    NomadMania stores this natively: ``year_from`` is set while ``month_from``
    and ``day_from`` are null. It is not an edge case — an audit of one profile
    found 13 such records among 892 — and it is the honest representation of
    "Myanmar, some time in 2013". YES reads only the year anyway, so a remembered
    year costs nothing in score against a manufactured day.

    It exists as a type rather than a bare ``int`` so that a caller cannot pass a
    year where a :class:`datetime.date` was meant, and so that
    :meth:`NomadMania.update_visit` can tell "vague on purpose" apart from
    "precise, and about to be destroyed by accident".
    """

    year: int

    def __post_init__(self) -> None:
        if not 1900 <= self.year <= 2100:
            raise ValueError(f"implausible year: {self.year}")

    def isoformat(self) -> str:
        return str(self.year)


When = dt.date | YearOnly


def _iso(day: When | None) -> str | None:
    """Dates are optional on a visit, so every serialiser has to cope."""
    return day.isoformat() if day else None


def _parts(day: When | None, side: str) -> dict[str, Any]:
    """One endpoint of a visit, as the wire fields the API expects.

    A :class:`YearOnly` sends an empty month and day. Empty, not omitted: this
    endpoint replaces the whole record, so a key that is not sent is not
    "unchanged", and the difference decides whether a vague date is stored or a
    precise one silently survives underneath.
    """
    if day is None:
        return {f"year_{side}": "", f"month_{side}": "", f"day_{side}": ""}
    if isinstance(day, YearOnly):
        return {f"year_{side}": day.year, f"month_{side}": "", f"day_{side}": ""}
    return {f"year_{side}": day.year, f"month_{side}": day.month, f"day_{side}": day.day}


def _date_parts(raw: dict, side: str) -> When | None:
    """``year_from``/``month_from``/``day_from`` -> a date, a year, or None.

    Four shapes come back and all four are real: fully dated, year-only, nothing
    at all, and a year of 0 with no month or day, which is nothing at all too
    (``YearOnly`` refuses the 0). Returning ``None`` for the year-only shape —
    which is what an ``all()`` check does — is worse than it looks: a
    read-modify-write would then see no date, and writing that back **erases a
    year the user recorded**.
    """
    parts = [raw.get(f"{unit}_{side}") for unit in ("year", "month", "day")]
    if parts[0] is None or parts[0] == "":
        return None
    try:
        if any(p is None or p == "" for p in parts[1:]):
            return YearOnly(int(parts[0]))
        return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (TypeError, ValueError):
        return None


def _iso_day(value: object) -> dt.date | None:
    """``YYYY-MM-DD`` as a date; anything else, other ISO spellings included, is None."""
    try:
        day = dt.date.fromisoformat(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return day if day.isoformat() == value else None


def _precision(day: When | None) -> int:
    """0 nothing, 1 year only, 2 a full date. Only ever compared, never stored."""
    if day is None:
        return 0
    return 1 if isinstance(day, YearOnly) else 2


@dataclass(frozen=True)
class Visit:
    """One visit record. Counts toward the region total whether or not it has a trip."""

    id: int
    region: int
    date_from: When | None
    date_to: When | None
    quality: int
    trip_id: int | None

    @classmethod
    def from_api(cls, raw: dict, region: int) -> Visit:
        """Build a visit from one API row, in all four of its date shapes.

        **A visit can have no dates at all.** Every date component comes back
        ``null`` for a region that was simply clicked as visited, which is how
        most long-standing profiles are populated — one profile here had 118 of
        them. Parsing those with ``int()`` raises ``TypeError`` on the first
        one, which is invisible until something reads *every* region's visits
        rather than the handful it just wrote.

        **Or a year of 0**, with a null month and day: two of 946 rows on one
        profile. That is no date either, and it goes back out as one: empty
        fields, never the 0.

        **And a visit can carry a year with no month or day**, which arrives as
        :class:`YearOnly`. Treating that as undated is the quieter and more
        expensive mistake of the two: nothing raises, and the next
        read-modify-write posts the record back with its year blanked.

        An undated visit still counts toward the region total. It is only the
        dates that are absent, and absent is not the same as wrong.
        """
        return cls(
            id=int(raw["id"]),
            region=region,
            date_from=_date_parts(raw, "from"),
            date_to=_date_parts(raw, "to"),
            quality=int(raw.get("quality", 3)),
            trip_id=int(raw["trip_id"]) if raw.get("trip_id") else None,
        )

    @property
    def signature(self) -> tuple:
        """What makes two visits 'the same visit' for reconciliation.

        Deliberately excludes quality and trip_id: both are mutable, and a
        change to either should read as an update, not as a different visit.
        """
        return (self.region, self.date_from, self.date_to)


def _whole(value: object) -> int | None:
    """An int that is not a bool, or the digits of one; ``None`` for anything else."""
    if type(value) is int:
        return value
    if isinstance(value, str) and len(value) <= 10 and value.isascii() and value.isdigit():
        return int(value)
    return None


VISIT_DATES = ("year_from", "month_from", "day_from", "year_to", "month_to", "day_to")


def _row_problem(raw: object) -> str | None:
    """Why one visit row cannot be read as proof, or ``None``.

    :meth:`Visit.from_api` fills what is missing: no quality is 3, no dates is
    an undated visit. That is for display. A row read that way before a write
    is how a "lived here" goes back as a "good visit" without its dates.
    """
    if not isinstance(raw, dict):
        return "it is not an object"
    if absent := [f for f in ("id", "quality", "trip_id", *VISIT_DATES) if f not in raw]:
        return f"it has no {', '.join(absent)}"
    for f in ("id", "quality"):
        if _whole(raw[f]) is None:
            return f"its {f} is {str(raw[f])[:30]!r}"
    for f in ("trip_id", *VISIT_DATES):
        if raw[f] is not None and _whole(raw[f]) is None:
            return f"its {f} is {str(raw[f])[:30]!r}"
    for side in ("from", "to"):
        parts = [raw[f"{unit}_{side}"] for unit in ("year", "month", "day")]
        # An end with nothing in it but nulls and zeroes is no date, as it is for
        # `_date_parts`. A year of 0 is how the server has some undated visits; a
        # 0 for the month or day was never seen, and beside a 0 or null year it
        # drops nothing either. Beside anything real it would: `_date_parts` reads
        # 2024-0-0 and 0-5-6 as no date, so those are refused, not made vaguer.
        if any(_whole(p) for p in parts) and _date_parts(raw, side) is None:
            return f"its {side} date is {parts}"
    return None


def strict_ids(action: str, values: object) -> set[int]:
    """The ids of a list read as proof: whole numbers, or the digits of them.

    Anything else raises instead of being dropped, because an id that is skipped
    reads as "not ticked".
    """
    if not isinstance(values, list):
        raise VerificationFailed(action, f"{str(values)[:60]!r} is not a list of ids")
    out = set()
    for v in values:
        if (n := _whole(v)) is None:
            raise VerificationFailed(action, f"the list holds {str(v)[:60]!r}, which is not an id")
        out.add(n)
    return out


class NomadMania:
    """Thin, honest wrapper over both surfaces."""

    def __init__(
        self,
        token: str,
        *,
        lang: str = "en",
        transport: Transport | None = None,
        cache: ReadCache | None = None,
    ):
        self.t = transport or Transport(token=token, lang=lang)
        # Only the read-only commands pass one. See api/cache for why.
        self.cache = cache
        # ``progress(label, done, total)`` for bulk reads; the CLI throttles it.
        self.progress: Callable[[str, int, int], None] | None = None
        # Which ids are TCC destinations, not which are ticked. See mark_tcc.
        self._tcc_ids: set[int] | None = None

    def _many(self, label: str, fn: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        """``fn`` over ``items`` on a small thread pool, in order, reporting progress.

        Reads only. A write must never come through here: the write path is
        sequential by design, so that a lost answer is one unknown outcome and
        not four.
        """
        total = len(items)
        out: list[Any] = [None] * total
        if not total:
            return out
        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
            for done, fut in enumerate(as_completed(futures), 1):
                out[futures[fut]] = fut.result()
                if self.progress:
                    self.progress(label, done, total)
        return out

    # ------------------------------------------------------------------ who

    def status(self) -> dict:
        return self.t.webapi("user/status")

    def status_quick(self) -> dict:
        """Like ``status`` but it actually includes the uid.

        ``user/status`` returns only ``{result, status, admin}`` — no account
        id at all — so this is the endpoint to use for identity.
        """
        return self.t.webapi("user/status-quick")

    def settings(self) -> dict:
        """Account settings, including the user's declared home regions."""
        return self.t.webapi("user/get-settings")

    def profile(self, profile_id: int) -> dict:
        """A profile page's data, as ``nomadmania.com/profile/<id>`` draws it.

        ``scores`` here are **the numbers on the profile**: ``score_un``,
        ``score_unp`` (UN+), ``score_nm``, ``score_mqp`` (DARE), ``score_kye``,
        ``score_tcc``, ``score_yes``, ``score_slow*``, ``rank_nm`` and
        ``rank_country``. Quote these rather than recomputing them.

        UN+ in particular cannot be recomputed from ``countries()``: that list
        is the 196 SLOW/YES countries, while UN+ is NomadMania's 265 "UN
        countries and territories". Counting visited rows of the 196 gave 104
        on a profile whose page said 121, and that number went on a card.
        """
        return self.t.webapi("profile/get-profile-data", profile_id=int(profile_id))["data"]

    def account_id(self) -> int:
        """The uid a plan is bound to. Applying a plan under a different uid is refused."""
        s = self.status_quick()
        for key in ("uid", "user_id", "id"):
            if key in s:
                return int(s[key])
        raise ApiError("user/status-quick", "no account id in response", s)

    def home_regions(self) -> list[int]:
        """The user's declared home region ids, if they set any.

        A hint, not a fact. Profile settings go stale — somebody who set a home
        years ago and has been nomadic since still has it sitting in their
        account, and feeding that to :func:`wanderfill.plan.segment.segment`
        would quietly delete their most-visited region from their own trips.
        Confirm with the person before using it.
        """
        s = self.settings()
        return [int(s[k]) for k in ("homebase", "homebase2") if s.get(k)]

    # -------------------------------------------------------------- catalog

    def regions(self) -> dict[int, dict]:
        """The live catalogue. Any id absent from here is stale — see geo.resolve."""
        data = self.cache.catalogue() if self.cache else None
        if data is None:
            data = self.t.webapi("regions/get-regions-list-2")["data"]
            if self.cache:
                self.cache.put_catalogue(data)
        return {int(k): v for k, v in data.items()}

    def megaregions(self) -> Any:
        return self.t.webapi("regions/get-megaregions")

    def countries(self) -> list[dict]:
        """196 countries with visited flag, region counts, SLOW tiers and per-country YES.

        Country, UN, UN+ and SLOW are *derived* from marked regions. There is
        nothing here to write — only to read back and check.

        The ``yes`` field is renamed to ``yes_stored`` on the way out, because it
        is **batch-computed and lags your writes**. It is not wrong — it was
        observed converging to 189/196 agreement with the published rule after a
        batch run — but between a write and that run it reports the old world.

        Read once and it will fool you. On this profile it read a flat ``8`` for
        every visited country an hour before it read the correct 0–15 spread, and
        an afternoon went into concluding the field was inert. ``8`` turned out to
        mean **"visited, year unknown"** — the state every country is in as far as
        the aggregate is concerned until the batch catches up.

        So: quote ``yes_stored`` as the score on the board, use
        :meth:`region_years` for what is true right now, and :meth:`yes_scores`
        only to cross-check. Never conclude from a single reading.
        """
        rows = self.t.webapi("slow/get-slow-app")["slow"]
        for r in rows:
            if "yes" in r:
                r["yes_stored"] = r.pop("yes")
        return rows

    def yes_scores(
        self, *, today: dt.date | None = None, undated: int = UNDATED_YES
    ) -> dict[int, dict]:
        """YES per country, computed from live region years. Country id -> detail.

        NomadMania's published rule: 0 if the country was visited this calendar
        year, 0 if the previous one (an explicit "gift"), otherwise the years
        since; and the traveller's age for a country **never visited**.

        The published rule is silent on the case that matters most in practice —
        a country marked visited whose year is unknown. Observation on one
        profile, before and after a batch run, is that the server scores those
        **8**, not the age, and that is what ``undated`` defaults to. Treat it as
        measured on one account rather than as a documented rule, and pass a
        different value if a second profile ever says otherwise.

        That 8 is why this method exists in its current form. Scoring undated
        countries as the age instead — which this did until it was checked
        against a batch run — makes a recomputation disagree with the server by
        33 points per country, and the disagreement looks like a server bug.

        Regions map to countries by flag: ``flag1`` first, then ``flag2``, because
        a territory carries its own flag first and its sovereign's second. That
        mapping is the weak part: it misses territories filed under a different
        flag from their sovereign (Greenland under Denmark, Åland under Finland),
        so where this disagrees with ``yes_stored`` the server is usually right.

        This is a cross-check, **not** the number on the ranking board — see
        :meth:`countries`. Say which you are quoting.
        """
        today = today or dt.date.today()
        born = self.settings().get("date_of_birth")
        if not born:
            raise ApiError("user/get-settings", "no date_of_birth; YES needs an age")
        birth = dt.date.fromisoformat(born)
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))

        rows = self.countries()
        by_flag = {r["flag"]: r for r in rows}
        last: dict[int, int] = {}
        for reg in self.region_years().values():
            c = by_flag.get(reg.get("flag1")) or by_flag.get(reg.get("flag2"))
            year = reg.get("last_visited_in_year")
            if c and year:
                cid = c["country_id"]
                last[cid] = max(last.get(cid, 0), int(year))

        out = {}
        for r in rows:
            cid = r["country_id"]
            year = last.get(cid)
            seen = bool(r.get("visited"))
            if year:
                score = 0 if year >= today.year - 1 else today.year - year
            else:
                score = undated if seen else age
            out[cid] = {
                "country": r["country"],
                "last_year": year,
                "yes": score,
                "visited": seen,
                "undated": seen and not year,
            }
        return out

    # ------------------------------------------------------------- my state

    def visited_region_ids(self, *, strict: bool = False) -> set[int]:
        """Region ids that count as visited.

        ``strict=True`` is for snapshots and read-backs: a response with no
        ``ids`` list raises instead of reading as an empty profile, which would
        otherwise "prove" that nothing is marked.
        """
        if strict:
            return self._strict_ids("maps/get-visited-regions-ids-simple", "ids")
        return set(self.t.webapi("maps/get-visited-regions-ids-simple").get("ids", []))

    def visited_dare_ids(self) -> set[int]:
        """Trust this over ``get-regions-mqp``, whose ``visited`` field is not a boolean."""
        return set(self.t.webapi("maps/get-visited-dare-ids-simple").get("ids", []))

    def visited_country_ids(self) -> set[int]:
        return set(self.t.webapi("maps/get-visited-countries-ids-simple").get("ids", []))

    def region_years(self) -> dict[int, dict]:
        """Every region in the catalogue with the years it was first and last visited.

        29 calls, one per megaregion, and the only *live* per-region summary the
        API offers: ``first_visited_in_year``, ``last_visited_in_year``,
        ``best_visit_quality``, ``no_of_visits``. Use this to recompute YES
        rather than believing ``countries()['yes']``, which is a stale aggregate.

        Both year fields are ``None`` for a region whose visits carry no dates —
        the legacy "clicked visited" records. That is not the same as unvisited:
        ``no_of_visits`` is still set. A country in that state scores the full
        ``UNDATED_YES`` under the YES rule, so these are worth surfacing — but
        dating one only helps if the real year is recent. See :meth:`yes_scores`.

        The payload nests one level deeper than the sibling endpoints
        (``data.regions``, alongside ``data.dare`` and ``data.tcc``).
        """
        def one(mega: int) -> list[dict]:
            data = self.t.webapi("quickEnter/get-regions", megaregion=mega).get("data") or {}
            return data.get("regions") or []

        out: dict[int, dict] = {}
        for rows in self._many("region summaries", one, list(range(29))):
            for r in rows:
                out[int(r["id"])] = r
        return out

    def visits_for_region(self, region: int) -> list[Visit]:
        """EVERY visit for the region — standalone and trip-owned.

        Filtering these by ``trip_id is None`` is the single most expensive
        mistake available against this API. It makes regions whose visit lives
        inside an auto-created trip look empty, and a second visit gets added.
        """
        data = self.t.webapi("quickEnter/get-visits-to-region", region=region)
        return [Visit.from_api(v, region) for v in data.get("data", [])]

    def visits_for_regions(
        self,
        regions: Iterable[int],
        *,
        summaries: dict[int, dict] | None = None,
        account: int | None = None,
    ) -> dict[int, list[Visit]]:
        """Every visit for many regions: from the cache where it still vouches, else live.

        A cached region is reused only while its row in ``summaries`` (a
        ``region_years()`` result) matches the row it was saved under. That
        catches an added or removed visit and a change of year or quality. It
        does **not** catch a date moved inside the same year, which is why the
        cache also expires and why ``history`` prints its age into the report.
        Without ``summaries``, ``account`` or a cache, everything is read live.
        """
        regions = sorted(set(regions))
        sigs = {r: summary_of((summaries or {}).get(r)) for r in regions}
        cached: dict[int, list[dict]] = {}
        if self.cache and summaries is not None and account is not None:
            cached = self.cache.visits(account, sigs)
        todo = [r for r in regions if r not in cached]

        def one(region: int) -> list[dict]:
            # Strict: a reply without its list is an error, never "no visits".
            # Read as empty, it would be cached and vouched for by the summary.
            return self._strict("quickEnter/get-visits-to-region", "data", region=region)

        fresh = dict(zip(todo, self._many("visit records", one, todo), strict=True))
        if self.cache and summaries is not None and account is not None and fresh:
            # Never cache an answer the summary contradicts: it says visits
            # exist and the list came back empty. Use it this once; ask again.
            keep = {
                r: rows for r, rows in fresh.items()
                if rows or not ((summaries.get(r) or {}).get("no_of_visits"))
            }
            self.cache.put_visits(account, keep, sigs)
        if self.cache and cached:
            self.cache.notes.append(
                f"visit records: {len(cached)} of {len(regions)} regions from cache, "
                f"{len(fresh)} read live"
            )
        raw = {**cached, **fresh}
        return {r: [Visit.from_api(v, r) for v in raw[r]] for r in regions}

    # ----------------------------------------------------------- geocoding

    def region_at(self, lat: float, lon: float) -> dict:
        """Reverse geocode a coordinate.

        ``share=0`` is hardcoded and not a parameter. Without it this endpoint
        publishes the coordinate as the user's live location, so geocoding an
        archive would broadcast a fictional travel diary. There is no legitimate
        reason for this library to offer the other behaviour.

        Note the returned id may be STALE: roughly one in seven ids this
        endpoint produces no longer exists in ``regions()``. Validate, and
        repair the failures with :mod:`wanderfill.geo.resolve`.
        """
        return self.t.webapi("location/get-region", lat=lat, lng=lon, share=0)

    # -------------------------------------------------------------- writing

    def _write(self, action: str, *, answers: str | None = None, **fields: Any) -> dict:
        """Send one write of a plan kind and insist on the answer it is known to give.

        All 502 such writes on record were answered ``result: OK``, with the new
        id where ``answers`` names one. An object that says anything else is not
        the server saying yes, and not a refusal either (that is ``ERROR``, an
        :class:`ApiError`), so it is an :class:`UnknownWriteOutcome`. What the
        undo methods and ``trips/update-trip`` are answered was not recorded,
        and their read-back settles them, so they do not come through here.
        """
        reply = self.t.webapi(action, **fields)
        ok = isinstance(reply, dict) and reply.get("result") == "OK"
        if not ok or (answers and (_whole(reply.get(answers)) or 0) <= 0):
            raise UnknownWriteOutcome(
                f"{action} sent, the answer was {str(reply)[:200]}, not 'result': 'OK'"
                + (f" with a {answers}" if answers else "")
                + ". Read the affected record back before running anything again."
            )
        return reply

    def add_visit(
        self, region: int, date_from: When, date_to: When, quality: int = 3
    ) -> dict:
        """Create a visit.

        Either endpoint may be a :class:`datetime.date` or a :class:`YearOnly`.

        Side effect you cannot switch off: the server wraps the new visit in an
        auto-created single-region Trip with an empty description. Record what
        appears so the debris stays reconcilable — see
        :meth:`trips_for_year`.
        """
        return self._write(
            "quickEnter/add-visit",
            answers="visit_id",
            region=region,
            quality=quality,
            **_parts(date_from, "from"),
            **_parts(date_to, "to"),
        )

    def update_visit(
        self,
        visit_id: int,
        region: int,
        date_from: When | None,
        date_to: When | None,
        *,
        quality: int,
        allow_vaguer: bool = False,
    ) -> dict:
        """Replace a visit record in full.

        ``quality`` is keyword-only and required because this call is a
        replacement, not a patch: omit it and a region marked *lived here*
        quietly becomes *good visit*. Read the current value first and pass the
        greater of it and what you intend.

        Dates may be :class:`datetime.date` or :class:`YearOnly`. Because this
        is a replacement, writing a ``YearOnly`` over a record that already held
        a full date **destroys the month and day**, and writing ``None`` over
        either destroys the lot. So this reads the record back first and refuses
        to reduce a date's precision unless ``allow_vaguer=True`` says that is
        the intent. It costs one extra GET per update, which is the cheapest
        insurance in this module: the same class of mistake — a write that looks
        like a patch and behaves like a replacement — is what downgraded a
        "lived here" the first time this was done by hand.

        Widening precision is always allowed: a bare year becoming a real date
        is the whole point of filling a profile in.

        An end passed as ``None`` goes as three empty fields. On a visit stored
        with a year of 0 that is no loss, 0 being no date, and the 0 is not sent.
        """
        if quality is None:
            raise ValueError("quality is required: update-visit replaces the whole record")

        try:
            live = self._visits_strict(region)
        except VerificationFailed as exc:  # before the write, so nothing is in doubt
            raise PrecisionLoss(
                f"visit {visit_id} cannot be read ({exc}) — refusing to write blind"
            ) from exc
        current = next((v for v in live if v.id == visit_id), None)
        if current is None:
            raise PrecisionLoss(
                f"visit {visit_id} is not on region {region} — refusing to write blind"
            )

        # The first incident this package exists to prevent: a replacement that
        # carried a lower quality than the record already held. Documenting
        # "pass the greater of the two" left the arithmetic with the caller, and
        # a caller that gets it wrong is exactly what happened. Do it here.
        quality = max(int(quality), current.quality or 0)

        if not allow_vaguer:
            for side, was, now in (
                ("date_from", current.date_from, date_from),
                ("date_to", current.date_to, date_to),
            ):
                if _precision(now) < _precision(was):
                    raise PrecisionLoss(
                        f"visit {visit_id}: {side} would go from {_iso(was)!r} to {_iso(now)!r}, "
                        "losing precision this call cannot restore. "
                        "Pass allow_vaguer=True if that is deliberate."
                    )

        return self._write(
            "quickEnter/update-visit",
            id=visit_id,
            region=region,
            quality=quality,
            **_parts(date_from, "from"),
            **_parts(date_to, "to"),
        )

    # ---------------------------------------------------------------- undo
    #
    # The reads below are *strict*: a response missing the list they need
    # is an error, not an empty list. The ordinary readers default a missing
    # field to empty because that is harmless for display; here an empty
    # answer is taken as proof of absence, and ``{"result": "OK"}`` with the
    # field dropped would otherwise verify a deletion that never happened, or
    # let a plan made against an "empty" profile write all of it again.

    def _read(self, action: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except ValueError as exc:  # a captive portal's page, not JSON
            raise VerificationFailed(action, f"the answer is not JSON ({exc})") from exc

    def _list_in(self, action: str, data: Any, field: str) -> list:
        value = data.get(field) if isinstance(data, dict) else None
        if not isinstance(value, list):
            raise VerificationFailed(
                action, f"the answer has no {field!r} list to check against: {str(data)[:200]}",
                data,
            )
        return value

    def _strict(self, action: str, field: str, **params: Any) -> list:
        data = self._read(action, lambda: self.t.webapi(action, **params))
        return self._list_in(action, data, field)

    def _strict_ids(self, action: str, field: str) -> set[int]:
        return strict_ids(action, self._strict(action, field))

    def _readback(self, action: str, fn: Callable[[], T]) -> T:
        """Run a read that follows the write ``action``; any failure is unproven.

        A plain :class:`ApiError` from here would look like the write endpoint
        refusing, and a journal would close its entry on a write that was
        answered ``OK``. So every failure comes out as
        :class:`VerificationFailed`: an ``ERROR`` reply, a dead connection, a
        lost session, and equally an answer that is not JSON or lacks a field.
        """
        try:
            return fn()
        except VerificationFailed:
            raise
        except Exception as exc:
            raise VerificationFailed(
                action,
                f"sent and answered, but the read-back failed ({exc!r}) — outcome unknown",
                getattr(exc, "payload", None),
            ) from exc

    def _visits_strict(self, region: int) -> list[Visit]:
        action = "quickEnter/get-visits-to-region"
        rows = self._strict(action, "data", region=region)
        for row in rows:
            if why := _row_problem(row):
                raise VerificationFailed(
                    action, f"region {region} has a visit that cannot be read: {why}"
                )
        return [Visit.from_api(v, region) for v in rows]

    def _dare_strict(self) -> set[int]:
        return self._strict_ids("maps/get-visited-dare-ids-simple", "ids")

    def kye_ticked(self) -> set[int]:
        """The ticked KYE quadrants; a reply without the list raises, see ``_strict``."""
        return self._strict_ids("kye/get-kye", "visited")

    def delete_visit(self, visit_id: int, region: int, *, allow_trip_owned: bool = False) -> Visit:
        """Delete one visit record by id.

        v1 had no delete path at all — not behind a flag, absent from the class,
        with a test asserting so — because cleaning up records the tool did not
        create is a conversation with a human, not a feature. The undo methods
        were added when the owner of a profile asked for a specific, mis-entered
        visit to go, named its id, and wanted the endpoint exercised. That is
        still the only shape they are meant for: **one record, named by the
        account owner, with a reason**. A loop over any of them is a policy
        violation, whatever the loop's argument is. None of them is a plan op.

        The endpoint is ``quickEnter/delete-visit`` with a single ``id`` field,
        found in the inline script of ``/regions/``. Three guards, each learnt
        from a write that looked fine at the time:

        * it reads the region first and refuses if the id is not on it, so a
          typo cannot delete somebody else's record on another region;
        * a trip-owned visit is refused unless ``allow_trip_owned`` says so —
          the site itself warns that the visit leaves the trip too;
        * ``OK`` is not evidence. It reads the region back afterwards and raises
          if the record is still there.

        Returns the record as it was, so the caller can print it, journal it,
        and re-create it with :meth:`add_visit` if the deletion was a mistake —
        with one limit: ``add_visit`` cannot put a visit back *into its trip*.
        It makes a standalone record (inside a fresh auto-created trip), so a
        trip-owned visit that was deleted by mistake comes back as a different
        kind of record, and restoring the original trip membership is a
        separate, separately approved edit of the trip.

        The transport gives writes one attempt: a lost answer raises
        :class:`UnknownWriteOutcome`, and the caller reads back rather than
        re-sends. A read-back that still shows the record, or that cannot be
        read, raises :class:`VerificationFailed`, which is an ``ApiError`` a
        journal can tell apart from a refusal.
        """
        before = self._visits_strict(region)
        current = next((v for v in before if v.id == int(visit_id)), None)
        if current is None:
            raise PrecisionLoss(
                f"visit {visit_id} is not on region {region} — refusing to delete blind"
            )
        if current.trip_id is not None and not allow_trip_owned:
            raise PrecisionLoss(
                f"visit {visit_id} belongs to trip {current.trip_id}; deleting it also "
                "removes it from the trip. Pass allow_trip_owned=True if that is deliberate."
            )
        resp = self.t.webapi("quickEnter/delete-visit", id=int(visit_id))
        after = self._readback("quickEnter/delete-visit", lambda: self._visits_strict(region))
        if any(v.id == int(visit_id) for v in after):
            raise VerificationFailed(
                "quickEnter/delete-visit",
                f"server answered {resp!r} but visit {visit_id} is still on region {region}",
                resp,
            )
        return current

    def clear_region(
        self,
        region: int,
        *,
        allow_trip_owned: bool = False,
        expect_ids: Iterable[int] | None = None,
    ) -> list[Visit]:
        """Mark a region "not visited": ``quickEnter/set-not-visited {region}``.

        ``expect_ids`` binds the call to what a human was shown: pass the visit
        ids from the preview, and the call refuses if the region now holds a
        different set. Without it the confirmation covers "whatever is there
        at send time", which is not what anybody confirmed.

        This is the red "Mark as not visited" button on ``/regions/``. It takes
        the region, not a visit, and the server removes **every** visit record
        on it — which makes it the widest of the undo calls and the one to
        prefer :meth:`delete_visit` over whenever a single record is the
        problem. Refused when the region has no visits (nothing to undo, and a
        typo in the id should not be silently fine), and when any visit is
        trip-owned unless ``allow_trip_owned`` — the site's own delete warns
        that such a visit leaves its trip too, and this call does not warn.

        Returns the visits as they were, standalone and trip-owned alike, so
        each can be re-created with :meth:`add_visit`. Reads back and raises if
        anything is still there.
        """
        before = self._visits_strict(region)
        if not before:
            raise PrecisionLoss(f"region {region} has no visits — nothing to clear")
        if expect_ids is not None and {v.id for v in before} != {int(i) for i in expect_ids}:
            raise PrecisionLoss(
                f"region {region} now holds visits {sorted(v.id for v in before)}, not the "
                f"{sorted(int(i) for i in expect_ids)} that were previewed — re-run to re-preview"
            )
        owned = [v for v in before if v.trip_id is not None]
        if owned and not allow_trip_owned:
            raise PrecisionLoss(
                f"region {region}: {len(owned)} of {len(before)} visit(s) belong to trips "
                f"({', '.join(str(v.trip_id) for v in owned)}); clearing removes them from "
                "those trips. Pass allow_trip_owned=True if that is deliberate."
            )
        resp = self.t.webapi("quickEnter/set-not-visited", region=region)
        after = self._readback("quickEnter/set-not-visited", lambda: self._visits_strict(region))
        if after:
            raise VerificationFailed(
                "quickEnter/set-not-visited",
                f"server answered {resp!r} but region {region} still has {len(after)} visit(s)",
                resp,
            )
        return before

    def delete_trip(self, trip_id: int, *, expect_regions: Iterable[int] | None = None) -> dict:
        """Delete a trip **and every visit it owns**: ``trips/delete-trip``.

        Reads the trip first, which doubles as the ownership check — the server
        answers ``Unauthorised.`` for a trip that is not yours or does not exist,
        and :meth:`trip` raises on that, so nothing is sent. ``expect_regions``
        binds the call to the preview: the region ids the trip carries must
        match what the human was shown, or nothing is sent.

        Returns the trip as it was, regions and their dates included, which is
        enough to re-create it with :meth:`create_trip`.

        **Absence is proved twice, and only the expected way.** After the write,
        ``get-trip`` must fail with exactly the ``Unauthorised.`` the server
        gives for a missing trip — any other error (a database hiccup, a token
        that just expired) is *not* proof and raises
        :class:`VerificationFailed`. And because ``Unauthorised.`` is also what
        a lost session would say, the year's trip listing is read as well and
        must no longer contain the id. That second read is a real, authenticated
        answer, so the two together cannot both be explained by lost access.
        """
        before = self.trip(trip_id)
        trip = before.get("trip", before)
        if expect_regions is not None:
            have = sorted(int(r.get("region", r.get("id"))) for r in trip.get("regions", []))
            if have != sorted(int(r) for r in expect_regions):
                raise PrecisionLoss(
                    f"trip {trip_id} now carries regions {have}, not the "
                    f"{sorted(int(r) for r in expect_regions)} previewed — re-run to re-preview"
                )
        year = int(str(trip.get("date_from", ""))[:4] or 0)
        resp = self.t.webapi("trips/delete-trip", trip_id=int(trip_id))

        def reads_back() -> tuple[bool, Any]:
            try:
                return True, self.trip(trip_id)
            except ApiError as exc:
                if exc.description.strip() != "Unauthorised.":
                    raise VerificationFailed(
                        "trips/delete-trip",
                        f"server answered {resp!r}; get-trip then failed with "
                        f"{exc.description!r}, which is not the missing-trip answer — "
                        "outcome unknown",
                        resp,
                    ) from exc
                return False, None

        there, still = self._readback("trips/delete-trip", reads_back)
        if there:
            raise VerificationFailed(
                "trips/delete-trip",
                f"server answered {resp!r} but trip {trip_id} still reads back: {still!r}"[:400],
                resp,
            )
        if year:
            listed = self._readback("trips/delete-trip", lambda: {
                int(t.get("id", -1))
                for t in self._strict("trips/get-trips-for-year-app", "trips", year=year)
            })
            if int(trip_id) in listed:
                raise VerificationFailed(
                    "trips/delete-trip",
                    f"get-trip says trip {trip_id} is gone but the {year} listing still has it",
                    resp,
                )
        return trip

    def unmark_dare(self, dare_id: int) -> None:
        """Un-mark one DARE area: ``quickEnter/updateMQP {region, visits: 0}``.

        That is what the "Mark as not visited" button on ``/dare/`` sends.
        Refused if the area is not currently marked, so a wrong id is an error
        rather than a no-op that looks like success. Reads the visited list
        back and raises if the id is still in it.
        """
        if int(dare_id) not in self._dare_strict():
            raise PrecisionLoss(f"DARE area {dare_id} is not marked — nothing to un-mark")
        resp = self.t.webapi("quickEnter/updateMQP", region=int(dare_id), visits=0)
        if int(dare_id) in self._readback("quickEnter/updateMQP", self._dare_strict):
            raise VerificationFailed(
                "quickEnter/updateMQP",
                f"server answered {resp!r} but DARE area {dare_id} is still marked",
                resp,
            )

    def unmark_kye(self, qid: int) -> None:
        """Un-tick one Know Your Earth quadrant: ``kye/set-kye {qid, visited: 0}``.

        Refused if the quadrant is not ticked. Read the count before and after
        — KYE has been seen to *gain* quadrants on its own hours after a write,
        so a changed total is not necessarily this call's doing. Reads the
        ticked list back and raises if the id is still in it.
        """
        if int(qid) not in self.kye_ticked():
            raise PrecisionLoss(f"KYE quadrant {qid} is not ticked — nothing to un-tick")
        resp = self.t.webapi("kye/set-kye", qid=int(qid), visited=0)
        if int(qid) in self._readback("kye/set-kye", self.kye_ticked):
            raise VerificationFailed(
                "kye/set-kye", f"server answered {resp!r} but quadrant {qid} is still ticked", resp
            )

    def kye(self) -> dict:
        """The Know Your Earth grid and which quadrants are ticked.

        Returns ``{visited: [qid…], max: 434, regions: [{qid, name}…]}``. KYE is
        **not** derived from your regions, trips or countries — the page says
        "mark quadrants as visited by clicking the map", and nothing fills it in
        for you. A profile with hundreds of regions can and does sit at zero.
        """
        return self.t.webapi("kye/get-kye")

    def mark_kye(self, qid: int) -> dict:
        """Tick one KYE quadrant. Only ever sends 1, like :meth:`mark_dare`.

        The endpoint takes ``visited: 0`` too — that is how the map un-ticks a
        cell — but un-marking is a deletion in spirit and lives in
        :meth:`unmark_kye`, one quadrant at a time, never here.

        A quadrant is a 10°×10° box, so membership is arithmetic on a coordinate
        rather than a polygon lookup, and there is no stale-id problem. What that
        does *not* settle is whether a coordinate inside the box represents a
        visit: a photo from a plane window sits in a cell as convincingly as a
        week on the ground. Grade candidates before calling this.
        """
        return self._write("kye/set-kye", qid=qid, visited=1)

    def mark_dare(self, dare_id: int) -> dict:
        """Mark a DARE area visited. Binary, no dates, no counts.

        Only ever sends 1. Un-marking is a deletion in spirit and lives in
        :meth:`unmark_dare`, one area at a time, never here.
        """
        return self._write("quickEnter/updateMQP", region=dare_id, visits=1)

    # ------------------------------------------------------------------ TCC

    def tcc(self) -> dict[int, dict]:
        """The TCC destinations that **no NomadMania region covers**, by id.

        TCC is the Travelers' Century Club's list of 330. NomadMania derives
        almost all of it from your regions, and there is no endpoint that lists
        those derived 314 — ``profile`` only gives ``score_tcc``. What *is*
        listed are the 16 that no region covers (the seven Antarctic claims,
        Akrotiri and Dhekelia, Lampedusa, Midway, Wake…): ``quickEnter/
        get-regions`` carries them beside the regions and DARE, as
        ``data.tcc`` rows ``{id, name, visited: 0|1}``, scattered across the 29
        megaregions. These are ticked by hand, and only these.

        The ids are their own namespace: TCC 64 is Akrotiri and Dhekelia, region
        64 is something else. 29 reads.

        Strict, because the answer is used as proof: a response without the list,
        or a row without its flag, raises :class:`VerificationFailed` instead of
        reading as "not ticked". An empty list is a real answer: most megaregions
        have none.
        """
        def one(mega: int) -> list[dict]:
            action = "quickEnter/get-regions"
            reply = self._read(action, lambda: self.t.webapi(action, megaregion=mega))
            data = reply.get("data") if isinstance(reply, dict) else None
            rows = data.get("tcc") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise VerificationFailed(
                    action, f"megaregion {mega} answered with no 'tcc' list", reply
                )
            if any(not isinstance(r, dict) or r.get("visited") not in (0, 1) for r in rows):
                raise VerificationFailed(
                    action, f"megaregion {mega} has a TCC row with no 0/1 'visited'", reply
                )
            strict_ids(action, [r.get("id") for r in rows])
            return rows

        out: dict[int, dict] = {}
        for rows in self._many("TCC lists", one, list(range(29))):
            for r in rows:
                out[int(r["id"])] = r
        return out

    def visited_tcc_ids(self) -> set[int]:
        return {i for i, r in self.tcc().items() if r["visited"] == 1}

    def mark_tcc(self, tcc_id: int) -> dict:
        """Tick one hand-entered TCC destination: ``quickEnter/updateTCC {region, visits: 1}``.

        That is the "visited" button in the TCC block on ``/regions/``
        (``markTCC`` in the page script); the field is called ``region`` but it
        carries the TCC id. Only ever sends 1 — the page un-ticks with
        ``visits: -1``, not 0, and that would be a deletion, which this method
        does not do.

        Refuses an id that is not in :meth:`tcc`, because the field name invites
        passing a region id, and region ids and TCC ids overlap. The list of ids
        is read once per client: it is a catalogue, 29 requests, and it does not
        change between two ticks. Whether one is *ticked* is never remembered.
        """
        if self._tcc_ids is None:
            self._tcc_ids = set(self.tcc())
        if int(tcc_id) not in self._tcc_ids:
            raise PrecisionLoss(
                f"{tcc_id} is not a hand-entered TCC destination; the {len(self._tcc_ids)} "
                "that exist are listed by tcc(). A region id here would tick something else."
            )
        return self._write("quickEnter/updateTCC", region=int(tcc_id), visits=1)

    def create_trip(
        self, date_from: dt.date, date_to: dt.date, regions: Sequence[dict], description: str = ""
    ) -> dict:
        """Create a trip carrying many region-visits.

        The server checks that a ``regions`` key exists before it will look at
        ``regions_json``, so both must be sent. The website itself builds a JS
        object, pushes it through URLSearchParams, and accidentally transmits
        ``"[object Object],[object Object]"`` — which is what the server has
        come to require. We reproduce the accident deliberately.
        """
        return self._write(
            "trips/new-trip",
            answers="trip_id",
            description=description,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            regions=",".join("[object Object]" for _ in regions),
            regions_json=json.dumps(list(regions)),
        )

    def sync_trip_header(self, trip_id: int) -> dict:
        """Widen a trip's own dates to span its regions, by **replacing the trip**.

        The server deletes the trip and its visits and creates new ones, so the
        trip id and every visit id in it change. Other visits stay as they are.

        ``update-visit`` moves a trip-owned visit but leaves the trip's header
        (``date_from``/``date_to``) where it was, so a trip extended visit by
        visit reads "31 Oct – 2 Nov" around a region row saying "31 Oct –
        12 Nov". This re-saves the trip the way the site's Save button does
        (``saveTrip()`` on ``/my-trip/<id>/``, 2026-11-22): ``trip_id``,
        ``description``, the header as min/max of the region dates, the junk
        ``regions`` field ``new-trip`` also needs, and ``regions_json``.

        The region rows are rebuilt from the server's own read, not from any
        plan, and each keeps its quality — the site's page would send 3 or 1
        and so downgrade a "lived here"; this does not. Region rows carry no
        visit id, and the server does not edit in place: the first live save
        (trip 501536, 2026-11-22) **deleted the trip and its visit and created
        a new trip and a new visit** — same dates and quality, new ids
        (547375 / 14213716), the old id answering ``Trip not found.``. So the
        read-back follows the replacement. Before the write it records which
        trips exist and every visit in every region; afterwards the
        replacement must be a trip that did not exist before, with the same
        header, rows (quality, hidden, dates) and description, one visit per
        region with the old visit's dates and quality, and every other visit
        in those regions exactly as it was. A failed or unanswered write is
        never resent; the exception carries ``replacement_candidates`` from a
        read. A read-back that cannot be read is a :class:`VerificationFailed`,
        like one that disagrees. The returned ``trip_id`` is the one to record;
        any id noted for the old trip or its visits is dead.

        What the write replaces cannot be put back under its old ids, and the
        read-back runs only after it. So these are refused before anything is
        sent: a header that would be shortened at either end (two trips longer
        than their regions were found on 2026-11-23; whether the header or the
        regions are wrong is the owner's to say); a region row and its visit
        that disagree on quality or on either date, since the body is built
        from the row and would overwrite the visit; a header or row date that
        is not ``YYYY-MM-DD``, or a row whose year, month and day are another
        date; a year-only region (the site sends ``year`` instead, a different
        shape); and a header that is already right. One attempt, never retried.
        """
        def rows(trip: dict) -> list[tuple]:
            return sorted((int(r["id"]), int(r["quality"]), int(r.get("hidden") or 0),
                           r.get("date_from"), r.get("date_to")) for r in trip["regions"])

        def inventory(region: int) -> dict[int, tuple]:
            return {v.id: (v.date_from, v.date_to, int(v.quality), v.trip_id)
                    for v in self._visits_strict(region)}

        def fail(msg: str) -> VerificationFailed:
            return VerificationFailed("trips/update-trip", f"server answered {resp!r}; {msg}",
                                      resp)

        before = self.trip(trip_id)["trip"]
        regions = before.get("regions") or []
        if not regions:
            raise ApiError("trips/update-trip", f"trip {trip_id} has no regions to span", before)
        if any(not (r.get("date_from") and r.get("date_to")) for r in regions):
            raise ApiError("trips/update-trip", f"trip {trip_id} has a year-only region; "
                           "the site saves those as `year`, which this does not send", before)
        header = [before.get(k) for k in ("date_from", "date_to")]
        if None in [_iso_day(d) for d in header]:
            raise ApiError("trips/update-trip", f"trip {trip_id} has a header that is not two "
                           f"YYYY-MM-DD dates: {header}", before)
        for r in regions:
            for side in ("from", "to"):
                iso = r[f"date_{side}"]
                parts = [r.get(f"{unit}_{side}") for unit in ("year", "month", "day")]
                day = _iso_day(iso)
                if day is None or day != _date_parts(r, side):
                    # the header goes out from the one, the region from the other
                    raise ApiError("trips/update-trip", f"trip {trip_id}, region {r['id']}: "
                                   f"date_{side} {iso!r} is not the YYYY-MM-DD of its year, "
                                   f"month and day {parts}", before)
        date_from = min(r["date_from"] for r in regions)
        date_to = max(r["date_to"] for r in regions)
        if header == [date_from, date_to]:
            raise ApiError("trips/update-trip",
                           f"trip {trip_id} already spans {date_from}..{date_to}", before)
        if date_from > header[0] or date_to < header[1]:
            raise ApiError("trips/update-trip", f"trip {trip_id} reads {header[0]}..{header[1]} "
                           f"around regions spanning {date_from}..{date_to}; the save would "
                           "shorten it", before)
        body = [{
            "id": int(r["id"]), "quality": int(r["quality"]), "hidden": int(r.get("hidden") or 0),
            "year_from": int(r["year_from"]), "month_from": int(r["month_from"]),
            "day_from": int(r["day_from"]), "year_to": int(r["year_to"]),
            "month_to": int(r["month_to"]), "day_to": int(r["day_to"]),
        } for r in regions]
        description = before.get("description") or ""
        # Every year of the new span, which contains the old header: nothing says
        # which of its years a trip is listed under, or that it is only one.
        years = range(int(date_from[:4]), int(date_to[:4]) + 1)

        def listing() -> list[dict]:
            return [t for y in years
                    for t in self._strict("trips/get-trips-for-year-app", "trips", year=y)]

        def region_ids(t: dict) -> set[int]:
            return {int(r.get("id", r.get("region"))) for r in t.get("regions") or []}
        # Everything the read-back compares against is read BEFORE the write:
        # which trips already exist (so an old look-alike cannot pose as the
        # replacement), and every visit in every region (so a duplicate, a
        # surviving old visit or a lost unrelated one cannot pass).
        listed_before = {int(t["id"]) for t in listing()}
        inv_before = {r["id"]: inventory(r["id"]) for r in body}
        mine_before = {rid: [k for k, v in inv.items() if v[3] == trip_id]
                       for rid, inv in inv_before.items()}
        if any(len(ids) != 1 for ids in mine_before.values()):
            raise ApiError("trips/update-trip", f"trip {trip_id} does not own exactly one visit "
                           f"per region before the save: {mine_before}", before)
        for r in body:
            (vid,) = mine_before[r["id"]]
            if inv_before[r["id"]][vid][2] != r["quality"]:
                # the body carries the trip row's quality; a visit that says
                # otherwise would be rewritten to it. Not this method's call.
                raise ApiError("trips/update-trip", f"region {r['id']}: trip row says q"
                               f"{r['quality']}, visit {vid} says q{inv_before[r['id']][vid][2]}",
                               before)
            row_days = tuple(_date_parts(r, side) for side in ("from", "to"))
            visit_days = inv_before[r["id"]][vid][:2]
            if visit_days != row_days:  # likewise; a YearOnly or no date is not a date
                row_says, visit_says = ("..".join(str(_iso(d)) for d in days)
                                        for days in (row_days, visit_days))
                raise ApiError("trips/update-trip", f"trip {trip_id}, region {r['id']}: trip row "
                               f"says {row_says}, visit {vid} says {visit_says}", before)
        ids = {r["id"] for r in body}

        def readback(fn: Callable[[], T]) -> T:
            return self._readback("trips/update-trip", fn)

        def candidates() -> list[int]:
            return readback(lambda: sorted({
                int(t["id"]) for t in listing()
                if int(t["id"]) not in listed_before
                and (t.get("date_from"), t.get("date_to")) == (date_from, date_to)
                and region_ids(t) == ids}))

        def read_trip(tid: int) -> tuple[dict, list[tuple]]:
            trip = self.trip(tid)["trip"]
            return trip, rows(trip)

        def saved_trip() -> tuple[dict, list[tuple]] | None:
            """The trip under its old id, or None for the two answers that mean gone."""
            try:
                return read_trip(trip_id)
            except ApiError as exc:
                if (exc.description or "").strip() not in ("Trip not found.", "Unauthorised."):
                    raise
                return None

        try:
            resp = self.t.webapi(
                "trips/update-trip",
                trip_id=trip_id,
                description=description,
                date_from=date_from,
                date_to=date_to,
                regions=",".join("[object Object]" for _ in body),
                regions_json=json.dumps(body),
            )
        except Exception as exc:
            # Refused, or no answer at all. Never resend: read what is there,
            # and hand the caller any replacement so it is not orphaned.
            try:
                exc.replacement_candidates = candidates()
            except Exception as read_exc:  # the write's error wins
                exc.replacement_candidates = f"unreadable: {read_exc}"
            raise

        new_id = trip_id
        saved = readback(saved_trip)
        if saved is None:
            if trip_id in readback(lambda: {int(t["id"]) for t in listing()}):
                raise fail(f"get-trip says {trip_id} is gone but the listing still has it")
            found = candidates()
            if len(found) != 1:
                raise fail(f"trip {trip_id} is gone and {len(found)} NEW trip(s) {found} match "
                           f"{date_from}..{date_to} {sorted(ids)}")
            new_id = found[0]
            saved = readback(lambda: read_trip(new_id))
        after, rows_after = saved
        if (after.get("date_from"), after.get("date_to")) != (date_from, date_to):
            raise fail(f"trip {new_id} reads {after.get('date_from')}..{after.get('date_to')}")
        if rows_after != rows(before):
            raise fail(f"trip {new_id} regions changed: {rows(before)} -> {rows_after}")
        if (after.get("description") or "") != description:
            raise fail(f"trip {new_id} description changed: {description!r} -> "
                       f"{after.get('description')!r}")
        visits = {}
        for rid in (r["id"] for r in body):
            was, now = inv_before[rid], readback(lambda rid=rid: inventory(rid))
            (old_visit,) = mine_before[rid]
            others_was = {k: v for k, v in was.items() if k != old_visit}
            others_now = {k: v for k, v in now.items() if v[3] != new_id}
            if new_id == trip_id:
                others_now = {k: v for k, v in now.items() if k != old_visit}
            if others_now != others_was:
                raise fail(f"region {rid}: visits outside the trip changed: {others_was} -> "
                           f"{others_now}")
            owned = [k for k, v in now.items() if v[3] == new_id]
            if len(owned) != 1:
                raise fail(f"region {rid} has {len(owned)} visit(s) in trip {new_id}")
            if now[owned[0]][:3] != was[old_visit][:3]:
                raise fail(f"region {rid}: visit {owned[0]} reads {now[owned[0]][:3]}, was "
                           f"{was[old_visit][:3]}")
            visits[rid] = owned[0]
        return {"trip_id": new_id, "old_trip_id": trip_id, "visits": visits,
                "was": (before.get("date_from"), before.get("date_to")),
                "now": (date_from, date_to), "response": resp}

    def trips_for_year(self, year: int) -> Any:
        return self.t.webapi("trips/get-trips-for-year-app", year=year)

    def trip(self, trip_id: int) -> Any:
        return self.t.webapi("trips/get-trip", trip_id=trip_id)

    # ---------------------------------------------------------- the series

    def series_list(self) -> Any:
        return self.t.webapi("series/get-list")

    def series(self, series_id: int) -> dict:
        """Read one series: catalogue, score, and the ids already ticked.

        Series live on the legacy surface, not on /webapi/ — the page at
        /series_single/<id>/ is a shell around an iframe, which is why the
        endpoints are invisible to anyone grepping the outer document.
        """
        return self.t.ajax_json(
            "V2/", action="getData", type="seriesSingle", id=series_id, lang=self.t.lang
        )

    def series_ticked(self, series_id: int) -> set[int]:
        """The ticked ids of one series; a reply without the list raises, see ``_strict``."""
        action = f"V2/ series {series_id}"
        data = self._read(action, lambda: self.series(series_id))
        return strict_ids(action, self._list_in(action, data, "visited"))

    def tick_series_item(self, series_id: int, item_id: int) -> bool:
        """Tick one object. Answers with the literal string "OK".

        Never sends state=0. The endpoint is a toggle, and a repeat may undo the
        tick: ``apply`` reads :meth:`series_ticked` first and does not call this
        for an id in it. A tick is not expected to reach another series: measured
        2026-11-24, no object id was in two of the 69 (75,449 ids in ``items2``).
        """
        res = self.t.ajax_text(
            "my_series/", action="toggle", item=item_id, state=1, series=series_id
        )
        return res == "OK"

    # ------------------------------------------------------------- helpers

    def snapshot(self, regions: Iterable[int] | None = None, *, tcc: bool = False) -> dict:
        """Everything needed to tell later whether a write did what was intended.

        Taken before any apply. If this fails, the apply must not start. The lists
        are read strictly: one that is missing raises :class:`VerificationFailed`,
        because read as an empty profile by planning and applying alike it makes
        drift pass. ``regions=None`` reads every visited region's visits, an empty
        ``regions`` none.

        ``tcc=True`` adds the ticked TCC ids, 29 more reads, for a plan that
        writes them. It is opt-in so that plans without TCC keep their fingerprint.
        """
        ids = self.visited_region_ids(strict=True)
        targets = sorted(set(regions) if regions is not None else ids)
        snap = {
            "account": self.account_id(),
            "taken_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "visited_regions": sorted(ids),
            "visited_dare": sorted(self._dare_strict()),
            "visits": {
                str(r): [v.__dict__ | {"date_from": _iso(v.date_from),
                                       "date_to": _iso(v.date_to)}
                         for v in self._visits_strict(r)]
                for r in targets
            },
        }
        if tcc:
            snap["visited_tcc"] = sorted(self.visited_tcc_ids())
        return snap
