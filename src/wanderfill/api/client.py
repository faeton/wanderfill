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
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ApiError, PrecisionLoss
from .transport import Transport

QUALITY = {
    0: "no visit",
    1: "transit",
    2: "minimal visit",
    3: "good visit",
    4: "worked here",
    5: "lived here",
    6: "travelguru",
}


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

    Three shapes come back and all three are real: fully dated, year-only, and
    nothing at all. Returning ``None`` for the year-only shape — which is what
    an ``all()`` check does — is worse than it looks: a read-modify-write would
    then see no date, and writing that back **erases a year the user recorded**.
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
        """Build a visit from one API row, in all three of its date shapes.

        **A visit can have no dates at all.** Every date component comes back
        ``null`` for a region that was simply clicked as visited, which is how
        most long-standing profiles are populated — one profile here had 118 of
        them. Parsing those with ``int()`` raises ``TypeError`` on the first
        one, which is invisible until something reads *every* region's visits
        rather than the handful it just wrote.

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


class NomadMania:
    """Thin, honest wrapper over both surfaces."""

    def __init__(self, token: str, *, lang: str = "en", transport: Transport | None = None):
        self.t = transport or Transport(token=token, lang=lang)

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
        data = self.t.webapi("regions/get-regions-list-2")
        return {int(k): v for k, v in data["data"].items()}

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

    def yes_scores(self, *, today: dt.date | None = None) -> dict[int, dict]:
        """YES per country, computed from live region years. Country id -> detail.

        NomadMania's published rule: 0 if the country was visited this calendar
        year, 0 if the previous one (an explicit "gift"), otherwise the years
        since; and **the traveller's age for a country never visited**. A country
        marked visited but carrying no year anywhere scores the age too — it is
        worth exactly as much as never having gone, which is the single largest
        and least visible drag on most profiles.

        Regions map to countries by flag: ``flag1`` first, then ``flag2``, because
        a territory carries its own flag first and its sovereign's second.

        This is the honest number and it is *not* the one on the ranking board —
        see :meth:`countries`. Say which you are quoting.
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
            score = age if not year else (0 if year >= today.year - 1 else today.year - year)
            out[cid] = {
                "country": r["country"],
                "last_year": year,
                "yes": score,
                "visited": bool(r.get("visited")),
                # the expensive case: claimed, but scoring as if it never happened
                "undated": bool(r.get("visited")) and not year,
            }
        return out

    # ------------------------------------------------------------- my state

    def visited_region_ids(self) -> set[int]:
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
        never-visited age under the YES rule, so these are worth surfacing.

        The payload nests one level deeper than the sibling endpoints
        (``data.regions``, alongside ``data.dare`` and ``data.tcc``).
        """
        out: dict[int, dict] = {}
        for mega in range(29):
            data = self.t.webapi("quickEnter/get-regions", megaregion=mega).get("data") or {}
            for r in data.get("regions") or []:
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
        return self.t.webapi(
            "quickEnter/add-visit",
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
        """
        if quality is None:
            raise ValueError("quality is required: update-visit replaces the whole record")

        current = next((v for v in self.visits_for_region(region) if v.id == visit_id), None)
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

        return self.t.webapi(
            "quickEnter/update-visit",
            id=visit_id,
            region=region,
            quality=quality,
            **_parts(date_from, "from"),
            **_parts(date_to, "to"),
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
        cell — but un-marking is a deletion in spirit and this library does not
        do deletions.

        A quadrant is a 10°×10° box, so membership is arithmetic on a coordinate
        rather than a polygon lookup, and there is no stale-id problem. What that
        does *not* settle is whether a coordinate inside the box represents a
        visit: a photo from a plane window sits in a cell as convincingly as a
        week on the ground. Grade candidates before calling this.
        """
        return self.t.webapi("kye/set-kye", qid=qid, visited=1)

    def mark_dare(self, dare_id: int) -> dict:
        """Mark a DARE area visited. Binary, no dates, no counts.

        Only ever sends 1. Un-marking is a deletion in spirit and this library
        does not do deletions.
        """
        return self.t.webapi("quickEnter/updateMQP", region=dare_id, visits=1)

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
        return self.t.webapi(
            "trips/new-trip",
            description=description,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            regions=",".join("[object Object]" for _ in regions),
            regions_json=json.dumps(list(regions)),
        )

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

    def tick_series_item(self, series_id: int, item_id: int) -> bool:
        """Tick one object. Answers with the literal string "OK".

        Never sends state=0. An object may belong to several series at once, so
        expect other series' scores to move as a side effect.
        """
        res = self.t.ajax_text(
            "my_series/", action="toggle", item=item_id, state=1, series=series_id
        )
        return res == "OK"

    # ------------------------------------------------------------- helpers

    def snapshot(self, regions: Iterable[int] | None = None) -> dict:
        """Everything needed to tell later whether a write did what was intended.

        Taken before any apply. If this fails, the apply must not start.
        """
        ids = self.visited_region_ids()
        targets = sorted(set(regions) if regions is not None else ids)
        return {
            "account": self.account_id(),
            "taken_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "visited_regions": sorted(ids),
            "visited_dare": sorted(self.visited_dare_ids()),
            "visits": {
                str(r): [v.__dict__ | {"date_from": _iso(v.date_from),
                                       "date_to": _iso(v.date_to)}
                         for v in self.visits_for_region(r)]
                for r in targets
            },
        }
