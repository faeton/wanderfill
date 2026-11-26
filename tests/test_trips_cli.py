"""Tests for `trips review` and `trips plan`, from photos to the plan and to what it does.

Everything here is made up: a country of rectangles on the map, a library of photos in
them, and a server that remembers what it is sent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import inspect
import json
import os
import time
from pathlib import Path
from typing import ClassVar

import pytest

from wanderfill.api.client import NomadMania
from wanderfill.api.errors import AccountMismatch, DriftError, PlanInvalid, WanderfillError
from wanderfill.cli import main as cli
from wanderfill.geo.tiles import deg2tile, tile_bounds
from wanderfill.plan import trips
from wanderfill.plan.apply import apply_plan
from wanderfill.plan.model import Plan, basis_of, fingerprint, validate
from wanderfill.plan.review import default_speeds
from wanderfill.plan.trip_list import TripList, load_answers, render_page
from wanderfill.plan.trip_plan import plan_for_trips, plan_trips
from wanderfill.plan.trips import kind_of, observe
from wanderfill.sources.photos_app import PhotoAsset

pytest.importorskip("shapely")

UTC = dt.timezone.utc
CAMERA = "com.apple.camera"
HOME, FAR = 80, 90
NAMES = {1: "Aldmoor", 2: "Berrow", 3: "Cray Islands", 4: "Dunvale", 5: "Elstow",
         6: "Fenmark", HOME: "Grayport", FAR: "Hollin"}
QUALITY_OF_ALL = {1: "3", 2: "3", 3: "2", 4: "3", 5: "4"}


def day(n, month=5):
    """Day `n` of May 2031, which is before it or after it too."""
    return dt.date(2031, month, 1) + dt.timedelta(days=n - 1)


def centre(region):
    """Regions stand on each other from the south, half a degree each; two are far off."""
    return {HOME: (25.25, 10.5), FAR: (10.25, 150.5)}.get(region, (region * 0.5 + 0.25, 10.5))


def square(region):
    from shapely.geometry import box

    lat, lon = centre(region)
    return box(lon - 0.5, lat - 0.25, lon + 0.5, lat + 0.25)


class Map:
    """A tile has the part of each region that is in it. One in `broken` never arrives."""

    def __init__(self):
        self.failed, self.broken, self.asked = set(), set(), []
        self.regions, self.cut = {r: square(r) for r in NAMES}, {}

    def shapes(self, layer, z, x, y):
        from shapely.geometry import box

        assert (layer, z) == ("regions", 10)
        self.asked.append((layer, z, x, y))
        if (x, y) in self.broken:
            self.failed.add((layer, z, x, y))
            return []
        if not (0 <= x < 1024 and 0 <= y < 1024):
            return []
        if (x, y) not in self.cut:
            tile = box(*tile_bounds(x, y, z))
            self.cut[x, y] = [({"id": r}, shape.intersection(tile))
                              for r, shape in self.regions.items() if shape.intersects(tile)]
        return self.cut[x, y]


def cam(n, region, hour=12.0, *, zone=1.0, importer=CAMERA, off=0.0, recorded=True, uuid=None):
    """One photo in the middle of a region, `off` degrees north of it."""
    lat, lon = centre(region)
    taken = dt.datetime.combine(day(n), dt.time()) + dt.timedelta(hours=hour)
    utc = (taken - dt.timedelta(hours=zone)).replace(tzinfo=UTC)
    return PhotoAsset(uuid or f"{n}-{region}-{hour}-{off}", taken, lat + off, lon, "0", "x.heic",
                      importer=importer, taken_utc=utc,
                      zone=round(zone * 3600) if recorded else None)


def days(region, first, last, **kw):
    """Two photos on every day, nine hours and over a kilometre apart."""
    return [p for n in range(first, last + 1)
            for p in (cam(n, region, 9, **kw), cam(n, region, 18, off=0.02 + n / 1e4, **kw))]


# Home; five regions in two weeks, one silent day (5), a same-day transfer (3), and a flight
# home that is so slow between the photos that nothing says it was one.
JOURNEY = [
    *days(HOME, -9, -4),
    *days(1, 1, 3), cam(3, 2, 22), *days(2, 4, 4), *days(2, 6, 6), *days(3, 7, 9),
    *days(4, 10, 11), *days(5, 12, 14), *days(HOME, 16, 17),
]
JOURNEY[-4] = cam(16, HOME, 9)


class Library:
    """A Photos library that remembers how it was read."""

    photos: ClassVar[list] = []
    unplaced: ClassVar[list] = []
    column = True
    opened: ClassVar[list] = []

    def __init__(self, library=None, since=None, until=None, origins=None):
        self.importer_column = None
        self.records_origin = None
        Library.opened.append((library, since, until, origins))

    def assets(self):
        self.importer_column = Library.column
        self.records_origin = Library.column
        yield from Library.photos

    def without_location(self):
        yield from Library.unplaced


class Site:
    """The server: what is on the profile, and what a trip does to it."""

    def __init__(self):
        self.account, self.sent, self.dare = 1, [], [9]
        self.visits, self.trips = {}, {}
        self.token, self.lang = "sekrit-token", "en"
        self.catalogue = {str(r): {"id": r, "name": n} for r, n in NAMES.items()}
        self.writable = False
        self.next = 5000
        self.settings = {}  # no homebase, unless a test says so

    def visit(self, region, first, last, quality=3, trip_id=None):
        self.next += 1
        self.visits.setdefault(region, []).append(
            {"id": self.next, "quality": quality, "trip_id": trip_id, "hidden": 0,
             **self.ends(first, "from"), **self.ends(last, "to")})
        return self.next

    @staticmethod
    def ends(when, side):
        year, month, d = ((when.year, when.month, when.day) if isinstance(when, dt.date) else
                          (when, None, None))
        return {f"year_{side}": year, f"month_{side}": month, f"day_{side}": d}

    def trip(self, id, first, last, description="", year=None):
        self.trips[id] = {
            "id": id, "date_from": first and first.isoformat(),
            "date_to": last and last.isoformat(), "year": year or (first and first.year),
            "description": description, "dates_missing": first is None, "regions": []}

    def webapi(self, action, **fields):
        self.sent.append((action, fields))
        if action == "user/status-quick":
            return {"uid": self.account}
        if action == "user/get-settings":
            return dict(self.settings)
        if action == "regions/get-regions-list-2":
            return {"data": self.catalogue}
        if action == "maps/get-visited-regions-ids-simple":
            return {"ids": sorted(r for r, vs in self.visits.items()
                                  if any(v.get("quality") in (2, 3, 4, 5, 6) for v in vs))}
        if action == "maps/get-visited-dare-ids-simple":
            return {"ids": self.dare}
        if action == "quickEnter/get-visits-to-region":
            return {"data": [dict(v) for v in self.visits.get(int(fields["region"]), [])]}
        if action == "trips/get-trips-for-year-app":
            return {"trips": [dict(t) for t in self.trips.values()
                              if t["year"] == int(fields["year"])]}
        if action == "trips/get-trip":
            return {"trip": self.trips[int(fields["trip_id"])]}
        assert action == "trips/new-trip" and self.writable, f"a write was sent: {action}"
        self.next += 1
        self.trip(new := self.next, dt.date.fromisoformat(fields["date_from"]),
                  dt.date.fromisoformat(fields["date_to"]), fields["description"])
        for r in json.loads(fields["regions_json"]):
            ends = [dt.date(*(r[f"{p}_{side}"] for p in ("year", "month", "day")))
                    for side in ("from", "to")]
            self.visit(r["id"], *ends, r["quality"], new)
        return {"result": "OK", "trip_id": new}

    def ajax_json(self, path, **fields):
        raise AssertionError(f"not read here: {path}")

    ajax_text = ajax_json

    def client(self):
        return NomadMania("fake", transport=self)

    def read(self, action):
        return [f for a, f in self.sent if a == action]

    def writes(self):
        return [a for a, _ in self.sent if a == "trips/new-trip"]


@pytest.fixture
def site(monkeypatch, tmp_path):
    from wanderfill.geo import tiles
    from wanderfill.sources import photos_app

    s = Site()
    s.asked, s.map = [], Map()

    def client_for(args, **kw):
        s.asked.append(kw)
        return s.client()

    monkeypatch.setattr(cli, "_client", client_for)
    monkeypatch.setattr(cli, "DEFAULT_WORKDIR", tmp_path / "home")
    monkeypatch.setattr(photos_app, "PhotosAppSource", Library)
    monkeypatch.setattr(tiles, "TileReader", lambda *a, **kw: s.map)
    monkeypatch.setattr(Library, "opened", [])
    monkeypatch.setattr(Library, "column", True)
    monkeypatch.setattr(Library, "photos", list(JOURNEY))
    monkeypatch.setattr(Library, "unplaced", [])
    (tmp_path / "cwd").mkdir()
    (tmp_path / "lib").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    return s


def run_cli(*argv):
    try:
        return cli.main([str(a) for a in argv])
    except SystemExit as exc:
        return exc.code


def do_review(*extra, out="out", track=("--no-track",)):
    return run_cli("trips", "review", "--since", "2031-05-01", "--library", "../lib", "--out", out,
                   *track, *extra)


def made(out="out"):
    """The list that review wrote last."""
    path = max(Path(out).glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    return TripList.load(path), path


def listed(*extra, out="out", **kw):
    assert do_review(*extra, out=out, **kw) == 0
    return made(out)


def answer(m, quality=None, words=None, cuts=None, path="trips-answers.json"):
    """An answers file: `quality` and `words` by region, `cuts` by the two regions."""
    quality = QUALITY_OF_ALL if quality is None else quality
    rows, of = {}, {r.id: r.region for r in m.runs}
    for r in m.runs:
        row = {}
        if r.region in quality and r.status != "there":
            row["a"] = quality[r.region]
        if words and r.region in words:
            row["words"] = words[r.region]
        if row:
            rows[r.id] = row
    cuts = {b.id: cuts[of[b.before], of[b.after]] for b in m.boundaries
            if cuts and (of[b.before], of[b.after]) in cuts}
    Path(path).write_text(json.dumps({"format": 1, "account": m.account, "manifest": m.sha256,
                                      "runs": rows, "cuts": cuts}), encoding="utf-8")
    return path


def do_plan(quality=None, *extra, m=None, words=None, cuts=None):
    m, path = m or made()
    return run_cli("trips", "plan", path, answer(m, quality, words, cuts), *extra)


def by_region(m):
    return {r.region: r for r in m.runs}


def status(m):
    return {r.region: (r.status, sorted({kind_of(x) for x in r.reasons})) for r in m.runs}


def rows(plan):
    return [[(r["id"], r["quality"]) for r in op.regions] for op in plan.ops]


def photos(*given):
    Library.photos = list(given)


# -- Review Focus ------------------------------------------------------------------------


def test_a_journey_from_its_photos_to_a_trip_that_reads_back(site, capsys, tmp_path):
    """Two weeks, five regions, a silent day, a transfer, and a flight home to an open stay."""
    assert do_review() == 0
    m, path = made()
    page = path.with_suffix(".html")
    assert path.stem == f"trips-{m.sha256[:12]}" and page.read_text("utf-8") == render_page(m)
    out = capsys.readouterr().out
    assert str(path) in out and str(page) in out and "trips plan" in out
    assert [(r.region, r.first, r.last) for r in m.runs] == [
        (1, day(1), day(3)), (2, day(3), day(6)), (3, day(7), day(9)), (4, day(10), day(11)),
        (5, day(12), day(14)), (HOME, day(16), day(17))]
    assert status(m) == {**{r: ("offered", []) for r in range(1, 6)}, HOME: ("held", ["open"])}
    assert by_region(m)[2].silent == (day(5),)
    assert [b.suggestions for b in m.boundaries] == [()] * 5  # the flight looks slow

    assert do_plan() == 0
    out = capsys.readouterr().out
    assert "1 trip, 5 region visits" in out and "Nothing was sent" in out
    assert "5 regions will be marked" in out
    assert "Grayport" in out and "is not answered" in out  # the open stay, and why not
    plan = Plan.load("plan-trips.json")
    assert validate(plan) == [] and plan.account == 1
    (op,) = plan.ops
    assert (op.kind, op.bucket, op.confidence) == ("create_trip", "apply", 0.0)
    assert (op.date_from, op.date_to) == (str(day(1)), str(day(14)))
    assert op.regions[1] == {"id": 2, "quality": 3, "year_from": 2031, "month_from": 5,
                             "day_from": 3, "year_to": 2031, "month_to": 5, "day_to": 6}
    assert rows(plan) == [[(1, 3), (2, 3), (3, 2), (4, 3), (5, 4)]]
    assert site.writes() == [] and site.asked == [{}, {}]  # and no cached client

    work = tmp_path / "w"
    assert run_cli("show", "-v", "plan-trips.json") == 0
    assert "proof ground_photo: 2 Berrow: 2031-05-03 to 2031-05-06" in capsys.readouterr().out
    assert run_cli("check", "plan-trips.json", "--workdir", work) == 0
    out = capsys.readouterr().out
    assert "Left to send: 1 of 1 op(s), in 5 region(s)" in out and "unchanged since" in out
    site.writable = True
    assert run_cli("apply", "plan-trips.json", "--confirm", "--workdir", work) == 0
    assert site.writes() == ["trips/new-trip"]
    (seen,) = [json.loads(p.read_text()) for p in work.glob("verify-*.json")]
    assert (seen["mismatches"], seen["unproven"], seen["not_counted"]) == ([], [], [])
    assert seen["checked"] == 1
    # Now the profile has it, and a new list says so.
    again, _ = listed()
    assert {r.status for r in again.runs if r.region != HOME} == {"there"}


def test_a_region_visited_once_and_undated_is_held_until_words_and_planned_once(
        site, tmp_path, capsys):
    site.visit(3, None, None)
    m, _ = listed()
    assert status(m)[3] == ("held", ["undated"])
    assert do_plan() == 0
    assert "No trip" in (out := capsys.readouterr().out) and "needs words of yours" in out
    assert not Path("plan-trips.json").exists()
    words = "the other time was a school camp, years before"
    assert do_plan(words={3: words}) == 0
    plan = Plan.load("plan-trips.json")
    assert rows(plan) == [[(1, 3), (2, 3), (3, 2), (4, 3), (5, 4)]]
    assert [p["kind"] for p in plan.ops[0].proof] == ["ground_photo"] * 3 + ["memory"] + [
        "ground_photo"] * 2
    assert plan.ops[0].proof[3] == {"kind": "memory", "detail": words}
    site.writable = True
    assert run_cli("apply", "plan-trips.json", "--confirm", "--workdir", tmp_path / "w") == 0
    assert len(site.visits[3]) == 2
    assert status(listed()[0])[3] == ("there", ["there"])


def test_a_journey_half_written_already_is_cut_around_what_is_there(site, capsys):
    for region, first, last in ((2, 3, 6), (3, 7, 9)):
        site.visit(region, day(first), day(last), trip_id=700)
    m, _ = listed()
    assert {r for r, (s, _) in status(m).items() if s == "there"} == {2, 3}
    assert do_plan() == 0
    assert rows(Plan.load("plan-trips.json")) == [[(1, 3)], [(4, 3), (5, 4)]]
    assert "3 region visits" in capsys.readouterr().out


def test_photos_after_a_long_hole_are_a_continuation_and_never_a_second_visit(site, capsys):
    site.visit(3, day(-40), day(-30))
    photos(*days(HOME, -60, -59), *days(3, -40, -30), *days(3, 5, 9), *days(1, 10, 11),
           *days(HOME, 13, 14))
    m, _ = listed()
    assert status(m)[3] == ("fixed", ["continuation"])
    files = made()[1], answer(m, {3: "3", 1: "3"})
    assert run_cli("trips", "plan", *files) == 1
    assert do_plan({3: "no", 1: "3"}) == 0
    assert rows(Plan.load("plan-trips.json")) == [[(1, 3)]]
    assert "continuation" not in Plan.load("plan-trips.json").to_json()
    capsys.readouterr()


def test_a_day_border_photo_by_another_continents_clock_proves_no_date(site):
    """Zone 0 at longitude 150: it is the next day there."""
    trip = lambda zone: [*days(1, 1, 3), cam(8, FAR, 23, zone=zone),  # noqa: E731
                         cam(8, FAR, 23.5, zone=zone, off=0.005), *days(5, 14, 15),
                         *days(HOME, 18, 19)]
    photos(*trip(0))
    m, _ = listed()
    assert FAR not in status(m) and m.left_out["clock"] == 2
    photos(*trip(10))
    m, _ = listed()
    assert (by_region(m)[FAR].first, m.left_out["clock"]) == (day(8), 0)


# -- flight days --------------------------------------------------------------------------


def flown(n, region, hour, **kw):
    """Two photos in a region twelve minutes apart, at the speed of an aircraft."""
    return [cam(n, region, hour, off=-0.2, **kw), cam(n, region, hour + 0.2, off=0.2, **kw)]


# 1 to 4 May in Aldmoor, at the airport on the 5th, and in Berrow the same evening.
AIRPORT = [*days(HOME, -9, -4), *days(1, 1, 4), *flown(5, 1, 8), cam(5, 2, 21),
           *days(2, 6, 7), *days(HOME, 10, 11)]


def test_photos_at_the_airport_on_the_last_day_are_a_flight_day(site, capsys):
    photos(*AIRPORT)
    m, _ = listed()
    r = by_region(m)[1]
    assert (r.first, r.last, r.days[-1], r.flight) == (day(1), day(5), day(4), (day(5),))
    assert status(m)[1] == ("offered", []) and r.flight_photos == ((2, 0),)
    assert m.params["flight_days"] is True
    assert "1 run with a flight day at an end" in capsys.readouterr().out
    assert do_plan({1: "3", 2: "3"}, "--plan", "blank.json") == 0  # not answered: one trip
    assert Plan.load("blank.json").ops[0].regions[0]["day_to"] == 5
    assert do_plan({1: "3", 2: "3"}, cuts={(1, 2): "same"}) == 0
    plan = Plan.load("plan-trips.json")
    (op,) = plan.ops
    assert (op.date_from, op.date_to) == (str(day(1)), str(day(7)))
    assert (op.regions[0]["day_from"], op.regions[0]["day_to"]) == (1, 5)
    assert "flight day 2031-05-05: 2 photos taken at speed in the region" in op.proof[0]["detail"]
    assert plan.params["flight_days"] is True and validate(plan) == []


def test_a_flight_day_left_out_is_not_in_the_plan(site):
    photos(*AIRPORT)
    m, path = listed()
    r, q = by_region(m)[1], by_region(m)[2]
    raw = {"format": 1, "account": 1, "manifest": m.sha256, "cuts": {}, "runs": {
        r.id: {"a": "3", "out": [str(day(5))]}, q.id: {"a": "no"}}}
    raw["cuts"] = {b.id: "cut" for b in m.boundaries if b.suggestions}
    Path("a.json").write_text(json.dumps(raw))
    assert run_cli("trips", "plan", path, "a.json") == 0
    (op,) = Plan.load("plan-trips.json").ops
    assert (op.date_from, op.date_to) == (str(day(1)), str(day(4)))
    assert op.regions[0]["day_to"] == 4
    assert "left out by the owner" in op.proof[0]["detail"]
    assert any("flight day left out by the owner: 2031-05-05" in e for e in op.evidence)


@pytest.mark.parametrize("first, last, want", [
    (5, 6, ("fixed", ["overlap"])), (1, 5, ("there", ["there"])),
    (1, 4, ("fixed", ["overlap"])),
])
def test_a_visit_on_the_flight_day_is_never_written_again(site, first, last, want):
    photos(*AIRPORT)
    site.visit(1, day(first), day(last))
    m, _ = listed()
    assert status(m)[1] == want
    quality = {2: "3"} | ({1: "no"} if want[0] == "fixed" else {})
    assert do_plan(quality) == 0
    assert rows(Plan.load("plan-trips.json")) == [[(2, 3)]]
    if want[0] == "fixed":
        assert do_plan({1: "3", 2: "3"}, "--plan", "b.json") == 1


def test_the_rule_can_be_turned_off_for_a_list(site, capsys):
    photos(*AIRPORT)
    on, _ = listed()
    off, _ = listed("--no-flight-days", out="off")
    assert off.params["flight_days"] is False and off.sha256 != on.sha256
    assert by_region(off)[1].last == day(4) and all(not r.flight for r in off.runs)
    assert "flight days are not used" in capsys.readouterr().out
    # The table too, and what is read.
    site.visit(1, day(1), day(5))
    on, off = listed(out="c")[0], listed("--no-flight-days", out="d")[0]
    assert [r.held for r in off.sweep] == [r.held + 1 for r in on.sweep]
    site.sent.clear()
    argv = ("trips", "review", "--since", day(5), "--library", "../lib", "--no-track", "--out")
    assert run_cli(*argv, "e", "--no-flight-days") == 0
    asked = [int(f["region"]) for f in site.read("quickEnter/get-visits-to-region")]
    assert 1 not in asked and 2 in asked
    assert run_cli(*argv, "f") == 0
    assert status(made("f")[0])[1] == ("there", ["there"])


# The phone still has Aldmoor when it is two thousand kilometres away.
LANDING = [*days(HOME, -9, -4), *days(1, 1, 4), cam(5, 1, 9, off=0.1), cam(5, HOME, 9.1),
           *days(HOME, 6, 7), *days(2, 10, 11), *days(HOME, 14, 15)]


def included(m, path, quality, *regions):
    """The answers file, with the days beside a jump of these regions included."""
    raw = json.loads(Path(answer(m, quality, cuts={
        (a.region, b.region): "cut" for a, b in zip(m.runs, m.runs[1:], strict=False)},
        path=path)).read_text())
    for r in m.runs:
        if r.region in regions:
            raw["runs"][r.id]["in"] = [str(d) for d in r.includable]
    Path(path).write_text(json.dumps(raw))
    return path


def test_a_day_with_an_impossible_jump_only_is_out_until_the_owner_includes_it(
        site, capsys, tmp_path):
    photos(*LANDING)
    m, path = listed()
    r = by_region(m)[1]
    assert (r.last, r.flight, r.jump, r.jump_photos, r.includable) == (
        day(4), (), (day(5),), (1,), (day(5),))
    out = capsys.readouterr().out
    assert "0 runs with a flight day at an end" in out
    assert ("2 runs with a day beside a jump, which is out unless you include it on the page"
            in " ".join(out.split()))
    assert run_cli("trips", "plan", path, included(m, "a.json", {1: "3"})) == 0
    (op,) = Plan.load("plan-trips.json").ops
    assert (op.date_from, op.date_to) == (str(day(1)), str(day(4)))
    assert ("day beside a jump 2031-05-05: 1 photo beside an impossible jump in the region, not "
            "included" in op.proof[0]["detail"])

    assert run_cli("trips", "plan", path, included(m, "b.json", {1: "3"}, 1),
                   "--plan", "b-plan.json") == 0
    plan = Plan.load("b-plan.json")
    (op,) = plan.ops
    assert (op.date_from, op.date_to) == (str(day(1)), str(day(5)))
    assert (op.regions[0]["day_from"], op.regions[0]["day_to"]) == (1, 5)
    assert "included by the owner" in op.proof[0]["detail"]
    assert "not included" not in op.proof[0]["detail"]
    assert any("day beside a jump included by the owner: 2031-05-05" in e for e in op.evidence)
    assert validate(plan) == []

    # A visit of just those days is a second copy to `check` and to `apply`.
    site.visit(1, day(1), day(5))
    snap = site.client().snapshot([1])
    again = dataclasses.replace(plan, basis={
        "fingerprint": fingerprint(basis_of(snap, [1])), "taken_at": "later"})
    with pytest.raises(DriftError, match="second copy"):
        apply_plan(site.client(), again, workdir=tmp_path / "x", confirm=True)
    site.visits.clear()
    site.writable = True
    report = apply_plan(site.client(), plan, workdir=tmp_path / "w", confirm=True)
    assert report.errors == [] and report.verified["mismatches"] == []
    assert report.verified["unproven"] == []
    (v,) = site.visits[1]
    assert (v["day_from"], v["day_to"]) == (1, 5)
    # And it comes out as there, with the days it was written with.
    m, _ = listed(out="again")
    r = by_region(m)[1]
    assert (r.status, r.last, r.includable) == ("there", day(4), ())
    assert r.reasons == (f"there: visit {v['id']}, 2031-05-01 to 2031-05-05",)


def test_the_rule_that_is_off_is_off_for_both(site, capsys):
    photos(*LANDING)
    m, _ = listed("--no-flight-days")
    assert all(r.jump == r.flight == () for r in m.runs)
    out = capsys.readouterr().out
    assert "flight days are not used" in out and "beside a jump, which" not in out


def test_silent_days_come_in_with_an_included_day_and_are_said(site):
    photos(*days(HOME, -9, -4), *days(1, 1, 3), cam(5, 1, 9, off=0.1), cam(5, HOME, 9.1),
           *days(HOME, 6, 7), *days(2, 10, 11))
    m, path = listed()
    assert (by_region(m)[1].jump, by_region(m)[1].silent) == ((day(5),), ())
    assert run_cli("trips", "plan", path, included(m, "a.json", {1: "3"}, 1)) == 0
    (op,) = Plan.load("plan-trips.json").ops
    assert op.date_to == str(day(5))
    assert "none on 2031-05-04" in op.proof[0]["detail"]


def test_a_visit_that_came_on_the_included_day_refuses_the_plan(site, capsys):
    photos(*LANDING)
    m, path = listed()
    files = path, included(m, "a.json", {1: "3"}, 1)
    site.visit(1, day(5), day(5))
    assert run_cli("trips", "plan", *files) == 2
    assert "Aldmoor (1)" in capsys.readouterr().err and nothing_written()
    m, _ = listed(out="b")
    assert by_region(m)[1].includable == ()


def test_a_day_of_the_answers_that_the_list_does_not_offer_is_refused(site):
    photos(*LANDING)
    m, path = listed()
    raw = json.loads(Path(included(m, "a.json", {1: "3"}, 1)).read_text())
    raw["runs"][by_region(m)[1].id]["in"] = [str(day(6))]
    Path("a.json").write_text(json.dumps(raw))
    assert run_cli("trips", "plan", path, "a.json") == 1 and nothing_written()


# -- review: what is read ----------------------------------------------------------------


def test_review_writes_two_private_files_and_sends_no_write(site, tmp_path):
    before = sorted(tmp_path.rglob("*"))
    m, path = listed()
    new = [p for p in sorted(tmp_path.rglob("*")) if p not in before]
    cwd = tmp_path / "cwd"
    assert new == [cwd / "out", cwd / path.with_suffix(".html"), cwd / path]
    assert Path("out").stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in Path("out").iterdir())
    assert all("get-" in a or a == "user/status-quick" for a, _ in site.sent)
    assert "sekrit" not in m.to_json()


def test_the_default_directory_is_under_the_ignored_one(site):
    assert run_cli("trips", "review", "--since", day(1), "--library", "../lib",
                   "--no-track") == 0
    (a, b) = sorted(Path("review/trips").iterdir())
    assert (a.suffix, b.suffix) == (".html", ".json") and a.stem == b.stem
    lines = (Path(__file__).parents[1] / ".gitignore").read_text("utf-8").splitlines()
    assert {"review/", "trips-*.json", "trips-*.html", "plan*.json"} <= set(lines)


def test_review_says_what_it_did_in_the_list(site, tmp_path):
    spans = tmp_path / "stretches.json"
    spans.write_text(json.dumps([{"from": str(day(12)), "to": str(day(12)), "use": "none",
                                  "bbox": [10.0, 2.5, 11.0, 3.0], "why": "wrong"}]))
    Library.photos.append(cam(2, 1, importer="com.apple.sharingd"))
    Library.photos.append(cam(-30, 1, importer="com.apple.sharingd"))
    m, _ = listed("--until", day(20), "--spans", spans, "--gap-days", "3", "--fast-kmh", "120.5")
    assert m.account == 1 and dt.datetime.fromisoformat(m.created).tzinfo
    assert m.params == {"since": str(day(1)), "until": str(day(20)), "gap_days": 3, "spans": True,
                        "flight_days": True,
                        "speeds": default_speeds() | {"fast_kmh": 120.5}}
    assert by_region(m)[5].days == (day(13), day(14)) and m.left_out["span_none"] == 2
    assert m.left_out["other_origin"] == 1  # of these dates
    assert m.sources == [
        {"kind": "photos", "label": "lib", "sha256": None, "first": str(day(-9)),
         "last": str(day(17)), "count": len(JOURNEY), "skipped": 0},
        {"kind": "spans", "label": "stretches.json", "first": str(day(12)),
         "last": str(day(12)), "count": 1, "skipped": 0,
         "sha256": hashlib.sha256(spans.read_bytes()).hexdigest()}]
    assert m.no_track and [r.gap_days for r in m.sweep] == [1, 2, 3, 5]
    assert listed()[0].params["until"] is None


def test_the_gap_is_the_one_asked_for(site):
    spans = lambda m: [(r.first, r.last) for r in m.runs if r.region == 2]  # noqa: E731
    assert spans(listed("--gap-days", "1")[0]) == [(day(3), day(4)), (day(6), day(6))]
    assert spans(listed(out="b")[0]) == [(day(3), day(6))]


def test_a_photo_after_until_still_ends_the_stay_before_it(site):
    m, _ = listed("--until", day(14))
    assert status(m)[5] == ("offered", []) and HOME not in status(m)
    assert status(listed("--until", day(13), out="b")[0])[5] == ("fixed", ["across"])


def test_the_gap_that_is_used_is_in_the_table_too(site):
    assert [r.gap_days for r in listed("--gap-days", "4")[0].sweep] == [1, 2, 3, 4, 5]


def test_the_whole_library_is_read_graded_and_placed(site):
    """The photo before --since says the first one after it had just landed; and a stay that
    began before it is not a stay of its own."""
    photos(*days(HOME, -9, -4), cam(0, HOME, 23.5), cam(1, 1, 8), *days(2, 1, 2),
           *days(3, 4, 5), *days(HOME, 8, 9))
    m, _ = listed()
    ((library, since, until, _),) = Library.opened
    assert (Path(library), since, until) == (Path("../lib"), None, None)
    assert m.left_out["fast"] >= 1 and 1 not in status(m)
    photos(*days(HOME, -9, -4), *days(1, -1, 3), *days(2, 5, 6), *days(HOME, 8, 9))
    assert status(listed()[0])[1] == ("fixed", ["across"])
    photos(*days(1, -20, -15), *days(1, 3, 4), *days(2, 5, 6), *days(HOME, 8, 9))
    assert status(listed()[0])[1] == ("fixed", ["continuation"])


def test_only_the_owners_own_camera_is_proof(site):
    photos(*[dataclasses.replace(p, importer="com.apple.sharingd") for p in days(1, 1, 3)],
           *days(2, 4, 5), *days(HOME, 8, 9))
    m, _ = listed()
    assert list(status(m)) == [2, HOME] and m.left_out["other_origin"] == 6


def test_every_fact_is_judged_by_the_speed_asked_for(site):
    hours = [cam(2, 1, 9), cam(2, 1, 10, off=0.02), cam(2, 5, 12), cam(2, 5, 13, off=0.02)]
    photos(*days(1, 1, 1), *hours, *days(5, 3, 4), *days(HOME, 8, 9))
    m, _ = listed("--fast-kmh", "120")
    assert m.boundaries[0].suggestions == () and m.left_out["fast"] == 0
    m, _ = listed("--fast-kmh", "20", out="slow")
    assert m.left_out["fast"] > 0 and any("fast" in s for b in m.boundaries for s in b.suggestions)
    assert m.params["speeds"]["fast_kmh"] == 20


def test_boundaries_and_the_table_use_the_speed_too(site, monkeypatch):
    seen = []
    for name in ("boundaries", "sweep"):
        real = getattr(trips, name)
        monkeypatch.setattr(trips, name, lambda *a, _r=real, _n=name, **kw: seen.append(
            (_n, kw["fast_kmh"])) or _r(*a, **kw))
    listed("--fast-kmh", "77")
    assert set(seen) == {("boundaries", 77.0), ("sweep", 77.0)}


def test_the_visits_of_every_gap_are_read_once_and_only_those(site):
    listed()
    asked = [int(f["region"]) for f in site.read("quickEnter/get-visits-to-region")]
    assert sorted(asked) == [1, 2, 3, 4, 5, HOME]
    m, _ = made()
    assert sorted(m.visits, key=int) == ["1", "2", "3", "4", "5", str(HOME)]


def test_a_visit_the_server_gives_in_another_order_makes_the_same_list(site):
    for n in (40, 30, 20):
        site.visit(3, day(-n), day(-n + 2), trip_id=n)
    site.visit(3, None, None)
    site.visit(3, None, None)
    a, b = site.trip(700, day(2), day(3)), site.trip(701, None, None, year=2031)
    m, _ = listed()
    assert len(by_region(m)[3].reasons) == 2 and a is b is None
    site.visits[3].reverse()
    site.trips = dict(reversed(site.trips.items()))
    Library.photos.reverse()
    again, _ = listed()
    assert again.sha256 == m.sha256 and len(list(Path("out").iterdir())) == 2


def test_trips_are_read_from_the_year_before_the_first_run_to_the_year_after(site):
    site.trip(700, day(2), day(3), "hill\x1b[2J walk")
    site.trip(701, dt.date(2030, 12, 1), day(1))      # listed under its first year
    site.trip(702, day(1, month=9), day(2, month=9))
    site.trip(703, None, None, year=2032)
    m, _ = listed()
    assert [f["year"] for f in site.read("trips/get-trips-for-year-app")] == [2030, 2031, 2032]
    assert [t["id"] for t in m.trips] == [700, 701]  # one with no dates is over no day


def test_no_runs_is_an_answer(site, capsys):
    photos()
    m, _ = listed()
    assert m.runs == [] and m.sources[0]["count"] == 0
    assert site.read("trips/get-trips-for-year-app") == []
    assert "nothing" in capsys.readouterr().out.lower()
    assert do_plan() == 0
    assert "No trip" in capsys.readouterr().out and not Path("plan-trips.json").exists()


def test_a_photo_at_the_end_of_the_map_stops_nothing(site):
    pole = dataclasses.replace(cam(9, 3, 12), lat=-90.0, lon=0.0, uuid="pole")
    far = dataclasses.replace(cam(9, 3, 13), lat=89.99, lon=180.0, uuid="far")
    photos(*JOURNEY, pole, far)
    assert listed()[0].left_out["conflict"] >= 2


# -- review: what could not be read --------------------------------------------------------


def test_an_unread_tile_is_exit_three_and_both_files_say_so(site, capsys):
    site.map.broken = {deg2tile(*centre(3), 10)}
    assert do_review() == 3
    both = capsys.readouterr()
    assert "incomplete" in both.out and "Run it again" in both.out
    m, path = made()
    assert m.coverage["tiles_unread"] == 1 and m.coverage["unread_photos"] > 0
    assert m.coverage["tiles_read"] == len(set(site.map.asked)) - 1
    assert m.left_out["unread"] > 0 and "incomplete" in path.with_suffix(".html").read_text()
    # The days beside it are not known, so nothing releases what is there.
    assert "not read" in status(m)[2][1] and status(m)[2][0] == "fixed"


def test_a_tile_that_came_at_the_second_asking_is_still_said(site):
    """The photo that asked first is not placed, so the list is not complete."""
    real, missed, key = site.map.shapes, [], ("regions", 10, *deg2tile(*centre(3), 10))

    def once(*asked):
        if asked == key and not missed:
            missed.append(asked)
            site.map.failed.add(key)
            return []
        if asked == key:
            site.map.failed.discard(key)
        return real(*asked)

    site.map.shapes = once
    photos(*JOURNEY, *(cam(8, 3, 13, off=n / 1e3) for n in (-2, 1, 2)))
    assert do_review() == 3
    m, _ = made()
    assert m.coverage["tiles_unread"] == 1 and site.map.failed == set()


def test_tiles_are_counted_once_each_and_not_off_the_map(site):
    m, _ = listed()
    asked = {(x, y) for _, _, x, y in site.map.asked}
    assert m.coverage == {"tiles_read": len(asked), "tiles_unread": 0, "unread_photos": 0}
    site.map.asked.clear()
    photos(dataclasses.replace(cam(9, 3, 12), lat=1.75, lon=-179.9999, uuid="edge"))
    m, _ = listed()
    asked = {x for _, _, x, _ in site.map.asked}
    assert -1 in asked and m.coverage["tiles_read"] == 6  # a column of three is off the map


def test_a_region_that_is_not_in_the_catalogue_is_stale(site):
    del site.catalogue["3"]
    m, _ = listed()
    assert 3 not in status(m) and m.left_out["stale"] == 6
    assert "stale map" in status(m)[2][1]


# -- review: the track -------------------------------------------------------------------


def track_file(*given, name="days.csv", head="date,lat,lon"):
    lines = [head]
    for n, region in given:
        lat, lon = centre(region) if isinstance(region, int) else region
        lines.append(f"{day(n)},{lat},{lon}")
    Path(name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


@pytest.mark.parametrize("region, joins", [
    (2, True), (4, False), ((-60.0, 10.5), False),
    ((centre(2)[0], centre(2)[1] + 0.7), True),      # beside the region, so nearest to it
])
def test_a_row_on_a_silent_day_in_another_region_is_a_barrier(site, region, joins):
    m, _ = listed(track=("--track", track_file((5, region), (1, 1))))
    spans = [(r.first, r.last) for r in m.runs if r.region == 2]
    assert spans == ([(day(3), day(6))] if joins else [(day(3), day(4)), (day(6), day(6))])
    (row,) = [s for s in m.sources if s["kind"] == "track"]
    assert row == {"kind": "track", "label": "days.csv", "first": str(day(1)),
                   "last": str(day(5)), "count": 2, "skipped": 0,
                   "sha256": hashlib.sha256(Path("days.csv").read_bytes()).hexdigest()}
    assert m.no_track is False


def test_a_row_in_a_stale_or_unread_place_is_no_region(site):
    from shapely.geometry import box

    lat, lon = centre(2)  # beside the region, where nearest to it would be the region
    site.map.regions[60] = box(lon + 0.51, lat - 0.1, lon + 0.6, lat + 0.1)
    m, _ = listed(track=("--track", track_file((5, (lat, lon + 0.55)))))
    assert len([r for r in m.runs if r.region == 2]) == 2
    x, y = deg2tile(-60.0, 10.5, 10)
    del site.map.regions[60]
    site.map.broken = {(x, y)}
    assert do_review(track=("--track", track_file((5, (-60.0, 10.5)))), out="b") == 3


def test_tracks_can_be_given_together_and_again(site):
    a, b, c = (track_file((n, 1), name=f"{x}.csv") for n, x in ((1, "a"), (2, "b"), (3, "c")))
    m, _ = listed(track=("--track", a, b, "--track", c))
    assert [s["label"] for s in m.sources if s["kind"] == "track"] == ["a.csv", "b.csv", "c.csv"]


@pytest.mark.parametrize("name, text", [
    ("days.csv", "date,lat,lon\n"), ("days.csv", "when,who\n1,2\n"), ("days.csv", ""),
    ("days.txt", "date,lat,lon\n"), ("days.gpx", "<gpx"), ("days.json", "[1, 2"),
    ("days.json", '{"features": [7]}'), ("days.csv", None), ("days.csv", b"\xff\xfe\x00"),
    *(("days.csv", f"date,lat,lon\n2031-05-05,{at}\n") for at in ("nan,10", "95,1", "5,181")),
], ids=str)
def test_a_track_that_cannot_be_read_is_an_error_and_not_an_empty_one(site, capsys, name, text):
    if text is not None:
        Path(name).write_bytes(text if isinstance(text, bytes) else text.encode())
    assert do_review(track=("--track", name)) == 2
    assert name in capsys.readouterr().err and not Path("out").exists()


def test_rows_that_cannot_be_read_are_said_and_are_exit_three(site, capsys):
    track_file((1, 1), (2, 1))
    with open("days.csv", "a", encoding="utf-8") as f:
        f.write(f"{day(5)},,\nsomeday,1.0,10.5\n,,\n")
    assert do_review(track=("--track", "days.csv")) == 3
    out = capsys.readouterr().out
    assert "days.csv: 2 rows" in out and "2 rows could not be read" in out
    assert "This list is incomplete" in out and len(list(Path("out").iterdir())) == 2
    m, path = made()
    (row,) = [s for s in m.sources if s["kind"] == "track"]
    assert (row["count"], row["skipped"]) == (2, 2)
    assert "incomplete" in path.with_suffix(".html").read_text()
    assert do_plan() == 0
    out = capsys.readouterr().out
    assert "The list was incomplete" in out and "2 rows" in out


GPX = """<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{}</trkseg></trk>{}</gpx>"""


def trkpt(n, where, hour=12, tag="trkpt"):
    lat, lon = centre(where) if isinstance(where, int) else where
    when = n if isinstance(n, str) else n and day(n)
    time = "" if n is None else f"<time>{when}T{hour:02}:00:00Z</time>"
    return f'<{tag} lat="{lat}" lon="{lon}">{time}</{tag}>'


def feature(n, where, kind="Point", **props):
    lat, lon = centre(where) if isinstance(where, int) else where
    at = {"Point": [lon, lat], "LineString": [[10.5, 1.25], [lon, lat]],
          "MultiLineString": [[[10.5, 1.25]], [[lon, lat]]]}[kind]
    return {"type": "Feature", "properties": {"date": str(day(n))} | props if n else props,
            "geometry": {"type": kind, "coordinates": at}}


def spans_of(m, region):
    return [(r.first, r.last) for r in m.runs if r.region == region]


APART = [(day(3), day(4)), (day(6), day(6))]
TOGETHER = [(day(3), day(6))]


@pytest.mark.parametrize("name, text", [
    ("t.gpx", GPX.format("".join([*(trkpt(5, 2, h) for h in (8, 9, 10)), trkpt(5, 4, 11)]), "")),
    ("t.gpx", GPX.format(trkpt(5, 2), trkpt(5, 4, tag="wpt"))),
    ("t.gpx", GPX.format(trkpt(5, 4), "".join(trkpt(5, 2, h, "rtept") for h in (8, 9, 10)))),
    ("t.xml", GPX.format("".join(trkpt(5, w) for w in (4, 2, 2, 2)), "")),
    ("t.geojson", json.dumps({"features": [feature(5, 2), feature(5, 4)]})),
    ("t.json", json.dumps([feature(5, 2), feature(5, 2), feature(5, 4), feature(5, 2)])),
    ("t.json", json.dumps({"features": [feature(5, 4, "LineString")]})),
    ("t.json", json.dumps({"features": [feature(5, 4, "MultiLineString")]})),
    ("t.json", json.dumps({"features": [feature(None, 4, startTime="2031-05-04T10:00:00Z",
                                                endTime="2031-05-06T10:00:00Z")]})),
    ("t.csv", f"date,lat,lon\n{day(5)},1.25,10.5\n{day(5)},2.25,10.5\n"),
], ids=range(10))
def test_every_place_of_a_day_in_a_track_counts(site, name, text):
    """Three fixes in Berrow and one in Dunvale: the one is the barrier."""
    Path(name).write_text(text)
    m, _ = listed(track=("--track", name))
    assert spans_of(m, 2) == APART
    assert [s["skipped"] for s in m.sources] == [0, 0]


@pytest.mark.parametrize("name, text", [
    ("t.gpx", GPX.format("".join(trkpt(5, 2, h) for h in (8, 9)) + trkpt(9, 3), "")),
    ("t.json", json.dumps([feature(5, 2), feature(5, 2, "LineString")])),
], ids=range(2))
def test_a_track_in_one_region_only_joins_as_before(site, name, text):
    Path(name).write_text(text)
    assert spans_of(listed(track=("--track", name))[0], 2) == TOGETHER


@pytest.mark.parametrize("name, text, n", [
    ("t.gpx", GPX.format(trkpt(1, 1) + trkpt(None, 2), ""), 1),                 # no time
    ("t.gpx", GPX.format(trkpt(1, 1), trkpt(None, 2, tag="wpt")), 0),           # a bookmark
    ("t.gpx", GPX.format(trkpt(1, 1), trkpt(None, 2, tag="rtept")), 0),         # a plan
    ("t.gpx", GPX.format(trkpt(1, 1), trkpt("someday", 2, tag="wpt")), 1),
    ("t.gpx", GPX.format(trkpt(1, 1) + trkpt("2031-13-05", 2), ""), 1),
    ("t.gpx", GPX.format(trkpt(1, 1) + trkpt(2, ("x", 10.5)) + "<trkpt lat='1'/>", ""), 2),
    ("t.json", json.dumps([feature(1, 1), feature(None, 2)]), 1),
    ("t.json", json.dumps([feature(1, 1), feature(None, 2, date="someday")]), 1),
    ("t.json", json.dumps([feature(1, 1), feature(2, 2) | {"geometry": None}]), 1),
    ("t.json", json.dumps([feature(1, 1), feature(2, 2) | {"geometry": {"coordinates": []}}]), 1),
    ("t.json", json.dumps([feature(1, 1), feature(2, 2) | {
        "geometry": {"coordinates": [[10.5, 1.25], ["x", 1], [10.5]]}}]), 1),
    ("t.json", json.dumps([feature(1, 1), 7, None, "x", {"properties": 5}]), 4),
    ("t.json", json.dumps([feature(1, 1), feature(2, 2, endTime="someday")]), 1),
    ("t.json", json.dumps([feature(1, 1), feature(None, 2, startTime="2031-05-05",
                                                  endTime="2031-05-01")]), 1),
    ("t.json", json.dumps([feature(1, 1), feature(None, 2, startTime="1931-05-05",
                                                  endTime="2031-05-01")]), 1),
    ("t.csv", f"date,lat,lon\n{day(1)},0.75,10.5\n{day(2)},x,10\n,,\n", 1),
], ids=range(16))
def test_rows_that_cannot_be_read_are_counted_in_every_format(site, name, text, n):
    Path(name).write_text(text)
    assert do_review(track=("--track", name)) == (3 if n else 0)
    (row,) = [s for s in made()[0].sources if s["kind"] == "track"]
    assert row["skipped"] == n and row["count"] >= 1


@pytest.mark.parametrize("bad", [["x", 1], [10.5], 1, None])
def test_what_can_be_read_after_what_cannot_still_counts(site, bad):
    there = feature(5, 4)
    there["geometry"]["coordinates"] = [bad, [10.5, 2.25]]
    Path("t.json").write_text(json.dumps([there]))
    assert do_review(track=("--track", "t.json")) == 3
    m, _ = made()
    assert spans_of(m, 2) == APART and m.sources[1]["skipped"] == 1


def test_what_the_other_readers_give_is_what_it_was(tmp_path):
    from wanderfill.sources.files import load

    path = tmp_path / "t.gpx"
    path.write_text(GPX.format("".join([*(trkpt(5, 2, h) for h in (8, 9)), trkpt(5, 4)]), ""))
    (p,) = load(path).points
    assert (p.lat, p.lon, p.weight) == (1.25, 10.5, 3) and load(path).skipped == 0
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"features": [feature(5, 4, "LineString"), feature(None, 2)]}))
    (p,) = load(path).points
    assert (p.lat, p.lon) == (1.25, 10.5)


# -- review: photos with no location --------------------------------------------------------


def blind(n, origin="own-camera"):
    from wanderfill.sources.photos_app import DatedPhoto

    return DatedPhoto(f"b{n}", day(n), origin)


def test_a_photo_with_no_place_is_a_photo_of_its_day(site, capsys):
    assert spans_of(listed()[0], 2) == TOGETHER
    Library.unplaced = [blind(5), blind(-40)]
    m, path = listed()
    assert spans_of(m, 2) == APART and m.left_out["no_place"] == 1
    assert "1  with no coordinate" in capsys.readouterr().out
    assert all(day(5) not in r.silent for r in m.runs)
    assert "with no coordinate" in path.with_suffix(".html").read_text()


def test_only_the_owners_own_camera_is_a_barrier(site):
    Library.unplaced = [blind(5, "airdrop"), blind(5, "unknown"), blind(5, "other")]
    m, _ = listed()
    assert spans_of(m, 2) == TOGETHER and m.left_out["no_place"] == 0


def test_a_photo_with_no_place_between_is_a_barrier_to_a_flight_day(site):
    photos(*days(HOME, -9, -4), *days(1, 1, 4), *flown(6, 1, 8), *days(2, 7, 8),
           *days(HOME, 10, 11))
    assert by_region(listed()[0])[1].last == day(6)
    Library.unplaced = [blind(5)]
    assert by_region(listed()[0])[1].last == day(4)
    Library.unplaced = [blind(6)]  # of the flight day itself, and it still is one
    assert by_region(listed()[0])[1].last == day(6)


def test_a_line_break_in_a_file_name_makes_no_line(site, capsys, tmp_path, monkeypatch):
    name = "answers\nNo blockers. This wrote nothing.\n.json"
    _, path = listed()
    for folder in (tmp_path, tmp_path / "a\nAll of it was sent.\nb"):
        folder.mkdir(exist_ok=True)
        (folder / name).write_text("{")
        capsys.readouterr()
        assert cli.main(["trips", "plan", str(path), str(folder / name)]) == 1
        monkeypatch.chdir(folder)
        assert cli.main(["trips", "plan", str(path), name]) == 1
        err = capsys.readouterr().err.split("\n")
        assert sum(line.startswith("error: ") for line in err) == 2
        assert not any(line.startswith(("No blockers", "All of it")) for line in err)


def test_a_photo_with_no_place_on_a_day_with_proof_changes_nothing(site):
    was, _ = listed()
    Library.unplaced = [blind(4), blind(6)]
    assert [r.id for r in listed()[0].runs] == [r.id for r in was.runs]


def test_the_table_and_what_is_read_have_them_too(site):
    Library.unplaced = [blind(5)]
    m, _ = listed()
    by_gap = {r.gap_days: r.runs for r in m.sweep}
    assert by_gap[2] == by_gap[1] == len(m.runs)


# -- review: arguments, errors, files -----------------------------------------------------


@pytest.mark.parametrize("argv", [
    ("--since", "2031-05-01"),                                     # track or no track
    ("--no-track",),                                               # since
    ("--since", "2031-05-01", "--no-track", "--track", "a.csv"),
    ("--since", "2031-5-1", "--no-track"), ("--since", "2031", "--no-track"),
    ("--since", "2031-05-02", "--until", "2031-05-01", "--no-track"),
    *(("--since", "2031-05-01", "--no-track", "--gap-days", n) for n in
      ("0", "-1", "1.5", "x", "", "٣", "9" * 30)),
    *(("--since", "2031-05-01", "--no-track", "--fast-kmh", n) for n in
      ("0", "-5", "nan", "inf", "x", "1201")),
    ("--since", "2031-05-01", "--no-track", "--spans", "absent.json"),
    ("--since", "2031-05-01", "--no-track", "--origin", "airdrop"),
], ids=str)
def test_review_arguments_that_make_no_sense_are_exit_two(site, argv):
    assert run_cli("trips", "review", "--library", "../lib", "--out", "out", *argv) == 2
    assert not Path("out").exists() and site.sent == [] and not Library.opened


def test_a_library_that_cannot_say_whose_a_photo_is_is_refused(site, capsys):
    Library.column = False
    assert do_review() == 2
    assert "own camera" in capsys.readouterr().err and not Path("out").exists()


def test_a_library_that_is_not_there_is_a_plain_error(site, capsys):
    assert run_cli("trips", "review", "--since", day(1), "--no-track", "--library", "nope") == 2
    assert "no Photos library" in capsys.readouterr().err


@pytest.mark.parametrize("change", [
    lambda s: s.visits.update({3: [{"id": 1}]}),
    lambda s: s.visits.update({3: [{"id": 1, "quality": "x"}]}),
    lambda s: s.trips.update({1: {"id": "x", "year": 2031}}),
    lambda s: setattr(s, "dare", None),
    lambda s: setattr(s, "catalogue", {}),
    lambda s: setattr(s, "catalogue", None),
], ids=["no field", "no number", "no trip id", "no list", "no regions", "no catalogue"])
def test_a_profile_that_cannot_be_read_makes_no_list(site, capsys, change):
    change(site)
    assert do_review() == 2
    assert capsys.readouterr().err.splitlines()[-1].startswith("error: ")
    assert not Path("out").exists()


def test_without_the_geo_extra_review_says_so(site, monkeypatch, capsys):
    def missing(*a):
        raise ImportError("No module named 'shapely'")

    monkeypatch.setattr(site.map, "shapes", missing)
    assert do_review() == 2
    assert "wanderfill[geo]" in capsys.readouterr().err and not Path("out").exists()


def test_the_same_list_is_rewritten_and_another_under_its_name_is_not(site, capsys):
    m, path = listed()
    assert listed()[0].sha256 == m.sha256 and len(list(Path("out").iterdir())) == 2
    path.write_text("{}")
    assert do_review() == 2
    assert path.read_text() == "{}" and str(path) in capsys.readouterr().err


def steps(monkeypatch, at):
    """Interrupt the run right after its `at`-th step on a file, whatever it is."""
    count = [0]

    def wrap(where, name):
        real = getattr(where, name)

        def step(*a, **kw):
            out = real(*a, **kw)
            count[0] += 1
            if count[0] == at:
                raise KeyboardInterrupt
            return out

        monkeypatch.setattr(where, name, step)

    for name in ("open", "fdopen"):
        wrap(os, name)
    wrap(Path, "replace")
    return count


def test_an_interrupt_after_any_step_leaves_no_half_of_a_pair(site, monkeypatch):
    m, path = listed(out="whole")
    made_up, html = TripList.load(path), path.with_suffix(".html").read_text()
    for at in range(1, 12):
        out = Path(f"out{at}")
        with monkeypatch.context() as broken:
            count = steps(broken, at)
            try:
                cli._write_pair(out, "trips", made_up, html)
                ended = True
            except KeyboardInterrupt:
                ended = False
        left = sorted(p.name for p in out.iterdir())
        assert left == ([f"trips-{m.sha256[:12]}.html", f"trips-{m.sha256[:12]}.json"]
                        if ended else []), at
        if count[0] < at:
            break
    assert at > 5


def test_a_failure_removes_only_what_this_run_put_there(site, monkeypatch):
    m, path = listed(out="whole")
    html, out = path.with_suffix(".html").read_text(), Path("out")
    replace = Path.replace

    def another(self, target):
        replace(self, target)
        if Path(target).suffix == ".json":  # somebody puts their own there at once
            Path(target).unlink()
            Path(target).write_text("theirs")
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", another)
    with pytest.raises(WanderfillError):
        cli._write_pair(out, "trips", m, html)
    (left,) = out.iterdir()
    assert left.read_text() == "theirs"


@pytest.mark.parametrize("fails", [".json", ".html"])
def test_half_a_list_is_none(site, monkeypatch, fails):
    replace = Path.replace

    def no_room(self, target):
        if Path(target).suffix == fails:
            raise OSError(28, "No space left on device")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", no_room)
    assert do_review() == 2
    assert list(Path("out").iterdir()) == []


# -- review: what it prints ----------------------------------------------------------------


def test_review_prints_counts_sources_the_table_and_what_to_do(site, capsys):
    site.visit(2, day(3), day(6))
    site.visit(4, None, None)
    site.visit(5, day(13), day(13))
    site.trip(700, day(2), day(3), "hill\x1b[2J ‮walk")
    site.trip(701, None, None, year=2031)
    Library.photos += [cam(2, 1, 13, importer=None), cam(2, 1, 14, recorded=False, zone=0)]
    assert do_review() == 0
    both = capsys.readouterr()
    out = " ".join(both.out.split())
    for words in (
        "account 1", "from 2031-05-01 to the newest one", "over 150 km/h",
        f"Photos lib: {len(JOURNEY) + 1} photos, {day(-9)} to {day(17)}",
        "You said there is no day-by-day track", "6 runs",
        "2 offered", "2 held, which your own words can release", "1 open", "1 undated",
        "1 that nothing releases", "1 overlap", "1 on the profile already",
        "1 from another camera", "1 with no clock",
        "trips on the profile that can be over these days: 1",
        "trip 700, 2031-05-02 to 2031-05-03: hill[2J walk",
        "map tiles:", "0 not read",
        "Nothing was sent",
    ):
        assert words in out, words
    assert not set(both.out + both.err) & {"\x1b", "‮"} and "sekrit" not in both.out
    table = both.out.splitlines()
    (head,) = [n for n, line in enumerate(table) if "cuts" in line and "held" in line]
    assert [line.split()[0] for line in table[head + 1:head + 5]] == ["1", "2", "3", "5"]
    assert "this list" in table[head + 2]
    (next_,) = [line for line in table if "wanderfill trips plan" in line]
    assert "trips-answers.json" in next_ and str(made()[1]) in next_


def test_review_shows_progress_and_says_what_the_first_run_costs(site, capsys):
    assert do_review() == 0
    err = capsys.readouterr().err
    assert "photos placed" in err and "map tiles" in err


BAD = "da\x1b[2J\u202eys"


def test_no_name_of_a_file_reaches_the_terminal_raw(site, capsys, tmp_path):
    raw = {"\x1b", "\u202e", "\x07"}
    track = track_file((5, 2), name=f"{BAD}.csv")
    spans = Path(f"{BAD}.json")
    spans.write_text("[]")
    library = tmp_path / f"{BAD}lib"
    library.mkdir()
    argv = ("trips", "review", "--since", day(1), "--library", library, "--spans", spans)
    assert run_cli(*argv, "--track", track, "--out", f"{BAD}out") == 0
    both = capsys.readouterr()
    assert not set(both.out + both.err) & raw and "da[2Jys.csv" in both.out
    assert "wanderfill trips plan" not in both.out and "cannot be shown" in both.out
    m, path = made(f"{BAD}out")
    files = path, answer(m, path=f"{BAD}answers.json")
    assert run_cli("trips", "plan", *files, "--plan", f"{BAD}plan.json") == 0
    assert run_cli("trips", "plan", *files, "--plan", f"{BAD}plan.json") == 2     # it is there
    assert run_cli("trips", "plan", path, f"{BAD}absent.json") == 1
    assert run_cli("trips", "plan", f"{BAD}absent.json", files[1]) == 1
    Path(f"{BAD}.txt").write_text("x")
    Path(f"{BAD}e.csv").write_text("date,lat,lon\n")
    Path(f"{BAD}p.csv").write_text("date,lat,lon\n2031-05-05,95,1\n")
    for bad in (".txt", "e.csv", "p.csv", "absent.csv"):
        assert run_cli(*argv, "--track", f"{BAD}{bad}", "--out", "o") == 2
    assert run_cli(*argv[:4], "--no-track", "--library", f"{BAD}none") == 2
    assert run_cli(*argv[:6], "--no-track", "--spans", f"{BAD}none") == 2
    path.write_text("{}")
    assert run_cli(*argv, "--track", track, "--out", f"{BAD}out") == 2
    Path(f"{BAD}file").write_text("x")
    assert run_cli(*argv, "--track", track, "--out", f"{BAD}file") == 2
    assert run_cli(*argv, "--no-track", f"{BAD}more") == 2
    both = capsys.readouterr()
    assert not set(both.out + both.err) & raw


def test_a_newline_in_a_name_makes_no_line_of_its_own(site, capsys):
    assert run_cli("trips", "review", "--since", day(1), "--library", "../lib",
                   "--track", "a\nerror: all is well.csv") == 2
    assert not any(line.startswith("error: all") for line in
                   capsys.readouterr().err.splitlines())


@pytest.mark.parametrize("out, want", [("out", "out/"), ("my out", "'my out/")])
def test_the_suggested_command_can_be_pasted(site, capsys, out, want):
    assert do_review(out=out) == 0
    (line,) = [x for x in capsys.readouterr().out.splitlines() if "wanderfill trips plan" in x]
    import shlex

    assert shlex.split(line) == ["wanderfill", "trips", "plan", str(made(out)[1]),
                                 "trips-answers.json"]
    assert want in line


@pytest.mark.parametrize("out", ["o\u200but", "o\nut", "o\u00a0ut"])
def test_a_command_that_cannot_be_shown_exactly_is_not_shown(site, capsys, out):
    assert do_review(out=out) == 0
    said = capsys.readouterr().out
    assert "wanderfill trips plan" not in said and "cannot be shown" in said


# -- placing -------------------------------------------------------------------------------


def test_each_coordinate_is_placed_once_and_every_photo_gets_it():
    from wanderfill.geo.resolve import Resolution
    from wanderfill.plan.review import Shot

    at = dt.datetime(2031, 5, 1, 12)
    shot = lambda lat, uuid: Shot(at, at.replace(tzinfo=UTC), lat, 10.5, "own-camera",  # noqa: E731
                                  "ground", "all", uuid=uuid, zone=3600)
    asked, seen = [], []

    def resolve(lat, lon):
        asked.append((lat, lon))
        return Resolution(lat, lon, 7, "tile_nearest")

    shots = [shot(2.0, "a"), shot(1.0, "b"), shot(2.0, "c")]
    obs = observe(shots, resolve, on_progress=lambda a, b: seen.append((a, b)))
    assert [o.shot for o in obs] == shots and {o.place.region for o in obs} == {7}
    assert sorted(asked) == [(1.0, 10.5), (2.0, 10.5)] and seen == [(1, 2), (2, 2)]


def test_photos_are_placed_in_the_order_of_their_tiles():
    from wanderfill.geo.resolve import Resolution
    from wanderfill.plan.review import Shot

    at = dt.datetime(2031, 5, 1, 12)
    asked = []

    def resolve(lat, lon):
        asked.append((lat, lon))
        return Resolution(lat, lon, None, "unresolved")

    where = [(n % 2 * 40.0, n / 1e3) for n in range(20)]
    observe([Shot(at, at.replace(tzinfo=UTC), a, b, "own-camera", "ground", "all")
             for a, b in where], resolve)
    tiles = [deg2tile(a, b, 10) for a, b in asked]
    assert len(set(tiles)) == 2 and tiles == sorted(tiles)


def test_placing_is_not_slow(site):
    """About a tenth of a millisecond a photo once its tiles are read, all nine points of it."""
    from wanderfill.geo.resolve import RegionResolver
    from wanderfill.plan.review import Shot

    at = dt.datetime(2031, 5, 1, 12)
    shots = [Shot(at, at.replace(tzinfo=UTC), 0.6 + n / 1e3, 10.5, "own-camera", "ground", "all")
             for n in range(2000)]
    resolver = RegionResolver({r: {} for r in NAMES}, reader=site.map, zoom=trips.ZOOM)
    start = time.perf_counter()
    obs = observe(shots, trips.region_resolver(resolver.from_tiles))
    assert {o.place.state for o in obs} == {"inside", "edge"}
    assert time.perf_counter() - start < 10


# -- plan ----------------------------------------------------------------------------------


def test_the_signature_of_the_plan():
    sig = inspect.signature(plan_for_trips)
    assert list(sig.parameters) == ["trip_list", "answers", "client", "answers_sha256", "max_rows"]
    assert sig.parameters["max_rows"].default == 50
    assert sig.parameters["answers_sha256"].kind is inspect.Parameter.KEYWORD_ONLY


def test_a_plan_says_what_it_rests_on(site):
    m, path = listed(track=("--track", track_file((5, 2))))
    files = path, answer(m, {1: "3", 2: "3", 3: "no", 4: "2", 5: "3"})
    assert run_cli("trips", "plan", *files, "--plan", "mine.json") == 0
    plan = Plan.load("mine.json")
    assert not Path("plan-trips.json").exists()
    assert rows(plan) == [[(1, 3), (2, 3), (4, 2), (5, 3)]]
    planned = [1, 2, 4, 5]
    assert set(plan.basis) == {"fingerprint", "taken_at"}
    snap = site.client().snapshot(planned)
    assert plan.basis["fingerprint"] == fingerprint(basis_of(snap, planned))
    assert dt.datetime.fromisoformat(plan.basis["taken_at"]).tzinfo
    assert plan.params == {**m.params, "no_track": False}
    assert plan.sources == [
        {"kind": "trips", "sha256": m.sha256},
        {"kind": "answers", "sha256": hashlib.sha256(Path(files[1]).read_bytes()).hexdigest()},
        *m.sources]
    assert [s["kind"] for s in m.sources] == ["photos", "track"]


def test_the_basis_is_of_the_planned_regions_and_no_others(site):
    """A visit to a region that is on the list and not planned is no drift for apply."""
    listed()
    assert do_plan({1: "3", **dict.fromkeys((2, 3, 4, 5), "no")}) == 0
    plan = Plan.load("plan-trips.json")
    assert rows(plan) == [[(1, 3)]]
    site.visit(HOME, day(16), day(17))
    site.visits[HOME][-1]["quality"] = 1  # so that the marked list is what it was
    site.writable = True
    assert apply_plan(site.client(), plan, workdir=Path("w"), confirm=True).succeeded == 1


def test_proof_is_one_entry_for_each_run_with_what_the_page_showed(site):
    site.visit(3, None, None, quality=1)
    Library.photos += [cam(8, 3, 12, importer=CAMERA, off=0.1)]
    m, _ = listed()
    words = "I stayed with a friend — not the  other time"
    assert do_plan(words={3: words}) == 0
    (op,) = Plan.load("plan-trips.json").ops
    photo = [p["detail"] for p in op.proof if p["kind"] == "ground_photo"]
    assert len(photo) == 5
    assert [int(d.split()[0]) for d in photo] == [1, 2, 3, 4, 5]
    for text in ("2 Berrow: 2031-05-03 to 2031-05-06", "photos on 3 days",
                 "none on 2031-05-05", "5 photos", "2031-05-03 22:00 (UTC+1)",
                 "2031-05-06 18:00 (UTC+1)", "evidence: "):
        assert text in photo[1], text
    for text in ("undated: may be visit", "on the profile: 1 visit", "no dates", "quality 1"):
        assert text in photo[2], text
    assert [p["detail"] for p in op.proof if p["kind"] == "memory"] == [words]
    assert "10.5" not in json.dumps(op.proof)  # no coordinate
    said = "\n".join(op.evidence)
    assert m.sha256[:12] in said and "Cray Islands (3)" in said and "2 minimal visit" in said
    assert len(op.evidence) == 6


def test_a_run_left_out_inside_a_trip_is_said(site):
    listed()
    assert do_plan(QUALITY_OF_ALL | {3: "no", 1: "no"}) == 0
    (op,) = Plan.load("plan-trips.json").ops
    assert (op.date_from, op.date_to) == (str(day(3)), str(day(14)))
    (line,) = [e for e in op.evidence if "left out" in e]
    assert "Cray Islands (3)" in line and "Aldmoor" not in line


def test_the_answers_decide_the_trips(site, capsys):
    photos(*days(HOME, -9, -4), *days(1, 1, 3), *days(2, 4, 5), *days(3, 14, 16),
           *days(HOME, 19, 20))
    m, _ = listed()
    assert [bool(b.suggestions) for b in m.boundaries] == [False, True, True]
    assert do_plan({1: "3", 2: "3", 3: "3"}, "--plan", "blank.json") == 0  # one trip
    assert rows(Plan.load("blank.json")) == [[(1, 3), (2, 3), (3, 3)]]
    both = {(2, 3): "cut", (3, HOME): "cut"}
    assert do_plan({1: "3", 2: "3", 3: "3"}, cuts=both) == 0
    assert rows(Plan.load("plan-trips.json")) == [[(1, 3), (2, 3)], [(3, 3)]]
    assert do_plan({1: "3", 2: "3", 3: "?"}, "--plan", "b.json", cuts=both) == 0
    assert rows(Plan.load("b.json")) == [[(1, 3), (2, 3)]]
    assert "question mark" in capsys.readouterr().out


def test_plan_says_what_will_mark_and_what_is_only_stored(site, capsys, tmp_path):
    site.visit(5, day(-90), day(-88), trip_id=1)
    Library.photos[:0] = days(5, -90, -88)
    listed()
    assert do_plan(QUALITY_OF_ALL | {4: "1"}) == 0
    out = capsys.readouterr().out
    assert "3 regions will be marked" in out
    assert "2 visits will be stored and mark nothing" in out
    assert "1 at quality 1" in out and "1 in a region that is marked already" in out
    site.writable = True
    work = tmp_path / "w"
    assert run_cli("apply", "plan-trips.json", "--confirm", "--workdir", work) == 0
    (seen,) = [json.loads(p.read_text()) for p in work.glob("verify-*.json")]
    assert [n["region"] for n in seen["not_counted"]] == [4]


def test_a_region_in_two_trips_is_marked_once(site):
    photos(*days(HOME, -9, -4), *days(1, 1, 3), *days(2, 4, 5), *days(1, 6, 8), *days(HOME, 10, 11))
    m, _ = listed()
    ids = [r.id for r in m.runs]
    raw = {"runs": {ids[0]: {"a": "3"}, ids[1]: {"a": "3"}, ids[2]: {"a": "3"}},
           "cuts": {m.boundaries[0].id: "cut", m.boundaries[1].id: "cut"}}
    got = plan_trips(m, raw, site.client())
    assert rows(got.plan) == [[(1, 3)], [(2, 3)], [(1, 3)]] and got.marks == {1, 2}


def test_transit_and_then_a_visit_is_marked_by_the_visit(site):
    photos(*days(HOME, -9, -4), *days(1, 1, 3), *days(2, 4, 5), *days(1, 6, 8), *days(HOME, 10, 11))
    m, _ = listed()
    ids = [r.id for r in m.runs]
    raw = {"runs": {ids[0]: {"a": "1"}, ids[1]: {"a": "1"}, ids[2]: {"a": "3"}},
           "cuts": {m.boundaries[0].id: "cut", m.boundaries[1].id: "cut"}}
    assert plan_trips(m, raw, site.client()).marks == {1}


def test_more_rows_than_fifty_need_to_be_said(site, capsys):
    m, _ = listed()
    assert do_plan(None, "--max-rows", "4") == 1
    err = capsys.readouterr().err
    assert "5 region visits" in err and "--max-rows" in err
    assert not Path("plan-trips.json").exists()
    assert do_plan(None, "--max-rows", "5") == 0
    answers = load_answers(answer(m), m)
    with pytest.raises(PlanInvalid, match="5 region visits"):
        plan_for_trips(m, answers, site.client(), max_rows=4)
    for bad in ("0", "-1", "x"):
        assert do_plan(None, "--max-rows", bad) == 2


def test_names_and_words_are_printed_without_controls_and_the_file_keeps_the_words(site, capsys):
    site.catalogue["3"]["name"] = "Cray\x1b[2J ‮Islands"
    site.catalogue[str(HOME)]["name"] = "\x1b]0;x\x07‮"
    site.visit(3, None, None)
    listed()
    words = "with friends   twice"
    capsys.readouterr()
    assert do_plan(words={3: words}) == 0
    assert run_cli("show", "-v", "plan-trips.json") == 0
    both = capsys.readouterr()
    assert not set(both.out + both.err) & {"\x1b", "\x07", "‮"}
    assert {"kind": "memory", "detail": words} in Plan.load("plan-trips.json").ops[0].proof


def test_an_incomplete_list_can_be_planned_and_is_said(site, capsys):
    m, path = listed()
    raw = dataclasses.replace(m, coverage=m.coverage | {"tiles_unread": 2, "unread_photos": 1})
    raw.save(path)
    assert do_plan() == 0
    out = capsys.readouterr().out
    assert "incomplete" in out and "2 map tiles" in out and "1 photo" in out


# -- plan: what refuses -------------------------------------------------------------------


def nothing_written():
    return not Path("plan-trips.json").exists()


def test_answers_to_another_list_are_refused(site, capsys):
    m, _ = listed()
    answer(m)
    Library.photos = [p for p in JOURNEY if p.date != day(14)]
    _, path = listed(out="other")
    assert run_cli("trips", "plan", path, "trips-answers.json") == 1
    assert "another list" in capsys.readouterr().err and nothing_written()


def test_a_list_of_another_account_is_refused(site, capsys):
    m, _ = listed()
    site.account = 2
    assert do_plan() == 2
    assert "account 1" in capsys.readouterr().err and nothing_written()
    with pytest.raises(AccountMismatch):
        plan_for_trips(m, load_answers(answer(m), m), site.client())


@pytest.mark.parametrize("change", [
    lambda s: s.visit(3, None, None), lambda s: s.visit(2, day(-90), day(-88)),
    lambda s: s.visit(HOME, day(16), day(17)),                       # not planned, and listed
    lambda s: s.visits[1].clear(), lambda s: s.visits[1][0].update(quality=2),
    lambda s: s.visits[1][0].update(trip_id=9), lambda s: s.visits[1][0].update(day_to=8),
], ids=["undated", "dated", "not planned", "gone", "quality", "trip", "date"])
def test_visits_that_are_not_what_was_shown_refuse_the_plan(site, capsys, change):
    site.visit(1, dt.date(2028, 3, 1), dt.date(2028, 3, 4))
    m, _ = listed()
    change(site)
    assert do_plan({r.region: "no" if r.status == "fixed" else "3" for r in m.runs
                    if r.region != HOME}) == 2
    err = capsys.readouterr().err
    assert "run `trips review` again" in err and nothing_written()
    (name,) = [n for r, n in NAMES.items() if f"{n} ({r})" in err]
    assert name in ("Aldmoor", "Berrow", "Cray Islands", "Grayport")


@pytest.mark.parametrize("change", [
    lambda s: s.trip(702, day(8), day(9)), lambda s: s.trips.pop(700),
    lambda s: s.trips[700].update(description="another"),
    lambda s: s.trips[700].update(date_to=str(day(4))),
    lambda s: s.trips[700].update(date_from=str(dt.date(2031, 9, 1)),
                                  date_to=str(dt.date(2031, 9, 2))),
    lambda s: s.trip(703, day(3), None, year=2031),
], ids=["new", "gone", "words", "dates", "moved away", "one end"])
def test_another_trip_over_the_same_days_refuses_the_plan(site, capsys, change):
    site.trip(700, day(2), day(3), "hills")
    listed()
    change(site)
    assert do_plan() == 2
    err = capsys.readouterr().err
    assert "run `trips review` again" in err and "trip 70" in err and nothing_written()


def test_a_trip_elsewhere_in_the_year_does_not_refuse_it(site):
    site.trip(700, day(2), day(3), "hills")
    site.trip(701, dt.date(2031, 9, 1), dt.date(2031, 9, 2))
    listed()
    site.trips[701]["description"] = "rewritten"
    site.trip(702, dt.date(2031, 11, 1), dt.date(2031, 11, 2))
    site.visit(6, dt.date(2031, 11, 1), dt.date(2031, 11, 2), 1, 702)
    assert do_plan() == 0


def test_a_trip_with_no_dates_does_not_refuse_it(site):
    """It is over no day; the visits in it are read with their regions."""
    site.trip(700, None, None, "old", year=2031)
    assert listed()[0].trips == []
    site.trips[700]["description"] = "rewritten"
    site.trip(701, None, None, year=2030)
    assert do_plan() == 0


def test_the_help_says_what_a_gap_is(capsys):
    assert run_cli("trips", "review", "--help") == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "with 1, every day with no photo ends a stay; with 2, one such day does not" in out
    assert "fewer than" not in out


def test_a_hub_seen_twice_is_cut_where_the_owner_says(site, capsys):
    """Between two places where nothing suggests it."""
    photos(*days(HOME, -9, -4), *days(1, 1, 3), *days(2, 4, 5), *days(3, 6, 6), *days(1, 7, 8),
           *days(HOME, 10, 11))
    m, _ = listed()
    assert not any(b.suggestions for b in m.boundaries[:3])
    assert do_plan({1: "3", 2: "3", 3: "3"}) == 0
    out = capsys.readouterr().out
    assert 'press "A new trip starts here" somewhere between the two' in out
    assert "Aldmoor (1), 2031-05-01 to 2031-05-03" in out
    assert do_plan({1: "3", 2: "3", 3: "3"}, cuts={(2, 3): "cut"}) == 0
    assert rows(Plan.load("plan-trips.json")) == [[(1, 3), (2, 3)], [(3, 3), (1, 3)]]


def test_a_profile_in_another_order_is_not_another_profile(site):
    for n in (90, 60):
        site.visit(4, day(-n), day(-n + 1))
    Library.photos[:0] = [*days(4, -90, -89), *days(4, -60, -59)]
    site.trip(700, day(2), day(3))
    site.trip(701, day(4), day(5))
    listed()
    site.visits[4].reverse()
    site.trips = dict(reversed(site.trips.items()))
    assert do_plan(QUALITY_OF_ALL | {4: "no"}) == 0


def test_a_profile_that_cannot_be_read_makes_no_plan(site):
    listed()
    for broken in ({"visits": {3: [{"id": 1}]}}, {"dare": None},
                   {"trips": {1: {"id": None, "year": 2031}}}):
        kept = {k: getattr(site, k) for k in broken}
        for k, v in broken.items():
            setattr(site, k, v)
        assert do_plan() == 2 and nothing_written()
        for k, v in kept.items():
            setattr(site, k, v)
    assert do_plan() == 0


def test_a_plan_file_is_never_replaced(site, capsys):
    listed()
    Path("plan-trips.json").write_text("mine")
    site.sent.clear()
    assert do_plan() == 2
    assert Path("plan-trips.json").read_text() == "mine" and site.sent == []
    os.symlink("nowhere", "link.json")
    assert do_plan(None, "--plan", "link.json") == 2
    assert not Path("nowhere").exists()
    assert do_plan(None, "--plan", "b.json") == 0
    assert Path("b.json").stat().st_mode & 0o777 == 0o600


def test_a_plan_that_is_not_valid_is_not_written(site, monkeypatch):
    from wanderfill.plan import trip_plan

    listed()
    monkeypatch.setattr(trip_plan, "validate", lambda plan: ["bad"])
    assert do_plan() == 1 and nothing_written()


def test_bad_files_are_exit_one(site):
    m, path = listed()
    good = answer(m)
    Path("bad.json").write_text("{")
    for files in (("bad.json", good), (path, "bad.json"), ("absent.json", good),
                  (good, path)):
        assert run_cli("trips", "plan", *files) == 1 and nothing_written()
    assert run_cli("trips", "plan", path) == 2


def test_no_trips_is_said_and_is_exit_zero(site, capsys):
    listed()
    for quality in ({}, dict.fromkeys(range(1, 6), "no"), dict.fromkeys(range(1, 6), "?")):
        assert do_plan(quality) == 0
        out = capsys.readouterr().out
        assert "No trip" in out and nothing_written()


# -- what check and apply do with it --------------------------------------------------------


def planned(site):
    listed()
    assert do_plan() == 0
    site.writable = True
    return Plan.load("plan-trips.json")


def test_a_second_copy_is_refused(site, tmp_path, capsys):
    plan = planned(site)
    apply_plan(site.client(), plan, workdir=tmp_path / "w", confirm=True)
    snap = site.client().snapshot([1, 2, 3, 4, 5])
    again = dataclasses.replace(plan, basis={
        "fingerprint": fingerprint(basis_of(snap, [1, 2, 3, 4, 5])), "taken_at": "later"})
    with pytest.raises(DriftError, match="second copy"):
        apply_plan(site.client(), again, workdir=tmp_path / "w", confirm=True)
    again.save("again.json")
    assert run_cli("check", "again.json", "--workdir", tmp_path / "w") == 1
    assert capsys.readouterr().out.count("second copy") == 5
    assert len(site.writes()) == 1


def test_drift_after_the_plan_stops_check_and_apply(site, tmp_path, capsys):
    plan = planned(site)
    site.visit(4, dt.date(2029, 1, 1), dt.date(2029, 1, 2))
    assert run_cli("check", "plan-trips.json", "--workdir", tmp_path / "w") == 1
    assert "drifted" in capsys.readouterr().out
    with pytest.raises(DriftError):
        apply_plan(site.client(), plan, workdir=tmp_path / "w", confirm=True)
    assert site.writes() == []


def test_verify_holds_the_server_to_every_region_row(site, tmp_path):
    plan = planned(site)
    real = site.visit

    def a_day_short(region, first, last, quality=3, trip_id=None):
        return real(region, first, last - dt.timedelta(days=region == 4), quality, trip_id)

    site.visit = a_day_short
    report = apply_plan(site.client(), plan, workdir=tmp_path / "w", confirm=True)
    (line,) = report.verified["mismatches"]
    assert "region 4" in line and report.verified["unproven"] == []


def test_the_visit_of_each_row_is_dated_and_not_the_header(site, tmp_path):
    plan = planned(site)
    apply_plan(site.client(), plan, workdir=tmp_path / "w", confirm=True)
    (v,) = site.visits[3]
    assert (v["day_from"], v["day_to"], v["quality"]) == (7, 9, 2)
    (fields,) = site.read("trips/new-trip")
    assert fields["description"] == ""
    assert all("hidden" not in r for r in json.loads(fields["regions_json"]))


# -- the words that say it -------------------------------------------------------------------


def test_the_help_says_whose_the_answers_are(capsys):
    assert run_cli("trips", "--help") == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "sends nothing" in out
    assert run_cli("trips", "review", "--help") == 0
    out = " ".join(capsys.readouterr().out.split())
    for words in ("--since DATE", "--no-track", "Exits 3", "Your own camera"):
        assert words in out, words
    assert run_cli("trips", "plan", "--help") == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "LIST ANSWERS" in out and "--max-rows" in out


def test_what_is_never_planned(site):
    plan = planned(site)
    assert {op.kind for op in plan.ops} == {"create_trip"}
    assert all(not op.region and not op.visit_id for op in plan.ops)


# -- review: --focus and --home, which only the owner gives ---------------------------------


def test_focus_and_home_ranges_are_in_the_params_and_home_folds_its_stay(site, capsys):
    m, _ = listed("--focus", "3,1", "--focus", "3", "--home", f"{HOME}:{day(15)}",
                  "--home", f"{HOME}::{day(-20)}")
    assert m.params["focus"] == [1, 3]
    assert m.params["home"] == [{"region": HOME, "from": None, "to": str(day(-20))},
                                {"region": HOME, "from": str(day(15)), "to": None}]
    assert status(m)[HOME] == ("home", ["home"]) and by_region(m)[HOME].includable == ()
    assert m.boundaries[-1].forced and "home" in {kind_of(s) for s in m.boundaries[-1].suggestions}
    out = capsys.readouterr().out
    assert f"home {HOME} (Grayport), from {day(15)} to any day: 1 run folded" in out
    assert f"home {HOME} (Grayport), from any day to {day(-20)}: 0 runs folded" in out
    assert ("focus on 1 (Aldmoor), 3 (Cray Islands): 1 trip with 5 runs shown, 1 run not "
            "shown") in out
    assert "your profile says" not in out
    assert do_plan() == 0  # a focused list plans as any other
    assert rows(Plan.load("plan-trips.json")) == [[(1, 3), (2, 3), (3, 2), (4, 3), (5, 4)]]
    assert "is not answered" not in capsys.readouterr().out  # home holds nothing


def test_a_stay_partly_inside_a_home_range_is_asked_about_and_named(site, capsys):
    m, _ = listed("--home", f"{HOME}:{day(10)}:{day(16)}")
    assert status(m)[HOME] == ("held", ["open"]) and "home part" in by_region(m)[HOME].flags
    out = capsys.readouterr().out
    assert f"{HOME} (Grayport), {day(16)} to {day(17)}: partly inside a home range" in out
    assert "0 runs folded" in out


def test_without_home_the_profile_is_only_named_and_nothing_is_folded(site, capsys):
    site.settings = {"homebase": HOME, "homebase2": 0}
    m, _ = listed()
    assert "home" not in m.params and status(m)[HOME] == ("held", ["open"])
    out = capsys.readouterr().out
    assert f"your profile says home is {HOME} (Grayport); pass --home" in out
    assert "Nothing was folded" in out
    listed("--home", f"{HOME}:{day(1)}", out="b")
    assert "your profile says" not in capsys.readouterr().out


@pytest.mark.parametrize("argv, says", [
    (("--focus", "999"), "--focus: region 999 is not in the list of regions"),
    (("--home", "999"), "--home: region 999 is not in the list of regions"),
    (("--focus", "1", "--home", "1,777:2031-01-01"), "--home: region 777 is not in the list"),
])
def test_a_region_that_is_not_in_the_catalogue_is_refused(site, capsys, argv, says):
    assert do_review(*argv) == 2 and not Path("out").exists()
    assert says in capsys.readouterr().err


@pytest.mark.parametrize("value", [
    f"{HOME}:2031-05-05:2031-05-04", f"{HOME}:5", f"{HOME}:2031-5-4", "x", f"{HOME}:a:b:c",
    "", f"{HOME}:2031-05-01:2031-05-04:2031-05-09"])
def test_a_home_range_that_makes_no_sense_is_exit_two(site, capsys, value):
    assert do_review("--home", value) == 2
    assert not Path("out").exists() and site.sent == []
