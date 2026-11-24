"""Tests for trip runs: which days are one stay, which stays are offered, and which never.

A run that is offered becomes a visit after one tired click, on a profile other people
check. Every rule here is a day, a stay or a second copy that did not happen.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import hashlib
import itertools
import json
import math
import random
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path

import pytest

from wanderfill.api.client import QUALITY, Visit, YearOnly, _date_parts
from wanderfill.api.errors import PlanInvalid
from wanderfill.geo.resolve import Resolution
from wanderfill.geo.tiles import haversine_km
from wanderfill.plan import trip_list, trips
from wanderfill.plan.manifest import canonical
from wanderfill.plan.model import _CONTROLS
from wanderfill.plan.review import Shot, default_speeds
from wanderfill.plan.trip_list import (
    ListedRun,
    PhotoTime,
    TripList,
    build_list,
    left_out,
    load_answers,
    read_answers,
    render_page,
    trips_rows,
    visits_rows,
    words_problem,
)
from wanderfill.plan.trips import (
    Boundary,
    Obs,
    Place,
    Run,
    SweepRow,
    boundaries,
    evidence_grade,
    find_runs,
    group,
    judge,
    kind_of,
    place,
    regions_to_read,
    status_of,
    sweep,
)
from wanderfill.sources.photos_app import PhotoAsset

UTC = dt.timezone.utc
H = 3600
R, Q, S = 1, 2, 3


def day(n, month=5, year=2026):
    return dt.date(year, month, 1) + dt.timedelta(days=n - 1)


_serial = itertools.count()


def ob(d, region=R, hour=12.0, *, zone=0.0, state="inside", kind="ground", use="all",
       lat=None, lon=None, origin="own-camera", timed=True, recorded=True, uuid=None):
    """One photo at `hour` of local day `d`, by a clock `zone` hours ahead of UTC."""
    d = day(d) if isinstance(d, int) else d
    n = next(_serial)
    taken = dt.datetime.combine(d, dt.time()) + dt.timedelta(hours=hour)
    utc = (taken - dt.timedelta(hours=zone)).replace(tzinfo=UTC)
    if not timed:
        kind, use = "untimed", "none"
    region = region if state in ("inside", "edge", "nearest") else None
    return Obs(Shot(taken, utc if timed else None,
                    (region or 0) * 0.3 + n % 97 * 1e-4 if lat is None else lat,
                    zone * 15 if lon is None else lon,
                    origin, kind, use, uuid=uuid or f"u{n}",
                    zone=round(zone * H) if recorded else None), Place(region, state))


def stay(region, first, last, **kw):
    """Two photos on every day, half a kilometre and nine hours apart."""
    out = []
    for d in range(first, last + 1):
        out += [ob(d, region, 9, **kw), ob(d, region, 18, **kw)]
    return out


def visit(a, b, region=R, id=7, quality=3, trip_id=None):
    as_when = lambda w: day(w) if isinstance(w, int) and w < 1000 else (  # noqa: E731
        YearOnly(w) if isinstance(w, int) else w)
    return Visit(id, region, as_when(a), as_when(b), quality, trip_id)


def spans(runs, region=None):
    return [(r.first.day, r.last.day) for r in runs if region in (None, r.region)]


def judged(obs, visits=None, *, since=None, until=None, gap_days=2, track=(), assets=None):
    """The runs by `(region, first day)`, every region's list read and empty unless given."""
    runs = find_runs(obs, gap_days=gap_days, track=track)
    read = {r.region: [] for r in runs} | (visits or {})
    return judge(runs, obs, read, since=since, until=until, track=track, assets=assets)


def one(obs, visits=None, **kw):
    """The one run in region R."""
    (run,) = [r for r in judged(obs, visits, **kw) if r.region == R]
    return run


def kinds(run):
    return {kind_of(r) for r in run.reasons}


def flags(run):
    return {kind_of(f) for f in run.flags}


# -- 2, 2a, 2b: what is proof ---------------------------------------------------


def test_a_ground_photo_strictly_inside_by_a_trusted_clock_is_proof():
    assert ob(1).proof


@pytest.mark.parametrize("bad", [
    {"kind": "fast"}, {"kind": "conflict"}, {"use": "none"}, {"use": "airports"},
    {"state": "edge"}, {"state": "nearest"}, {"state": "nowhere"}, {"state": "unread"},
    {"state": "stale"}, {"timed": False}, {"recorded": False},
    {"zone": 0, "lon": 150.0}, {"origin": "airdrop"}, {"origin": "unknown"},
    {"origin": "other"},
], ids=str)
def test_nothing_else_is_proof(bad):
    assert not ob(1, **bad).proof
    assert find_runs([ob(1, **bad)]) == []


def test_a_day_border_photo_by_the_clock_of_another_continent_dates_nothing():
    """Zone 0 at longitude 150: the photo is on the next day there."""
    assert not ob(1, hour=23, zone=0, lon=150.0).proof
    assert ob(1, hour=23, zone=10, lon=150.0).proof


def resolver(answer, *, asked=None):
    """`answer(dy, dx)` in metres from the photo; a region, a Resolution, or None."""
    lat0, lon0 = 60.0, 20.0

    def resolve(lat, lon):
        dy = haversine_km(lat0, lon0, lat, lon0) * 1000 * (1 if lat >= lat0 else -1)
        dx = haversine_km(lat0, lon0, lat0, lon) * 1000 * (1 if lon >= lon0 else -1)
        if asked is not None:
            asked.append((dy, dx))
        got = answer(dy, dx)
        if isinstance(got, Resolution):
            return got
        return Resolution(lat, lon, got, "tile_interior" if got else "unresolved")

    return lambda: place(resolve, lat0, lon0)


def res(method, region=None, **kw):
    return Resolution(0, 0, region, method, **kw)


def test_the_eight_points_are_a_hundred_metres_around_the_photo():
    asked = []
    assert resolver(lambda dy, dx: R, asked=asked)() == Place(R, "inside")
    assert len(asked) == 9 and asked[0] == (0, 0)
    around = [(dy, dx) for dy, dx in asked[1:]]
    assert all(abs((dy**2 + dx**2) ** 0.5 - 100) < 1 for dy, dx in around)
    octants = {(round(dy / 70), round(dx / 70)) for dy, dx in around}
    assert len(octants) == 8 and (0, 0) not in octants


@pytest.mark.parametrize("dy, dx", [(1, 0), (-1, 0), (0, 1), (0, -1),
                                    (1, 1), (1, -1), (-1, 1), (-1, -1)])
@pytest.mark.parametrize("there, state", [
    (R, "inside"), (None, "inside"),                        # the same region, or the sea
    (res("tile_nearest", R), "inside"),
    (Q, "edge"), (res("tile_nearest", Q), "edge"),
    (res("tile_error"), "edge"),
    (res("unresolved", stale=True), "edge"), (res("tile_nearest", R, stale=True), "edge"),
], ids=str)
def test_one_point_around_it_in_another_region_makes_it_edge(dy, dx, there, state):
    hit = lambda y, x: round(y / 70.8) == dy and round(x / 70.8) == dx  # noqa: E731
    got = resolver(lambda y, x: there if hit(y, x) or (
        0 in (dy, dx) and round(y / 100) == dy and round(x / 100) == dx
        and min(abs(y), abs(x)) < 1) else R)()
    assert got == Place(R, state)


@pytest.mark.parametrize("answer, want", [
    (res("tile_nearest", R), Place(R, "nearest")),
    (res("unresolved"), Place(None, "nowhere")),
    (res("tile_error"), Place(None, "unread")),
    (res("unresolved", stale=True), Place(None, "stale")),
    (res("tile_nearest", R, stale=True), Place(None, "stale")),
    (res("geocoder", R), Place(R, "nearest")),              # an older map, never interior
    (res("geocoder"), Place(None, "nowhere")),
], ids=str)
def test_neighbours_are_asked_for_an_interior_point_only(answer, want):
    asked = []
    assert resolver(lambda dy, dx: answer, asked=asked)() == want
    assert len(asked) == 1


def unit(lat, lon):
    y, x = math.radians(lat), math.radians(lon)
    return (math.cos(y) * math.cos(x), math.cos(y) * math.sin(x), math.sin(y))


@pytest.mark.parametrize("lat, lon", [
    (90.0, 0.0), (-90.0, 0.0), (90.0, 180.0), (89.9999, 10.0), (-89.9999, -170.0),
    (89.9991, 0.0), (0.0, 180.0), (-17.0, -180.0), (70.0, 179.9999), (0.0, 0.0), (60.0, 20.0),
])
def test_the_points_around_a_photo_are_a_hundred_metres_off_at_eight_bearings(lat, lon):
    asked = []

    def resolve(y, x):
        asked.append((y, x))
        return Resolution(y, x, R, "tile_interior")

    place(resolve, lat, lon)
    around = asked[1:]
    assert len(around) == 8
    assert all(-90 <= y <= 90 and -180 <= x <= 180 for y, x in around)
    assert all(haversine_km(lat, lon, y, x) == pytest.approx(0.1, rel=1e-4) for y, x in around)
    # Bearings, with any two directions across the photo for axes.
    here = unit(lat, lon)
    east = unit(0.0, lon + 90)
    north = [here[(i + 1) % 3] * east[(i + 2) % 3] - here[(i + 2) % 3] * east[(i + 1) % 3]
             for i in range(3)]
    turns = sorted(math.degrees(math.atan2(*(sum(a * b for a, b in zip(unit(y, x), axis,
                                                                       strict=True))
                                             for axis in (east, north)))) % 360
                   for y, x in around)
    steps = [b - a for a, b in zip(turns, [*turns[1:], turns[0] + 360], strict=True)]
    assert all(step == pytest.approx(45, abs=0.05) for step in steps)


@pytest.mark.parametrize("pole", [90, -90])
def test_a_border_beside_a_pole_is_seen_whichever_longitude_the_pole_has(pole):
    def resolve(lat, lon):
        there = abs(lon) > 90 and abs(lat) < 89.9995
        return Resolution(lat, lon, Q if there else R, "tile_interior")

    for lon in (0, 45, -90):
        assert place(resolve, pole, lon) == Place(R, "edge")


# -- 3: runs ----------------------------------------------------------------------


def test_adjacent_days_are_one_run():
    (r,) = find_runs(stay(R, 1, 3))
    assert (r.region, r.first, r.last) == (R, day(1), day(3))
    assert r.days == (day(1), day(2), day(3)) and r.silent == ()
    assert r.id == "1:2026-05-01:2026-05-03" and len(r.shots) == 6
    assert list(r.shots) == sorted(r.shots, key=lambda o: o.shot.taken_utc)


def test_a_run_that_was_not_judged_is_not_offered():
    (r,) = find_runs(stay(R, 1, 3))
    assert r.status == "fixed" and r.reasons


@pytest.mark.parametrize("gap_days, silent, joined", [
    (1, 0, True), (1, 1, False), (2, 1, True), (2, 2, False), (3, 2, True), (3, 3, False),
    (5, 4, True), (5, 5, False),
])
def test_silent_days_join_up_to_the_gap(gap_days, silent, joined):
    runs = find_runs([ob(1), ob(2 + silent)], gap_days=gap_days)
    assert len(runs) == (1 if joined else 2)
    if joined:
        assert runs[0].silent == tuple(day(d) for d in range(2, 2 + silent))
        assert runs[0].days == (day(1), day(2 + silent))


def test_silent_days_are_counted_in_a_row():
    """A compatible day between two silent ones starts the count again."""
    between = ob(3, state="edge")
    assert spans(find_runs([ob(1), between, ob(5)])) == [(1, 5)]
    assert find_runs([ob(1), between, ob(5)])[0].silent == (day(2), day(4))
    assert len(find_runs([ob(1), between, ob(6)])) == 2
    assert spans(find_runs([ob(1), ob(3), ob(5)])) == [(1, 5)]


@pytest.mark.parametrize("gap_days", [0, -1, True, 1.5])
def test_a_gap_is_a_whole_number_of_days(gap_days):
    with pytest.raises(ValueError):
        find_runs([ob(1)], gap_days=gap_days)


@pytest.mark.parametrize("state", ["inside", "edge", "nearest"])
def test_coast_days_are_compatible(state):
    """A day of photos on the water's edge is neither proof nor absence."""
    obs = [ob(1), *[ob(d, state=state, recorded=False) for d in (2, 3, 4, 5)], ob(6)]
    (r,) = find_runs(obs)
    assert (r.first, r.last, r.days) == (day(1), day(6), (day(1), day(6)))
    assert r.silent == ()


@pytest.mark.parametrize("bad", [
    {"kind": "fast"}, {"kind": "conflict"}, {"use": "none"}, {"use": "airports"},
    {"timed": False}, {"state": "nowhere"}, {"state": "unread"}, {"state": "stale"},
    {"region": Q}, {"region": Q, "state": "edge"}, {"region": Q, "state": "nearest"},
], ids=str)
@pytest.mark.parametrize("beside", ["alone", "before", "after"])
def test_anything_else_on_a_day_between_is_a_barrier(bad, beside):
    """Also beside a photo that is compatible."""
    obs = [ob(1), ob(2, **bad), ob(3)]
    if beside != "alone":
        obs.insert(1 if beside == "before" else 2, ob(2, state="edge"))
    assert spans(find_runs(obs, gap_days=5), R) == [(1, 1), (3, 3)]


def test_a_day_without_a_photo_is_not_a_barrier_and_with_anybody_elses_neither():
    assert spans(find_runs([ob(1), ob(2, Q, origin="airdrop"), ob(3)]), R) == [(1, 3)]


def test_a_track_row_elsewhere_on_a_silent_day_is_a_barrier():
    obs = [ob(1), ob(3)]
    assert spans(find_runs(obs, track=[(day(2), R)])) == [(1, 3)]
    assert find_runs(obs, track=[(day(2), R)])[0].silent == (day(2),)
    for rows in ([(day(2), Q)], [(day(2), R), (day(2), Q)], [(day(2), None)]):
        assert spans(find_runs(obs, track=rows)) == [(1, 1), (3, 3)]
    # On a day with photos it is only a flag, and a day-level row proves nothing.
    assert spans(find_runs([*obs, ob(2, state="edge")], track=[(day(2), Q)])) == [(1, 3)]
    assert spans(find_runs(obs, track=[(day(1), Q), (day(4), R)])) == [(1, 3)]


def test_other_photos_on_a_proof_day_do_not_matter():
    """Transfer days and day trips: there is proof of both on one day."""
    obs = [ob(1), ob(2, R, 8), ob(2, Q, 12), ob(2, R, 20), ob(2, kind="fast", hour=13), ob(3)]
    assert spans(find_runs(obs), R) == [(1, 3)]
    assert spans(find_runs(obs), Q) == [(2, 2)]
    obs = [ob(1, R, 8), ob(1, Q, 20), ob(2, Q, 8), ob(2, R, 20)]
    assert spans(find_runs(obs), R) == [(1, 2)]


def test_the_order_of_the_photos_does_not_matter():
    obs = [*stay(R, 1, 3), *stay(Q, 3, 5), ob(4, R, kind="fast"), *stay(R, 7, 9),
           *stay(S, 12, 14)]
    want = judged(obs)
    cuts = boundaries(want, gap_days=2, obs=obs)
    for seed in range(5):
        mixed = obs[:]
        random.Random(seed).shuffle(mixed)
        assert judged(mixed) == want
        assert boundaries(judged(mixed), gap_days=2, obs=mixed) == cuts


def test_runs_are_in_time_order():
    obs = [*stay(S, 8, 9), *stay(R, 1, 3), *stay(Q, 4, 6)]
    assert [r.region for r in find_runs(obs)] == [R, Q, S]


# -- the date line ----------------------------------------------------------------


def eastward():
    """R at +13 until the evening of the 5th; Q at -10 from the 4th, eleven hours later."""
    return [*stay(R, 3, 5, zone=13, lon=178.0),
            ob(4, Q, 19, zone=-10, lon=-155.0), ob(4, Q, 21, zone=-10, lon=-155.0),
            *stay(Q, 5, 6, zone=-10, lon=-155.0)]


def test_a_flight_east_over_the_date_line():
    obs = eastward()
    r, q = judged(obs)
    assert (r.region, q.region) == (R, Q)  # by UTC, not by the clock
    assert spans([r, q]) == [(3, 5), (4, 6)]
    assert "open" not in kinds(r)  # the 6th is a later day, and later
    # With Q only until the 5th nothing is on a later local day.
    r, _ = judged([o for o in obs if o.shot.taken.date() < day(6)])
    assert kinds(r) == {"open"}


def test_what_was_earlier_is_before_what_has_the_earlier_date():
    obs = [ob(5, R, 10, zone=13, lon=178.0), ob(4, Q, 20, zone=-10, lon=-155.0)]
    assert [(r.region, r.first.day) for r in find_runs(obs)] == [(R, 5), (Q, 4)]


def test_a_flight_west_over_the_date_line():
    obs = [*stay(Q, 3, 4, zone=-10, lon=-155.0), *stay(R, 6, 7, zone=13, lon=178.0)]
    q, r = judged(obs)
    assert (q.region, r.region) == (Q, R)
    assert spans([q, r]) == [(3, 4), (6, 7)]
    assert "open" not in kinds(q) and "open" in kinds(r)


def test_a_later_local_day_that_is_not_later_is_no_finish():
    """Q at +13 on the 6th is before R at -10 on the 5th has ended."""
    obs = [ob(4, R, 9, zone=-10, lon=-155.0), ob(5, R, 18, zone=-10, lon=-155.0),
           ob(6, Q, 9, zone=13, lon=178.0)]
    assert obs[2].shot.taken_utc < obs[1].shot.taken_utc
    assert "open" in kinds(one(obs))


def test_a_day_seen_elsewhere_over_the_line_splits_a_run_and_never_swaps_its_ends():
    """R on the 3rd and 5th, and Q on its own 4th after that 5th: a barrier by the calendar."""
    obs = [ob(3, R, zone=13, lon=178.0), ob(5, R, 10, zone=13, lon=178.0),
           ob(4, Q, 20, zone=-10, lon=-155.0)]
    runs = find_runs(obs)
    assert spans(runs, R) == [(3, 3), (5, 5)]
    assert all(r.first <= r.last for r in runs)
    assert all("continuation" in kinds(r) for r in judged(obs) if r.region == R)


def test_two_clocks_a_day_apart_in_one_run_hold_it_for_good():
    """Both fit the place, because a day is taken off; one of them shows the wrong date."""
    obs = [ob(4, zone=-10, lon=178.0, hour=20), ob(6, zone=13, lon=178.0), *stay(Q, 8, 9)]
    run = one(obs)
    assert run.status == "fixed" and "clock" in kinds(run)
    obs = [ob(4, zone=8, lon=90.0, hour=23.9), ob(4, zone=2, lon=90.0, hour=23.9),
           *stay(Q, 8, 9)]
    assert "clock" not in kinds(one(obs))


# -- 4: finished --------------------------------------------------------------------


@pytest.mark.parametrize("after, finished", [
    ([ob(4, Q)], True),
    ([ob(90, Q)], True),
    ([], False),
    ([ob(3, Q, 23)], False),                         # later, and the same day
    ([ob(4, Q, state="edge")], False),
    ([ob(4, Q, kind="fast")], False),
    ([ob(4, Q, recorded=False)], False),
    ([ob(4, Q, origin="airdrop")], False),
    ([ob(9, R)], False),                             # the same region ends nothing
], ids=lambda v: str(v)[-40:])
def test_a_run_is_finished_by_a_later_proof_elsewhere(after, finished):
    run = next(r for r in judged([*stay(R, 1, 3), *after]) if r.region == R)
    assert ("open" in kinds(run)) is not finished


def test_the_finishing_photo_may_be_outside_the_dates_asked_for():
    run = one([*stay(R, 1, 3), ob(9, Q)], since=day(1), until=day(3))
    assert run.status == "offered"


def test_an_open_run_is_released_by_words():
    run = one(stay(R, 1, 3))
    assert run.status == "held" and kinds(run) == {"open"}


# -- 5: left -------------------------------------------------------------------------


def test_two_runs_with_nothing_seen_between_are_one_stay_perhaps():
    """A long hole, or a photo in the air: neither is offered, and no words release it."""
    for between in ([], [ob(5, kind="fast")], [ob(5, Q, state="edge")], [ob(5, use="none")],
                    [ob(5, Q, recorded=False)]):
        a, b = [r for r in judged([*stay(R, 1, 3), *between, *stay(R, 9, 11), *stay(Q, 12, 13)])
                if r.region == R]
        assert a.status == b.status == "fixed"
        assert kinds(a) == kinds(b) == {"continuation"}
        assert b.id in " ".join(a.reasons) and a.id in " ".join(b.reasons)


def test_a_proof_elsewhere_between_them_makes_two_stays():
    obs = [*stay(R, 1, 3), ob(5, Q, 9), ob(5, Q, 18), *stay(R, 9, 11), *stay(Q, 12, 13)]
    assert [r.status for r in judged(obs) if r.region == R] == ["offered", "offered"]


def test_lies_between_is_strict_at_the_last_and_the_first_photo():
    base = [ob(1, R, 9), ob(3, R, 18), ob(5, kind="fast"), ob(8, R, 9), ob(9, R, 18),
            *stay(S, 12, 13)]

    def two(*more):
        a, b = [r for r in judged([*base, *more]) if r.region == R]
        return "continuation" in kinds(a), "continuation" in kinds(b)

    assert two() == (True, True)
    assert two(ob(3, Q, 18)) == two(ob(8, Q, 9)) == two(ob(3, Q, 17)) == two(ob(8, Q, 10)) \
        == (True, True)
    assert two(ob(3, Q, 18.1)) == two(ob(8, Q, 8.9)) == (False, False)


def test_a_run_outside_the_dates_asked_for_still_counts():
    """The earlier stay is not on the list, and the later one may be the rest of it."""
    obs = [*stay(R, 1, 3), *stay(R, 20, 22), *stay(Q, 25, 26)]
    (a,) = [r for r in judged(obs, since=day(10)) if r.region == R]
    assert a.first == day(20) and kinds(a) == {"continuation"}
    obs = [*stay(R, 1, 3), ob(9, Q), *stay(R, 20, 22), *stay(Q, 25, 26)]
    (a,) = [r for r in judged(obs, since=day(10)) if r.region == R]
    assert a.status == "offered"


# -- 6: the visits that are there -----------------------------------------------------


def settled(*more):
    """R on 10-14, Q well before and after, so that only visits can hold it."""
    return [*stay(Q, 2, 4), *stay(R, 10, 14), *stay(Q, 20, 22), *more]


def held_by(v, obs=None):
    run = one(settled() if obs is None else obs, {R: v if isinstance(v, list) else [v]})
    return run.status, kinds(run)


def test_no_visit_at_all_is_offered():
    assert held_by([]) == ("offered", set())


def test_equal_full_dates_are_there():
    run = one(settled(), {R: [visit(10, 14, id=7123)]})
    assert run.status == "there" and "7123" in run.reasons[0]
    assert "2026-05-10" in run.reasons[0]


def test_there_wins_over_all():
    rows = [visit(None, None, id=1), visit(10, 14, id=2), visit(12, 20, id=3)]
    for v in (rows, rows[::-1]):
        assert held_by(v) == ("there", {"there"})
    assert one(stay(R, 10, 14), {R: [visit(10, 14)]}, since=day(12)).status == "there"


@pytest.mark.parametrize("a, b", [(10, 13), (11, 14), (14, 14), (10, 10), (1, 10), (14, 30),
                                  (1, 30), (12, 12), (10, 15), (9, 14)])
def test_a_shared_day_is_an_overlap(a, b):
    assert held_by(visit(a, b)) == ("fixed", {"overlap"})


def test_a_transit_and_a_trip_owned_visit_count_too():
    assert held_by(visit(12, 12, quality=1)) == ("fixed", {"overlap"})
    assert held_by(visit(10, 14, quality=1, trip_id=5))[0] == "there"
    assert held_by(visit(14, 16, trip_id=5)) == ("fixed", {"overlap"})


def test_a_visit_on_the_day_beside_is_a_continuation_and_not_a_second_visit():
    assert held_by(visit(8, 9)) == ("fixed", {"continuation"})
    assert held_by(visit(15, 16)) == ("fixed", {"continuation"})
    assert held_by(visit(6, 8)) == ("fixed", {"continuation"})  # Q was before it


def test_a_visit_left_in_between_is_another_visit():
    assert held_by(visit(1, 1)) == ("offered", set())
    assert held_by(visit(25, 26)) == ("offered", set())
    assert held_by([visit(1, 1, id=1), visit(8, 9, id=2)]) == ("fixed", {"continuation"})


@pytest.mark.parametrize("more, apart", [
    ((), False),
    ((ob(7, Q, 23.9),), False),       # the visit's last day says no time
    ((ob(8, Q, 1 + 59 / 60),), False),  # nor do two hours of the night after it
    ((ob(8, Q, 2),), True),
    ((ob(8, Q, 9),), True),
    ((ob(9, Q),), True),
    ((ob(10, Q, 8.9),), True),        # the run's own first day, before its first photo
    ((ob(10, Q, 9.0),), False), ((ob(10, Q, 9.1),), False),
    ((ob(9, Q, state="edge"),), False), ((ob(9, Q, kind="fast"),), False),
    ((ob(9, Q, recorded=False),), False), ((ob(9, R, state="edge"),), False),
], ids=lambda v: str(v)[-60:])
def test_between_a_visit_and_the_run_after_it(more, apart):
    want = ("offered", set()) if apart else ("fixed", {"continuation"})
    assert held_by(visit(5, 7), settled(*more)) == want


@pytest.mark.parametrize("more, apart", [
    ((), False),
    ((ob(17, Q, 0.1),), False),       # the visit's first day says no time
    ((ob(16, Q, 22 + 1 / 60),), False),  # nor do two hours of the night before it
    ((ob(16, Q, 22),), True),
    ((ob(16, Q, 9),), True),
    ((ob(15, Q),), True),
    ((ob(14, Q, 18.1),), True),
    ((ob(14, Q, 18.0),), False), ((ob(14, Q, 17.9),), False),
])
def test_between_the_run_and_a_visit_after_it(more, apart):
    want = ("offered", set()) if apart else ("fixed", {"continuation"})
    assert held_by(visit(17, 18), settled(*more)) == want


def test_the_visits_clock_need_not_be_the_runs():
    """Half past midnight where the run is may be the evening of the visit's last day."""
    home = {"zone": 1, "lon": 15.0}
    obs = [*stay(R, 10, 14, **home), *stay(Q, 20, 22, **home), ob(1, Q, **home)]
    assert held_by(visit(5, 7), [*obs, ob(8, Q, 0.5, **home)]) == ("fixed", {"continuation"})
    assert held_by(visit(5, 7), [*obs, ob(8, Q, 7, **home)]) == ("offered", set())
    assert held_by(visit(17, 18), [*obs, ob(16, Q, 23.5, **home)]) \
        == ("fixed", {"continuation"})
    assert held_by(visit(17, 18), [*obs, ob(16, Q, 20, **home)]) == ("offered", set())


def test_a_later_date_across_the_line_may_be_before_the_visit_ended():
    """Q at +13 on the 8th, at one in the morning, is R's 7th at -10 at two."""
    obs = [*stay(R, 10, 14, zone=-10, lon=-155.0), *stay(Q, 20, 22, zone=-10, lon=-155.0)]
    early = ob(8, Q, 1, zone=13, lon=178.0)
    late = ob(9, Q, 1.1, zone=13, lon=178.0)
    assert held_by(visit(5, 7), [*obs, early])[1] == {"continuation"}
    assert held_by(visit(5, 7), [*obs, late])[0] == "offered"
    # And after the run: Q on the 16th at -10 is R's 17th at +13 already.
    obs = [*stay(Q, 2, 4), *stay(R, 10, 14, zone=13, lon=178.0),
           *stay(Q, 20, 22, zone=13, lon=178.0)]
    assert held_by(visit(17, 18), [*obs, ob(16, Q, 9, zone=-10, lon=-155.0)])[1] \
        == {"continuation"}
    assert held_by(visit(17, 18), [*obs, ob(15, Q, 21.9, zone=-10, lon=-155.0)])[0] == "offered"


def test_a_photo_between_them_in_time_but_not_by_its_own_date_is_doubtful():
    """In every one of these the calendar of one side and the clock disagree."""
    east = {"zone": 13, "lon": 178.0}
    west = {"zone": -10, "lon": -155.0}

    def why(home, v, *photo):
        obs = [*stay(R, 10, 14, **home), *stay(Q, 30, 31, **home), ob(-20, Q, **home)]
        return held_by(v, [*obs, ob(*photo[:3], **photo[3])])[1]

    assert why(east, visit(5, 7), 7, Q, 9, west) == {"continuation"}    # still the visit's day
    assert why(west, visit(5, 7), 11, Q, 1, east) == {"continuation"}   # past the first day
    assert why(west, visit(17, 18), 17, Q, 9, east) == {"continuation"}  # the visit's day
    assert why(east, visit(17, 18), 13, Q, 20, west) == {"continuation"}  # before the last day
    assert why(east, visit(5, 7), 8, Q, 9, west) == why(west, visit(17, 18), 16, Q, 9, east) \
        == set()


@pytest.mark.parametrize("a, b", [
    (None, None), (2026, 2026), (2025, 2026), (2026, None), (None, 2026), (9, None),
    (None, 15), (2020, None), (None, 2030), (2026, 30), (2025, None),
], ids=str)
def test_a_visit_that_may_be_this_one_is_undated(a, b):
    """The region was visited, nobody wrote when, and this may be when."""
    assert held_by(visit(a, b)) == ("held", {"undated"})


def test_year_zero_is_undated_too():
    nothing = _date_parts({"year_from": 0, "month_from": None, "day_from": None}, "from")
    assert nothing is None
    assert held_by(visit(nothing, nothing)) == ("held", {"undated"})


def test_the_undated_reason_names_the_visit_and_what_it_says():
    (reason,) = one(settled(), {R: [visit(2026, None, id=7123)]}).reasons
    assert "7123" in reason and "2026" in reason and "no date" in reason


@pytest.mark.parametrize("a, b", [
    (2024, 2025), (None, 2025), (2027, None), (2024, 2024), (2027, 2027), (None, 1),
    (25, None), (2020, 1),
], ids=str)
def test_a_partly_dated_visit_that_cannot_be_this_one_is_judged_by_its_nearest_end(a, b):
    assert held_by(visit(a, b)) == ("offered", set())
    lone = [*stay(R, 10, 14), ob(40, Q)]
    if a in (25, 2027):
        lone = [ob(-40, Q), *stay(R, 10, 14)]
    run = one(lone, {R: [visit(a, b)]})
    assert run.status == "fixed" and "continuation" in kinds(run)


@pytest.mark.parametrize("a, b", [
    (12, None), (None, 12), (10, None), (None, 14), (14, None), (None, 10),
    (2026, 12), (12, 2027), (2025, 2027), (1, 2027), (2025, 10),
], ids=str)
def test_a_day_that_a_partly_dated_visit_does_say_is_an_overlap(a, b):
    assert held_by(visit(a, b)) == ("fixed", {"overlap"})


NEW_YEAR = [ob(dt.date(2026, 12, 1), Q), ob(dt.date(2026, 12, 30), R, 9),
            ob(dt.date(2026, 12, 31), R), ob(dt.date(2027, 1, 1), R),
            ob(dt.date(2027, 1, 2), R, 18), ob(dt.date(2027, 1, 9), Q)]


@pytest.mark.parametrize("a, b", [(2026, 2026), (2027, 2027), (2027, None), (None, 2026),
                                  (2025, 2026)], ids=str)
def test_a_year_that_may_hold_a_part_of_the_run_is_undated(a, b):
    """It may have been one of those days, and whoever left it undated may say so."""
    assert held_by(visit(a, b), NEW_YEAR) == ("held", {"undated"})


@pytest.mark.parametrize("year", [2026, 2027])
def test_one_day_of_the_run_in_that_year_is_enough(year):
    obs = [NEW_YEAR[0], *NEW_YEAR[2:4], NEW_YEAR[5]]  # the last of one and the first of the next
    assert held_by(visit(year, year), obs) == ("held", {"undated"})


@pytest.mark.parametrize("a, b", [(2025, 2025), (2028, 2028), (2028, None), (None, 2025)],
                         ids=str)
def test_a_year_with_no_day_of_the_run_is_judged_by_what_was_seen_between(a, b):
    assert held_by(visit(a, b), NEW_YEAR) == ("offered", set())
    rest = NEW_YEAR[:5] if a == 2028 else NEW_YEAR[1:]
    assert "continuation" in held_by(visit(a, b), rest)[1]


@pytest.mark.parametrize("a, b, want", [
    (2026, 9, "offered"), (2026, 10, "overlap"), (14, 2026, "overlap"), (15, 2026, "offered"),
    (None, 9, "offered"), (15, None, "offered"),
], ids=str)
def test_the_day_beside_the_run_is_not_a_day_of_it(a, b, want):
    obs = settled(ob(10, Q, 5), ob(14, Q, 20))
    assert held_by(visit(a, b), obs) == (
        ("offered", set()) if want == "offered" else ("fixed", {want}))


@pytest.mark.parametrize("a, b", [(14, 10), (30, 1), (2027, 2025), (12, 2025), (2026, 2025)],
                         ids=str)
def test_a_visit_whose_ends_are_the_wrong_way_round_is_never_nothing(a, b):
    want = ("held", {"undated"}) if a == 2026 else ("fixed", {"overlap"})
    assert held_by(visit(a, b)) == want


def test_a_run_can_have_several_reasons_and_the_worst_one_counts():
    run = one(stay(R, 10, 14), {R: [visit(None, None, id=1), visit(14, 16, id=2)]})
    assert kinds(run) == {"undated", "overlap", "open"} and run.status == "fixed"
    run = one(stay(R, 10, 14), {R: [visit(None, None, id=1)]})
    assert kinds(run) == {"undated", "open"} and run.status == "held"


def test_a_region_whose_visits_were_not_read_is_not_one_without_visits():
    obs = [*stay(R, 1, 3), *stay(Q, 5, 8)]
    with pytest.raises(PlanInvalid, match="2"):
        judge(find_runs(obs), obs, {R: []}, since=None, until=None)
    # A run that is not on the list needs none.
    assert len(judge(find_runs(obs), obs, {R: []}, since=None, until=day(3))) == 1


def test_which_regions_to_read():
    obs = [*stay(Q, 1, 2), ob(5, R), ob(9, R), *stay(S, 20, 22)]
    ask = {"since": day(7), "until": day(8)}
    assert regions_to_read(obs, **ask, gaps=(1, 2)) == set()
    assert regions_to_read(obs, **ask, gaps=(1, 2, 3, 5)) == {R}
    assert regions_to_read(obs, since=None, until=day(3)) == {Q}
    assert regions_to_read(obs, since=day(5), until=None, track=[(day(6), Q)]) == {R, S}
    assert regions_to_read(obs, **ask, gaps=(5,), track=[(day(6), Q)]) == set()


# -- 7: held ---------------------------------------------------------------------------


@pytest.mark.parametrize("obs, brief", [
    ([ob(10)], True),
    ([ob(10, hour=9), ob(10, hour=9.25)], True),
    ([ob(10, hour=9), ob(10, hour=9.26)], False),
    ([ob(10, lat=1.23456, lon=1.0), ob(14, lat=1.23456, lon=1.0)], True),
    ([ob(10, lat=1.23456, lon=1.0), ob(14, lat=1.23456, lon=1.00001)], False),
    ([ob(10, hour=9), ob(10, hour=12, state="edge")], True),
    ([ob(10, hour=9), ob(10, hour=12, kind="fast")], True),
], ids=range(7))
def test_brief(obs, brief):
    between = [ob(d, state="edge") for d in (11, 12, 13)] if obs[-1].shot.taken.day == 14 else []
    run = one([*stay(Q, 2, 4), *obs, *between, *stay(Q, 20, 22)])
    assert ("brief" in kinds(run)) is brief
    assert run.status == ("held" if brief else "offered")


@pytest.mark.parametrize("hours, before, after, layover", [
    (10, 6, 6, True), (23.9, 6, 6, True), (24, 6, 6, False), (10, 6.1, 6, False),
    (10, 6, 6.1, False), (10, None, 6, False), (10, 6, None, False), (10, 1, 1, True),
])
def test_layover(hours, before, after, layover):
    obs = [*stay(Q, 1, 2), ob(10, hour=10), ob(10, hour=10 + hours), *stay(Q, 20, 22)]
    if before is not None:
        obs.append(ob(10, Q, 10 - before, kind="fast", state="nowhere"))
    if after is not None:
        obs.append(ob(10, Q, 10 + hours + after, kind="fast", state="inside"))
    run = one(obs)
    assert ("layover?" in kinds(run)) is layover
    assert run.status == ("held" if layover else "offered")


@pytest.mark.parametrize("state, kind", [("unread", "not read"), ("stale", "stale map")])
@pytest.mark.parametrize("at, held", [(8, False), (9, True), (10, True), (12, True), (14, True),
                                      (15, True), (16, False)])
def test_an_unread_or_stale_photo_on_or_beside_its_days_holds_it(state, kind, at, held):
    run = one(settled(ob(at, state=state, hour=13)))
    assert (kinds(run), run.status) == (({kind}, "fixed") if held else (set(), "offered"))


def test_an_unread_day_between_two_runs_holds_both():
    obs = [*stay(R, 1, 3), ob(4, state="unread"), *stay(R, 5, 7), *stay(Q, 9, 10)]
    assert [(spans([r])[0], "not read" in kinds(r)) for r in judged(obs) if r.region == R] \
        == [((1, 3), True), ((5, 7), True)]


@pytest.mark.parametrize("since, until, want", [
    (10, 14, "offered"), (None, None, "offered"), (11, 14, "fixed"), (14, 14, "fixed"),
    (10, 13, "fixed"), (10, 10, "fixed"), (9, 15, "offered"), (15, 30, None), (1, 9, None),
])
def test_a_run_across_since_or_until_is_held(since, until, want):
    ask = {"since": since and day(since), "until": until and day(until)}
    runs = [r for r in judged(settled(), **ask) if r.region == R]
    assert [r.status for r in runs] == ([want] if want else [])
    if want == "fixed":
        assert kinds(runs[0]) == {"across"}


# -- 7b: flags hold nothing --------------------------------------------------------------


def test_flags_hold_nothing():
    run = one(settled(ob(12, kind="conflict", hour=13), ob(12, kind="fast", hour=14)),
              track=[(day(12), S)])
    assert flags(run) == {"conflict", "fast near", "track", "evidence thin"}
    assert run.status == "offered"


def test_conflict_is_of_its_days_and_its_region():
    assert "conflict" in flags(one(settled(ob(14, kind="conflict", state="nearest"))))
    assert "conflict" not in flags(one(settled(ob(14, Q, kind="conflict"))))
    assert "conflict" not in flags(one(settled(ob(15, R, kind="conflict"))))


def test_fast_near_is_within_six_hours():
    for d, hour, near in ((10, 3, True), (10, 2.9, False), (14, 24, True), (15, 0.1, False)):
        run = one(settled(ob(d, hour=hour, kind="fast", state="nowhere")))
        assert ("fast near" in flags(run)) is near, (d, hour)


def test_a_track_row_where_no_photo_is_that_day():
    assert "track" in flags(one(settled(), track=[(day(12), Q)]))
    assert "track" not in flags(one(settled(), track=[(day(12), R), (day(15), S), (day(9), S)]))
    assert "track" not in flags(one(settled(ob(12, Q, state="edge")), track=[(day(12), Q)]))
    assert "track" in flags(one(settled(), track=[(day(12), None)]))
    at_sea = ob(12, state="nowhere", kind="fast")
    assert "track" in flags(one(settled(at_sea), track=[(day(12), None)]))
    obs = [*stay(Q, 1, 2), ob(10), ob(12), *stay(Q, 20, 22)]
    assert "track" not in flags(one(obs, track=[(day(11), R)]))  # nothing against a silent day


def asset(o, **kw):
    s = o.shot
    return PhotoAsset(s.uuid, s.taken, s.lat, s.lon, "0", "x.heic", taken_utc=s.taken_utc,
                      zone=s.zone, importer="com.apple.camera", **kw)


def test_evidence_is_graded_as_the_dossier_grades_it():
    obs = [*stay(Q, 1, 2), *[ob(10 + i, lat=1 + i / 10, lon=1.0) for i in range(4)],
           *stay(Q, 20, 22)]
    assets = {o.shot.uuid: asset(o) for o in obs}
    run = one(obs, assets=assets)
    assert "evidence thin" not in flags(run) and evidence_grade(run, assets) == "serial"
    selfies = {o.shot.uuid: asset(o, selfie=True) for o in obs}
    assert evidence_grade(run, selfies) == "strong"
    assert "evidence thin" in flags(one(obs)) and evidence_grade(run, {}) == "none"
    near = [*stay(Q, 1, 2), ob(10, lat=1.0, lon=1.0), ob(11, lat=1.0001, lon=1.0),
            ob(12, lat=1.0002, lon=1.0), *stay(Q, 20, 22)]
    assets = {o.shot.uuid: asset(o) for o in near}
    assert "evidence thin" in flags(one(near, assets=assets))


# -- 8: boundaries -----------------------------------------------------------------------


def cuts(obs, visits=None, gap_days=2, **kw):
    runs = judged(obs, visits, gap_days=gap_days, **kw)
    return runs, boundaries(runs, gap_days=gap_days, obs=obs)


def suggested(b):
    return {kind_of(s) for s in b.suggestions}


def test_neighbours_with_nothing_between_stand_at_the_same_trip():
    obs = [*stay(R, 1, 3), ob(3, Q, 22), *stay(Q, 4, 5), *stay(S, 6, 8)]
    runs, bs = cuts(obs)
    assert [(b.before, b.after) for b in bs] == [(runs[0].id, runs[1].id),
                                                 (runs[1].id, runs[2].id)]
    assert all(b.suggestions == () and not b.forced for b in bs)
    assert bs[0].id == f"{runs[0].id}|{runs[1].id}"


@pytest.mark.parametrize("gap_days, first, gap", [
    (2, 4, False), (2, 5, False), (2, 6, True), (1, 4, False), (1, 5, True), (5, 8, False),
    (5, 9, True),
])
def test_a_gap_between_two_runs_is_a_suggestion(gap_days, first, gap):
    _, (b,) = cuts([*stay(R, 1, 3), *stay(Q, first, first + 2)], gap_days=gap_days)
    assert suggested(b) == ({"gap"} if gap else set()) and not b.forced


def test_a_fast_photo_between_two_runs_is_a_suggestion():
    base = [*stay(R, 1, 3), *stay(Q, 4, 5)]
    for kind in ("fast", "conflict"):
        _, (b,) = cuts([*base, ob(3, S, 22, kind=kind, state="nowhere")])
        assert suggested(b) == {"fast"}
    for outside in (ob(3, hour=17, kind="fast"), ob(4, hour=9.5, kind="fast")):
        _, (b,) = cuts([*base, outside])
        assert suggested(b) == set()


def test_a_fast_leg_between_two_runs_is_a_suggestion():
    obs = [ob(1, R, 9, lat=0, lon=0), ob(1, R, 12, lat=0.1, lon=0),
           ob(1, Q, 14, lat=3.0, lon=0), ob(2, Q, 14, lat=3.1, lon=0)]
    _, (b,) = cuts(obs)
    assert suggested(b) == {"fast"}
    obs[2] = ob(1, Q, 15, lat=3.0, lon=0)
    _, (b,) = cuts(obs)
    assert suggested(b) == set()


def test_a_day_trip_inside_a_stay_is_between_the_two_photos_around_it():
    """A long stay with a day trip in it: a fast photo a week later is not between them."""
    obs = [*stay(R, 1, 3), ob(4, R, 8), ob(4, Q, 12), ob(4, Q, 13), ob(4, R, 18),
           *stay(R, 5, 9), ob(8, R, 13, kind="fast")]
    runs, (b,) = cuts(obs)
    assert [r.region for r in runs] == [R, Q] and suggested(b) == set()


def test_forced_cuts_around_a_run_that_is_there_and_an_overlap():
    obs = [*stay(R, 1, 3), *stay(Q, 4, 6), *stay(S, 7, 9), *stay(4, 10, 12)]
    _, bs = cuts(obs, {Q: [visit(4, 6, Q)]})
    assert [b.forced for b in bs] == [True, True, False]
    assert [suggested(b) for b in bs] == [{"profile"}, {"profile"}, set()]
    _, bs = cuts(obs, {Q: [visit(6, 8, Q)]})
    assert [b.forced for b in bs] == [True, True, False]
    _, bs = cuts(obs, {Q: [visit(None, None, Q)]})
    assert [b.forced for b in bs] == [False, False, False]
    _, bs = cuts(obs, {Q: [visit(3, 3, Q)]})  # a continuation
    assert [b.forced for b in bs] == [False, False, False]


# -- 8-10: trips ------------------------------------------------------------------------

JOURNEY = [*stay(R, 1, 3), ob(3, Q, 22), *stay(Q, 4, 5), *stay(S, 6, 8), *stay(4, 9, 10),
           *stay(9, 13, 14), ob(40, 8)]


def made(obs, answers, cut=None, visits=None, until=30, words=None):
    """`answers` and `words` by region, `cut` by the pair of regions."""
    runs, bs = cuts(obs, visits, until=day(until))
    of = {r.id: r for r in runs}
    ans = {}
    for r in runs:
        row = {}
        if r.region in answers:
            row["a"] = answers[r.region]
        if words and r.region in words:
            row["words"] = words[r.region]
        if row:
            ans[r.id] = row
    cut = {b.id: cut[(of[b.before].region, of[b.after].region)] for b in bs
           if cut and (of[b.before].region, of[b.after].region) in cut}
    return group(runs, bs, {"runs": ans, "cuts": cut})


def regions(drafts):
    return [[row.run.region for row in d.rows] for d in drafts]


ALL = {R: "3", Q: "3", S: "2", 4: "1", 9: "3"}


def test_one_trip_with_its_rows_and_header():
    (d,), not_made = made(JOURNEY, ALL, {(4, 9): "same"})
    assert [(row.run.region, row.quality, row.words) for row in d.rows] == [
        (R, 3, ""), (Q, 3, ""), (S, 2, ""), (4, 1, ""), (9, 3, "")]
    assert (d.date_from, d.date_to) == (day(1), day(14)) and not_made == []


def test_a_cut_makes_two_trips():
    drafts, not_made = made(JOURNEY, ALL, {(Q, S): "cut", (4, 9): "cut"})
    assert regions(drafts) == [[R, Q], [S, 4], [9]] and not_made == []
    assert [(d.date_from.day, d.date_to.day) for d in drafts] == [(1, 5), (6, 10), (13, 14)]


def test_a_suggested_cut_that_is_not_answered_holds_both_sides():
    drafts, (n,) = made(JOURNEY, ALL)
    assert drafts == [] and len(n.runs) == 5 and "9:2026-05-13" in " ".join(n.reasons)
    drafts, (n,) = made(JOURNEY, ALL, {(Q, S): "cut"})
    assert regions(drafts) == [[R, Q]] and len(n.runs) == 3


@pytest.mark.parametrize("answer", [None, "?"])
def test_a_blank_and_a_question_mark_hold_their_trip(answer):
    answers = {k: v for k, v in ALL.items() if k != S} | ({S: answer} if answer else {})
    drafts, (n,) = made(JOURNEY, answers, {(4, 9): "cut"})
    assert regions(drafts) == [[9]] and len(n.runs) == 4
    assert "3:2026-05-06:2026-05-08" in " ".join(n.reasons)


def test_no_in_the_middle_is_left_out_and_the_rest_is_the_trip():
    (d,), not_made = made(JOURNEY, ALL | {S: "no"}, {(4, 9): "same"})
    assert regions([d]) == [[R, Q, 4, 9]] and not_made == []
    (d,), _ = made(JOURNEY, ALL | {R: "no", 9: "no"}, {(4, 9): "same"})
    assert (d.date_from, d.date_to) == (day(3), day(10))  # only the rows that are in it


def test_a_cut_beside_a_run_that_is_left_out_still_cuts():
    drafts, _ = made(JOURNEY, ALL | {S: "no"}, {(Q, S): "cut", (4, 9): "same"})
    assert regions(drafts) == [[R, Q], [4, 9]]


def test_all_no_is_no_trip():
    assert made(JOURNEY, dict.fromkeys(ALL, "no"), {(4, 9): "same"}) == ([], [])


def test_a_run_that_is_there_cuts_and_is_in_nothing():
    drafts, not_made = made(JOURNEY, {k: v for k, v in ALL.items() if k != S},
                            {(Q, S): "same", (S, 4): "same", (4, 9): "same"},
                            {S: [visit(6, 8, S)]})
    assert regions(drafts) == [[R, Q], [4, 9]] and not_made == []


@pytest.mark.parametrize("answer", ["no", None, "?"])
def test_an_overlap_cuts_whatever_was_answered(answer):
    answers = {k: v for k, v in ALL.items() if k != S} | ({S: answer} if answer else {})
    drafts, not_made = made(JOURNEY, answers,
                            {(Q, S): "same", (S, 4): "same", (4, 9): "same"},
                            {S: [visit(8, 10, S)]})
    assert regions(drafts) == [[R, Q], [4, 9]]
    assert [len(n.runs) for n in not_made] == ([] if answer == "no" else [1])


FIXED = {
    "continuation": (JOURNEY, {S: [visit(5, 5, S)]}),
    "not read": ([*JOURNEY, ob(7, state="unread", hour=13)], {}),
    "stale map": ([*JOURNEY, ob(7, state="stale", hour=13)], {}),
}


@pytest.mark.parametrize("kind", FIXED)
def test_the_other_fixed_runs_left_out_do_not_cut(kind):
    obs, visits = FIXED[kind]
    rest = {k: v for k, v in ALL.items() if k != S}
    drafts, not_made = made(obs, rest | {S: "no"}, {(4, 9): "same"}, visits)
    assert regions(drafts) == [[R, Q, 4, 9]] and not_made == []
    drafts, (n,) = made(obs, rest, {(4, 9): "same"}, visits)
    assert drafts == [] and len(n.runs) == 5


def test_a_run_across_until_left_out_does_not_cut():
    obs = [*stay(R, 1, 3), *stay(Q, 4, 5), *stay(S, 6, 8), ob(40, 8)]
    drafts, not_made = made(obs, {R: "3", Q: "3", S: "no"}, until=7)
    assert regions(drafts) == [[R, Q]] and not_made == []
    drafts, (n,) = made(obs, {R: "3", Q: "3"}, until=7)
    assert drafts == [] and len(n.runs) == 3


def test_a_held_run_takes_words_with_its_quality():
    visits = {Q: [visit(None, None, Q)]}
    same = {(4, 9): "same"}
    for words in (None, {Q: ""}, {Q: " \n\t"}, {Q: "‎"}):
        drafts, (n,) = made(JOURNEY, ALL, same, visits, words=words)
        assert drafts == [] and "words" in " ".join(n.reasons)
    (d,), _ = made(JOURNEY, ALL, same, visits, words={Q: " the other one was as a child "})
    assert d.rows[1].words == " the other one was as a child "  # verbatim
    (d,), _ = made(JOURNEY, ALL | {Q: "no"}, same, visits)
    assert regions([d]) == [[R, S, 4, 9]]


OPEN = [*stay(R, 1, 3), *stay(Q, 4, 6)]  # Q is the last thing there is


def test_an_open_run_with_no_words_never_joins_the_trip_before_it():
    drafts, (n,) = made(OPEN, {R: "3", Q: "3"}, {(R, Q): "same"})
    assert regions(drafts) == [[R]] and n.runs == ("2:2026-05-04:2026-05-06",)
    drafts, (n,) = made(OPEN, {R: "3"})
    assert regions(drafts) == [[R]] and len(n.runs) == 1
    drafts, not_made = made(OPEN, {R: "3", Q: "no"})
    assert regions(drafts) == [[R]] and not_made == []


def test_an_open_run_with_words_stays_where_it_is():
    (d,), not_made = made(OPEN, {R: "3", Q: "3"}, words={Q: "I flew home on the 7th"})
    assert regions([d]) == [[R, Q]] and not_made == []
    drafts, _ = made(OPEN, {R: "3", Q: "3"}, {(R, Q): "cut"}, words={Q: "home on the 7th"})
    assert regions(drafts) == [[R], [Q]]


def test_what_is_after_an_open_run_with_no_words_is_held_with_it():
    """Both end on one day, so both are open; the first is not the end of the second."""
    obs = [*stay(R, 1, 3), *stay(Q, 4, 6), ob(6, S, 20), ob(6, S, 21)]
    drafts, (n,) = made(obs, {R: "3", Q: "3", S: "3"}, words={S: "home the next day"})
    assert regions(drafts) == [[R]] and len(n.runs) == 2


def test_a_region_twice_in_one_trip_is_not_planned():
    obs = [*stay(R, 1, 3), *stay(Q, 4, 6), *stay(R, 7, 9), ob(40, S)]
    drafts, (n,) = made(obs, {R: "3", Q: "3"})
    assert drafts == []
    text = " ".join(n.reasons)
    assert "1:2026-05-01:2026-05-03" in text and "1:2026-05-07:2026-05-09" in text
    drafts, not_made = made(obs, {R: "3", Q: "3"}, {(Q, R): "cut"})
    assert regions(drafts) == [[R, Q], [R]] and not_made == []


def test_a_region_twice_with_one_of_them_left_out_is_once():
    obs = [*stay(R, 1, 3), *stay(Q, 4, 6), *stay(R, 7, 9), ob(40, S)]
    runs, bs = cuts(obs, until=day(30))
    answers = {runs[0].id: {"a": "3"}, runs[1].id: {"a": "3"}, runs[2].id: {"a": "no"}}
    (d,), not_made = group(runs, bs, {"runs": answers, "cuts": {}})
    assert regions([d]) == [[R, Q]] and not_made == []


@pytest.mark.parametrize("runs, cut", [
    ({"9:2026-05-01:2026-05-03": {"a": "3"}}, {}),
    ({"1:2026-05-01:2026-05-03": {"a": "0"}}, {}),
    ({"1:2026-05-01:2026-05-03": {"a": "7"}}, {}),
    ({"1:2026-05-01:2026-05-03": {"a": 3}}, {}),
    ({"1:2026-05-01:2026-05-03": {"a": "yes"}}, {}),
    ({"1:2026-05-01:2026-05-03": {"a": "3", "words": "x"}}, {}),
    ({"2:2026-05-04:2026-05-06": {"a": "3", "words": 5}}, {}),
    ({}, {"a|b": "cut"}),
    ({}, {"1:2026-05-01:2026-05-03|2:2026-05-04:2026-05-06": "join"}),
], ids=range(9))
def test_an_answer_the_page_cannot_give_is_refused(runs, cut):
    rs, bs = cuts(OPEN)
    with pytest.raises(PlanInvalid):
        group(rs, bs, {"runs": runs, "cuts": cut})


def test_a_quality_where_only_no_is_taken_is_refused():
    for visits in ({S: [visit(8, 10, S)]}, {S: [visit(6, 8, S)]}, {S: [visit(5, 5, S)]}):
        with pytest.raises(PlanInvalid):
            made(JOURNEY, ALL, {(4, 9): "same"}, visits)
        with pytest.raises(PlanInvalid):
            made(JOURNEY, {}, {}, visits, words={S: "but I was"})


def test_boundaries_of_other_runs_are_refused():
    runs, bs = cuts(JOURNEY)
    for wrong in (bs[1:], [], bs[::-1], [Boundary("a", "b", (), False), *bs[1:]]):
        with pytest.raises(PlanInvalid):
            group(runs, wrong, {"runs": {}, "cuts": {}})
    assert group(runs[::-1], bs, {"runs": {}, "cuts": {}})[0] == []
    assert group([], [], {"runs": {}, "cuts": {}}) == ([], [])


# -- 13: the sweep ------------------------------------------------------------------------


def test_the_sweep_is_the_same_algorithm_at_each_gap():
    obs = [*stay(R, 1, 2), *stay(R, 5, 6), *stay(Q, 10, 12), *stay(S, 15, 16), ob(40, 8)]
    visits = {r: [] for r in (R, Q, S, 8)} | {S: [visit(15, 16, S)]}
    rows = sweep(obs, visits, since=day(1), until=day(30))
    assert [r.gap_days for r in rows] == [1, 2, 3, 5]
    assert [r.runs for r in rows] == [4, 4, 3, 3]
    assert [r.held for r in rows] == [2, 2, 0, 0]  # the two halves of R
    assert [r.suggested for r in rows] == [2, 2, 1, 0]  # beside what is there it is forced
    with pytest.raises(PlanInvalid):
        sweep(obs, {}, since=day(1), until=day(30))


@pytest.mark.parametrize("fast_kmh, fast", [(None, False), (30.0, False), (10.0, True)])
def test_the_sweep_suggests_the_cuts_that_boundaries_suggests(fast_kmh, fast):
    """Over 33 km between the two of them, in the last two hours and a half."""
    obs = [ob(1, R, 9, lat=0.0), ob(2, R, 9, lat=0.1), ob(2, Q, 11.5, lat=0.4),
           ob(3, Q, 9, lat=0.5), ob(40, 8)]
    (row,) = sweep(obs, {R: [], Q: [], 8: []}, since=day(1), until=day(30), gaps=(2,),
                   fast_kmh=fast_kmh)
    bs = boundaries(judged(obs, until=day(30)), gap_days=2, obs=obs, fast_kmh=fast_kmh)
    assert row.suggested == sum(bool(b.suggestions) for b in bs) == fast


# -- and the module ------------------------------------------------------------------------


def test_fifty_thousand_photos_are_not_a_square_of_anything():
    obs, d = [], dt.date(2012, 1, 1)
    for n in range(50_000):
        obs.append(ob(d + dt.timedelta(days=n // 10), 1 + n // 10 // 5 % 40, 8 + n % 10))
    for n in range(0, 5000, 50):
        obs.append(ob(d + dt.timedelta(days=n), kind="fast", state="nowhere", hour=23))
    visits = {r: [visit(dt.date(2000, 1, 1), dt.date(2000, 1, 1 + i), r, id=i)
                  for i in range(20)] for r in range(1, 41)}
    start = time.perf_counter()
    runs = judge(find_runs(obs), obs, visits, since=None, until=None)
    group(runs, boundaries(runs, gap_days=2, obs=obs), {"runs": {}, "cuts": {}})
    assert len(runs) >= 1000 and time.perf_counter() - start < 10


def test_nothing_here_speaks_to_the_server_or_to_a_clock():
    tree = ast.parse(Path(trips.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"NomadMania", "Transport", "today", "now", "open", "urlopen"}


def test_the_shape_of_a_run():
    assert list(Run.__dataclass_fields__) == [
        "region", "first", "last", "days", "silent", "shots", "reasons", "flags", "status"]


# -- the list: what the owner was shown -----------------------------------------------------

HOSTILE = ("</script><script>boom()</script><!--\"'&amp;"
           "  ‮ 東京 \U0001f5fc $& \\u003c")
NAMES = {R: "Aldmoor", Q: "Berrow", S: "Cray Islands", 4: "Dunvale", 8: "Elstow", 9: "Fenmark"}
PARAMS = {"since": "2026-04-20", "until": "2026-05-30", "gap_days": 2,
          "speeds": default_speeds(), "spans": False}
PHOTOS = {"kind": "photos", "label": "Photos library", "sha256": None, "first": "2019-03-02",
          "last": "2026-06-20", "count": 4321}
TRACK = {"kind": "track", "label": "days.csv", "sha256": "a" * 64, "first": "2012-01-01",
         "last": "2018-12-30", "count": 900}
SPANS = {"kind": "spans", "label": "spans.json", "sha256": "b" * 64, "first": "2020-01-01",
         "last": "2020-01-09", "count": 2}
COVERAGE = {"tiles_read": 12, "tiles_unread": 0, "unread_photos": 0}
LEFT_OUT = {name: n for n, name in enumerate((
    "other_origin", "no_utc", "span_none", "conflict", "fast", "span_airports", "unread",
    "stale", "nowhere", "nearest", "edge", "clock"), 2)}
SWEEP = [SweepRow(1, 6, 2, 1), SweepRow(2, 5, 1, 2)]
TRIP = {"id": 551, "date_from": "2026-05-03", "date_to": "2026-05-04", "description": "hills"}


def listed(obs=JOURNEY, visits=None, *, until=30, **kw):
    runs, bs = cuts(obs, visits, until=day(until))
    return build_list(runs, bs, **{
        "account": 1, "created": "2026-06-01T10:00:00+00:00", "names": NAMES,
        "visits": {r.region: [] for r in runs} | (visits or {}), "trips": [], "params": PARAMS,
        "sources": [PHOTOS], "no_track": True, "coverage": COVERAGE, "left_out": LEFT_OUT,
        "sweep": SWEEP, "assets": {}} | kw)


UNDATED_Q = {Q: [visit(None, None, Q, id=72)]}
# Offered, held, fixed, offered, offered; with one boundary that has to be answered.
MIXED = UNDATED_Q | {S: [visit(5, 5, S, id=73, quality=1, trip_id=552)]}
# And two that are on the profile: S in part, 9 to the day.
KNOWN = UNDATED_Q | {S: [visit(8, 10, S, id=74)], 9: [visit(13, 14, 9, id=75)]}


def small(**kw):
    return listed([*stay(R, 1, 3), ob(3, Q, 22), *stay(Q, 4, 5), ob(40, 8)],
                  **{"visits": UNDATED_Q, "trips": [TRIP]} | kw)


def test_a_list_comes_back_from_disk_as_it_was(tmp_path):
    for n, m in enumerate((small(), listed(visits=MIXED), listed(visits=KNOWN), listed([]),
                           small(sources=[PHOTOS, TRACK, SPANS], no_track=False,
                                 params=PARAMS | {"spans": True, "until": None}))):
        back = TripList.load(m.save(tmp_path / f"{n}.json"))
        assert back == m and back.sha256 == m.sha256
        assert all(type(r) is ListedRun and type(r.first) is dt.date for r in back.runs)
        assert all(type(b) is Boundary for b in back.boundaries)


def test_the_hash_of_a_trip_list_is_what_it_was():
    """Answers on disk name it. Another hash for the same list is another list to them."""
    assert small().sha256 == "3084d90c53c6b41e161242da89ed02baf38032846d75dc56decfa006e45c476a"


def test_what_the_list_says_of_a_run():
    obs = [ob(1, hour=9, zone=2, uuid="SECRET-1"), ob(3, hour=20.5, zone=3, uuid="SECRET-2"),
           *stay(Q, 5, 6, zone=2)]
    m = listed(obs)
    r = m.runs[0]
    assert r.id == "1:2026-05-01:2026-05-03" and r.name == "Aldmoor"
    assert (r.days, r.silent, r.photos) == ((day(1), day(3)), (day(2),), 2)
    assert r.first_photo == PhotoTime(dt.datetime(2026, 5, 1, 9), 7200)
    assert r.last_photo == PhotoTime(dt.datetime(2026, 5, 3, 20, 30), 10800)
    assert r.start == dt.datetime(2026, 5, 1, 7, tzinfo=UTC)
    assert r.end == dt.datetime(2026, 5, 3, 17, 30, tzinfo=UTC)
    assert (r.evidence, r.flags, r.status) == ("none", ("evidence thin",), "offered")
    assert m.runs[1].status == "held" and kinds(m.runs[1]) == {"open"}
    assert m.boundaries == cuts(obs)[1]


def test_the_list_has_no_coordinates_and_no_photos(tmp_path):
    obs = [ob(1, hour=9, lat=12.34567, lon=1.0, uuid="SECRET-1"), ob(3, lat=12.5, lon=1.0),
           *stay(Q, 5, 6)]
    m = listed(obs)
    for text in (m.to_json(), render_page(m)):
        assert "12.345" not in text and "SECRET" not in text
    assert set(m._stored()["runs"][0]) == {
        "region", "name", "first", "last", "days", "silent", "photos", "first_photo",
        "last_photo", "start", "end", "evidence", "flags", "reasons", "status"}


def test_the_list_grades_the_photos_as_the_dossier_does():
    obs = [*stay(Q, 1, 2), *[ob(10 + i, lat=1 + i / 10, lon=1.0) for i in range(4)],
           *stay(Q, 20, 22)]
    assets = {o.shot.uuid: asset(o) for o in obs}
    runs, bs = cuts(obs, assets=assets)
    m = build_list(runs, bs, account=1, created="", names=NAMES, visits={R: [], Q: []}, trips=[],
                   params=PARAMS, sources=[PHOTOS], no_track=True, coverage=COVERAGE,
                   left_out=LEFT_OUT, sweep=SWEEP, assets=assets)
    assert [r.evidence for r in m.runs if r.region == R] == ["serial"]
    with pytest.raises(PlanInvalid, match="evidence thin"):  # two gradings, one list
        listed(obs, assets=assets)


def test_the_hash_is_of_the_canonical_json_of_the_list_but_created(tmp_path):
    m = small(names=NAMES | {R: "東"})
    stored = json.loads(m.save(tmp_path / "m.json").read_text(encoding="utf-8"))
    said = stored.pop("sha256")
    del stored["created"]
    assert "東" in canonical(stored)
    assert said == m.sha256 == hashlib.sha256(canonical(stored).encode("utf-8")).hexdigest()
    assert small(created="another day").sha256 == small().sha256


def test_the_hash_does_not_depend_on_an_order_that_means_nothing(tmp_path):
    two = [TRIP, TRIP | {"id": 400}]
    other = TRACK | {"label": "a.csv"}
    m = small(sources=[TRACK, PHOTOS, other], no_track=False, trips=two,
              visits={Q: [visit(None, None, Q, id=n) for n in (3, 1, 2)]})
    assert dataclasses.replace(m, sources=[other, TRACK, PHOTOS], trips=two[::-1], visits={
        "2": m.visits["2"][::-1], "1": []}) == m
    assert small(params=dict(reversed(PARAMS.items()))).sha256 == small().sha256
    assert [v["id"] for v in m.visits["2"]] == [1, 2, 3]
    assert [t["id"] for t in m.trips] == [400, 551]
    raw = json.loads(m.to_json())
    path = tmp_path / "n.json"
    path.write_text(json.dumps(dict(reversed(raw.items()))), encoding="utf-8")
    assert TripList.load(path) == m


GONE = object()


def put(raw, path, value):
    """A copy of `raw` with the value at `path`, `a.0.b`, another one or not there."""
    raw = json.loads(json.dumps(raw))
    at, (*way, last) = raw, path.split(".")
    for k in way:
        at = at[int(k) if isinstance(at, list) else k]
    last = int(last) if isinstance(at, list) else last
    if value is GONE:
        del at[last]
    else:
        at[last] = value
    return raw


def rehashed(raw):
    """The file as somebody who can count sha256 would have altered it."""
    content = {k: v for k, v in raw.items() if k not in ("sha256", "created")}
    return raw | {"sha256": hashlib.sha256(json.dumps(
        content, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()}


def reload(tmp_path, raw):
    path = tmp_path / "m.json"
    path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    return TripList.load(path)


GOOD_CHANGES = [
    ("account", 2), ("no_track", GONE), ("params.since", "2026-04-21"), ("params.until", None),
    ("params.gap_days", 3), ("params.speeds.fast_kmh", 151.0), ("sources.0.label", "another"),
    ("sources.0.sha256", "c" * 64), ("sources.0.first", "2019-03-03"), ("sources.0.count", 1),
    ("coverage.tiles_read", 13), ("coverage.tiles_unread", 1), ("coverage.unread_photos", 1),
    *((f"left_out.{k}", 99) for k in LEFT_OUT), ("sweep.0.runs", 7), ("sweep.1", GONE),
    ("sweep.0.suggested", 7), ("sweep.0.held", 7), ("runs.0.name", "x"),
    ("runs.0.days", ["2026-05-01", "2026-05-03"]), ("runs.0.photos", 99),
    ("runs.0.first_photo.at", "2026-05-01T09:00:01"), ("runs.0.first_photo.zone", 3600),
    ("runs.0.last_photo.at", "2026-05-03T18:00:01"), ("runs.0.start", "2026-05-01T09:00:01+00:00"),
    ("runs.0.end", "2026-05-03T18:00:00.500000+00:00"), ("runs.0.evidence", "thin"),
    ("runs.0.flags", ["evidence thin", "fast near"]),
    ("runs.1.reasons", ["brief: one photo"]), ("boundaries.0.suggestions", ["fast: it was"]),
    ("visits.2.0", GONE), ("visits.2.0.id", 5), ("visits.2.0.date_from", "2013"),
    ("visits.2.0.date_to", "2013-02-03"), ("visits.2.0.quality", 5), ("visits.2.0.trip_id", 5),
    ("trips.0.id", 5), ("trips.0.date_from", None), ("trips.0.date_to", "2026-05-30"),
    ("trips.0.description", None), ("trips.0", GONE),
]


@pytest.mark.parametrize("path, value", GOOD_CHANGES, ids=lambda v: str(v)[:40])
def test_everything_that_was_shown_is_in_the_hash(tmp_path, path, value):
    m = small()
    raw = put(json.loads(m.to_json()), path, value)
    if path == "no_track":
        raw |= {"no_track": False, "sources": [*raw["sources"], TRACK]}
    with pytest.raises(PlanInvalid, match=r"m\.json.*sha256"):
        reload(tmp_path, raw)
    assert reload(tmp_path, rehashed(raw)).sha256 != m.sha256  # the hash was all that was wrong


BAD_CHANGES = [
    ("account", True), ("account", "1"), ("account", 1.0), ("created", None), ("created", 5),
    ("format", True), ("format", "1"), ("format", 1.0), ("no_track", 1), ("no_track", False),
    *((p, v) for p in ("params", "sources", "coverage", "left_out", "sweep", "runs", "boundaries",
                       "visits", "trips") for v in (None, 5, "x", GONE)),
    ("params.since", None), ("params.since", "2026"), ("params.since", "20260420"),
    ("params.since", GONE), ("params.until", ""), ("params.until", GONE),
    ("params.gap_days", 0), ("params.gap_days", True), ("params.gap_days", 2.0),
    ("params.speeds", {}), ("params.speeds.fast_kmh", "150"), ("params.speeds.warp", 1.0),
    ("params.speeds.fast_kmh", 150), ("params.spans", 0), ("params.spans", True),
    ("params.more", 1),
    ("sources.0", GONE), ("sources.0.kind", "memory"), ("sources.0.kind", ["photos"]),
    ("sources.0.kind", "spans"),
    ("sources.0.label", "/Users/x/lib"), ("sources.0.label", "a\\b"), ("sources.0.label", ""),
    ("sources.0.label", None), ("sources.0.sha256", "A" * 64), ("sources.0.sha256", "a" * 63),
    ("sources.0.sha256", 5), ("sources.0.first", None), ("sources.0.last", "2019-03-01"),
    ("sources.0.first", "2019"), ("sources.0.count", -1), ("sources.0.count", 0),
    ("sources.0.count", True), ("sources.0.path", "x"),
    ("sources", [PHOTOS, PHOTOS]), ("sources", [PHOTOS, TRACK]), ("sources", [PHOTOS, SPANS]),
    ("coverage.tiles_read", -1), ("coverage.tiles_unread", None), ("coverage.unread_photos", 1.0),
    ("coverage.more", 1), ("left_out.fast", -1), ("left_out.fast", True), ("left_out.edge", GONE),
    ("left_out.bored", 1),
    ("sweep.0.gap_days", 0), ("sweep.0.gap_days", 2), ("sweep.0.runs", -1), ("sweep.0.odd", 1),
    ("sweep.0.held", GONE), ("sweep.0", [1, 2, 3, 4]),
    ("runs.0", "x"), ("runs.0.region", "1"), ("runs.0.region", 3), ("runs.0.region", True),
    ("runs.0.name", None), ("runs.0.first", "2026-05-02"), ("runs.0.last", "2026-05-02"),
    ("runs.0.first", "2026"), ("runs.0.days", []), ("runs.0.days", "2026-05-01"),
    ("runs.0.days", ["2026-05-01", "2026-05-01", "2026-05-03"]),
    ("runs.0.days", ["2026-05-03", "2026-05-01"]), ("runs.0.days", [None]),
    ("runs.0.silent", ["2026-05-02"]), ("runs.0.silent", ["2026-05-04"]), ("runs.0.silent", None),
    ("runs.0.photos", 2), ("runs.0.photos", True), ("runs.0.photos", "6"),
    ("runs.0.first_photo", None), ("runs.0.first_photo.at", "2026-04-30T09:00:00"),
    ("runs.0.first_photo.at", "2026-05-01 09:00:00"), ("runs.0.first_photo.zone", 1.5),
    ("runs.0.first_photo.zone", 14 * 3600 + 1), ("runs.0.first_photo.zone", -12 * 3600 - 1),
    ("runs.0.first_photo.zone", None), ("runs.0.first_photo.uuid", "x"),
    ("runs.0.last_photo.at", "2026-05-04T09:00:00"),
    ("runs.0.start", "2026-05-01T09:00:00"), ("runs.0.start", "2026-05-01T11:00:00+02:00"),
    ("runs.0.start", "2026-05-01T09:00:00Z"), ("runs.0.start", None),
    ("runs.0.start", "2026-05-03T18:00:01+00:00"),
    ("runs.0.end", "2026-04-30T18:00:00+00:00"),
    ("runs.1.start", "2026-05-01T08:00:00+00:00"),  # before the one that is before it
    ("runs.0.evidence", "great"), ("runs.0.evidence", None), ("runs.0.evidence", "serial"),
    ("runs.0.flags", ["bored"]), ("runs.0.flags", None), ("runs.0.flags", "evidence thin"),
    ("runs.0.flags", []), ("runs.0.flags", [5]),
    ("runs.0.reasons", ["bored: yes"]), ("runs.0.reasons", ["not judged"]),
    ("runs.0.reasons", ["open: no"]), ("runs.0.reasons", None), ("runs.0.reasons", [None]),
    ("runs.0.status", "held"), ("runs.0.status", "maybe"), ("runs.0.status", None),
    ("runs.0.status", ["offered"]), ("runs.0.status", "there"), ("runs.0.lat", 1.0),
    ("runs.1.reasons", []), ("runs.1.reasons", ["overlap: x"]), ("runs.1.status", "offered"),
    ("boundaries", []), ("boundaries.0.before", "2:2026-05-03:2026-05-05"),
    ("boundaries.0.after", "x"), ("boundaries.0.suggestions", ["bored"]),
    ("boundaries.0.suggestions", None), ("boundaries.0.suggestions", ["profile: x"]),
    ("boundaries.0.suggestions", ["gap: x"]), ("boundaries.0.forced", True),
    ("boundaries.0.forced", 0), ("boundaries.0.forced", None), ("boundaries.0.id", "x"),
    ("visits.2", GONE), ("visits.2", None), ("visits.02", []), ("visits.9", []), ("visits.x", []),
    ("visits.2.0.id", "72"), ("visits.2.0.date_from", "2026-5-1"), ("visits.2.0.date_from", 2013),
    ("visits.2.0.date_from", "1776"), ("visits.2.0.quality", None), ("visits.2.0.quality", -1),
    ("visits.2.0.trip_id", "5"), ("visits.2.0.trip_id", GONE), ("visits.2.0.region", 2),
    ("visits.2.0", "x"),
    ("trips.0.id", None), ("trips.0.id", True), ("trips.0.date_from", 2026),
    ("trips.0", TRIP | {"date_from": "2026-05-06", "date_to": "2026-05-09"}),  # other days
    ("trips.0.description", 5), ("trips.0.regions", []), ("trips.0.description", GONE),
    ("trips.0", None),
    ("trips", [TRIP, TRIP]), ("trips", [TRIP, TRIP | {"date_to": "2026-05-09"}]),
    ("more", 1), ("sha256", "0" * 64), ("sha256", None), ("sha256", GONE),
]


@pytest.mark.parametrize("path, value", BAD_CHANGES, ids=lambda v: str(v)[:40])
def test_a_list_of_another_shape_is_refused_whatever_its_hash(tmp_path, path, value):
    raw = put(json.loads(small().to_json()), path, value)
    for bad in (raw, raw if path == "sha256" else rehashed(raw)):
        with pytest.raises(PlanInvalid, match=r"m\.json"):
            reload(tmp_path, bad)


@pytest.mark.parametrize("change", [
    lambda r: r["runs"].append(r["runs"][1]),                          # one id twice
    lambda r: r["runs"].reverse(),                                     # another order
    lambda r: r["boundaries"].append(r["boundaries"][0]),
    lambda r: (r["runs"].reverse(), r["boundaries"][0].update(
        before=r["boundaries"][0]["after"], after=r["boundaries"][0]["before"])),
    lambda r: (r["runs"][1].update(status="there", reasons=["there: visit 72", "open: x"]),
               r["boundaries"][0].update(forced=True, suggestions=["profile: x"])),
    lambda r: (r["runs"][1].update(status="there", reasons=["there: visit 72"]),
               r["boundaries"][0].update(forced=True)),                # does not say why
    lambda r: (r["runs"].append(r["runs"][1]), r["boundaries"].append(
        {"before": Q_ID, "after": Q_ID, "suggestions": [], "forced": False})),
    lambda r: r.update(no_track=False, sources=[PHOTOS, TRACK | {"sha256": None}]),
    lambda r: r["params"].update(since="2026-04-21", until=20260530),
], ids=range(9))
def test_runs_and_boundaries_that_do_not_fit_each_other_are_refused(tmp_path, change):
    raw = json.loads(small().to_json())
    change(raw)
    with pytest.raises(PlanInvalid, match=r"m\.json"):
        reload(tmp_path, rehashed(raw))


def test_a_run_that_is_there_forces_its_boundary_in_the_list(tmp_path):
    raw = json.loads(small().to_json())
    raw["runs"][1].update(status="there", reasons=["there: visit 72, 2026-05-03 to 2026-05-05"])
    with pytest.raises(PlanInvalid, match="forced"):
        reload(tmp_path, rehashed(raw))
    raw["boundaries"][0].update(forced=True, suggestions=["profile: x"])
    assert reload(tmp_path, rehashed(raw)).runs[1].status == "there"


def test_the_constructor_takes_its_own_kinds_only():
    m = small()
    for change in ({"runs": m._stored()["runs"]}, {"boundaries": m._stored()["boundaries"]},
                   {"sweep": [(1, 2, 3, 4)]}, {"runs": cuts([*stay(R, 1, 3)])[0]},
                   {"runs": 5}, {"format": 2}, {"visits": m.visits | {"9": []}}):
        with pytest.raises(PlanInvalid):
            dataclasses.replace(m, **change)
    assert dataclasses.replace(m) == m
    day_of = dataclasses.replace(m.runs[0], first=dt.datetime(2026, 5, 1))
    with pytest.raises(PlanInvalid, match="first"):
        dataclasses.replace(m, runs=[day_of, m.runs[1]])
    east = m.runs[0].start.astimezone(dt.timezone(dt.timedelta(hours=2)))
    with pytest.raises(PlanInvalid, match="start"):
        dataclasses.replace(m, runs=[dataclasses.replace(m.runs[0], start=east), m.runs[1]])


def test_a_name_no_file_can_hold_is_mended_once(tmp_path):
    m = small(names=NAMES | {R: "half \ud83d of a pair"},
              trips=[TRIP | {"description": "\udc00"}])
    assert m.runs[0].name == "half � of a pair" and m.trips[0]["description"] == "�"
    assert TripList.load(m.save(tmp_path / "m.json")) == m


@pytest.mark.parametrize("content", [
    "", "{", "null", "[]", "5", "{}", "[" * 100_000, '{"format": 1, "format": 1}',
    '{"account": NaN}', '{"account": Infinity}', '{"account": ' + "9" * 5000 + "}",
])
def test_load_refuses_a_file_that_is_no_trip_list(tmp_path, content):
    with pytest.raises(PlanInvalid, match=r"m\.json"):
        reload(tmp_path, content)


def test_load_refuses_a_trip_list_it_cannot_read_and_says_a_later_format_is_one(tmp_path):
    (tmp_path / "bytes.json").write_bytes(b"\xff\xfe")
    for path in (tmp_path / "absent.json", tmp_path, tmp_path / "bytes.json"):
        with pytest.raises(PlanInvalid, match=re.escape(str(path))):
            TripList.load(path)
    with pytest.raises(PlanInvalid, match="format 2"):
        reload(tmp_path, json.loads(small().to_json()) | {"format": 2, "more": 1})
    text = small().to_json()
    with pytest.raises(PlanInvalid, match="'name' twice"):
        reload(tmp_path, text.replace('"name": "Aldmoor"', '"name": "Aldmoor", "name": "x"'))


# -- what the builder reads


def test_the_visits_of_every_region_on_the_list_and_of_no_other():
    m = listed([*stay(R, 1, 3), *stay(Q, 4, 6)], visits=UNDATED_Q | {77: [visit(1, 2, 77)]})
    assert m.visits == {"1": [], "2": [{"id": 72, "date_from": None, "date_to": None,
                                        "quality": 3, "trip_id": None}]}
    with pytest.raises(PlanInvalid, match="region 2"):  # not read is not empty
        visits_rows({R: []}, [R, Q])
    with pytest.raises(PlanInvalid, match=r"name.*region 1"):
        small(names={Q: "Berrow"})


def test_live_visits_in_the_form_of_the_list():
    """As `judge` has them and as a snapshot has them: one form, in one order."""
    rows = [visit(2013, None, id=9, trip_id=5), visit(10, 14, id=3, quality=1)]
    want = {"1": [
        {"id": 3, "date_from": "2026-05-10", "date_to": "2026-05-14", "quality": 1,
         "trip_id": None},
        {"id": 9, "date_from": "2013", "date_to": None, "quality": 3, "trip_id": 5}]}
    assert visits_rows({R: rows, Q: []}, [R]) == want
    snap = {"1": [v.__dict__ | {"date_from": v.date_from and v.date_from.isoformat(),
                                "date_to": v.date_to and v.date_to.isoformat()} for v in rows]}
    assert visits_rows(snap, {R}) == want
    assert visits_rows(snap, []) == {}
    for bad in ({R: [visit(1, 2, Q)]}, {R: ["x"]}, {R: None}, {R: [{"id": 1}]}, {"01": []}):
        with pytest.raises(PlanInvalid):
            visits_rows(bad, [R])
    assert trip_list.visits_rows({R: rows}, [R]) != visits_rows({R: rows[:1]}, [R])


def header(a, b, id=9, **kw):
    return {"id": id, "date_from": a, "date_to": b, "description": ""} | kw


def test_trip_headers_over_the_same_days():
    runs = small().runs  # 1 to 3, and 3 to 5 May
    over = lambda a, b: bool(trips_rows([header(a, b)], runs))  # noqa: E731
    assert over("2026-05-05", "2026-05-09") and over("2026-04-01", "2026-05-01")
    assert not over("2026-05-06", "2026-05-09") and not over("2026-04-01", "2026-04-30")
    # An end that is not there, or is a year, or cannot be read, is not shown to be apart.
    assert over(None, None) and over("2026-05-09", None) is False and over(None, "2026-05-02")
    assert over("2026", "2026") and not over("2025", "2025") and over("soon", "never")
    assert over("2026-05-09", "2026-05-02")  # the wrong way round
    assert trips_rows([], runs) == [] and trips_rows([header(None, None)], []) == []


def test_live_trips_in_the_form_of_the_list():
    runs = small().runs
    live = {"id": "551", "regions": [{"id": 1}], "flags": "x"} | {k: TRIP[k] for k in (
        "date_from", "date_to", "description")}
    assert trips_rows([live], runs) == [TRIP]
    assert trips_rows([live, dict(live)], runs) == [TRIP]  # listed under two years
    assert trips_rows([{"id": 7}], runs) == [
        {"id": 7, "date_from": None, "date_to": None, "description": None}]
    for bad in ([live, live | {"date_to": "2026-05-09"}], [{"date_from": None}], ["x"], [5],
                [live | {"id": True}], [live | {"date_from": 5}], [live | {"description": 5}]):
        with pytest.raises(PlanInvalid):
            trips_rows(bad, runs)


def test_left_out_gives_each_photo_of_the_dates_one_reason():
    obs = [ob(5), ob(5, kind="fast"), ob(5, kind="fast", state="nowhere"),
           ob(5, kind="conflict", use="all"), ob(5, use="none", kind="fast"),
           ob(5, use="airports"), ob(5, timed=False), ob(5, state="unread"), ob(5, state="stale"),
           ob(5, state="nowhere"), ob(5, state="nearest"), ob(5, state="edge"),
           ob(5, recorded=False), ob(5, recorded=False, state="edge"),
           ob(5, origin="airdrop", kind="fast"), ob(4, kind="fast"), ob(7, kind="fast")]
    ask = {"since": day(5), "until": day(6)}
    assert left_out(obs, **ask) == {
        "other_origin": 1, "no_utc": 1, "span_none": 1, "conflict": 1, "fast": 2,
        "span_airports": 1, "unread": 1, "stale": 1, "nowhere": 1, "nearest": 1, "edge": 2,
        "clock": 1}
    assert left_out(obs, since=None, until=None, other_origin=5) == left_out(obs, **ask) | {
        "fast": 4, "other_origin": 6}
    assert left_out([], **ask) == dict.fromkeys(LEFT_OUT, 0)
    assert small(left_out=left_out(obs, **ask)).left_out["fast"] == 2
    out = sum(left_out(obs, since=None, until=None).values())
    assert out == sum(not o.proof for o in obs)


# -- words


def test_words_are_the_owners_and_one_line():
    for good in ("a", "home on the 7th", "x" * 500, "\U0001f5fc" * 500, "東京", "й", "a‍b",
                 " x ", "́"):
        assert words_problem(good) is None, good
    for bad in ("", " ", " ", "　", "​", "﻿", "ㅤ", "⠀", "️",
                "\U000e0041", "", "x" * 501, "\U0001f5fc" * 501, "a\nb", "a\rb", "a\tb",
                "a\x00", "a\x85b", "a b", "a ", "a‮b", "‎a", "a؜",
                "a⁦", "a\ud83d", "\udc00a"):
        assert words_problem(bad), repr(bad)


def test_what_says_something_does_not_depend_on_a_version_of_unicode():
    """The table is the definition. It has all that Python 3.2's Unicode called blank."""
    old = unicodedata.ucd_3_2_0
    ranges = trips.SAYS_NOTHING
    assert list(ranges) == sorted(ranges)
    assert all(a <= b for a, b in ranges)
    assert all(b + 1 < c for (_, b), (c, _) in itertools.pairwise(ranges))
    inside = {n for a, b in ranges for n in range(a, b + 1)}
    assert {n for n in range(0x110000) if old.category(chr(n)) in (
        "Cc", "Cf", "Cs", "Co", "Zs", "Zl", "Zp")} <= inside
    letters = {n for n in range(0x110000) if old.category(chr(n))[0] in "LNPS"}
    assert letters & inside == {0xAD, 0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0}
    for n in (0x41, 0x20, 0x2800, 0xD800, 0x10FFFF, 0x1F5FC):
        assert trips._says(chr(n)) is (n not in inside)


def test_what_words_may_not_hold_is_what_the_pages_take_out_and_the_line_breaks():
    inside = {n for a, b in trip_list._NOT_IN_WORDS for n in range(a, b + 1)}
    assert inside == {*_CONTROLS, 0x2028, 0x2029, *range(0xD800, 0xE000)}


# -- the answers


def given(m, runs=None, cuts=None, **kw):
    return {"format": 1, "account": 1, "manifest": m.sha256, "runs": {} if runs is None else runs,
            "cuts": {} if cuts is None else cuts} | kw


def answers_file(tmp_path, raw):
    path = tmp_path / "trips-answers.json"
    if isinstance(raw, bytes):
        path.write_bytes(raw)
    else:
        path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    return path


R_ID, Q_ID, S_ID = "1:2026-05-01:2026-05-03", "2:2026-05-03:2026-05-05", "3:2026-05-06:2026-05-08"
FOUR_ID, NINE_ID = "4:2026-05-09:2026-05-10", "9:2026-05-13:2026-05-14"
CUT_RQ, CUT_QS, CUT_49 = f"{R_ID}|{Q_ID}", f"{Q_ID}|{S_ID}", f"{FOUR_ID}|{NINE_ID}"


def test_answers_are_read_and_blank_ones_are_absent(tmp_path):
    m = listed(visits=MIXED)
    runs = {R_ID: {"a": "3"}, Q_ID: {"a": "2", "words": " the other one was as a child "},
            S_ID: {"a": "no"}, FOUR_ID: {"a": "?"}}
    cut = {CUT_RQ: "cut", CUT_QS: "same"}
    path = answers_file(tmp_path, given(m, runs, cut))
    assert load_answers(path, m) == {"runs": runs, "cuts": cut}
    assert read_answers(path, m) == ({"runs": runs, "cuts": cut},
                                     hashlib.sha256(path.read_bytes()).hexdigest())
    assert load_answers(answers_file(tmp_path, given(m)), m) == {"runs": {}, "cuts": {}}
    words = {Q_ID: {"words": "later"}}
    assert load_answers(answers_file(tmp_path, given(m, words)), m)["runs"] == words
    made_up = listed(visits=MIXED, created="tomorrow")
    assert load_answers(answers_file(tmp_path, given(m, runs)), made_up)["runs"] == runs


def test_the_five_hundredth_character_is_the_last(tmp_path):
    m = listed(visits=MIXED)
    for char in ("x", "\U0001f5fc"):
        full = {Q_ID: {"words": char * 500}}
        for raw in (given(m, full), json.dumps(given(m, full), ensure_ascii=False)):
            assert load_answers(answers_file(tmp_path, raw), m)["runs"] == full
        with pytest.raises(PlanInvalid, match="501 of 500"):
            load_answers(answers_file(tmp_path, given(m, {Q_ID: {"words": char * 501}})), m)


@pytest.mark.parametrize("change, why", [
    ({"manifest": "0" * 64}, "another list"), ({"manifest": None}, "another list"),
    ({"manifest": ["x"]}, "another list"), ({"account": 2}, "account"),
    ({"account": True}, "account"), ({"account": "1"}, "account"), ({"account": 1.0}, "account"),
    ({"format": 2}, "format"), ({"format": True}, "format"), ({"format": "1"}, "format"),
    ({"note": "x"}, "note"),
    *(({k: v}, f"{k} must be an object") for k in ("runs", "cuts") for v in ([], "x", 0)),
    *(({"runs": {k: {"a": "3"}}}, "not on this list") for k in (
        "1:2026-05-01:2026-05-04", "01:2026-05-01:2026-05-03", "", CUT_RQ, "1")),
    *(({"runs": {R_ID: v}}, R_ID) for v in (
        {}, {"a": "0"}, {"a": "7"}, {"a": 3}, {"a": None}, {"a": ""}, {"a": "3 "}, {"a": ["3"]},
        {"a": True}, {"a": "No"}, {"a": "yes"}, {"a": "12"}, {"a": "٣"},
        {"a": "3", "words": "x"}, {"words": "x"}, {"words": ""}, {"a": "3", "b": 1}, "3", None,
        ["3"])),
    *(({"runs": {Q_ID: {"a": "3", "words": v}}}, Q_ID) for v in (
        "", " ", "​", "x" * 501, "a\nb", "a‮b", "\x1b[0m", "a\ud800", None, 5, ["x"])),
    *(({"runs": {S_ID: {"a": v}}}, S_ID) for v in ("1", "3", "6", "?")),
    ({"runs": {S_ID: {"words": "but I was"}}}, S_ID),
    *(({"cuts": {k: "cut"}}, "not on this list") for k in (
        R_ID, f"{Q_ID}|{R_ID}", f"{R_ID}|{S_ID}", "")),
    *(({"cuts": {CUT_RQ: v}}, R_ID) for v in ("join", "", None, True, ["cut"], "Cut")),
])
def test_answers_that_do_not_fit_are_refused(tmp_path, change, why):
    m = listed(visits=MIXED)
    with pytest.raises(PlanInvalid, match=rf"trips-answers\.json.*{re.escape(why)}"):
        load_answers(answers_file(tmp_path, given(m, **change)), m)


def test_what_the_page_has_no_control_for_is_refused(tmp_path):
    """A forced cut, a run that is there, and a run that nothing releases."""
    m = listed(visits=KNOWN)
    assert [r.status for r in m.runs] == ["offered", "held", "fixed", "offered", "there"]
    assert [b.forced for b in m.boundaries] == [False, True, True, True]
    for cut in ("cut", "same"):
        with pytest.raises(PlanInvalid, match="forced"):
            load_answers(answers_file(tmp_path, given(m, cuts={CUT_QS: cut})), m)
    for a in ("no", "?", "3"):
        with pytest.raises(PlanInvalid, match="takes no answer"):
            load_answers(answers_file(tmp_path, given(m, {NINE_ID: {"a": a}})), m)
    assert load_answers(answers_file(tmp_path, given(m, {S_ID: {"a": "no"}})), m)


@pytest.mark.parametrize("gone", ["format", "account", "manifest", "runs", "cuts"])
def test_answers_without_a_field_are_refused(tmp_path, gone):
    m = small()
    raw = {k: v for k, v in given(m).items() if k != gone}
    with pytest.raises(PlanInvalid, match=gone):
        load_answers(answers_file(tmp_path, raw), m)


def test_answers_to_a_list_since_changed_are_refused(tmp_path):
    path = answers_file(tmp_path, given(small(), {R_ID: {"a": "3"}}))
    for changed in (small(trips=[]), small(visits={Q: [visit(None, None, Q, id=73)]}),
                    small(names=NAMES | {R: "Elsewhere"}), small(no_track=False,
                                                                sources=[PHOTOS, TRACK])):
        with pytest.raises(PlanInvalid, match="another list"):
            load_answers(path, changed)


@pytest.mark.parametrize("content", [
    "", "{", "null", "[]", '"y"', "5", "[" * 100_000, b"\xff\xfe", b"\x00", '{"runs": NaN}',
    '{"account": ' + "9" * 5000 + "}",
])
def test_a_file_that_is_not_answers_is_refused(tmp_path, content):
    with pytest.raises(PlanInvalid, match=r"trips-answers\.json"):
        load_answers(answers_file(tmp_path, content), small())


def test_a_repeated_answer_and_a_huge_file_are_refused(tmp_path):
    m = small()
    text = json.dumps(given(m, {R_ID: {"a": "3"}}, {CUT_RQ: "cut"}))
    for old, new, why in (('"a": "3"', '"a": "3", "a": "no"', "'a' twice"),
                          ('"cuts": {', '"cuts": {}, "cuts": {', "'cuts' twice"),
                          (f'"{CUT_RQ}": "cut"', f'"{CUT_RQ}": "cut", "{CUT_RQ}": "same"',
                           "twice")):
        assert old in text
        with pytest.raises(PlanInvalid, match=why):
            load_answers(answers_file(tmp_path, text.replace(old, new)), m)
    path = answers_file(tmp_path, text + " " * (5_000_000 - len(text)))
    assert load_answers(path, m)
    path.write_text(text + " " * (5_000_001 - len(text)), encoding="utf-8")
    with pytest.raises(PlanInvalid, match="5 MB"):
        load_answers(path, m)
    for path in (tmp_path / "absent.json", tmp_path):
        with pytest.raises(PlanInvalid, match="cannot be read"):
            load_answers(path, m)


# -- the rules of `group`, from the list


def as_text(drafts, not_made):
    """What `group` returned, as the page's `group` returns it."""
    return {"trips": [{"rows": [[row.run.id, row.quality, row.words] for row in d.rows],
                       "date_from": str(d.date_from), "date_to": str(d.date_to)} for d in drafts],
            "notMade": [{"runs": list(n.runs), "reasons": list(n.reasons)} for n in not_made]}


TWICE = [*stay(R, 1, 3), *stay(Q, 4, 6), *stay(R, 7, 9), ob(40, S)]
OPEN_INSIDE = [*stay(R, 1, 3), *stay(Q, 4, 6), ob(6, S, 20), ob(6, S, 21)]
ACROSS = [*stay(R, 1, 3), *stay(Q, 4, 5), *stay(S, 6, 8), ob(40, 8)]
SAME = {(4, 9): "same"}
BUT_S = {k: v for k, v in ALL.items() if k != S}

# obs, answers by region, cuts by pair of regions, visits, words by region, until
RULES = [
    ([], {}, {}, {}, {}, 30),
    (JOURNEY, {}, {}, {}, {}, 30),
    (JOURNEY, ALL, SAME, {}, {}, 30),
    (JOURNEY, ALL, {(Q, S): "cut", (4, 9): "cut"}, {}, {}, 30),
    (JOURNEY, ALL, {}, {}, {}, 30),
    (JOURNEY, ALL, {(Q, S): "cut"}, {}, {}, 30),
    (JOURNEY, BUT_S, {(4, 9): "cut"}, {}, {}, 30),
    (JOURNEY, BUT_S | {S: "?"}, {(4, 9): "cut"}, {}, {}, 30),
    (JOURNEY, ALL | {S: "no"}, SAME, {}, {}, 30),
    (JOURNEY, ALL | {R: "no", 9: "no"}, SAME, {}, {}, 30),
    (JOURNEY, ALL | {S: "no"}, SAME | {(Q, S): "cut"}, {}, {}, 30),
    (JOURNEY, dict.fromkeys(ALL, "no"), SAME, {}, {}, 30),
    (JOURNEY, dict.fromkeys(ALL, "no"), {}, {}, {}, 30),
    (JOURNEY, BUT_S, {(Q, S): "same", (S, 4): "same"} | SAME, {S: [visit(6, 8, S)]}, {}, 30),
    *[(JOURNEY, BUT_S | ({S: a} if a else {}), {(Q, S): "same", (S, 4): "same"} | SAME,
       {S: [visit(8, 10, S)]}, {}, 30) for a in ("no", "?", None)],
    *[(obs, BUT_S | a, SAME, visits, {}, 30) for obs, visits in FIXED.values()
      for a in ({}, {S: "no"})],
    (ACROSS, {R: "3", Q: "3", S: "no"}, {}, {}, {}, 7),
    (ACROSS, {R: "3", Q: "3"}, {}, {}, {}, 7),
    *[(JOURNEY, ALL, SAME, {Q: [visit(None, None, Q)]}, w, 30)
      for w in ({}, {Q: " "}, {Q: "‎"}, {Q: "ㅤ"}, {Q: " as a child "}, {Q: "x" * 600})],
    (JOURNEY, ALL | {Q: "no"}, SAME, {Q: [visit(None, None, Q)]}, {}, 30),
    (OPEN, {R: "3", Q: "3"}, {(R, Q): "same"}, {}, {}, 30),
    (OPEN, {R: "3"}, {}, {}, {}, 30),
    (OPEN, {R: "3", Q: "no"}, {}, {}, {}, 30),
    (OPEN, {R: "3", Q: "3"}, {}, {}, {Q: "I flew home on the 7th"}, 30),
    (OPEN, {R: "3", Q: "3"}, {(R, Q): "cut"}, {}, {Q: "home on the 7th"}, 30),
    (OPEN_INSIDE, {R: "3", Q: "3", S: "3"}, {}, {}, {S: "home the next day"}, 30),
    (TWICE, {R: "3", Q: "3"}, {}, {}, {}, 30),
    (TWICE, {R: "3", Q: "3"}, {(Q, R): "cut"}, {}, {}, 30),
]


def rule_case(obs, by_region, cut, visits, words, until):
    m = listed(obs, visits, until=until)
    of = {r.id: r.region for r in m.runs}
    runs = {r.id: {k: src[r.region] for k, src in (("a", by_region), ("words", words))
                   if r.region in src} for r in m.runs}
    pairs = {b.id: cut[key] for b in m.boundaries if (key := (of[b.before], of[b.after])) in cut}
    return m, {"runs": {k: v for k, v in runs.items() if v}, "cuts": pairs}


@pytest.mark.parametrize("case", RULES, ids=range(len(RULES)))
def test_group_is_the_same_on_a_list_that_was_read_back(tmp_path, case):
    obs, _, _, visits, _, until = case
    m, ans = rule_case(*case)
    back = TripList.load(m.save(tmp_path / "m.json"))
    runs, bs = cuts(obs, visits, until=day(until))
    want = as_text(*group(runs, bs, ans))
    assert as_text(*group(back.runs, back.boundaries, ans)) == want
    assert as_text(*group(back.runs[::-1], back.boundaries, ans)) == want


REASONS = [(), (), (), (), ("open: x",), ("brief: one photo",), ("open: x", "undated: y"),
           ("layover?: x",), ("overlap: x",), ("overlap: x", "open: x"), ("continuation: x",),
           ("not read: x", "open: x"), ("there: visit 5, x",), ("clock: x",)]
WORDS = [None, None, " ", "‎", "home", " a \U0001f5fc "]


def any_list(rng):
    """A list of made-up runs whose parts fit each other, and answers of every kind."""
    runs, on = [], day(1)
    for n in range(rng.randrange(9)):
        first = on + dt.timedelta(days=rng.choice((0, 1, 1, 2, 6)))
        last = on = first + dt.timedelta(days=rng.randrange(3))
        a, b = (dt.datetime.combine(d, dt.time(6 + 12 * e, n)) for e, d in enumerate((first, last)))
        region, reasons = rng.choice((R, R, Q, S, 4)), rng.choice(REASONS)
        runs.append(ListedRun(
            region, NAMES[region], first, last, tuple(sorted({first, last})), (), 4,
            PhotoTime(a, 0), PhotoTime(b, 0), a.replace(tzinfo=UTC), b.replace(tzinfo=UTC),
            "thin", ("evidence thin",), reasons, status_of(reasons)))
    if len({r.id for r in runs}) < len(runs):
        return any_list(rng)
    bs = []
    for a, b in itertools.pairwise(runs):
        forced = any(kind_of(x) in ("there", "overlap") for x in (*a.reasons, *b.reasons))
        why = [*(["gap: x"] if (b.first - a.last).days > 2 else []),
               *(["profile: x"] if forced else []),
               *(["fast: x"] if rng.random() < 0.2 else [])]
        bs.append(Boundary(a.id, b.id, tuple(why), forced))
    m = TripList(1, "", PARAMS, [PHOTOS], True, COVERAGE, LEFT_OUT, SWEEP, runs, bs,
                 {str(r.region): [] for r in runs}, [])
    ans = {}
    for r in runs:
        row = {}
        takes = {"offered": "123456", "held": "123456"}.get(r.status, "")
        a = rng.choice([None, "no", "?", *takes, *takes])
        if a is not None and r.status != "there":
            row["a"] = a
        if r.status == "held" and (words := rng.choice(WORDS)) is not None:
            row["words"] = words
        if row:
            ans[r.id] = row
    cut = {b.id: c for b in bs if (c := rng.choice((None, "cut", "same", "same")))}
    return m, {"runs": ans, "cuts": cut}


# -- the page --------------------------------------------------------------------------------

CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "base-uri 'none'; form-action 'none'")
DATA = re.compile(r'(<script type="application/json" id="data">)(.*?)(</script>)', re.S)


def embedded(page):
    (match,) = DATA.findall(page)
    return match[1]


def hostile(**kw):
    return listed(visits=MIXED, names=dict.fromkeys(NAMES, HOSTILE),
                  trips=[TRIP | {"description": HOSTILE}], **kw)


def test_the_page_holds_the_list_and_its_hash_and_the_sites_words():
    m = hostile()
    data = json.loads(embedded(render_page(m)))
    page = data.pop("page")
    assert data == json.loads(m.to_json()) and data["sha256"] == m.sha256
    assert page["quality"] == {str(q): word for q, word in QUALITY.items() if q}
    assert page["marks_from"] == 2 and page["max_words"] == 500
    assert page["blank"] == [list(r) for r in trips.SAYS_NOTHING]
    assert page["refused"] == [list(r) for r in trip_list._NOT_IN_WORDS]
    assert set(page["controls"]) == set(_CONTROLS)
    rest = DATA.sub("", render_page(m))
    for word in QUALITY.values():  # not written twice
        assert word not in rest, word


def test_hostile_names_and_descriptions_stay_inside_the_data():
    page, plain = render_page(hostile()), render_page(listed([]))
    for mark in ("<script", "</script", "<!--"):
        assert page.count(mark) == plain.count(mark)
    assert not set("<>&  ") & set(embedded(page))
    data = json.loads(embedded(page))
    assert data["runs"][0]["name"] == data["trips"][0]["description"] == HOSTILE
    assert DATA.sub("", page) == DATA.sub("", plain)
    for name in ("$&", "\\g<0>", "{data}", "%s", "__DATA__"):
        page = render_page(small(names=NAMES | {R: name}))
        assert json.loads(embedded(page))["runs"][0]["name"] == name
        assert DATA.sub("", page) == DATA.sub("", plain)


def test_the_page_reaches_for_nothing():
    """Nothing in it can load, send or lead anywhere, so the dates stay here."""
    url = "http://a.test/x //b.test fetch import src= href="
    page = render_page(small(names=NAMES | {R: url}))
    meta = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
    assert page.count(meta) == 1
    rest = DATA.sub("", page).replace(meta, "").lower()
    for word in ("http", "//", "fetch", "import", "src=", "href=", "xmlhttp", "websocket",
                 "eventsource", "beacon", "worker", "eval", "new function", "<form",
                 "<iframe", "<img", "<link", "<a ", "ping=", "window.open", "location"):
        assert word not in rest, word
    assert "url(" not in rest.split("<style>")[1].split("</style>")[0]
    at = page.index("Content-Security-Policy")
    assert all(at < page.index(tag) for tag in ("<style", "<script", "<body"))
    assert page.index('<meta charset="utf-8">') < at and page.startswith("<!doctype html>")
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                 "createContextualFragment", "DOMParser", "setHTML", "srcdoc"):
        assert word not in DATA.sub("", page)


def test_nothing_is_preselected_proposed_or_answered_in_bulk():
    rest = DATA.sub("", render_page(hostile()))
    for word in ('aria-pressed="true"', "checked", " selected", "autofocus", "accesskey",
                 "keydown", "keyup", "keypress", "placeholder", "maxlength", "datalist"):
        assert word not in rest.split("</style>")[1], word
    assert not re.search(r"(select|mark|answer|tick|clear|choose|cut)\W+(them\W+)?(all|every)\b",
                         rest, re.I)
    for word in ("probably", "likely", "we suggest", "must have", "you stayed", "you were in",
                 "recommended"):
        assert word not in rest.lower(), word


def test_the_page_says_what_an_answer_means():
    rest = re.sub(r"\s+", " ", DATA.sub("", render_page(small())))
    for copy in ("Leave it out", "I don't remember", "Clear this answer", "Same trip",
                 "A new trip starts here", "stored, but does not mark the region",
                 "Leaving a question blank is fine", "a final answer",
                 "This page sends nothing anywhere", "days with no photo",
                 "by the photo's own clock"):
        assert copy in rest, copy


def test_the_trips_template_is_in_the_package_and_its_answers_are_never_committed():
    page = Path(trip_list.__file__).with_name("trips_page.html")
    assert page.is_file() and "src/wanderfill/plan" in page.as_posix()
    root = Path(__file__).parents[1]
    assert 'packages = ["src/wanderfill"]' in (root / "pyproject.toml").read_text("utf-8")
    assert {"review/", "trips-answers*.json"} <= set(
        (root / ".gitignore").read_text("utf-8").splitlines())


# -- the page's own scripts, run

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="no node on this machine")


def run(tmp_path, m, body, **names):
    """What `body` returns, run in node with the scripts of the rendered page itself."""
    page, code = tmp_path / "page.html", tmp_path / "body.js"
    page.write_text(render_page(m), encoding="utf-8")
    code.write_text("".join(f"const {k} = {json.dumps(v)};\n" for k, v in names.items()) + body,
                    encoding="utf-8")
    done = subprocess.run([NODE, str(Path(__file__).with_name("trips_page_dom.js")),
                           str(page), str(code)], capture_output=True, encoding="utf-8",
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def stored(m, *keys):
    """The names this list's answers are stored under."""
    return [f"wanderfill-trips-review:{m.sha256}:{key}" for key in keys]


def page_data(m):
    return json.loads(embedded(render_page(m)))


GROUP = "return cases.map(([data, answers]) => logic.group(data, answers));"


@needs_node
def test_the_page_groups_by_every_rule_of_group(tmp_path):
    cases = [rule_case(*c) for c in RULES]
    got = run(tmp_path, small(), GROUP, cases=[[page_data(m), a] for m, a in cases])
    for (m, ans), page in zip(cases, got, strict=True):
        assert page == as_text(*group(m.runs, m.boundaries, ans))
    said = " ".join(r for g in got for n in g["notMade"] for r in n["reasons"])
    for rule in ("it is not answered whether a new trip starts", "is not answered", "question mark",
                 "needs words of yours", "cut between them"):
        assert rule in said, rule


@needs_node
def test_the_page_groups_whatever_the_answers_are(tmp_path):
    rng = random.Random(20260511)
    cases = [any_list(rng) for _ in range(400)]
    got = run(tmp_path, small(), GROUP, cases=[[page_data(m), a] for m, a in cases])
    for (m, ans), page in zip(cases, got, strict=True):
        assert page == as_text(*group(m.runs, m.boundaries, ans))
    assert sum(len(g["trips"]) for g in got) > 200 and sum(len(g["notMade"]) for g in got) > 200
    assert max(len(t["rows"]) for g in got for t in g["trips"]) > 2


@needs_node
def test_the_page_reads_words_as_the_answers_file_does(tmp_path):
    """At both ends of every range, alone and beside a letter; and by length."""
    edges = {n for table in (trips.SAYS_NOTHING, trip_list._NOT_IN_WORDS) for a, b in table
             for n in (a - 1, a, b, b + 1) if 0 <= n < 0x110000}
    texts = [t for n in sorted(edges) for t in (chr(n), "a" + chr(n))]
    texts += ["", "x" * 500, "x" * 501, "\U0001f5fc" * 500, "\U0001f5fc" * 501, "a\r\nb",
              "a‍́", "\U0001f468‍\U0001f469"]
    got = run(tmp_path, small(), "return texts.map((t) => "
              "[logic.wordsProblem(data, t), logic.says(data, t)]);", texts=texts)
    for t, (problem, says) in zip(texts, got, strict=True):
        assert (problem or None) == words_problem(t), repr(t)
        assert says is trips._says(t), repr(t)


@needs_node
def test_the_page_opens_with_nothing_answered_and_every_run_in_place(tmp_path):
    for m in (listed(visits=MIXED), listed(visits=KNOWN)):
        got = run(tmp_path, m, """
          const p = open();
          return [p.runs().map((r) => r.cls("name")[0].textContent), p.pressed(),
                  p.storage.held.size, p.byId.start.hidden, p.betweens().length,
                  p.byId.list.children.map((c) => c.className.split(" ")[0]),
                  p.runs().map((r) => r.className.split(" ").includes("grey")),
                  p.runs().map((r) => { const f = p.field(r); return f && f.value; })];
        """)
        assert got[0] == [f"{r.name} ({r.region}), {r.first} to {r.last}" for r in m.runs]
        # Only what stands at "same trip" is, and nothing is stored of it.
        standing = [b for b in m.boundaries if not b.suggestions]
        assert got[1] == ["Same trip"] * len(standing) and got[2] == 0
        assert got[3] is True and got[4] == len(m.boundaries)
        assert got[5] == ["run", "between"] * 4 + ["run"]
        assert got[6] == [r.status in ("fixed", "there") for r in m.runs]
        assert got[7] == ["" if r.status == "held" else None for r in m.runs]


QUALITIES = [f"{q} {word}" for q, word in QUALITY.items() if q]


@needs_node
def test_each_run_has_the_controls_its_answers_take(tmp_path):
    read = """
      const p = open();
      return p.runs().map((r) => [p.buttons(r), p.field(r) !== null]);
    """
    full = [*QUALITIES, "Leave it out", "I don't remember", "Clear this answer"]
    only_no = ["Leave it out", "Clear this answer"]
    assert run(tmp_path, listed(visits=MIXED), read) == [
        [full, False], [full, True], [only_no, False], [full, False], [full, False]]
    assert run(tmp_path, listed(visits=KNOWN), read) == [
        [full, False], [full, True], [only_no, False], [full, False], [[], False]]


@needs_node
def test_quality_one_says_beside_it_that_it_marks_nothing(tmp_path):
    got = run(tmp_path, small(), """
      const p = open();
      return p.runs().map((r) => r.cls("choice").map((c) => c.textContent));
    """)
    for texts in got:
        assert texts[0] == "1 transit stored, but does not mark the region"
        assert texts[1:] == QUALITIES[1:]


@needs_node
def test_each_boundary_has_a_control_or_says_why_not(tmp_path):
    read = """
      const p = open();
      return p.betweens().map((b) => [p.buttons(b), p.pressed(b), b.textContent]);
    """
    got = run(tmp_path, listed(visits=MIXED), read)
    two, clear = ["Same trip", "A new trip starts here"], "Clear this answer"
    assert [g[0] for g in got] == [two, two, two, [*two, clear]]
    assert [g[1] for g in got] == [["Same trip"]] * 3 + [[]]
    assert "More than 2 days" in got[3][2] and "2026-05-10 to 2026-05-13" in got[3][2]
    got = run(tmp_path, listed(visits=KNOWN), read)
    assert [g[0] for g in got] == [two, [], [], []]
    for _, _, text in got[1:]:
        assert "on your profile already" in text and "always starts here" in text
    got = run(tmp_path, listed(OPEN), read)
    assert "no words of yours" in got[0][2] and got[0][1] == ["Same trip"]
    got = run(tmp_path, listed(OPEN, {R: [visit(1, 3)]}), read)
    assert "no words of yours" not in got[0][2] and "always starts here" in got[0][2]


@needs_node
def test_a_standing_boundary_stores_a_cut_and_the_way_back_and_nothing_else(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, """
      const p = open();
      const b = p.betweens()[0];
      const out = [];
      const step = (word) => {
        p.press(b, word);
        out.push([Object.fromEntries(p.storage.held), p.pressed(b)]);
      };
      step("Same trip"); step("A new trip starts here"); step("Same trip");
      p.storage.held.clear();
      p.tell("storage", {});
      out.push([{}, p.pressed(b)]);
      p.storage.held.set(key, "cut");   /* another tab, and no event yet */
      step("Same trip");
      return out;
    """, key=stored(m, f"cut:{CUT_RQ}")[0])
    (key,) = stored(m, f"cut:{CUT_RQ}")
    same, cut = ["Same trip"], ["A new trip starts here"]
    assert got == [[{}, same], [{key: "cut"}, cut], [{key: "same"}, same], [{}, same],
                   [{key: "same"}, same]]


@needs_node
def test_a_suggested_boundary_is_blank_until_it_is_answered(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, """
      const p = open();
      const b = p.betweens()[3];
      const out = [];
      for (const word of ["Same trip", "A new trip starts here", "Clear this answer"]) {
        p.press(b, word);
        out.push([[...p.storage.held.values()], p.pressed(b)]);
      }
      return out;
    """)
    assert got == [[["same"], ["Same trip"]], [["cut"], ["A new trip starts here"]], [[], []]]


ANSWER_ALL = """
  const p = open();
  p.press(p.run("Aldmoor"), "3 good visit");
  p.press(p.run("Berrow"), "2 minimal visit");
  p.type(p.run("Berrow"), " the other one was as a child \U0001f5fc");
  p.press(p.run("Cray"), "Leave it out");
  p.press(p.run("Dunvale"), "I don't remember");
  p.press(p.run("Fenmark"), "1 transit");
  p.press(p.betweens()[0], "A new trip starts here");
  p.press(p.betweens()[3], "A new trip starts here");
"""
ANSWERED = {
    "runs": {R_ID: {"a": "3"},
             Q_ID: {"a": "2", "words": " the other one was as a child \U0001f5fc"},
             S_ID: {"a": "no"}, FOUR_ID: {"a": "?"}, NINE_ID: {"a": "1"}},
    "cuts": {CUT_RQ: "cut", CUT_49: "cut"}}


@needs_node
def test_the_pages_file_is_what_load_answers_reads(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, ANSWER_ALL + """
      p.byId.download.click();
      const during = p.revoked.length;
      p.timers.forEach((f) => f());
      p.byId.copy.click();
      await null; await null;
      const a = p.clicked[0], said = p.byId.said.textContent;
      p.press(p.run("Aldmoor"), "Clear this answer");
      return {text: p.blobs[0].text, type: p.blobs[0].type, name: a.getAttribute("download"),
              link: a.getAttribute("href"), during, revoked: p.revoked, copied: p.copied,
              said, after: p.byId.said.textContent, counts: p.byId.counts.textContent};
    """)
    assert load_answers(answers_file(tmp_path, got["text"]), m) == ANSWERED
    raw = json.loads(got["text"])
    assert list(raw) == ["format", "account", "manifest", "runs", "cuts"]
    assert list(raw["runs"]) == [r.id for r in m.runs]
    assert got["copied"] == [got["text"]] and got["type"] == "application/json"
    assert got["name"] == "trips-answers.json" and got["link"] == "blob:1"
    assert got["during"] == 0 and got["revoked"] == ["blob:1"]
    assert "5 of 5" in got["said"] and got["after"] == "" and "4 of 5" in got["counts"]


@needs_node
def test_an_untouched_page_exports_no_answer(tmp_path):
    m = listed(visits=KNOWN)
    got = run(tmp_path, m, "const p = open(); p.byId.download.click(); return p.blobs[0].text;")
    assert load_answers(answers_file(tmp_path, got), m) == {"runs": {}, "cuts": {}}


@needs_node
def test_each_answer_is_stored_on_its_own_and_read_back(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, ANSWER_ALL + """
      const again = open({storage: p.storage});
      again.byId.download.click();
      const held = Object.fromEntries(p.storage.held);
      p.storage.log.length = 0;
      p.press(p.run("Aldmoor"), "4 worked here");
      const touched = [...new Set(p.storage.log.map(([, key]) => key))];
      p.storage.log.length = 0;
      p.type(p.run("Berrow"), "x");
      touched.push(...new Set(p.storage.log.map(([, key]) => key)));
      p.type(p.run("Berrow"), "");
      const left = [...p.storage.held.keys()];
      const other = open({storage: p.storage});
      return [held, again.blobs[0].text, again.field(again.run("Berrow")).value,
              again.byId.kept.textContent, touched, left,
              other.field(other.run("Berrow")).value];
    """)
    words = ANSWERED["runs"][Q_ID]["words"]
    names = dict(zip(stored(m, *[f"run:{r.id}" for r in m.runs], f"words:{Q_ID}",
                            f"cut:{CUT_RQ}", f"cut:{CUT_49}"),
                     ["3", "2", "no", "?", "1", words, "cut", "cut"], strict=True))
    assert got[0] == names
    assert load_answers(answers_file(tmp_path, got[1]), m) == ANSWERED
    assert got[2] == words and "8 answers" in got[3]
    assert got[4] == stored(m, f"run:{R_ID}", f"words:{Q_ID}")
    assert stored(m, f"words:{Q_ID}")[0] not in got[5] and len(got[5]) == 7 and got[6] == ""


@needs_node
def test_answers_of_another_list_are_not_this_lists(tmp_path):
    one, two = listed(visits=MIXED), listed(visits=MIXED, account=2)
    got = run(tmp_path, two, """
      const storage = new Storage();
      for (const k of theirs) storage.held.set(k, "3");
      const p = open({storage});
      p.byId.download.click();
      return [p.pressed().filter((w) => w !== "Same trip"), JSON.parse(p.blobs[0].text).runs];
    """, theirs=stored(one, f"run:{R_ID}", f"words:{Q_ID}"))
    assert got == [[], {}]


@needs_node
@pytest.mark.parametrize("key, junk", [
    *((f"run:{R_ID}", j) for j in ("0", "7", "", " 3", "y", "null", '"3"', "__proto__", "12")),
    (f"run:{S_ID}", "3"), (f"run:{S_ID}", "?"), (f"cut:{CUT_RQ}", "join"), (f"cut:{CUT_RQ}", ""),
])
def test_a_stored_value_that_is_no_answer_is_not_used_and_said(tmp_path, key, junk):
    m = listed(visits=MIXED)
    (name,) = stored(m, key)
    got = run(tmp_path, m, """
      const storage = new Storage();
      storage.held.set(name, junk);
      const p = open({storage});
      p.byId.download.click();
      const raw = JSON.parse(p.blobs[0].text);
      return [raw.runs, raw.cuts, p.byId.unused.hidden, p.byId.unused.textContent,
              storage.held.get(name), p.pressed().filter((w) => w !== "Same trip")];
    """, name=name, junk=junk)
    assert got[:3] == [{}, {}, False] and "could not be read" in got[3]
    assert got[4] == junk and got[5] == []
    assert run(tmp_path, m, "return open().byId.unused.hidden;") is True


@needs_node
def test_what_has_no_control_takes_nothing_from_storage(tmp_path):
    m = listed(visits=KNOWN)
    keys = stored(m, f"run:{NINE_ID}", f"words:{R_ID}", f"words:{S_ID}", f"cut:{CUT_QS}")
    got = run(tmp_path, m, """
      const storage = new Storage();
      for (const k of keys) storage.held.set(k, "cut");
      const p = open({storage});
      const store = logic.openStore(() => storage, data);
      for (const [k, v] of [["run:" + nine, "no"], ["words:" + nine, "x"], ["cut:" + qs, "cut"],
                            ["run:x", "3"], ["__proto__", "3"], ["run:" + nine, ""]]) {
        store.set(k, v);
      }
      p.byId.download.click();
      const raw = JSON.parse(p.blobs[0].text);
      return [raw.runs, raw.cuts, storage.log.filter(([op]) => op !== "getItem"), store.kept()];
    """, keys=keys, nine=NINE_ID, qs=CUT_QS)
    assert got == [{}, {}, [], True]


# -- words on the page


@needs_node
@pytest.mark.parametrize("text, why", [
    ("x" * 501, "501 of 500"), ("\U0001f5fc" * 501, "501 of 500"), ("two\nlines", "line break"),
    ("a‮b", "control character"), (" ​", "nothing that can be read"),
])
def test_words_that_cannot_be_taken_stop_every_way_out_and_say_why(tmp_path, text, why):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, ANSWER_ALL + """
      p.byId.show.click();
      const before = p.byId.text.value.length > 0;
      const box = p.run("Berrow");
      p.type(box, text);
      const out = [];
      for (const id of ["download", "copy", "show"]) {
        p.byId[id].click();
        await null; await null; await null;
        out.push(p.byId.said.textContent);
      }
      const bad = [p.blobs.length, p.copied.length, p.byId.text.value, box.cls("problem")[0].hidden,
                   box.cls("problem")[0].textContent, p.field(box).value,
                   p.field(box).getAttribute("aria-invalid"), p.byId.preview.textContent];
      p.type(box, "as a child");
      p.byId.download.click();
      return [before, out, ...bad, box.cls("problem")[0].hidden, p.blobs.length,
              JSON.parse(p.blobs[0].text).runs[berrow].words,
              p.field(box).getAttribute("aria-invalid")];
    """, text=text, berrow=Q_ID)
    assert got[0] is True
    for said in got[1]:
        assert "Nothing was saved" in said and "Berrow (2)" in said and why in said
    assert got[2:5] == [0, 0, ""]
    assert got[5] is False and why in got[6] and got[7] == text and got[8] == "true"
    assert "needs words of yours" in got[9]  # no trip is shown from words that are not taken
    assert got[10:] == [True, 1, "as a child", "false"]


@needs_node
def test_words_cleared_to_nothing_are_absent_and_no_problem(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, ANSWER_ALL + """
      p.type(p.run("Berrow"), "");
      p.byId.download.click();
      return [JSON.parse(p.blobs[0].text).runs[berrow], p.run("Berrow").cls("problem")[0].hidden];
    """, berrow=Q_ID)
    assert got == [{"a": "2"}, True]


@needs_node
def test_a_field_says_which_reasons_its_words_release(tmp_path):
    obs = [*stay(R, 1, 3), ob(5, Q, hour=9), ob(5, Q, hour=9.1)]
    got = run(tmp_path, listed(obs, UNDATED_Q), """
      const p = open();
      const box = p.run("Berrow"), field = p.field(box);
      const label = box.all((e) => e.tag === "label")[0];
      return [label.textContent, label.getAttribute("for") === field.getAttribute("id"),
              Object.keys(field.attrs), field.value, box.cls("reasons")[0].children.length];
    """)
    for word in ("may not be over", "very short", "may be the visit on your profile"):
        assert word in got[0], word
    assert "change of planes" not in got[0]
    assert got[1] is True and got[3] == "" and got[4] == 3
    assert "placeholder" not in got[2] and "maxlength" not in got[2]


# -- two tabs, and a storage that fails


@needs_node
def test_two_tabs_at_once_on_two_answers_both_count(tmp_path):
    """B's whole press, at each point A's own press can be cut at."""
    m = listed(visits=MIXED)
    rows = run(tmp_path, m, """
      const acts = [
        (p) => p.press(p.run("Aldmoor"), "Clear this answer"),
        (p) => p.type(p.run("Berrow"), "in B's words"),
        (p) => p.press(p.betweens()[0], "A new trip starts here"),
      ];
      const out = [];
      for (const act of acts) {
        for (let at = 0; at < 8; at += 1) {
          const storage = new Storage();
          const first = open({storage});
          first.press(first.run("Aldmoor"), "3 good visit");
          const b = open({storage});
          let n = -1, fired = false;
          const a = open({storage: new View(storage, () => {
            if (n >= 0 && n++ === at) { fired = true; act(b); }
          })});
          n = 0;
          a.press(a.run("Dunvale"), "5 lived here");
          n = -1;
          if (!fired) break;
          const value = (p) => {
            p.byId.download.click();
            const { runs, cuts } = JSON.parse(p.blobs[0].text);
            return [runs, cuts];
          };
          out.push([acts.indexOf(act), value(a), value(b), value(open({storage})),
                    a.byId.kept.textContent]);
        }
      }
      return out;
    """)
    wanted = [[{FOUR_ID: {"a": "5"}}, {}],
              [{R_ID: {"a": "3"}, Q_ID: {"words": "in B's words"}, FOUR_ID: {"a": "5"}}, {}],
              [{R_ID: {"a": "3"}, FOUR_ID: {"a": "5"}}, {CUT_RQ: "cut"}]]
    assert len(rows) >= 12 and {r[0] for r in rows} == {0, 1, 2}
    for which, a, b, again, kept in rows:
        assert a == b == again == wanted[which] and "not being kept" not in kept


@needs_node
def test_a_tab_shows_the_words_and_answers_another_tab_has_stored(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, """
      const storage = new Storage();
      const a = open({storage}), b = open({storage});
      b.type(b.run("Berrow"), "from B");
      b.press(b.run("Aldmoor"), "6 travelguru");
      const before = a.field(a.run("Berrow")).value;
      a.tell("storage", {});
      const after = [a.field(a.run("Berrow")).value, a.pressed(a.run("Aldmoor"))];
      a.type(a.run("Berrow"), "two\\nlines");
      b.tell("pageshow", {persisted: true});
      return [before, ...after, b.run("Berrow").cls("problem")[0].hidden];
    """)
    assert got == ["", "from B", ["6 travelguru"], False]


@needs_node
@pytest.mark.parametrize("storage", [
    "null", "{getItem() { throw 1; }, setItem() { throw 1; }, removeItem() { throw 1; }}",
    "{getItem: () => null, setItem() { throw 1; }, removeItem() {}}",
    "{getItem: () => null, setItem() {}, removeItem() {}}",
    "{getItem() { throw 1; }, setItem() {}, removeItem() {}}",
    "{getItem: () => undefined, setItem() {}, removeItem() {}}",
])
def test_the_page_works_without_storage_and_says_so(tmp_path, storage):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, f"const storage = {storage};" + ANSWER_ALL.replace(
        "open()", "open({storage})") + """
      p.press(p.run("Dunvale"), "Clear this answer");
      p.tell("storage", {});
      p.byId.download.click();
      return [p.blobs[0].text, p.byId.kept.textContent, p.field(p.run("Berrow")).value];
    """)
    runs = {k: v for k, v in ANSWERED["runs"].items() if k != FOUR_ID}
    assert load_answers(answers_file(tmp_path, got[0]), m) == ANSWERED | {"runs": runs}
    assert "not being kept" in got[1] and got[2] == ANSWERED["runs"][Q_ID]["words"]


@needs_node
def test_a_pull_that_cannot_read_keeps_what_was_read_before(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, ANSWER_ALL + """
      const out = [];
      for (const odd of [() => { throw 1; }, () => undefined, () => 0, () => ["3"]]) {
        p.storage.getItem = odd;
        p.tell("storage", {});
        p.byId.download.click();
        out.push([p.blobs.pop().text, p.byId.kept.textContent.includes("not being kept"),
                  p.byId.unused.hidden]);
        delete p.storage.getItem;
        p.tell("storage", {});
        out.push([p.byId.kept.textContent.includes("not being kept")]);
      }
      return out;
    """)
    for (text, lost, unused), (later,) in zip(got[::2], got[1::2], strict=True):
        assert load_answers(answers_file(tmp_path, text), m) == ANSWERED
        assert (lost, unused, later) == (True, True, False)


@needs_node
def test_a_quota_that_fails_one_answer_holds_that_one_and_stores_the_rest(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, """
      const storage = new Storage();
      const full = {on: true};
      storage.setItem = function (k, v) {
        if (full.on && k === words) throw new Error("full");
        Storage.prototype.setItem.call(this, k, v);
      };
      const p = open({storage});
      p.press(p.run("Aldmoor"), "3 good visit");
      p.type(p.run("Berrow"), "as a child");
      storage.held.set(words, "another tab's");
      p.tell("storage", {});
      p.byId.download.click();
      const during = [JSON.parse(p.blobs[0].text).runs, p.byId.kept.textContent,
                      p.field(p.run("Berrow")).value];
      full.on = false;
      p.type(p.run("Berrow"), "as a child, once");
      return [...during, storage.held.get(words), p.byId.kept.textContent];
    """, words=stored(m, f"words:{Q_ID}")[0])
    assert got[0] == {R_ID: {"a": "3"}, Q_ID: {"words": "as a child"}}
    assert "not being kept" in got[1] and got[2] == "as a child"
    assert got[3] == "as a child, once" and "not being kept" not in got[4]


@needs_node
def test_a_clear_that_did_not_take_stays_cleared(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, ANSWER_ALL + """
      p.storage.removeItem = () => {};
      p.press(p.run("Aldmoor"), "Clear this answer");
      p.type(p.run("Berrow"), "");
      p.tell("storage", {});
      p.byId.download.click();
      const { runs } = JSON.parse(p.blobs[0].text);
      return [aldmoor in runs, runs[berrow], p.byId.kept.textContent.includes("not being kept")];
    """, aldmoor=R_ID, berrow=Q_ID)
    assert got == [False, {"a": "2"}, True]


@needs_node
@pytest.mark.parametrize("later", [None, "Clear this answer"])
def test_the_note_after_a_copy_is_about_what_was_copied(tmp_path, later):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, """
      let text = null, done = null;
      const clipboard = {writeText: (t) => new Promise((ok) => { text = t; done = ok; })};
      const p = open({clipboard});
      p.press(p.run("Aldmoor"), "3 good visit");
      p.byId.copy.click();
      const early = p.byId.said.textContent;
      if (later) p.press(p.run("Aldmoor"), later);
      done();
      await null; await null; await null;
      return [early, text, p.byId.said.textContent];
    """, later=later)
    assert got[0] == "" and "Copied" in got[2] and "1 of 5" in got[2]
    assert load_answers(answers_file(tmp_path, got[1]), m)["runs"] == {R_ID: {"a": "3"}}
    assert ("older than this page" in got[2]) is bool(later)


@needs_node
@pytest.mark.parametrize("clipboard", ["absent", "refused"])
def test_without_a_clipboard_the_text_is_there_to_select(tmp_path, clipboard):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, """
      const p = open({clipboard});
      const before = p.byId.text.hidden;
      p.byId.copy.click();
      await null; await null; await null;
      const shown = [p.byId.text.hidden, p.byId.text.value];
      p.press(p.run("Aldmoor"), "3 good visit");
      return [before, ...shown, p.byId.text.value];
    """, clipboard=clipboard)
    assert got[:2] == [True, False]
    assert load_answers(answers_file(tmp_path, got[2]), m)["runs"] == {}
    assert load_answers(answers_file(tmp_path, got[3]), m)["runs"] == {R_ID: {"a": "3"}}


# -- what it says


@needs_node
def test_the_preview_is_the_trips_and_why_the_rest_is_not_made(tmp_path):
    m = listed(visits=MIXED)
    got = run(tmp_path, m, "const at = [open().byId.preview.textContent];" + ANSWER_ALL + """
      const read = () => [p.byId.preview.cls("trip").map((t) => t.textContent),
                          p.byId.preview.cls("not-made").map((t) => t.textContent),
                          p.byId.preview.cls("left-out").map((t) => t.textContent)];
      at.push(read());
      p.press(p.run("Dunvale"), "1 transit");
      p.press(p.betweens()[0], "Same trip");
      at.push(read(), p.byId.counts.textContent);
      return at;
    """)
    assert "is not answered" in got[0]
    trips_, held, left = got[1]
    assert len(trips_) == 2 and "2026-05-01 to 2026-05-03" in trips_[0]
    assert "Aldmoor (1), 2026-05-01 to 2026-05-03: 3 good visit" in trips_[0]
    assert "Fenmark (9), 2026-05-13 to 2026-05-14: 1 transit" in trips_[1]
    assert "stored, but does not mark the region" in trips_[1]
    assert "stored" not in trips_[0]
    (held,) = held
    assert "Dunvale (4), 2026-05-09 to 2026-05-10 is answered with a question mark" in held
    assert not re.search(r"\d+:2026-", held)  # no run ids, which nobody reads
    assert any("Cray Islands (3)" in t for t in left)
    trips_, held, _ = got[2]
    assert len(trips_) == 2 and held == [] and "2026-05-01 to 2026-05-10" in trips_[0]
    assert "the other one was as a child" in trips_[0]
    assert "2 trips with 4 regions" in got[3]


@needs_node
def test_the_preview_is_what_group_makes_of_the_file(tmp_path):
    m = listed(visits=MIXED)
    text, page = run(tmp_path, m, ANSWER_ALL + """
      p.byId.download.click();
      return [p.blobs[0].text, logic.group(data, JSON.parse(p.blobs[0].text))];
    """)
    ans = load_answers(answers_file(tmp_path, text), m)
    assert page == as_text(*group(m.runs, m.boundaries, ans))


@needs_node
def test_a_run_shows_its_facts(tmp_path):
    obs = [ob(1, hour=9.5, zone=-3.5, lon=-52.0), ob(3, hour=20, zone=5.75, lon=85.0),
           ob(5, hour=20, zone=0, lon=0.0), ob(6, hour=20, zone=0, lon=0.0), *stay(Q, 8, 9)]
    visits = {R: [visit(2013, None, id=71, quality=4, trip_id=9), visit(25, 26, id=72, quality=0)]}
    m = listed(obs, visits, trips=[TRIP, header(None, "2026-05-01", id=4)])
    first, second = run(tmp_path, m, "return open().runs().map((r) => r.textContent);")
    for fact in ("Days with photos: 2026-05-01, 2026-05-03, 2026-05-05 to 2026-05-06",
                 "Days with no photo: 2026-05-02, 2026-05-04", "4 photos",
                 "First: 2026-05-01 09:30, clock UTC-3:30", "Last: 2026-05-06 20:00, clock UTC",
                 "2013 to no date, 4 worked here, in trip 9",
                 "2026-05-25 to 2026-05-26, no visit",
                 "hills", "2026-05-03 to 2026-05-04", "no date to 2026-05-01"):
        assert fact in first, fact
    assert "Days with no photo" not in second and "hills" not in second
    assert "No visit to this region is on your profile" in second
    assert "by the photo" not in first or "clock" in first


@needs_node
def test_every_reason_and_flag_is_said_in_plain_words(tmp_path):
    texts = {
        "open: no photo on a later day shows you in another region": "may not be over",
        "brief: one photo": "one photo",
        "layover?: under 24 hours, with a fast photo before it and after it": "change of planes",
        "undated: may be visit 7, with no dates": "visit 7, with no dates",
        "overlap: shares a day with visit 7, 2026-05-10 to 2026-05-11": "It shares a day with",
        f"continuation: no photo shows that you left between it and run {R_ID}":
            "the photos in Aldmoor (1), 2026-05-01 to 2026-05-03",
        "not read: a map tile on 2026-05-02": "could not be read",
        "stale map: a region not in the catalogue on 2026-05-02": "not in the list of regions",
        "across: it starts before --since 2026-04-20": "before the first day you asked for",
        "across: it ends after --until 2026-05-30": "after the last day you asked for",
        "clock: its photos have two clocks over 12 hours apart": "12 hours apart",
        "there: visit 7, 2026-05-10 to 2026-05-11": "on your profile already",
        "conflict": "impossible", "evidence thin": "few", "fast near": "travel speed",
        "track: 2026-05-12 in region 2, 2026-05-13 in region 77, 2026-05-14 in no known region":
            "2026-05-12 in Berrow (2), 2026-05-13 in region 77, 2026-05-14 in no known region",
        "fast: a photo between them was taken at speed": "at speed",
        "fast: over 150 km/h from one to the other": "150 km/h",
        "gap: nothing from 2026-05-10 to 2026-05-13": "2026-05-10 to 2026-05-13",
        "profile: one of them is on the profile already": "on your profile already",
    }
    assert {kind_of(t) for t in texts} == (
        trips.RELEASED_BY_WORDS | trips.NEVER_RELEASED | {"there"}) - {"not judged"} | {
        "conflict", "evidence thin", "fast near", "track", "fast", "gap", "profile"}
    assert {kind_of(t) for t in texts} == trip_list._KINDS
    got = run(tmp_path, small(), "return texts.map((t) => logic.plainText(data, t));",
              texts=list(texts))
    for (raw, want), text in zip(texts.items(), got, strict=True):
        assert want in text, raw
        assert text != raw and "--" not in text and not re.search(r"\d:2026", text), raw
    # Every text that the planner itself can give is one of these, word for word.
    source = Path(trips.__file__).read_text(encoding="utf-8")
    for raw in texts:
        assert re.split(r" visit| run| \d|,| --|: ?\d", raw)[0] in source, raw


@needs_node
def test_the_header_says_what_was_read_and_what_was_left_out(tmp_path):
    m = small(sources=[PHOTOS, TRACK, SPANS], no_track=False, params=PARAMS | {"spans": True},
              trips=[TRIP], coverage=COVERAGE | {"tiles_unread": 3, "unread_photos": 5})
    read = """
      const p = open();
      return [p.byId.about.textContent, p.byId.sources.textContent,
              p.byId["left-out"].children.map((c) => c.textContent),
              p.byId.gaps.children.map((r) => r.children.map((c) => c.textContent)),
              p.byId.incomplete.textContent, p.byId.incomplete.hidden, p.order[0]];
    """
    about, sources, out, gaps, stop, hidden, first = run(tmp_path, m, read)
    for word in ("Account 1", "2026-04-20", "2026-05-30", "2 days", "150 km/h", m.sha256[:12],
                 "12 map tiles read, 3 not read", "2 stretches"):
        assert word in about, word
    for word in ("Photos library", "2019-03-02 to 2026-06-20", "4321", "days.csv",
                 "2012-01-01 to 2018-12-30", "spans.json"):
        assert word in sources, word
    assert "no day-by-day track" not in sources
    for line, n, word in zip(out, LEFT_OUT.values(), (
            "another camera", "no absolute time", "not to use", "impossible jump", "travel speed",
            "layover", "could not be read", "list of regions", "no land", "beside a region",
            "near the edge", "clock"), strict=True):
        assert line.startswith(f"{n} photos ") and word in line, word
    assert gaps == [["Days", "Stretches", "Cuts to answer", "Held"], ["1", "6", "2", "1"],
                    ["2 (this list)", "5", "1", "2"]]
    assert hidden is False and first == "incomplete"
    for word in ("This list is incomplete", "3 of 15 map tiles", "5 photos", "again"):
        assert word in stop, word
    about, sources, _, _, stop, hidden, _ = run(
        tmp_path, small(params=PARAMS | {"until": None}), read)
    assert (stop, hidden) == ("", True) and "the newest one" in about
    assert "no day-by-day track" in sources


@needs_node
def test_a_page_with_nothing_in_it_says_so(tmp_path):
    got = run(tmp_path, listed([]), """
      const p = open();
      return [p.byId.list.textContent, p.byId.preview.textContent, p.byId.start.hidden];
    """)
    assert "Nothing" in got[0] and "No trip" in got[1] and got[2] is True


@needs_node
@pytest.mark.parametrize("code", [*sorted(_CONTROLS)[::7], 0x202E, 0x2066, 0x61C], ids=hex)
def test_controls_are_taken_out_of_every_text_shown(tmp_path, code):
    bad = chr(code)
    m = listed(visits=MIXED, names=NAMES | {R: f"Ald{bad}moor", Q: bad},
               trips=[TRIP | {"description": f"hi{bad}lls"}],
               sources=[PHOTOS | {"label": f"lib{bad}rary"}], created=f"to{bad}day")
    got = run(tmp_path, m, """
      const p = open();
      p.press(p.run("Aldmoor"), "3 good visit");
      p.type(p.run("Region 2"), "mine" + bad);
      return [p.order.filter((id) => id !== "data").map((id) => p.byId[id].textContent).join(""),
              p.runs().map((r) => r.cls("name")[0].textContent), data.runs[0].name];
    """, bad=bad)
    assert bad not in got[0] and "hills" in got[0] and "library" in got[0] and "today" in got[0]
    assert got[1][0].startswith("Aldmoor (1), ") and got[1][1].startswith("Region 2, ")
    assert got[2] == f"Ald{bad}moor"  # what is hashed is as it was


@needs_node
def test_hostile_text_is_text_wherever_it_is_shown(tmp_path):
    words = "</textarea><script>boom()</script>"
    got = run(tmp_path, hostile(), """
      const p = open();
      const box = p.runs()[1];
      p.press(p.runs()[0], "3 good visit");
      p.press(box, "2 minimal visit");
      p.type(box, words);
      p.press(p.runs()[2], "Leave it out");
      p.byId.show.click();
      return [p.runs()[0].cls("name")[0].textContent, p.runs()[0].textContent,
              p.byId.preview.textContent, p.byId.text.value];
    """, words=words)
    plain = HOSTILE.replace("‮", "")
    assert got[0].startswith(plain) and plain in got[1] and plain in got[2]
    assert json.loads(got[3])["runs"][Q_ID]["words"] == words


@needs_node
def test_names_are_isolated_and_groups_of_buttons_are_named(tmp_path):
    got = run(tmp_path, listed(visits=MIXED), """
      const p = open();
      const ids = p.runs().map((r) => r.cls("name")[0].getAttribute("id"));
      return [p.runs().map((r) => r.cls("name")[0].getAttribute("dir")), new Set(ids).size,
              p.runs().map((r, n) => r.all((e) => e.getAttribute("role") === "group")
                .map((g) => g.getAttribute("aria-labelledby") === ids[n])),
              p.betweens().map((b) => b.all((e) => e.getAttribute("role") === "group")
                .map((g) => Boolean(g.getAttribute("aria-label")))),
              p.field(p.run("Berrow")).getAttribute("dir")];
    """)
    assert got[0] == ["auto"] * 5 and got[1] == 5
    assert got[2] == [[True]] * 5 and got[3] == [[True]] * 4 and got[4] == "auto"
    style = render_page(small()).split("<style>")[1].split("</style>")[0]
    assert re.search(r"^\.name {[^}]*unicode-bidi: isolate", style, re.M)


@needs_node
def test_the_counts_are_said_once_and_the_bar_hides_nothing(tmp_path):
    page = DATA.sub("", render_page(small()))
    (region,) = re.findall(r'<div id="news" aria-live="polite" aria-atomic="false">(.*?)</div>',
                           page, re.S)
    assert all(f'<p id="{i}"' in region for i in ("counts", "said", "kept"))
    body = page.split("<body>")[1]
    assert body.lstrip().startswith('<div id="incomplete"')
    assert body.index('id="bar"') > body.index("</main>") and "position: fixed" not in page
    got = run(tmp_path, listed(visits=MIXED), """
      const p = open();
      const writes = () => [p.byId.counts.writes, p.byId.said.writes, p.byId.kept.writes];
      const out = [writes()];
      p.press(p.run("Aldmoor"), "3 good visit"); out.push(writes());
      p.press(p.run("Aldmoor"), "3 good visit"); out.push(writes());
      p.byId.download.click(); out.push(writes());
      p.tell("storage", {}); out.push(writes());
      const first = p.root.style.scrollPaddingBottom;
      p.byId.bar.offsetHeight = 333;
      p.resized();
      return [out, first, p.root.style.scrollPaddingBottom,
              open({observer: false}).byId.start.hidden];
    """)
    (c, s, k), pressed, again, saved, told = got[0]
    assert pressed == again == [c + 1, s, k] and saved == told == [c + 1, s + 1, k]
    assert got[1:] == ["100px", "333px", True]


def test_the_list_speaks_to_nobody_and_writes_only_where_it_is_told():
    tree = ast.parse(Path(trip_list.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"NomadMania", "Transport", "today", "now", "open", "urlopen"}
