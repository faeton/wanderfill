"""The read cache, the slow-link transport, and the line between them and the write path.

No network: urlopen or the handlers under it are replaced, and the cache lives in tmp_path.
"""

from __future__ import annotations

import ast
import datetime as dt
import gzip
import json
import stat
from pathlib import Path

import pytest
from test_quirks import FakeTransport, wire

from wanderfill.api import cache as cache_mod
from wanderfill.api import transport as tr
from wanderfill.api.cache import ReadCache, summary_of
from wanderfill.api.client import NomadMania
from wanderfill.cli import main as cli

# --------------------------------------------------------------------------
# transport: gzip, and reads paced apart from writes
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes, encoding: str | None):
        self.body, self.headers = body, {"Content-Encoding": encoding} if encoding else {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_requests_ask_for_gzip_and_inflate_it(monkeypatch):
    """The catalogue is 173 KB plain and 42 KB gzipped; urllib asks for neither by default."""
    payload = json.dumps({"result": "OK", "data": {"1": {"id": 1}}}).encode()
    sent = wire(monkeypatch, gzip.compress(payload), encoding="gzip")
    t = tr.Transport(token="x" * 36, keep_alive=False)
    assert t.webapi("regions/get-regions-list-2")["data"] == {"1": {"id": 1}}
    assert sent[0].headers.get("Accept-encoding") == "gzip"
    assert t.traffic.requests == 1 and t.traffic.wire_bytes == len(gzip.compress(payload))


def test_gzip_without_a_header_is_still_inflated():
    """The tile server already does this; assume the API may too."""
    assert tr._inflate(gzip.compress(b"{}"), None) == b"{}"
    assert tr._inflate(b"{}", None) == b"{}"


def test_a_header_claiming_gzip_over_plain_json_is_not_inflated(monkeypatch):
    """A proxy that decompressed the body but kept the header.

    On a write this would turn an applied ``add-visit`` into an unknown outcome.
    """
    sent = wire(monkeypatch, b'{"result":"OK"}', encoding="gzip")
    t = tr.Transport(token="x" * 36)
    assert t.webapi("quickEnter/add-visit", region=1) == {"result": "OK"}
    assert len(sent) == 1


def test_writes_still_get_exactly_one_attempt(monkeypatch):
    from wanderfill.api.errors import UnknownWriteOutcome

    calls = wire(monkeypatch, TimeoutError("lost"))
    t = tr.Transport(token="x" * 36)
    with pytest.raises(UnknownWriteOutcome):
        t.webapi("quickEnter/add-visit", region=1)
    assert len(calls) == 1


class _Conn(tr.http.client.HTTPSConnection):
    """A stand-in for HTTPSConnection. ``script`` is what each request does: bytes
    are a 200, a tuple is a status and a ``Location``, an exception is raised.

    A subclass because building an opener reads attributes off the class; every
    method that would touch a socket is replaced.
    """

    made: list = []  # noqa: RUF012 - reset per test by the fixture

    def __init__(self, host, timeout=None):
        self.host, self.sock, self.sent = host, None, []
        _Conn.made.append(self)

    def request(self, method, path, body=None, headers=None):
        action = _Conn.script.pop(0)
        if isinstance(action, Exception):
            raise action
        self.sent.append(path)
        self.sock = object()
        self._body = action

    def getresponse(self):
        answer = self._body
        code, where = answer if isinstance(answer, tuple) else (200, None)

        class R:
            status, reason, headers = code, "OK", {"Location": where} if where else {}

            def read(self):
                return answer if code == 200 else b"<html>"

            def getheader(self, name):
                return None

        return R()

    def close(self):
        self.sock = None


@pytest.fixture
def conns(monkeypatch):
    _Conn.made, _Conn.script = [], []
    monkeypatch.setattr(tr.http.client, "HTTPSConnection", _Conn)
    monkeypatch.setattr(tr.urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    return _Conn


def test_reads_reuse_one_connection(conns):
    conns.script = [b"{}", b"{}", b"{}"]
    t = tr.Transport(token="x" * 36)
    for _ in range(3):
        t.webapi("quickEnter/get-visits-to-region", region=1)
    assert len(conns.made) == 1 and len(conns.made[0].sent) == 3


def test_a_read_on_a_connection_the_server_closed_goes_again_on_a_fresh_one(conns):
    import http.client

    conns.script = [b"{}", http.client.RemoteDisconnected("idle"), b'{"ok":1}']
    t = tr.Transport(token="x" * 36)
    t.webapi("maps/get-visited-regions-ids-simple")
    assert t.webapi("maps/get-visited-regions-ids-simple") == {"ok": 1}
    assert len(conns.made) == 2


@pytest.mark.parametrize("answer", [(302, "http://portal.example/in?token=" + "x" * 36),
                                    (307, "/login"), (301, None), (304, None)], ids=str)
def test_a_redirect_on_a_kept_alive_read_is_not_followed(conns, answer):
    """Its body came back as the answer, so a redirect read as broken JSON."""
    from wanderfill.api.errors import TransportError

    for read in (lambda t: t.webapi("maps/get-visited-regions-ids-simple"),
                 lambda t: t.ajax_json("V2/", action="getData", id=1)):
        conns.made, conns.script = [], [answer, b'{"ok":1}']
        t = tr.Transport(token="x" * 36)
        with pytest.raises(TransportError, match=str(answer[0])) as info:
            read(t)
        assert [len(c.sent) for c in conns.made] == [1], "not retried and not followed"
        assert "xxx" not in str(info.value) and "?" not in str(info.value)
        assert ("portal.example" in str(info.value)) == ("portal" in str(answer[1]))


def test_a_redirected_read_through_a_proxy_is_not_followed(conns, monkeypatch):
    """With a proxy configured reads go through urllib, which followed it, token and all."""
    from wanderfill.api.errors import TransportError

    monkeypatch.setattr(tr.urllib.request, "getproxies", lambda: {"https": "http://p:3128"})
    monkeypatch.setattr(tr.urllib.request, "proxy_bypass", lambda host: False)
    sent = wire(monkeypatch, (302, "http://portal.example/"), b"{}")
    with pytest.raises(TransportError, match="portal"):
        tr.Transport(token="x" * 36).webapi("maps/get-visited-regions-ids-simple")
    assert len(sent) == 1 and conns.made == []


def test_writes_never_use_the_kept_alive_connection(conns, monkeypatch):
    """A write sent down a connection the server already closed has an unknown
    outcome. Writes open their own, exactly as before."""
    opened = wire(monkeypatch, b"{}")
    t = tr.Transport(token="x" * 36)
    t.webapi("quickEnter/add-visit", region=1)
    t.ajax_text("my_series/", action="toggle", item=1, state=1, series=22)
    assert len(opened) == 2 and conns.made == []
    assert all(r.host == "nomadmania.com" for r in opened)


@pytest.mark.parametrize("action", ["quickEnter/add-visit", "maps/get-visited-dare-ids-simple"])
def test_a_request_is_tunnelled_through_the_configured_proxy(conns, monkeypatch, action):
    """Writes have an opener of their own; it must not be a way round the proxy."""
    monkeypatch.setattr(tr.urllib.request, "getproxies", lambda: {"https": "http://p:3128"})
    monkeypatch.setattr(tr.urllib.request, "proxy_bypass", lambda host: False)
    opened = wire(monkeypatch, b"{}")
    tr.Transport(token="x" * 36).webapi(action)
    (req,) = opened
    assert req.host == "p:3128" and req._tunnel_host == "nomadmania.com"
    assert conns.made == []


def test_a_configured_proxy_is_not_bypassed(conns, monkeypatch):
    monkeypatch.setattr(tr.urllib.request, "getproxies", lambda: {"https": "http://p:3128"})
    opened = wire(monkeypatch, b"{}")
    tr.Transport(token="x" * 36).webapi("maps/get-visited-regions-ids-simple")
    assert len(opened) == 1 and conns.made == []


def test_reads_and_writes_are_paced_by_different_limiters(monkeypatch):
    wire(monkeypatch, b"{}")
    t = tr.Transport(token="x" * 36, keep_alive=False)
    calls = []
    t.limiter.wait = lambda: calls.append("write")
    t.read_limiter.wait = lambda: calls.append("read")
    t.webapi("quickEnter/get-visits-to-region", region=1)
    t.webapi("quickEnter/add-visit", region=1)
    assert calls == ["read", "write"]
    assert t.limiter.min_interval >= 0.1, "writes stay at human speed"


# --------------------------------------------------------------------------
# the cache itself
# --------------------------------------------------------------------------


def _row(n=1, first=2024, last=2024, q=3):
    return {"no_of_visits": n, "first_visited_in_year": first,
            "last_visited_in_year": last, "best_visit_quality": q}


def _visits_reply(*ids):
    return {"data": [{"id": i, "year_from": 2024, "month_from": 3, "day_from": 3,
                      "year_to": 2024, "month_to": 3, "day_to": 5, "quality": 3} for i in ids]}


def test_catalogue_is_read_once_then_served_from_disk(tmp_path):
    t = FakeTransport({"regions/get-regions-list-2": {"data": {"7": {"id": 7}}}})
    c = NomadMania("fake", transport=t, cache=ReadCache(tmp_path))
    assert c.regions() == {7: {"id": 7}}
    assert NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).regions() == {7: {"id": 7}}
    assert [a for a, _ in t.sent].count("regions/get-regions-list-2") == 1


def test_refresh_reads_the_catalogue_again(tmp_path):
    t = FakeTransport({"regions/get-regions-list-2": {"data": {"7": {"id": 7}}}})
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).regions()
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path, refresh=True)).regions()
    assert [a for a, _ in t.sent].count("regions/get-regions-list-2") == 2


def test_an_old_catalogue_is_not_served(tmp_path, monkeypatch):
    t = FakeTransport({"regions/get-regions-list-2": {"data": {"7": {"id": 7}}}})
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).regions()
    later = dt.datetime.now(dt.timezone.utc) + cache_mod.CATALOGUE_MAX_AGE + dt.timedelta(hours=1)
    monkeypatch.setattr(cache_mod, "_now", lambda: later)
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).regions()
    assert [a for a, _ in t.sent].count("regions/get-regions-list-2") == 2


def test_visits_are_reused_only_while_the_summary_matches(tmp_path):
    t = FakeTransport({"quickEnter/get-visits-to-region": _visits_reply(1)})
    summaries = {10: _row(), 11: _row()}
    first = NomadMania("fake", transport=t, cache=ReadCache(tmp_path))
    first.visits_for_regions([10, 11], summaries=summaries, account=5)
    assert len(t.sent) == 2

    t.sent.clear()
    again = NomadMania("fake", transport=t, cache=ReadCache(tmp_path))
    again.visits_for_regions([10, 11], summaries={10: _row(), 11: _row(n=2)}, account=5)
    assert [f["region"] for _, f in t.sent] == [11], "only the region whose summary moved"


def test_a_date_moved_within_the_year_is_invisible_to_the_summary(tmp_path):
    """The known blind spot, pinned so nobody mistakes the cache for a live read.

    This is why ``history`` writes the cache's age into the report and tells
    the reader to use ``--refresh`` before a form, and why ``evidence
    --check-dates`` and ``export`` never use the visit cache at all.
    """
    t = FakeTransport({"quickEnter/get-visits-to-region": _visits_reply(1)})
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions(
        [10], summaries={10: _row()}, account=5)
    t.replies["quickEnter/get-visits-to-region"] = {"data": [{
        "id": 1, "year_from": 2024, "month_from": 3, "day_from": 13,
        "year_to": 2024, "month_to": 3, "day_to": 15, "quality": 3}]}
    stale = NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions(
        [10], summaries={10: _row()}, account=5)
    assert stale[10][0].date_from == dt.date(2024, 3, 3)
    fresh = NomadMania("fake", transport=t, cache=ReadCache(tmp_path, refresh=True))
    assert fresh.visits_for_regions([10], summaries={10: _row()}, account=5)[10][0].date_from \
        == dt.date(2024, 3, 13)


def test_visit_cache_is_per_account(tmp_path):
    t = FakeTransport({"quickEnter/get-visits-to-region": _visits_reply(1)})
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions(
        [10], summaries={10: _row()}, account=5)
    t.sent.clear()
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions(
        [10], summaries={10: _row()}, account=6)
    assert len(t.sent) == 1


def test_without_summaries_visits_are_always_live(tmp_path):
    t = FakeTransport({"quickEnter/get-visits-to-region": _visits_reply(1)})
    for _ in range(2):
        NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions([10])
    assert len(t.sent) == 2


def test_unsummarised_region_is_never_cached():
    assert summary_of(None) is None
    assert summary_of({"no_of_visits": 1}) is None


def test_cache_files_are_private(tmp_path):
    t = FakeTransport({"quickEnter/get-visits-to-region": _visits_reply(1)})
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions(
        [10], summaries={10: _row()}, account=5)
    mode = stat.S_IMODE((tmp_path / "visits-5.json").stat().st_mode)
    assert mode == 0o600, "a dated record of where somebody was"


# --------------------------------------------------------------------------
# the line: only reporting commands may be given a cache
# --------------------------------------------------------------------------


def test_only_reporting_commands_get_a_cache():
    """plan, apply, check, state, export and delete must see the server.

    Drift checks and deletion read-backs against a cached answer would pass
    for the wrong reason. Asserted on the source so a new ``cached=True``
    has to be argued for here.
    """
    tree = ast.parse(Path(cli.__file__).read_text())
    cached_in = set()
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            if getattr(call.func, "id", None) == "_client" and any(
                k.arg == "cached" and getattr(k.value, "value", False) for k in call.keywords
            ):
                cached_in.add(fn.name)
    assert cached_in == {"cmd_history", "cmd_evidence", "cmd_share"}


# --------------------------------------------------------------------------
# what the first review found
# --------------------------------------------------------------------------


def test_a_reply_without_its_list_is_an_error_not_an_empty_region(tmp_path):
    from wanderfill.api.errors import VerificationFailed

    t = FakeTransport({"quickEnter/get-visits-to-region": {"result": "OK"}})
    c = NomadMania("fake", transport=t, cache=ReadCache(tmp_path))
    with pytest.raises(VerificationFailed):
        c.visits_for_regions([10], summaries={10: _row()}, account=5)


def test_an_empty_list_the_summary_contradicts_is_not_cached(tmp_path):
    t = FakeTransport({"quickEnter/get-visits-to-region": {"data": []}})
    NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions(
        [10], summaries={10: _row(n=2)}, account=5)
    t.replies["quickEnter/get-visits-to-region"] = _visits_reply(1, 2)
    t.sent.clear()
    got = NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).visits_for_regions(
        [10], summaries={10: _row(n=2)}, account=5)
    assert len(t.sent) == 1 and len(got[10]) == 2


@pytest.mark.parametrize("damage", [
    {"10": {"rows": "x", "summary": [1], "saved": "2026-01-01T00:00:00+00:00"}},
    {"10": {"rows": [], "summary": [1], "saved": "yesterday"}},
    {"ten": {"rows": [], "summary": [1], "saved": "2026-01-01T00:00:00+00:00"}},
    {"10": "nonsense"},
    ["not", "a", "dict"],
])
def test_a_damaged_visit_cache_is_a_miss_and_is_rewritten(tmp_path, damage):
    (tmp_path / "visits-5.json").write_text(json.dumps(damage))
    t = FakeTransport({"quickEnter/get-visits-to-region": _visits_reply(1)})
    for refresh in (False, True):
        c = NomadMania("fake", transport=t, cache=ReadCache(tmp_path, refresh=refresh))
        assert len(c.visits_for_regions([10], summaries={10: _row()}, account=5)[10]) == 1


@pytest.mark.parametrize("damage", [{"saved": "x", "data": {}}, {"data": {"1": {}}}, [1]])
def test_a_damaged_catalogue_is_a_miss(tmp_path, damage):
    (tmp_path / "catalogue.json").write_text(json.dumps(damage))
    t = FakeTransport({"regions/get-regions-list-2": {"data": {"7": {"id": 7}}}})
    assert NomadMania("fake", transport=t, cache=ReadCache(tmp_path)).regions() == {7: {"id": 7}}


def test_a_stale_catalogue_is_reread_when_the_tiles_know_more(tmp_path, monkeypatch):
    """Tiles carrying region 99 against a cached catalogue without it.

    Before: every point in region 99 was filed as unresolved, and the coordinate
    cache kept that answer through any later refresh.
    """
    from types import SimpleNamespace

    from wanderfill.geo import resolve as rs

    class FakeResolver:
        def __init__(self, catalogue, **kw):
            self.catalogue, self.unknown_ids = catalogue, set()

        def resolve_many(self, todo, on_progress=None):
            out = {}
            for lat, lon in todo:
                if 99 in self.catalogue:
                    out[(lat, lon)] = rs.Resolution(lat, lon, 99, "tile_interior", 0.0)
                else:
                    self.unknown_ids.add(99)
                    out[(lat, lon)] = rs.Resolution(lat, lon, None, "unresolved", 0.0)
            return out

    monkeypatch.setattr(rs, "RegionResolver", FakeResolver)
    cache = ReadCache(tmp_path / "c")
    cache.put_catalogue({"1": {"id": 1}})
    t = FakeTransport({"regions/get-regions-list-2": {"data": {"1": {"id": 1}, "99": {"id": 99}}}})
    c = NomadMania("fake", transport=t, cache=cache)
    stale = c.regions()
    assert 99 not in stale and cache.served_catalogue
    args = SimpleNamespace(cache=str(tmp_path / "coords.json"), zoom=10, tolerance=0.3,
                           no_resolve=False, refresh=False)
    known, catalogue = cli._resolve_coords(args, stale, [(7.0, 80.0)], c)
    assert 99 in catalogue
    assert known[(7.0, 80.0)].region == 99


def test_refresh_asks_unresolved_points_again(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from wanderfill.geo import resolve as rs

    asked = []

    class FakeResolver:
        def __init__(self, catalogue, **kw):
            self.unknown_ids = set()

        def resolve_many(self, todo, on_progress=None):
            asked.extend(todo)
            return {k: rs.Resolution(*k, 1, "tile_interior", 0.0) for k in todo}

    monkeypatch.setattr(rs, "RegionResolver", FakeResolver)
    path = tmp_path / "coords.json"
    rs.save_coord_map(path, {(7.0, 80.0): rs.Resolution(7.0, 80.0, None, "unresolved", 0.0)},
                      {"zoom": 10, "tolerance_deg": 0.3})
    args = SimpleNamespace(cache=str(path), zoom=10, tolerance=0.3, no_resolve=False,
                           refresh=False)
    cli._resolve_coords(args, {1: {}}, [(7.0, 80.0)])
    assert asked == []
    args.refresh = True
    known, _ = cli._resolve_coords(args, {1: {}}, [(7.0, 80.0)])
    assert asked == [(7.0, 80.0)] and known[(7.0, 80.0)].region == 1


# --------------------------------------------------------------------------
# tiles: one that failed is not one that is empty
# --------------------------------------------------------------------------

HERE = (10.0, 20.35)  # in no polygon of its own tile; the next tile east is 0.04° away


def _tile(rid, *box):
    """One region, as a box in tile units. Outside 0..4096 is past the tile's edge."""
    mvt = pytest.importorskip("mapbox_vector_tile")
    from shapely.geometry import box as shape

    return mvt.encode([{"name": "regions", "features": [
        {"geometry": shape(*(box or (0, 0, 4096, 4096))), "properties": {"id": rid}}]}])


class _Tiles:
    """urlopen for the tile server: an address answers with bytes or raises; the rest are 404."""

    def __init__(self, monkeypatch):
        import urllib.error

        from wanderfill.geo import tiles

        self.error, self.at, self.asked = urllib.error.HTTPError, {}, []
        self.default = 404
        monkeypatch.setattr(tiles.urllib.request, "urlopen", self._open)

    def _open(self, req, timeout=None):
        _, z, x, y = req.full_url.removesuffix(".pbf").rsplit("/", 3)
        key = (int(z), int(x), int(y))
        self.asked.append(key)
        got = self.at.get(key, self.default)
        if isinstance(got, list):  # one answer per request, the last one for good
            got = got.pop(0) if len(got) > 1 else got[0]
        if isinstance(got, int):
            got = self.error(req.full_url, got, "no", {}, None)
        if isinstance(got, BaseException):
            raise got
        return _Resp(got, None)


def _home(point=HERE, z=10):
    from wanderfill.geo.tiles import deg2tile

    x, y = deg2tile(*point, z)
    return z, x, y


def _east(point=HERE, z=10):
    z, x, y = _home(point, z)
    return z, x + 1, y


def _resolver(tmp_path, catalogue=(1, 2)):
    from wanderfill.geo.resolve import RegionResolver
    from wanderfill.geo.tiles import TileReader

    return RegionResolver({r: {"id": r} for r in catalogue},
                          reader=TileReader(cache_dir=tmp_path / "tiles"))


def _coords_args(tmp_path, **kw):
    from types import SimpleNamespace

    return SimpleNamespace(**{"cache": str(tmp_path / "coords.json"), "zoom": 10,
                              "tolerance": 0.3, "no_resolve": False, "refresh": False, **kw})


FAILURES = [TimeoutError("timed out"), OSError("no route"), 500, 503, 429, 403,
            b"<!doctype html><html>log in to the wifi</html>", b"error",
            b"\x1f\x8b\x08 not really gzip", gzip.compress(b"x" * 4000)[:-8]]


@pytest.mark.parametrize("failure", FAILURES, ids=repr)
def test_a_failed_home_tile_is_not_answered_by_its_neighbour(tmp_path, monkeypatch, failure):
    """The point's own tile timed out, the next one held a polygon 0.04° away, and the
    point became that region. The answer was saved and never asked for again."""
    net = _Tiles(monkeypatch)
    net.at = {_home(): failure, _east(): _tile(2)}
    r = _resolver(tmp_path)
    got = r.resolve_many([HERE])[HERE]
    assert (got.method, got.region, got.distance_deg) == ("tile_error", None, 0.0)
    assert not got.confident
    assert r.from_tiles(*HERE, ring=0).method == "tile_error"
    assert r.reader.failed == {("regions", *_home())}
    assert not (tmp_path / "tiles" / "regions" / "10" / str(_home()[1])
                / f"{_home()[2]}.pbf").exists()


@pytest.mark.parametrize("code", [204, 404, 410])
def test_an_absent_tile_is_still_empty(tmp_path, monkeypatch, code):
    """Most of the planet has no tile. That is an answer, and it is kept."""
    net = _Tiles(monkeypatch)
    net.default = b"" if code == 204 else code
    r = _resolver(tmp_path)
    assert r.resolve_many([HERE])[HERE].method == "unresolved"
    assert r.reader.failed == set()
    asked = len(net.asked)
    assert _resolver(tmp_path).resolve_many([HERE])[HERE].method == "unresolved"
    assert len(net.asked) == asked  # an absent tile is kept on disk
    net.at = {_east((10.0, 21.2)): _tile(2)}
    assert r.resolve_many([(10.0, 21.2)])[(10.0, 21.2)].method == "tile_nearest"


def test_a_failed_neighbour_does_not_spoil_an_interior_hit(tmp_path, monkeypatch):
    net = _Tiles(monkeypatch)
    net.default = TimeoutError()
    net.at = {_home(): _tile(1)}
    r = _resolver(tmp_path)
    assert r.resolve_many([HERE])[HERE].region == 1
    assert r.from_tiles(*HERE, ring=1).method == "tile_interior"


def test_a_failed_home_tile_still_takes_an_interior_hit_next_door(tmp_path, monkeypatch):
    """A polygon drawn past its tile's edge contains the point: that is not a guess."""
    net = _Tiles(monkeypatch)
    net.at = {_home(): TimeoutError(), _east(): _tile(2, -4096, 0, 4096, 4096)}
    got = _resolver(tmp_path).resolve_many([HERE])[HERE]
    assert (got.method, got.region) == ("tile_interior", 2)


def test_a_failed_neighbour_leaves_a_nearest_candidate_unanswered(tmp_path, monkeypatch):
    """The tile that failed may hold the region the point is in, or a nearer one."""
    net = _Tiles(monkeypatch)
    net.at = {_home(): _tile(1, 3000, 0, 3400, 4096), _east(): TimeoutError()}
    assert _resolver(tmp_path).resolve_many([HERE])[HERE].method == "tile_error"


def test_a_failing_tile_is_asked_for_twice_at_most(tmp_path, monkeypatch):
    """Ten thousand points on a dead network are not ten thousand timeouts."""
    net = _Tiles(monkeypatch)
    net.default = TimeoutError()
    r = _resolver(tmp_path)
    got = r.resolve_many([(10.0, 20.1 + i / 1000) for i in range(50)])
    assert {v.method for v in got.values()} == {"tile_error"}
    assert max(net.asked.count(k) for k in net.asked) == 2
    assert len(set(net.asked)) == 9


def test_a_tile_that_answers_the_second_time_is_not_failed(tmp_path, monkeypatch):
    net = _Tiles(monkeypatch)
    net.at = {_home(): [TimeoutError(), _tile(1)]}
    r = _resolver(tmp_path)
    assert r.reader.shapes("regions", *_home()) == []
    assert ("regions", *_home()) in r.reader.failed
    assert len(r.reader.shapes("regions", *_home())) == 1  # not memoised as empty
    assert r.reader.failed == set()
    assert r.resolve_many([HERE])[HERE].method == "tile_interior"


@pytest.mark.parametrize("refetch, method", [
    (_tile, "tile_interior"), (lambda rid: b"still not a tile", "tile_error"),
], ids=["good", "corrupt again"])
def test_a_corrupt_tile_on_disk_is_fetched_once_more(tmp_path, monkeypatch, refetch, method):
    """Earlier versions wrote whatever the network said; a portal's page stayed for good."""
    net = _Tiles(monkeypatch)
    net.at = {_home(): refetch(1)}
    z, x, y = _home()
    path = tmp_path / "tiles" / "regions" / str(z) / str(x) / f"{y}.pbf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"<html>log in to the wifi</html>")
    r = _resolver(tmp_path)
    assert r.resolve_many([HERE])[HERE].method == method
    assert net.asked.count(_home()) == 1
    assert path.exists() is (method == "tile_interior")
    if path.exists():
        assert path.read_bytes() == _tile(1)


def test_a_tile_off_the_grid_is_absent_and_not_asked_for(tmp_path, monkeypatch):
    """Beside the antimeridian the ring runs off the map. Whatever the server says to
    tile 1024 of 1024 must not make every point there unreadable for good."""
    net = _Tiles(monkeypatch)
    net.at = {_home((-17.0, 179.95)): _tile(1, 3000, 0, 3400, 4096)}
    r = _resolver(tmp_path)
    r.resolve_many([(-17.0, 179.95), (-17.0, -179.95)])
    assert r.resolve_many([(-17.0, 179.95)])[(-17.0, 179.95)].method == "tile_nearest"
    assert all(0 <= x < 1024 and 0 <= y < 1024 for _, x, y in net.asked)
    assert r.reader.failed == set()


def test_a_region_measured_across_a_failed_tile_is_not_small(tmp_path, monkeypatch):
    """Half a region is a small region. Only a whole one may be exempted."""
    from wanderfill.geo.resolve import region_extent_km, small_regions

    net = _Tiles(monkeypatch)
    net.at = {_home(z=8): _tile(1, 0, 0, 10, 10)}  # a sliver of it, the rest next door
    reader = _resolver(tmp_path).reader
    assert 0 < region_extent_km(reader, *HERE, 1) < 5
    net.at[_east(z=8)] = TimeoutError()
    # A second reader: the first holds the 404 it was told for that tile.
    reader = _resolver(tmp_path / "again").reader
    assert region_extent_km(reader, *HERE, 1) == 0.0
    assert small_regions(reader, {1: HERE}, threshold_km=5) == set()


def test_tile_errors_are_not_saved_to_the_coordinate_cache(tmp_path, monkeypatch):
    from wanderfill.geo import resolve as rs

    monkeypatch.setenv("HOME", str(tmp_path))
    fine = (10.0, 25.0)
    net = _Tiles(monkeypatch)
    net.at = {_home(): TimeoutError(), _east(): _tile(2), _home(fine): _tile(1)}
    args = _coords_args(tmp_path)
    known, _ = cli._resolve_coords(args, {1: {}, 2: {}}, [HERE, fine])
    assert known[HERE].method == "tile_error" and known[fine].region == 1
    assert set(rs.load_coord_map(args.cache)) == {fine}
    net.at[_home()] = _tile(1)
    known, _ = cli._resolve_coords(args, {1: {}, 2: {}}, [HERE, fine])
    assert known[HERE].region == 1
    assert rs.load_coord_map(args.cache)[HERE].region == 1


def test_a_tile_error_in_a_cache_is_asked_again():
    from wanderfill.geo.resolve import Resolution, valid_only

    sea, lost = Resolution(0, 0, None, "unresolved"), Resolution(0, 1, None, "tile_error")
    assert valid_only({(0, 0): sea, (0, 1): lost}, {}) == {(0, 0): sea}


def test_a_tile_error_does_not_hide_a_stale_catalogue(tmp_path, monkeypatch):
    """Region 99 is in a tile that was read, beside one that was not."""
    monkeypatch.setenv("HOME", str(tmp_path))
    net = _Tiles(monkeypatch)
    inland = (10.0, 25.0)
    net.at = {_home(): TimeoutError(), _east(): _tile(99), _home(inland): _tile(99)}
    cache = ReadCache(tmp_path / "c")
    cache.put_catalogue({"1": {"id": 1}})
    t = FakeTransport({"regions/get-regions-list-2": {"data": {"1": {"id": 1}, "99": {"id": 99}}}})
    c = NomadMania("fake", transport=t, cache=cache)
    known, catalogue = cli._resolve_coords(_coords_args(tmp_path), c.regions(), [HERE, inland], c)
    assert 99 in catalogue
    assert known[inland].region == 99 and known[HERE].method == "tile_error"


def test_history_and_evidence_say_how_many_points_could_not_be_read(
        tmp_path, monkeypatch, capsys):
    """Three photos on a day whose tile timed out printed as open water: a form answered
    from that run would have said there was no evidence of the country they are in."""
    from wanderfill.sources import photos_app

    monkeypatch.setenv("HOME", str(tmp_path))
    net = _Tiles(monkeypatch)
    fine = (10.0, 25.0)
    net.at = {_home(): TimeoutError(), _east(): _tile(2), _home(fine): _tile(1),
              _home(fine, 8): TimeoutError()}
    monkeypatch.setattr(cli, "_client", lambda args, **kw: NomadMania(
        "fake", transport=FakeTransport({
            "regions/get-regions-list-2": {"data": {"1": {"name": "Istanbul"},
                                                    "2": {"name": "Madrid"}}},
            "quickEnter/get-regions": {"data": {"regions": [{"id": 1, "flag1": "tr"},
                                                            {"id": 2, "flag1": "es"}]}},
            "slow/get-slow-app": {"slow": [
                {"country": "Turkey", "flag": "tr", "country_id": 1},
                {"country": "Spain", "flag": "es", "country_id": 2}]},
            "maps/get-visited-regions-ids-simple": {"ids": [2]},
            "user/status-quick": {"uid": 5},
        })))
    out, cache = tmp_path / "o", str(tmp_path / "coords.json")

    track = tmp_path / "track.csv"
    track.write_text("date,lat,lon\n2024-03-01,10.0,25.0\n"
                     "2024-03-02,10.0,20.35\n2024-03-03,10.0,20.35\n2024-03-04,10.0,25.0\n")
    history = ["history", "check", "--countries", "es", "--track", str(track), "--no-photos",
               "--no-profile", "--out", str(out), "--cache", cache]
    assert cli.main(history) == 0
    said = capsys.readouterr().out
    assert "2 points" in said and "could not be read" in said and "Re-running" in said
    for name in ("history-check.md", "history-check.csv", "history-check.json"):
        text = (out / name).read_text()
        assert "2 points" in text and "could not be read" in text, name
        assert "2024-03-02 → 2024-03-03" in text, name
    md = (out / "history-check.md").read_text()
    assert "**unresolved points:** 0 (open water" in md and "**points not read:** 2 points" in md
    # The cache holds neither; a run told not to fetch must not call them open water.
    assert cli.main([*history, "--no-resolve"]) == 0
    md = (out / "history-check.md").read_text()
    assert "**unresolved points:** 0 (open water" in md and "**points not read:** 2 points" in md

    assets = [photos_app.PhotoAsset(
        uuid=f"u{i}", taken=dt.datetime(2024, 3, 2 + i), lat=lat, lon=lon, directory="0",
        filename="x.heic", original_name="x.heic")
        for i, (lat, lon) in enumerate([fine, HERE, HERE])]
    monkeypatch.setattr(photos_app.PhotosAppSource, "assets", lambda self: iter(assets))
    evidence = ["evidence", "--library", str(tmp_path / "x.photoslibrary"),
                "--out", str(out), "--cache", cache]
    for argv in (evidence, [*evidence, "--no-resolve"]):
        assert cli.main(argv) == 0
        said = capsys.readouterr().out
        assert "2 photos" in said and "could not be read" in said and "Re-running" in said
        md = (out / "index.md").read_text()
        assert "0 in no region (flights" in md
        assert "2 photos could not be placed" in md and "Re-running" in md
        body = json.loads((out / "evidence.json").read_text())
        assert (body["assets_unplaced"], body["assets_unread"]) == (2, 2)
        if "--no-resolve" not in argv:
            assert "No region here was small enough" not in md
            assert "because 1 map tile could not be read" in md
            assert body["tiles_unread"] == 1

    net.at[_home()] = _tile(2)
    assert cli.main(history) == 0
    assert "could not be read" not in capsys.readouterr().out
    md = (out / "history-check.md").read_text()
    assert "not read" not in md and "YES" in md
    assert cli.main(evidence) == 0
    assert "could not be placed" not in (out / "index.md").read_text()


# --------------------------------------------------------------------------
# history: the visit cache is for recall, not for forms
# --------------------------------------------------------------------------


@pytest.mark.parametrize("argv, cached", [
    (["trips"], False), (["check", "--countries", "es"], False), (["list"], False),
    (["where", "2024"], True), (["trips", "--cached"], True),
    (["where", "2024", "--no-cached"], False),
])
def test_form_queries_read_visits_live_unless_asked(tmp_path, monkeypatch, argv, cached):
    """A delete-and-re-add in the same year leaves the summary unchanged, so the
    cache can answer a form with a visit that no longer exists. Forms read live."""
    summary = {"no_of_visits": 1, "first_visited_in_year": 2019,
               "last_visited_in_year": 2019, "best_visit_quality": 3}
    t = FakeTransport({
        "regions/get-regions-list-2": {"data": {"4": {"name": "Madrid"}}},
        "quickEnter/get-regions": {"data": {"regions": [{"id": 4, "flag1": "es", **summary}]}},
        "slow/get-slow-app": {"slow": [{"country": "Spain", "flag": "es", "country_id": 2}]},
        "maps/get-visited-regions-ids-simple": {"ids": [4]},
        "user/status-quick": {"uid": 5},
        "quickEnter/get-visits-to-region": _visits_reply(9),
    })
    monkeypatch.setattr(cli, "_client", lambda args, **kw: NomadMania(
        "fake", transport=t, cache=ReadCache(tmp_path / "c")))
    rc = cli.main(["history", *argv, "--no-photos", "--out", str(tmp_path / "o")])
    assert rc == 0
    assert (tmp_path / "c" / "visits-5.json").exists() is cached
