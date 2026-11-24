"""Tests for series candidates: what may be offered, and what must never be.

A wrong offer becomes a false tick after one tired click, on a profile other
people check. Every rule here is a photo that looked like a visit and was not:
somebody else's, one from an aircraft, one with a frozen or a spoofed coordinate.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random

import pytest

from wanderfill.api.errors import PlanInvalid
from wanderfill.geo.tiles import SeriesPoint, deg2tile, haversine_km
from wanderfill.plan import review
from wanderfill.plan.model import Op, Plan, validate
from wanderfill.plan.review import (
    SKIP_SERIES,
    SeriesInfo,
    Shot,
    Span,
    count_skipped,
    find_candidates,
    grade_shots,
    harvest,
    left_out,
    load_spans,
    recall,
)
from wanderfill.sources.photos_app import PhotoAsset

UTC = dt.timezone.utc
NOON = dt.datetime(2026, 5, 1, 12, tzinfo=UTC)
KM = 1 / haversine_km(0.0, 0.0, 1.0, 0.0)  # degrees of latitude in one kilometre
CAMERA = "com.apple.camera"


def photo(lat, lon, minutes=0.0, *, zone=0.0, importer=CAMERA, timed=True, uuid=""):
    """One photo, `minutes` after noon UTC, by a clock `zone` hours ahead of UTC."""
    utc = NOON + dt.timedelta(minutes=minutes)
    return PhotoAsset(
        uuid=uuid or f"{lat},{lon},{minutes}",
        taken=(utc + dt.timedelta(hours=zone)).replace(tzinfo=None),
        lat=lat, lon=lon, directory="0", filename="x.heic", importer=importer,
        taken_utc=utc if timed else None,
    )


def kinds(*photos, spans=(), **kw):
    return [s.kind for s in grade_shots(photos, spans, **kw)]


def shot(lat, lon, day=1, *, kind="ground", use="all", origin="own-camera", minute=0):
    taken = dt.datetime(2026, 5, day, 12, minute)
    return Shot(taken, taken.replace(tzinfo=UTC), lat, lon, origin, kind, use)


def obj(lat, lon, item=1, series=22, name="from the tile"):
    return {(series, item): SeriesPoint(item, series, name, lat, lon)}


def catalogue(objects, **names):
    out = {}
    for series, item in objects:
        out.setdefault(series, SeriesInfo(f"Series {series}", {})).items[item] = f"Object {item}"
    return out


def offered(shots, objects, ticked=None, **kw):
    cat = catalogue(objects)
    return find_candidates(shots, objects, cat, dict.fromkeys(cat, ()) | (ticked or {}), **kw)


def span(west=-180.0, south=-90.0, east=180.0, north=90.0, *, first=1, last=31, use="none"):
    return Span(dt.date(2026, 5, first), dt.date(2026, 5, last), west, south, east, north,
                "spoofed", use)


class FakeReader:
    """Series objects by the tile they are in; `broken` tiles never arrive."""

    def __init__(self, points=(), broken=(), zoom=9):
        self.points, self.broken, self.asked = list(points), set(broken), []
        self.failed = {("series2", 9, 400, 400), ("regions", 9, 300, 200)}  # an earlier run's
        self.zoom = zoom

    def series_points(self, z, x, y):
        self.asked.append((z, x, y))
        if (x, y) in self.broken:
            self.failed.add(("series2", z, x, y))
            return
        n = 2**z
        for p in self.points:
            px, py = deg2tile(p.lat, p.lon, z)
            if (px % n, py) == (x, y):
                yield p


# ------------------------------------------------------------------ decision 1: what is offered


def test_an_object_near_a_ground_photo_is_offered_with_its_facts():
    shots = [shot(10.0, 20.0 + 0.5 * KM), shot(10.0, 20.0 + 0.8 * KM, minute=5),
             shot(10.0, 20.0 - 0.9 * KM, day=3), shot(10.0, 20.0 + 5 * KM, day=4)]
    (c,) = offered(shots, obj(10.0, 20.0))
    assert (c.series, c.item, c.series_name, c.name) == (22, 1, "Series 22", "Object 1")
    assert (c.lat, c.lon) == (10.0, 20.0)
    assert c.km == haversine_km(10.0, 20.0, 10.0, 20.0 + 0.5 * KM)
    assert c.at == dt.datetime(2026, 5, 1, 12)
    assert (c.photos, c.days, c.origins) == (3, 2, ("own-camera",))
    assert (c.fast_near, c.conflict, c.thin) == (0, False, False)


def test_the_radius_is_two_kilometres_unless_said_and_is_inclusive():
    objects = obj(10.0, 20.0)
    assert offered([shot(10.0 + 1.99 * KM, 20.0)], objects)
    assert not offered([shot(10.0 + 2.01 * KM, 20.0)], objects)
    assert offered([shot(10.0 + 2.01 * KM, 20.0)], objects, radius_km=2.5)
    at = shot(10.0 + 1.5 * KM, 20.0)
    edge = haversine_km(10.0, 20.0, at.lat, at.lon)
    assert offered([at], objects, radius_km=edge)
    assert not offered([at], objects, radius_km=math.nextafter(edge, 0))


@pytest.mark.parametrize("radius", [-1.0, float("nan"), float("inf")])
def test_a_radius_that_is_not_a_distance_is_refused(radius):
    """Compared with a NaN every distance is out of range, and the page would be empty."""
    with pytest.raises(ValueError, match="radius"):
        offered([shot(10.0, 20.0)], obj(10.0, 20.0), radius_km=radius)
    with pytest.raises(ValueError, match="radius"):
        harvest([shot(10.0, 20.0)], FakeReader(), radius_km=radius)


def test_thin_is_one_photo_or_farther_than_a_kilometre():
    near, far = shot(10.0 + 0.9 * KM, 20.0), shot(10.0 + 1.1 * KM, 20.0)
    objects = obj(10.0, 20.0)
    assert offered([near], objects)[0].thin
    assert offered([far, far], objects)[0].thin
    assert not offered([near, far], objects)[0].thin


@pytest.mark.parametrize("ticked", [{22: {1}}, {22: {"1"}}, {"22": {"1"}}, {22: ["1"]}])
def test_a_ticked_object_is_not_offered_however_its_id_is_spelled(ticked):
    """The legacy surface answers ids as text as readily as numbers."""
    objects = obj(10.0, 20.0)
    assert not find_candidates([shot(10.0, 20.0)], objects, catalogue(objects), ticked)
    assert offered([shot(10.0, 20.0)], obj(10.0, 20.0), {22: {2}, 23: {1}})


def test_an_object_outside_the_catalogue_is_not_offered():
    objects = obj(10.0, 20.0) | obj(10.0, 20.0, item=2) | obj(10.0, 20.0, series=30)
    cat = {22: SeriesInfo("WHS", {2: "Two"})}
    assert [c.item for c in find_candidates([shot(10.0, 20.0)], objects, cat, {22: ()})] == [2]


def test_a_catalogue_series_with_no_ticked_list_is_refused():
    """A list that was not read is not an empty one: everything ticked would be offered."""
    objects = obj(10.0, 20.0) | obj(10.0, 20.0, series=30)
    cat = catalogue(objects)
    with pytest.raises(PlanInvalid, match="30"):
        find_candidates([shot(10.0, 20.0)], objects, cat, {22: {1}})
    with pytest.raises(PlanInvalid, match="30"):
        recall([shot(10.0, 20.0)], objects, {22: {1}}, catalogue=cat)
    # Objects of a series the catalogue does not list need no list.
    assert find_candidates([shot(10.0, 20.0)], objects, {22: cat[22]}, {"22": ()})
    assert list(recall([shot(10.0, 20.0)], objects, {"22": ()}, catalogue={22: cat[22]})) == [22]


def test_one_item_id_in_two_series_is_two_candidates():
    objects = obj(10.0, 20.0, series=22) | obj(10.0, 20.0, series=30)
    got = offered([shot(10.0, 20.0)], objects, {22: {1}})
    assert [(c.series, c.item) for c in got] == [(30, 1)]
    assert [c.series for c in offered([shot(10.0, 20.0)], objects)] == [22, 30]


def test_a_skipped_series_is_not_offered_and_is_counted():
    assert frozenset({-2, 12, 17, 60}) == SKIP_SERIES
    objects = {}
    for n, series in enumerate([*SKIP_SERIES, 22]):
        objects |= obj(10.0, 20.0, item=n, series=series)
        objects |= obj(50.0, 20.0, item=100 + n, series=series)  # no photo near
    objects |= obj(10.0, 20.0, item=50, series=12) | obj(10.0, 20.0, item=51, series=12)
    shots = [shot(10.0, 20.0)]
    assert [c.series for c in offered(shots, objects)] == [22]
    assert count_skipped(shots, objects, {12: {"51"}}) == {-2: 1, 12: 2, 17: 1, 60: 1}
    assert count_skipped([shot(10.0, 20.0, kind="fast")], objects, {}) == {}
    assert [c.series for c in offered(shots, objects, skip=frozenset({22}))] == [-2, 12, 12, 12,
                                                                               17, 60]


def test_order_is_by_series_distance_and_item_whatever_order_things_came_in():
    shots = [shot(10.0, 20.0, minute=m) for m in range(3)] + [shot(10.0, 20.0 + KM, day=2)]
    objects = {}
    for series in (30, 22):
        for item, km in ((9, 0.1), (3, 0.1), (5, 0.0), (1, 1.5)):
            objects |= obj(10.0 + km * KM, 20.0, item=item, series=series)
    want = offered(shots, objects)
    assert [(c.series, c.item) for c in want] == [(s, i) for s in (22, 30) for i in (5, 3, 9, 1)]
    rng = random.Random(1)
    for _ in range(5):
        pairs = list(objects.items())
        rng.shuffle(pairs)
        mixed = rng.sample(shots, len(shots))
        assert offered(mixed, dict(pairs)) == want


# ------------------------------------------------------------------ decision 2: origins


def test_every_candidate_names_the_origins_of_its_proof_and_of_nothing_else():
    shots = [shot(10.0, 20.0, origin="unknown"), shot(10.0, 20.0, origin="airdrop"),
             shot(10.0, 20.0, origin="unknown", minute=1),
             shot(10.0, 20.0, origin="other", kind="fast")]
    assert offered(shots, obj(10.0, 20.0))[0].origins == ("airdrop", "unknown")


def test_a_shot_carries_where_the_photo_came_from():
    got = grade_shots([photo(10.0, 20.0, importer="com.apple.sharingd"),
                       photo(10.0, 20.0, 1, importer=None)], [])
    assert [s.origin for s in got] == ["airdrop", "unknown"]


# ------------------------------------------------------------------ decision 3: fast photos


def test_a_flight_is_fast_at_both_ends_and_a_walk_is_not():
    walk = [photo(10.0, 20.0, -240), photo(10.005, 20.0, -230)]
    flight = [photo(12.0, 20.0, 40), photo(14.0, 20.0, 70)]
    assert kinds(*walk, *flight) == ["ground", "ground", "fast", "fast"]
    assert kinds(*walk, photo(10.005, 20.0, 10), *flight) == ["ground"] + ["fast"] * 4
    assert kinds(*walk) == ["ground", "ground"]


def test_every_photo_of_a_burst_from_a_window_is_fast():
    """Two seconds at 900 km/h are 500 metres, and no leg of the burst is a kilometre.
    Leg by leg everything inside it was jitter, and the ground."""
    burst = [photo(10.0 + 0.5 * KM * i, 20.0, i / 30) for i in range(8)]
    assert kinds(*burst) == ["fast"] * 8
    # Too short to reach a kilometre by itself, it is judged by what is around it.
    before, after = photo(10.0 - 20 * KM, 20.0, -1.5), photo(10.0 + 21 * KM, 20.0, 1.6)
    assert kinds(before, *burst[:2], after) == ["fast"] * 4
    # ...as is one with other bursts around it, whichever side they are on.
    assert kinds(before, *burst[:2]) == kinds(*burst[:2], after) == ["fast"] * 3
    assert kinds(*burst[:2]) == ["ground"] * 2  # nothing says otherwise: an inherent limit


def test_a_frozen_run_inside_a_burst_is_one_place():
    """Left from its last photo and entered at its first, at any distance along the order."""
    run = [photo(10.0, 20.0, m) for m in (0, 30, 60)]
    step = photo(10.0 + 0.5 * KM, 20.0, 60.5)
    far = photo(10.0 + 10 * KM, 20.0, 62)
    assert kinds(*run, step, far) == ["fast"] * 5  # 10 km in 2 min, not in 62
    back = photo(10.0 - 0.5 * KM, 20.0, -0.5)
    assert kinds(photo(10.0 - 10 * KM, 20.0, -2), back, *run) == ["fast"] * 5


def test_a_long_stay_is_fast_only_near_the_landing():
    """The photos near the landing may be; one hours later is slow from the aircraft,
    and nothing taken later in the stay changes an earlier photo of it."""
    air = [photo(10.0 - 400 * KM, 20.0, -45), photo(10.0 - 100 * KM, 20.0, -15)]
    stay = [photo(10.0 + 0.3 * KM * (i % 2), 20.0 + i * 1e-6, 10 * i) for i in range(60)]
    alone = kinds(*air, *stay)
    first = alone.index("ground")
    assert alone == ["fast"] * first + ["ground"] * (62 - first)
    assert first == 2 + 4  # 100 km since -15 min is under 120 km/h from 35 min on
    walk = [photo(10.0 + 3 * KM, 20.0, 640), photo(10.0, 20.0, 700)]
    assert kinds(*air, *stay, *walk) == [*alone, "ground", "ground"]


@pytest.mark.parametrize("place", [(10.0, 20.0), (-5.5, 179.9999), (70.0, 25.0)])
def test_a_day_of_photos_in_one_place_is_not_quadratic(monkeypatch, place):
    calls = []
    monkeypatch.setattr(review, "haversine_km", lambda *a: calls.append(1) or haversine_km(*a))
    rng = random.Random(4)

    def near():
        east = rng.uniform(-0.17, 0.17) * KM / math.cos(math.radians(place[0]))
        return place[0] + rng.uniform(-0.17, 0.17) * KM, (place[1] + east + 180) % 360 - 180

    stay = [photo(*near(), 24 * 60 * i / 5000) for i in range(5000)]
    assert kinds(*stay) == ["ground"] * 5000
    assert len(calls) < 5000 * 60  # every pair is 25,000,000
    away = photo(place[0] - 5.0, place[1], -24 * 60)
    calls.clear()
    assert kinds(away, *stay) == ["ground"] * 5001
    assert len(calls) < 5000 * 60


def test_the_search_finds_what_a_plain_scan_finds():
    rng = random.Random(5)
    for spread in (0.3, 0.8, 1.2, 5.0):
        for place in ((10.0, 20.0), (-5.5, 180.0), (89.99, 0.0)):
            at = [(min(90.0, place[0] + rng.uniform(-spread, spread) * KM),
                   (place[1] + rng.uniform(-spread, spread) * KM + 180) % 360 - 180)
                  for _ in range(70)]
            want = [next((m for m in range(n + 1, 70) if haversine_km(*at[n], *at[m]) >= 1.0),
                         None) for n in range(70)]
            assert review._next_far(at, 1.0) == want
    assert review._next_far([], 1.0) == [] and review._next_far([(1, 2)], 1.0) == [None]


def test_one_leg_is_enough_at_either_end_of_the_library():
    """The first and the last photo have one leg, and a lone photo has none."""
    assert kinds(photo(10.0, 20.0), photo(11.0, 20.0, 30)) == ["fast", "fast"]
    assert kinds(photo(10.0, 20.0)) == ["ground"]
    assert kinds() == []


def test_two_photos_four_seconds_and_sixty_metres_apart_are_ground():
    """54 km/h on paper; at 400 metres it is 360 km/h, and still jitter."""
    for km in (0.06, 0.4, 0.999):
        assert kinds(photo(10.0, 20.0), photo(10.0 + km * KM, 20.0, 4 / 60)) == ["ground"] * 2
    assert kinds(photo(10.0, 20.0), photo(10.0 + 1.001 * KM, 20.0, 4 / 60)) == ["fast"] * 2


def test_the_speed_must_be_over_120_and_the_thresholds_can_be_moved():
    a, b = photo(10.0, 20.0), photo(10.0 + 60 * KM, 20.0, 30)
    assert kinds(a, photo(10.0 + 59.9 * KM, 20.0, 30)) == ["ground"] * 2
    assert kinds(a, photo(10.0 + 60.1 * KM, 20.0, 30)) == ["fast"] * 2
    assert kinds(a, b, fast_kmh=100.0) == ["fast"] * 2
    assert kinds(a, b, fast_kmh=100.0, min_leg_km=61.0) == ["ground"] * 2


@pytest.mark.parametrize("lat,lon", [(10.123456, 20.123456), (10.1234, 20.1234)])
def test_a_frozen_run_before_a_jump_is_fast_every_photo_of_it(lat, lon):
    """A phone can repeat the last coordinate of the ground for hours in flight, and all
    but the last photo of it sits there at 0 km/h. Four decimals repeat as well as six."""
    frozen = [photo(lat, lon, 15 * i) for i in range(12)]
    jump = photo(lat + 200 * KM, lon, 15 * 11 + 20)
    after = photo(lat + 200.5 * KM, lon, 15 * 11 + 120)
    assert kinds(*frozen, jump, after) == ["fast"] * 13 + ["ground"]
    # ...and a jump into a run marks the run, timed to its first photo.
    before = photo(lat - 200 * KM, lon, -20)
    assert kinds(before, *frozen) == ["fast"] * 13
    # 100 km in 50 minutes since the last photo, not in the 2 h 45 since the first.
    assert kinds(*frozen, photo(lat + 120 * KM, lon, 15 * 11 + 50)) == ["fast"] * 13
    assert kinds(*frozen) == ["ground"] * 12


def test_a_run_is_consecutive_so_coming_back_is_another_place():
    home = (10.123456, 20.123456)
    got = kinds(photo(*home, 0), photo(10.14, 20.14, 60), photo(*home, 120),
                photo(13.0, 20.0, 150))
    assert got == ["ground", "ground", "fast", "fast"]


def test_legs_are_taken_before_any_span_or_until_or_place():
    """Leaving the aircraft's other end out would make the one left look like a walk."""
    a, b = photo(10.0, 20.0), photo(12.0, 20.0, 30)
    far = span(15.0, 11.0, 25.0, 13.0)
    assert [(s.kind, s.use) for s in grade_shots([a, b], [far])] == [("fast", "all"),
                                                                     ("fast", "none")]
    late = photo(12.0, 20.0, 30, zone=13)  # the next day, by its own clock
    got = grade_shots([a, late], [], until=dt.date(2026, 5, 1))
    assert [(s.kind, s.use) for s in got] == [("fast", "all"), ("fast", "none")]


def test_a_date_line_flight_is_ordered_by_utc():
    """Flying east over it, the landing is earlier on the clock than the departure was."""
    leave = photo(-5.0, 175.0, -300, zone=12)
    later = photo(-5.004, 175.0, 10, zone=12)
    land = photo(-5.5, -179.9, 40, zone=-11)
    assert land.taken < leave.taken
    got = grade_shots([land, later, leave], [])
    assert [(s.lat, s.kind) for s in got] == [(-5.0, "ground"), (-5.004, "fast"),
                                              (-5.5, "fast")]


def test_crossing_the_antimeridian_on_foot_is_not_a_flight():
    assert kinds(photo(-5.5, 179.9, -60), photo(-5.5, -179.9, 10)) == ["ground"] * 2


def test_the_grade_does_not_depend_on_the_order_the_photos_came_in():
    photos = [photo(10.0, 20.0, 0, uuid="a"), photo(10.0, 20.0, 0, uuid="b"),
              photo(10.001, 20.0, 0), photo(10.0, 20.0, 5), photo(14.0, 20.0, 30),
              photo(1.0, 2.0, 7, timed=False), photo(3.0, 2.0, 7, timed=False)]
    want = grade_shots(photos, [])
    rng = random.Random(2)
    for _ in range(10):
        assert grade_shots(rng.sample(photos, len(photos)), []) == want


def test_a_photo_without_utc_time_is_never_ground_and_breaks_no_leg():
    """It cannot be shown to be slow. It cannot be shown to be fast either."""
    a, b = photo(10.0, 20.0), photo(12.0, 20.0, 30)
    got = grade_shots([a, photo(10.0, 20.0, 10, timed=False), b], [])
    assert [s.kind for s in got[:2]] == ["fast", "fast"]
    assert (got[2].kind, got[2].use, got[2].taken_utc) == ("untimed", "none", None)
    assert not offered(got[2:], obj(10.0, 20.0))
    assert left_out(got)["no_utc"] == 1
    # One built by hand is no proof either.
    by_hand = Shot(NOON.replace(tzinfo=None), None, 10.0, 20.0, "own-camera", "ground", "all")
    assert not offered([by_hand], obj(10.0, 20.0))


# ------------------------------------------------------------------ decision 4: impossible photos


def test_an_impossible_leg_is_a_conflict_at_both_ends_and_outranks_fast():
    a, b = photo(10.0, 20.0, 0), photo(10.001, 20.0, 10)
    c, d = photo(15.0, 20.0, 20), photo(15.5, 20.0, 45)
    assert kinds(a, b, c, d) == ["conflict", "conflict", "conflict", "fast"]
    early = photo(10.002, 20.0, -600)
    assert kinds(early, b, c, d) == ["ground", "conflict", "conflict", "fast"]
    # Both ends, though the far one looks back to a place just under 100 km away.
    near, nearer = photo(10.0, 20.0, 0), photo(10.0 + 0.5 * KM, 20.0, 0.1)
    far = photo(10.0 + 100.2 * KM, 20.0, 1)
    assert kinds(near, nearer, far) == ["conflict", "fast", "conflict"]


def test_conflict_marks_the_whole_run():
    run = [photo(10.123456, 20.0, i) for i in range(5)]
    assert kinds(*run, photo(15.0, 20.0, 10)) == ["conflict"] * 6


def test_a_conflict_is_over_1200_km_h_and_at_least_100_km():
    a = photo(10.0, 20.0)
    assert kinds(a, photo(10.0 + 99 * KM, 20.0, 1)) == ["fast"] * 2
    assert kinds(a, photo(10.0 + 101 * KM, 20.0, 1)) == ["conflict"] * 2
    assert kinds(a, photo(10.0 + 599 * KM, 20.0, 30)) == ["fast"] * 2
    assert kinds(a, photo(10.0 + 601 * KM, 20.0, 30)) == ["conflict"] * 2
    assert kinds(a, photo(10.0 + 601 * KM, 20.0, 30), impossible_kmh=1300.0) == ["fast"] * 2
    assert kinds(a, photo(10.0 + 99 * KM, 20.0, 1), impossible_km=50.0) == ["conflict"] * 2


def test_two_places_in_one_instant_are_a_conflict_unless_they_are_nearly_one():
    """One camera is not in two places. Under a kilometre it is the jitter of one."""
    a = photo(10.0, 20.0)
    assert kinds(a, photo(10.0 + 1.001 * KM, 20.0)) == ["conflict"] * 2
    assert kinds(a, photo(10.0 + 101 * KM, 20.0)) == ["conflict"] * 2
    assert kinds(a, photo(10.0 + 0.999 * KM, 20.0)) == ["ground"] * 2
    assert kinds(a, photo(10.0 + 2 * KM, 20.0), min_leg_km=3.0) == ["ground"] * 2
    # The two need not be beside each other in the order; the one between them is neither.
    middle, far = photo(10.0 + 0.6 * KM, 20.0), photo(10.0 + 1.2 * KM, 20.0)
    assert kinds(a, middle, far) == ["conflict", "ground", "conflict"]


def test_a_conflict_is_never_proof_alone_and_marks_the_proof_of_its_day():
    objects = obj(10.0, 20.0)
    bad = shot(10.0, 20.0, day=2, kind="conflict")
    assert not offered([bad, bad], objects)
    (c,) = offered([bad, shot(10.0, 20.0, day=2)], objects)
    assert c.conflict and c.photos == 1
    assert not offered([bad, shot(10.0, 20.0, day=3)], objects)[0].conflict
    away = shot(10.0 + 3 * KM, 20.0, day=2, kind="conflict")
    assert not offered([away, shot(10.0, 20.0, day=2)], objects)[0].conflict


def test_fast_photos_are_counted_beside_the_proof_and_are_never_it():
    objects = obj(10.0, 20.0)
    fast = [shot(10.0, 20.0, day=d, kind="fast") for d in (1, 2)]
    assert not offered(fast, objects)
    far = shot(10.0 + 3 * KM, 20.0, kind="fast")
    (c,) = offered([*fast, far, shot(10.0 + KM, 20.0, day=4)], objects)
    assert (c.fast_near, c.photos, c.days) == (2, 1, 1)
    assert c.km == pytest.approx(1.0) and c.at.day == 4


# ------------------------------------------------------------------ decision 5: spans


def test_a_span_is_by_the_photos_own_clock_and_inclusive():
    one_day = span(first=2, last=2)
    for hours, zone, use in ((0, 0, "all"), (0, 13, "none"), (12, 0, "none"), (12, -1, "all"),
                             (35.9, 0, "none"), (36, 0, "all"), (36, -1, "none")):
        (s,) = grade_shots([photo(10.0, 20.0, hours * 60, zone=zone)], [one_day])
        assert s.use == use, (hours, zone)


def test_a_span_covers_its_box_with_its_edges():
    box = span(19.0, 9.0, 21.0, 11.0)
    for lat, lon, use in ((10, 20, "none"), (9, 19, "none"), (11, 21, "none"),
                          (8.99, 20, "all"), (11.01, 20, "all"), (10, 18.99, "all"),
                          (10, 21.01, "all")):
        assert grade_shots([photo(lat, lon)], [box])[0].use == use, (lat, lon)


def test_a_box_whose_west_is_past_its_east_crosses_the_antimeridian():
    box = span(170.0, -30.0, -170.0, 0.0)
    for lon, use in ((175, "none"), (180, "none"), (-180, "none"), (-175, "none"), (170, "none"),
                     (-170, "none"), (169, "all"), (-169, "all"), (0, "all")):
        assert grade_shots([photo(-16.0, lon)], [box])[0].use == use, lon
    # 180 and -180 are one meridian, whichever way the box was written.
    assert grade_shots([photo(-16.0, 180.0)], [span(-180.0, -30.0, -170.0, 0.0)])[0].use == "none"
    assert grade_shots([photo(-16.0, -180.0)], [span(170.0, -30.0, 180.0, 0.0)])[0].use == "none"


def test_none_outranks_airports_in_either_order():
    both = [span(use="airports"), span(use="none")]
    for spans in (both, both[::-1]):
        assert grade_shots([photo(10.0, 20.0)], spans)[0].use == "none"
    assert grade_shots([photo(10.0, 20.0)], both[:1])[0].use == "airports"


def test_a_layover_is_proof_for_series_5_only():
    objects = obj(10.0, 20.0, series=5) | obj(10.0, 20.0, series=22)
    got = offered([shot(10.0, 20.0, use="airports")], objects)
    assert [c.series for c in got] == [5]
    both = [shot(10.0, 20.0, use="airports"), shot(10.0, 20.0, day=2)]
    assert [(c.series, c.photos, c.days) for c in offered(both, objects)] == [(5, 2, 2),
                                                                             (22, 1, 1)]


def test_a_shot_the_owner_ruled_out_takes_no_part_in_anything():
    objects = obj(10.0, 20.0, series=5)
    out = [shot(10.0, 20.0, kind=k, use="none") for k in ("ground", "fast", "conflict")]
    assert not offered(out, objects)
    (c,) = offered([*out, shot(10.0, 20.0)], objects)
    assert (c.photos, c.fast_near, c.conflict) == (1, 0, False)


GOOD_SPAN = {"from": "2026-05-01", "to": "2026-05-03", "bbox": [1, 2.5, 3, 4], "why": "layover",
             "use": "airports"}


def spans_file(tmp_path, content):
    path = tmp_path / "spans.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content if isinstance(content, str) else json.dumps(content),
                        encoding="utf-8")
    return path


def test_load_spans_reads_what_the_owner_wrote(tmp_path):
    crossing = GOOD_SPAN | {"bbox": [170, -30, -170, 0], "use": "none"}
    assert load_spans(spans_file(tmp_path, [GOOD_SPAN, crossing])) == [
        Span(dt.date(2026, 5, 1), dt.date(2026, 5, 3), 1.0, 2.5, 3.0, 4.0, "layover", "airports"),
        Span(dt.date(2026, 5, 1), dt.date(2026, 5, 3), 170.0, -30.0, -170.0, 0.0, "layover",
             "none"),
    ]
    assert load_spans(spans_file(tmp_path, [])) == []


@pytest.mark.parametrize("change", [
    {"from": None}, {"from": "2026"}, {"from": "20260501"}, {"from": "2026-02-30"},
    {"from": 20260501}, {"to": "2026-04-30"}, {"to": ""}, {"to": ["2026-05-03"]},
    {"bbox": None}, {"bbox": [1, 2, 3]}, {"bbox": [1, 2, 3, 4, 5]}, {"bbox": "1234"},
    {"bbox": [1, 2, 3, "4"]}, {"bbox": [1, 2, 3, True]}, {"bbox": [1, 4, 3, 2]},
    {"bbox": [1, -91, 3, 4]}, {"bbox": [1, 2, 3, 91]}, {"bbox": [-181, 2, 3, 4]},
    {"bbox": [1, 2, 181, 4]}, {"bbox": [1, 2, 3, float("nan")]},
    {"bbox": [1, 2, float("inf"), 4]}, {"bbox": [1, 2, 10**400, 4]},
    {"bbox": {"w": 1, "s": 2, "e": 3, "n": 4}},
    {"why": ""}, {"why": "  "}, {"why": None}, {"why": 5},
    {"use": "all"}, {"use": "None"}, {"use": None}, {"use": ["none"]}, {"note": "x"},
])
def test_a_malformed_span_is_refused_and_named(tmp_path, change):
    """A span the owner wrote and this cannot read is a spoofed photo taken for proof."""
    with pytest.raises(PlanInvalid, match=r"spans\.json.*\[1\]"):
        load_spans(spans_file(tmp_path, [GOOD_SPAN, GOOD_SPAN | change]))


@pytest.mark.parametrize("field", GOOD_SPAN)
def test_a_span_without_a_field_is_refused(tmp_path, field):
    bad = {k: v for k, v in GOOD_SPAN.items() if k != field}
    with pytest.raises(PlanInvalid, match=field):
        load_spans(spans_file(tmp_path, [bad]))


@pytest.mark.parametrize("content", [
    "", "{", b"\xff\xfe", {}, GOOD_SPAN, "null", "[null]", "[[]]", '["x"]',
    '[{"from": "2026-05-01", "use": "none", "use": "airports"}]', "[" * 100_000,
])
def test_a_spans_file_that_is_not_a_list_of_objects_is_refused(tmp_path, content):
    with pytest.raises(PlanInvalid, match=r"spans\.json"):
        load_spans(spans_file(tmp_path, content))


def test_a_spans_file_that_cannot_be_opened_is_refused(tmp_path):
    for path in (tmp_path / "absent.json", tmp_path):
        with pytest.raises(PlanInvalid, match="cannot be read"):
            load_spans(path)


def test_a_repeated_key_is_named(tmp_path):
    text = json.dumps([GOOD_SPAN])[:-2] + ', "use": "none"}]'
    with pytest.raises(PlanInvalid, match="'use' twice"):
        load_spans(spans_file(tmp_path, text))


# ------------------------------------------------------------------ decision 6: the unfinished stay


def test_until_is_the_photos_own_clock_and_includes_its_day():
    last = dt.date(2026, 5, 1)
    for minutes, zone, use in ((0, 0, "all"), (719, 0, "all"), (720, 0, "none"),
                               (720, -1, "all"), (0, 13, "none")):
        (s,) = grade_shots([photo(10.0, 20.0, minutes, zone=zone)], [], until=last)
        assert s.use == use, (minutes, zone)
    assert grade_shots([photo(10.0, 20.0, 9999)], [])[0].use == "all"


def test_left_out_gives_each_photo_one_reason():
    photos = [
        photo(10.0, 20.0, 0), photo(10.001, 20.0, 30),                 # ground
        photo(14.0, 20.0, 5 * 60), photo(16.0, 20.0, 6 * 60),          # fast
        photo(26.0, 20.0, 6 * 60 + 5),                                 # conflict, both
        photo(26.5, 20.0, 11 * 60),                                    # ground, for airports only
        photo(27.0, 20.0, 11 * 60 + 59),                               # ruled out
        photo(27.1, 20.0, 20 * 60),                                    # the next day
        photo(10.0, 20.0, 40, timed=False),
    ]
    spans = [span(19, 26.4, 21, 26.6, use="airports"), span(19, 26.9, 21, 27.2)]
    shots = grade_shots(photos, spans, until=dt.date(2026, 5, 1))
    assert len(shots) == len(photos)
    assert left_out(shots) == {"fast": 1, "conflict": 2, "span_none": 1, "span_airports": 1,
                               "after_until": 1, "no_utc": 1}
    assert list(left_out([])) == list(left_out(shots)) and set(left_out([]).values()) == {0}
    assert left_out([shot(1, 2, use="none"), shot(1, 2)])["span_none"] == 1


# ------------------------------------------------------------------ decision 10: nothing else


def test_an_object_without_a_photo_leaves_no_trace():
    objects = obj(10.0, 20.0) | obj(10.0 + 2.5 * KM, 20.0, item=2) | obj(50.0, 20.0, item=3)
    got = offered([shot(10.0, 20.0)], objects)
    assert "Object 2" not in repr(got) and "Object 3" not in repr(got)
    counted = count_skipped([shot(10.0, 20.0)], obj(50.0, 20.0, series=12), {})
    assert counted == {}


# ------------------------------------------------------------------ the neighbourhood


@pytest.mark.parametrize("lat,lon,dlat,dlon", [
    (70.0, 25.0, 0, 1.9), (70.0, 25.0, 0, -1.9), (-70.0, 25.0, 1.3, 1.3), (89.0, 25.0, 0, 1.9),
    (0.0, 0.0, 1.0, 1.0), (0.001, 0.001, -1.0, -1.0), (-0.001, -0.001, 1.0, 1.0),
    (0.0, 0.0, -1.9, 0), (0.0, 0.0, 0, -1.9),
    (-5.5, 179.9999, 0, 1.0), (-5.5, -179.9999, 0, -1.0), (-5.5, 180.0, 0.1, 0.1),
    (-5.5, -180.0, -0.1, -0.1), (70.0, 179.99, 0, 1.9), (70.0, -179.99, 1.0, -1.0),
])
def test_a_photo_in_radius_is_found_wherever_the_grid_is_awkward(lat, lon, dlat, dlon):
    """A cell of longitude is a third as wide at 70° north, does not end at 180, and
    int() truncates toward zero, which makes one cell of two at the equator."""
    at = (lat + dlat * KM, lon + dlon * KM / math.cos(math.radians(lat)))
    wrapped = (at[0], (at[1] + 180) % 360 - 180)
    assert haversine_km(lat, lon, *wrapped) < 2.0
    for photo_at, object_at in (((lat, lon), wrapped), (wrapped, (lat, lon)), ((lat, lon), at)):
        (c,) = offered([shot(*photo_at)], obj(*object_at))
        assert c.km < 2.0


def test_across_the_pole_is_near():
    (c,) = offered([shot(89.995, 10.0)], obj(89.995, -170.0))
    assert c.km == pytest.approx(haversine_km(89.995, 10.0, 89.995, -170.0))
    assert offered([shot(-90.0, 10.0)], obj(-89.99, 77.0))


@pytest.mark.parametrize("radius", [0.2, 2.0, 60.0])
def test_the_grid_finds_exactly_what_measuring_every_pair_finds(radius):
    rng = random.Random(7)
    places = [(0, 0), (70, 25), (-5.5, 180), (89.9, 0), (-89.9, 0), (0, 180), (45, -90)]

    def scatter(n):
        out = []
        for i in range(n):
            lat, lon = places[i % len(places)]
            lat += rng.uniform(-2, 2) * radius * KM
            lon += rng.uniform(-2, 2) * radius * KM / math.cos(math.radians(min(abs(lat), 89.9)))
            out.append((max(-90, min(90, lat)), (lon + 180) % 360 - 180))
        return out

    shots = [shot(*p, minute=i % 60) for i, p in enumerate(scatter(210))]
    objects = {}
    for i, p in enumerate(scatter(210)):
        objects |= obj(*p, item=i)
    want = {}
    for (_, item), o in objects.items():
        near = [d for s in shots if (d := haversine_km(o.lat, o.lon, s.lat, s.lon)) <= radius]
        if near:
            want[item] = (min(near), len(near))
    got = {c.item: (c.km, c.photos) for c in offered(shots, objects, radius_km=radius)}
    assert got == want and len(want) > 20


def test_finding_is_not_a_full_cross_product(monkeypatch):
    calls = []
    monkeypatch.setattr(review, "haversine_km", lambda *a: calls.append(1) or haversine_km(*a))
    rng = random.Random(3)
    shots = [shot(rng.uniform(-60, 60), rng.uniform(-180, 180)) for _ in range(2000)]
    objects = {}
    for i in range(2000):
        objects |= obj(rng.uniform(-60, 60), rng.uniform(-180, 180), item=i)
    offered(shots, objects)
    assert len(calls) < 2000


# ------------------------------------------------------------------ the harvest


def test_harvest_reads_the_tile_of_each_photo_and_the_ring_around_it():
    p = SeriesPoint(7, 22, "here", 10.0, 20.0)
    beside = SeriesPoint(7, 30, "another series, the same id", 10.6, 20.0)
    far = SeriesPoint(8, 22, "elsewhere", 20.0, 20.0)
    reader, seen = FakeReader([p, beside, far]), []
    objects, unread = harvest([shot(10.0, 20.0), shot(10.0, 20.0, day=2)], reader, radius_km=2.0,
                              on_progress=lambda *a: seen.append(a))
    assert objects == {(22, 7): p, (30, 7): beside} and unread == 0
    x, y = deg2tile(10.0, 20.0, 9)
    assert reader.asked == sorted((9, x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))
    assert seen == [(n, 9) for n in range(1, 10)]


def test_harvest_reads_nothing_for_a_photo_that_cannot_count():
    reader = FakeReader()
    assert harvest([shot(10.0, 20.0, use="none")], reader, radius_km=2.0) == ({}, 0)
    assert reader.asked == []
    harvest([shot(10.0, 20.0, use="airports", kind="fast")], reader, radius_km=2.0)
    assert len(reader.asked) == 9


def test_a_failed_tile_is_counted_and_the_rest_is_still_offered():
    x, y = deg2tile(10.0, 20.0, 9)
    p = SeriesPoint(7, 22, "here", 10.0, 20.0)
    reader = FakeReader([p], broken={(x + 1, y), (x, y - 1)})
    shots = [shot(10.0, 20.0)]
    objects, unread = harvest(shots, reader, radius_km=2.0)
    assert unread == 2  # not the two an earlier run left in `failed`
    assert [c.item for c in offered(shots, objects)] == [7]


def test_harvest_goes_round_the_antimeridian_and_stops_at_the_edge_of_the_map():
    p = SeriesPoint(7, 22, "over the line", -5.5, -179.99)
    for lon in (179.99, 180.0, -180.0):
        reader = FakeReader([p])
        objects, _ = harvest([shot(-5.5, lon)], reader, radius_km=2.0)
        assert (22, 7) in objects, lon
        assert all(0 <= x < 512 for _, x, _ in reader.asked) and len(reader.asked) == 9
    for lat in (90.0, 86.0, -86.0, -90.0):  # a web map ends at 85.05
        reader = FakeReader()
        harvest([shot(lat, 25.0)], reader, radius_km=2.0)
        assert len(reader.asked) >= 6 and all(0 <= y < 512 for *_, y in reader.asked), lat


def test_the_ring_is_wide_enough_for_the_radius():
    """A zoom-9 tile is 78 km across at the equator and 5 km at 86° south."""
    lat, lon = 80.0, 25.0  # tiles of 13.6 km
    far = SeriesPoint(7, 22, "far", lat, lon + 45 * KM / math.cos(math.radians(lat)))
    assert haversine_km(lat, lon, far.lat, far.lon) < 46
    objects, _ = harvest([shot(lat, lon)], FakeReader([far]), radius_km=46.0)
    assert (22, 7) in objects
    reader = FakeReader([far])
    assert harvest([shot(lat, lon)], reader, radius_km=2.0) == ({}, 0)
    assert len(reader.asked) == 9


def test_harvest_reads_at_the_zoom_it_is_given():
    reader = FakeReader([SeriesPoint(7, 22, "here", 10.0, 20.0)])
    objects, _ = harvest([shot(10.0, 20.0)], reader, radius_km=2.0, zoom=11)
    assert objects and {z for z, *_ in reader.asked} == {11}


# ------------------------------------------------------------------ recall


def test_recall_says_how_many_ticked_objects_the_photos_find():
    objects, shots = {}, [shot(10.0, 20.0)]
    for item, km in ((1, 0.1), (2, 0.4), (3, 0.9), (4, 1.8), (5, 3.0)):
        objects |= obj(10.0 + km * KM, 20.0, item=item)
    objects |= obj(10.0, 20.0, item=6)  # not ticked
    cat = catalogue(objects)
    got = recall(shots, objects, {22: {1, 2, "3", 4, 5, 99}, 4: set(), "5": {"8"}}, catalogue=cat)
    assert got == {
        4: {"ticked": 0, "located": 0, "found": {0.2: 0, 0.5: 0, 1.0: 0, 2.0: 0}, "no_photo": []},
        5: {"ticked": 1, "located": 0, "found": {0.2: 0, 0.5: 0, 1.0: 0, 2.0: 0}, "no_photo": [8]},
        22: {"ticked": 6, "located": 5, "found": {0.2: 1, 0.5: 2, 1.0: 3, 2.0: 4},
             "no_photo": [5, 99]},
    }
    assert list(got) == [4, 5, 22]
    got = recall(shots, objects, {22: {5}}, (5.0, 1.0), catalogue=cat)
    assert got[22]["found"] == {5.0: 1, 1.0: 0}


def test_recall_is_measured_with_the_proof_the_offers_use():
    """A recall found by photos that may not offer anything would promise too much."""
    objects = obj(10.0, 20.0, series=5) | obj(10.0, 20.0, series=22)
    ticked = {5: {1}, 22: {1}}
    for s, want in ((shot(10.0, 20.0, use="airports"), (1, 0)), (shot(10.0, 20.0), (1, 1)),
                    (shot(10.0, 20.0, kind="fast"), (0, 0)), (shot(10.0, 20.0, use="none"), (0, 0)),
                    (shot(10.0, 20.0, kind="conflict"), (0, 0))):
        got = recall([s], objects, ticked, catalogue=catalogue(objects))
        assert (got[5]["found"][2.0], got[22]["found"][2.0]) == want, s


# ------------------------------------------------------------------ Op.proof


GOOD_PROOF = [{"kind": k, "detail": f"a {k}"}
              for k in ("ground_photo", "fast_photo", "document", "memory")]


def tick(**kw):
    return Op(kind="tick_series", bucket="apply", series=22, item=1, label="L", **kw)


def test_proof_is_not_in_the_key_and_is_empty_by_default():
    assert tick().proof == [] and tick().key == tick(proof=GOOD_PROOF).key
    trip = {"kind": "create_trip", "regions": [{"id": 5, "quality": 3}]}
    assert Op(**trip).legacy_key == Op(**trip, proof=GOOD_PROOF).legacy_key is not None
    # Positional construction elsewhere must not shift.
    assert list(Op.__dataclass_fields__)[-1] == "proof"


def test_a_plan_keeps_proof_and_one_written_without_it_still_loads(tmp_path):
    path = Plan(account=1, ops=[tick(proof=GOOD_PROOF)]).save(tmp_path / "p.json")
    plan = Plan.load(path)
    assert plan.ops[0].proof == GOOD_PROOF and validate(plan) == []
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["ops"][0]["proof"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert Plan.load(path).ops[0].proof == [] and validate(Plan.load(path)) == []


@pytest.mark.parametrize("proof", [
    None, "ground_photo", {}, {"kind": "memory", "detail": "x"}, ["memory"], [None], [[]],
    [{}], [{"kind": "memory"}], [{"detail": "x"}], [{"kind": "Memory", "detail": "x"}],
    [{"kind": "photo", "detail": "x"}], [{"kind": None, "detail": "x"}],
    [{"kind": ["memory"], "detail": "x"}], [{"kind": "memory", "detail": None}],
    [{"kind": "memory", "detail": 5}], [{"kind": "memory", "detail": ["x"]}],
    [GOOD_PROOF[0], {"kind": "memory", "detail": 10**5000}],
])
def test_proof_of_another_shape_is_refused(proof):
    (why,) = validate(Plan(account=1, ops=[tick(proof=proof)]))
    assert "ops[0]" in why and "proof" in why


def test_proof_is_checked_on_every_kind():
    op = Op(kind="mark_dare", bucket="apply", region=1, proof=["x"])
    assert "proof" in validate(Plan(account=1, ops=[op]))[0]


def test_proof_stays_out_of_the_journals_payload(tmp_path):
    """The journal says what was sent, and proof is coordinates and dates that were not."""
    from wanderfill.plan.apply import Journal

    j = Journal(tmp_path / "j.ndjson")
    j.opening(tick(proof=GOOD_PROOF))
    entry = json.loads(j.path.read_text(encoding="utf-8"))
    assert "proof" not in entry["payload"] and Op(**entry["payload"]).key == tick().key


@pytest.mark.parametrize("proof,wanted", [
    (GOOD_PROOF, ["proof ground_photo: a ground_photo", "proof memory: a memory"]),
    ([{"kind": "odd"}, "loose", 5], ["proof {'kind': 'odd'}", "proof loose", "proof 5"]),
    ("not a list", ["proof not a list"]), (None, ["proof None"]), ({"a": 1}, ["proof {'a': 1}"]),
])
def test_show_prints_proof_whatever_shape_it_is(tmp_path, capsys, proof, wanted):
    from wanderfill.cli import main as cli

    path = Plan(account=1, ops=[tick(proof=proof)]).save(tmp_path / "p.json")
    assert cli.main(["show", str(path), "-v"]) == 0
    out = capsys.readouterr().out
    for line in wanted:
        assert f"        - {line}\n" in out
    Plan(account=1, ops=[tick()]).save(path)
    assert cli.main(["show", str(path), "-v"]) == 0
    assert "proof" not in capsys.readouterr().out
