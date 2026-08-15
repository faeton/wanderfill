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

from .errors import ApiError
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
class Visit:
    """One visit record. Counts toward the region total whether or not it has a trip."""

    id: int
    region: int
    date_from: dt.date
    date_to: dt.date
    quality: int
    trip_id: int | None

    @classmethod
    def from_api(cls, raw: dict, region: int) -> Visit:
        return cls(
            id=int(raw["id"]),
            region=region,
            date_from=dt.date(int(raw["year_from"]), int(raw["month_from"]), int(raw["day_from"])),
            date_to=dt.date(int(raw["year_to"]), int(raw["month_to"]), int(raw["day_to"])),
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

        Country, UN, UN+, SLOW and YES totals are all *derived* from marked
        regions. There is nothing here to write — only to read back and check.
        """
        return self.t.webapi("slow/get-slow-app")["slow"]

    # ------------------------------------------------------------- my state

    def visited_region_ids(self) -> set[int]:
        return set(self.t.webapi("maps/get-visited-regions-ids-simple").get("ids", []))

    def visited_dare_ids(self) -> set[int]:
        """Trust this over ``get-regions-mqp``, whose ``visited`` field is not a boolean."""
        return set(self.t.webapi("maps/get-visited-dare-ids-simple").get("ids", []))

    def visited_country_ids(self) -> set[int]:
        return set(self.t.webapi("maps/get-visited-countries-ids-simple").get("ids", []))

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
        self, region: int, date_from: dt.date, date_to: dt.date, quality: int = 3
    ) -> dict:
        """Create a visit.

        Side effect you cannot switch off: the server wraps the new visit in an
        auto-created single-region Trip with an empty description. Record what
        appears so the debris stays reconcilable — see
        :meth:`trips_for_year`.
        """
        return self.t.webapi(
            "quickEnter/add-visit",
            region=region,
            quality=quality,
            year_from=date_from.year, month_from=date_from.month, day_from=date_from.day,
            year_to=date_to.year, month_to=date_to.month, day_to=date_to.day,
        )

    def update_visit(
        self, visit_id: int, region: int, date_from: dt.date, date_to: dt.date, *, quality: int
    ) -> dict:
        """Replace a visit record in full.

        ``quality`` is keyword-only and required because this call is a
        replacement, not a patch: omit it and a region marked *lived here*
        quietly becomes *good visit*. Read the current value first and pass the
        greater of it and what you intend.
        """
        if quality is None:
            raise ValueError("quality is required: update-visit replaces the whole record")
        return self.t.webapi(
            "quickEnter/update-visit",
            id=visit_id,
            region=region,
            quality=quality,
            year_from=date_from.year, month_from=date_from.month, day_from=date_from.day,
            year_to=date_to.year, month_to=date_to.month, day_to=date_to.day,
        )

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
                str(r): [v.__dict__ | {"date_from": v.date_from.isoformat(),
                                       "date_to": v.date_to.isoformat()}
                         for v in self.visits_for_region(r)]
                for r in targets
            },
        }
