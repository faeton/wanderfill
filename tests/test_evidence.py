"""Tests for the dossier: the failure modes here are reputational.

Getting the importer wrong adds a visit you can delete. Getting the dossier
wrong sends somebody into a verification with five photos of one square and a
belief that they are covered — or, worse, with a photo taken across a border
offered as proof of the region on this side of it.
"""

from __future__ import annotations

import datetime as dt

from wanderfill.evidence import (
    MIN_SERIAL_SHOTS,
    Shot,
    build,
    choose_exhibits,
    collect_documents,
    diameter_km,
    gradable,
    grade,
    render_applescript,
    render_json,
    render_markdown,
)
from wanderfill.geo.tiles import haversine_km
from wanderfill.sources.photos_app import MINE, PhotoAsset


def asset(day: str, lat: float, lon: float, uuid: str = "", **kw) -> PhotoAsset:
    at = kw.pop("at", "12:00:00")
    return PhotoAsset(
        uuid=uuid or f"{day}-{lat}-{lon}",
        taken=dt.datetime.fromisoformat(f"{day}T{at}"),
        lat=lat,
        lon=lon,
        directory="0",
        filename="x.heic",
        original_name=kw.pop("name", "IMG_0001.HEIC"),
        **kw,
    )


def shot(day: str, lat: float, lon: float, **kw) -> Shot:
    near = kw.pop("near_boundary", False)
    return Shot(asset=asset(day, lat, lon, **kw), region=1, near_boundary=near)


# ------------------------------------------------------------------ grading


def test_one_spot_one_day_is_never_serial():
    """Thirty photos of one dinner are one photo. NomadMania asks for serial."""
    shots = [shot("2024-05-01", 50.0, 14.0, uuid=str(i)) for i in range(30)]
    assert grade(shots) == "thin"


def test_serial_needs_movement_not_just_days():
    """Same balcony, three days running, still proves one balcony."""
    shots = [shot(f"2024-05-0{d}", 50.0, 14.0, uuid=str(d)) for d in (1, 2, 3)]
    assert grade(shots) == "thin"


def test_serial_photos_across_days_and_kilometres():
    shots = [
        shot("2024-05-01", 50.00, 14.00, uuid="a"),
        shot("2024-05-02", 50.05, 14.05, uuid="b"),
        shot("2024-05-03", 50.10, 14.10, uuid="c"),
    ]
    assert grade(shots) == "serial"
    assert len(shots) >= MIN_SERIAL_SHOTS


def test_a_day_trip_across_a_region_is_serial():
    """Twelve photos over twenty kilometres and a whole day is a visit.

    Requiring two days would grade most of the hard regions — the ones visited
    once, on a long drive — the same as a single frame from a car window.
    """
    shots = [
        shot(
            "2026-06-03", 41.08 + i / 100, 44.65 + i / 100,
            uuid=f"day{i}", at=f"{9 + i // 2:02d}:00:00",
        )
        for i in range(12)
    ]
    assert grade(shots) == "serial"


def test_a_drive_through_is_not_a_day_trip():
    """Three frames twenty minutes apart on a motorway is a transit.

    Same distance, same spot count as the day trip above; the only difference
    is the clock, and the clock is what a verifier reads.
    """
    shots = [
        shot("2026-06-03", 41.08, 44.65, uuid="a", at="14:00:00"),
        shot("2026-06-03", 41.12, 44.70, uuid="b", at="14:10:00"),
        shot("2026-06-03", 41.16, 44.75, uuid="c", at="14:20:00"),
    ]
    assert grade(shots) == "thin"


def test_a_region_too_small_to_cross_can_still_be_serial():
    """The Vatican is 500 m wide. No stay produces a kilometre of movement."""
    shots = [
        shot("2024-05-01", 41.9022, 12.4539, uuid="basilica"),
        shot("2024-05-02", 41.9065, 12.4536, uuid="museums"),
        shot("2024-05-02", 41.9064, 12.4534, uuid="museums2"),
    ]
    assert grade(shots, small=True) == "serial"


def test_the_same_photos_in_a_large_region_are_a_hotel():
    """Two days in one Nairobi hotel looks identical in the data.

    Which is why the exemption is granted by measuring the region, never by
    noticing that the photos are clustered. This case is the reason: it was
    briefly graded `strong` off five photos 300 m apart.
    """
    shots = [
        shot("2024-05-01", -1.2921, 36.8219, uuid="hotel1"),
        shot("2024-05-02", -1.2945, 36.8240, uuid="hotel2"),
        shot("2024-05-02", -1.2946, 36.8241, uuid="hotel3"),
    ]
    assert grade(shots, small=False) == "thin"


def test_one_balcony_for_three_days_is_still_thin():
    """The small-region route must not become a loophole for staying put."""
    shots = [shot(f"2024-05-0{d}", 41.9022, 12.4539, uuid=f"b{d}") for d in (1, 2, 3)]
    assert grade(shots, small=True) == "thin"


def test_a_selfie_is_what_lifts_serial_to_strong():
    """Their Class 1 list ranks a selfie above an ordinary photo. So does this."""
    shots = [
        shot("2024-05-01", 50.00, 14.00, uuid="a", selfie=True),
        shot("2024-05-02", 50.05, 14.05, uuid="b"),
        shot("2024-05-03", 50.10, 14.10, uuid="c"),
    ]
    assert grade(shots) == "strong"


def test_no_photos_is_none_not_thin():
    assert grade([]) == "none"


# ------------------------------------------------------------------ exhibits


def test_exhibits_spread_out_instead_of_taking_the_first_five():
    """A shortlist of five consecutive frames proves five seconds."""
    burst = [shot("2024-05-01", 50.0, 14.0, uuid=f"burst{i}") for i in range(10)]
    far = [
        shot("2024-05-02", 50.4, 14.4, uuid="far-a"),
        shot("2024-05-03", 50.8, 14.8, uuid="far-b"),
    ]
    picked = choose_exhibits(burst + far, limit=4)
    uuids = {s.asset.uuid for s in picked}
    assert "far-a" in uuids and "far-b" in uuids
    assert len({s.date for s in picked}) == 3


def test_exhibit_choice_is_deterministic():
    shots = [shot(f"2024-05-0{d}", 50.0 + d / 10, 14.0, uuid=f"u{d}") for d in (1, 2, 3, 4)]
    assert choose_exhibits(shots, 3) == choose_exhibits(list(reversed(shots)), 3)


def test_a_selfie_anchors_the_shortlist():
    shots = [
        shot("2024-05-01", 50.0, 14.0, uuid="plain"),
        shot("2024-05-02", 50.5, 14.5, uuid="me", selfie=True),
    ]
    assert any(s.asset.selfie for s in choose_exhibits(shots, 1))


# -------------------------------------------------------------- the dossier


def _dossier(**kw):
    shots = [
        shot("2024-05-01", 50.00, 14.00, uuid="a", selfie=True),
        shot("2024-05-02", 50.05, 14.05, uuid="b"),
        shot("2024-05-03", 50.10, 14.10, uuid="c"),
    ]
    return build(
        {1: shots},
        {1: {"name": "Prague"}, 2: {"name": "Somewhere Unphotographed"}},
        {1, 2},
        account=79597,
        library=kw.pop("library", "/nowhere"),
        **kw,
    )


def test_a_claimed_region_with_no_photos_still_appears():
    """The empty entries are the finding, not the omission."""
    d = _dossier()
    names = {r.name: r.grade for r in d.regions}
    assert names["Somewhere Unphotographed"] == "none"
    assert d.counts()["none"] == 1


def test_photographed_but_unclaimed_is_reported_not_claimed():
    """The audit runs both ways — and this file still writes nothing anywhere."""
    d = build(
        {7: [shot("2024-05-01", 1.0, 1.0, uuid="z")]},
        {7: {"name": "Unlisted"}},
        set(),
        account=1,
        library="/nowhere",
    )
    assert [r.name for r in d.unclaimed_with_evidence] == ["Unlisted"]
    assert d.counts()["none"] == 0  # it is not claimed, so it is not a gap


def test_dates_that_disagree_are_flagged():
    """A region claimed in the wrong year reads as guessing, and they look for it."""
    d = build(
        {1: [shot("2024-05-01", 50.0, 14.0, uuid="a")]},
        {1: {"name": "Prague"}},
        {1},
        account=1,
        library="/nowhere",
        visits={1: [(dt.date(2019, 1, 1), dt.date(2019, 1, 8))]},
    )
    assert d.regions[0].dates_disagree


def test_dates_that_agree_are_not_flagged():
    d = build(
        {1: [shot("2024-05-01", 50.0, 14.0, uuid="a")]},
        {1: {"name": "Prague"}},
        {1},
        account=1,
        library="/nowhere",
        visits={1: [(dt.date(2024, 4, 28), dt.date(2024, 5, 3))]},
    )
    assert not d.regions[0].dates_disagree


def test_the_projection_offers_no_verdict():
    """Half the regions provable means about half of any sixty they draw.

    And no boolean: printing "ready" would make a promise on the committee's
    behalf, which is precisely how somebody stops collecting evidence early.
    """
    shots = {
        i: [
            shot("2024-05-01", 50.0 + i, 14.0, uuid=f"{i}a", selfie=True),
            shot("2024-05-02", 50.1 + i, 14.1, uuid=f"{i}b"),
            shot("2024-05-03", 50.2 + i, 14.2, uuid=f"{i}c"),
        ]
        for i in range(5)
    }
    d = build(
        shots,
        {i: {"name": f"R{i}"} for i in range(10)},
        set(range(10)),
        account=1,
        library="/nowhere",
    )
    p = d.projection()
    assert p["even"] == 30
    assert "ready" not in p
    # The five regions with nothing are the thinnest half, so a leaning draw
    # lands in them: the pessimistic number must be worse than the even one.
    assert p["weighted"] < p["even"]
    assert "does not offer one" in render_markdown(d)


def test_a_region_whose_photos_argue_with_the_profile_counts_for_neither():
    """A photo set that contradicts your own record is not an answer."""
    good = [
        shot("2024-05-01", 50.0, 14.0, uuid="a", selfie=True),
        shot("2024-05-02", 50.1, 14.1, uuid="b"),
        shot("2024-05-03", 50.2, 14.2, uuid="c"),
    ]
    d = build(
        {1: good},
        {1: {"name": "Mislabelled"}},
        {1},
        account=1,
        library="/nowhere",
        visits={1: [(dt.date(2019, 1, 1), dt.date(2019, 1, 8))]},
    )
    p = d.projection()
    assert d.regions[0].grade == "strong"
    assert p["provable"] == 0
    assert p["excluded_date_conflict"] == 1


# ------------------------------------------------------------- the artefacts


def test_applescript_uses_the_id_format_photos_actually_answers_to():
    """A bare UUID raises -1728 against a real library. Verified the hard way."""
    d = _dossier()
    script = render_applescript(d, "/tmp/out")
    assert "/L0/001" in script
    assert "using originals" in script


def test_markdown_names_files_not_just_regions():
    """'You were in Prague' is not evidence. A filename and a date is."""
    text = render_markdown(_dossier())
    assert "IMG_0001.HEIC" in text
    assert "selfie" in text
    assert "Somewhere Unphotographed" in text


def test_json_carries_the_boundary_warning_through():
    """A nearest-polygon hit must never reach an email looking like a fact."""
    s = shot("2024-05-01", 50.0, 14.0, uuid="edge", near_boundary=True)
    d = build({1: [s]}, {1: {"name": "Edge"}}, {1}, account=1, library="/nowhere")
    exhibit = render_json(d)["regions"][0]["exhibits"][0]
    assert exhibit["near_boundary"] is True


def test_filed_documents_are_indexed_but_never_graded(tmp_path):
    """A file named hotel.pdf could be anything. Scoring it would invent proof."""
    folder = tmp_path / "0292-iceland-south"
    folder.mkdir()
    (folder / "hotel-reykjavik.pdf").write_text("x")
    (folder / ".DS_Store").write_text("x")
    docs = collect_documents(tmp_path)
    assert docs == {292: ["0292-iceland-south/hotel-reykjavik.pdf"]}

    d = build({}, {292: {"name": "Iceland"}}, {292}, account=1, library="/nowhere",
              documents=docs)
    assert d.regions[0].grade == "none"  # photos are what this can grade
    assert "hotel-reykjavik.pdf" in render_markdown(d)


def test_the_source_excludes_photos_you_did_not_take():
    """Syndicated assets are somebody else's travel, geotagged with their trip."""
    assert "ZSAVEDASSETTYPE" in MINE
    assert "ZKINDSUBTYPE = 10" in MINE


def test_the_equator_is_not_a_missing_location():
    """Latitude zero runs through eight countries somebody may be claiming."""
    assert "a.ZLATITUDE != 0" not in MINE
    assert "NOT (a.ZLATITUDE = 0 AND a.ZLONGITUDE = 0)" in MINE


# ------------------------------------------- what must not inflate a grade


def test_photos_outside_every_polygon_cannot_earn_a_grade():
    """Nearest-polygon means the wrong side of a border is in play."""
    shots = [
        shot("2024-05-01", 50.00, 14.00, uuid="a", near_boundary=True),
        shot("2024-05-02", 50.05, 14.05, uuid="b", near_boundary=True),
        shot("2024-05-03", 50.10, 14.10, uuid="c", near_boundary=True),
    ]
    assert grade(shots) == "thin"
    assert gradable(shots) == []


def test_a_boundary_selfie_does_not_lift_a_region_to_strong():
    inside = [
        shot("2024-05-01", 50.00, 14.00, uuid="a"),
        shot("2024-05-02", 50.05, 14.05, uuid="b"),
        shot("2024-05-03", 50.10, 14.10, uuid="c"),
    ]
    outside = shot("2024-05-03", 50.11, 14.11, uuid="edge", selfie=True, near_boundary=True)
    assert grade([*inside, outside]) == "serial"


def test_spread_is_a_real_distance_not_a_bounding_box_corner():
    """The bounding box has a corner no photo was taken at, and it is further."""
    shots = [
        shot("2024-05-01", 50.00, 14.00, uuid="s"),
        shot("2024-05-01", 50.10, 14.00, uuid="n"),
        shot("2024-05-02", 50.05, 14.05, uuid="mid"),
    ]
    real = diameter_km(shots)
    bbox = haversine_km(50.00, 14.00, 50.10, 14.05)
    assert real < bbox


def test_a_sampled_diameter_never_overstates():
    shots = [shot("2024-05-01", 50.0 + i / 1000, 14.0, uuid=f"u{i}") for i in range(500)]
    assert diameter_km(shots, cap=50) <= diameter_km(shots, cap=1000) + 1e-9


def test_no_exhibits_means_no_exhibits():
    assert choose_exhibits([shot("2024-05-01", 1.0, 1.0, uuid="a")], limit=0) == []


def test_a_second_untold_visit_shows_up_as_days_outside():
    """Partial disagreement is the common case and the easiest one to hide."""
    d = build(
        {
            1: [
                shot("2019-01-03", 50.0, 14.0, uuid="recorded"),
                shot("2024-05-01", 50.1, 14.1, uuid="unrecorded"),
            ]
        },
        {1: {"name": "Prague"}},
        {1},
        account=1,
        library="/nowhere",
        visits={1: [(dt.date(2019, 1, 1), dt.date(2019, 1, 8))]},
    )
    entry = d.regions[0]
    assert entry.days_outside == [dt.date(2024, 5, 1)]
    assert not entry.dates_disagree  # partial, not total
    text = render_markdown(d)
    assert "1 of 2" in text
    assert "No region's photos contradict its recorded dates outright." in text
