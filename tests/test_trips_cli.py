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
from wanderfill.api.errors import AccountMismatch, DriftError, PlanInvalid
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
    column = True
    opened: ClassVar[list] = []

    def __init__(self, library=None, since=None, until=None, origins=None):
        self.importer_column = None
        Library.opened.append((library, since, until, origins))

    def assets(self):
        self.importer_column = Library.column
        yield from Library.photos


class Site:
    """The server: what is on the profile, and what a trip does to it."""

    def __init__(self):
        self.account, self.sent, self.dare = 1, [], [9]
        self.visits, self.trips = {}, {}
        self.token, self.lang = "sekrit-token", "en"
        self.catalogue = {str(r): {"id": r, "name": n} for r, n in NAMES.items()}
        self.writable = False
        self.next = 5000

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
                        "speeds": default_speeds() | {"fast_kmh": 120.5}}
    assert by_region(m)[5].days == (day(13), day(14)) and m.left_out["span_none"] == 2
    assert m.left_out["other_origin"] == 1  # of these dates
    assert m.sources == [
        {"kind": "photos", "label": "lib", "sha256": None, "first": str(day(-9)),
         "last": str(day(17)), "count": len(JOURNEY)},
        {"kind": "spans", "label": "stretches.json", "first": str(day(12)),
         "last": str(day(12)), "count": 1,
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
    assert [t["id"] for t in m.trips] == [700, 701, 703]


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
                   "last": str(day(5)), "count": 2,
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
    assert "2 row(s) of days.csv" in out and len(list(Path("out").iterdir())) == 2


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
        "trip 700, 2031-05-02 to 2031-05-03: hill[2J walk", "1 more with no dates",
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
    assert do_plan({1: "3", 2: "3", 3: "3"}) == 0  # neither edge is answered
    assert "not answered whether" in capsys.readouterr().out
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
    lambda s: s.trip(703, None, None, year=2030),
], ids=["new", "gone", "words", "dates", "moved away", "undated"])
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
