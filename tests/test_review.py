"""Tests for series candidates: what may be offered, and what must never be.

A wrong offer becomes a false tick after one tired click, on a profile other
people check. Every rule here is a photo that looked like a visit and was not:
somebody else's, one from an aircraft, one with a frozen or a spoofed coordinate.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import inspect
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from wanderfill.api.client import NomadMania
from wanderfill.api.errors import DriftError, PlanInvalid, VerificationFailed
from wanderfill.cli import main as cli
from wanderfill.geo.tiles import SeriesPoint, deg2tile, haversine_km
from wanderfill.plan import review
from wanderfill.plan.apply import apply_plan
from wanderfill.plan.model import Op, Plan, basis_of, fingerprint, validate
from wanderfill.plan.review import (
    SKIP_SERIES,
    Candidate,
    Manifest,
    SeriesInfo,
    Shot,
    Span,
    count_skipped,
    find_candidates,
    grade_shots,
    harvest,
    left_out,
    load_answers,
    load_spans,
    recall,
    render_page,
)
from wanderfill.plan.series import AIRPORT_SERIES, match_series
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


TILE = (9, 256, 184)


def decoded(tmp_path, *down):
    """The points of a real tile, at these pixels from its west edge and its top."""
    mvt = pytest.importorskip("mapbox_vector_tile")
    from wanderfill.geo.tiles import TileReader

    features = [{"geometry": f"POINT ({px} {py})",
                 "properties": {"id": n, "series_id": 22, "name": "x"}}
                for n, (px, py) in enumerate(down)]
    data = mvt.encode([{"name": "series2", "features": features}],
                      default_options={"y_coord_down": True})
    z, x, y = TILE
    path = tmp_path / "series2" / str(z) / str(x) / f"{y}.pbf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)  # on disk, so nothing is fetched
    return list(TileReader(cache_dir=tmp_path).series_points(*TILE))


def mercator_lat(y):
    """The latitude of a fractional tile row at zoom 9."""
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / 512))))


def test_a_tile_point_is_at_the_exact_mercator_latitude(tmp_path):
    """Read as a straight line from edge to edge, the middle of this tile was 60 m off."""
    p, *rest = decoded(tmp_path, (2048, 2048), (0, 0), (4096, 4096), (1000, 100), (9, 4000))
    assert haversine_km(p.lat, p.lon, mercator_lat(184.5), 0.3515625) < 1e-5  # one centimetre
    for got, (px, py) in zip(rest, [(0, 0), (4096, 4096), (1000, 100), (9, 4000)], strict=True):
        want = mercator_lat(184 + py / 4096), px / 4096 * 360 / 512
        assert haversine_km(got.lat, got.lon, *want) < 1e-5


def test_a_tile_point_near_the_top_of_the_tile_is_near_its_north(tmp_path):
    """Tiles count pixels down and the decoder counts up. The wrong way round is 39 km."""
    from wanderfill.geo.tiles import tile_bounds

    _, south, _, north = tile_bounds(*TILE[1:], TILE[0])
    top, bottom = decoded(tmp_path, (2048, 100), (2048, 4000))
    assert north - 0.02 < top.lat < north and south < bottom.lat < south + 0.02


@pytest.mark.parametrize("north", [1, -1])
def test_the_radius_is_kept_from_the_tile_to_the_candidate(tmp_path, north):
    (p,) = decoded(tmp_path, (2048, 2048))
    lat, objects = mercator_lat(184.5), {(p.series_id, p.id): p}
    assert offered([shot(lat + north * 1.999 * KM, 0.3515625)], objects)
    assert not offered([shot(lat + north * 2.001 * KM, 0.3515625)], objects)


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


def test_a_flagged_candidate_comes_after_the_clean_ones_of_its_series():
    """Nearer is not better when one of its photos cannot be where it says."""
    clean, bad = obj(10.0, 20.0, item=1), obj(50.0, 20.0, item=2)
    still = obj(-50.0, 20.0, item=3)
    shots = [shot(10.0 + 1.5 * KM, 20.0), shot(10.0 + 1.6 * KM, 20.0, day=2),
             shot(50.0 + 0.1 * KM, 20.0), shot(50.0 + 0.1 * KM, 20.0, kind="conflict"),
             *(shot(-50.0 + 0.2 * KM, 20.0, day=d) for d in (1, 2))]
    got = offered(shots, clean | bad | still)
    assert [(c.item, c.conflict, c.one_spot) for c in got] == [
        (1, False, False), (2, True, False), (3, False, True)]
    # Within its series only.
    other = obj(10.0, 20.0, item=4, series=30)
    assert [c.item for c in offered(shots, clean | bad | still | other)] == [1, 2, 3, 4]


def test_one_spot_is_one_raw_coordinate_on_two_days_or_more():
    """A spoofed receiver and a stale fix repeat one coordinate, and so does a phone on
    one desk: a flag for the owner to look at, and still a candidate."""
    objects = obj(10.0, 20.0)

    def one_spot(*shots, **kw):
        return [c.one_spot for c in offered(shots, objects, **kw)]

    here = (10.001234, 20.001234)
    assert one_spot(shot(*here), shot(*here, day=2)) == [True]
    assert one_spot(*(shot(*here, day=d, minute=m) for d in (1, 2, 9) for m in (0, 5))) == [True]
    assert one_spot(shot(*here)) == [False]
    assert one_spot(shot(*here), shot(*here, minute=5), shot(*here, minute=9)) == [False]
    assert one_spot(shot(*here), shot(*here, day=2), shot(10.001235, 20.001234)) == [False]
    assert one_spot(shot(*here), shot(10.001234, 20.001235, day=2)) == [False]
    # A source rounded to three decimals repeats itself by construction, and is flagged.
    assert one_spot(shot(10.001, 20.001), shot(10.001, 20.001, day=2)) == [True]


def test_one_spot_looks_at_the_proof_in_the_radius_only():
    objects = obj(10.0, 20.0, series=5) | obj(10.0, 20.0, series=22)
    same = [shot(10.001, 20.0, day=d) for d in (1, 2)]
    others = [shot(10.0 + 3 * KM, 20.0), shot(10.002, 20.0, kind="fast"),
              shot(10.003, 20.0, use="none"), shot(10.004, 20.0, use="airports")]
    got = offered([*same, *others], objects)
    assert [(c.series, c.one_spot) for c in got] == [(5, False), (22, True)]


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


def stay(*minutes):
    """Photos in one place, a few metres apart, at these minutes."""
    return [photo(10.0 + i * 1e-5, 20.0, m) for i, m in enumerate(minutes)]


def test_after_a_flight_the_landing_and_its_minutes_are_fast_and_the_stay_is_not():
    """A long gap is slow for nobody who can see across it, and a stay is not a burst."""
    air = photo(-60.0, 20.0, -12 * 60)
    assert kinds(air, *stay(0, 4, 10, 10.1, 60, 65, 600)) == ["fast"] * 4 + ["ground"] * 4
    # With nothing between the landing and an hour later, the landing is what is met.
    assert kinds(air, *stay(0, 60)) == ["fast", "fast", "ground"]
    # A photo within minutes of its neighbours, but not of the landing, does not reach it.
    assert kinds(air, *stay(0, 8, 16, 24)) == ["fast"] * 3 + ["ground"] * 2
    # Photos taken later in the stay change nothing.
    walk = [photo(10.0 + 3 * KM, 20.0, 700), photo(10.0, 20.0, 760)]
    assert kinds(air, *stay(0, 60), *walk) == ["fast", "fast"] + ["ground"] * 3


def test_before_a_flight_the_departure_and_its_minutes_are_fast_and_the_stay_is_not():
    air = photo(-60.0, 20.0, 12 * 60)
    assert kinds(*stay(-600, -65, -60, -10.1, -10, -4, 0), air) == ["ground"] * 4 + ["fast"] * 4
    assert kinds(*stay(-60, 0), air) == ["ground", "fast", "fast"]
    assert kinds(*stay(-24, -16, -8, 0), air) == ["ground"] * 2 + ["fast"] * 3


def test_a_conflict_reaches_as_far_as_fast_does():
    there = photo(60.0, 20.0, -30)
    assert kinds(there, *stay(0, 10, 10.1, 60)) == ["conflict"] * 3 + ["ground"] * 2
    there = photo(60.0, 20.0, 30)
    assert kinds(*stay(-60, -10.1, -10, 0), there) == ["ground"] * 2 + ["conflict"] * 3


def test_how_far_a_burst_reaches_can_be_moved():
    air = photo(-60.0, 20.0, -12 * 60)
    assert kinds(air, *stay(0, 4, 8), burst_minutes=4) == ["fast"] * 3 + ["ground"]
    assert kinds(air, *stay(0, 4, 8), burst_minutes=0) == ["fast"] * 2 + ["ground"] * 2
    # At no minutes at all, a photo of the same instant is still looked past.
    assert kinds(air, photo(10.0, 20.0), photo(10.001, 20.0), burst_minutes=0) == ["fast"] * 3


def test_a_run_is_graded_whole_and_looked_past_only_whole():
    """It is left from its last photo and entered at its first, however long it lasted;
    and it is nearby photos like any others, the farthest of which decides."""
    air = photo(-60.0, 20.0, 12 * 60)
    run = [photo(10.0, 20.0, m) for m in (-180, -90, 0)]
    assert kinds(*run, photo(10.001, 20.0, 5), air) == ["fast"] * 5
    before = photo(10.001, 20.0, -185)
    assert kinds(before, *run, air) == ["ground"] + ["fast"] * 4
    short = [photo(10.0, 20.0, m) for m in (-6, -3, 0)]
    assert kinds(photo(10.001, 20.0, -10), *short, air) == ["fast"] * 5
    assert kinds(photo(10.001, 20.0, -10.1), *short, air) == ["ground"] + ["fast"] * 4
    # The same the other way round.
    air = photo(-60.0, 20.0, -12 * 60)
    run = [photo(10.0, 20.0, m) for m in (0, 90, 180)]
    assert kinds(air, *run, photo(10.001, 20.0, 185)) == ["fast"] * 4 + ["ground"]
    short = [photo(10.0, 20.0, m) for m in (0, 3, 6)]
    assert kinds(air, *short, photo(10.001, 20.0, 10)) == ["fast"] * 5
    assert kinds(air, *short, photo(10.001, 20.0, 10.1)) == ["fast"] * 4 + ["ground"]


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
    assert len(calls) < 5000 * 80  # every pair is 25,000,000
    away = photo(place[0] - 5.0, place[1], -24 * 60)
    calls.clear()
    assert kinds(away, *stay) == ["ground"] * 5001
    assert len(calls) < 5000 * 80


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
    assert kinds(*frozen, photo(lat + 130 * KM, lon, 15 * 11 + 50)) == ["fast"] * 13
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


def test_fast_is_over_150_km_h():
    assert kinds(photo(10.0, 20.0, 0), photo(10.0 + 149 * KM, 20.0, 60)) == ["ground"] * 2
    assert kinds(photo(10.0, 20.0, 0), photo(10.0 + 151 * KM, 20.0, 60)) == ["fast"] * 2


def test_an_impossible_leg_is_a_conflict_at_both_ends_and_outranks_fast():
    a, b = photo(10.0, 20.0, 0), photo(10.001, 20.0, 10)
    c, d = photo(15.0, 20.0, 20), photo(15.5, 20.0, 40)
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


def test_show_says_how_much_of_the_plan_it_does_not_show(tmp_path, capsys):
    """Forty ops of a longer plan looked like all of it, and were approved as that."""
    from wanderfill.cli import main as cli

    path = Plan(account=1, ops=[dataclasses.replace(tick(), item=n) for n in range(1, 44)]).save(
        tmp_path / "p.json")
    for argv, shown in (([], 40), (["--limit", "42"], 42), (["--limit", "0"], 0),
                        (["--limit", "-1"], 42)):
        assert cli.main(["show", str(path), "-v", *argv]) == 0
        out = capsys.readouterr().out
        assert out.count("  [apply] tick_series") == shown
        assert f"{43 - shown} more op(s) are not shown; --limit 43 shows them all" in out
    for argv in (["-v", "--limit", "43"], ["-v", "--limit", "99"], []):
        assert cli.main(["show", str(path), *argv]) == 0
        assert "not shown" not in capsys.readouterr().out


# ------------------------------------------------------------------ the manifest: what was shown

HOSTILE = ("</script><script>boom()</script><!--\"'&amp;"
           "\u2028\u2029\u202e 東京 \U0001f5fc $& \\u003c")


def candidate(item=1, series=22, **kw):
    return Candidate(**{
        "series": series, "item": item, "series_name": f"Series {series}",
        "name": f"Object {item}", "lat": 10.0, "lon": 20.0, "km": 0.5,
        "at": dt.datetime(2026, 5, 1, 14, 5, 9), "photos": 3, "days": 2,
        "origins": ("own-camera",), "fast_near": 0, "conflict": False, "thin": False,
        "one_spot": False,
    } | kw)


PARAMS = {
    "since": "2026-05-01", "until": "2026-05-31", "radius_km": 2.0, "origins": ["own-camera"],
    "skip": [-2, 12, 17, 60],
    "speeds": {"fast_kmh": 150.0, "min_leg_km": 1.0, "impossible_kmh": 1200.0,
               "impossible_km": 100.0, "burst_minutes": 10.0},
}
RECALL = {22: {"ticked": 4, "located": 3, "found": {0.2: 1, 2.0: 2}, "no_photo": [7, 9]}}
COVERAGE = {"tiles_read": 12, "tiles_unread": 0, "recall": RECALL}
LEFT_OUT = {"fast": 5, "conflict": 2, "span_none": 3, "span_airports": 1, "after_until": 4,
            "no_utc": 6}


def manifest(candidates=None, **kw):
    return Manifest(**{
        "account": 1, "created": "2026-06-01T10:00:00+00:00", "params": PARAMS,
        "coverage": COVERAGE, "left_out": LEFT_OUT,
        "candidates": [candidate(1), candidate(2)] if candidates is None else candidates,
    } | kw)


EVERYTHING = [
    candidate(1, name=HOSTILE, series_name=HOSTILE),
    candidate(2, km=1.5, thin=True, photos=1, days=1, at=dt.datetime(2026, 5, 1, 9, 0, 0, 25)),
    candidate(3, one_spot=True, at=dt.datetime(2026, 5, 2, 8)),
    candidate(4, conflict=True, at=dt.datetime(2026, 5, 2, 8)),
    candidate(5, fast_near=2, origins=("airdrop", "own-camera"), km=0.0),
    candidate(1, series=-7, name="", lat=-90.0, lon=-180.0, at=dt.datetime(2026, 5, 3, 0, 0)),
]


def test_a_manifest_comes_back_from_disk_as_it_was(tmp_path):
    m = manifest(EVERYTHING, coverage=COVERAGE | {"skipped": {12: 3}},
                 left_out=LEFT_OUT | {"other_origin": 8}, params=PARAMS | {"until": None})
    back = Manifest.load(m.save(tmp_path / "m.json"))
    assert back == m and back.sha256 == m.sha256
    assert back.candidates == EVERYTHING  # ids, times, tuples and flags, all of one type
    assert Manifest.load(back.save(tmp_path / "n.json")) == m


def test_the_hash_of_a_list_is_what_it_was():
    """Answers on disk name it. Another hash for the same list is another list to them."""
    m = manifest(EVERYTHING, coverage=COVERAGE | {"skipped": {12: 3}},
                 left_out=LEFT_OUT | {"other_origin": 8})
    assert m.sha256 == "ff0eeda787e2ba9703091ff737ea10a618e989a6bbf3ea0bebcd40a7506b8d49"


def test_the_file_binding_is_one_module_for_both_lists():
    from wanderfill.plan import manifest as shared

    for name in ("canonical", "fields", "whole", "number", "text", "date", "read_json",
                 "read_bytes", "json_of"):
        assert getattr(review, f"_{name}") is getattr(shared, name), name
    value = {"b": ["<x>&\u2028\u2029", 1.5], "a": "東"}
    assert shared.canonical(value) == '{"a":"東","b":["<x>&\u2028\u2029",1.5]}'
    assert shared.sha256_of(value) == hashlib.sha256(
        shared.canonical(value).encode("utf-8")).hexdigest()
    assert not set(shared.escape_data(value)) & set("<>&\u2028\u2029")
    assert json.loads(shared.escape_data(value)) == value


def test_a_shot_says_which_photo_it_is_and_by_what_clock():
    assert [f.name for f in dataclasses.fields(Shot)][-2:] == ["uuid", "zone"]
    assert shot(1.0, 1.0).uuid == "" and shot(1.0, 1.0).zone is None
    one = dataclasses.replace(photo(10.0, 20.0, uuid="a"), zone=0)
    two = dataclasses.replace(photo(10.0, 20.0, 60, uuid="b", zone=1), zone=3600)
    three = photo(10.0, 20.0, uuid="c", timed=False)
    assert [(s.uuid, s.zone) for s in grade_shots([three, two, one], ())] == [
        ("a", 0), ("b", 3600), ("c", None)]


def test_the_hash_is_of_the_canonical_json_of_all_but_created(tmp_path):
    m = manifest([candidate(1, name="東京")])
    stored = json.loads(m.save(tmp_path / "m.json").read_text(encoding="utf-8"))
    said = stored.pop("sha256")
    del stored["created"]
    text = json.dumps(stored, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert "東京" in text and '"at":"2026-05-01T14:05:09"' in text
    assert said == m.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_hash_does_not_depend_on_when_or_on_the_order_of_keys(tmp_path):
    m = manifest()
    assert manifest(created="another day").sha256 == m.sha256
    assert manifest(params=dict(reversed(PARAMS.items()))).sha256 == m.sha256
    assert manifest(params=PARAMS | {"origins": ("own-camera",), "skip": SKIP_SERIES}
                    ).sha256 == m.sha256
    text = json.loads(m.save(tmp_path / "m.json").read_text(encoding="utf-8"))
    text["candidates"] = [dict(reversed(c.items())) for c in text["candidates"]]
    path = tmp_path / "n.json"
    path.write_text(json.dumps(dict(reversed(text.items()))), encoding="utf-8")
    assert Manifest.load(path) == m


OTHER = {
    "series": 23, "item": 9, "series_name": "x", "name": "x", "lat": 10.00001, "lon": 20.00001,
    "km": 0.501, "at": dt.datetime(2026, 5, 1, 14, 5, 10), "photos": 4, "days": 3,
    "origins": ("airdrop",), "fast_near": 1, "conflict": True, "thin": True, "one_spot": True,
}


def test_every_field_of_a_candidate_is_in_the_hash():
    assert set(OTHER) == {f.name for f in dataclasses.fields(Candidate)}
    hashes = {manifest([candidate(), candidate(**{"item": 2, k: v})]).sha256
              for k, v in OTHER.items()}
    assert len(hashes) == len(OTHER) and manifest().sha256 not in hashes


def test_everything_else_shown_is_in_the_hash():
    speeds = PARAMS["speeds"]
    row = RECALL[22]
    others = [
        {"account": 2}, {"candidates": []}, {"candidates": [candidate(2), candidate(1)]},
        *({"params": PARAMS | p} for p in (
            {"since": "2026-05-02"}, {"until": None}, {"radius_km": 2.5}, {"skip": []},
            {"origins": ["airdrop", "own-camera"]},
            *({"speeds": speeds | {k: v + 1}} for k, v in speeds.items()))),
        *({"coverage": COVERAGE | c} for c in (
            {"tiles_read": 13}, {"tiles_unread": 1}, {"recall": {}}, {"skipped": {}},
            {"skipped": {12: 1}}, {"recall": {23: row}},
            *({"recall": {22: row | r}} for r in (
                {"ticked": 5}, {"located": 4}, {"found": {0.2: 1}}, {"no_photo": [7]},
                {"name": "x"})))),
        *({"left_out": LEFT_OUT | {k: v + 1}} for k, v in LEFT_OUT.items()),
        {"left_out": LEFT_OUT | {"other_origin": 0}},
    ]
    hashes = {manifest(**kw).sha256 for kw in others} | {manifest().sha256}
    assert len(hashes) == len(others) + 1


def test_float_noise_does_not_reach_the_hash():
    a = manifest([candidate(lat=10.000012, lon=0.000001, km=0.1234)])
    b = manifest([candidate(lat=10.0000120000001, lon=-0.000001, km=0.12340000001)])
    assert a.sha256 == b.sha256
    (c,) = a.candidates
    assert (c.lat, c.lon, c.km) == (10.00001, 0.0, 0.123)
    a, b = (manifest([candidate(lat=lat, km=km)]) for lat, km in ((1e-9, 0.0), (-1e-9, -0.0)))
    assert a.sha256 == b.sha256
    assert manifest([candidate(km=1, lat=10)]).sha256 == manifest(
        [candidate(km=1.0, lat=10.0)]).sha256
    assert manifest(params=PARAMS | {"radius_km": 2}).sha256 == manifest().sha256


def test_an_object_in_the_edge_of_the_last_tile_is_where_it_is():
    """A tile reaches past its own bounds, at the antimeridian past 180."""
    for lon, wanted in ((180.01, -179.99), (-180.01, 179.99), (180.0, -180.0), (-180.0, -180.0),
                        (179.999999, -180.0), (20.00001, 20.00001)):
        assert manifest([candidate(lon=lon)]).candidates[0].lon == wanted


@pytest.mark.parametrize("change", [
    {"series": True}, {"series": "22"}, {"series": 2**53}, {"item": 1.0}, {"item": None},
    {"series_name": None}, {"name": 5}, {"name": ["x"]},
    {"lat": float("nan")}, {"lat": float("inf")}, {"lat": 90.1}, {"lat": "10"}, {"lat": True},
    {"lat": 10**400}, {"lon": float("-inf")}, {"lon": 360.1}, {"lon": None},
    {"km": float("nan")}, {"km": float("inf")}, {"km": -0.1}, {"km": "1"},
    {"at": "2026-05-01T14:05:09Z"}, {"at": dt.date(2026, 5, 1)}, {"at": 5}, {"at": "2026-05-01"},
    {"at": dt.datetime(2026, 5, 1, tzinfo=UTC)}, {"at": "2026-05-01 14:05:09"}, {"at": None},
    {"photos": 0}, {"photos": True}, {"photos": 1.0}, {"days": 0}, {"days": 4},
    {"origins": ()}, {"origins": "own-camera"}, {"origins": ("nobody",)}, {"origins": (1,)},
    {"origins": ("own-camera", "own-camera")},
    {"fast_near": -1}, {"fast_near": False}, {"conflict": 0}, {"thin": None}, {"one_spot": "no"},
])
def test_a_candidate_of_another_shape_is_refused(change):
    with pytest.raises(PlanInvalid, match=rf"candidates\[1\].*{next(iter(change))}"):
        manifest([candidate(1), candidate(**{"item": 2} | change)])


@pytest.mark.parametrize("change", [
    {"account": True}, {"account": "1"}, {"account": None}, {"account": 1.0},
    {"created": None}, {"created": 5}, {"format": 2}, {"format": True}, {"format": "1"},
    {"format": 1.0}, {"params": None}, {"params": []}, {"candidates": 5}, {"candidates": {}},
    {"candidates": ["x"]}, {"coverage": []}, {"left_out": None},
    *({"params": PARAMS | p} for p in (
        {"since": None}, {"since": "2026"}, {"since": "20260501"}, {"since": 5}, {"until": ""},
        {"until": "soon"}, {"radius_km": float("nan")}, {"radius_km": float("inf")},
        {"radius_km": -1}, {"radius_km": "2"}, {"radius_km": True}, {"origins": "own-camera"},
        {"origins": []}, {"origins": ["nobody"]}, {"skip": [1.5]}, {"skip": ["x"]}, {"skip": 5},
        {"skip": [True]}, {"skip": [5, 5]}, {"speeds": {}}, {"speeds": None},
        {"speeds": PARAMS["speeds"] | {"fast_kmh": float("nan")}},
        {"speeds": PARAMS["speeds"] | {"warp": 1.0}}, {"cached": True})),
    *({"params": {k: v for k, v in PARAMS.items() if k != gone}} for gone in PARAMS),
    *({"coverage": COVERAGE | c} for c in (
        {"tiles_read": -1}, {"tiles_read": True}, {"tiles_unread": None}, {"tiles_unread": 1.0},
        {"recall": []}, {"recall": {"x": RECALL[22]}}, {"recall": {22: []}}, {"recall": {22: {}}},
        {"recall": {22: RECALL[22] | {"found": {"x": 1}}}},
        {"recall": {22: RECALL[22] | {"found": {float("nan"): 1}}}},
        {"recall": {22: RECALL[22] | {"found": {2.0: -1}}}},
        {"recall": {22: RECALL[22] | {"no_photo": ["7"]}}},
        {"recall": {22: RECALL[22] | {"name": 5}}}, {"recall": {22: RECALL[22] | {"odd": 5}}},
        {"recall": {22: RECALL[22], "22": RECALL[22]}}, {"recall": {"022": RECALL[22]}},
        {"skipped": {12: -1}}, {"skipped": [12]}, {"odd": 1})),
    *({"coverage": {k: v for k, v in COVERAGE.items() if k != gone}} for gone in COVERAGE),
    {"left_out": LEFT_OUT | {"fast": -1}}, {"left_out": LEFT_OUT | {"fast": True}},
    {"left_out": LEFT_OUT | {"fast": "5"}}, {"left_out": LEFT_OUT | {"bored": 1}},
    {"left_out": {}},
])
def test_a_manifest_of_another_shape_is_refused(change):
    with pytest.raises(PlanInvalid, match=next(iter(change))):
        manifest(**change)


def test_one_object_cannot_be_asked_twice():
    """One answer would stand for two questions."""
    with pytest.raises(PlanInvalid, match="22:1"):
        manifest([candidate(1), candidate(2), candidate(1, km=0.1)])
    assert manifest([candidate(1, series=5), candidate(1, series=22)])


def test_a_name_no_file_can_hold_is_mended_once(tmp_path):
    m = manifest([candidate(1, name="half \ud83d of a pair", series_name="\udc00")])
    assert m.candidates[0].name == "half � of a pair"
    assert Manifest.load(m.save(tmp_path / "m.json")) == m
    assert "� of a pair" in render_page(m)


def test_the_manifest_records_every_speed_there_is():
    wanted = [n for n, p in inspect.signature(grade_shots).parameters.items()
              if p.kind is p.KEYWORD_ONLY and n != "until"]
    assert sorted(manifest().params["speeds"]) == sorted(wanted)
    assert set(left_out([])) == set(LEFT_OUT)


def test_what_the_functions_return_goes_in_as_it_is(tmp_path):
    shots = [shot(10.0, 20.0 + 0.5 * KM), shot(10.0, 20.0 + 1.2 * KM, day=2)]
    objects = obj(10.0, 20.0) | obj(10.0, 20.0, item=2) | obj(10.0, 20.0, item=3, series=12)
    cat = {22: catalogue(objects)[22]}
    ticked = {22: {2}, 12: set()}
    m = manifest(
        find_candidates(shots, objects, cat, ticked), left_out=left_out(shots),
        coverage={"tiles_read": 9, "tiles_unread": 0,
                  "recall": recall(shots, objects, ticked, catalogue=cat),
                  "skipped": count_skipped(shots, objects, ticked)},
        params=PARAMS | {"since": dt.date(2026, 5, 1), "skip": SKIP_SERIES,
                         "origins": {"own-camera"}})
    assert [c.item for c in m.candidates] == [1]
    assert m.coverage["recall"]["22"]["found"] == {"0.2": 0, "0.5": 1, "1.0": 1, "2.0": 1}
    assert m.coverage["skipped"] == {"12": 1} and m.params["since"] == "2026-05-01"
    assert Manifest.load(m.save(tmp_path / "m.json")) == m


# -- load


def saved(tmp_path, m=None):
    return json.loads((m or manifest()).save(tmp_path / "m.json").read_text(encoding="utf-8"))


def written(tmp_path, raw):
    path = tmp_path / "m.json"
    path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    return path


def rehashed(raw):
    """The file as somebody who can count sha256 would have altered it."""
    content = {k: v for k, v in raw.items() if k not in ("sha256", "created")}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return raw | {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


@pytest.mark.parametrize("field", OTHER)
def test_load_refuses_an_altered_candidate(tmp_path, field):
    raw = saved(tmp_path)
    value = OTHER[field]
    raw["candidates"][1][field] = (value.isoformat() if field == "at" else
                                   list(value) if field == "origins" else value)
    with pytest.raises(PlanInvalid, match=r"m\.json.*sha256"):
        Manifest.load(written(tmp_path, raw))
    Manifest.load(written(tmp_path, rehashed(raw)))  # the hash was all that was wrong


@pytest.mark.parametrize("change", [
    {"account": 2}, {"params": PARAMS | {"radius_km": 3.0}}, {"left_out": LEFT_OUT | {"fast": 0}},
    {"candidates": []}, {"sha256": "0" * 64}, {"sha256": None}, {"sha256": 5},
])
def test_load_refuses_any_other_altered_field(tmp_path, change):
    with pytest.raises(PlanInvalid, match=r"m\.json.*sha256"):
        Manifest.load(written(tmp_path, saved(tmp_path) | change))


@pytest.mark.parametrize("change", [
    {"lat": 10.000001}, {"km": 0.5001}, {"lat": -0.0}, {"at": "2026-05-01T14:05:09+00:00"},
    {"at": "2026-05-01T14:05:09.000000"}, {"at": 5}, {"lat": 10}, {"odd": 1},
    {"origins": ["own-camera", "airdrop"]}, {"origins": "own-camera"}, {"item": 1},
    {"item": "2"}, {"conflict": 0},
])
def test_load_takes_only_what_save_writes(tmp_path, change):
    raw = saved(tmp_path)
    raw["candidates"][1].update(change)
    for bad in (raw, rehashed(raw)):  # the hash it had, and the hash of what it says now
        with pytest.raises(PlanInvalid, match=r"m\.json"):
            Manifest.load(written(tmp_path, bad))


def test_load_refuses_unknown_and_missing_fields(tmp_path):
    raw = saved(tmp_path)
    for bad in (raw | {"note": "x"}, *({k: v for k, v in raw.items() if k != gone}
                                       for gone in raw)):
        with pytest.raises(PlanInvalid, match=r"m\.json"):
            Manifest.load(written(tmp_path, rehashed(bad) if "sha256" in bad else bad))
    del raw["candidates"][0]["thin"]
    with pytest.raises(PlanInvalid, match="thin"):
        Manifest.load(written(tmp_path, rehashed(raw)))


def test_load_says_a_later_format_is_one(tmp_path):
    """Before the hash, which a later format may count over something else."""
    with pytest.raises(PlanInvalid, match="format 2"):
        Manifest.load(written(tmp_path, saved(tmp_path) | {"format": 2, "more": 1}))


@pytest.mark.parametrize("content", [
    "", "{", "null", "[]", "5", "{}", "[" * 100_000, '{"format": 1, "format": 1}',
    '{"account": NaN}', '{"account": Infinity}', '{"account": ' + "9" * 5000 + "}",
])
def test_load_refuses_a_file_that_is_not_a_manifest(tmp_path, content):
    with pytest.raises(PlanInvalid, match=r"m\.json"):
        Manifest.load(written(tmp_path, content))


def test_load_refuses_a_file_it_cannot_open_or_read(tmp_path):
    (tmp_path / "bytes.json").write_bytes(b"\xff\xfe")
    for path in (tmp_path / "absent.json", tmp_path, tmp_path / "bytes.json"):
        with pytest.raises(PlanInvalid, match=re.escape(str(path))):
            Manifest.load(path)


def test_load_refuses_a_repeated_key_and_a_number_that_is_none(tmp_path):
    text = json.dumps(saved(tmp_path))
    for bad, why in ((text.replace('"km": 0.5', '"km": 0.5, "km": 0.5', 1), "'km' twice"),
                     (text.replace('"km": 0.5', '"km": NaN', 1), "NaN"),
                     (text.replace('"km": 0.5', '"km": 1e999', 1), "km")):
        assert bad != text
        with pytest.raises(PlanInvalid, match=why):
            Manifest.load(written(tmp_path, bad))


# ------------------------------------------------------------------ the page

CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "base-uri 'none'; form-action 'none'")
DATA = re.compile(r'(<script type="application/json" id="data">)(.*?)(</script>)', re.S)


def embedded(page):
    (match,) = DATA.findall(page)
    return match[1]


def test_the_page_holds_the_manifest_and_its_hash():
    m = manifest(EVERYTHING)
    data = json.loads(embedded(render_page(m)))
    assert data["sha256"] == m.sha256 and data["account"] == 1 and data["format"] == 1
    assert data["created"] == m.created
    assert [c["name"] for c in data["candidates"]] == [c.name for c in EVERYTHING]
    assert data["candidates"][0]["at"] == "2026-05-01T14:05:09"


def test_a_hostile_name_stays_inside_the_data():
    page, plain = render_page(manifest(EVERYTHING)), render_page(manifest([]))
    for mark in ("<script", "</script", "<!--"):
        assert page.count(mark) == plain.count(mark)
    assert "boom()" in embedded(page) and "boom()" not in DATA.sub("", page)
    assert not set("<>&\u2028\u2029") & set(embedded(page))
    assert json.loads(embedded(page))["candidates"][0]["name"] == HOSTILE
    assert DATA.sub("", page) == DATA.sub("", plain)  # the names changed nothing around them


@pytest.mark.parametrize("name", ["$&", "\\g<0>", "{data}", "%s", "/*DATA*/", "__DATA__"])
def test_a_name_that_looks_like_a_template_is_a_name(name):
    page = render_page(manifest([candidate(1, name=name)]))
    assert json.loads(embedded(page))["candidates"][0]["name"] == name
    assert DATA.sub("", page) == DATA.sub("", render_page(manifest([])))


def test_the_page_reaches_for_nothing():
    """Nothing in it can load, send or lead anywhere, so the coordinates stay here."""
    url = "http://a.test/x //b.test fetch import src= href="
    page = render_page(manifest([candidate(1, name=url)]))
    assert "http://a.test/x //b.test" in embedded(page)
    meta = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
    assert page.count(meta) == 1
    rest = DATA.sub("", page).replace(meta, "").lower()  # the policy is spelt with http
    for word in ("http", "//", "fetch", "import", "src=", "href=", "xmlhttp", "websocket",
                 "eventsource", "beacon", "worker", "eval", "new function", "<form",
                 "<iframe", "<img", "<link", "<a ", "ping=", "window.open", "location"):
        assert word not in rest, word
    assert "url(" not in rest.split("<style>")[1].split("</style>")[0]


def test_the_policy_comes_before_anything_it_governs():
    page = render_page(manifest())
    at = page.index("Content-Security-Policy")
    assert all(at < page.index(tag) for tag in ("<style", "<script", "<body"))
    assert page.index('<meta charset="utf-8">') < at and page.startswith("<!doctype html>")


def test_text_goes_in_as_text():
    rest = DATA.sub("", render_page(manifest()))
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                 "createContextualFragment", "DOMParser", "setHTML", "srcdoc"):
        assert word not in rest
    assert "textContent" in rest


def test_nothing_is_preselected_and_nothing_answers_in_bulk():
    rest = DATA.sub("", render_page(manifest(EVERYTHING)))
    for word in ('aria-pressed="true"', "checked", " selected", "autofocus", "accesskey",
                 "keydown", "keyup", "keypress"):
        assert word not in rest.split("</style>")[1], word
    assert not re.search(r"(select|mark|answer|tick|clear|choose)\W+(them\W+)?(all|every)\b", rest,
                         re.I)


def test_the_page_says_what_an_answer_means():
    rest = re.sub(r"\s+", " ", DATA.sub("", render_page(manifest())))
    for copy in ("I was there", "I was not there", "I don't remember", "Clear this answer",
                 ": a final answer.", "Leaving an item blank is fine and means no answer.",
                 "A photo nearby does not prove that you went inside.",
                 "never removes anything from your profile",
                 "one photo, or over 1 km away",
                 "every photo on several days has exactly the same coordinate: check it is not "
                 "a frozen or false position",
                 "a photo that day has an impossible jump",
                 "under the day of its nearest ground photo",
                 "Times are by the photo's own clock, and a photo with no recorded time zone "
                 "shows UTC."):
        assert copy in rest, copy
    assert "earest photo" not in rest


def test_the_template_is_in_the_package():
    page = Path(review.__file__).with_name("review_page.html")
    assert page.is_file() and "src/wanderfill/plan" in page.as_posix()
    assert 'packages = ["src/wanderfill"]' in (
        Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")


# -- the page's own scripts, run

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="no node on this machine")


def run(tmp_path, m, body):
    """What `body` returns, run in node with the scripts of the rendered page itself."""
    page, code = tmp_path / "page.html", tmp_path / "body.js"
    page.write_text(render_page(m), encoding="utf-8")
    code.write_text(body, encoding="utf-8")
    done = subprocess.run([NODE, str(Path(__file__).with_name("review_page_dom.js")),
                           str(page), str(code)], capture_output=True, encoding="utf-8",
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def answers_file(tmp_path, raw):
    path = tmp_path / "series-answers.json"
    if isinstance(raw, bytes):
        path.write_bytes(raw)
    else:
        path.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
    return path


@needs_node
def test_the_pages_export_is_what_load_answers_reads(tmp_path):
    m = manifest(EVERYTHING)
    text = run(tmp_path, m, """
      const storage = new Storage();
      const store = logic.openStore(() => storage, data);
      for (const [k, v] of [["22:5", "q"], ["-7:1", "n"], ["22:1", "y"], ["22:2", "y"],
                            ["22:2", ""]]) {
        store.set(k, v);
      }
      return logic.exportText(data, store.answers());
    """)
    assert json.loads(text) == {"format": 1, "account": 1, "manifest": m.sha256,
                                "answers": {"22:1": "y", "22:5": "q", "-7:1": "n"}}
    assert load_answers(answers_file(tmp_path, text), m) == {
        (22, 1): "y", (22, 5): "q", (-7, 1): "n"}


@needs_node
def test_an_untouched_page_exports_no_answer(tmp_path):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      return [logic.exportText(data, new Map()), logic.counts(data, new Map()),
              logic.counts(data, new Map([["22:1", "y"], ["22:2", "n"], ["22:3", "q"]]))];
    """)
    assert load_answers(answers_file(tmp_path, got[0]), m) == {}
    assert got[1:] == [{"y": 0, "n": 0, "q": 0, "blank": 6}, {"y": 1, "n": 1, "q": 1, "blank": 3}]


def test_the_embedded_data_is_enough_to_answer_by(tmp_path):
    """The export without node: the same four fields, from what the page was given."""
    m = manifest(EVERYTHING)
    data = json.loads(embedded(render_page(m)))
    keys = [f"{c['series']}:{c['item']}" for c in data["candidates"]]
    raw = {"format": data["format"], "account": data["account"], "manifest": data["sha256"],
           "answers": dict.fromkeys(keys, "q")}
    assert list(load_answers(answers_file(tmp_path, raw), m)) == [
        (c.series, c.item) for c in m.candidates]


@needs_node
def test_the_page_opens_with_nothing_answered(tmp_path):
    got = run(tmp_path, manifest(EVERYTHING), """
      const p = open();
      p.openAll();
      const states = p.byId.days.all((e) => e.tag === "button")
        .map((e) => e.getAttribute("aria-pressed")).filter((s) => s !== null);
      return [p.items().length, states, p.storage.held.size, p.byId.start.hidden];
    """)
    assert got == [6, ["false"] * 18, 0, True]


@needs_node
def test_names_and_facts_are_shown_as_they_are(tmp_path):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      const p = open();
      p.openAll();
      return p.items().map((i) => [i.cls("name")[0].textContent, i.textContent]);
    """)
    shown = dict(got)
    hostile = HOSTILE.replace(chr(0x202E), "")
    assert set(shown) == {hostile, "Object 2", "Object 3", "Object 4", "Object 5",
                          "(no name in the catalogue)"}
    flags = {
        "one photo, or over 1 km away": {"Object 2"},
        "every photo on several days has exactly the same coordinate: check it is not a frozen "
        "or false position": {"Object 3"},
        "a photo that day has an impossible jump": {"Object 4"},
        "2 photos nearby were taken at travel speed and do not count": {"Object 5"},
    }
    for flag, names in flags.items():
        assert {n for n, text in shown.items() if flag in text} == names, flag
    for fact in ("Nearest ground photo: 500 m away", "2026-05-01 14:05", "by the photo's own clock",
                 "3 photos on 2 days", "your own camera"):
        assert fact in shown[hostile], fact
    assert "1.5 km away" in shown["Object 2"] and "1 photo on 1 day" in shown["Object 2"]
    assert "AirDrop" in shown["Object 5"]


@needs_node
def test_items_are_by_day_then_series_then_distance_with_the_flagged_last(tmp_path):
    day = dt.datetime(2026, 5, 2, 9)
    m = manifest([candidate(1, km=0.9), candidate(2, km=0.1, conflict=True),
                  candidate(3, km=0.2, one_spot=True), candidate(4, km=0.3),
                  candidate(5, series=5, km=1.9), candidate(6, at=day), candidate(7, km=0.3)])
    got = run(tmp_path, m, """
      const p = open();
      p.openAll();
      return p.days().map((d) => [d.cls("when")[0].textContent,
                                  d.cls("name").map((n) => n.textContent)]);
    """)
    assert got == [
        ["2026-05-01, Friday", [f"Object {n}" for n in (5, 4, 7, 1, 2, 3)]],
        ["2026-05-02, Saturday", ["Object 6"]],
    ]


@needs_node
def test_an_answer_is_given_changed_cleared_and_counted(tmp_path):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      const p = open();
      p.openAll();
      const out = [];
      const note = () => out.push([p.pressed(), p.byId.counts.textContent]);
      p.press(p.item("Object 2"), "I was there"); note();
      p.press(p.item("Object 2"), "I was there"); note();
      p.press(p.item("Object 2"), "I was not there"); note();
      p.press(p.item("Object 3"), "I don't remember"); note();
      p.press(p.item("Object 2"), "Clear this answer"); note();
      return out;
    """)
    words = [g[0] for g in got]
    assert words == [["I was there"], ["I was there"], ["I was not there"],
                     ["I was not there", "I don't remember"], ["I don't remember"]]
    assert "1 there" in got[1][1] and "5 with no answer" in got[1][1]
    assert "0 there" in got[4][1] and "1 don't remember" in got[4][1]


@needs_node
def test_download_and_copy_give_the_same_file(tmp_path):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      const p = open();
      p.openAll();
      p.press(p.item("Object 4"), "I was there");
      p.byId.download.click();
      const during = p.revoked.length;
      p.timers.forEach((f) => f());
      p.byId.copy.click();
      await null; await null;
      const a = p.clicked[0], said = p.byId.said.textContent;
      p.press(p.item("Object 3"), "I was there");
      return {text: p.blobs[0].text, type: p.blobs[0].type, name: a.getAttribute("download"),
              link: a.getAttribute("href"), during, revoked: p.revoked, copied: p.copied,
              said, after: p.byId.said.textContent};
    """)
    assert load_answers(answers_file(tmp_path, got["text"]), m) == {(22, 4): "y"}
    assert got["copied"] == [got["text"]] and got["type"] == "application/json"
    assert got["name"] == "series-answers.json" and got["link"] == "blob:1"
    assert got["during"] == 0 and got["revoked"] == ["blob:1"]
    assert "1 answer" in got["said"] and "5 items have no answer" in got["said"]
    assert got["after"] == ""  # what was said is not about a file with the later answer


@needs_node
@pytest.mark.parametrize("clipboard", ["absent", "refused"])
def test_without_a_clipboard_the_text_is_there_to_select(tmp_path, clipboard):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, f"const clipboard = '{clipboard}';" + """
      const p = open({clipboard});
      p.openAll();
      const before = p.byId.text.hidden;
      p.byId.copy.click();
      await null; await null; await null;
      const shown = [p.byId.text.hidden, p.byId.text.value];
      p.press(p.item("Object 4"), "I was there");
      return [before, ...shown, p.byId.text.value];
    """)
    assert got[:2] == [True, False]
    assert load_answers(answers_file(tmp_path, got[2]), m) == {}
    assert load_answers(answers_file(tmp_path, got[3]), m) == {(22, 4): "y"}  # never out of date


# -- one stored answer per item


def stored(m, *keys):
    """The names this list's answers are stored under."""
    return [f"wanderfill-series-review:{m.sha256}:{key}" for key in keys]


@needs_node
def test_each_answer_is_stored_on_its_own(tmp_path):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      const storage = new Storage();
      const p = open({storage});
      p.openAll();
      const out = [];
      const step = (name, word) => {
        p.press(p.item(name), word);
        out.push(Object.fromEntries(storage.held));
      };
      step("Object 4", "I was there");
      step("Object 5", "I don't remember");
      step("Object 4", "I was not there");
      step("Object 4", "Clear this answer");
      step("Object 4", "Clear this answer");
      return [out, storage.log.filter(([op]) => op !== "getItem")];
    """)
    four, five = stored(m, "22:4", "22:5")
    assert got[0] == [{four: "y"}, {four: "y", five: "q"}, {four: "n", five: "q"}, {five: "q"},
                      {five: "q"}]
    assert got[1] == [["setItem", four], ["setItem", five], ["setItem", four],
                      ["removeItem", four], ["removeItem", four]]


@needs_node
def test_a_press_reads_and_writes_its_own_item_only(tmp_path):
    m = manifest(EVERYTHING)
    touched = run(tmp_path, m, """
      const storage = new Storage();
      const p = open({storage});
      p.openAll();
      storage.log.length = 0;
      p.press(p.item("Object 4"), "I was there");
      p.press(p.item("Object 4"), "Clear this answer");
      return [...new Set(storage.log.map(([, key]) => key))];
    """)
    assert touched == stored(m, "22:4")


@needs_node
def test_answers_come_back_on_a_reload_and_on_no_other_page(tmp_path):
    one, two = manifest(EVERYTHING), manifest(EVERYTHING, account=2)
    (mine,), (theirs,) = stored(one, "22:4"), stored(two, "22:4")
    given = f"const mine = {json.dumps(mine)}, theirs = {json.dumps(theirs)};"
    body = given + """
      const storage = new Storage();
      storage.held.set(mine, "y");
      storage.held.set(mine + "0", "y");
      const p = open({storage});
      p.openAll();
      const first = [p.pressed(), p.byId.kept.textContent];
      p.press(p.item("Object 4"), "I was not there");
      p.byId.download.click();
      return [...first, storage.held.get(mine), storage.held.get(theirs) || null,
              Object.keys(JSON.parse(p.blobs[0].text).answers)];
    """
    got = run(tmp_path, one, body)
    assert got[0] == ["I was there"] and "1 answer" in got[1]
    assert got[2:] == ["n", None, ["22:4"]]
    got = run(tmp_path, two, body)
    assert got[0] == [] and "1 answer" not in got[1]
    assert got[2:] == ["y", "n", ["22:4"]]  # the other list's answer is as it was


@needs_node
@pytest.mark.parametrize("text", [
    '{"22:1":"n","22:1":"y"}', '{"22:4":"y"}', "y", "",
])
def test_a_map_of_answers_in_one_value_is_not_read(tmp_path, text):
    """JSON keeps the last of two answers to one item, which nobody may have given."""
    m = manifest(EVERYTHING)
    name = f"wanderfill-series-review:{m.sha256}"
    got = run(tmp_path, m, f"const name = {json.dumps(name)}, text = {json.dumps(text)};" + """
      const storage = new Storage();
      storage.held.set(name, text);
      const p = open({storage});
      p.openAll();
      p.byId.download.click();
      return [p.pressed(), p.blobs[0].text, storage.held.get(name)];
    """)
    assert got[0] == [] and json.loads(got[1])["answers"] == {} and got[2] == text


@needs_node
@pytest.mark.parametrize("junk", ["Y", "yes", "", " y", "y ", "y\n", "yn", "null", '"y"', "{}",
                                  '{"22:4":"y"}', "undefined", "__proto__", "true"])
def test_a_stored_value_that_is_no_answer_is_not_used_and_said(tmp_path, junk):
    m = manifest(EVERYTHING)
    (four, five) = stored(m, "22:4", "22:5")
    given = f"const [four, five, junk] = {json.dumps([four, five, junk])};"
    got = run(tmp_path, m, given + """
      const storage = new Storage();
      storage.held.set(four, junk);
      storage.held.set(five, "n");
      const p = open({storage});
      p.openAll();
      p.byId.download.click();
      const at = [p.pressed(), p.blobs[0].text, p.byId.unused.hidden, p.byId.unused.textContent,
                  storage.held.get(four)];
      p.press(p.item("Object 4"), "I was there");
      return [...at, storage.held.get(four), p.byId.unused.hidden, p.pressed()];
    """)
    assert got[0] == ["I was not there"]
    assert json.loads(got[1])["answers"] == {"22:5": "n"}
    assert got[2] is False and "could not be read" in got[3] and "1 item" in got[3]
    assert got[4] == junk  # only a press writes
    assert got[5:7] == ["y", True]
    assert sorted(got[7]) == ["I was not there", "I was there"]


@needs_node
def test_a_page_with_nothing_odd_stored_does_not_say_so(tmp_path):
    assert run(tmp_path, manifest(), "return open().byId.unused.hidden;") is True


@needs_node
def test_a_cleared_answer_does_not_come_back_from_the_other_tab(tmp_path):
    """Tab A has read a yes; B clears it; A answers something else."""
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      const storage = new Storage();
      const first = open({storage});
      first.openAll();
      first.press(first.item("Object 2"), "I was there");
      const b = open({storage});
      b.openAll();
      /* B clears after whatever A reads, and before A writes. */
      let due = true;
      const a = open({storage: new View(storage, (op, when) => {
        if (due && when === "before" && op !== "getItem") {
          due = false;
          b.press(b.item("Object 2"), "Clear this answer");
        }
      })});
      a.openAll();
      const before = a.pressed();
      a.press(a.item("Object 3"), "I was not there");
      a.byId.download.click();
      const again = open({storage});
      again.openAll();
      return [before, a.blobs[0].text, a.pressed(), again.pressed(), a.byId.kept.textContent];
    """)
    assert got[0] == ["I was there"]
    assert load_answers(answers_file(tmp_path, got[1]), m) == {(22, 3): "n"}
    assert got[2] == got[3] == ["I was not there"] and "not being kept" not in got[4]


@needs_node
@pytest.mark.parametrize("word,left", [("I was not there", "n"), ("Clear this answer", None)])
def test_two_tabs_at_once_on_two_items_both_count(tmp_path, word, left):
    """B's whole press, at each point A's own press can be cut at."""
    m = manifest(EVERYTHING)
    rows = run(tmp_path, m, f"const word = {json.dumps(word)};" + """
      const out = [];
      for (let at = 0; at < 8; at += 1) {
        const storage = new Storage();
        const first = open({storage});
        first.openAll();
        first.press(first.item("Object 2"), "I was there");
        const b = open({storage});
        b.openAll();
        let n = -1, fired = false;
        const a = open({storage: new View(storage, () => {
          if (n >= 0 && n++ === at) {
            fired = true;
            b.press(b.item("Object 2"), word);
          }
        })});
        a.openAll();
        n = 0;
        a.press(a.item("Object 3"), "I was there");
        n = -1;
        if (!fired) break;
        const value = (p) => {
          p.byId.download.click();
          return JSON.parse(p.blobs[0].text).answers;
        };
        out.push([value(a), value(b), value(open({storage})), a.byId.kept.textContent]);
      }
      return out;
    """)
    wanted = {"22:3": "y"} | ({"22:2": left} if left else {})
    assert len(rows) >= 4
    for a, b, again, kept in rows:
        assert a == b == again == wanted and "not being kept" not in kept


@needs_node
def test_two_tabs_on_one_item_show_what_is_stored(tmp_path):
    m = manifest(EVERYTHING)
    rows = run(tmp_path, m, """
      const out = [];
      for (let at = 0; at < 8; at += 1) {
        const storage = new Storage();
        const b = open({storage});
        b.openAll();
        let n = -1, fired = false;
        const a = open({storage: new View(storage, () => {
          if (n >= 0 && n++ === at) {
            fired = true;
            b.press(b.item("Object 2"), "I was not there");
          }
        })});
        a.openAll();
        n = 0;
        a.press(a.item("Object 2"), "I was there");
        n = -1;
        if (!fired) break;
        const shown = a.pressed(), held = [...storage.held.values()];
        const kept = a.byId.kept.textContent;
        a.press(a.item("Object 4"), "I was there");
        const p = open({storage});
        p.byId.download.click();
        out.push([shown, held, kept, Object.keys(JSON.parse(p.blobs[0].text).answers)]);
      }
      return out;
    """)
    words = {"y": "I was there", "n": "I was not there"}
    assert {held[0] for _, held, _, _ in rows} == {"y", "n"}  # each of them last, at some cut
    for shown, (value,), kept, later in rows[:-1]:
        assert shown == [words[value]] and "not being kept" not in kept
        assert later == ["22:2", "22:4"]  # and what came after was stored
    shown, (value,), kept, later = rows[-1]  # after A's last look: the event tells it
    assert value == "n" and shown == ["I was there"] and later == ["22:2", "22:4"]


@needs_node
@pytest.mark.parametrize("storage", [
    "null", "{getItem() { throw 1; }, setItem() { throw 1; }, removeItem() { throw 1; }}",
    "{getItem: () => null, setItem() { throw 1; }, removeItem() {}}",
    "{getItem: () => null, setItem() {}, removeItem() {}}",
    "{getItem() { throw 1; }, setItem() {}, removeItem() {}}",
])
def test_the_page_works_without_storage_and_says_so(tmp_path, storage):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, f"const storage = {storage};" + """
      const p = open({storage});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      p.press(p.item("Object 3"), "I was not there");
      p.press(p.item("Object 4"), "I was there");
      p.press(p.item("Object 4"), "Clear this answer");
      p.tell("storage", {});
      p.byId.download.click();
      return [p.pressed(), p.blobs[0].text, p.byId.kept.textContent];
    """)
    assert got[0] == ["I was there", "I was not there"]
    assert load_answers(answers_file(tmp_path, got[1]), m) == {(22, 2): "y", (22, 3): "n"}
    assert "not being kept" in got[2]


@needs_node
def test_storage_that_cannot_be_read_is_said_before_any_press_and_while_it_lasts(tmp_path):
    m = manifest(EVERYTHING)
    (four,) = stored(m, "22:4")
    got = run(tmp_path, m, f"const four = {json.dumps(four)};" + """
      const storage = new Storage();
      storage.held.set(four, "y");
      storage.getItem = () => { throw new Error("off"); };
      const p = open({storage});
      p.openAll();
      const out = [];
      const note = () => {
        out.push([p.pressed(), p.byId.kept.textContent.includes("not being kept")]);
      };
      note();
      delete storage.getItem;
      p.tell("storage", {}); note();
      storage.getItem = () => { throw new Error("off"); };
      p.tell("storage", {}); note();   /* what was read stays */
      return out;
    """)
    assert got == [[[], True], [["I was there"], False], [["I was there"], True]]


@needs_node
@pytest.mark.parametrize("op", ["getItem", "setItem", "removeItem"])
def test_an_operation_that_starts_to_throw_holds_what_was_pressed_since(tmp_path, op):
    """Object 2 was yes, and is cleared; Object 3 is answered; the storage keeps its old say."""
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, f"const op = '{op}';" + """
      const storage = new Storage();
      const p = open({storage});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      p.press(p.item("Object 5"), "I don't remember");
      const good = storage[op];
      storage[op] = () => { throw new Error(op); };
      p.press(p.item("Object 2"), "Clear this answer");
      p.press(p.item("Object 3"), "I was not there");
      p.tell("storage", {});
      p.byId.download.click();
      const during = [p.blobs[0].text, p.pressed(), p.byId.kept.textContent];
      storage[op] = good;
      p.tell("storage", {});
      p.byId.download.click();
      return [...during, p.blobs[1].text, p.pressed(), p.byId.kept.textContent];
    """)
    wanted = {(22, 3): "n", (22, 5): "q"}
    for text in (got[0], got[3]):
        assert load_answers(answers_file(tmp_path, text), m) == wanted
    assert sorted(got[1]) == sorted(got[4]) == ["I don't remember", "I was not there"]
    # What was asked of the operation that threw is held; with setItem the clear went through.
    assert "not being kept" in got[2] and "not being kept" in got[5]


@needs_node
def test_a_quota_that_fails_one_item_holds_that_item_and_stores_the_rest(tmp_path):
    m = manifest(EVERYTHING)
    (two, three, four) = stored(m, "22:2", "22:3", "22:4")
    got = run(tmp_path, m, f"const [two, three, four] = {json.dumps([two, three, four])};" + """
      const storage = new Storage();
      const full = {on: true};
      const set = Storage.prototype.setItem;
      storage.setItem = function (k, v) {
        if (full.on && k === three) throw new Error("full");
        set.call(this, k, v);
      };
      const p = open({storage});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      p.press(p.item("Object 3"), "I was there");
      p.press(p.item("Object 4"), "I was not there");
      const held = Object.fromEntries(storage.held);
      storage.held.set(three, "n");   /* another tab, which had room */
      storage.held.set(two, "q");
      p.tell("storage", {});
      p.byId.download.click();
      const during = [p.blobs[0].text, p.byId.kept.textContent];
      full.on = false;
      p.press(p.item("Object 3"), "I don't remember");
      return [held, ...during, storage.held.get(three), p.byId.kept.textContent, p.pressed()];
    """)
    assert got[0] == {two: "y", four: "n"}
    assert load_answers(answers_file(tmp_path, got[1]), m) == {
        (22, 2): "q", (22, 3): "y", (22, 4): "n"}
    assert list(json.loads(got[1])["answers"]) == ["22:2", "22:3", "22:4"]  # the list's order
    assert "not being kept" in got[2]
    assert got[3] == "q" and "not being kept" not in got[4]  # it is kept once it can be


@needs_node
def test_a_write_that_does_not_take_is_held_and_one_that_another_tab_changed_is_not(tmp_path):
    m = manifest(EVERYTHING)
    (two, three) = stored(m, "22:2", "22:3")
    got = run(tmp_path, m, f"const [two, three] = {json.dumps([two, three])};" + """
      const storage = new Storage();
      const set = Storage.prototype.setItem;
      storage.held.set(two, "y");
      storage.setItem = () => {};
      storage.removeItem = () => {};
      const p = open({storage});
      p.openAll();
      const out = [];
      const note = () => {
        p.tell("storage", {});
        p.byId.download.click();
        out.push([JSON.parse(p.blobs[p.blobs.length - 1].text).answers,
                  p.byId.kept.textContent.includes("not being kept")]);
      };
      p.press(p.item("Object 2"), "I was there"); note();       /* as it was stored */
      p.press(p.item("Object 2"), "Clear this answer"); note(); /* did not take */
      p.press(p.item("Object 2"), "I was not there"); note();   /* nor did this */
      delete storage.setItem;
      p.press(p.item("Object 2"), "I was not there"); note();
      storage.setItem = function (k, v) { set.call(this, k, v); this.held.set(k, "q"); };
      p.press(p.item("Object 3"), "I was there"); note();       /* another tab's, over ours */
      delete storage.setItem;
      p.press(p.item("Object 4"), "I was there"); note();
      return [out, storage.held.size];
    """)
    assert got[0] == [
        [{"22:2": "y"}, False], [{}, True], [{"22:2": "n"}, True], [{"22:2": "n"}, False],
        [{"22:2": "n", "22:3": "q"}, False],
        [{"22:2": "n", "22:3": "q", "22:4": "y"}, False],
    ]
    assert got[1] == 3


SAY = {"y": "I was there", "n": "I was not there", "": "Clear this answer"}


@needs_node
@pytest.mark.parametrize("was, mine, theirs", [
    ("n", "y", "n"), ("", "y", ""), ("n", "", "n"), ("n", "y", ""), ("", "y", "n"),
], ids=str)
def test_what_another_tab_answers_after_this_one_wins(tmp_path, was, mine, theirs):
    """B's whole press at each point of A's. Back to what it was is an answer as well."""
    m = manifest(EVERYTHING)
    (two,) = stored(m, "22:2")
    given = f"const [two, was, mine, theirs] = {json.dumps([two, was, SAY[mine], SAY[theirs]])};"
    rows = run(tmp_path, m, given + """
      const out = [];
      for (let at = 0; at < 30; at += 1) {
        const storage = new Storage();
        if (was) storage.held.set(two, was);
        const b = open({storage});
        b.openAll();
        let n = -1, fired = false;
        const a = open({storage: new View(storage, () => {
          if (n >= 0 && n++ === at) {
            fired = true;
            b.press(b.item("Object 2"), theirs);
          }
        })});
        a.openAll();
        n = 0;
        a.press(a.item("Object 2"), mine);
        n = -1;
        if (!fired) break;
        const pressed = a.pressed();
        const lost = a.byId.kept.textContent.includes("not being kept");
        const value = (p) => {
          p.byId.download.click();
          return JSON.parse(p.blobs[0].text).answers["22:2"] || "";
        };
        out.push([lost, [...storage.held.keys()], value(a), value(b),
                  storage.held.get(two) || "", pressed]);
      }
      return out;
    """)
    assert len(rows) >= 4
    for lost, names, a, b, now, _ in rows:
        assert not lost and names in ([], [two])  # nothing is left behind by the look
        assert a == b == now
    for *_, now, pressed in rows[:-1]:  # after A's last look the event tells it
        assert pressed == ([SAY[now]] if now else [])
    assert {row[4] for row in rows} == {mine, theirs}


@needs_node
def test_another_answer_in_a_storage_with_no_room_left_wins_too(tmp_path):
    (two,) = stored(m := manifest(EVERYTHING), "22:2")
    got = run(tmp_path, m, f"const two = {json.dumps(two)};" + """
      const storage = new Storage();
      storage.held.set(two, "n");
      storage.setItem = function (k, v) {
        if (k !== two) throw new Error("full");
        Storage.prototype.setItem.call(this, k, "q"); /* and another tab, at once */
      };
      const p = open({storage});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      return [p.pressed(), p.byId.kept.textContent.includes("not being kept")];
    """)
    assert got == [["I don't remember"], False]


@needs_node
def test_what_changes_while_the_storage_is_looked_at_is_seen(tmp_path):
    """Another tab puts the old answer back, and then gives one more."""
    (two,) = stored(m := manifest(EVERYTHING), "22:2")
    got = run(tmp_path, m, f"const two = {json.dumps(two)};" + """
      const storage = new Storage();
      storage.held.set(two, "n");
      storage.setItem = function (k, v) {
        Storage.prototype.setItem.call(this, k, k === two ? "n" : v);
        if (k !== two) this.held.set(two, "q");
      };
      const p = open({storage});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      return [p.pressed(), p.byId.kept.textContent.includes("not being kept")];
    """)
    assert got == [["I don't remember"], False]


@needs_node
@pytest.mark.parametrize("was, mine", [("", "y"), ("n", "y"), ("n", "")], ids=str)
def test_a_storage_that_drops_what_it_is_given_loses_no_answer(tmp_path, was, mine):
    m = manifest(EVERYTHING)
    (two,) = stored(m, "22:2")
    for drops in (["setItem", "removeItem"], ["removeItem"] if mine == "" else ["setItem"]):
        given = f"const [two, was, mine, drops] = {json.dumps([two, was, SAY[mine], drops])};"
        got = run(tmp_path, m, given + """
          const storage = new Storage();
          if (was) storage.held.set(two, was);
          const p = open({storage});
          p.openAll();
          for (const op of drops) storage[op] = () => {};
          const out = [];
          const note = () => {
            p.byId.download.click();
            out.push(JSON.parse(p.blobs[p.blobs.length - 1].text).answers["22:2"] || "",
                     p.byId.kept.textContent.includes("not being kept"));
          };
          p.press(p.item("Object 2"), mine);
          p.tell("storage", {});
          note();
          for (const op of drops) delete storage[op];
          p.press(p.item("Object 2"), mine);
          note();
          return out;
        """)
        assert got == [mine, True, mine, False]


NO_TEXT = ["undefined", "false", "0", "({})", "['y']", "new String('y')"]


@needs_node
@pytest.mark.parametrize("value", NO_TEXT)
def test_a_read_after_a_press_that_is_no_text_holds_the_press(tmp_path, value):
    """The write went through, and then nothing could be read: it is not known to be stored."""
    m = manifest(EVERYTHING)
    (two,) = stored(m, "22:2")
    got = run(tmp_path, m, f"const two = {json.dumps(two)}, odd = () => {value};" + """
      const storage = new Storage();
      const p = open({storage});
      p.openAll();
      let n = 0;
      storage.getItem = function (k) {
        n += 1;
        return n === 1 ? Storage.prototype.getItem.call(this, k) : odd();
      };
      p.press(p.item("Object 2"), "I was there");
      const during = [p.pressed(), p.byId.kept.textContent.includes("not being kept"), n];
      delete storage.getItem;
      storage.held.set(two, "n");
      p.tell("storage", {});
      p.byId.download.click();
      return [...during, JSON.parse(p.blobs[0].text).answers];
    """)
    assert got == [["I was there"], True, 2, {"22:2": "y"}]


@needs_node
@pytest.mark.parametrize("value", NO_TEXT)
@pytest.mark.parametrize("odd_reads", [[1], [2], [1, 2]], ids=str)
def test_a_clear_that_did_nothing_and_cannot_be_read_back_stays_cleared(
        tmp_path, value, odd_reads):
    """Before the write, after it, or both."""
    m = manifest(EVERYTHING)
    (two,) = stored(m, "22:2")
    given = f"const two = {json.dumps(two)}, odd = () => {value}, odds = {odd_reads};"
    got = run(tmp_path, m, given + """
      const storage = new Storage();
      storage.held.set(two, "y");
      const p = open({storage});
      p.openAll();
      let n = 0;
      storage.removeItem = () => {};
      storage.getItem = function (k) {
        n += 1;
        return odds.includes(n) ? odd() : Storage.prototype.getItem.call(this, k);
      };
      const before = p.pressed();
      p.press(p.item("Object 2"), "Clear this answer");
      const out = [before];
      const note = () => {
        p.tell("storage", {});
        p.byId.download.click();
        out.push([p.pressed(), JSON.parse(p.blobs[p.blobs.length - 1].text).answers,
                  p.byId.kept.textContent.includes("not being kept"), p.byId.unused.hidden]);
      };
      note();
      delete storage.getItem; /* as it was, with the old answer still in it */
      note();
      delete storage.removeItem;
      p.press(p.item("Object 2"), "Clear this answer");
      note();
      return [...out, storage.held.size];
    """)
    assert got == [["I was there"], [[], {}, True, True], [[], {}, True, True],
                   [[], {}, False, True], 0]


@needs_node
@pytest.mark.parametrize("value", NO_TEXT)
def test_a_pull_that_reads_no_text_keeps_what_was_read_before(tmp_path, value):
    m = manifest(EVERYTHING)
    (two, three) = stored(m, "22:2", "22:3")
    given = f"const [two, three] = {json.dumps([two, three])}, odd = () => {value};"
    got = run(tmp_path, m, given + """
      const storage = new Storage();
      storage.held.set(two, "y");
      storage.held.set(three, "n");
      const p = open({storage});
      p.openAll();
      storage.getItem = odd;
      const out = [];
      const note = () => {
        p.tell("storage", {});
        out.push([p.pressed(), p.byId.kept.textContent.includes("not being kept"),
                  p.byId.unused.hidden]);
      };
      note();
      delete storage.getItem;
      storage.held.delete(two);
      note();
      return out;
    """)
    assert got == [[["I was there", "I was not there"], True, True],
                   [["I was not there"], False, True]]


@needs_node
@pytest.mark.parametrize("press,later", [
    ("Clear this answer", "older than this page"), (None, None),
])
def test_the_note_after_a_copy_is_about_what_was_copied(tmp_path, press, later):
    """The browser takes its time, and the owner answers on."""
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, f"const word = {json.dumps(press)};" + """
      let text = null, done = null;
      const clipboard = {writeText: (t) => new Promise((ok) => { text = t; done = ok; })};
      const p = open({clipboard});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      p.press(p.item("Object 3"), "I was there");
      p.byId.copy.click();
      const early = p.byId.said.textContent;
      if (word) p.press(p.item("Object 2"), word);
      done();
      await null; await null; await null;
      return [early, text, p.byId.said.textContent, p.byId.counts.textContent];
    """)
    assert got[0] == ""  # nothing is said to be copied before it is
    assert load_answers(answers_file(tmp_path, got[1]), m) == {(22, 2): "y", (22, 3): "y"}
    assert "Copied" in got[2] and "2 answers in it. 4 items" in got[2]
    if later:
        assert later in got[2] and "1 there" in got[3]
    else:
        assert "older" not in got[2] and "2 there" in got[3]


@needs_node
def test_a_copy_that_is_changed_and_changed_back_is_not_older(tmp_path):
    got = run(tmp_path, manifest(EVERYTHING), """
      let done = null;
      const clipboard = {writeText: () => new Promise((ok) => { done = ok; })};
      const p = open({clipboard});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      p.byId.copy.click();
      p.press(p.item("Object 2"), "I was not there");
      p.press(p.item("Object 2"), "I was there");
      done();
      await null; await null; await null;
      return p.byId.said.textContent;
    """)
    assert "Copied" in got and "older" not in got and "1 answer in it" in got


@needs_node
def test_a_copy_refused_late_shows_the_answers_as_they_are_then(tmp_path):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      let refuse = null;
      const clipboard = {writeText: () => new Promise((_, no) => { refuse = no; })};
      const p = open({clipboard});
      p.openAll();
      p.press(p.item("Object 2"), "I was there");
      p.byId.copy.click();
      p.press(p.item("Object 2"), "Clear this answer");
      refuse(new Error("no"));
      await null; await null; await null;
      return [p.byId.text.hidden, p.byId.text.value, p.byId.said.textContent];
    """)
    assert got[0] is False and load_answers(answers_file(tmp_path, got[1]), m) == {}
    assert "0 answers in it" in got[2]


@needs_node
def test_a_store_takes_only_items_and_answers(tmp_path):
    m = manifest(EVERYTHING)
    got = run(tmp_path, m, """
      const storage = new Storage();
      const store = logic.openStore(() => storage, data);
      for (const [k, v] of [["22:9", "y"], ["__proto__", "y"], ["22:3", "yes"], ["22:3", true],
                            ["22:3", null], ["22:3", "toString"], ["22:9", ""]]) {
        store.set(k, v);
      }
      return [store.answers().size, storage.held.size, store.kept()];
    """)
    assert got == [0, 0, True]


@needs_node
def test_a_page_brought_back_by_the_browser_is_drawn_again(tmp_path):
    """Back and forward, or a restored filter: no event says what changed in between."""
    m = manifest(EVERYTHING)
    (four,) = stored(m, "22:4")
    got = run(tmp_path, m, f"const four = {json.dumps(four)};" + """
      const storage = new Storage();
      const p = open({storage});
      p.openAll();
      storage.held.set(four, "y");
      p.tell("pageshow", {persisted: true});
      const same = p.items().length, pressed = p.pressed();
      p.byId["by-answer"].value = "y";
      p.tell("pageshow", {persisted: false});
      p.openAll();
      return [same, pressed, p.byId.counts.textContent,
              p.items().map((i) => i.cls("name")[0].textContent)];
    """)
    assert got[:2] == [6, ["I was there"]] and "1 there" in got[2] and got[3] == ["Object 4"]


# -- names that rearrange what is around them

CONTROLS = [0x00, 0x09, 0x0A, 0x1F, 0x7F, 0x85, 0x9F, 0x061C, 0x200E, 0x200F,
            *range(0x202A, 0x202F), *range(0x2066, 0x206A)]


@needs_node
@pytest.mark.parametrize("code", CONTROLS, ids=hex)
def test_controls_are_taken_out_of_every_name_shown(tmp_path, code):
    bad = chr(code)
    name = f"Series {bad}51{bad}"
    m = manifest([candidate(1, series=51, series_name=name, name=f"{bad}Ob{bad}ject"),
                  candidate(2, series=15, series_name=bad, name=bad + bad),
                  candidate(3, series=7, series_name="", name="")],
                 coverage=COVERAGE | {"recall": {51: RECALL[22], 8: RECALL[22] | {"name": name},
                                                 9: RECALL[22] | {"name": bad}}})
    got = run(tmp_path, m, """
      const p = open();
      p.openAll();
      const texts = (nodes) => nodes.map((n) => n.textContent);
      return {
        options: texts(p.byId["by-series"].children),
        heads: texts(p.byId.days.all((e) => e.tag === "h3")),
        names: texts(p.byId.days.cls("name")),
        recall: p.byId.recall.children.slice(1).map((r) => r.children[0].textContent),
        all: p.order.filter((id) => id !== "data").map((id) => p.byId[id].textContent).join(""),
        data: data.candidates.map((c) => c.series_name),
      };
    """)
    assert got["options"] == ["All series", "Series 7", "Series 15", "Series 51 (51)"]
    assert got["heads"] == ["Series 7", "Series 15", "Series 51 (51)"]
    assert got["names"] == ["(no name in the catalogue)"] * 2 + ["Object"]
    assert got["recall"] == ["Series 51 (8)", "Series 9", "Series 51 (51)"]
    assert bad not in got["all"]
    assert got["data"] == [name, bad, ""]  # what is hashed and exported is as it was
    assert bad in json.loads(embedded(render_page(m)))["candidates"][0]["name"]


@needs_node
def test_two_series_that_look_alike_are_told_apart_by_id(tmp_path):
    m = manifest([candidate(1, series=5, series_name="Bridges"),
                  candidate(1, series=6, series_name="Bridges" + chr(0x200F))])
    got = run(tmp_path, m, """
      return open().byId["by-series"].children.map((o) => o.textContent);
    """)
    assert got == ["All series", "Bridges (5)", "Bridges (6)"]


@needs_node
def test_names_are_isolated_where_they_stand_beside_other_text(tmp_path):
    got = run(tmp_path, manifest(), """
      const p = open();
      p.openAll();
      return [p.byId.recall.children[1].children[0].getAttribute("dir"),
              p.byId.days.cls("name")[0].getAttribute("dir"),
              p.byId.days.all((e) => e.tag === "h3")[0].getAttribute("dir")];
    """)
    assert got == ["auto"] * 3
    style = render_page(manifest()).split("<style>")[1].split("</style>")[0]
    for selector in (r"h3", r"\.name", r"th, td"):
        assert re.search(rf"^{selector} {{[^}}]*unicode-bidi: isolate", style, re.M), selector


# -- the buttons and the bar


def rule(selector):
    style = render_page(manifest()).split("<style>")[1].split("</style>")[0]
    (found,) = re.findall(rf"^{re.escape(selector)} {{(.*?)}}", style, re.M | re.S)
    return found


def test_a_pressed_button_keeps_its_size():
    """Wider when pressed, the second click of a double click could land on its neighbour."""
    pressed = rule('button[aria-pressed="true"]')
    for word in ("font", "padding", "border-width", "border:", "letter", "width", "margin"):
        assert word not in pressed, word


@needs_node
def test_the_bar_covers_neither_the_last_item_nor_the_focus(tmp_path):
    assert "position: sticky" in rule(".bar")  # it takes its own room at the end of the page
    assert "position: fixed" not in render_page(manifest())
    body = DATA.sub("", render_page(manifest())).split("<body>")[1]
    assert body.index('id="bar"') > body.index("</main>")
    got = run(tmp_path, manifest(), """
      const p = open();
      const first = p.root.style.scrollPaddingBottom;
      p.byId.bar.offsetHeight = 333;
      p.resized();
      return [first, p.root.style.scrollPaddingBottom, p.watched === p.byId.bar];
    """)
    assert got == ["100px", "333px", True]


@needs_node
def test_a_page_without_a_resize_observer_still_starts(tmp_path):
    assert run(tmp_path, manifest(), """
      return open({observer: false}).byId.start.hidden;
    """) is True


@needs_node
def test_flags_are_a_list_only_where_there_are_some(tmp_path):
    got = run(tmp_path, manifest(EVERYTHING), """
      const p = open();
      p.openAll();
      return p.items().map((i) => i.cls("flags").map((f) => f.children.length));
    """)
    assert sorted(got) == [[], [], [1], [1], [1], [1]]


@needs_node
def test_each_row_of_buttons_is_named_by_its_item(tmp_path):
    got = run(tmp_path, manifest(EVERYTHING), """
      const p = open();
      p.openAll();
      return p.items().map((i) => {
        const [row] = i.cls("buttons"), [name] = i.cls("name");
        return [row.getAttribute("role"), row.getAttribute("aria-labelledby"),
                name.getAttribute("id"), i.all((e) => e.getAttribute("role") === "group").length,
                row.all((e) => e.getAttribute("aria-pressed") !== null).length];
      });
    """)
    assert len({name for _, _, name, _, _ in got}) == 6
    for role, by, name, groups, pressable in got:
        assert role == "group" and by == name and groups == 1 and pressable == 3


@needs_node
def test_the_counts_are_said_once(tmp_path):
    """One polite region holds both, and a text that did not change is not written again."""
    page = DATA.sub("", render_page(manifest()))
    (region,) = re.findall(r'<div id="news" aria-live="polite" aria-atomic="false">(.*?)</div>',
                           page, re.S)
    for inside in ("counts", "said", "kept"):
        (tag,) = re.findall(rf'<p id="{inside}"[^>]*>', region)
        assert "role=" not in tag and "aria-live" not in tag
    got = run(tmp_path, manifest(EVERYTHING), """
      const p = open();
      p.openAll();
      const writes = () => [p.byId.counts.writes, p.byId.said.writes, p.byId.kept.writes];
      const out = [writes()];
      p.press(p.item("Object 2"), "I was there"); out.push(writes());
      p.press(p.item("Object 2"), "I was there"); out.push(writes());
      p.byId.download.click(); out.push(writes());
      p.tell("storage", {}); out.push(writes());
      return out;
    """)
    (c, s, k), pressed, again, saved, told = got
    assert pressed == again == [c + 1, s, k]
    assert saved == told == [c + 1, s + 1, k]


# -- what it says


@needs_node
def test_an_incomplete_list_says_so_above_everything(tmp_path):
    read = """
      const p = open();
      return [p.order[0], p.byId.incomplete.hidden, p.byId.incomplete.textContent];
    """
    whole = run(tmp_path, manifest(), read)
    assert whole == ["incomplete", True, ""]
    part = run(tmp_path, manifest(coverage=COVERAGE | {"tiles_unread": 3}), read)
    assert part[:2] == ["incomplete", False]
    for words in ("This list is incomplete", "3 of 15", "again"):
        assert words in part[2]
    body = render_page(manifest()).split("<body>")[1].lstrip()
    assert body.startswith('<div id="incomplete"')


@needs_node
def test_the_header_says_what_was_read_and_what_was_left_out(tmp_path):
    m = manifest(EVERYTHING, left_out=LEFT_OUT | {"other_origin": 8}, coverage=COVERAGE | {
        "skipped": {12: 31}, "recall": {22: RECALL[22], 23: RECALL[22] | {"name": "Bridges"}}})
    got = run(tmp_path, m, """
      const p = open();
      return [p.byId.about.textContent, p.byId["left-out"].textContent,
              p.byId.recall.textContent, p.byId["recall-note"].textContent];
    """)
    for words in ("Account 1", "2026-05-01", "2026-05-31", "2 km", "your own camera",
                  m.sha256[:12], "6 items", "12 map tiles"):
        assert words in got[0], words
    for words in ("5 photos taken at travel speed, over 150 km/h", "2 photos beside an impossible",
                  "3 photos in a stretch", "1 photo in a layover", "6 photos with no absolute",
                  "4 photos taken after 2026-05-31", "8 photos from another origin",
                  "31 things of series 12", "never offered: -2, 12, 17, 60"):
        assert words in got[1], words
    assert "Bridges" in got[2] and "0.2 km" in got[2] and "2 km" in got[2]
    assert "will miss" in got[3]


@needs_node
def test_a_page_with_nothing_in_it_says_so(tmp_path):
    m = manifest([], params=PARAMS | {"until": None},
                 coverage=COVERAGE | {"recall": {}, "tiles_unread": 1})
    got = run(tmp_path, m, """
      const p = open();
      return [p.byId.shown.textContent, p.byId.about.textContent, p.byId.incomplete.hidden];
    """)
    assert "No items" in got[0] and "0 items" in got[1] and got[2] is False


@needs_node
def test_a_day_is_built_when_it_is_opened(tmp_path):
    m = manifest([candidate(n, at=dt.datetime(2026, 5, 1 + n % 25, 12)) for n in range(3000)])
    got = run(tmp_path, m, """
      const p = open();
      const before = p.items().length;
      p.days()[0].toggle(true);
      const one = p.items().length;
      p.days()[0].toggle(false);
      p.days()[0].toggle(true);
      return [p.days().length, before, one, p.items().length];
    """)
    assert got == [25, 0, 120, 120]


@needs_node
def test_filters_narrow_the_list_and_never_move_an_item_being_answered(tmp_path):
    m = manifest([candidate(1, km=0.1), candidate(2, km=0.45), candidate(3, series=5, km=1.2),
                  candidate(4, series=5, km=0.2)])
    got = run(tmp_path, m, """
      const p = open();
      const names = () => {
        p.openAll();
        return p.items().map((i) => i.cls("name")[0].textContent);
      };
      const pick = (id, value) => { p.byId[id].value = value; p.byId[id].fire("change"); };
      const out = {all: names()};
      pick("by-series", "5"); out.series = names();
      pick("by-km", "0.5"); out.both = names();
      pick("by-series", ""); out.km = names();
      pick("by-km", ""); pick("by-answer", "blank"); out.blank = names();
      p.press(p.item("Object 1"), "I was there"); out.still = names();
      pick("by-answer", "blank"); out.gone = names();
      pick("by-answer", "y"); out.there = names();
      pick("by-answer", "n"); out.not = names();
      pick("by-answer", "answered"); out.answered = names();
      out.exported = Object.keys(logic.exportAnswers(data, new Map([["22:2", "n"]])).answers);
      out.counts = p.byId.counts.textContent;
      out.options = p.byId["by-series"].children.map((o) => o.textContent);
      return out;
    """)
    assert got["all"] == ["Object 4", "Object 3", "Object 1", "Object 2"]
    assert got["series"] == ["Object 4", "Object 3"] and got["both"] == ["Object 4"]
    assert got["km"] == ["Object 4", "Object 1", "Object 2"]
    assert got["blank"] == got["still"] == got["all"]
    assert got["gone"] == ["Object 4", "Object 3", "Object 2"]
    assert got["there"] == got["answered"] == ["Object 1"] and got["not"] == []
    assert got["exported"] == ["22:2"] and "3 with no answer" in got["counts"]
    assert got["options"] == ["All series", "Series 5 (5)", "Series 22 (22)"]


# ------------------------------------------------------------------ the answers

def answers(m=None, **kw):
    m = m or manifest()
    return {"format": 1, "account": 1, "manifest": m.sha256, "answers": {"22:1": "y"}} | kw


def test_answers_are_read_and_blank_ones_are_absent(tmp_path):
    m = manifest([candidate(n) for n in range(1, 5)])
    raw = answers(m, answers={"22:3": "q", "22:1": "y", "22:2": "n"})
    assert load_answers(answers_file(tmp_path, raw), m) == {
        (22, 3): "q", (22, 1): "y", (22, 2): "n"}
    assert load_answers(answers_file(tmp_path, answers(m, answers={})), m) == {}


def test_answers_to_the_same_list_made_another_day_are_good(tmp_path):
    path = answers_file(tmp_path, answers())
    assert load_answers(path, manifest(created="tomorrow")) == {(22, 1): "y"}


@pytest.mark.parametrize("change,why", [
    ({"manifest": "0" * 64}, "another list"), ({"manifest": None}, "another list"),
    ({"manifest": ""}, "another list"), ({"manifest": ["x"]}, "another list"),
    ({"account": 2}, "account"), ({"account": True}, "account"), ({"account": "1"}, "account"),
    ({"account": 1.0}, "account"), ({"account": None}, "account"),
    ({"format": 2}, "format"), ({"format": True}, "format"), ({"format": "1"}, "format"),
    ({"format": 1.0}, "format"),
    ({"answers": {"22:9": "y"}}, "22:9"), ({"answers": {"23:1": "y"}}, "23:1"),
    ({"answers": {"022:1": "y"}}, "022:1"), ({"answers": {" 22:1": "y"}}, "22:1"),
    ({"answers": {"22:+1": "y"}}, "22:"), ({"answers": {"1": "y"}}, "'1'"),
    ({"answers": {"": "y"}}, "''"), ({"answers": {"22:1:1": "y"}}, "22:1:1"),
    *(({"answers": {"22:1": v}}, "22:1") for v in (
        "Y", "yes", "", " y", "a", None, True, 1, ["y"], {"y": 1}, "yn")),
    ({"answers": None}, "answers"), ({"answers": []}, "answers"), ({"answers": "y"}, "answers"),
    ({"answers": [["22:1", "y"]]}, "answers"), ({"note": "x"}, "note"),
])
def test_answers_that_do_not_fit_are_refused(tmp_path, change, why):
    with pytest.raises(PlanInvalid, match=rf"series-answers\.json.*{re.escape(why)}"):
        load_answers(answers_file(tmp_path, answers(**change)), manifest())


@pytest.mark.parametrize("gone", ["format", "account", "manifest", "answers"])
def test_answers_without_a_field_are_refused(tmp_path, gone):
    raw = {k: v for k, v in answers().items() if k != gone}
    with pytest.raises(PlanInvalid, match=gone):
        load_answers(answers_file(tmp_path, raw), manifest())


@pytest.mark.parametrize("field", ["lat", "at", "name", "thin"])
def test_answers_to_a_list_since_changed_are_refused(tmp_path, field):
    path = answers_file(tmp_path, answers())
    changed = manifest([candidate(1), candidate(2, **{field: OTHER[field]})])
    with pytest.raises(PlanInvalid, match="another list"):
        load_answers(path, changed)


@pytest.mark.parametrize("content", [
    "", "{", "null", "[]", '"y"', "5", "[" * 100_000, b"\xff\xfe", b"\x00",
    '{"answers": NaN}', '{"answers": -Infinity}', '{"account": ' + "9" * 5000 + "}",
])
def test_a_file_that_is_not_answers_is_refused(tmp_path, content):
    with pytest.raises(PlanInvalid, match=r"series-answers\.json"):
        load_answers(answers_file(tmp_path, content), manifest())


def test_a_repeated_answer_is_refused(tmp_path):
    """json keeps the last of two, and the owner may have meant the first."""
    text = json.dumps(answers())
    for bad, why in ((text.replace('"22:1": "y"', '"22:1": "y", "22:1": "n"'), "22:1"),
                     (text.replace('"format": 1', '"format": 1, "format": 1'), "format")):
        with pytest.raises(PlanInvalid, match=f"'{why}' twice"):
            load_answers(answers_file(tmp_path, bad), manifest())


def test_a_huge_answers_file_is_refused_unread(tmp_path):
    text = json.dumps(answers())
    path = answers_file(tmp_path, text + " " * (5_000_000 - len(text)))
    assert load_answers(path, manifest())
    path.write_text(text + " " * (5_000_001 - len(text)), encoding="utf-8")
    with pytest.raises(PlanInvalid, match="5 MB"):
        load_answers(path, manifest())


def test_an_answers_file_that_cannot_be_opened_is_refused(tmp_path):
    for path in (tmp_path / "absent.json", tmp_path):
        with pytest.raises(PlanInvalid, match="cannot be read"):
            load_answers(path, manifest())


def test_review_output_is_never_committed():
    root = Path(__file__).parents[1]
    assert "review/" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()


# ------------------------------------------------------------------ the commands

ITEMS = {1: "Glass Hall", 2: "Tin Bridge", 3: "Paper Mill", 4: "Salt Tower"}


class Site:
    """The server as the commands read it. A write fails the test that sent it."""

    def __init__(self, account=1, regions=(4, 5), dare=(9,)):
        self.account, self.sent = account, []
        self.regions, self.dare = list(regions), list(dare)
        self.token, self.lang = "sekrit-token", "en"
        self.series = {22: self.reply("Series 22", ITEMS)}

    @staticmethod
    def reply(title, items, visited=()):
        return {"title": title, "items2": [{"id": i, "name": n} for i, n in items.items()],
                "visited": list(visited)}

    def webapi(self, action, **fields):
        self.sent.append((action, fields))
        return {
            "user/status-quick": {"uid": self.account},
            "maps/get-visited-regions-ids-simple": {"ids": self.regions},
            "maps/get-visited-dare-ids-simple": {"ids": self.dare},
        }[action]

    def ajax_json(self, path, **fields):
        self.sent.append((path, fields))
        assert (path, fields["action"]) == ("V2/", "getData")
        return self.series.get(fields["id"], {"result": "OK"})

    def ajax_text(self, path, **fields):
        raise AssertionError(f"a write was sent: {path} {fields}")

    def client(self):
        return NomadMania("fake", transport=self)

    def read(self, series):
        return sum(f.get("id") == series for _, f in self.sent)


@pytest.fixture
def site(monkeypatch, tmp_path):
    """One fake server, no token looked for, and a directory of its own to run in."""
    s = Site()
    s.asked = []

    def client_for(args, **kw):
        s.asked.append(kw)
        return s.client()

    monkeypatch.setattr(cli, "_client", client_for)
    monkeypatch.setattr(cli, "DEFAULT_WORKDIR", tmp_path / "home")
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    return s


def run_cli(*argv):
    try:
        return cli.main([str(a) for a in argv])
    except SystemExit as exc:
        return exc.code


def last_line(capsys):
    return capsys.readouterr().err.splitlines()[-1]  # after the progress


# -- the two readings of one series


def test_a_series_is_one_request_and_gives_names_and_ticks():
    s = Site()
    s.series[22]["items2"].append({"id": "5", "name": "Lead Gate", "more": 1})
    s.series[22]["visited"] = ["2", 3]
    info, ticked = review.read_series(s.client(), 22)
    assert info == SeriesInfo("Series 22", ITEMS | {5: "Lead Gate"}) and ticked == {2, 3}
    assert len(s.sent) == 1


@pytest.mark.parametrize("change", [
    {"visited": None}, {"visited": {}}, {"visited": "1"}, {"visited": [1, None]},
    {"visited": [True]}, {"visited": [{"id": 1, "name": "Secret Cave"}]}, {"visited": ["1.0"]},
    {"items2": None}, {"items2": {}}, {"items2": []}, {"items2": [7]},
    {"items2": [{"id": 1}]}, {"items2": [{"name": "Secret Cave"}]},
    {"items2": [{"id": 1, "name": 5}]}, {"items2": [{"id": None, "name": "Secret Cave"}]},
    {"items2": [{"id": True, "name": "Secret Cave"}]},
    {"items2": [{"id": 1, "name": "Secret Cave"}, {"id": "1", "name": "Other Cave"}]},
    {"title": None}, {"title": 5},
], ids=str)
def test_a_series_reply_of_another_shape_is_refused_and_not_quoted(change):
    s = Site()
    s.series[22] = {"title": "Secret Cave", "a": "Secret Cave"} | s.series[22] | change
    with pytest.raises(VerificationFailed) as e:
        review.read_series(s.client(), 22)
    assert "series 22" in str(e.value)
    assert "Cave" not in str(e.value) and "Glass" not in str(e.value)


@pytest.mark.parametrize("reply", [None, [], "x", 5, {}, {"result": "ERROR", "why": "Glass Hall"}])
def test_a_series_reply_that_is_no_series_is_refused(reply):
    s = Site()
    s.series[22] = reply
    for read in (review.read_series, NomadMania.series_ticked):
        with pytest.raises(VerificationFailed) as e:
            read(s.client(), 22)
        assert "Glass" not in str(e.value)


@pytest.mark.parametrize("bad", ["Glass Hall", None, True, 5.5])
def test_a_tick_that_is_no_id_is_found_by_its_place_and_not_quoted(bad):
    s = Site()
    s.series[22] = {"visited": [4, "5", bad, "Glass Roof"]}
    with pytest.raises(VerificationFailed) as e:
        s.client().series_ticked(22)
    assert "visited[2]" in str(e.value) and "Glass" not in str(e.value)
    assert str(bad) not in str(e.value)


def test_only_the_ticked_list_of_a_skipped_series_is_needed():
    s = Site()
    s.series[12] = {"visited": ["4"]}
    assert s.client().series_ticked(12) == {4}


# -- names in a terminal


@pytest.mark.parametrize("code", CONTROLS, ids=hex)
def test_controls_are_taken_out_of_a_name_for_the_terminal(code):
    bad = chr(code)
    assert review.shown(f"{bad}Tin {bad}Bridge{bad}") == "Tin Bridge"
    assert review.shown(bad, "item 3") == "item 3"


def test_the_terminal_and_the_page_take_out_the_same():
    page = (Path(review.__file__).parent / "review_page.html").read_text(encoding="utf-8")
    listed = re.search(r"const CONTROLS = (\[.*?\]);", page, re.S).group(1)
    pairs = [tuple(int(x, 16) for x in p)
             for p in re.findall(r"\[(0x[0-9a-f]+), (0x[0-9a-f]+)\]", listed)]
    for n in range(0x3000):
        out = any(low <= n <= high for low, high in pairs)
        assert review.shown("a" + chr(n) + "b") == ("ab" if out else "a" + chr(n) + "b"), hex(n)


# -- grading what was read


def test_each_origin_is_graded_on_its_own():
    """Two phones a country apart, taking turns: nobody flew between them."""
    mine = [photo(10.0, 20.0, m, uuid=f"a{m}") for m in (0, 20, 40)]
    theirs = [photo(30.0, 60.0, m, importer="com.apple.sharingd", uuid=f"b{m}")
              for m in (10, 30, 50)]
    since = dt.date(2026, 1, 1)
    shots, other = review.grade_by_origin([*mine, *theirs], (), {"own-camera", "airdrop"},
                                          since=since)
    assert [s.kind for s in shots] == ["ground"] * 6 and other == 0
    assert sorted(s.origin for s in shots) == ["airdrop"] * 3 + ["own-camera"] * 3
    assert set(kinds(*mine, *theirs)) == {"conflict"}  # what grading them as one would say
    shots, other = review.grade_by_origin([*mine, *theirs], (), {"airdrop"}, since=since)
    assert [s.origin for s in shots] == ["airdrop"] * 3 and other == 3


def test_a_flight_that_began_before_since_is_still_a_flight():
    """The photo it left from is before the first day asked for, and is still read."""
    flight = [photo(10.0, 20.0, -13 * 60), photo(10.0, 30.0, -11.9 * 60)]
    theirs = [photo(12.0, 60.0, m, importer=None) for m in (-3000, 600)]
    shots, other = review.grade_by_origin(flight + theirs, (), {"own-camera"},
                                          since=dt.date(2026, 5, 1))
    assert [(s.kind, s.taken.day) for s in shots] == [("fast", 1)] and other == 1


def test_until_is_passed_on_and_its_photos_are_counted():
    shots, _ = review.grade_by_origin(
        [photo(10.0, 20.0), photo(10.0, 20.0, 24 * 60)], (), {"own-camera"},
        since=dt.date(2026, 5, 1), until=dt.date(2026, 5, 1))
    assert [s.late for s in shots] == [False, True]


# -- review


class Library:
    """A Photos library that remembers how it was read."""

    photos: ClassVar[list] = []
    column = True
    opened: ClassVar[list] = []
    error = None

    def __init__(self, library=None, since=None, until=None, origins=None):
        self.importer_column = None
        self.records_origin = None
        Library.opened.append((library, since, until, origins))

    def assets(self):
        if Library.error:
            raise Library.error
        self.importer_column = Library.column
        self.records_origin = Library.column
        yield from Library.photos


@pytest.fixture
def world(site, monkeypatch, tmp_path):
    """Photos at three things of series 22, one of them ticked, and one thing with none."""
    from wanderfill.geo import tiles
    from wanderfill.sources import photos_app

    step = 24 * 60
    monkeypatch.setattr(Library, "opened", [])
    monkeypatch.setattr(Library, "error", None)
    monkeypatch.setattr(Library, "column", True)
    monkeypatch.setattr(Library, "photos", [
        photo(10.0, 20.0, 0), photo(10.5, 20.5, step), photo(10.5, 20.5, step + 30, uuid="u"),
        photo(11.0, 21.0, 2 * step),
        photo(12.0, 22.0, 3 * step, importer="com.apple.sharingd"),
    ])
    site.reader = FakeReader([
        SeriesPoint(1, 22, "", 10.0 + 0.1 * KM, 20.0), SeriesPoint(2, 22, "", 10.5, 20.5),
        SeriesPoint(3, 22, "", 11.0 + 1.5 * KM, 21.0), SeriesPoint(4, 22, "", 10.0, 20.2),
        SeriesPoint(8, 12, "", 10.0, 20.0), SeriesPoint(9, 12, "", 10.0, 20.0),
    ])
    site.series[22]["visited"] = [2]
    site.series[12] = {"visited": [9]}
    (tmp_path / "lib").mkdir()
    monkeypatch.setattr(photos_app, "PhotosAppSource", Library)
    monkeypatch.setattr(tiles, "TileReader", lambda *a, **kw: site.reader)
    return site


def do_review(*extra, out="out", since="2026-05-01"):
    return run_cli("series", "review", "--since", since, "--library", "../lib",
                   "--out", out, *extra)


def written_by(out="out"):
    paths = sorted(Path(out).iterdir())
    assert [p.suffix for p in paths] == [".html", ".json"]
    return paths[0], Manifest.load(paths[1])


def test_review_writes_its_two_files_and_sends_no_write(world, capsys, tmp_path):
    before = sorted(p for p in tmp_path.rglob("*"))
    assert do_review() == 0
    out = capsys.readouterr().out
    page, m = written_by()
    new = [p for p in sorted(tmp_path.rglob("*")) if p not in before]
    cwd = tmp_path / "cwd"
    assert new == [cwd / "out", cwd / page, cwd / page.with_suffix(".json")]
    assert page.stem == f"review-{m.sha256[:12]}"
    assert page.read_text(encoding="utf-8") == render_page(m)
    assert str(page) in out and str(page.with_suffix(".json")) in out
    assert [(c.item, c.name) for c in m.candidates] == [(1, "Glass Hall"), (3, "Paper Mill")]
    assert world.asked == [{}]  # no cached client


def test_review_files_are_the_owners_alone(world):
    assert do_review() == 0
    page, _ = written_by()
    assert Path("out").stat().st_mode & 0o777 == 0o700
    for p in (page, page.with_suffix(".json")):
        assert p.stat().st_mode & 0o777 == 0o600


def test_review_reads_live_once_per_series_of_the_harvest_and_after_it(world, monkeypatch):
    order = []
    points, ask = world.reader.series_points, Site.ajax_json
    monkeypatch.setattr(world.reader, "series_points",
                        lambda *a: order.append("tile") or points(*a))
    monkeypatch.setattr(Site, "ajax_json",
                        lambda self, p, **f: order.append("read") or ask(self, p, **f))
    assert do_review() == 0
    assert order == sorted(order, reverse=True) and order.count("read") == 2
    assert (world.read(22), world.read(12)) == (1, 1)


def test_review_records_what_it_did(world, tmp_path):
    spans = tmp_path / "s.json"
    spans.write_text(json.dumps([{"from": "2026-05-03", "to": "2026-05-03", "use": "none",
                                  "bbox": [20.9, 10.9, 21.1, 11.1], "why": "x"}]))
    assert do_review("--until", "2026-05-03", "--spans", spans, "--radius", "1.5") == 0
    _, m = written_by()
    assert m.account == 1 and [c.item for c in m.candidates] == [1]
    assert m.params == {
        "since": "2026-05-01", "until": "2026-05-03", "radius_km": 1.5,
        "origins": ["own-camera"], "skip": [-2, 12, 17, 60], "speeds": PARAMS["speeds"]}
    assert m.left_out == dict.fromkeys(left_out([]), 0) | {"span_none": 1, "other_origin": 1}
    assert m.coverage["skipped"] == {"12": 1}
    assert m.coverage["tiles_read"] == len(set(world.reader.asked))
    assert m.coverage["recall"] == {"22": {
        "ticked": 1, "located": 1, "no_photo": [], "name": "Series 22",
        "found": {"0.2": 1, "0.5": 1, "1.0": 1, "1.5": 1}}}


def test_the_speeds_recorded_are_the_speeds_used(world, monkeypatch):
    assert review.default_speeds() == PARAMS["speeds"]
    slow = PARAMS["speeds"] | {"fast_kmh": 2.0}
    monkeypatch.setattr(review, "default_speeds", lambda: slow)
    assert do_review() == 0
    _, m = written_by()
    assert m.params["speeds"]["fast_kmh"] == 2.0 and m.left_out["fast"] > 0


@pytest.mark.parametrize("radius,items", [("1", [1]), ("2", [1, 3]), ("0.05", []),
                                          ("20", [1, 3])])
def test_the_radius_is_the_one_asked_for(world, radius, items):
    assert do_review("--radius", radius) == 0
    assert [c.item for c in written_by()[1].candidates] == items


def test_review_reads_the_library_whole_so_that_no_leg_is_cut(world):
    assert do_review("--until", "2026-05-02") == 0
    ((library, since, until, _),) = Library.opened
    assert (Path(library), since, until) == (Path("../lib"), None, None)
    _, m = written_by()
    assert m.left_out["after_until"] == 1


def test_review_prints_counts_and_no_name_without_a_photo(world, capsys):
    assert do_review() == 0
    both = capsys.readouterr()
    for words in ("Series 22", "account 1", "other origin", "series 12"):
        assert words in both.out, words
    assert "Salt Tower" not in both.out + both.err
    page, _ = written_by()
    assert "Salt Tower" not in page.read_text() + page.with_suffix(".json").read_text()
    assert "sekrit" not in both.out + both.err


def test_review_prints_a_hostile_title_without_its_controls(world, capsys):
    world.series[22]["title"] = "Ser\u202eies\x1b[31m 22"
    assert do_review() == 0
    out = capsys.readouterr().out
    assert "Series[31m 22" in out and "\u202e" not in out and "\x1b" not in out


def test_an_unread_tile_is_exit_three_and_the_files_are_still_written(world, capsys):
    x, y = deg2tile(11.0, 21.0, 9)
    world.reader.broken = {(x, y)}
    assert do_review() == 3
    both = capsys.readouterr()
    assert "incomplete" in both.out + both.err
    _, m = written_by()
    assert m.coverage["tiles_unread"] == 1 and [c.item for c in m.candidates] == [1]
    assert m.coverage["tiles_read"] == len(set(world.reader.asked)) - 1


def test_no_photos_is_an_answer_and_unread_tiles_are_not_hidden_by_it(world, capsys):
    Library.photos = []
    assert do_review() == 0
    assert "no photo" in capsys.readouterr().out.lower()
    _, m = written_by()
    assert m.candidates == [] and m.coverage["tiles_read"] == 0
    flight = [photo(10.0, 20.0, 0), photo(10.0, 30.0, 60)]
    Library.photos = flight
    world.reader.broken = {deg2tile(10.0, 20.0, 9)}
    assert do_review(out="again") == 3
    assert "no photo" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("origin", [(), ("--origin", "unknown")])
def test_a_library_that_cannot_say_whose_a_photo_is_is_refused(world, capsys, origin):
    Library.column = False
    Library.photos = [dataclasses.replace(p, importer=None) for p in Library.photos]
    assert do_review(*origin) == 2
    assert "import" in capsys.readouterr().err
    assert not Path("out").exists() and world.sent == []


@pytest.mark.parametrize("error", [None, FileNotFoundError("no Photos database at x"),
                                   PermissionError("denied"), sqlite3.DatabaseError("not one"),
                                   ValueError("year 99999 is out of range")])
def test_a_library_that_cannot_be_read_is_a_plain_error(world, capsys, error):
    Library.error = error
    library = "../lib" if error else "../nowhere"
    assert run_cli("series", "review", "--since", "2026-05-01", "--library", library) == 2
    assert last_line(capsys).startswith("error: ")
    assert not Path("review").exists()


def test_a_real_library_without_the_column_and_one_that_is_not_there(site, tmp_path, capsys):
    from test_quirks import _photos_library

    lib = _photos_library(tmp_path, [(dt.datetime(2026, 5, 1, 12), 0, 10.0, 20.0)])
    for path in (lib, tmp_path / "no.photoslibrary"):
        assert run_cli("series", "review", "--since", "2026-05-01", "--library", path) == 2
        assert last_line(capsys).startswith("error: ")
    assert not Path("review").exists() and site.sent == []


@pytest.mark.parametrize("change", [
    {"visited": None}, {"items2": None}, {"items2": []}, {"title": None}], ids=str)
def test_a_series_that_cannot_be_read_stops_review(world, capsys, change):
    """An unread list must not read as "nothing ticked"."""
    world.series[22] |= change
    assert do_review() == 2
    assert last_line(capsys).startswith("error: ") and not Path("out").exists()


def test_a_skipped_series_that_cannot_be_read_stops_review(world, capsys):
    world.series[12] = {"title": "x"}
    assert do_review() == 2
    assert "series 12" in capsys.readouterr().err and not Path("out").exists()


def test_series_asked_for_are_exactly_the_ones_read(world):
    world.reader.points.append(SeriesPoint(1, 31, "", 10.0, 20.0))  # and no reply for 31
    assert do_review("--series", "22") == 0
    assert (world.read(22), world.read(12), world.read(31)) == (1, 0, 0)
    _, m = written_by()
    assert set(m.coverage["recall"]) == {"22"} and "skipped" not in m.coverage


def test_a_series_asked_for_is_read_though_no_tile_has_it(world):
    world.series[31] = Site.reply("Series 31", {7: "Zinc Pier"}, [7])
    assert do_review("--series", "31") == 0
    _, m = written_by()
    assert m.candidates == [] and m.coverage["recall"]["31"]["no_photo"] == [7]


def test_tile_objects_of_no_series_are_not_read_and_stop_nothing(world):
    """Live, -2 answers with no items and 0 with a list: neither is a series."""
    for series in (-2, -1, 0):
        world.reader.points.append(SeriesPoint(1, series, "", 10.0, 20.0))
        world.series[series] = [] if series == 0 else Site.reply("x", {})
    assert do_review() == 0
    assert [world.read(n) for n in (-2, -1, 0, 12, 22)] == [0, 0, 0, 1, 1]
    _, m = written_by()
    assert m.coverage["skipped"] == {"12": 1} and m.params["skip"] == [-2, 12, 17, 60]
    assert [c.item for c in m.candidates] == [1, 3]


@pytest.mark.parametrize("series", ["-2", "12", "17", "60", "0"])
def test_a_skipped_series_cannot_be_asked_for(world, capsys, series):
    assert do_review("--series", series) == 2
    assert last_line(capsys).startswith("error: ")
    assert not Path("out").exists() and world.sent == []


def test_origins_asked_for_widen_it_and_each_is_graded_alone(world):
    world.reader.points.append(SeriesPoint(1, 22, "", 0, 0))
    world.series[22]["items2"].append({"id": 5, "name": "Lead Gate"})
    world.reader.points.append(SeriesPoint(5, 22, "", 12.0, 22.0))
    noon = Library.photos[0]
    Library.photos = [*Library.photos, dataclasses.replace(
        noon, lat=40.0, importer="com.apple.sharingd", uuid="b",
        taken_utc=noon.taken_utc + dt.timedelta(minutes=1))]
    assert do_review("--origin", "airdrop", "--origin", "own-camera", "--origin", "airdrop") == 0
    _, m = written_by()
    assert {c.item: c.origins for c in m.candidates} == {
        1: ("own-camera",), 3: ("own-camera",), 5: ("airdrop",)}
    assert m.params["origins"] == ["airdrop", "own-camera"]
    assert m.left_out["conflict"] == 0 and m.left_out["other_origin"] == 0


@pytest.mark.parametrize("argv", [
    ("--since", "2026"), ("--since", "20260501"), ("--until", "soon"), ("--until", "2026-04-30"),
    ("--radius", "-1"), ("--radius", "nan"), ("--radius", "inf"), ("--radius", "far"),
    ("--radius", "0"), ("--radius", "-0.0"), ("--radius", "20.01"), ("--radius", "2000"),
    ("--origin", "mine"), ("--spans", "nowhere.json"), ("--series", "x"),
])
def test_review_arguments_that_make_no_sense_are_exit_two(world, argv):
    assert do_review(*argv) == 2
    assert not Path("out").exists() and world.sent == [] and not Library.opened


def test_review_needs_since(world):
    assert run_cli("series", "review") == 2


def test_the_default_directory_is_the_ignored_one(world):
    assert run_cli("series", "review", "--since", "2026-05-01", "--library", "../lib") == 0
    assert len(list(Path("review").iterdir())) == 2


def test_the_same_list_again_is_rewritten_and_another_has_another_name(world):
    assert do_review() == 0
    page, m = written_by()
    assert do_review() == 0
    assert written_by()[1].sha256 == m.sha256
    world.series[22]["visited"] = [1, 2]
    kept = page.read_bytes()
    assert do_review() == 0
    assert len(list(Path("out").iterdir())) == 4 and page.read_bytes() == kept


def test_another_list_under_the_same_name_is_not_replaced(world, capsys):
    assert do_review() == 0
    page, _ = written_by()
    path = page.with_suffix(".json")
    path.write_text("{}")
    assert do_review() == 2
    assert path.read_text() == "{}" and str(path) in capsys.readouterr().err


@pytest.mark.parametrize("suffix", [".json", ".html"])
def test_review_writes_through_no_symlink(world, suffix, capsys, tmp_path):
    assert do_review() == 0
    page, _ = written_by()
    other = page.with_suffix({".json": ".html", ".html": ".json"}[suffix])
    link, target = page.with_suffix(suffix), tmp_path / "target"
    target.write_bytes(link.read_bytes())
    link.unlink()
    link.symlink_to(target)
    was, beside = target.read_bytes(), other.stat().st_mtime_ns
    assert do_review() == 2
    assert link.is_symlink() and target.read_bytes() == was
    assert other.stat().st_mtime_ns == beside and "symbolic link" in capsys.readouterr().err
    target.unlink()  # dangling
    assert do_review() == 2 and not target.exists()


@pytest.mark.parametrize("fails", [".json", ".html"])
def test_a_list_that_cannot_be_written_whole_is_not_there(world, monkeypatch, capsys, fails):
    replace = Path.replace

    def no_room(self, target):
        if Path(target).suffix == fails:
            raise OSError(28, "No space left on device")
        return replace(self, target)

    with monkeypatch.context() as broken:
        broken.setattr(Path, "replace", no_room)
        assert do_review() == 2
    assert last_line(capsys).startswith("error: ") and list(Path("out").iterdir()) == []
    assert do_review() == 0  # and with both files there, a failure takes neither
    page, m = written_by()
    monkeypatch.setattr(Path, "replace", no_room)
    assert do_review() == 2
    assert written_by()[1].sha256 == m.sha256 and page.read_text() == render_page(m)


def test_a_page_that_was_there_is_not_removed_by_a_failure_beside_it(world, monkeypatch):
    assert do_review() == 0
    page, _ = written_by()
    stored = page.with_suffix(".json")
    page.unlink()

    def no_room(self, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", no_room)
    assert do_review() == 2
    assert list(Path("out").iterdir()) == [stored]


def test_an_interrupt_between_the_two_files_leaves_neither(world, monkeypatch):
    replace = Path.replace

    def stop(self, target):
        if Path(target).suffix == ".html":
            raise KeyboardInterrupt
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", stop)
    assert do_review() == 130
    assert list(Path("out").iterdir()) == []


def test_a_temporary_file_somebody_else_made_stays(world, capsys):
    path = Path("x.html")
    theirs = Path(f".x.html.{os.getpid()}.part")
    theirs.write_text("theirs")
    with pytest.raises(FileExistsError):
        cli._write_private(path, "mine")
    assert theirs.read_text() == "theirs" and not path.exists()


@pytest.mark.parametrize("write", [cli._write_private, lambda p, t: cli._write_new(p, t)])
def test_a_file_cut_short_is_removed(tmp_path, monkeypatch, write):
    def full(fd, mode):
        os.close(fd)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fdopen", full)
    with pytest.raises(OSError, match="No space"):
        write(tmp_path / "x", "mine")
    assert list(tmp_path.iterdir()) == []


def test_a_new_file_is_there_whole_or_not_at_all(tmp_path, monkeypatch):
    """A run that was killed left half a plan, and the next one would not replace it."""
    path, fdopen, seen = tmp_path / "plan.json", os.fdopen, []

    def halfway(fd, mode):
        (part,) = tmp_path.iterdir()
        seen.append((path.exists(), part.name.endswith(".part"), part.stat().st_mode & 0o777))
        return fdopen(fd, mode)

    monkeypatch.setattr(os, "fdopen", halfway)
    cli._write_new(path, "mine")
    assert seen == [(False, True, 0o600)]
    assert list(tmp_path.iterdir()) == [path] and path.read_text() == "mine"
    assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1


@pytest.mark.parametrize("make", [
    lambda p: p.write_text("theirs"), lambda p: p.symlink_to("elsewhere"),
    lambda p: p.mkdir()], ids=["file", "dangling", "directory"])
def test_a_new_file_never_replaces_what_came_to_be_there(tmp_path, monkeypatch, make):
    """Also what is made while it is being written."""
    path, fdopen = tmp_path / "plan.json", os.fdopen

    def meanwhile(fd, mode):
        make(path)
        return fdopen(fd, mode)

    monkeypatch.setattr(os, "fdopen", meanwhile)
    with pytest.raises(FileExistsError):
        cli._write_new(path, "mine")
    assert list(tmp_path.iterdir()) == [path]
    assert path.is_symlink() or path.is_dir() or path.read_text() == "theirs"
    assert not (tmp_path / "elsewhere").exists()


def test_what_a_killed_run_left_does_not_stop_the_next_and_is_not_committed(tmp_path, monkeypatch):
    def killed(*a):
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(os, "fdopen", killed)
        m.setattr(Path, "unlink", lambda *a, **kw: None)
        with pytest.raises(KeyboardInterrupt):
            cli._write_new(tmp_path / "plan.json", "mine")
    (part,) = tmp_path.iterdir()
    cli._write_new(tmp_path / "plan.json", "mine")
    assert (tmp_path / "plan.json").read_text() == "mine"
    ignored = (Path(__file__).parents[1] / ".gitignore").read_text().split()
    assert part.suffix == ".part" and "*.part" in ignored


def test_an_out_that_cannot_be_a_directory_is_a_plain_error(world, capsys):
    Path("out").write_text("x")
    assert do_review() == 2
    assert last_line(capsys).startswith("error: ")


def test_without_the_geo_extra_review_says_so(world, monkeypatch, capsys):
    def missing(*a):
        raise ImportError("No module named 'mapbox_vector_tile'")

    monkeypatch.setattr(world.reader, "series_points", missing)
    assert do_review() == 2
    assert "geo" in capsys.readouterr().err and not Path("out").exists()


# -- plan


def listed(*items, **kw):
    rows = {1: {}, 2: {"km": 1.234, "photos": 1, "days": 1, "thin": True, "conflict": True,
                       "fast_near": 2, "one_spot": False}, 3: {}, 4: {"one_spot": True}}
    return manifest([candidate(i, name=ITEMS[i], **rows[i]) for i in items or ITEMS], **kw)


def answered(m=None, account=None, **given):
    """The two files, with the answers of ``i1="y"`` and so on."""
    m = m or listed()
    raw = {"format": 1, "account": account or m.account, "manifest": m.sha256,
           "answers": {f"22:{k[1:]}": v for k, v in given.items()}}
    Path("a.json").write_text(json.dumps(raw), encoding="utf-8")
    return m.save("m.json"), Path("a.json")


def do_plan(m=None, *extra, series=22, **given):
    return run_cli("series", "plan", *answered(m, **given), "--series", series, *extra)


def test_a_plan_is_the_yes_items_of_one_series_and_nothing_else(site, capsys):
    other = candidate(1, series=31, name="Zinc Pier")
    m = manifest([*listed().candidates, other])
    files = answered(m, i1="y", i2="n", i3="q")
    raw = json.loads(files[1].read_text())
    raw["answers"]["31:1"] = "y"
    files[1].write_text(json.dumps(raw))
    assert run_cli("series", "plan", *files, "--series", 22) == 0
    out = capsys.readouterr().out
    assert "one series; read it with `show -v`, then `check`" in out
    plan = Plan.load("plan-series-22.json")
    (op,) = plan.ops
    assert (op.kind, op.series, op.item, op.bucket, op.confidence, op.label) == (
        "tick_series", 22, 1, "apply", 0.0, "Glass Hall")
    assert op.key == Op("tick_series", series=22, item=1).key
    assert validate(plan) == [] and plan.account == 1
    assert "incomplete" not in out
    assert site.read(22) == 1 and site.read(31) == 0 and site.asked == [{}]


def test_a_plan_says_what_it_rests_on(site):
    files = answered(i1="y")
    assert run_cli("series", "plan", *files, "--series", 22) == 0
    plan, m = Plan.load("plan-series-22.json"), listed()
    c = site.client()
    assert plan.basis["fingerprint"] == fingerprint(basis_of(c.snapshot([]), []))
    assert set(plan.basis) == {"fingerprint", "series_ticked", "taken_at"}
    assert dt.datetime.fromisoformat(plan.basis["taken_at"]).tzinfo
    assert plan.params == m.params
    assert plan.sources == [
        {"kind": "manifest", "sha256": m.sha256},
        {"kind": "answers", "sha256": hashlib.sha256(files[1].read_bytes()).hexdigest()},
        {"kind": "photos", "origins": ["own-camera"]},
    ]


def test_the_ticks_of_the_series_are_in_the_basis(site):
    site.series[22]["visited"] = ["3", 2]
    assert do_plan(i1="y") == 0
    assert Plan.load("plan-series-22.json").basis["series_ticked"] == {"22": [2, 3]}


def test_proof_is_what_the_owner_was_shown_and_no_more(site):
    assert do_plan(i1="y", i2="y", i4="y") == 0
    one, two, four = Plan.load("plan-series-22.json").ops
    assert [p["kind"] for p in one.proof + two.proof + four.proof] == ["ground_photo"] * 3
    detail = one.proof[0]["detail"]
    for words in ("500 m", "2026-05-01 14:05", "3 photos", "2 days", "own-camera"):
        assert words in detail, words
    assert "thin" not in detail and "conflict" not in detail and "one spot" not in detail
    detail = two.proof[0]["detail"]
    for words in ("1.23 km", "1 photo on 1 day", "thin", "conflict", "2 fast photos"):
        assert words in detail, words
    assert "one spot" in four.proof[0]["detail"]
    assert all(len(op.evidence) == 1 and "answered" in op.evidence[0] for op in (one, two, four))


def test_show_and_check_read_the_plan(site, capsys, tmp_path):
    assert do_plan(i1="y") == 0
    assert run_cli("show", "-v", "plan-series-22.json") == 0
    assert "proof ground_photo" in capsys.readouterr().out
    assert run_cli("check", "plan-series-22.json", "--workdir", tmp_path / "w") == 0
    assert "Left to send: 1 of 1" in capsys.readouterr().out


@pytest.mark.parametrize("reply", [
    {"title": "Secret Cave"}, {"visited": {"Secret Cave": 1}}, {"visited": "Secret Cave"},
    {"visited": [1, "Secret Cave"]}, {"visited": [{"id": 1, "name": "Secret Cave"}]},
    ["Secret Cave"], "Secret Cave"], ids=str)
def test_check_and_apply_quote_nothing_of_a_series_they_cannot_read(
        site, capsys, tmp_path, reply):
    assert do_plan(i1="y") == 0
    site.series[22] = reply
    capsys.readouterr()
    assert run_cli("check", "plan-series-22.json", "--workdir", tmp_path / "w") == 1
    both = capsys.readouterr()
    assert "cannot be read" in both.out and "series 22" in both.out
    assert run_cli("apply", "plan-series-22.json", "--confirm", "--workdir", tmp_path / "w") == 2
    both = [both.out, both.err, *capsys.readouterr()]
    assert not any("Secret" in text for text in both)
    with pytest.raises(VerificationFailed) as e:
        site.client().series_ticked(22)
    assert "Secret" not in repr(e.value) + str(e.value) + str(e.value.payload)


def test_other_reads_keep_their_excerpt():
    s = Site()
    s.regions = {"why": "a portal"}
    with pytest.raises(VerificationFailed, match="a portal"):
        s.client().snapshot([])


def test_show_prints_no_control_and_the_file_keeps_the_words(site, capsys):
    said = "I \x1b[2Jwalked \u202eover\u2066 it,\nin the\train\x9b"
    assert do_named(said=said) == 0
    (op,) = Plan.load("plan-series-22.json").ops
    assert op.proof == [{"kind": "memory", "detail": said}]
    capsys.readouterr()
    assert run_cli("show", "-v", "plan-series-22.json") == 0
    assert "- proof memory: I [2Jwalked over it, in the rain\n" in capsys.readouterr().out
    bad = "\x1b]0;x\x07\u202e"
    op = dataclasses.replace(op, label=bad + "Label", method=bad + "Method",
                             evidence=[bad + "Said"], date_from=bad, bucket="review",
                             proof=[{"kind": bad, "detail": bad}, bad + "Odd", 5])
    plan = dataclasses.replace(Plan.load("plan-series-22.json"), ops=[op], created=bad, tool=bad,
                               params={bad: bad})
    plan.save("odd.json")
    for argv in (("show", "-v", "odd.json"), ("show", "odd.json")):
        assert run_cli(*argv) == 0
        out = capsys.readouterr().out
        assert not set(out) & {"\x1b", "\x07", "\u202e"}
    assert run_cli("show", "-v", "odd.json") == 0
    out = capsys.readouterr().out
    for word in ("Label", "Method", "Said", "Odd"):
        assert word in out


def test_apply_prints_labels_without_controls(site, tmp_path, capsys, monkeypatch):
    assert do_plan(i1="y", i2="y") == 0
    plan = Plan.load("plan-series-22.json")
    for op in plan.ops:
        op.label = "\x1b[2J\u202e" + op.label
    plan.save("odd.json")
    site.series[22]["visited"] = [1]
    monkeypatch.setattr(Site, "ajax_text", lambda self, path, **f: (
        self.series[22]["visited"].append(f["item"]) or "OK"))
    assert run_cli("apply", "odd.json", "--confirm", "--workdir", tmp_path / "w") == 0
    out = capsys.readouterr().out
    assert "Glass Hall" in out and "Tin Bridge" in out and not set(out) & {"\x1b", "\u202e"}


def test_apply_takes_the_plan_and_drift_stops_it(site, tmp_path, monkeypatch):
    assert do_plan(i1="y") == 0
    plan = Plan.load("plan-series-22.json")
    sent = []

    def tick(path, **f):
        sent.append((f["series"], f["item"], f["state"]))
        site.series[22]["visited"].append(f["item"])
        return "OK"

    site.ajax_text = tick
    site.regions.append(77)
    with pytest.raises(DriftError):
        apply_plan(site.client(), plan, workdir=tmp_path / "w", confirm=True)
    site.regions.remove(77)
    assert sent == []
    apply_plan(site.client(), plan, workdir=tmp_path / "w", confirm=True)
    assert sent == [(22, 1, 1)]


def test_a_list_of_another_account_is_refused(site, capsys):
    site.account = 2
    assert do_plan(i1="y") == 2
    assert "account 1" in capsys.readouterr().err
    assert not Path("plan-series-22.json").exists()
    assert site.read(22) == 0


@pytest.mark.parametrize("series", [31, 0, -2, 12])
def test_a_series_that_is_not_on_the_list_is_refused(site, series, capsys):
    assert do_plan(i1="y", series=series) == 1
    assert str(series) in capsys.readouterr().err
    assert not list(Path().glob("plan*")) and site.sent == []


def test_a_skipped_series_cannot_be_planned_even_from_a_made_up_list(site):
    m = manifest([candidate(1, series=12)])
    with pytest.raises(PlanInvalid):
        review.plan_for_series(m, {(12, 1): "y"}, 12, site.client())
    assert site.sent == []


def test_an_item_gone_from_the_series_refuses_the_whole_plan_and_is_named(site, capsys):
    site.series[22] = Site.reply("Series 22", {i: n for i, n in ITEMS.items() if i != 2})
    assert do_plan(i1="y", i2="y") == 2
    err = capsys.readouterr().err
    assert "Tin Bridge" in err and "Glass Hall" not in err
    assert not Path("plan-series-22.json").exists()
    assert do_plan(i1="y", i2="n") == 0  # only a yes has to be there


def test_an_item_gone_is_refused_though_it_is_ticked(site):
    site.series[22] = Site.reply("Series 22", {1: ITEMS[1]}, [2])
    assert do_plan(i1="y", i2="y") == 2


def test_an_id_that_names_another_thing_now_is_refused(site, capsys):
    site.series[22] = Site.reply("Series 22", ITEMS | {2: "Secret Cave"})
    assert do_plan(i1="y", i2="y") == 2
    err = capsys.readouterr().err
    assert "Tin Bridge" in err and "Secret" not in err
    site.series[22]["visited"] = [2]
    assert do_plan(i1="y", i2="y") == 0  # nothing is written to an id that is ticked


def test_items_already_ticked_are_dropped_and_named(site, capsys):
    site.series[22]["visited"] = ["2", 4]
    assert do_plan(i1="y", i2="y", i3="y") == 0
    out = capsys.readouterr().out
    assert "Tin Bridge" in out and "already ticked" in out and "Salt Tower" not in out
    assert [op.item for op in Plan.load("plan-series-22.json").ops] == [1, 3]


@pytest.mark.parametrize("given", [{}, {"i1": "n", "i2": "q"}, {"i3": "y"}])
def test_nothing_left_to_tick_writes_no_plan(site, capsys, given):
    site.series[22]["visited"] = [3]
    assert do_plan(**given) == 0
    out = capsys.readouterr().out
    assert "no plan" in out.lower() and not list(Path().glob("plan*"))
    assert ("Paper Mill" in out) == ("i3" in given)


def test_names_are_printed_without_controls(site, capsys):
    bad = "Tin\u202e Bri\x1bdge"
    m = manifest([candidate(1, name=bad), candidate(2, name="\u2066")])
    site.series[22] = Site.reply("x", {1: bad, 2: "\u2066"}, [1])
    assert do_plan(m, i1="y", i2="y") == 0
    out = capsys.readouterr().out
    assert "Tin Bridge" in out and "\u202e" not in out and "\x1b" not in out
    assert [op.label for op in Plan.load("plan-series-22.json").ops] == ["item 2"]


def test_an_incomplete_list_can_be_planned_and_is_said(site, capsys):
    m = listed(coverage=COVERAGE | {"tiles_unread": 2})
    assert do_plan(m, i1="y") == 0
    assert "incomplete" in capsys.readouterr().out


def test_a_plan_file_is_never_replaced(site, capsys):
    Path("plan-series-22.json").write_text("mine")
    assert do_plan(i1="y") == 2
    assert Path("plan-series-22.json").read_text() == "mine" and site.sent == []
    Path("dangling.json").symlink_to("elsewhere.json")
    assert do_plan(None, "--plan", "dangling.json", i1="y") == 2
    assert not Path("elsewhere.json").exists() and site.sent == []
    assert do_plan(None, "--plan", "other.json", i1="y") == 0
    assert Path("other.json").stat().st_mode & 0o777 == 0o600
    assert do_plan(None, "--plan", "no/where.json", i1="y") == 2
    assert capsys.readouterr().err.count("error: ") == 3


def test_a_plan_file_that_appeared_meanwhile_is_not_replaced(site, monkeypatch):
    snapshot = NomadMania.snapshot

    def slow(self, *a, **kw):
        Path("plan-series-22.json").write_text("mine")
        return snapshot(self, *a, **kw)

    monkeypatch.setattr(NomadMania, "snapshot", slow)
    assert do_plan(i1="y") == 2
    assert Path("plan-series-22.json").read_text() == "mine"


def test_a_plan_that_is_not_valid_is_not_written(site, monkeypatch):
    monkeypatch.setattr(review, "validate", lambda plan: ["something is wrong"])
    assert do_plan(i1="y") == 1
    assert do_named() == 1
    assert not list(Path().glob("plan*"))


def test_bad_files_are_exit_one(site, capsys):
    good = answered(i1="y")
    Path("bad.json").write_text("{}")
    for files in (("bad.json", good[1]), (good[0], "bad.json"), ("nowhere", good[1]),
                  (good[0], "nowhere")):
        assert run_cli("series", "plan", *files, "--series", 22) == 1
        assert last_line(capsys).startswith("error: ")
    assert run_cli("series", "plan", *answered(account=2, i1="y"), "--series", 22) == 1
    assert site.sent == [] and not list(Path().glob("plan*"))


def test_plan_and_named_need_their_arguments(site):
    files = answered(i1="y")
    assert run_cli("series", "plan", *files) == 2
    for argv in (("--name", "x", "--said", "y"), ("--series", 22, "--said", "y"),
                 ("--series", 22, "--name", "x")):
        assert run_cli("series", "named", *argv) == 2
    assert site.sent == []


@pytest.mark.parametrize("change", [{"visited": None}, {"items2": None}])
def test_a_series_that_cannot_be_read_makes_no_plan(site, change):
    site.series[22] |= change
    assert do_plan(i1="y") == 2
    assert do_named() == 2
    assert not list(Path().glob("plan*"))


def test_a_profile_that_cannot_be_read_makes_no_plan(site):
    site.regions = None
    assert do_plan(i1="y") == 2
    assert do_named() == 2
    assert not list(Path().glob("plan*"))


def test_answers_are_hashed_as_they_were_read(tmp_path, site, monkeypatch):
    m = listed()
    _, path = answered(m, i1="y")
    data = path.read_bytes()
    got, sha = review.read_answers(path, m)
    assert got == {(22, 1): "y"} == load_answers(path, m)
    assert sha == hashlib.sha256(data).hexdigest()


# -- named


def do_named(name="Tin Bridge", said="I walked over it in the rain", *extra, series=22):
    return run_cli("series", "named", "--series", series, "--name", name, "--said", said, *extra)


def test_a_named_item_is_one_op_with_the_owners_words(site, capsys):
    said = "  I walked over it,\nin the rain  "
    assert do_named(said=said) == 0
    plan = Plan.load("plan-series-22.json")
    (op,) = plan.ops
    assert (op.kind, op.series, op.item, op.bucket, op.confidence, op.label) == (
        "tick_series", 22, 2, "apply", 0.0, "Tin Bridge")
    assert op.proof == [{"kind": "memory", "detail": said}]
    assert plan.account == 1 and validate(plan) == []
    c = site.client()
    assert plan.basis["fingerprint"] == fingerprint(basis_of(c.snapshot([]), []))
    assert plan.basis["series_ticked"] == {"22": []} and plan.basis["taken_at"]
    out = capsys.readouterr().out
    assert "one series; read it with `show -v`, then `check`" in out
    assert not any(n in out for n in ITEMS.values() if n != "Tin Bridge")
    assert site.read(22) == 1 and site.asked == [{}]


def test_a_near_name_is_refused_and_no_name_is_printed(site, capsys):
    """AGENTS rule 7: the tool never proposes, and the nearest name is a proposal."""
    said = set()
    for name in ("tin bridge", "Tin Bridge ", " Tin Bridge", "Tin  Bridge", "Tin", "Tin Bridg",
                 "Tin Bridge\n", "TIN BRIDGE", "Tin\u00a0Bridge", "Gla", "2", "Nothing Like It"):
        assert do_named(name) == 1, name
        both = capsys.readouterr()
        said.add(both.out + both.err)
    (text,) = said  # nothing in it says how near
    assert text.startswith("error: ") and not any(n in text for n in ITEMS.values())
    assert not list(Path().glob("plan*"))


def test_two_items_of_one_name_are_refused(site, capsys):
    site.series[22] = Site.reply("x", ITEMS | {9: "Tin Bridge"})
    assert do_named() == 1
    both = capsys.readouterr()
    assert not any(n in both.out + both.err for n in ITEMS.values())
    assert not list(Path().glob("plan*"))


def test_a_named_item_already_ticked_makes_no_plan(site, capsys):
    site.series[22]["visited"] = ["2"]
    assert do_named() == 0
    assert "already ticked" in capsys.readouterr().out and not list(Path().glob("plan*"))


@pytest.mark.parametrize("said", ["", " ", "\n\t\u00a0", "\u202e\u2066", "\x1b\x07 \x85",
                                  "\u200b\ufeff"])
def test_the_owners_words_cannot_be_empty(site, said):
    assert do_named(said=said) == 1
    assert site.sent == [] and not list(Path().glob("plan*"))


@pytest.mark.parametrize("kw", [{"name": ""}, {"name": " "}, {"said": "a\udc80b"}])
def test_words_that_cannot_be_kept_are_refused(site, kw):
    site.series[22] = Site.reply("x", ITEMS | {8: "", 9: " "})
    assert do_named(**kw) == 1
    assert site.sent == [] and not list(Path().glob("plan*"))


@pytest.mark.parametrize("series", [0, -2, -1])
def test_named_refuses_a_series_that_is_none(site, series):
    assert do_named(series=series) == 1
    assert site.sent == []


def test_named_takes_a_series_that_review_skips(site):
    site.series[12] = Site.reply("x", {7: "Tin Bridge"})
    assert do_named(series=12) == 0
    assert [op.item for op in Plan.load("plan-series-12.json").ops] == [7]


def test_named_never_replaces_a_plan(site):
    Path("plan-series-22.json").write_text("mine")
    assert do_named() == 2
    assert Path("plan-series-22.json").read_text() == "mine" and site.sent == []
    assert do_named("Tin Bridge", "x", "--plan", "two.json") == 0


def test_the_help_says_whose_words_they_are(capsys):
    assert run_cli("series", "named", "--help") == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "own words" in out and "never proposes" in out


def test_what_the_commands_write_is_never_committed():
    root = Path(__file__).parents[1]
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"review/", "review-*.json", "review-*.html", "series-answers*.json",
            "plan*.json"} <= set(lines)


# ------------------------------------------------------------------ airports


def test_airports_are_unflagged_first_then_thin_then_distance():
    shots, objects = [], {}
    for n, places in {
        1: [(0.5, 1), (0.6, 1)],  # plain
        2: [(0.1, 1)],  # thin: one photo
        3: [(0.05, 1), (0.05, 2)],  # one spot
        4: [(1.5, 1), (1.5, 2)],  # one spot and thin
        5: [(1.2, 1), (1.3, 1)],  # thin: far
        6: [(0.4, 1), (0.45, 1)],  # plain, nearer
    }.items():
        for series in (5, 22):
            objects |= obj(float(n), 20.0, item=n, series=series)
        shots += [shot(n + km * KM, 20.0, day=day) for km, day in places]
    got = offered(shots, objects)
    assert [c.item for c in got if c.series == 5] == [6, 1, 2, 5, 3, 4]
    assert [c.item for c in got if c.series == 22] == [2, 6, 1, 5, 3, 4]
    assert [c.series for c in got] == [5] * 6 + [22] * 6
    flagged = [c.conflict or c.one_spot for c in got if c.series == 5]
    assert flagged == sorted(flagged)


def test_a_layover_offers_an_airport_and_nothing_beside_it():
    objects = obj(10.0, 20.0, series=5) | obj(10.0, 20.0, series=22) | obj(10.0, 20.0, series=1)
    (c,) = offered([shot(10.0, 20.0, use="airports")], objects)
    assert c.series == 5


def test_a_fast_photo_offers_no_airport_and_no_nearest_one():
    """A photo from a window is over an airport, and over many other things."""
    objects = obj(10.0, 20.0, series=5) | obj(10.0 + 5 * KM, 20.0, item=2, series=5)
    fast = [shot(10.0, 20.0, kind="fast"), shot(10.0, 20.0, kind="fast", day=2)]
    assert offered(fast, objects) == []
    (c,) = offered([*fast, shot(10.0 + 5.5 * KM, 20.0)], objects)
    assert (c.item, c.photos, c.fast_near) == (2, 1, 0)
    (c,) = offered([*fast, shot(10.0 + 1.0 * KM, 20.0)], obj(10.0, 20.0, series=5))
    assert (c.photos, c.days, c.fast_near, c.thin) == (1, 1, 2, True)


AIRPORTS = {1: "Alder Field", 2: "Birch Field", 3: "Cedar Field", 4: "Dogwood Field"}


def test_recall_names_the_ticked_airports_no_photo_is_near_and_no_other_series():
    objects = obj(10.0, 20.0, item=1, series=5) | obj(50.0, 20.0, item=2, series=5)
    cat = {5: SeriesInfo("Airports", AIRPORTS), 22: SeriesInfo("x", {7: "Glass Hall"})}
    got = recall([shot(10.0, 20.0)], objects, {5: {1, 2, 3, 99}, 22: {7}}, catalogue=cat)
    assert got[5]["no_photo"] == [2, 3, 99]
    assert got[5]["no_photo_names"] == {2: "Birch Field", 3: "Cedar Field"}
    assert "no_photo_names" not in got[22] and got[22]["no_photo"] == [7]


def airports_row(**kw):
    return {"ticked": 4, "located": 2, "found": {0.2: 0, 2.0: 1}, "no_photo": [2, 3, 99],
            "no_photo_names": {2: "Birch Field", 3: "Cedar Field"}, "name": "Airports"} | kw


def airports_manifest(candidates=(), **kw):
    return manifest(list(candidates), coverage=COVERAGE | {
        "recall": {1: RECALL[22], 5: airports_row(**kw), 22: RECALL[22]}})


def test_names_of_airports_are_in_the_list_and_in_its_hash(tmp_path):
    m = airports_manifest()
    row = m.coverage["recall"]["5"]
    assert row["no_photo_names"] == {"2": "Birch Field", "3": "Cedar Field"}
    assert Manifest.load(m.save(tmp_path / "m.json")) == m
    assert airports_manifest(no_photo_names={2: "Birch Field", 3: "C"}).sha256 != m.sha256


@pytest.mark.parametrize("names", [{4: "Dogwood Field"}, {2: 5}, {2: None}, ["Birch Field"],
                                   {"x": "y"}, {"02": "Birch Field"}, {2: "a", "2": "b"}, "x"])
def test_only_names_of_what_is_ticked_and_has_no_photo_are_taken(names):
    """Any other name would be one of an unticked thing without a photo."""
    with pytest.raises(PlanInvalid, match="no_photo_names"):
        airports_manifest(no_photo_names=names)


def test_names_are_of_airports_only():
    with pytest.raises(PlanInvalid, match="no_photo_names"):
        manifest(coverage=COVERAGE | {"recall": {22: airports_row()}})


@pytest.fixture
def flights(world):
    """Airports, one of them ticked with a photo, two ticked without; and two series more."""
    world.reader.points += [
        SeriesPoint(1, 5, "", 10.0, 20.0), SeriesPoint(2, 5, "", 10.2, 20.2),
        SeriesPoint(4, 5, "", 10.0 + 0.1 * KM, 20.0), SeriesPoint(5, 5, "", 10.3, 20.3),
        SeriesPoint(1, 1, "", 10.0, 20.0)]
    world.series[5] = Site.reply("Airports", AIRPORTS | {5: "Elm Field"}, [1, 2, 3])
    world.series[1] = Site.reply("Series 1", {1: "Zinc Pier"}, [1])
    return world


def test_review_prints_airports_first_and_what_it_would_miss(flights, capsys):
    flights.series[5]["items2"][1]["name"] = "Birch\u202e \x1bField"
    assert do_review() == 0
    out = capsys.readouterr().out
    rows = [line.split()[0] for line in out.splitlines() if re.match(r"  \d+ \S", line)]
    assert rows == ["5", "1", "22"]
    assert "2 ticked airports with no photo near" in out and "named on the page" in out
    for name in AIRPORTS.values():
        assert name not in out
    assert "\u202e" not in out and "\x1b" not in out
    _, m = written_by()
    assert set(m.coverage["recall"]["5"]["no_photo_names"]) == {"2", "3"}
    flights.series[5]["visited"] = [1, 2]
    capsys.readouterr()
    assert do_review(out="again") == 0
    out = capsys.readouterr().out
    assert "1 ticked airport with no photo near" in out and "it is named on the page" in out
    assert [(c.series, c.item, c.thin) for c in m.candidates if c.series == 5] == [(5, 4, True)]
    assert "Elm Field" not in json.dumps(m._stored())


def test_review_says_nothing_of_airports_it_did_not_read(flights, capsys):
    assert do_review("--series", "22") == 0
    assert "airport" not in capsys.readouterr().out.replace("used for airports only", "")
    flights.series[5]["visited"] = [1]
    assert do_review(out="again") == 0
    assert "ticked airports" not in capsys.readouterr().out


@needs_node
def test_the_page_puts_airports_first_in_recall_and_names_what_is_missed(tmp_path):
    bad = "Birch\u202e Field"
    m = airports_manifest(no_photo_names={2: bad, 3: "\u2066"})
    got = run(tmp_path, m, """
      const p = open();
      return [p.byId.recall.children.map((r) => r.children[0].textContent),
              p.byId["airports-note"].textContent, p.byId["airports-note"].hidden,
              p.byId["airports-missed"].children.map((e) => e.textContent)];
    """)
    assert got[0] == ["Series", "Airports (5)", "Series 1", "Series 22"]
    assert "3 airports you have ticked" in got[1] and "not complete" in got[1] and not got[2]
    assert got[3] == ["Birch Field", "Airport 3, with no name in the catalogue",
                      "Airport 99, with no name in the catalogue"]


@needs_node
@pytest.mark.parametrize("recall_of", [
    {22: RECALL[22]}, {}, {5: airports_row(no_photo=[], no_photo_names={})}])
def test_the_page_says_nothing_of_airports_where_none_is_missed(tmp_path, recall_of):
    got = run(tmp_path, manifest(coverage=COVERAGE | {"recall": recall_of}), """
      const p = open();
      return [p.byId["airports-note"].textContent, p.byId["airports-note"].hidden,
              p.byId["airports-missed"].children.length];
    """)
    assert got == ["", True, 0]


THIN_AIRPORTS = "Airports: one photo, or over 1 km away"
MARKED_AIRPORTS = "Airports: with a conflict or one spot"
BOTH_AIRPORTS = "Airports: with a conflict or one spot, and one photo or over 1 km away"


def mixed():
    day = dt.datetime(2026, 5, 2, 9)
    def a(item, **kw):
        return candidate(item, series=5, **kw)
    return [a(1, km=0.9), a(2, km=0.1, thin=True), a(3, km=0.05, conflict=True),
            a(4, km=0.01, one_spot=True, thin=True), a(5, km=1.5, thin=True), a(6, km=0.2),
            a(7, km=0.3, thin=True, at=day), a(8, km=0.02, one_spot=True),
            candidate(9, thin=True, km=0.1), candidate(10, km=0.2),
            candidate(11, conflict=True, km=0.1), candidate(12, series=1, thin=True)]


@needs_node
def test_thin_airports_are_under_a_heading_of_their_own_before_the_flagged(tmp_path):
    got = run(tmp_path, manifest(mixed()), """
      const p = open();
      p.openAll();
      const names = (d) => d.cls("name").map((n) => Number(n.textContent.split(" ")[1]));
      return p.byId.days.children.map((e) => e.tag === "h2" ? e.textContent : names(e));
    """)
    assert got == [[12, 6, 1, 9, 10, 11], THIN_AIRPORTS, [2, 5], [7],
                   MARKED_AIRPORTS, [8, 3], BOTH_AIRPORTS, [4]]


@needs_node
def test_a_day_puts_no_thin_flagged_airport_before_one_that_is_not_thin(tmp_path):
    """Inside one heading the days came first, and a thin one of an earlier day with them."""
    first, last = dt.datetime(2026, 5, 1, 9), dt.datetime(2026, 5, 2, 9)
    m = manifest([candidate(1, series=5, km=0.01, conflict=True, thin=True, at=first),
                  candidate(2, series=5, km=0.9, one_spot=True, at=last),
                  candidate(3, series=5, km=0.8, conflict=True, at=first),
                  candidate(4, series=5, km=0.02, one_spot=True, thin=True, at=last)])
    got = run(tmp_path, m, """
      const p = open();
      const ids = (s) => s.days.flatMap((d) => d.items.map((c) => c.item));
      return [logic.sections(data.candidates).map((s) => [s.title, ids(s)]),
              p.days().length];
    """)
    assert got == [[[MARKED_AIRPORTS, [3, 2]], [BOTH_AIRPORTS, [1, 4]]], 4]


@needs_node
def test_the_page_and_the_list_have_one_order(tmp_path):
    """Series by series, and on one day, they are the same."""
    keyed = [dataclasses.replace(c, at=dt.datetime(2026, 5, 1, 9)) for c in mixed()]
    want = [c.item for c in sorted(keyed, key=review._rank) if c.series == 5]
    assert want == [6, 1, 2, 7, 5, 8, 3, 4]
    got = run(tmp_path, manifest(keyed), """
      return logic.sections(data.candidates).flatMap((s) => s.days.flatMap((d) => d.items))
        .filter((c) => c.series === 5).map((c) => c.item);
    """)
    assert got == want


@needs_node
def test_a_heading_with_nothing_under_it_is_not_there_and_days_stay_open(tmp_path):
    got = run(tmp_path, manifest(mixed()), """
      const p = open();
      p.openAll();
      const heads = () => p.byId.days.children.filter((e) => e.tag === "h2")
        .map((e) => e.textContent);
      const pick = (series) => {
        p.byId["by-series"].value = series;
        p.tell("pageshow", {persisted: false});
        return heads();
      };
      const opened = () => p.days().every((d) => d.open);
      return [pick("22"), pick("5"), opened(), pick("1"), pick("")];
    """)
    heads = [THIN_AIRPORTS, MARKED_AIRPORTS, BOTH_AIRPORTS]
    assert got == [[], heads, True, [], heads]


@needs_node
def test_a_day_opened_under_one_heading_is_not_open_under_another(tmp_path):
    m = manifest([candidate(n, series=22) for n in range(1, 200)]
                 + [candidate(n, series=5, thin=True) for n in range(1, 200)])
    got = run(tmp_path, m, """
      const p = open();
      const opened = () => p.days().map((d) => d.open);
      const first = opened();
      p.days()[0].toggle(true);
      p.byId["by-series"].value = "5";
      p.tell("pageshow", {persisted: false});
      const thin = opened();
      p.byId["by-series"].value = "";
      p.tell("pageshow", {persisted: false});
      return [first, thin, opened()];
    """)
    assert got == [[False, False], [False], [True, False]]


def test_the_page_knows_which_series_is_airports():
    page = (Path(review.__file__).parent / "review_page.html").read_text(encoding="utf-8")
    assert f"const AIRPORTS = {next(iter(AIRPORT_SERIES))};" in page and len(AIRPORT_SERIES) == 1


def test_match_series_says_what_its_recall_was():
    text = " ".join(match_series.__doc__.split())
    for words in ("25%", "day-level", "photo", "recall", "not precision"):
        assert words in text, words
