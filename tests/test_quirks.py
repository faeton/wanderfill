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
from wanderfill.plan.segment import (
    MULTI_REGION_FLOOR,
    HomeWindow,
    Journey,
    compare_homes,
    multi_region_share,
    segment,
    split_first_and_repeat,
)
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


def test_jump_cuts_a_dense_track_the_cap_would_glue():
    """The incident: one month came out as a single trip.

    Photographs on every single day, three unrelated places in a row. Nothing
    ever breaks on a gap, so gap+cap alone returns one 30-day "trip" spanning
    all three. Cutting on jumps returns the three journeys a person would name.
    """
    start = dt.date(2026, 7, 16)
    day_regions = {}
    for i in range(14):                      # a fortnight in region 1
        day_regions[start + dt.timedelta(days=i)] = {1}
    for i in range(9):                       # a drive: each day overlaps the last
        day_regions[start + dt.timedelta(days=14 + i)] = {10 + i, 11 + i}
    for i in range(7):                       # then a flight somewhere unrelated
        day_regions[start + dt.timedelta(days=23 + i)] = {99}

    glued = segment(day_regions, gap_days=2, cap_days=30, home=None,
                    split_on_jump=False)
    cut = segment(day_regions, gap_days=2, cap_days=30, home=None)

    assert len(glued) == 1, "gap+cap cannot see a boundary here — that is the bug"
    assert len(cut) == 3, "stay, drive, flight"
    # The drive must survive as ONE journey: consecutive days share a region.
    drive = cut[1]
    assert drive.days == 9 and len(drive.regions) == 10


def test_auto_refuses_to_jump_cut_a_one_region_per_day_track():
    """The opposite incident: jump-cutting a collapsed track shreds it.

    A track already reduced to one region per day makes every move look like a
    jump. On a real 17-year history that turned 326 journeys into 1,155, 38% of
    them one day long. "auto" has to measure the track and decline.
    """
    start = dt.date(2024, 1, 1)
    # Ten days, moving every other day, one region per day and never overlapping.
    day_regions = {start + dt.timedelta(days=i): {i // 2} for i in range(10)}
    assert multi_region_share(day_regions) == 0.0

    auto = segment(day_regions, gap_days=2, cap_days=30, home=None)
    forced = segment(day_regions, gap_days=2, cap_days=30, home=None, split_on_jump=True)

    assert len(auto) == 1, "auto must leave a collapsed track alone"
    assert len(forced) == 5, "forced jump-cutting shreds it, which is the point"


def test_auto_enables_jump_cutting_on_a_detailed_track():
    """And it has to say yes when the track does carry both ends of a day."""
    start = dt.date(2024, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {i, i + 1} for i in range(6)}
    day_regions[start + dt.timedelta(days=6)] = {99}      # an unrelated flight
    assert multi_region_share(day_regions) > MULTI_REGION_FLOOR
    assert len(segment(day_regions, gap_days=2, cap_days=30, home=None)) == 2


def test_split_on_jump_rejects_nonsense():
    """A typo'd string must not be read as truthy and silently enable cutting."""
    day_regions = {dt.date(2024, 1, 1): {1}}
    with pytest.raises(ValueError, match="split_on_jump"):
        segment(day_regions, home=None, split_on_jump="yes")


def test_home_windows_describe_a_home_that_started_and_stopped():
    """Neither a bare region list nor "infer" can express a home with dates.

    Region 1 was home for the first year and nothing afterwards. A bare
    ``home=[1]`` discards the later stay too; ``home=None`` keeps the earlier
    one. Only a window gets both halves right.
    """
    day_regions = {}
    for i in range(300):                                  # year one: living in 1
        day_regions[dt.date(2021, 1, 1) + dt.timedelta(days=i)] = {1}
    for i in range(60):                                   # year three: passing through 1
        day_regions[dt.date(2023, 6, 1) + dt.timedelta(days=i)] = {1}

    windowed = segment(
        day_regions,
        gap_days=2,
        cap_days=9999,
        home=[HomeWindow(dt.date(2021, 1, 1), dt.date(2021, 12, 31), {1})],
    )
    days_kept = sum(j.days for j in windowed)
    assert days_kept == 60, "the 2023 stay is travel, the 2021 one is not"

    always_home = segment(day_regions, gap_days=2, cap_days=9999, home=[1])
    assert always_home == [], "a bare id wrongly discards the later stay"


def test_open_ended_home_window():
    """``start=None`` means 'and we are not saying when it began'."""
    day_regions = {dt.date(2020, 1, 1) + dt.timedelta(days=i): {1} for i in range(10)}
    w = HomeWindow(None, dt.date(2020, 1, 5), {1})
    assert w.covers(dt.date(1999, 1, 1)) and not w.covers(dt.date(2020, 1, 6))
    js = segment(day_regions, gap_days=2, cap_days=9999, home=[w])
    assert sum(j.days for j in js) == 5


def test_empty_home_window_is_rejected():
    """A window with no regions is the 'no home then' case written wrongly."""
    with pytest.raises(ValueError, match="uncovered"):
        HomeWindow(dt.date(2021, 1, 1), dt.date(2021, 12, 31), [])


def test_windows_and_bare_ids_cannot_be_mixed():
    """A bare id alongside a dated window silently means 'home forever'."""
    day_regions = {dt.date(2021, 1, 1): {1}}
    with pytest.raises(TypeError, match="mixes"):
        segment(day_regions, home=[HomeWindow(None, None, {1}), 2])


def test_compare_homes_reports_days_not_just_trips():
    """Choosing a home model is a choice about which days get discarded.

    Trip counts hide that. The number somebody needs to see before approving a
    model is how much of their life it has decided was not travel.
    """
    day_regions = {dt.date(2024, 1, 1) + dt.timedelta(days=i): {1 if i < 200 else 2}
                   for i in range(300)}
    rows = {r["model"]: r for r in compare_homes(
        day_regions, {"inferred": "infer", "nomadic": None}, cap_days=9999)}
    assert rows["nomadic"]["days_home"] == 0
    assert rows["inferred"]["days_home"] == 200, "region 1 is the modal region"
    assert rows["nomadic"]["days_travel"] > rows["inferred"]["days_travel"]


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


# --------------------------------------------------------------------------
# the geocoder's response shape depends on a request parameter
# --------------------------------------------------------------------------

def test_resolver_reads_both_geocoder_shapes():
    """share=0 answers {"region": N}; without it, {"nm": {"id": N}}.

    Parsing only the second silently resolves nothing at all — every coordinate
    comes back unplaced, which reads like a data problem rather than a parsing
    one. That cost a full re-run of a 513-point trip.
    """
    from wanderfill.geo.resolve import RegionResolver

    r = RegionResolver(catalogue={181: {"name": "Iceland"}}, reader=object())
    assert r.from_geocoder({"result": "OK", "region": 181}) == 181
    assert r.from_geocoder({"result": "OK", "nm": {"id": 181}}) == 181


def test_resolver_rejects_ids_absent_from_the_catalogue():
    """Stale ids and the -1 open-water sentinel must both come back as None."""
    from wanderfill.geo.resolve import RegionResolver

    r = RegionResolver(catalogue={181: {"name": "Iceland"}}, reader=object())
    assert r.from_geocoder({"region": 1387}) is None   # stale: Switzerland, since split
    assert r.from_geocoder({"region": -1}) is None     # open water
    assert r.from_geocoder({"result": "OK"}) is None


# --------------------------------------------------------------------------
# a visit can have no dates at all
# --------------------------------------------------------------------------

def test_visit_survives_null_dates():
    """Every date field comes back null for a region that was merely clicked.

    That is how most long-standing profiles are populated — one real profile
    had 118 of them — and ``int(None)`` raises ``TypeError``. The failure is
    invisible until something reads *every* region's visits rather than the
    handful it just wrote, at which point a whole run dies on region 4.
    """
    v = Visit.from_api(
        {
            "id": 13132630, "quality": 3, "trip_id": None,
            "year_from": None, "month_from": None, "day_from": None,
            "year_to": None, "month_to": None, "day_to": None,
        },
        region=4,
    )
    assert v.date_from is None and v.date_to is None
    assert v.id == 13132630          # it is still a visit
    assert v.quality == 3            # and it still counts toward the region


def test_visit_still_reads_a_dated_row():
    v = Visit.from_api(
        {
            "id": 1, "quality": 4, "trip_id": 77,
            "year_from": 2024, "month_from": 1, "day_from": 3,
            "year_to": 2024, "month_to": 1, "day_to": 4,
        },
        region=3,
    )
    assert v.date_from == dt.date(2024, 1, 3)
    assert v.date_to == dt.date(2024, 1, 4)
    assert v.trip_id == 77


# --------------------------------------------------------------------------
# the token comes from somewhere, and which somewhere matters
# --------------------------------------------------------------------------

def test_dotenv_reads_both_shapes_people_write(tmp_path):
    from wanderfill.cli.main import read_dotenv

    f = tmp_path / ".env"
    f.write_text(
        "# a comment\n"
        "\n"
        "export NM_TOKEN='abc-123'\n"
        "OTHER=plain\n"
        'QUOTED="with spaces"\n'
        "not a variable\n"
    )
    env = read_dotenv(f)
    assert env["NM_TOKEN"] == "abc-123"     # export stripped, quotes stripped
    assert env["OTHER"] == "plain"
    assert env["QUOTED"] == "with spaces"
    assert "not a variable" not in env


def test_dotenv_never_raises_on_a_file_it_cannot_read(tmp_path):
    """Failing to start because of a stray character is a bad half-hour."""
    from wanderfill.cli.main import read_dotenv

    assert read_dotenv(tmp_path / "absent") == {}
    weird = tmp_path / ".env"
    weird.write_bytes(b"\xff\xfe not utf-8 at all")
    assert read_dotenv(weird) == {}


def test_the_environment_beats_a_file(tmp_path, monkeypatch):
    """An exported variable is somebody saying 'this one, now'.

    A file on disk is not, and a token the user forgot they wrote is how the
    wrong account gets written to.
    """
    from wanderfill.cli.main import find_token

    (tmp_path / ".env").write_text("NM_TOKEN=from-file\n")
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("NM_TOKEN", "from-environment")
    assert find_token()[0] == "from-environment"

    monkeypatch.delenv("NM_TOKEN")
    token, source = find_token()
    assert token == "from-file"
    assert source.endswith(".env")      # and it says where it came from


def test_the_search_stops_at_the_repository_root(tmp_path, monkeypatch):
    """Never climb out of the project hunting for somebody else's secrets."""
    from wanderfill.cli.main import find_token

    (tmp_path / ".env").write_text("NM_TOKEN=outside-the-repo\n")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("NM_TOKEN", raising=False)
    assert find_token() == ("", "")
