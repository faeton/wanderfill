"""Tests for trip runs: which days are one stay, which stays are offered, and which never.

A run that is offered becomes a visit after one tired click, on a profile other people
check. Every rule here is a day, a stay or a second copy that did not happen.
"""

from __future__ import annotations

import ast
import datetime as dt
import itertools
import random
import time
from pathlib import Path

import pytest

from wanderfill.api.client import Visit, YearOnly, _date_parts
from wanderfill.api.errors import PlanInvalid
from wanderfill.geo.resolve import Resolution
from wanderfill.geo.tiles import haversine_km
from wanderfill.plan import trips
from wanderfill.plan.review import Shot
from wanderfill.plan.trips import (
    Boundary,
    Obs,
    Place,
    Run,
    boundaries,
    evidence_grade,
    find_runs,
    group,
    judge,
    kind_of,
    place,
    regions_to_read,
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


@pytest.mark.parametrize("lat, lon", [(90.0, 0.0), (-90.0, 0.0), (0.0, 180.0), (-17.0, -180.0)])
def test_the_points_around_a_photo_are_on_the_planet(lat, lon):
    asked = []

    def resolve(y, x):
        asked.append((y, x))
        return Resolution(y, x, R, "tile_interior")

    place(resolve, lat, lon)
    assert all(-90 <= y <= 90 and -180 <= x <= 180 for y, x in asked)
    assert all(haversine_km(lat, lon, y, x) < 0.2 for y, x in asked)


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
    ((ob(8, Q, 0.1),), True),
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
    ((ob(16, Q, 23.9),), True),
    ((ob(15, Q),), True),
    ((ob(14, Q, 18.1),), True),
    ((ob(14, Q, 18.0),), False), ((ob(14, Q, 17.9),), False),
])
def test_between_the_run_and_a_visit_after_it(more, apart):
    want = ("offered", set()) if apart else ("fixed", {"continuation"})
    assert held_by(visit(17, 18), settled(*more)) == want


def test_a_later_date_across_the_line_may_be_before_the_visit_ended():
    """Q at +13 on the 8th, at one in the morning, is R's 7th at -10 at two."""
    obs = [*stay(R, 10, 14, zone=-10, lon=-155.0), *stay(Q, 20, 22, zone=-10, lon=-155.0)]
    early = ob(8, Q, 1, zone=13, lon=178.0)
    late = ob(8, Q, 23.9, zone=13, lon=178.0)
    assert held_by(visit(5, 7), [*obs, early])[1] == {"continuation"}
    assert held_by(visit(5, 7), [*obs, late])[0] == "offered"
    # And after the run: Q on the 16th at -10 is R's 17th at +13 already.
    obs = [*stay(Q, 2, 4), *stay(R, 10, 14, zone=13, lon=178.0),
           *stay(Q, 20, 22, zone=13, lon=178.0)]
    assert held_by(visit(17, 18), [*obs, ob(16, Q, 9, zone=-10, lon=-155.0)])[1] \
        == {"continuation"}
    assert held_by(visit(17, 18), [*obs, ob(16, Q, 0.5, zone=-10, lon=-155.0)])[0] == "offered"


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


def test_a_year_that_holds_a_part_of_the_run_is_a_continuation():
    """It cannot be the whole of it, and nothing was seen elsewhere between."""
    jan = dt.date(2027, 1, 2)
    obs = [ob(dt.date(2026, 12, 30), R, 9), ob(dt.date(2026, 12, 31), R), ob(jan, R, 18),
           ob(dt.date(2027, 1, 1), state="edge"), ob(dt.date(2026, 12, 1), Q),
           ob(dt.date(2027, 1, 9), Q)]
    assert held_by(visit(2026, 2026), obs) == ("fixed", {"continuation"})
    assert held_by(visit(2027, None), obs) == ("fixed", {"continuation"})


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
