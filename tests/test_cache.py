"""The read cache, the slow-link transport, and the line between them and the write path.

No network: urlopen is replaced, and the cache lives in tmp_path.
"""

from __future__ import annotations

import ast
import datetime as dt
import gzip
import json
import stat
from pathlib import Path

import pytest
from test_quirks import FakeTransport

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
    seen = {}
    payload = json.dumps({"result": "OK", "data": {"1": {"id": 1}}}).encode()

    def fake_urlopen(req, timeout):
        seen.update(req.headers)
        return _Resp(gzip.compress(payload), "gzip")

    monkeypatch.setattr(tr.urllib.request, "urlopen", fake_urlopen)
    t = tr.Transport(token="x" * 36, keep_alive=False)
    assert t.webapi("regions/get-regions-list-2")["data"] == {"1": {"id": 1}}
    assert seen.get("Accept-encoding") == "gzip"
    assert t.traffic.requests == 1 and t.traffic.wire_bytes == len(gzip.compress(payload))


def test_gzip_without_a_header_is_still_inflated():
    """The tile server already does this; assume the API may too."""
    assert tr._inflate(gzip.compress(b"{}"), None) == b"{}"
    assert tr._inflate(b"{}", None) == b"{}"


def test_a_header_claiming_gzip_over_plain_json_is_not_inflated(monkeypatch):
    """A proxy that decompressed the body but kept the header.

    On a write this would turn an applied ``add-visit`` into an unknown outcome.
    """
    monkeypatch.setattr(tr.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b'{"result":"OK"}', "gzip"))
    t = tr.Transport(token="x" * 36)
    assert t.webapi("quickEnter/add-visit", region=1) == {"result": "OK"}


def test_writes_still_get_exactly_one_attempt(monkeypatch):
    from wanderfill.api.errors import UnknownWriteOutcome

    calls = []

    def boom(req, timeout):
        calls.append(1)
        raise TimeoutError("lost")

    monkeypatch.setattr(tr.urllib.request, "urlopen", boom)
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    t = tr.Transport(token="x" * 36)
    with pytest.raises(UnknownWriteOutcome):
        t.webapi("quickEnter/add-visit", region=1)
    assert len(calls) == 1


class _Conn:
    """A stand-in for HTTPSConnection. ``script`` is what each request does."""

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
        body = self._body

        class R:
            status, reason, headers = 200, "OK", {}

            def read(self):
                return body

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


def test_writes_never_use_the_kept_alive_connection(conns, monkeypatch):
    """A write sent down a connection the server already closed has an unknown
    outcome. Writes open their own, exactly as before."""
    opened = []
    monkeypatch.setattr(tr.urllib.request, "urlopen",
                        lambda req, timeout: opened.append(req) or _Resp(b"{}", None))
    t = tr.Transport(token="x" * 36)
    t.webapi("quickEnter/add-visit", region=1)
    t.ajax_text("my_series/", action="toggle", item=1, state=1, series=22)
    assert len(opened) == 2 and conns.made == []


def test_a_configured_proxy_is_not_bypassed(conns, monkeypatch):
    monkeypatch.setattr(tr.urllib.request, "getproxies", lambda: {"https": "http://p:3128"})
    opened = []
    monkeypatch.setattr(tr.urllib.request, "urlopen",
                        lambda req, timeout: opened.append(req) or _Resp(b"{}", None))
    tr.Transport(token="x" * 36).webapi("maps/get-visited-regions-ids-simple")
    assert len(opened) == 1 and conns.made == []


def test_reads_and_writes_are_paced_by_different_limiters(monkeypatch):
    monkeypatch.setattr(tr.urllib.request, "urlopen", lambda req, timeout: _Resp(b"{}", None))
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
