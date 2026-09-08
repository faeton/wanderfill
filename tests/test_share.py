"""``wanderfill share`` — the picture must say only what the profile says.

No network in here: the map is fed hand-made polygons, the fonts fall back
to whatever the machine has, and the client is the same fake transport the
rest of the suite uses.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PIL")

from test_quirks import FakeTransport

from wanderfill import share
from wanderfill.api.client import NomadMania
from wanderfill.api.errors import ApiError


def rows(visited_names=()):
    """196 country rows: 193 stand-ins plus the three non-UN ones."""
    names = [f"Country {i}" for i in range(193)] + sorted(share.NON_UN)
    return [
        {"country": n, "country_id": i, "visited": int(n in visited_names),
         "slow11": 0, "slow31": 0, "slow101": 0, "yes_stored": 0}
        for i, n in enumerate(names)
    ]


def test_un_excludes_the_three_extras_and_un_plus_includes_them():
    un, un_plus, total = share.un_counts(rows({"Country 1", "Country 2", "Kosovo", "Taiwan"}))
    assert (un, un_plus, total) == (2, 4, 196)


def test_un_refuses_a_changed_country_list():
    """A renamed row would silently move a number on the card; it must fail instead."""
    bad = [r for r in rows() if r["country"] != "Taiwan"]
    with pytest.raises(ApiError):
        share.un_counts(bad)
    with pytest.raises(ApiError):
        share.un_counts([*rows(), {"country": "Atlantis", "country_id": 999, "visited": 1}])


def test_collect_reads_and_never_writes():
    t = FakeTransport({
        "user/get-settings": {"user_id": 7, "first_name": "A", "last_name": "B",
                              "rank": "12", "country_rank": "", "country": "X",
                              "avatar": "7.webp"},
        "slow/get-slow-app": {"slow": rows({"Country 0", "Kosovo"})},
        "regions/get-regions-list-2": {"data": {"1": {"id": 1}, "2": {"id": 2}, "3": {"id": 3}}},
        "maps/get-visited-regions-ids-simple": {"ids": [1, 3]},
        "kye/get-kye": {"visited": [5, 6], "max": 434},
        "maps/get-visited-dare-ids-simple": {"ids": [9]},
    })
    st = share.collect(NomadMania(token="fake", transport=t))
    assert (st.un, st.un_plus, st.nm, st.nm_total) == (1, 2, 2, 3)
    assert st.name == "A B" and st.rank == 12 and st.country_rank is None
    assert st.kye == 2 and st.kye_total == 434 and st.dare == 1
    assert st.visited_regions == [1, 3]
    reads = {"user/get-settings", "slow/get-slow-app", "regions/get-regions-list-2",
             "maps/get-visited-regions-ids-simple", "kye/get-kye",
             "maps/get-visited-dare-ids-simple", "user/status-quick"}
    for action, fields in t.sent:
        assert action in reads, f"{action} is not a read this command is allowed"
        assert not any(k in fields for k in ("lat", "lng", "visits", "qid")), action


def square(lon, lat, size=10.0):
    return [(lon, lat), (lon + size, lat), (lon + size, lat + size), (lon, lat + size), (lon, lat)]


@pytest.fixture
def stats():
    return share.Stats(uid=7, name="A B", un=101, un_plus=103, un_plus_total=196,
                       nm=391, nm_total=1381, rank=1418, country_rank=26, country="X",
                       dare=30, kye=100, kye_total=434, generated="2026-09-08",
                       visited_regions=[1])


@pytest.mark.parametrize("size", sorted(share.SIZES))
@pytest.mark.parametrize("theme", sorted(share.THEMES))
def test_every_size_renders_at_its_declared_pixels(stats, size, theme):
    polys = [(1, [square(0, 40)]), (2, [square(20, -20)]), (3, [square(0, -80)])]
    img = share.render(stats, size=size, theme=theme, polygons=polys)
    assert img.size == share.SIZES[size]


def test_visited_region_is_painted_and_antarctica_is_not(stats):
    th = share.THEMES["night"]
    polys = [(1, [square(-10, 30, 20)]), (2, [square(150, -20, 20)]), (3, [square(0, -85, 5)])]
    layer = share.draw_map(polys, {1}, (0, 0, 400, 200), th)
    px = layer.load()

    def at(lon, lat):
        x = int((lon + 180) / 360 * 400)
        y_top, y_bot = share.miller_y(share.LAT_CEIL), share.miller_y(share.LAT_FLOOR)
        y = int((y_top - share.miller_y(lat)) / (y_top - y_bot) * 200)
        return px[x, y]

    def close(a, b):  # the glow under a visited region shifts a channel by one
        return all(abs(x - y) <= 2 for x, y in zip(a, b, strict=True))

    assert close(at(0, 40)[:3], th.visited[:3])
    assert close(at(160, -10)[:3], th.land[:3])
    assert at(100, 0)[3] == 0  # open ocean: nothing drawn


def test_write_cards_records_the_claim_without_the_region_list(stats, tmp_path):
    written = share.write_cards(stats, tmp_path, sizes=["square"], theme="paper",
                                polygons=None, avatar=None)
    assert (tmp_path / "nomadmania-7-square-paper.png").exists()
    claim = json.loads((tmp_path / "stats.json").read_text())
    assert claim["un"] == 101 and claim["un_plus"] == 103 and claim["nm"] == 391
    assert claim["visited_regions"] == 1  # the count; the ids are not the claim
    assert written[-1].name == "stats.json"


def test_wrap_keeps_items_whole():
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = share.Fonts().get("text", 20)
    items = ["#1418 worldwide", "#26 in X", "DARE 30", "KYE 100/434"]
    one = share._wrap(d, items, f, " · ", 10_000)
    assert one == [" · ".join(items)]
    many = share._wrap(d, items, f, " · ", 1)
    assert many == items
