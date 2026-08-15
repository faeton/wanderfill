"""Tests for the quirks that cost real damage the first time round.

These are not tests of the server. They are tests that this package cannot make
the same mistakes again, and each one names the incident it prevents.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from wanderfill.api.client import NomadMania, Visit
from wanderfill.api.errors import ApiError
from wanderfill.plan.model import Op, Plan
from wanderfill.plan.segment import Journey, segment, split_first_and_repeat
from wanderfill.sources.base import DayPoint, Track
from wanderfill.sources.normalize import components, fold


class FakeTransport:
    """Records what would have been sent, answers with whatever is queued."""

    def __init__(self, replies=None):
        self.sent = []
        self.replies = replies or {}
        self.token = "fake"
        self.lang = "en"

    def webapi(self, action, **fields):
        self.sent.append((action, fields))
        reply = self.replies.get(action, {"result": "OK"})
        if isinstance(reply, dict) and reply.get("result") == "ERROR":
            raise ApiError(action, reply.get("result_description", ""), reply)
        return reply

    def ajax_json(self, path, **fields):
        self.sent.append((path, fields))
        return self.replies.get(path, {})

    def ajax_text(self, path, **fields):
        self.sent.append((path, fields))
        return self.replies.get(path, "OK")


def client(replies=None):
    t = FakeTransport(replies)
    return NomadMania(token="fake", transport=t), t


# --------------------------------------------------------------------------
# trip creation: the server demands a field it never reads
# --------------------------------------------------------------------------

def test_create_trip_sends_both_region_fields():
    """Sending only regions_json fails with 'Missing params: regions.'"""
    c, t = client()
    regions = [{"id": 1, "quality": 3}, {"id": 2, "quality": 3}]
    c.create_trip(dt.date(2024, 1, 1), dt.date(2024, 1, 5), regions)
    _, fields = t.sent[0]
    assert "regions" in fields, "the server checks this key exists before reading regions_json"
    assert "regions_json" in fields
    assert fields["regions"] == "[object Object],[object Object]"
    assert json.loads(fields["regions_json"]) == regions


# --------------------------------------------------------------------------
# update-visit: a full replacement, not a patch
# --------------------------------------------------------------------------

def test_update_visit_requires_quality():
    """Omitting quality once turned a 'lived here' into a 'good visit'."""
    c, _ = client()
    with pytest.raises(TypeError):
        c.update_visit(1, 2, dt.date(2024, 1, 1), dt.date(2024, 1, 2))  # no quality


def test_update_visit_passes_quality_through():
    c, t = client()
    c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=5)
    _, fields = t.sent[0]
    assert fields["quality"] == 5


# --------------------------------------------------------------------------
# geocoding must never broadcast a live location
# --------------------------------------------------------------------------

def test_region_at_always_sends_share_zero():
    """Without share=0 this endpoint publishes the coordinate as a live location."""
    c, t = client({"location/get-region": {"result": "OK", "nm": {"id": 29}}})
    c.region_at(41.89, 12.49)
    _, fields = t.sent[0]
    assert fields["share"] == 0


# --------------------------------------------------------------------------
# dare is never unset
# --------------------------------------------------------------------------

def test_mark_dare_never_sends_zero():
    c, t = client()
    c.mark_dare(1142)
    _, fields = t.sent[0]
    assert fields["visits"] == 1


def test_client_has_no_delete_methods():
    """v1 has no delete path at all — not a flag, absent from the class."""
    names = [n for n in dir(NomadMania) if not n.startswith("_")]
    assert not [n for n in names if "delete" in n or "remove" in n or "unmark" in n]


# --------------------------------------------------------------------------
# visit identity
# --------------------------------------------------------------------------

def test_visit_signature_ignores_quality_and_trip():
    """Two records for the same region and dates are the same visit.

    quality and trip_id are both mutable, so a change to either is an update,
    not a different visit. Treating them as identity is how duplicates appear.
    """
    a = Visit(1, 100, dt.date(2024, 1, 1), dt.date(2024, 1, 3), 3, None)
    b = Visit(2, 100, dt.date(2024, 1, 1), dt.date(2024, 1, 3), 5, 987)
    assert a.signature == b.signature


def test_visits_for_region_returns_trip_owned_visits_too():
    """Filtering by trip_id is None created 50 duplicate visits once."""
    raw = {
        "data": [
            {"id": 1, "year_from": 2024, "month_from": 1, "day_from": 1,
             "year_to": 2024, "month_to": 1, "day_to": 2, "quality": 3, "trip_id": None},
            {"id": 2, "year_from": 2024, "month_from": 5, "day_from": 1,
             "year_to": 2024, "month_to": 5, "day_to": 2, "quality": 3, "trip_id": 77},
        ]
    }
    c, _ = client({"quickEnter/get-visits-to-region": raw})
    visits = c.visits_for_region(100)
    assert len(visits) == 2
    assert {v.trip_id for v in visits} == {None, 77}


# --------------------------------------------------------------------------
# segmentation
# --------------------------------------------------------------------------

def _days(pairs):
    return {dt.date.fromisoformat(d): set(rs) for d, rs in pairs}


def test_cap_breaks_unbounded_runs():
    """Without a cap a continuous absence becomes one enormous 'trip'.

    200 days at home, then 165 unbroken days away — which is the shape of a
    nomadic year, and the shape that produced a single 465-day, 101-region
    "trip" before the cap existed. Home is inferred as the year's modal region,
    so it has to be the one with more days.
    """
    start = dt.date(2022, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {1} for i in range(200)}
    day_regions |= {start + dt.timedelta(days=200 + i): {2} for i in range(165)}

    uncapped = segment(day_regions, gap_days=2, cap_days=99999)
    capped = segment(day_regions, gap_days=2, cap_days=30)

    assert len(uncapped) == 1, "one unbroken absence is one run"
    assert uncapped[0].days == 165
    assert len(capped) > len(uncapped)
    assert max(j.days for j in capped) <= 31


def test_split_first_and_repeat_avoids_double_counting():
    """First visits stay standalone; only repeats go into trips."""
    j1 = Journey(dt.date(2024, 1, 1), dt.date(2024, 1, 3),
                 {10: (dt.date(2024, 1, 1), dt.date(2024, 1, 3))})
    j2 = Journey(dt.date(2024, 6, 1), dt.date(2024, 6, 3),
                 {10: (dt.date(2024, 6, 1), dt.date(2024, 6, 3)),
                  20: (dt.date(2024, 6, 2), dt.date(2024, 6, 3))})
    first, repeats = split_first_and_repeat([j1, j2])
    assert set(first) == {10, 20}
    assert len(repeats) == 1
    assert set(repeats[0].regions) == {10}, "20 is a first visit, so it must not be in the trip"


# --------------------------------------------------------------------------
# place-name folding
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [("Nukuʻalofa", "Nukualofa"), ("Jönköping", "Jonkoping"), ("Valparaíso", "Valparaiso")],
)
def test_fold_matches_across_alphabets(a, b):
    assert fold(a) == fold(b)


def test_components_splits_compound_names():
    assert "laspaz" not in components("La Paz/El Alto (BO)")
    assert "lapaz" in components("La Paz/El Alto (BO)")
    assert "elalto" in components("La Paz/El Alto (BO)")


def test_components_drops_short_fragments():
    """Two-letter state codes collide with everything."""
    assert "tx" not in components("Austin/Round Rock (TX)")


# --------------------------------------------------------------------------
# plan safety
# --------------------------------------------------------------------------

def test_only_apply_bucket_executes():
    plan = Plan(account=1, ops=[
        Op(kind="add_visit", bucket="apply", region=1),
        Op(kind="add_visit", bucket="review", region=2),
        Op(kind="add_visit", bucket="reject", region=3),
    ])
    assert [o.region for o in plan.to_apply()] == [1]


def test_op_key_is_stable_and_ignores_provenance():
    a = Op(kind="add_visit", region=5, date_from="2024-01-01", date_to="2024-01-02",
           evidence=["photo A"], confidence=0.9)
    b = Op(kind="add_visit", region=5, date_from="2024-01-01", date_to="2024-01-02",
           evidence=["photo B"], confidence=0.1)
    assert a.key == b.key, "re-running the same intent must be a no-op"


def test_plan_roundtrip(tmp_path):
    plan = Plan(account=12345, ops=[Op(kind="mark_dare", region=1142, bucket="apply")])
    path = plan.save(tmp_path / "plan.json")
    again = Plan.load(path)
    assert again.account == 12345
    assert again.ops[0].kind == "mark_dare"


# --------------------------------------------------------------------------
# track handling
# --------------------------------------------------------------------------

def test_track_dedupes_coordinates_before_geocoding():
    pts = [
        DayPoint(dt.date(2024, 1, 1), 50.4180, 30.5350, "Kyiv"),
        DayPoint(dt.date(2024, 1, 2), 50.41802, 30.53499, "Kyiv"),
        DayPoint(dt.date(2024, 1, 3), 41.8902, 12.4922, "Rome"),
    ]
    track = Track(points=pts)
    assert len(track.distinct_coords()) == 2
    assert set(track.places()) == {"kyiv", "rome"}


def test_transport_repr_hides_token():
    from wanderfill.api.transport import Transport

    t = Transport(token="00000000-DEAD-BEEF-0000-NOTAREALTOKEN")
    assert "DEAD" not in repr(t)
    assert "token" in repr(t) and "redacted" in repr(t)


def test_no_home_counts_every_day_as_travel():
    """A traveller with no home must not have a region silently treated as one.

    With home="infer" the modal region of the year is excluded from trips. For
    somebody genuinely nomadic that quietly deletes the place they spent most of
    the year in from their own record, which is the opposite of what they want.
    """
    start = dt.date(2022, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {1} for i in range(200)}
    day_regions |= {start + dt.timedelta(days=200 + i): {2} for i in range(160)}

    inferred = segment(day_regions, gap_days=2, cap_days=9999, home="infer")
    nomadic = segment(day_regions, gap_days=2, cap_days=9999, home=None)

    assert {r for j in inferred for r in j.regions} == {2}, "region 1 was treated as home"
    assert {r for j in nomadic for r in j.regions} == {1, 2}, "no home means everything counts"


def test_stated_home_is_used_verbatim():
    start = dt.date(2022, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {1} for i in range(200)}
    day_regions |= {start + dt.timedelta(days=200 + i): {2} for i in range(160)}

    js = segment(day_regions, gap_days=2, cap_days=9999, home=[2])
    assert {r for j in js for r in j.regions} == {1}, "region 2 was stated as home"


def test_empty_home_list_is_rejected_rather_than_guessed():
    """home=[] could mean 'no home' or 'I forgot to fill this in'. Refuse it."""
    day_regions = {dt.date(2022, 1, 1): {1}}
    with pytest.raises(ValueError, match="home=None"):
        segment(day_regions, home=[])
