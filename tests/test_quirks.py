"""Tests for the quirks that cost real damage the first time round.

These are not tests of the server. They are tests that this package cannot make
the same mistakes again, and each one names the incident it prevents.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from wanderfill.api.client import NomadMania, Visit, YearOnly
from wanderfill.api.errors import ApiError, DriftError, PrecisionLoss
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


def _one_visit(visit_id=11, **over):
    """The reply shape ``update_visit`` reads back before it agrees to write."""
    row = {"id": visit_id, "quality": 5, "trip_id": None,
           "year_from": 2024, "month_from": 1, "day_from": 1,
           "year_to": 2024, "month_to": 1, "day_to": 2}
    row.update(over)
    return {"quickEnter/get-visits-to-region": {"result": "OK", "data": [row]}}


def test_update_visit_passes_quality_through():
    c, t = client(_one_visit())
    c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=5)
    _, fields = t.sent[-1]
    assert fields["quality"] == 5


# --------------------------------------------------------------------------
# a visit can be precise, year-only, or undated — and a replacement can
# quietly destroy the difference
# --------------------------------------------------------------------------

def test_year_only_visit_is_not_read_as_undated():
    """The 13-in-892 shape. Reading it as ``None`` erases a year on write-back."""
    v = Visit.from_api(
        {"id": 1, "quality": 3, "trip_id": None,
         "year_from": 2013, "month_from": None, "day_from": None,
         "year_to": 2013, "month_to": None, "day_to": None},
        region=658,
    )
    assert v.date_from == YearOnly(2013)
    assert v.date_to == YearOnly(2013)


def test_year_only_sends_empty_month_and_day():
    """Empty, not omitted: an absent key is not 'unchanged' on a full replacement."""
    c, t = client(_one_visit(year_from=None, month_from=None, day_from=None,
                             year_to=None, month_to=None, day_to=None))
    c.update_visit(11, 22, YearOnly(2013), YearOnly(2013), quality=3)
    _, fields = t.sent[-1]
    assert fields["year_from"] == 2013
    assert fields["month_from"] == "" and fields["day_from"] == ""


def test_update_visit_refuses_to_blur_a_precise_date():
    """Writing a bare year over a full date deletes the month and day."""
    c, _ = client(_one_visit())
    with pytest.raises(PrecisionLoss, match="losing precision"):
        c.update_visit(11, 22, YearOnly(2024), YearOnly(2024), quality=5)


def test_update_visit_refuses_to_erase_dates_entirely():
    c, _ = client(_one_visit())
    with pytest.raises(PrecisionLoss):
        c.update_visit(11, 22, None, None, quality=5)


def test_update_visit_allows_deliberate_blurring():
    c, t = client(_one_visit())
    c.update_visit(11, 22, YearOnly(2024), YearOnly(2024), quality=5, allow_vaguer=True)
    _, fields = t.sent[-1]
    assert fields["month_from"] == ""


def test_update_visit_allows_sharpening_a_year_into_a_date():
    """The whole point of filling a profile in. Never blocked."""
    c, t = client(_one_visit(month_from=None, day_from=None, month_to=None, day_to=None))
    c.update_visit(11, 22, dt.date(2024, 3, 4), dt.date(2024, 3, 5), quality=3)
    _, fields = t.sent[-1]
    assert fields["month_from"] == 3 and fields["day_from"] == 4


def test_update_visit_refuses_to_write_to_a_visit_that_is_not_there():
    """A wrong region id would otherwise replace some other region's record."""
    c, _ = client(_one_visit(visit_id=999))
    with pytest.raises(PrecisionLoss, match="refusing to write blind"):
        c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=3)


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


# --------------------------------------------------------------------------
# quality can never go down — incident #1, structurally
# --------------------------------------------------------------------------

def test_update_visit_takes_max_against_the_live_record():
    """A caller passing 3 over a live 'lived here' must not downgrade it."""
    c, t = client(_one_visit(quality=5))
    c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=3)
    _, fields = t.sent[-1]
    assert fields["quality"] == 5, "the live 5 must win over the caller's 3"


def test_update_visit_still_raises_quality_upwards():
    c, t = client(_one_visit(quality=3))
    c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=5)
    _, fields = t.sent[-1]
    assert fields["quality"] == 5


def test_apply_refuses_an_op_with_no_quality():
    """`op.quality or 3` turned a missing field into a silent downgrade."""
    from wanderfill.plan.apply import _execute
    c, _ = client(_one_visit())
    op = Op(kind="update_visit", region=22, visit_id=11,
            date_from="2024-01-01", date_to="2024-01-02", quality=None)
    with pytest.raises(ValueError, match="no quality"):
        _execute(c, op)


def test_apply_does_not_promote_quality_zero_to_three():
    """0 is falsey; `or 3` silently rewrote 'no visit' as 'good visit'."""
    from wanderfill.plan.apply import _execute
    c, t = client(_one_visit(quality=0))
    op = Op(kind="update_visit", region=22, visit_id=11,
            date_from="2024-01-01", date_to="2024-01-02", quality=0)
    _execute(c, op)
    _, fields = t.sent[-1]
    assert fields["quality"] == 0


def test_plan_year_only_round_trips_through_execute():
    """A bare "2013" must reach the wire as a year, not as 1 January."""
    from wanderfill.plan.apply import _execute
    c, t = client(_one_visit(year_from=None, month_from=None, day_from=None,
                             year_to=None, month_to=None, day_to=None, quality=3))
    _execute(c, Op(kind="update_visit", region=22, visit_id=11,
                   date_from="2013", date_to="2013", quality=3))
    _, fields = t.sent[-1]
    assert fields["year_from"] == 2013 and fields["month_from"] == ""


# --------------------------------------------------------------------------
# the plan/apply safety boundary
# --------------------------------------------------------------------------

def test_op_key_distinguishes_different_visits_and_qualities():
    """Colliding keys let a journal mark a *different* write as already done."""
    base = dict(kind="update_visit", region=5, date_from="2024-01-01", date_to="2024-01-02")
    assert Op(**base, visit_id=1, quality=3).key != Op(**base, visit_id=2, quality=3).key
    assert Op(**base, visit_id=1, quality=3).key != Op(**base, visit_id=1, quality=5).key
    # provenance still must not affect it
    assert (Op(**base, visit_id=1, quality=3, label="a", confidence=0.1).key
            == Op(**base, visit_id=1, quality=3, label="b", confidence=0.9).key)


def test_basis_covers_visit_dates_not_just_region_ids():
    """Re-dating a visit changes no region id, so ids alone cannot detect drift."""
    from wanderfill.plan.model import basis_of
    def snap(d):
        return {"visited_regions": [5], "visited_dare": [],
                "visits": {"5": [{"id": 1, "date_from": d, "date_to": d, "quality": 3}]}}

    assert basis_of(snap("2024-01-01"), [5]) != basis_of(snap("2024-06-01"), [5])


def test_apply_refuses_a_plan_with_no_basis():
    """Failing open made the drift check decorative."""
    import pathlib
    import tempfile

    from wanderfill.plan.apply import apply_plan
    c, _ = client({"user/status-quick": {"result": "OK", "uid": 7},
                   "maps/get-visited-regions-ids-simple": {"result": "OK", "ids": []},
                   "maps/get-visited-dare-ids-simple": {"result": "OK", "ids": []}})
    plan = Plan(account=7, ops=[Op(kind="mark_dare", bucket="apply", region=1)])
    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(DriftError, match="no basis fingerprint"):
            apply_plan(c, plan, workdir=pathlib.Path(td), confirm=True)


def test_journal_is_write_ahead_and_survives_a_crash():
    """An op that opened and never closed must block a blind retry."""
    import pathlib
    import tempfile

    from wanderfill.plan.apply import Journal
    op = Op(kind="mark_dare", region=1)
    with tempfile.TemporaryDirectory() as td:
        j = Journal(pathlib.Path(td) / "j.ndjson")
        j.opening(op)                      # crash right here
        assert j.unresolved() == {op.key}
        assert op.key not in j.done_keys()
        j.note(op, "OK")
        assert j.unresolved() == set()
        assert op.key in j.done_keys()


def test_countries_does_not_hand_out_a_bare_yes_field():
    """`row["yes"]` reads as the YES score and is not one."""
    c, _ = client({"slow/get-slow-app": {"result": "OK", "slow": [
        {"country_id": 1, "country": "X", "flag": "/f/1.png", "visited": 1, "yes": 8}]}})
    row = c.countries()[0]
    assert "yes" not in row
    assert row["yes_stored"] == 8


def test_kye_quadrant_id_is_not_a_region_id():
    """qids and NM region ids overlap numerically; conflating them is silent."""
    from wanderfill.plan.apply import _execute
    c, t = client()
    _execute(c, Op(kind="mark_kye", item=570, region=None))
    action, fields = t.sent[-1]
    assert action == "kye/set-kye"
    assert fields == {"qid": 570, "visited": 1}


def test_mark_kye_never_unticks():
    c, t = client()
    c.mark_kye(38)
    _, fields = t.sent[-1]
    assert fields["visited"] == 1


# --------------------------------------------------------------------------
# YES: undated is 8, not the age — and the rule of thumb is not a rule
# --------------------------------------------------------------------------

def test_undated_country_scores_eight_not_the_age():
    """Scoring undated as the age made a recompute disagree with the server by 33."""
    from wanderfill.api.client import UNDATED_YES
    assert UNDATED_YES == 8
    c, _ = client({
        "user/get-settings": {"result": "OK", "date_of_birth": "1985-01-03"},
        "slow/get-slow-app": {"result": "OK", "slow": [
            {"country_id": 1, "country": "Seen", "flag": "/f/1.png", "visited": 1, "yes": 8},
            {"country_id": 2, "country": "Never", "flag": "/f/2.png", "visited": 0, "yes": 41}]},
        "quickEnter/get-regions": {"result": "OK", "data": {"regions": []}},
    })
    y = c.yes_scores(today=dt.date(2026, 8, 16))
    assert y[1]["yes"] == 8 and y[1]["undated"] is True
    assert y[2]["yes"] == 41, "never visited still scores the age"


def test_yes_delta_is_a_gain_for_recent_years_and_a_loss_for_old_ones():
    """"Dating an old visit makes YES worse" is false for a recent visit."""
    from wanderfill.api.client import yes_delta
    today = dt.date(2026, 8, 16)
    assert yes_delta(8, 2026, today=today) == -8   # this year: full gain
    assert yes_delta(8, 2025, today=today) == -8   # the "gift" year
    assert yes_delta(8, 2019, today=today) == -1   # still a gain
    assert yes_delta(8, 2018, today=today) == 0    # break-even, exactly
    assert yes_delta(8, 2013, today=today) == 5    # Myanmar: a real loss


# --------------------------------------------------------------------------
# grading a coordinate: the speed rule is code, not prose
# --------------------------------------------------------------------------

def _fix(lat, lon, day, hh=None, mm=0, ss=0):
    from wanderfill.grade import Fix
    at = dt.datetime(day.year, day.month, day.day, hh, mm, ss) if hh is not None else None
    return Fix(lat, lon, day, at)


def test_grade_rejects_a_flight():
    """Two mid-Pacific points 1,100 km and 2.5 hours apart were one flight."""
    from wanderfill.grade import grade
    d = dt.date(2025, 10, 22)
    v = grade([_fix(-18.265, -173.905, d, 12, 59), _fix(-18.318, -163.413, d, 15, 28)])
    assert not v.ok and "km/h" in v.reason


def test_grade_rejects_an_airport_layover_on_time_not_speed():
    """Sitting in Terminal E is slow; it is the span that catches it."""
    from wanderfill.grade import grade
    d = dt.date(2023, 4, 27)
    v = grade([_fix(29.984, -95.333, d, 17, m) for m in (27, 39, 53)])
    assert not v.ok and "min" in v.reason


def test_grade_accepts_two_distinct_days():
    from wanderfill.grade import grade
    v = grade([_fix(48.85, 2.35, dt.date(2024, 5, 1)), _fix(48.86, 2.34, dt.date(2024, 5, 2))])
    assert v.ok and "2 distinct days" in v.reason


def test_grade_accepts_one_dense_day_at_ground_speed():
    from wanderfill.grade import grade
    d = dt.date(2024, 3, 3)
    v = grade([_fix(-33.4 + i * 0.001, -70.79, d, 3 + i // 4, (i * 13) % 60) for i in range(12)])
    assert v.ok and "one day" in v.reason


def test_grade_never_infers_speed_between_untimed_rows():
    """Day-level rows have no intra-day clock; pairing them invents a speed."""
    from wanderfill.grade import looks_airborne
    d = dt.date(2013, 7, 1)
    fast, legs = looks_airborne([_fix(50.4, 30.5, d), _fix(16.8, 96.1, d)])
    assert (fast, legs) == (0, 0)


def test_grade_holds_rather_than_rejects():
    """St Petersburg was in the held pile and was real. Held is not rejected."""
    from wanderfill.grade import grade
    v = grade([_fix(60.002, 30.299, dt.date(2014, 6, 17))])
    assert not v.ok and "single day" in v.reason


# --------------------------------------------------------------------------
# writes are never retried, and a lost answer is not a failure
# --------------------------------------------------------------------------

def test_writes_are_not_retried():
    """A retried write is how duplicate visits are made."""
    from wanderfill.api.transport import Transport
    assert Transport._is_write("quickEnter/add-visit")
    assert Transport._is_write("quickEnter/update-visit")
    assert Transport._is_write("trips/new-trip")
    assert Transport._is_write("kye/set-kye")
    assert not Transport._is_write("quickEnter/get-visits-to-region")
    assert not Transport._is_write("slow/get-slow-app")
    assert not Transport._is_write("regions/get-regions-list-2")


def test_apply_refuses_a_plan_containing_the_same_write_twice():
    """`already` is read once, so both copies would otherwise execute."""
    import pathlib
    import tempfile

    from wanderfill.plan.apply import apply_plan
    twice = [Op(kind="mark_dare", bucket="apply", region=7),
             Op(kind="mark_dare", bucket="apply", region=7)]
    plan = Plan(account=7, ops=twice, basis={"fingerprint": "x"})
    c, _ = client({"user/status-quick": {"result": "OK", "uid": 7},
                   "maps/get-visited-regions-ids-simple": {"result": "OK", "ids": []},
                   "maps/get-visited-dare-ids-simple": {"result": "OK", "ids": []}})
    with tempfile.TemporaryDirectory() as td:
        with pytest.raises((ValueError, DriftError)) as e:
            apply_plan(c, plan, workdir=pathlib.Path(td), confirm=True)
        assert "twice" in str(e.value) or "drift" in str(e.value).lower()


def test_regions_touched_includes_trip_members():
    """create_trip names its regions in `regions`, not `region`."""
    from wanderfill.plan.model import regions_touched
    ops = [Op(kind="create_trip", regions=[{"id": 11, "quality": 3}, {"id": 12, "quality": 3}]),
           Op(kind="update_visit", region=5, visit_id=1),
           Op(kind="mark_kye", item=570)]
    assert regions_touched(ops) == {5, 11, 12}, "a qid must not be read as a region"


def test_year_only_visit_widens_to_a_whole_year_for_date_checks():
    """Comparing a YearOnly to a date raised TypeError and broke `evidence`."""
    from wanderfill.cli.main import _visit_window

    class V:
        def __init__(self, a, b): self.date_from, self.date_to = a, b

    assert _visit_window(V(YearOnly(2013), YearOnly(2013))) == (
        dt.date(2013, 1, 1), dt.date(2013, 12, 31))
    assert _visit_window(V(dt.date(2024, 1, 2), dt.date(2024, 1, 3))) == (
        dt.date(2024, 1, 2), dt.date(2024, 1, 3))
    assert _visit_window(V(None, None)) is None


def test_apply_verifies_writes_against_the_server():
    """"OK" is not evidence. Both historical incidents returned OK at the time."""
    from wanderfill.plan.apply import verify
    c, _ = client(_one_visit(quality=3, year_from=2024, month_from=1, day_from=1,
                             year_to=2024, month_to=1, day_to=2))
    good = verify(c, [Op(kind="update_visit", region=22, visit_id=11,
                         date_from="2024-01-01", date_to="2024-01-02", quality=3)])
    assert good["checked"] == 1 and good["mismatches"] == []

    bad = verify(c, [Op(kind="update_visit", region=22, visit_id=11,
                        date_from="2024-06-01", date_to="2024-06-02", quality=3)])
    assert bad["mismatches"], "a date the server did not store must be reported"


def test_verify_reports_a_duplicate_rather_than_passing():
    """Fifty duplicate visits were made once; this is the check that would see it."""
    from wanderfill.plan.apply import verify
    row = {"id": 1, "quality": 3, "trip_id": 99,
           "year_from": 2024, "month_from": 1, "day_from": 1,
           "year_to": 2024, "month_to": 1, "day_to": 1}
    c, _ = client({"quickEnter/get-visits-to-region":
                   {"result": "OK", "data": [row, {**row, "id": 2}]}})
    out = verify(c, [Op(kind="add_visit", region=5,
                        date_from="2024-01-01", date_to="2024-01-01", quality=3)])
    assert any("duplicate" in m for m in out["mismatches"])
    assert out["phantom_trips"], "the auto-created trip id must be recorded"
