"""``wanderfill history`` — omission is the dangerous error, invention the other one.

No network. Regions are hand-made: 1 and 2 in Turkey, 3 in Greece, 4 in Spain,
5 in Iran, 6 Greenland (a Danish territory), 7 in Portugal.
"""

from __future__ import annotations

import datetime as dt

import pytest

from wanderfill import history as h
from wanderfill.api.client import Visit, YearOnly
from wanderfill.countries import (
    GROUPS,
    CountryMap,
    GroupError,
    Place,
    UnmappedCountries,
    build_country_map,
    parse_groups,
    validate,
)

D = dt.date
TR1, TR2, GR, ES, IR, GL, PT = 1, 2, 3, 4, 5, 6, 7


def cmap() -> CountryMap:
    return CountryMap(
        places={
            TR1: Place("TR", "Türkiye"),
            TR2: Place("TR", "Türkiye"),
            GR: Place("GR", "Greece"),
            ES: Place("ES", "Spain"),
            IR: Place("IR", "Iran"),
            GL: Place("DK", "Denmark", territory="Greenland"),
            PT: Place("PT", "Portugal"),
        },
        region_names={
            TR1: "Istanbul",
            TR2: "Antalya",
            GR: "Thessaloniki",
            ES: "Madrid",
            IR: "Tehran",
            GL: "Greenland",
            PT: "Lisbon",
        },
    )


def pt(day, region, source="track", at=None, lat=0.0, lon=0.0):
    return h.Point(day, lat, lon, region, source, at=at)


def days_of(start, n, region, source="track"):
    return [pt(start + dt.timedelta(days=i), region, source) for i in range(n)]


def visit(region, a, b=None, vid=1):
    return Visit(id=vid, region=region, date_from=a, date_to=b, quality=3, trip_id=None)


def ledger(points=(), visits=(), **kw):
    return h.build_ledger(points, visits, cmap(), **kw)


# ----------------------------------------------------------------- countries


def test_groups_mix_and_subtract():
    got = parse_groups("eea,ch,uk,us,-ie")
    assert {"CH", "GB", "US", "DE"} <= got
    assert "IE" not in got


def test_subtraction_is_order_independent():
    assert parse_groups("-ie,eu") == parse_groups("eu,-ie")


def test_unknown_group_is_an_error_not_an_empty_set():
    with pytest.raises(GroupError):
        parse_groups("eea,schengn")


def test_group_shapes():
    assert GROUPS["eu"].members < GROUPS["eea"].members
    assert "CH" not in GROUPS["eea"].members
    assert {"IE", "CY"}.isdisjoint(GROUPS["schengen"].members)
    assert len(GROUPS["schengen"].members) == 29
    assert len(GROUPS["un"].members) == 193
    assert len(GROUPS["un+"].members) == 196


def test_country_names_are_folded():
    assert parse_groups("Czech Republic") == parse_groups("Czechia") == {"CZ"}
    assert parse_groups("St. Lucia") == {"LC"}


def test_validate_names_every_unmapped_country():
    with pytest.raises(UnmappedCountries, match="Atlantis"):
        validate([{"country": "Spain"}, {"country": "Atlantis"}])


def test_palestine_as_nomadmania_spells_it():
    assert validate([{"country": "Palestinian Territory"}]) == {"Palestinian Territory": "PS"}


def test_nauru_as_nomadmania_spells_it_since_october_2026():
    # The live list said "Nauru" in August 2026 and "Naoero" by October; the
    # first real run of `history` stopped on it.
    assert validate([{"country": "Naoero"}]) == {"Naoero": "NR"}
    assert parse_groups("Nauru") == {"NR"}


def test_territory_by_second_flag():
    countries = [{"country": "Denmark", "flag": "dk"}, {"country": "Spain", "flag": "es"}]
    regions = {
        10: {"name": "Copenhagen", "flag1": "dk"},
        11: {"name": "Greenland", "flag1": "gl", "flag2": "dk"},
        12: {"name": "Nowhere", "flag1": "zz"},
    }
    m = build_country_map(countries, regions)
    assert m.place(10) == Place("DK", "Denmark")
    assert m.place(11).territory == "Greenland" and m.place(11).iso == "DK"
    # unmapped is kept, loudly, never dropped
    assert m.place(12).iso == "??" and "Nowhere" in m.place(12).country


def test_syria_changed_its_flag_before_its_regions_did():
    # Live in October 2026: the country row had flag 809, its four regions 82,
    # and an ESTA check said "Syria: no evidence" for any Syrian visit.
    countries = [{"country": "Syria", "flag": "809"}, {"country": "Russia", "flag": "58"}]
    regions = {
        1: {"name": "Syria - Southwest (Damascus, Dara'a)", "flag1": "82", "flag2": None},
        2: {"name": "Russia – Franz Josef Land", "flag1": "306", "flag2": None},
        3: {"name": "Vatican", "flag1": "247", "flag2": None},
        4: {"name": "Greenland – Northeast NP (Kulusuk)", "flag1": "185", "flag2": None},
    }
    m = build_country_map(countries, regions)
    assert m.place(1) == Place("SY", "Syria")
    assert m.place(2) == Place("RU", "Russia")
    # not one of the live countries by name: still unmapped, still listed
    assert m.place(3).iso == "??" and m.place(4).iso == "??"


def test_a_region_placed_by_name_keeps_its_territory_label():
    # With the flags gone a name cannot say that New Caledonia is not France
    # proper. A second flag is the only hint left, so it is listed on its own
    # rather than folded in and lost to `--exclude FR`.
    countries = [{"country": "France", "flag": "new"}]
    regions = {
        1: {"name": "France – New Caledonia - Main island (Noumea)", "flag1": "nc", "flag2": "old"},
        2: {"name": "France – Aquitaine (Bordeaux)", "flag1": "old", "flag2": None},
    }
    m = build_country_map(countries, regions)
    assert m.place(1).iso == "FR" and "New Caledonia" in m.place(1).territory
    assert m.place(2) == Place("FR", "France")


def test_a_region_placed_by_name_follows_the_alias_table():
    # The country list says "Naoero"; a region still called "Nauru" is the same place.
    m = build_country_map([{"country": "Naoero", "flag": "new"}],
                          {1: {"name": "Nauru - Yaren", "flag1": "old", "flag2": None}})
    assert m.place(1).iso == "NR"


# ------------------------------------------------------------------- ledger


def test_one_day_three_countries_gives_three_rows():
    day = D(2024, 3, 1)
    lg = ledger([pt(day, TR1), pt(day, GR), pt(day, ES)])
    assert {d.iso for d in lg.days()} == {"TR", "GR", "ES"}


def test_same_day_two_sources_is_one_day():
    day = D(2024, 3, 1)
    lg = ledger([pt(day, TR1, "track"), pt(day, TR1, "photos")])
    [d] = lg.days()
    assert d.sources == {"track", "photos"}


def test_profile_range_expands_per_day():
    lg = ledger(visits=[visit(ES, D(2024, 1, 1), D(2024, 1, 5))])
    assert [d.date.day for d in lg.days()] == [1, 2, 3, 4, 5]


def test_profile_range_backwards_does_not_invert():
    lg = ledger(visits=[visit(ES, D(2024, 1, 5), D(2024, 1, 1))])
    assert [d.date for d in lg.days()] == [D(2024, 1, 5)]


def test_year_only_and_undated_visits_are_claims_not_days():
    lg = ledger(visits=[visit(IR, YearOnly(2019), YearOnly(2019), 7), visit(GR, None, None, 8)])
    assert lg.days() == []
    assert {(u.region, u.year) for u in lg.undated} == {(IR, 2019), (GR, None)}


def test_trip_owned_visits_count():
    v = Visit(
        id=1, region=ES, date_from=D(2024, 1, 1), date_to=D(2024, 1, 1), quality=3, trip_id=99
    )
    assert ledger(visits=[v]).days()


# -------------------------------------------------------------------- speed


def _flight(day):
    t0 = dt.datetime(2024, 3, 1, 10)
    # Istanbul -> 1,000 km east in an hour: aircraft
    return [
        pt(day, TR1, "photos", t0, 41.0, 29.0),
        pt(day, IR, "photos", t0 + dt.timedelta(minutes=30), 41.0, 35.0),
        pt(day, IR, "photos", t0 + dt.timedelta(minutes=60), 41.0, 41.0),
    ]


def test_fast_points_are_airborne():
    lg = ledger(_flight(D(2024, 3, 1)))
    kinds = {d.iso: d.kind for d in lg.days()}
    assert kinds["IR"] == h.AIRBORNE


def test_a_slow_point_same_day_keeps_it_on_the_ground():
    day = D(2024, 3, 1)
    pts = [
        *_flight(day),
        pt(day, IR, "photos", dt.datetime(2024, 3, 1, 20), 41.0, 41.0),
        pt(day, IR, "photos", dt.datetime(2024, 3, 1, 21), 41.0, 41.01),
    ]
    kinds = {d.iso: d.kind for d in ledger(pts).days()}
    assert kinds["IR"] == h.OBSERVED


def test_day_level_rows_are_never_airborne():
    day = D(2024, 3, 1)
    lg = ledger([pt(day, TR1, lat=41, lon=29), pt(day, IR, lat=35, lon=51)])
    assert all(d.kind == h.OBSERVED for d in lg.days())


# ----------------------------------------------------------------- bridging


def test_same_country_short_gap_is_bridged_and_labelled():
    pts = days_of(D(2024, 3, 1), 3, TR1) + days_of(D(2024, 3, 9), 4, TR2)
    [t] = h.trips(ledger(pts))
    assert (t.entry, t.exit, t.observed, t.bridged) == (D(2024, 3, 1), D(2024, 3, 12), 7, 5)


def test_gap_longer_than_bridge_is_two_trips():
    pts = days_of(D(2024, 3, 1), 3, TR1) + days_of(D(2024, 3, 9), 4, TR1)
    assert len(h.trips(ledger(pts, bridge=4))) == 2


def test_different_countries_are_never_bridged_and_edges_open():
    pts = days_of(D(2024, 3, 1), 3, TR1) + days_of(D(2024, 3, 9), 2, GR)
    tr, gr = h.trips(ledger(pts))
    assert tr.exit == D(2024, 3, 3) and tr.exit_latest == D(2024, 3, 9) and tr.exit_open
    assert gr.entry == D(2024, 3, 9) and gr.entry_earliest == D(2024, 3, 3) and gr.entry_open


def test_third_country_inside_the_gap_blocks_the_bridge():
    pts = [*days_of(D(2024, 3, 1), 2, TR1), pt(D(2024, 3, 4), GR), *days_of(D(2024, 3, 6), 2, TR1)]
    assert len([t for t in h.trips(ledger(pts)) if t.iso == "TR"]) == 2


def test_a_crossing_day_edge_blocks_the_bridge():
    pts = [
        pt(D(2024, 3, 1), TR1),
        pt(D(2024, 3, 2), TR1),
        pt(D(2024, 3, 2), GR),
        pt(D(2024, 3, 6), TR1),
        pt(D(2024, 3, 6), GR),
        pt(D(2024, 3, 7), TR1),
    ]
    assert not any(d.kind == h.BRIDGED for d in ledger(pts).days())


def test_crossing_day_closes_the_edge():
    pts = [*days_of(D(2024, 3, 1), 2, TR1), pt(D(2024, 3, 2), GR), *days_of(D(2024, 3, 3), 2, GR)]
    gr = next(t for t in h.trips(ledger(pts)) if t.iso == "GR")
    assert not gr.entry_open


def test_adjacent_days_in_two_countries_leave_the_crossing_open():
    """Spain on the 1st, Portugal on the 2nd: either day could be the travel day."""
    pts = [pt(D(2024, 1, 1), ES), pt(D(2024, 1, 2), PT)]
    es, pt_ = h.trips(ledger(pts))
    assert es.exit_open and es.exit_latest == D(2024, 1, 2)
    assert pt_.entry_open and pt_.entry_earliest == D(2024, 1, 1)


def test_an_unplaced_point_blocks_the_bridge():
    pts = [pt(D(2024, 1, 1), ES), pt(D(2024, 1, 2), None), pt(D(2024, 1, 3), ES)]
    assert not any(d.kind == h.BRIDGED for d in ledger(pts).days())


def test_an_unplaced_point_on_an_edge_blocks_the_bridge():
    pts = [pt(D(2024, 1, 1), ES), pt(D(2024, 1, 1), None), pt(D(2024, 1, 3), ES)]
    assert not any(d.kind == h.BRIDGED for d in ledger(pts).days())


def test_two_unmapped_regions_stay_two_and_never_bridge():
    pts = [pt(D(2024, 1, 1), 901), pt(D(2024, 1, 3), 902)]
    lg = ledger(pts)
    assert len({d.key for d in lg.days()}) == 2
    assert not any(d.kind == h.BRIDGED for d in lg.days())
    assert len(h.trips(lg)) == 2


def test_half_dated_visit_is_a_day_and_a_claim_over_both_years():
    lg = ledger(visits=[visit(ES, D(2023, 12, 1), YearOnly(2024), 3)])
    assert [d.date for d in lg.days()] == [D(2023, 12, 1)]
    [u] = lg.undated
    assert (u.year_from, u.year_to) == (2023, 2024)
    r = h.query_trips(lg, since=D(2024, 1, 1), until=D(2024, 12, 31))
    assert any("#3" in row["flags"] for row in r.sections[0].rows)


def test_year_range_visit_overlaps_a_later_window():
    lg = ledger(visits=[visit(ES, YearOnly(2023), YearOnly(2024), 4)])
    r = h.query_trips(lg, since=D(2024, 1, 1), until=D(2024, 12, 31))
    assert [row["entry"] for row in r.sections[0].rows] == ["CHECK"]


# -------------------------------------------------------------------- trips


def test_first_keeps_earliest_and_counts_the_rest():
    pts = days_of(D(2020, 1, 1), 2, TR1) + days_of(D(2022, 1, 1), 2, TR1)
    r = h.query_trips(ledger(pts), since=D(2016, 1, 1), until=D(2026, 1, 1), first=True)
    [row] = r.sections[0].rows
    assert row["entry"].startswith("2020-01-01") and row["trips in window"] == 2
    assert "no data earlier" in row["entry"]


def test_exclude_removes_country_but_not_its_territory():
    pts = days_of(D(2024, 1, 1), 2, ES) + days_of(D(2024, 2, 1), 2, GL)
    r = h.query_trips(ledger(pts), since=None, until=None, exclude=parse_groups("eea"))
    [row] = r.sections[0].rows
    assert row["country"] == "Greenland (Denmark)" and "territory" in row["flags"]


def test_sovereign_mode_folds_territory_and_excludes_it():
    pts = days_of(D(2024, 2, 1), 2, GL)
    r = h.query_trips(ledger(pts), since=None, until=None, exclude={"DK"}, territories="sovereign")
    assert r.sections[0].rows == []


def test_undated_visit_in_window_is_a_check_row():
    lg = ledger(visits=[visit(IR, None, None, 42)])
    r = h.query_trips(lg, since=D(2016, 1, 1), until=D(2026, 1, 1))
    [row] = r.sections[0].rows
    assert row["entry"] == "CHECK" and "#42" in row["flags"]


def test_year_only_visit_outside_window_is_left_out():
    lg = ledger(visits=[visit(IR, YearOnly(2010), None)])
    assert h.query_trips(lg, since=D(2016, 1, 1), until=D(2026, 1, 1)).sections[0].rows == []


def test_undated_visit_to_listed_country_is_kept_alongside():
    lg = ledger(days_of(D(2020, 1, 1), 2, ES), [visit(ES, None, None, 5)])
    r = h.query_trips(lg, since=None, until=None)
    assert len(r.sections[0].rows) == 1
    assert any("#5" in row["visit"] for row in r.sections[1].rows)


def test_airborne_only_country_goes_to_the_overflight_list():
    on_ground = pt(D(2024, 3, 1), TR1, "photos", dt.datetime(2024, 3, 1, 8), 41.0, 29.0)
    r = h.query_trips(ledger([on_ground, *_flight(D(2024, 3, 1))]), since=None, until=None)
    assert [row["iso"] for row in r.sections[0].rows] == ["TR"]
    air = next(s for s in r.sections if "overflight" in s.title)
    assert [row["iso"] for row in air.rows] == ["IR"]


def test_trip_straddling_the_window_is_reported_and_flagged():
    pts = days_of(D(2015, 12, 28), 10, TR1)
    [row] = h.query_trips(ledger(pts), since=D(2016, 1, 1), until=None).sections[0].rows
    assert row["entry"].startswith("2015-12-28") and "before the window" in row["flags"]


def test_sources_disagreeing_are_reported():
    pts = days_of(D(2024, 1, 2), 3, ES, "photos")
    lg = ledger(pts, [visit(ES, D(2024, 1, 1), D(2024, 1, 4))])
    [t] = h.trips(lg)
    assert t.agreement() == "disagree"


def test_empty_window_says_so_with_coverage():
    lg = ledger(days_of(D(2024, 1, 1), 2, ES))
    r = h.query_trips(lg, since=D(2010, 1, 1), until=D(2011, 1, 1))
    assert "Nothing in the window" in r.notes[0] and "2024-01-01" in r.notes[0]


# -------------------------------------------------------------------- check


def test_check_never_says_a_bare_no():
    lg = ledger(days_of(D(2024, 1, 1), 2, ES))
    r = h.query_check(lg, countries={"IR", "ES"}, since=D(2011, 3, 1), until=None)
    rows = {row["iso"]: row for row in r.sections[0].rows}
    assert rows["ES"]["answer"] == "YES"
    assert rows["IR"]["answer"] == "no evidence" and "covering" in rows["IR"]["evidence"]


def test_check_overflight_or_undated_is_check():
    lg = ledger(_flight(D(2024, 3, 1)), [visit(GR, None, None)])
    r = h.query_check(lg, countries={"IR", "GR"}, since=None, until=None)
    assert {row["answer"] for row in r.sections[0].rows} == {"CHECK"}


# --------------------------------------------------------------------- days


def test_uk_tax_year_boundaries():
    assert h.tax_year_bounds(2025, "uk")[:2] == (D(2025, 4, 6), D(2026, 4, 5))
    pts = [pt(D(2025, 4, 5), ES), pt(D(2025, 4, 6), ES)]
    r = h.query_days(ledger(pts), year=2025, scheme="uk", today=D(2026, 10, 1))
    [row] = r.sections[0].rows
    assert row["present"] == 1


def test_two_country_day_counts_in_both():
    day = D(2025, 6, 1)
    r = h.query_days(ledger([pt(day, ES), pt(day, PT)]), year=2025, today=D(2026, 1, 1))
    assert {row["iso"]: row["present"] for row in r.sections[0].rows} == {"ES": 1, "PT": 1}


def test_near_183_warns_about_no_data_days():
    pts = days_of(D(2025, 1, 1), 180, ES)
    r = h.query_days(ledger(pts), year=2025, today=D(2026, 1, 1))
    [row] = r.sections[0].rows
    assert "could reach 183" in row["vs 183"]


def test_days_fold_territories_by_default():
    pts = days_of(D(2025, 1, 1), 3, GL)
    r = h.query_days(ledger(pts), year=2025, today=D(2026, 1, 1))
    assert r.sections[0].rows[0]["iso"] == "DK"


# ----------------------------------------------------------------- absences


def test_absence_with_no_data_inside_is_split_into_columns():
    pts = (
        days_of(D(2025, 1, 1), 2, ES)
        + days_of(D(2025, 1, 3), 3, PT)
        + days_of(D(2025, 1, 10), 2, ES)
    )
    r = h.query_absences(
        ledger(pts, bridge=0), home={"ES"}, since=D(2025, 1, 1), until=D(2025, 1, 11)
    )
    [row] = r.sections[0].rows
    assert (row["away"], row["no data"]) == (3, 4)


def test_no_data_alone_is_not_an_absence():
    pts = days_of(D(2025, 1, 1), 2, ES) + days_of(D(2025, 1, 20), 2, ES)
    r = h.query_absences(
        ledger(pts, bridge=0), home={"ES"}, since=D(2025, 1, 1), until=D(2025, 1, 21)
    )
    assert r.sections[0].rows == []
    nodata = next(s for s in r.sections if "no data" in s.title)
    assert nodata.rows[0]["days"] == 17


def test_rolling_max():
    days = [D(2024, 1, 1) + dt.timedelta(days=i) for i in range(10)] + [D(2025, 6, 1)]
    assert h._max_rolling(days, 365) == 10


# -------------------------------------------------------------------- where


def test_where_collapses_runs_and_shows_no_data():
    pts = days_of(D(2024, 3, 1), 3, TR1) + days_of(D(2024, 3, 20), 2, GR)
    r = h.query_where(ledger(pts, bridge=0), since=D(2024, 3, 1), until=D(2024, 3, 21))
    rows = r.sections[0].rows
    assert [row["days"] for row in rows] == [3, 16, 2]
    assert rows[1]["where"] == "no data"
    assert "Istanbul" in rows[0]["where"]


def test_where_by_country():
    pts = [pt(D(2024, 3, 1), TR1), pt(D(2024, 3, 2), TR2)]
    r = h.query_where(ledger(pts), since=D(2024, 3, 1), until=D(2024, 3, 2), by="country")
    assert len(r.sections[0].rows) == 1


def test_where_lists_bridged_days_as_bridged():
    pts = [pt(D(2024, 3, 1), TR1), pt(D(2024, 3, 3), TR1)]
    r = h.query_where(ledger(pts), since=D(2024, 3, 1), until=D(2024, 3, 3))
    assert "bridged" in r.sections[0].rows[1]["where"]


# -------------------------------------------------------------------- dates


def test_relative_dates():
    today = D(2026, 10, 6)
    assert h.parse_when("10y", today) == D(2016, 10, 6)
    assert h.parse_when("1y", D(2024, 2, 29)) == D(2023, 2, 28)
    assert h.parse_period("2024-02", today) == (D(2024, 2, 1), D(2024, 2, 29))
    assert h.parse_period("2024-03-01..2024-04-15", today) == (D(2024, 3, 1), D(2024, 4, 15))


def test_since_takes_a_bare_year_as_the_spec_says():
    # `history list --since 2016` is in the design spec and died on fromisoformat.
    today = D(2026, 10, 6)
    assert h.parse_when("2016", today) == D(2016, 1, 1)
    assert h.parse_when("2016", today, end=True) == D(2016, 12, 31)
    assert h.parse_when("2024-02", today, end=True) == D(2024, 2, 29)
    # either end of a range, and the relative forms are not swallowed
    assert h.parse_period("2016..2017", today) == (D(2016, 1, 1), D(2017, 12, 31))
    assert h.parse_period("2024-03..2024-06", today) == (D(2024, 3, 1), D(2024, 6, 30))
    assert h.parse_when("90d", today) == D(2026, 7, 8)
    assert h.parse_when("1m", today) == D(2026, 9, 6)


def test_render_markdown_has_header_and_notes():
    lg = ledger(days_of(D(2024, 1, 1), 2, ES))
    hdr = h.header(
        lg,
        command="wanderfill history trips",
        since=None,
        until=None,
        territories="separate",
        today=D(2026, 1, 1),
    )
    md = h.render_markdown(h.query_trips(lg, since=None, until=None, hdr=hdr))
    assert "**command:**" in md and "Read this before copying" in md and "| Spain |" in md


# ---------------------------------------------------------------------- CLI


def test_cli_end_to_end_reads_only(tmp_path, monkeypatch):
    """The whole command against a fake server: nothing but reads go out."""
    from test_quirks import FakeTransport

    from wanderfill.api.client import NomadMania
    from wanderfill.cli import main as cli

    track = tmp_path / "track.csv"
    track.write_text("date,lat,lon\n2024-03-01,41.0,29.0\n2024-03-02,41.0,29.0\n")
    replies = {
        "regions/get-regions-list-2": {
            "data": {"1": {"name": "Istanbul"}, "4": {"name": "Madrid"}},
        },
        "quickEnter/get-regions": {
            "data": {
                "regions": [
                    {"id": 1, "flag1": "tr"},
                    {"id": 4, "flag1": "es"},
                ]
            }
        },
        "slow/get-slow-app": {
            "slow": [
                {"country": "Turkey", "flag": "tr", "country_id": 1},
                {"country": "Spain", "flag": "es", "country_id": 2},
            ]
        },
        "maps/get-visited-regions-ids-simple": {"ids": [4]},
        "quickEnter/get-visits-to-region": {
            "data": [
                {
                    "id": 9,
                    "year_from": 2019,
                    "month_from": None,
                    "day_from": None,
                    "year_to": None,
                    "month_to": None,
                    "day_to": None,
                    "quality": 3,
                },
            ]
        },
    }
    t = FakeTransport(replies)
    monkeypatch.setattr(cli, "_client", lambda args: NomadMania(token="fake", transport=t))
    monkeypatch.setattr(
        cli,
        "_resolve_coords",
        lambda args, cat, coords: {k: type("R", (), {"region": 1})() for k in coords},
    )
    out = tmp_path / "out"
    rc = cli.main(
        [
            "history",
            "trips",
            "--track",
            str(track),
            "--no-photos",
            "--since",
            "2016-01-01",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    md = (out / "history-trips.md").read_text()
    assert "Türkiye" in md and "2024-03-01" in md
    assert "CHECK" in md and "some time in 2019" in md
    sent = {a for a, _ in t.sent}
    assert sent <= {
        "regions/get-regions-list-2",
        "quickEnter/get-regions",
        "slow/get-slow-app",
        "maps/get-visited-regions-ids-simple",
        "quickEnter/get-visits-to-region",
    }


def test_cli_tax_year_choices_match_the_table():
    from wanderfill.cli.main import TAX_YEAR_CHOICES

    assert set(TAX_YEAR_CHOICES) == set(h.TAX_YEARS)


def test_cli_rejects_unknown_group_before_any_request(monkeypatch):
    from wanderfill.cli import main as cli

    monkeypatch.setattr(cli, "_client", lambda args: pytest.fail("no request expected"))
    with pytest.raises(SystemExit, match="unknown country or group"):
        cli.main(["history", "trips", "--exclude", "eea,schengn"])


# ------------------------------------------------- the second review's holes


def test_csv_carries_check_rows_overflights_and_notes():
    on_ground = pt(D(2024, 3, 1), TR1, "photos", dt.datetime(2024, 3, 1, 8), 41.0, 29.0)
    lg = ledger([on_ground, *_flight(D(2024, 3, 1))], [visit(ES, None, None, 42)])
    out = h.render_csv(h.query_trips(lg, since=None, until=None))
    assert "#42" in out and "overflight" in out and "note" in out


def test_days_lists_undated_claims_and_overflights_beside_the_count():
    lg = ledger(visits=[visit(ES, YearOnly(2024), None, 11)])
    r = h.query_days(lg, year=2024, today=D(2026, 1, 1))
    assert r.sections[0].rows == []
    assert any("#11" in row["visit"] for s in r.sections[1:] for row in s.rows)


def test_absences_lists_undated_claims_abroad():
    lg = ledger(days_of(D(2024, 1, 1), 2, PT), [visit(ES, YearOnly(2024), None, 12)])
    r = h.query_absences(lg, home={"PT"}, since=D(2024, 1, 1), until=D(2024, 12, 31))
    assert any("#12" in str(row) for s in r.sections for row in s.rows)


def test_vwp_note_has_one_exception_and_it_is_cuba():
    note = GROUPS["vwp-restricted"].note
    assert "2011-03-01" in note and "Cuba" in note and "2021-01-12" in note
    assert "North Korea" not in note


def test_far_relative_dates_raise_rather_than_hang():
    with pytest.raises(ValueError):
        h.parse_when("2026y", D(2026, 10, 6))
    assert h.parse_when("1m", D(2026, 3, 31)) == D(2026, 2, 28)


def test_check_honours_exclude():
    lg = ledger(days_of(D(2024, 1, 1), 2, IR))
    r = h.query_check(lg, countries={"IR", "IQ"}, exclude={"IR"}, since=None, until=None)
    assert [row["iso"] for row in r.sections[0].rows] == ["IQ"]
