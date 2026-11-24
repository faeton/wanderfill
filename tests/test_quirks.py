"""Tests for the quirks that cost real damage the first time round.

These are not tests of the server. They are tests that this package cannot make
the same mistakes again, and each one names the incident it prevents.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from wanderfill.api.client import NomadMania, Visit, YearOnly
from wanderfill.api.errors import ApiError, DriftError, PrecisionLoss, VerificationFailed
from wanderfill.api.transport import Transport
from wanderfill.plan.model import Op, Plan
from wanderfill.plan.segment import (
    MULTI_REGION_FLOOR,
    HomeWindow,
    Journey,
    compare_homes,
    multi_region_share,
    segment,
    split_first_and_repeat,
)
from wanderfill.sources.base import DayPoint, Track
from wanderfill.sources.normalize import components, fold

# What the server was recorded answering, where it is more than ``result: OK``.
ANSWERS = {"quickEnter/add-visit": {"result": "OK", "visit_id": 77},
           "trips/new-trip": {"result": "OK", "trip_id": 78}}


class FakeTransport:
    """Records what would have been sent, answers with whatever is queued."""

    def __init__(self, replies=None):
        self.sent = []
        self.replies = replies or {}
        self.token = "fake"
        self.lang = "en"

    def webapi(self, action, **fields):
        self.sent.append((action, fields))
        reply = self.replies.get(action, ANSWERS.get(action, {"result": "OK"}))
        if isinstance(reply, dict) and reply.get("result") == "ERROR":
            raise ApiError(action, reply.get("result_description", ""), reply)
        return reply

    def ajax_json(self, path, **fields):
        self.sent.append((path, fields))
        return self.replies.get(path, {})

    def ajax_text(self, path, **fields):
        self.sent.append((path, fields))
        return self.replies.get(path, "OK")


def client(replies=None):
    t = FakeTransport(replies)
    return NomadMania(token="fake", transport=t), t


@pytest.fixture(autouse=True)
def _no_home_directory(monkeypatch, tmp_path):
    """The CLI always scans and locks the default workdir: never the real one here."""
    from wanderfill.cli import main as cli

    monkeypatch.setattr(cli, "DEFAULT_WORKDIR", tmp_path)


# --------------------------------------------------------------------------
# trip creation: the server demands a field it never reads
# --------------------------------------------------------------------------

def test_create_trip_sends_both_region_fields():
    """Sending only regions_json fails with 'Missing params: regions.'"""
    c, t = client()
    regions = [{"id": 1, "quality": 3}, {"id": 2, "quality": 3}]
    c.create_trip(dt.date(2024, 1, 1), dt.date(2024, 1, 5), regions)
    _, fields = t.sent[0]
    assert "regions" in fields, "the server checks this key exists before reading regions_json"
    assert "regions_json" in fields
    assert fields["regions"] == "[object Object],[object Object]"
    assert json.loads(fields["regions_json"]) == regions


# --------------------------------------------------------------------------
# update-visit: a full replacement, not a patch
# --------------------------------------------------------------------------

def test_update_visit_requires_quality():
    """Omitting quality once turned a 'lived here' into a 'good visit'."""
    c, _ = client()
    with pytest.raises(TypeError):
        c.update_visit(1, 2, dt.date(2024, 1, 1), dt.date(2024, 1, 2))  # no quality


def _one_visit(visit_id=11, **over):
    """The reply shape ``update_visit`` reads back before it agrees to write."""
    row = {"id": visit_id, "quality": 5, "trip_id": None,
           "year_from": 2024, "month_from": 1, "day_from": 1,
           "year_to": 2024, "month_to": 1, "day_to": 2}
    row.update(over)
    return {"quickEnter/get-visits-to-region": {"result": "OK", "data": [row]}}


def test_update_visit_passes_quality_through():
    c, t = client(_one_visit())
    c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=5)
    _, fields = t.sent[-1]
    assert fields["quality"] == 5


# --------------------------------------------------------------------------
# a visit can be precise, year-only, or undated — and a replacement can
# quietly destroy the difference
# --------------------------------------------------------------------------

def test_year_only_visit_is_not_read_as_undated():
    """The 13-in-892 shape. Reading it as ``None`` erases a year on write-back."""
    v = Visit.from_api(
        {"id": 1, "quality": 3, "trip_id": None,
         "year_from": 2013, "month_from": None, "day_from": None,
         "year_to": 2013, "month_to": None, "day_to": None},
        region=658,
    )
    assert v.date_from == YearOnly(2013)
    assert v.date_to == YearOnly(2013)


def test_year_only_sends_empty_month_and_day():
    """Empty, not omitted: an absent key is not 'unchanged' on a full replacement."""
    c, t = client(_one_visit(year_from=None, month_from=None, day_from=None,
                             year_to=None, month_to=None, day_to=None))
    c.update_visit(11, 22, YearOnly(2013), YearOnly(2013), quality=3)
    _, fields = t.sent[-1]
    assert fields["year_from"] == 2013
    assert fields["month_from"] == "" and fields["day_from"] == ""


def test_update_visit_refuses_to_blur_a_precise_date():
    """Writing a bare year over a full date deletes the month and day."""
    c, _ = client(_one_visit())
    with pytest.raises(PrecisionLoss, match="losing precision"):
        c.update_visit(11, 22, YearOnly(2024), YearOnly(2024), quality=5)


def test_update_visit_refuses_to_erase_dates_entirely():
    c, _ = client(_one_visit())
    with pytest.raises(PrecisionLoss):
        c.update_visit(11, 22, None, None, quality=5)


def test_update_visit_allows_deliberate_blurring():
    c, t = client(_one_visit())
    c.update_visit(11, 22, YearOnly(2024), YearOnly(2024), quality=5, allow_vaguer=True)
    _, fields = t.sent[-1]
    assert fields["month_from"] == ""


def test_update_visit_allows_sharpening_a_year_into_a_date():
    """The whole point of filling a profile in. Never blocked."""
    c, t = client(_one_visit(month_from=None, day_from=None, month_to=None, day_to=None))
    c.update_visit(11, 22, dt.date(2024, 3, 4), dt.date(2024, 3, 5), quality=3)
    _, fields = t.sent[-1]
    assert fields["month_from"] == 3 and fields["day_from"] == 4


def test_update_visit_refuses_to_write_to_a_visit_that_is_not_there():
    """A wrong region id would otherwise replace some other region's record."""
    c, _ = client(_one_visit(visit_id=999))
    with pytest.raises(PrecisionLoss, match="refusing to write blind"):
        c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=3)


# --------------------------------------------------------------------------
# geocoding must never broadcast a live location
# --------------------------------------------------------------------------

def test_region_at_always_sends_share_zero():
    """Without share=0 this endpoint publishes the coordinate as a live location."""
    c, t = client({"location/get-region": {"result": "OK", "nm": {"id": 29}}})
    c.region_at(41.89, 12.49)
    _, fields = t.sent[0]
    assert fields["share"] == 0


# --------------------------------------------------------------------------
# dare is never unset
# --------------------------------------------------------------------------

def test_mark_dare_never_sends_zero():
    c, t = client()
    c.mark_dare(1142)
    _, fields = t.sent[0]
    assert fields["visits"] == 1


# --------------------------------------------------------------------------
# the one delete
# --------------------------------------------------------------------------

UNDO_METHODS = {"delete_visit", "delete_trip", "clear_region", "unmark_dare", "unmark_kye"}


def test_the_undo_methods_are_exactly_these_five():
    """v1 had no delete path at all. There are now five, each by one id.

    Anything else that removes — bulk un-marking, series un-ticking — stays
    absent. The class may undo a single thing the owner has named, and nothing
    else, and none of the five is a plan op.
    """
    names = [n for n in dir(NomadMania) if not n.startswith("_")]
    found = {n for n in names if any(w in n for w in ("delete", "remove", "unmark", "clear"))}
    assert found == UNDO_METHODS


class SequencedTransport(FakeTransport):
    """Reads answer from per-action queues, so 'before' and 'after' can differ.

    ``sequences`` maps an action to the list of replies it gives in order; any
    other action gets the static reply from ``replies`` (default ``OK``).
    """

    def __init__(self, sequences, replies=None):
        super().__init__(replies)
        self.queues = {k: list(v) for k, v in sequences.items()}

    def webapi(self, action, **fields):
        if action in self.queues:
            self.sent.append((action, fields))
            reply = self.queues[action].pop(0)
            if isinstance(reply, dict) and reply.get("result") == "ERROR":
                raise ApiError(action, reply.get("result_description", ""), reply)
            return reply
        return super().webapi(action, **fields)


def visits(*rows):
    return {"result": "OK", "data": list(rows)}


def writes(t, action):
    return [(a, f) for a, f in t.sent if a == action]


ROW = {"id": 13450095, "quality": 3, "year_from": None, "month_from": None, "day_from": None,
       "year_to": None, "month_to": None, "day_to": None, "trip_id": None}
READ = "quickEnter/get-visits-to-region"


def test_delete_visit_sends_only_the_id_and_verifies():
    """The site sends {id}; OK is not evidence, so the region is read back."""
    t = SequencedTransport({READ: [visits(ROW), visits()]})
    c = NomadMania(token="fake", transport=t)
    gone = c.delete_visit(13450095, 791)
    assert gone.id == 13450095 and gone.region == 791 and gone.quality == 3
    assert writes(t, "quickEnter/delete-visit") == [("quickEnter/delete-visit", {"id": 13450095})]
    assert [a for a, _ in t.sent] == [READ, "quickEnter/delete-visit", READ]


def test_delete_visit_refuses_an_id_not_on_the_region():
    """A typo in the region must not delete a record elsewhere. Nothing is sent."""
    t = SequencedTransport({READ: [visits(ROW)]})
    c = NomadMania(token="fake", transport=t)
    with pytest.raises(PrecisionLoss):
        c.delete_visit(999, 791)
    assert not writes(t, "quickEnter/delete-visit")


def test_delete_visit_refuses_trip_owned_unless_told():
    """The site warns a trip-owned visit leaves the trip too; so does this."""
    owned = {**ROW, "trip_id": 55}
    t = SequencedTransport({READ: [visits(owned)]})
    c = NomadMania(token="fake", transport=t)
    with pytest.raises(PrecisionLoss):
        c.delete_visit(13450095, 791)
    assert not writes(t, "quickEnter/delete-visit")
    t = SequencedTransport({READ: [visits(owned), visits()]})
    c = NomadMania(token="fake", transport=t)
    assert c.delete_visit(13450095, 791, allow_trip_owned=True).trip_id == 55


def test_delete_visit_raises_when_ok_but_record_remains():
    """Both incidents on this profile returned OK and were wrong. Read back."""
    t = SequencedTransport({READ: [visits(ROW), visits(ROW)]})
    c = NomadMania(token="fake", transport=t)
    with pytest.raises(ApiError):
        c.delete_visit(13450095, 791)


def test_clear_region_sends_region_returns_all_visits_and_verifies():
    """set-not-visited takes the region and removes every visit on it."""
    second = {**ROW, "id": 2, "quality": 5}
    t = SequencedTransport({READ: [visits(ROW, second), visits()]})
    c = NomadMania(token="fake", transport=t)
    gone = c.clear_region(791)
    assert [v.id for v in gone] == [13450095, 2] and gone[1].quality == 5
    assert writes(t, "quickEnter/set-not-visited") == [
        ("quickEnter/set-not-visited", {"region": 791})
    ]


def test_clear_region_refuses_empty_and_trip_owned_and_leftovers():
    c = NomadMania(token="fake", transport=SequencedTransport({READ: [visits()]}))
    with pytest.raises(PrecisionLoss):
        c.clear_region(791)
    owned = {**ROW, "trip_id": 55}
    t = SequencedTransport({READ: [visits(ROW, owned)]})
    c = NomadMania(token="fake", transport=t)
    with pytest.raises(PrecisionLoss):
        c.clear_region(791)
    assert not writes(t, "quickEnter/set-not-visited")
    t = SequencedTransport({READ: [visits(ROW), visits(ROW)]})
    with pytest.raises(ApiError):
        NomadMania(token="fake", transport=t).clear_region(791)


TRIP = {"result": "OK", "trip": {"id": 305716, "date_from": "2024-12-27", "date_to": "2025-01-09",
                                 "description": "", "regions": [{"region": 211, "quality": 3}]}}
GONE = {"result": "ERROR", "result_description": "Unauthorised."}
YEAR = "trips/get-trips-for-year-app"


def test_delete_trip_reads_first_sends_trip_id_and_proves_absence_twice():
    """Success is get-trip failing with exactly 'Unauthorised.' AND the year's
    listing no longer carrying the id. The trip is returned for re-creation."""
    t = SequencedTransport({"trips/get-trip": [TRIP, GONE], YEAR: [{"trips": [{"id": 1}]}]})
    c = NomadMania(token="fake", transport=t)
    before = c.delete_trip(305716)
    assert before["regions"][0]["region"] == 211
    assert writes(t, "trips/delete-trip") == [("trips/delete-trip", {"trip_id": 305716})]
    assert writes(t, YEAR) == [(YEAR, {"year": 2024})]


def test_delete_trip_refuses_unknown_trip_and_a_trip_that_survives():
    """Unauthorised on the first read means not yours or not there: nothing sent."""
    t = SequencedTransport({"trips/get-trip": [GONE]})
    with pytest.raises(ApiError):
        NomadMania(token="fake", transport=t).delete_trip(1)
    assert not writes(t, "trips/delete-trip")
    t = SequencedTransport({"trips/get-trip": [TRIP, TRIP]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).delete_trip(305716)


def test_delete_trip_does_not_take_any_error_as_proof():
    """A database error on the read-back is not 'the trip is gone'. Codex found
    the first version accepting any ApiError here."""
    other = {"result": "ERROR", "result_description": "Internal error."}
    t = SequencedTransport({"trips/get-trip": [TRIP, other]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).delete_trip(305716)
    # 'Unauthorised.' alone is not enough either if the listing still has it
    t = SequencedTransport({"trips/get-trip": [TRIP, GONE], YEAR: [{"trips": [{"id": 305716}]}]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).delete_trip(305716)


def test_delete_trip_is_bound_to_the_previewed_regions():
    t = SequencedTransport({"trips/get-trip": [TRIP]})
    with pytest.raises(PrecisionLoss):
        NomadMania(token="fake", transport=t).delete_trip(305716, expect_regions=[211, 176])
    assert not writes(t, "trips/delete-trip")


def test_clear_region_is_bound_to_the_previewed_visit_ids():
    """What was confirmed is a set of records, not 'whatever is there now'."""
    second = {**ROW, "id": 2}
    t = SequencedTransport({READ: [visits(ROW, second)]})
    with pytest.raises(PrecisionLoss):
        NomadMania(token="fake", transport=t).clear_region(791, expect_ids=[13450095])
    assert not writes(t, "quickEnter/set-not-visited")


def test_undo_read_backs_reject_a_response_missing_the_list():
    """{"result":"OK"} with the field dropped must not verify a deletion."""
    bare = {"result": "OK"}
    t = SequencedTransport({READ: [visits(ROW), bare]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).delete_visit(13450095, 791)
    t = SequencedTransport({"maps/get-visited-dare-ids-simple": [{"ids": [1142]}, bare]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).unmark_dare(1142)
    t = SequencedTransport({"kye/get-kye": [{"visited": [570]}, bare]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).unmark_kye(570)
    # and before the write, too: a bare answer is not "no visits here"
    t = SequencedTransport({READ: [bare]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).clear_region(791)
    assert not writes(t, "quickEnter/set-not-visited")


DARE_READ = "maps/get-visited-dare-ids-simple"


def test_unmark_dare_sends_zero_only_for_a_marked_area_and_verifies():
    t = SequencedTransport({DARE_READ: [{"ids": [1142, 7]}, {"ids": [7]}]})
    c = NomadMania(token="fake", transport=t)
    c.unmark_dare(1142)
    assert writes(t, "quickEnter/updateMQP") == [
        ("quickEnter/updateMQP", {"region": 1142, "visits": 0})
    ]
    t = SequencedTransport({DARE_READ: [{"ids": [7]}]})
    with pytest.raises(PrecisionLoss):
        NomadMania(token="fake", transport=t).unmark_dare(1142)
    assert not writes(t, "quickEnter/updateMQP")
    t = SequencedTransport({DARE_READ: [{"ids": [1142]}, {"ids": [1142]}]})
    with pytest.raises(ApiError):
        NomadMania(token="fake", transport=t).unmark_dare(1142)


def test_unmark_kye_sends_zero_only_for_a_ticked_quadrant_and_verifies():
    t = SequencedTransport({"kye/get-kye": [{"visited": [112, 570]}, {"visited": [112]}]})
    c = NomadMania(token="fake", transport=t)
    c.unmark_kye(570)
    assert writes(t, "kye/set-kye") == [("kye/set-kye", {"qid": 570, "visited": 0})]
    t = SequencedTransport({"kye/get-kye": [{"visited": [112]}]})
    with pytest.raises(PrecisionLoss):
        NomadMania(token="fake", transport=t).unmark_kye(570)
    assert not writes(t, "kye/set-kye")
    t = SequencedTransport({"kye/get-kye": [{"visited": [570]}, {"visited": [570]}]})
    with pytest.raises(ApiError):
        NomadMania(token="fake", transport=t).unmark_kye(570)


def test_mark_methods_still_never_send_zero():
    """The undo lives in unmark_*; the mark methods stay one-directional."""
    c, t = client()
    c.mark_kye(570)
    c.mark_dare(1142)
    assert all(f.get("visited", f.get("visits")) == 1 for _, f in t.sent)


def test_deletions_are_not_plan_ops():
    """A plan is what the tool computed; a deletion is what the owner decided."""
    from typing import get_args

    from wanderfill.plan.model import OpKind

    assert not [k for k in get_args(OpKind) if "delete" in k or "clear" in k or "unmark" in k]


# --------------------------------------------------------------------------
# the delete command's journal discipline
# --------------------------------------------------------------------------

class FakeClient:
    """Just enough of NomadMania for cmd_delete: one DARE area, scripted outcome."""

    def __init__(self, outcome=None):
        self.outcome = outcome  # None = success, else an exception to raise
        self.calls = []

    def account_id(self):
        return 79597

    def visited_dare_ids(self):
        return {1142}

    def unmark_dare(self, dare_id):
        self.calls.append(dare_id)
        if self.outcome is not None:
            raise self.outcome


def _run_delete(monkeypatch, tmp_path, fake, *argv):
    from wanderfill.cli import main as cli

    monkeypatch.setattr(cli, "_client", lambda args: fake)
    return cli.main(["delete", *argv, "--workdir", str(tmp_path)])


def _journal_lines(tmp_path):
    files = list(tmp_path.glob("deletes-*.ndjson"))
    if not files:
        return []
    return [json.loads(x) for x in files[0].read_text().splitlines() if x.strip()]


def test_delete_dry_run_sends_nothing_and_writes_no_journal(monkeypatch, tmp_path):
    fake = FakeClient()
    assert _run_delete(monkeypatch, tmp_path, fake, "dare", "1142") == 0
    assert fake.calls == [] and _journal_lines(tmp_path) == []


def test_delete_confirm_needs_a_reason(monkeypatch, tmp_path):
    fake = FakeClient()
    assert _run_delete(monkeypatch, tmp_path, fake, "dare", "1142", "--confirm") == 2
    assert fake.calls == [] and _journal_lines(tmp_path) == []


def test_delete_confirm_journals_open_then_done_with_snapshot(monkeypatch, tmp_path):
    fake = FakeClient()
    rc = _run_delete(monkeypatch, tmp_path, fake, "dare", "1142", "--confirm", "--reason", "typo")
    assert rc == 0 and fake.calls == [1142]
    lines = _journal_lines(tmp_path)
    assert [e["phase"] for e in lines] == ["open", "done"]
    assert lines[0]["snapshot"] == {"dare": 1142, "was_marked": True}
    assert "typo" in lines[0]["label"] and lines[1]["error"] == ""


def test_delete_leaves_entry_open_on_failed_verification_and_lost_answer(monkeypatch, tmp_path):
    from wanderfill.api.errors import UnknownWriteOutcome

    for exc in (VerificationFailed("quickEnter/updateMQP", "still marked"),
                VerificationFailed("quickEnter/updateMQP", "read-back died"),
                UnknownWriteOutcome("no answer")):
        for f in tmp_path.glob("deletes-*"):
            f.unlink()
        fake = FakeClient(outcome=exc)
        rc = _run_delete(monkeypatch, tmp_path, fake, "dare", "1142", "--confirm", "--reason", "x")
        assert rc == 1
        assert [e["phase"] for e in _journal_lines(tmp_path)] == ["open"], type(exc).__name__


def test_delete_closes_entry_only_on_definite_refusals(monkeypatch, tmp_path):
    from wanderfill.api.errors import AuthError, TransportError

    for exc in (PrecisionLoss("not marked"), ApiError("quickEnter/updateMQP", "Missing params"),
                AuthError("server rejected the token (403)"), TransportError("refused: 400")):
        for f in tmp_path.glob("deletes-*"):
            f.unlink()
        fake = FakeClient(outcome=exc)
        rc = _run_delete(monkeypatch, tmp_path, fake, "dare", "1142", "--confirm", "--reason", "x")
        assert rc == 1
        lines = _journal_lines(tmp_path)
        assert [e["phase"] for e in lines] == ["open", "done"] and lines[1]["error"]


def test_any_open_entry_blocks_every_further_deletion(monkeypatch, tmp_path):
    """Not only the same key: a region cannot be cleared while one of its
    visits is in an unknown state."""
    from wanderfill.api.errors import UnknownWriteOutcome

    _run_delete(monkeypatch, tmp_path, FakeClient(outcome=UnknownWriteOutcome("lost")),
                "dare", "1142", "--confirm", "--reason", "x")
    fake = FakeClient()
    rc = _run_delete(monkeypatch, tmp_path, fake, "dare", "1142", "--confirm", "--reason", "x")
    assert rc == 2 and fake.calls == []


def test_journal_unresolved_is_chronological(tmp_path):
    """open → done → open is unresolved: the latest attempt has no answer."""
    from types import SimpleNamespace

    from wanderfill.plan.apply import Journal

    j = Journal(tmp_path / "j.ndjson")
    op = SimpleNamespace(key="k", kind="delete_visit", label="")
    j.opening(op)
    j.note(op, None, error="refused")
    assert j.unresolved() == set()
    j.opening(op)
    assert j.unresolved() == {"k"}


# --------------------------------------------------------------------------
# an error after sending is not proof of failure
# --------------------------------------------------------------------------

def wire(monkeypatch, *answers, encoding=None):
    """Script the server underneath urllib: its real opener and handlers still run.

    Stubbed where a connection would be opened, so redirects, HTTP errors and
    proxies go through urllib's own chain. Each request takes the next answer,
    and the last one repeats: bytes are a 200, an int an HTTP status, a pair a
    status and a ``Location``, an exception is raised. Returns the requests sent.
    """
    import io
    import urllib.request
    import urllib.response
    from email.message import Message

    from wanderfill.api import transport as tr

    sent = []

    def open_(self, req):
        answer = answers[min(len(sent), len(answers) - 1)]
        sent.append(req)
        if isinstance(answer, Exception):
            raise answer
        headers = Message()
        if encoding:
            headers["Content-Encoding"] = encoding
        status, body = (200, answer) if isinstance(answer, bytes) else (answer, b"")
        if isinstance(status, tuple):
            status, headers["Location"] = status
        resp = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, status)
        resp.msg = "scripted"
        return resp

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", open_)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", open_)
    monkeypatch.setattr(urllib.request, "_opener", None)  # urlopen caches its proxies here
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    return sent


def _answering(monkeypatch, *answers):
    """A real Transport over a scripted wire, and the requests it sends."""
    sent = wire(monkeypatch, *answers)
    return Transport(token="x" * 36, keep_alive=False), sent


@pytest.mark.parametrize("code", [500, 502, 503, 504, 408])
def test_a_500_on_a_write_is_an_unknown_outcome(monkeypatch, code):
    """A gateway that gave up says nothing about what the server behind it did."""
    from wanderfill.api.errors import UnknownWriteOutcome

    t, calls = _answering(monkeypatch, code)
    with pytest.raises(UnknownWriteOutcome, match=str(code)):
        t.webapi("quickEnter/add-visit", region=1)
    assert len(calls) == 1
    t, calls = _answering(monkeypatch, code)
    with pytest.raises(UnknownWriteOutcome):
        t.ajax_text("my_series/", action="toggle", item=1, state=1, series=1)
    assert len(calls) == 1, "a replayed toggle can undo the tick"


@pytest.mark.parametrize("code", [400, 429])
def test_a_refusal_of_a_write_is_still_definite(monkeypatch, code):
    from wanderfill.api.errors import TransportError

    t, calls = _answering(monkeypatch, code)
    with pytest.raises(TransportError):
        t.webapi("quickEnter/add-visit", region=1)
    assert len(calls) == 1


def test_a_500_on_a_read_is_still_retried(monkeypatch):
    t, calls = _answering(monkeypatch, 500, b'{"result":"OK","ids":[1]}')
    assert t.webapi("maps/get-visited-regions-ids-simple")["ids"] == [1]
    assert len(calls) == 2


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_token_on_a_write_is_still_definite(monkeypatch, code):
    from wanderfill.api.errors import AuthError

    t, calls = _answering(monkeypatch, code)
    with pytest.raises(AuthError):
        t.webapi("quickEnter/add-visit", region=1)
    assert len(calls) == 1


PORTAL = "https://portal.example/login"
REDIRECTS = [(code, PORTAL) for code in (301, 302, 303, 307, 308)]
REDIRECTS += [(302, "http://portal.example/"), (302, "javascript:void(0)"), 300, 302, 304]


@pytest.mark.parametrize("answer", REDIRECTS, ids=str)
def test_a_redirect_on_a_write_is_an_unknown_outcome(monkeypatch, answer):
    """urllib turns a redirected POST into a GET, or refuses a 307 outright. Neither
    says what the server did with the POST, and the GET's answer is not its answer:
    here it says OK. The GET would also carry the token to wherever it points."""
    from wanderfill.api.errors import UnknownWriteOutcome

    for write in (
        lambda t: t.webapi("quickEnter/add-visit", region=1),
        lambda t: t.ajax_json("my_series/", action="toggle", item=1),
        lambda t: t.ajax_text("my_series/", action="toggle", item=1, state=1, series=1),
    ):
        t, calls = _answering(monkeypatch, answer, b'{"result":"OK"}')
        with pytest.raises(UnknownWriteOutcome) as info:
            write(t)
        assert [c.get_method() for c in calls] == ["POST"]
        assert "javascript" not in str(info.value), "urllib quotes such an address, query and all"


@pytest.mark.parametrize("code", [401, 403])
def test_a_redirect_to_a_403_is_not_the_server_rejecting_the_token(monkeypatch, code):
    """POST -> 303 -> GET -> 403 raised AuthError, which closes a journal entry."""
    from wanderfill.api.errors import UnknownWriteOutcome

    t, calls = _answering(monkeypatch, (303, PORTAL), code)
    with pytest.raises(UnknownWriteOutcome):
        t.webapi("quickEnter/add-visit", region=1)
    assert len(calls) == 1


TOKEN = "x" * 36
READS = {
    "webapi": lambda t: t.webapi("maps/get-visited-regions-ids-simple"),
    "legacy": lambda t: t.ajax_json("V2/", action="getData", type="seriesSingle", id=1),
}


@pytest.mark.parametrize("read", READS)
@pytest.mark.parametrize("answer", [*REDIRECTS, (302, f"{PORTAL}?token={TOKEN}"),
                                    (302, f"http://{TOKEN}@portal.example/{TOKEN}"),
                                    (302, "http://[bad/"), (307, "/login")], ids=str)
def test_a_redirect_on_a_read_is_not_followed(monkeypatch, answer, read):
    """urllib forwards NMTOKEN with the redirected read: to another host, over plain HTTP.
    It is a year-long full-power credential."""
    from wanderfill.api.errors import TransportError

    t, calls = _answering(monkeypatch, answer, b'{"result":"OK","ids":[1]}')
    with pytest.raises(TransportError) as info:
        READS[read](t)
    (req,) = calls
    assert (req.get_method(), req.type, req.host) == ("POST", "https", "nomadmania.com")
    code, where = answer if isinstance(answer, tuple) else (answer, "")
    said = str(info.value)
    assert str(code) in said and TOKEN not in said and "token=" not in said
    assert ("portal.example" in said) == ("portal.example" in where)
    assert info.value.__cause__ is None and info.value.__suppress_context__, "nor in a traceback"


def test_a_read_after_a_redirect_elsewhere_still_works(monkeypatch):
    """The error is the answer to one request; the transport is not left broken."""
    from wanderfill.api.errors import TransportError

    t, _ = _answering(monkeypatch, (302, PORTAL), b'{"result":"OK","ids":[1]}')
    with pytest.raises(TransportError):
        t.webapi("maps/get-visited-regions-ids-simple")
    assert t.webapi("maps/get-visited-regions-ids-simple")["ids"] == [1]


def test_a_redirected_tile_is_still_followed(monkeypatch, tmp_path):
    """Tiles carry no token, and the rule must not become a process-wide opener."""
    from wanderfill.geo.tiles import TileReader

    t, _ = _answering(monkeypatch, (302, PORTAL), b"a tile")
    with pytest.raises(Exception, match="302"):
        t.webapi("maps/get-visited-regions-ids-simple")
    sent = wire(monkeypatch, (302, "https://maps.example/tile"), b"a tile")
    assert TileReader(tmp_path)._fetch("regions", 10, 1, 1) == b"a tile"
    assert [r.host for r in sent][1:] == ["maps.example"]
    assert all("Nmtoken" not in r.headers for r in sent)


@pytest.mark.parametrize("answers", [((307, PORTAL),), ((303, PORTAL), 403)], ids=str)
def test_a_redirected_write_leaves_the_journal_open(monkeypatch, tmp_path, answers):
    from wanderfill.api.errors import UnknownWriteOutcome

    real, calls = _answering(monkeypatch, *answers)

    class WritesForReal(FakeTransport):
        def webapi(self, action, **fields):
            write = Transport._is_write(action)
            return (real if write else super()).webapi(action, **fields)

    op = Op(kind="mark_dare", bucket="apply", region=1)
    exc, journal = _apply_one(tmp_path, op, transport=WritesForReal)
    assert isinstance(exc, UnknownWriteOutcome)
    assert journal.unresolved() == {op.key}
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"<html>", b"[]", b"", b"\xff\xfe"])
def test_a_write_answered_with_html_is_an_unknown_outcome(monkeypatch, body):
    """A captive portal or an error page: no word on what the server did."""
    from wanderfill.api.errors import UnknownWriteOutcome

    t, calls = _answering(monkeypatch, body)
    with pytest.raises(UnknownWriteOutcome):
        t.webapi("quickEnter/add-visit", region=1)
    with pytest.raises(UnknownWriteOutcome):
        t.ajax_json("my_series/", action="toggle", item=1)
    assert len(calls) == 2, "one attempt each"


def _apply_one(tmp_path, op, replies=None, transport=FakeTransport):
    """Run a one-op plan that has not drifted; return what it raised and its journal."""
    from wanderfill.plan.apply import Journal, apply_plan
    from wanderfill.plan.model import basis_of, fingerprint

    state = {"user/status-quick": {"result": "OK", "uid": 7},
             "maps/get-visited-regions-ids-simple": {"result": "OK", "ids": []},
             "maps/get-visited-dare-ids-simple": {"result": "OK", "ids": []},
             "V2/": {"title": "a series", "visited": []}}
    c = NomadMania(token="fake", transport=transport(state | (replies or {})))
    plan = Plan(account=7, ops=[op], basis={"fingerprint": fingerprint(basis_of({}, []))})
    with pytest.raises(Exception) as info:
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    (path,) = tmp_path.glob("journal-*.ndjson")
    return info.value, Journal(path)


def test_a_series_toggle_that_does_not_say_ok_leaves_the_journal_open(tmp_path):
    """Closed as failed, the next run toggled it again, which can undo the tick."""
    from wanderfill.api.errors import UnknownWriteOutcome

    op = Op(kind="tick_series", bucket="apply", series=1, item=22)
    exc, journal = _apply_one(tmp_path, op, {"my_series/": "<html>"})
    assert isinstance(exc, UnknownWriteOutcome)
    assert journal.unresolved() == {op.key}


def test_an_unexpected_exception_leaves_the_journal_open(tmp_path, monkeypatch):
    """Only an outcome that proves nothing changed may close an entry."""
    from wanderfill.api.errors import AuthError, TransportError, UnknownWriteOutcome
    from wanderfill.plan import apply as ap

    op = Op(kind="mark_dare", bucket="apply", region=1)
    for n, (outcome, left_open) in enumerate((
        (RuntimeError("bug in our own parsing"), True),
        (KeyError("result"), True),
        (UnknownWriteOutcome("lost"), True),
        (VerificationFailed("quickEnter/updateMQP", "still there"), True),
        (ApiError("quickEnter/add-visit", "Missing params"), False),
        (PrecisionLoss("refused before sending"), False),
        (AuthError("401"), False),
        (TransportError("400"), False),
    )):
        def execute(client, op, outcome=outcome):
            raise outcome

        monkeypatch.setattr(ap, "_execute", execute)
        workdir = tmp_path / str(n)
        exc, journal = _apply_one(workdir, op)
        assert exc is outcome
        assert journal.unresolved() == ({op.key} if left_open else set()), repr(outcome)
        assert journal.done_keys() == set()


# --------------------------------------------------------------------------
# one account, one journal truth
# --------------------------------------------------------------------------

STATE = {"user/status-quick": {"result": "OK", "uid": 7},
         "maps/get-visited-regions-ids-simple": {"result": "OK", "ids": []},
         "maps/get-visited-dare-ids-simple": {"result": "OK", "ids": []},
         READ: {"result": "OK", "data": []}}
ACCOUNT_READ = [("user/status-quick", {})]


def _dare_plan(region=1):
    """A one-op plan for account 7, not drifted against ``STATE``."""
    from wanderfill.plan.model import basis_of, fingerprint

    return Plan(account=7, ops=[Op(kind="mark_dare", bucket="apply", region=region)],
                basis={"fingerprint": fingerprint(basis_of({}, []))})


def _apply(workdir, **kw):
    """Apply that plan. Returns the report, or what was raised, and what was sent."""
    from wanderfill.plan.apply import apply_plan

    c, t = client(STATE)
    try:
        return apply_plan(c, _dare_plan(), workdir=workdir, confirm=True, **kw), t.sent
    except Exception as exc:
        return exc, t.sent


def _open_line(path, key="k1"):
    """A write that was started and never confirmed, as an older version recorded it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": "x", "phase": "open", "key": key, "kind": "mark_dare",
                                "label": ""}) + "\n", encoding="utf-8")
    return path


def test_a_torn_journal_line_refuses_instead_of_vanishing(tmp_path):
    """A crash mid-append cut an ``open`` line, the reader skipped it, and the write
    it stood for was free to be sent again."""
    from wanderfill.api.errors import JournalCorrupt
    from wanderfill.plan.apply import Journal, open_entries

    j = Journal(tmp_path / "journal-7-aaaaaaaaaaaa.ndjson")
    j.opening(Op(kind="mark_dare", region=1))
    j.note(Op(kind="mark_dare", region=1), "OK")
    j.opening(Op(kind="mark_dare", region=2))
    torn = j.path.read_bytes()[:-20]
    j.path.write_bytes(torn)
    for read in (j.unresolved, j.done_keys, lambda: open_entries([tmp_path], 7)):
        with pytest.raises(JournalCorrupt, match=r"journal-7-aaaaaaaaaaaa\.ndjson.*line 3"):
            read()
    exc, sent = _apply(tmp_path)
    assert isinstance(exc, JournalCorrupt) and sent == ACCOUNT_READ
    assert j.path.read_bytes() == torn, "a human repairs it, not the tool"


def test_a_line_torn_inside_a_character_is_corrupt_not_a_crash(tmp_path):
    from wanderfill.api.errors import JournalCorrupt
    from wanderfill.plan.apply import Journal

    j = Journal(tmp_path / "j.ndjson")
    j.opening(Op(kind="mark_dare", region=1, label="Kraków"))
    raw = j.path.read_bytes()
    j.path.write_bytes(raw[:raw.index("ó".encode()) + 1])
    with pytest.raises(JournalCorrupt, match="line 1"):
        j.unresolved()


@pytest.mark.parametrize("line", [
    '{"phase": "note", "key": "k1"}', '{"key": "k1"}', '{"phase": "done"}',
    '{"phase": "done", "key": 1}', '{"phase": null, "key": "k1"}',
    '{"phase": ["done"], "key": "k1"}', '["done", "k1"]', '"done"', "null", "done k1", "\x00\x00",
])
def test_only_done_closes_an_open_entry(tmp_path, line):
    """Any phase that was not ``open`` closed the entry, whatever it meant."""
    from wanderfill.api.errors import JournalCorrupt
    from wanderfill.plan.apply import Journal

    j = Journal(_open_line(tmp_path / "j.ndjson"))
    with j.path.open("a", encoding="utf-8") as fh:
        fh.write("\n" + line + "\n")
    for read in (j.unresolved, j.done_keys):
        with pytest.raises(JournalCorrupt, match="line 3"):
            read()


def test_a_line_separator_inside_a_value_does_not_split_the_entry(tmp_path):
    """``str.splitlines`` also cuts at U+2028 and U+0085, which JSON leaves unescaped."""
    from wanderfill.plan.apply import Journal

    j = Journal(tmp_path / "j.ndjson")
    op = Op(kind="mark_dare", region=1, label="a\u2028b\x85c")
    j.opening(op)
    assert j.unresolved() == {op.key}
    j.note(op, {"name": "lone surrogate \ud83d"})
    assert j.unresolved() == set() and j.done_keys() == {op.key}


def test_a_hand_closed_entry_without_a_newline_is_not_glued_to_the_next(tmp_path):
    """Closing an entry is done in an editor, and editors omit the last newline."""
    from wanderfill.plan.apply import Journal

    j = Journal(_open_line(tmp_path / "j.ndjson"))
    with j.path.open("a", encoding="utf-8") as fh:
        fh.write('{"phase": "done", "key": "k1"}')
    op = Op(kind="mark_dare", region=1)
    j.opening(op)
    assert j.unresolved() == {op.key} and j.done_keys() == {"k1"}


def test_replanning_does_not_hide_an_open_write(tmp_path):
    """The journal is named after the plan's basis, so a new plan never opened it."""
    stuck = _open_line(tmp_path / "journal-7-aaaaaaaaaaaa.ndjson")
    exc, sent = _apply(tmp_path)
    assert isinstance(exc, DriftError) and stuck.name in str(exc) and "k1" in str(exc)
    assert sent == ACCOUNT_READ, "refused before the snapshot"


def test_an_open_write_blocks_a_delete_and_the_reverse(monkeypatch, tmp_path, capsys):
    """Applies were journalled in --workdir, the deletion in the default directory."""
    from wanderfill.cli import main as cli

    work, default = tmp_path / "work", tmp_path / "default"
    work.mkdir()
    stuck = _open_line(default / "deletes-7.ndjson", "delete_visit:1:2")
    exc, sent = _apply(work, also=(default,))
    assert isinstance(exc, DriftError) and str(stuck) in str(exc)
    assert sent == ACCOUNT_READ

    # The same through the CLI, which has to name the default directory itself.
    plan = _dare_plan().save(tmp_path / "p.json")
    c, t = client(STATE)
    monkeypatch.setattr(cli, "_client", lambda args: c)
    monkeypatch.setattr(cli, "DEFAULT_WORKDIR", default)
    assert cli.main(["apply", str(plan), "--confirm", "--workdir", str(work)]) == 2
    assert t.sent == ACCOUNT_READ and str(stuck) in capsys.readouterr().err

    # An open apply in the default directory, a deletion run elsewhere.
    stuck = _open_line(default / "journal-79597-aaaaaaaaaaaa.ndjson")
    fake = FakeClient()
    rc = _run_delete(monkeypatch, work, fake, "dare", "1142", "--confirm", "--reason", "x")
    assert rc == 2 and fake.calls == [] and _journal_lines(work) == []
    assert str(stuck) in capsys.readouterr().out


def test_check_prints_every_open_entry_and_writes_nothing(monkeypatch, tmp_path, capsys):
    from wanderfill.cli import main as cli

    work, default = tmp_path / "work", tmp_path / "default"
    plan = _dare_plan().save(tmp_path / "p.json")
    c, _ = client(STATE)
    monkeypatch.setattr(cli, "_client", lambda args: c)
    monkeypatch.setattr(cli, "DEFAULT_WORKDIR", default)
    assert cli.main(["check", str(plan), "--workdir", str(work)]) == 0
    assert not work.exists() and not default.exists(), "check is read-only on disk too"

    one = _open_line(work / "journal-7-aaaaaaaaaaaa.ndjson", "k1")
    two = _open_line(default / "deletes-7.ndjson", "delete_visit:1:2")
    capsys.readouterr()
    assert cli.main(["check", str(plan), "--workdir", str(work)]) == 1
    out = capsys.readouterr().out
    for wanted in (str(one), "k1", str(two), "delete_visit:1:2"):
        assert wanted in out
    assert out.count("append") == 1, "what to do is said once"
    two.write_text("torn")
    assert cli.main(["check", str(plan), "--workdir", str(work)]) == 1
    out = capsys.readouterr().out
    assert "BLOCKERS" in out and "line 1" in out
    assert sorted(p.name for d in (work, default) for p in d.iterdir()) == [two.name, one.name]


def test_journals_from_before_the_phase_field_are_ignored(tmp_path):
    """Their names carry no account, so nobody can say whose they are."""
    from wanderfill.plan.apply import open_entries

    (tmp_path / "journal-20260816T090000Z.ndjson").write_text('{"op": "x"}\nnot json')
    assert open_entries([tmp_path], 7) == {}
    report, _ = _apply(tmp_path)
    assert report.succeeded == 1


def test_another_accounts_open_entry_does_not_block(tmp_path):
    from wanderfill.plan.apply import open_entries

    for name in ("journal-79-aaaaaaaaaaaa.ndjson", "journal-17-aaaaaaaaaaaa.ndjson",
                 "deletes-79.ndjson", "deletes-77.ndjson"):
        _open_line(tmp_path / name)
    assert open_entries([tmp_path], 7) == {}
    report, _ = _apply(tmp_path)
    assert report.succeeded == 1


def test_open_entries_reads_each_directory_once_and_skips_missing_ones(tmp_path):
    from wanderfill.plan.apply import account_lock, open_entries

    stuck = _open_line(tmp_path / "journal-7-aaaaaaaaaaaa.ndjson")
    _open_line(tmp_path / "deletes-7.ndjson").write_text("")
    (tmp_path / "alias").symlink_to(tmp_path)
    dirs = [tmp_path, tmp_path / "alias", tmp_path / "none"]
    assert open_entries(dirs, 7) == {stuck: {"k1"}}
    with account_lock(dirs, 7):  # locking one directory twice would refuse itself
        pass
    assert not (tmp_path / "none").exists()


def test_a_second_apply_is_refused_while_the_first_holds_the_lock(monkeypatch, tmp_path, capsys):
    """Both read the journal before either had written to it, so both sent the plan."""
    from wanderfill.plan.apply import account_lock, apply_plan

    work, default = tmp_path / "a-work", tmp_path / "default"  # locked in this order
    default.mkdir()
    with account_lock([default], 7):
        exc, sent = _apply(work, also=(default,))
        assert isinstance(exc, DriftError) and ".lock-7" in str(exc)
        assert sent == ACCOUNT_READ, "only the account read is sent"
        assert [p.name for p in work.iterdir()] == [".lock-7"]
        # ...and the refusal let go of the directory it did get.
        with account_lock([work], 7):
            pass
        c, t = client(STATE)
        dry = tmp_path / "dry"
        assert apply_plan(c, _dare_plan(), workdir=dry, also=(default,)).skipped == 1
        assert t.sent == [] and not dry.exists(), "a dry run takes no lock and creates nothing"
    with account_lock([tmp_path], 79597):
        fake = FakeClient()
        rc = _run_delete(monkeypatch, work, fake, "dare", "1142", "--confirm", "--reason", "x")
        assert rc == 2 and fake.calls == [] and _journal_lines(work) == []
        assert ".lock-79597" in capsys.readouterr().err
    with account_lock([default], 8):
        report, _ = _apply(work, also=(default,))
    assert report.succeeded == 1, "another account's lock is not ours"


def test_the_lock_is_held_for_the_write_and_the_read_back(monkeypatch, tmp_path):
    from wanderfill.plan import apply as ap

    held = []

    def probe(*a, **kw):
        with pytest.raises(DriftError):
            with ap.account_lock([tmp_path], 7):
                pass
        held.append(True)
        return {}

    monkeypatch.setattr(ap, "_execute", probe)
    monkeypatch.setattr(ap, "verify", probe)
    assert ap.open_entries([tmp_path], 7) == {}
    report, _ = _apply(tmp_path)
    assert report.succeeded == 1 and len(held) == 2
    with ap.account_lock([tmp_path], 7):
        pass


def test_without_fcntl_the_lock_warns_and_continues(monkeypatch, tmp_path, capsys):
    import sys

    monkeypatch.setitem(sys.modules, "fcntl", None)
    report, _ = _apply(tmp_path)
    assert report.succeeded == 1
    assert "lock" in capsys.readouterr().err
    assert not list(tmp_path.glob(".lock-*"))


def test_journal_lines_reach_the_disk(monkeypatch, tmp_path):
    """Flushed is not written: a power cut could take the ``open`` line with it."""
    import os
    import stat

    from wanderfill.plan.apply import Journal

    j = Journal(tmp_path / "j.ndjson")
    lines_at_sync = []

    def fsync(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            lines_at_sync.append(j.path.read_bytes().count(b"\n"))

    monkeypatch.setattr(os, "fsync", fsync)
    j.opening(Op(kind="mark_dare", region=1))
    j.note(Op(kind="mark_dare", region=1), "OK")
    assert lines_at_sync == [1, 2]


def test_a_journal_entry_says_what_was_sent_and_what_came_back(tmp_path):
    """The key is a hash and the result was cut at 400 characters of ``repr``."""
    from types import SimpleNamespace

    from wanderfill.plan.apply import Journal

    j = Journal(tmp_path / "j.ndjson")
    op = Op(kind="create_trip", bucket="apply", regions=[{"id": 5, "quality": 3}],
            date_from="2024-01-01", date_to="2024-01-02", label="L", confidence=0.5,
            method="m", evidence=["IMG_1"])
    answer = {"trip_id": 9, "text": "x" * 1000, "when": dt.date(2024, 1, 1)}
    j.opening(op)
    j.note(op, answer)
    opened, done = (json.loads(x) for x in j.path.read_text(encoding="utf-8").splitlines())
    assert done["result"] == {"trip_id": 9, "text": "x" * 1000, "when": "2024-01-01"}
    assert Op(**opened["payload"]).key == op.key
    assert not {"label", "confidence", "method", "evidence"} & set(opened["payload"])
    # A deletion is not an Op; its record is the snapshot.
    j.opening(SimpleNamespace(key="k", kind="delete_visit", label=""), snapshot={"id": 1})
    assert "payload" not in json.loads(j.path.read_text(encoding="utf-8").splitlines()[-1])


def _undo_calls():
    """method call, the read it verifies with, and that read's answer beforehand."""
    return {
        "delete_visit": (lambda c: c.delete_visit(13450095, 791), READ, visits(ROW)),
        "clear_region": (lambda c: c.clear_region(791), READ, visits(ROW)),
        "delete_trip": (lambda c: c.delete_trip(305716), "trips/get-trip", TRIP),
        "unmark_dare": (lambda c: c.unmark_dare(1142), DARE_READ, {"ids": [1142]}),
        "unmark_kye": (lambda c: c.unmark_kye(570), "kye/get-kye", {"visited": [570]}),
    }


def _raising(exc):
    """A transport whose queued exceptions are raised in their turn."""
    class T(SequencedTransport):
        def webapi(self, action, **fields):
            if self.queues.get(action) and self.queues[action][0] is exc:
                self.sent.append((action, fields))
                raise exc
            return super().webapi(action, **fields)
    return T


@pytest.mark.parametrize("method", sorted(UNDO_METHODS))
def test_a_delete_whose_readback_errors_stays_open(method):
    """delete-visit said OK, the read-back said ERROR, and the plain ApiError read
    as "the server refused, nothing changed" — which closed the journal entry."""
    from wanderfill.api.errors import AuthError, TransportError

    call, read, before = _undo_calls()[method]
    error = {"result": "ERROR", "result_description": "Internal error."}
    t = SequencedTransport({read: [before, error]})
    with pytest.raises(VerificationFailed):
        call(NomadMania(token="fake", transport=t))
    # ...and the same for a read-back that died, lost its session or was not JSON.
    for exc in (TransportError("wifi"), AuthError("401"), json.JSONDecodeError("html", "<", 0)):
        t = _raising(exc)({read: [before, exc]})
        with pytest.raises(VerificationFailed) as info:
            call(NomadMania(token="fake", transport=t))
        assert info.value.__cause__ is exc


@pytest.mark.parametrize("method", sorted(UNDO_METHODS))
def test_a_read_that_errors_before_the_delete_is_still_a_plain_refusal(method):
    """Nothing was sent, so nothing is uncertain and the entry may close."""
    call, read, _ = _undo_calls()[method]
    t = SequencedTransport({read: [{"result": "ERROR", "result_description": "Internal error."}]})
    with pytest.raises(ApiError) as info:
        call(NomadMania(token="fake", transport=t))
    assert not isinstance(info.value, VerificationFailed)
    assert len(t.sent) == 1


def test_a_delete_trip_whose_listing_errors_stays_open():
    """The second half of the proof failing is not proof either."""
    error = {"result": "ERROR", "result_description": "Internal error."}
    t = SequencedTransport({"trips/get-trip": [TRIP, GONE], YEAR: [error]})
    with pytest.raises(VerificationFailed):
        NomadMania(token="fake", transport=t).delete_trip(305716)


# --------------------------------------------------------------------------
# visit identity
# --------------------------------------------------------------------------

def test_visit_signature_ignores_quality_and_trip():
    """Two records for the same region and dates are the same visit.

    quality and trip_id are both mutable, so a change to either is an update,
    not a different visit. Treating them as identity is how duplicates appear.
    """
    a = Visit(1, 100, dt.date(2024, 1, 1), dt.date(2024, 1, 3), 3, None)
    b = Visit(2, 100, dt.date(2024, 1, 1), dt.date(2024, 1, 3), 5, 987)
    assert a.signature == b.signature


def test_visits_for_region_returns_trip_owned_visits_too():
    """Filtering by trip_id is None created 50 duplicate visits once."""
    raw = {
        "data": [
            {"id": 1, "year_from": 2024, "month_from": 1, "day_from": 1,
             "year_to": 2024, "month_to": 1, "day_to": 2, "quality": 3, "trip_id": None},
            {"id": 2, "year_from": 2024, "month_from": 5, "day_from": 1,
             "year_to": 2024, "month_to": 5, "day_to": 2, "quality": 3, "trip_id": 77},
        ]
    }
    c, _ = client({"quickEnter/get-visits-to-region": raw})
    visits = c.visits_for_region(100)
    assert len(visits) == 2
    assert {v.trip_id for v in visits} == {None, 77}


# --------------------------------------------------------------------------
# segmentation
# --------------------------------------------------------------------------

def _days(pairs):
    return {dt.date.fromisoformat(d): set(rs) for d, rs in pairs}


def test_cap_breaks_unbounded_runs():
    """Without a cap a continuous absence becomes one enormous 'trip'.

    200 days at home, then 165 unbroken days away — which is the shape of a
    nomadic year, and the shape that produced a single 465-day, 101-region
    "trip" before the cap existed. Home is inferred as the year's modal region,
    so it has to be the one with more days.
    """
    start = dt.date(2022, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {1} for i in range(200)}
    day_regions |= {start + dt.timedelta(days=200 + i): {2} for i in range(165)}

    uncapped = segment(day_regions, gap_days=2, cap_days=99999)
    capped = segment(day_regions, gap_days=2, cap_days=30)

    assert len(uncapped) == 1, "one unbroken absence is one run"
    assert uncapped[0].days == 165
    assert len(capped) > len(uncapped)
    assert max(j.days for j in capped) <= 31


def test_jump_cuts_a_dense_track_the_cap_would_glue():
    """The incident: one month came out as a single trip.

    Photographs on every single day, three unrelated places in a row. Nothing
    ever breaks on a gap, so gap+cap alone returns one 30-day "trip" spanning
    all three. Cutting on jumps returns the three journeys a person would name.
    """
    start = dt.date(2026, 7, 16)
    day_regions = {}
    for i in range(14):                      # a fortnight in region 1
        day_regions[start + dt.timedelta(days=i)] = {1}
    for i in range(9):                       # a drive: each day overlaps the last
        day_regions[start + dt.timedelta(days=14 + i)] = {10 + i, 11 + i}
    for i in range(7):                       # then a flight somewhere unrelated
        day_regions[start + dt.timedelta(days=23 + i)] = {99}

    glued = segment(day_regions, gap_days=2, cap_days=30, home=None,
                    split_on_jump=False)
    cut = segment(day_regions, gap_days=2, cap_days=30, home=None)

    assert len(glued) == 1, "gap+cap cannot see a boundary here — that is the bug"
    assert len(cut) == 3, "stay, drive, flight"
    # The drive must survive as ONE journey: consecutive days share a region.
    drive = cut[1]
    assert drive.days == 9 and len(drive.regions) == 10


def test_auto_refuses_to_jump_cut_a_one_region_per_day_track():
    """The opposite incident: jump-cutting a collapsed track shreds it.

    A track already reduced to one region per day makes every move look like a
    jump. On a real 17-year history that turned 326 journeys into 1,155, 38% of
    them one day long. "auto" has to measure the track and decline.
    """
    start = dt.date(2024, 1, 1)
    # Ten days, moving every other day, one region per day and never overlapping.
    day_regions = {start + dt.timedelta(days=i): {i // 2} for i in range(10)}
    assert multi_region_share(day_regions) == 0.0

    auto = segment(day_regions, gap_days=2, cap_days=30, home=None)
    forced = segment(day_regions, gap_days=2, cap_days=30, home=None, split_on_jump=True)

    assert len(auto) == 1, "auto must leave a collapsed track alone"
    assert len(forced) == 5, "forced jump-cutting shreds it, which is the point"


def test_auto_enables_jump_cutting_on_a_detailed_track():
    """And it has to say yes when the track does carry both ends of a day."""
    start = dt.date(2024, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {i, i + 1} for i in range(6)}
    day_regions[start + dt.timedelta(days=6)] = {99}      # an unrelated flight
    assert multi_region_share(day_regions) > MULTI_REGION_FLOOR
    assert len(segment(day_regions, gap_days=2, cap_days=30, home=None)) == 2


def test_split_on_jump_rejects_nonsense():
    """A typo'd string must not be read as truthy and silently enable cutting."""
    day_regions = {dt.date(2024, 1, 1): {1}}
    with pytest.raises(ValueError, match="split_on_jump"):
        segment(day_regions, home=None, split_on_jump="yes")


def test_home_windows_describe_a_home_that_started_and_stopped():
    """Neither a bare region list nor "infer" can express a home with dates.

    Region 1 was home for the first year and nothing afterwards. A bare
    ``home=[1]`` discards the later stay too; ``home=None`` keeps the earlier
    one. Only a window gets both halves right.
    """
    day_regions = {}
    for i in range(300):                                  # year one: living in 1
        day_regions[dt.date(2021, 1, 1) + dt.timedelta(days=i)] = {1}
    for i in range(60):                                   # year three: passing through 1
        day_regions[dt.date(2023, 6, 1) + dt.timedelta(days=i)] = {1}

    windowed = segment(
        day_regions,
        gap_days=2,
        cap_days=9999,
        home=[HomeWindow(dt.date(2021, 1, 1), dt.date(2021, 12, 31), {1})],
    )
    days_kept = sum(j.days for j in windowed)
    assert days_kept == 60, "the 2023 stay is travel, the 2021 one is not"

    always_home = segment(day_regions, gap_days=2, cap_days=9999, home=[1])
    assert always_home == [], "a bare id wrongly discards the later stay"


def test_open_ended_home_window():
    """``start=None`` means 'and we are not saying when it began'."""
    day_regions = {dt.date(2020, 1, 1) + dt.timedelta(days=i): {1} for i in range(10)}
    w = HomeWindow(None, dt.date(2020, 1, 5), {1})
    assert w.covers(dt.date(1999, 1, 1)) and not w.covers(dt.date(2020, 1, 6))
    js = segment(day_regions, gap_days=2, cap_days=9999, home=[w])
    assert sum(j.days for j in js) == 5


def test_empty_home_window_is_rejected():
    """A window with no regions is the 'no home then' case written wrongly."""
    with pytest.raises(ValueError, match="uncovered"):
        HomeWindow(dt.date(2021, 1, 1), dt.date(2021, 12, 31), [])


def test_windows_and_bare_ids_cannot_be_mixed():
    """A bare id alongside a dated window silently means 'home forever'."""
    day_regions = {dt.date(2021, 1, 1): {1}}
    with pytest.raises(TypeError, match="mixes"):
        segment(day_regions, home=[HomeWindow(None, None, {1}), 2])


def test_compare_homes_reports_days_not_just_trips():
    """Choosing a home model is a choice about which days get discarded.

    Trip counts hide that. The number somebody needs to see before approving a
    model is how much of their life it has decided was not travel.
    """
    day_regions = {dt.date(2024, 1, 1) + dt.timedelta(days=i): {1 if i < 200 else 2}
                   for i in range(300)}
    rows = {r["model"]: r for r in compare_homes(
        day_regions, {"inferred": "infer", "nomadic": None}, cap_days=9999)}
    assert rows["nomadic"]["days_home"] == 0
    assert rows["inferred"]["days_home"] == 200, "region 1 is the modal region"
    assert rows["nomadic"]["days_travel"] > rows["inferred"]["days_travel"]


def test_split_first_and_repeat_avoids_double_counting():
    """First visits stay standalone; only repeats go into trips."""
    j1 = Journey(dt.date(2024, 1, 1), dt.date(2024, 1, 3),
                 {10: (dt.date(2024, 1, 1), dt.date(2024, 1, 3))})
    j2 = Journey(dt.date(2024, 6, 1), dt.date(2024, 6, 3),
                 {10: (dt.date(2024, 6, 1), dt.date(2024, 6, 3)),
                  20: (dt.date(2024, 6, 2), dt.date(2024, 6, 3))})
    first, repeats = split_first_and_repeat([j1, j2])
    assert set(first) == {10, 20}
    assert len(repeats) == 1
    assert set(repeats[0].regions) == {10}, "20 is a first visit, so it must not be in the trip"


# --------------------------------------------------------------------------
# place-name folding
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [("Nukuʻalofa", "Nukualofa"), ("Jönköping", "Jonkoping"), ("Valparaíso", "Valparaiso")],
)
def test_fold_matches_across_alphabets(a, b):
    assert fold(a) == fold(b)


def test_components_splits_compound_names():
    assert "laspaz" not in components("La Paz/El Alto (BO)")
    assert "lapaz" in components("La Paz/El Alto (BO)")
    assert "elalto" in components("La Paz/El Alto (BO)")


def test_components_drops_short_fragments():
    """Two-letter state codes collide with everything."""
    assert "tx" not in components("Austin/Round Rock (TX)")


# --------------------------------------------------------------------------
# plan safety
# --------------------------------------------------------------------------

def test_only_apply_bucket_executes():
    plan = Plan(account=1, ops=[
        Op(kind="add_visit", bucket="apply", region=1),
        Op(kind="add_visit", bucket="review", region=2),
        Op(kind="add_visit", bucket="reject", region=3),
    ])
    assert [o.region for o in plan.to_apply()] == [1]


def test_op_key_is_stable_and_ignores_provenance():
    a = Op(kind="add_visit", region=5, date_from="2024-01-01", date_to="2024-01-02",
           evidence=["photo A"], confidence=0.9)
    b = Op(kind="add_visit", region=5, date_from="2024-01-01", date_to="2024-01-02",
           evidence=["photo B"], confidence=0.1)
    assert a.key == b.key, "re-running the same intent must be a no-op"


def test_plan_roundtrip(tmp_path):
    plan = Plan(account=12345, ops=[Op(kind="mark_dare", region=1142, bucket="apply")])
    path = plan.save(tmp_path / "plan.json")
    again = Plan.load(path)
    assert again.account == 12345
    assert again.ops[0].kind == "mark_dare"


# --------------------------------------------------------------------------
# track handling
# --------------------------------------------------------------------------

def test_track_dedupes_coordinates_before_geocoding():
    pts = [
        DayPoint(dt.date(2024, 1, 1), 50.4180, 30.5350, "Kyiv"),
        DayPoint(dt.date(2024, 1, 2), 50.41802, 30.53499, "Kyiv"),
        DayPoint(dt.date(2024, 1, 3), 41.8902, 12.4922, "Rome"),
    ]
    track = Track(points=pts)
    assert len(track.distinct_coords()) == 2
    assert set(track.places()) == {"kyiv", "rome"}


def test_transport_repr_hides_token():
    from wanderfill.api.transport import Transport

    t = Transport(token="00000000-DEAD-BEEF-0000-NOTAREALTOKEN")
    assert "DEAD" not in repr(t)
    assert "token" in repr(t) and "redacted" in repr(t)


def test_no_home_counts_every_day_as_travel():
    """A traveller with no home must not have a region silently treated as one.

    With home="infer" the modal region of the year is excluded from trips. For
    somebody genuinely nomadic that quietly deletes the place they spent most of
    the year in from their own record, which is the opposite of what they want.
    """
    start = dt.date(2022, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {1} for i in range(200)}
    day_regions |= {start + dt.timedelta(days=200 + i): {2} for i in range(160)}

    inferred = segment(day_regions, gap_days=2, cap_days=9999, home="infer")
    nomadic = segment(day_regions, gap_days=2, cap_days=9999, home=None)

    assert {r for j in inferred for r in j.regions} == {2}, "region 1 was treated as home"
    assert {r for j in nomadic for r in j.regions} == {1, 2}, "no home means everything counts"


def test_stated_home_is_used_verbatim():
    start = dt.date(2022, 1, 1)
    day_regions = {start + dt.timedelta(days=i): {1} for i in range(200)}
    day_regions |= {start + dt.timedelta(days=200 + i): {2} for i in range(160)}

    js = segment(day_regions, gap_days=2, cap_days=9999, home=[2])
    assert {r for j in js for r in j.regions} == {1}, "region 2 was stated as home"


def test_empty_home_list_is_rejected_rather_than_guessed():
    """home=[] could mean 'no home' or 'I forgot to fill this in'. Refuse it."""
    day_regions = {dt.date(2022, 1, 1): {1}}
    with pytest.raises(ValueError, match="home=None"):
        segment(day_regions, home=[])


# --------------------------------------------------------------------------
# the geocoder's response shape depends on a request parameter
# --------------------------------------------------------------------------

def test_resolver_reads_both_geocoder_shapes():
    """share=0 answers {"region": N}; without it, {"nm": {"id": N}}.

    Parsing only the second silently resolves nothing at all — every coordinate
    comes back unplaced, which reads like a data problem rather than a parsing
    one. That cost a full re-run of a 513-point trip.
    """
    from wanderfill.geo.resolve import RegionResolver

    r = RegionResolver(catalogue={181: {"name": "Iceland"}}, reader=object())
    assert r.from_geocoder({"result": "OK", "region": 181}) == 181
    assert r.from_geocoder({"result": "OK", "nm": {"id": 181}}) == 181


def test_resolver_rejects_ids_absent_from_the_catalogue():
    """Stale ids and the -1 open-water sentinel must both come back as None."""
    from wanderfill.geo.resolve import RegionResolver

    r = RegionResolver(catalogue={181: {"name": "Iceland"}}, reader=object())
    assert r.from_geocoder({"region": 1387}) is None   # stale: Switzerland, since split
    assert r.from_geocoder({"region": -1}) is None     # open water
    assert r.from_geocoder({"result": "OK"}) is None


# --------------------------------------------------------------------------
# a visit can have no dates at all
# --------------------------------------------------------------------------

def test_visit_survives_null_dates():
    """Every date field comes back null for a region that was merely clicked.

    That is how most long-standing profiles are populated — one real profile
    had 118 of them — and ``int(None)`` raises ``TypeError``. The failure is
    invisible until something reads *every* region's visits rather than the
    handful it just wrote, at which point a whole run dies on region 4.
    """
    v = Visit.from_api(
        {
            "id": 13132630, "quality": 3, "trip_id": None,
            "year_from": None, "month_from": None, "day_from": None,
            "year_to": None, "month_to": None, "day_to": None,
        },
        region=4,
    )
    assert v.date_from is None and v.date_to is None
    assert v.id == 13132630          # it is still a visit
    assert v.quality == 3            # and it still counts toward the region


def test_visit_still_reads_a_dated_row():
    v = Visit.from_api(
        {
            "id": 1, "quality": 4, "trip_id": 77,
            "year_from": 2024, "month_from": 1, "day_from": 3,
            "year_to": 2024, "month_to": 1, "day_to": 4,
        },
        region=3,
    )
    assert v.date_from == dt.date(2024, 1, 3)
    assert v.date_to == dt.date(2024, 1, 4)
    assert v.trip_id == 77


# --------------------------------------------------------------------------
# the token comes from somewhere, and which somewhere matters
# --------------------------------------------------------------------------

def test_dotenv_reads_both_shapes_people_write(tmp_path):
    from wanderfill.cli.main import read_dotenv

    f = tmp_path / ".env"
    f.write_text(
        "# a comment\n"
        "\n"
        "export NM_TOKEN='abc-123'\n"
        "OTHER=plain\n"
        'QUOTED="with spaces"\n'
        "not a variable\n"
    )
    env = read_dotenv(f)
    assert env["NM_TOKEN"] == "abc-123"     # export stripped, quotes stripped
    assert env["OTHER"] == "plain"
    assert env["QUOTED"] == "with spaces"
    assert "not a variable" not in env


def test_dotenv_never_raises_on_a_file_it_cannot_read(tmp_path):
    """Failing to start because of a stray character is a bad half-hour."""
    from wanderfill.cli.main import read_dotenv

    assert read_dotenv(tmp_path / "absent") == {}
    weird = tmp_path / ".env"
    weird.write_bytes(b"\xff\xfe not utf-8 at all")
    assert read_dotenv(weird) == {}


def test_the_environment_beats_a_file(tmp_path, monkeypatch):
    """An exported variable is somebody saying 'this one, now'.

    A file on disk is not, and a token the user forgot they wrote is how the
    wrong account gets written to.
    """
    from wanderfill.cli.main import find_token

    (tmp_path / ".env").write_text("NM_TOKEN=from-file\n")
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("NM_TOKEN", "from-environment")
    assert find_token()[0] == "from-environment"

    monkeypatch.delenv("NM_TOKEN")
    token, source = find_token()
    assert token == "from-file"
    assert source.endswith(".env")      # and it says where it came from


def test_the_search_stops_at_the_repository_root(tmp_path, monkeypatch):
    """Never climb out of the project hunting for somebody else's secrets."""
    from wanderfill.cli.main import find_token

    (tmp_path / ".env").write_text("NM_TOKEN=outside-the-repo\n")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("NM_TOKEN", raising=False)
    assert find_token() == ("", "")


# --------------------------------------------------------------------------
# quality can never go down — incident #1, structurally
# --------------------------------------------------------------------------

def test_update_visit_takes_max_against_the_live_record():
    """A caller passing 3 over a live 'lived here' must not downgrade it."""
    c, t = client(_one_visit(quality=5))
    c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=3)
    _, fields = t.sent[-1]
    assert fields["quality"] == 5, "the live 5 must win over the caller's 3"


def test_update_visit_still_raises_quality_upwards():
    c, t = client(_one_visit(quality=3))
    c.update_visit(11, 22, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=5)
    _, fields = t.sent[-1]
    assert fields["quality"] == 5


def test_apply_refuses_an_op_with_no_quality():
    """`op.quality or 3` turned a missing field into a silent downgrade."""
    from wanderfill.plan.apply import _execute
    c, _ = client(_one_visit())
    op = Op(kind="update_visit", region=22, visit_id=11,
            date_from="2024-01-01", date_to="2024-01-02", quality=None)
    with pytest.raises(ValueError, match="no quality"):
        _execute(c, op)


def test_apply_does_not_promote_quality_zero_to_three():
    """0 is falsey; `or 3` silently rewrote 'no visit' as 'good visit'."""
    from wanderfill.plan.apply import _execute
    c, t = client(_one_visit(quality=0))
    op = Op(kind="update_visit", region=22, visit_id=11,
            date_from="2024-01-01", date_to="2024-01-02", quality=0)
    _execute(c, op)
    _, fields = t.sent[-1]
    assert fields["quality"] == 0


def test_plan_year_only_round_trips_through_execute():
    """A bare "2013" must reach the wire as a year, not as 1 January."""
    from wanderfill.plan.apply import _execute
    c, t = client(_one_visit(year_from=None, month_from=None, day_from=None,
                             year_to=None, month_to=None, day_to=None, quality=3))
    _execute(c, Op(kind="update_visit", region=22, visit_id=11,
                   date_from="2013", date_to="2013", quality=3))
    _, fields = t.sent[-1]
    assert fields["year_from"] == 2013 and fields["month_from"] == ""


# --------------------------------------------------------------------------
# the plan/apply safety boundary
# --------------------------------------------------------------------------

def test_op_key_distinguishes_different_visits_and_qualities():
    """Colliding keys let a journal mark a *different* write as already done."""
    base = dict(kind="update_visit", region=5, date_from="2024-01-01", date_to="2024-01-02")
    assert Op(**base, visit_id=1, quality=3).key != Op(**base, visit_id=2, quality=3).key
    assert Op(**base, visit_id=1, quality=3).key != Op(**base, visit_id=1, quality=5).key
    # provenance still must not affect it
    assert (Op(**base, visit_id=1, quality=3, label="a", confidence=0.1).key
            == Op(**base, visit_id=1, quality=3, label="b", confidence=0.9).key)


def test_basis_covers_visit_dates_not_just_region_ids():
    """Re-dating a visit changes no region id, so ids alone cannot detect drift."""
    from wanderfill.plan.model import basis_of
    def snap(d):
        return {"visited_regions": [5], "visited_dare": [],
                "visits": {"5": [{"id": 1, "date_from": d, "date_to": d, "quality": 3}]}}

    assert basis_of(snap("2024-01-01"), [5]) != basis_of(snap("2024-06-01"), [5])


def test_apply_refuses_a_plan_with_no_basis():
    """Failing open made the drift check decorative."""
    import pathlib
    import tempfile

    from wanderfill.plan.apply import apply_plan
    c, _ = client({"user/status-quick": {"result": "OK", "uid": 7},
                   "maps/get-visited-regions-ids-simple": {"result": "OK", "ids": []},
                   "maps/get-visited-dare-ids-simple": {"result": "OK", "ids": []}})
    plan = Plan(account=7, ops=[Op(kind="mark_dare", bucket="apply", region=1)])
    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(DriftError, match="no basis fingerprint"):
            apply_plan(c, plan, workdir=pathlib.Path(td), confirm=True)


def test_journal_is_write_ahead_and_survives_a_crash():
    """An op that opened and never closed must block a blind retry."""
    import pathlib
    import tempfile

    from wanderfill.plan.apply import Journal
    op = Op(kind="mark_dare", region=1)
    with tempfile.TemporaryDirectory() as td:
        j = Journal(pathlib.Path(td) / "j.ndjson")
        j.opening(op)                      # crash right here
        assert j.unresolved() == {op.key}
        assert op.key not in j.done_keys()
        j.note(op, "OK")
        assert j.unresolved() == set()
        assert op.key in j.done_keys()


def test_countries_does_not_hand_out_a_bare_yes_field():
    """`row["yes"]` reads as the YES score and is not one."""
    c, _ = client({"slow/get-slow-app": {"result": "OK", "slow": [
        {"country_id": 1, "country": "X", "flag": "/f/1.png", "visited": 1, "yes": 8}]}})
    row = c.countries()[0]
    assert "yes" not in row
    assert row["yes_stored"] == 8


def test_kye_quadrant_id_is_not_a_region_id():
    """qids and NM region ids overlap numerically; conflating them is silent."""
    from wanderfill.plan.apply import _execute
    c, t = client()
    _execute(c, Op(kind="mark_kye", item=570, region=None))
    action, fields = t.sent[-1]
    assert action == "kye/set-kye"
    assert fields == {"qid": 570, "visited": 1}


def test_mark_kye_never_unticks():
    c, t = client()
    c.mark_kye(38)
    _, fields = t.sent[-1]
    assert fields["visited"] == 1


# --------------------------------------------------------------------------
# TCC: the field is called `region`, the id is not one
# --------------------------------------------------------------------------

TCC_ROWS = {"quickEnter/get-regions": {"result": "OK", "data": {"regions": [], "dare": [], "tcc": [
    {"id": 64, "name": "Cyprus – Sovereign BBA (UK) – Akrotiri and Dhekelia", "visited": 0},
    {"id": 325, "name": "Antarctica - British Antarctic Territory", "visited": 1},
]}}}


def test_mark_tcc_sends_one_and_the_tcc_id():
    """Found as markTCC on /regions/: {region: <tcc id>, visits: 1}; the page un-ticks with -1."""
    c, t = client(TCC_ROWS)
    c.mark_tcc(64)
    action, fields = t.sent[-1]
    assert action == "quickEnter/updateTCC"
    assert fields == {"region": 64, "visits": 1}


def test_mark_tcc_refuses_an_id_that_is_not_a_tcc_destination():
    """`region` invites a region id; region 493 is Kuwait, and no TCC row is 493."""
    c, t = client(TCC_ROWS)
    with pytest.raises(PrecisionLoss):
        c.mark_tcc(493)
    assert not [a for a, _ in t.sent if a == "quickEnter/updateTCC"]


def test_tcc_visited_reads_the_flag_not_presence():
    c, _ = client(TCC_ROWS)
    assert c.visited_tcc_ids() == {325}


def test_tcc_costs_one_list_read_per_run_not_one_per_op():
    """The first live apply of four ticks spent 636 requests re-reading the list."""
    from wanderfill.plan.apply import verify
    c, t = client(TCC_ROWS)
    c.mark_tcc(64)
    c.mark_tcc(325)
    lists = [a for a, _ in t.sent if a == "quickEnter/get-regions"]
    assert len(lists) == 29
    t.sent.clear()
    report = verify(c, [Op(kind="mark_tcc", item=64), Op(kind="mark_tcc", item=325)])
    assert len([a for a, _ in t.sent if a == "quickEnter/get-regions"]) == 29
    assert report["checked"] == 2
    assert report["mismatches"] == ["TCC destination 64 did not stick"]


def test_tcc_verify_reads_the_server_after_the_writes_not_the_catalogue():
    """The id cache must never answer "is it ticked": that would pass every write."""
    from wanderfill.plan.apply import verify
    c, t = client(dict(TCC_ROWS))  # a copy: this test changes the server's answer
    c.mark_tcc(325)
    t.replies["quickEnter/get-regions"] = {"result": "OK", "data": {"tcc": [
        {"id": 64, "visited": 0}, {"id": 325, "visited": 0},
    ]}}
    report = verify(c, [Op(kind="mark_tcc", item=325)])
    assert report["mismatches"] == ["TCC destination 325 did not stick"]


def test_tcc_list_with_a_field_dropped_is_an_error_not_unticked():
    """`{"result": "OK"}` read as an empty list would report a good write as failed."""
    c, _ = client()
    with pytest.raises(VerificationFailed):
        c.tcc()
    c, _ = client({"quickEnter/get-regions": {"result": "OK", "data": {"tcc": [{"id": 64}]}}})
    with pytest.raises(VerificationFailed):
        c.visited_tcc_ids()


def test_tcc_list_may_be_empty_most_megaregions_have_none():
    c, _ = client({"quickEnter/get-regions": {"result": "OK", "data": {"tcc": []}}})
    assert c.tcc() == {}


def test_verify_says_unproven_when_the_tcc_list_cannot_be_read():
    from wanderfill.plan.apply import verify
    c, t = client()
    report = verify(c, [Op(kind="mark_tcc", item=64), Op(kind="mark_tcc", item=325)])
    assert report["mismatches"] == []
    assert len(report["unproven"]) == 2
    assert len([a for a, _ in t.sent if a == "quickEnter/get-regions"]) <= 29


def test_tcc_plan_drifts_when_a_tick_changes_and_other_plans_do_not_notice():
    from wanderfill.plan.model import basis_of, fingerprint
    base = {"visited_regions": [1], "visited_dare": [], "visits": {}}
    before = fingerprint(basis_of(base | {"visited_tcc": [64]}, []))
    after = fingerprint(basis_of(base | {"visited_tcc": []}, []))
    assert before != after
    assert basis_of(base, []).keys() == {"visited", "dare", "visits"}


def test_snapshot_reads_tcc_only_when_asked():
    c, t = client(TCC_ROWS | {"maps/get-visited-regions-ids-simple": {"ids": []},
                              "maps/get-visited-dare-ids-simple": {"ids": []},
                              "user/status-quick": {"uid": 7}})
    assert "visited_tcc" not in c.snapshot([])
    assert not [a for a, _ in t.sent if a == "quickEnter/get-regions"]
    assert c.snapshot([], tcc=True)["visited_tcc"] == [325]


def test_tcc_op_carries_its_id_in_item_and_touches_no_region():
    from wanderfill.plan.apply import _execute
    from wanderfill.plan.model import regions_touched
    c, t = client(TCC_ROWS)
    op = Op(kind="mark_tcc", item=64)
    assert regions_touched([op]) == set()
    _execute(c, op)
    assert t.sent[-1] == ("quickEnter/updateTCC", {"region": 64, "visits": 1})


# --------------------------------------------------------------------------
# YES: undated is 8, not the age — and the rule of thumb is not a rule
# --------------------------------------------------------------------------

def test_undated_country_scores_eight_not_the_age():
    """Scoring undated as the age made a recompute disagree with the server by 33."""
    from wanderfill.api.client import UNDATED_YES
    assert UNDATED_YES == 8
    c, _ = client({
        "user/get-settings": {"result": "OK", "date_of_birth": "1985-01-03"},
        "slow/get-slow-app": {"result": "OK", "slow": [
            {"country_id": 1, "country": "Seen", "flag": "/f/1.png", "visited": 1, "yes": 8},
            {"country_id": 2, "country": "Never", "flag": "/f/2.png", "visited": 0, "yes": 41}]},
        "quickEnter/get-regions": {"result": "OK", "data": {"regions": []}},
    })
    y = c.yes_scores(today=dt.date(2026, 8, 16))
    assert y[1]["yes"] == 8 and y[1]["undated"] is True
    assert y[2]["yes"] == 41, "never visited still scores the age"


def test_yes_delta_is_a_gain_for_recent_years_and_a_loss_for_old_ones():
    """"Dating an old visit makes YES worse" is false for a recent visit."""
    from wanderfill.api.client import yes_delta
    today = dt.date(2026, 8, 16)
    assert yes_delta(8, 2026, today=today) == -8   # this year: full gain
    assert yes_delta(8, 2025, today=today) == -8   # the "gift" year
    assert yes_delta(8, 2019, today=today) == -1   # still a gain
    assert yes_delta(8, 2018, today=today) == 0    # break-even, exactly
    assert yes_delta(8, 2013, today=today) == 5    # Myanmar: a real loss


# --------------------------------------------------------------------------
# grading a coordinate: the speed rule is code, not prose
# --------------------------------------------------------------------------

def _fix(lat, lon, day, hh=None, mm=0, ss=0):
    from wanderfill.grade import Fix
    at = dt.datetime(day.year, day.month, day.day, hh, mm, ss) if hh is not None else None
    return Fix(lat, lon, day, at)


def test_grade_rejects_a_flight():
    """Two mid-Pacific points 1,100 km and 2.5 hours apart were one flight."""
    from wanderfill.grade import grade
    d = dt.date(2025, 10, 22)
    v = grade([_fix(-18.265, -173.905, d, 12, 59), _fix(-18.318, -163.413, d, 15, 28)])
    assert not v.ok and "km/h" in v.reason


def test_grade_rejects_an_airport_layover_on_time_not_speed():
    """Sitting in Terminal E is slow; it is the span that catches it."""
    from wanderfill.grade import grade
    d = dt.date(2023, 4, 27)
    v = grade([_fix(29.984, -95.333, d, 17, m) for m in (27, 39, 53)])
    assert not v.ok and "min" in v.reason


def test_grade_accepts_two_distinct_days():
    from wanderfill.grade import grade
    v = grade([_fix(48.85, 2.35, dt.date(2024, 5, 1)), _fix(48.86, 2.34, dt.date(2024, 5, 2))])
    assert v.ok and "2 distinct days" in v.reason


def test_grade_accepts_one_dense_day_at_ground_speed():
    from wanderfill.grade import grade
    d = dt.date(2024, 3, 3)
    v = grade([_fix(-33.4 + i * 0.001, -70.79, d, 3 + i // 4, (i * 13) % 60) for i in range(12)])
    assert v.ok and "one day" in v.reason


def test_grade_never_infers_speed_between_untimed_rows():
    """Day-level rows have no intra-day clock; pairing them invents a speed."""
    from wanderfill.grade import looks_airborne
    d = dt.date(2013, 7, 1)
    fast, legs = looks_airborne([_fix(50.4, 30.5, d), _fix(16.8, 96.1, d)])
    assert (fast, legs) == (0, 0)


def test_grade_holds_rather_than_rejects():
    """St Petersburg was in the held pile and was real. Held is not rejected."""
    from wanderfill.grade import grade
    v = grade([_fix(60.002, 30.299, dt.date(2014, 6, 17))])
    assert not v.ok and "single day" in v.reason


def _frozen():
    """12 photos over 2 h 45 min in the air, every one stamped with the same coordinate."""
    d = dt.date(2025, 10, 22)
    return [_fix(-17.553412, -149.606731, d, 9 + i * 15 // 60, i * 15 % 60) for i in range(12)]


def test_grade_holds_a_frozen_fix():
    """A phone repeated one stale coordinate for hours in flight, and 12 photos of
    it read as a day on the ground."""
    from wanderfill.grade import grade

    for fixes in (_frozen(), _frozen()[::-1]):
        v = grade(fixes)
        assert not v.ok and v.repeated == 11
        assert "one coordinate repeated" in v.reason
        assert "frozen fix" in v.reason and "phone lying still" in v.reason
        assert (v.days, v.points, v.span_hours, v.fast_legs, v.timed_legs) == (1, 1, 0.0, 0, 0)


def test_grade_sees_the_jump_after_a_frozen_fix():
    """Eleven legs of 0 km/h outvoted the one that showed the aircraft."""
    from wanderfill.grade import grade, looks_airborne

    d = dt.date(2025, 10, 22)
    v = grade([*_frozen(), _fix(33.942512, -118.408134, d, 12, 5)])
    assert not v.ok and "km/h" in v.reason and "airborne" in v.reason
    assert (v.fast_legs, v.timed_legs, v.repeated) == (1, 1, 11)
    # 500 km in the 20 minutes since the last photo, not in the 3 hours since the first.
    near = [*_frozen(), _fix(-22.05, -149.606731, d, 12, 5)]
    assert looks_airborne(near) == (1, 1) and not grade(near).ok
    # ...and a leg into a run ends at its first photo, not its last.
    into = [_fix(-22.05, -149.606731, d, 8, 40), *_frozen()]
    assert looks_airborne(into) == (1, 1) and not grade(into).ok


def test_grade_survives_a_jump_in_the_second_a_frozen_fix_ended():
    """No time passed, so no speed was measured: the leg is skipped, as it always was."""
    from wanderfill.grade import grade

    v = grade([*_frozen(), _fix(33.942512, -118.408134, dt.date(2025, 10, 22), 11, 45)])
    assert not v.ok and (v.points, v.fast_legs, v.timed_legs, v.repeated) == (2, 0, 0, 11)


def test_grade_holds_one_coordinate_on_two_dates():
    """Two photos with one coordinate either side of midnight were two distinct days."""
    from wanderfill.grade import grade

    a = _fix(-17.553412, -149.606731, dt.date(2025, 10, 22), 23, 50)
    b = _fix(-17.553412, -149.606731, dt.date(2025, 10, 23), 0, 10)
    v = grade([a, b])
    assert not v.ok and v.days == 1 and v.repeated == 1
    assert "one coordinate repeated" in v.reason
    # A day that stands without the repeat still stands.
    v = grade([a, b, _fix(-17.553, -149.607, dt.date(2025, 10, 24))])
    assert v.ok and v.reason == "2 distinct days" and v.repeated == 1


def test_grade_does_not_collapse_rounded_or_untimed_points():
    """A coordinate rounded to a hundred metres repeats whenever somebody sits down,
    and a day-level row repeats by construction. Neither is a frozen receiver."""
    from wanderfill.grade import Verdict, grade

    d = dt.date(2024, 3, 3)
    for lat, lon in ((-33.401, -70.79), (-33.4012, -70.7934), (-33.0, -70.0)):
        v = grade([_fix(lat, lon, d, 9 + i * 15 // 60, i * 15 % 60) for i in range(12)])
        assert v == Verdict(
            True, "one day, 12 points over 2.8h at ground speed", 1, 12, 2.75, 0, 0)
    rows = [_fix(50.450123, 30.523456, dt.date(2013, 7, n)) for n in (1, 2, 2)]
    assert grade(rows) == Verdict(True, "2 distinct days", 2, 3, 0.0, 0, 0)
    assert grade(rows[1:]) == Verdict(False, "single day, 2 point(s)", 1, 2, 0.0, 0, 0)


def test_standing_still_at_a_rounded_coordinate_does_not_outvote_a_flight():
    """Twelve rounded photos in one place are twelve observations, but not eleven
    legs at 0 km/h: those outvoted the 6,600 km in the next twenty minutes."""
    from wanderfill.grade import grade, looks_airborne

    d = dt.date(2025, 10, 22)
    still = [_fix(-17.553, -149.607, d, f.at.hour, f.at.minute) for f in _frozen()]
    jump = [*still, _fix(33.943, -118.408, d, 12, 5)]
    for fixes in (jump, jump[::-1]):
        assert looks_airborne(fixes) == (1, 1)
        v = grade(fixes)
        assert not v.ok and "airborne" in v.reason and "repeated" not in v.reason
        assert (v.points, v.fast_legs, v.timed_legs, v.repeated) == (13, 1, 1, 0)
    # 500 km in the 20 minutes since the last photo, not in the 3 hours since the first.
    assert looks_airborne([*still, _fix(-22.05, -149.607, d, 12, 5)]) == (1, 1)
    # A slow leg between two places is still a leg, beside any number of still ones.
    walk = [*still, _fix(-17.563, -149.607, d, 12, 5), _fix(33.943, -118.408, d, 12, 25)]
    assert looks_airborne(walk) == (1, 2)


def test_two_photos_of_one_place_in_one_second_are_no_leg():
    from wanderfill.grade import grade, looks_airborne

    d = dt.date(2024, 3, 3)
    for lat, lon in ((-33.401, -70.79), (-33.401234, -70.791234)):
        pair = [_fix(lat, lon, d, 9, 0), _fix(lat, lon, d, 9, 0)]
        assert looks_airborne(pair) == (0, 0)
        v = grade(pair)
        assert not v.ok and v.span_hours == 0.0


def test_grade_collapses_past_four_decimals_however_the_float_prints():
    """Beside the equator a float prints as `2e-05`, which has no decimal point to
    count from; one full-precision axis is enough, whichever it is."""
    from wanderfill.grade import grade

    d = dt.date(2024, 3, 3)
    for lat, lon in ((0.00002, 32.5), (0.5, 0.000012), (0.5, 32.50001), (-17.55341, 32.5)):
        v = grade([_fix(lat, lon, d, 9, 0), _fix(lat, lon, d, 12, 0)])
        assert v.repeated == 1, (lat, lon)
    for lat, lon in ((0.0001, 32.5), (0.5, 32.5001)):
        assert grade([_fix(lat, lon, d, 9, 0), _fix(lat, lon, d, 12, 0)]).repeated == 0


def test_an_untimed_row_neither_joins_nor_breaks_a_run():
    from wanderfill.grade import grade

    d = dt.date(2025, 10, 22)
    row = _fix(-17.553412, -149.606731, d)
    run = _frozen()
    v = grade([*run[:6], row, *run[6:]])
    assert (v.repeated, v.points, v.ok) == (11, 2, False)


def test_a_coordinate_that_comes_back_is_not_a_run():
    """Out and back to the same spot is movement; only consecutive repeats collapse."""
    from wanderfill.grade import grade

    d = dt.date(2024, 3, 3)
    v = grade([_fix(*((-33.401234, -70.791234) if i % 2 else (-33.411234, -70.791234)), d,
                    9 + i // 4, i % 4 * 15) for i in range(12)])
    assert v.ok and (v.repeated, v.points, v.timed_legs) == (0, 12, 11)


def test_grade_accepts_a_stay_that_moves_a_little():
    """A phone that is awake jitters by metres; that is what a real stay looks like."""
    from wanderfill.grade import grade

    d = dt.date(2025, 10, 22)
    v = grade([_fix(-17.553412 + i % 3 * 1e-5, -149.606731 + i % 2 * 1e-5, d,
                    9 + i // 5, i % 5 * 12) for i in range(12)])
    assert v.ok and "ground speed" in v.reason and v.repeated == 0


def test_a_burst_in_one_spot_does_not_hold_a_day_that_moves():
    from wanderfill.grade import grade

    d = dt.date(2024, 3, 3)
    walk = [_fix(-33.401234 + i * 0.001, -70.791234, d, 9 + i // 4, i % 4 * 15)
            for i in range(12)]
    burst = [_fix(walk[4].lat, walk[4].lon, d, 10, 0, n) for n in range(1, 9)]
    v = grade([*walk, *burst])
    assert v.ok and (v.points, v.repeated) == (12, 8)
    assert v.reason == "one day, 12 points over 2.8h at ground speed"


# --------------------------------------------------------------------------
# writes are never retried, and a lost answer is not a failure
# --------------------------------------------------------------------------

def test_writes_are_not_retried():
    """A retried write is how duplicate visits are made."""
    from wanderfill.api.transport import Transport
    assert Transport._is_write("quickEnter/add-visit")
    assert Transport._is_write("quickEnter/update-visit")
    assert Transport._is_write("trips/new-trip")
    assert Transport._is_write("kye/set-kye")
    assert Transport._is_write("quickEnter/updateTCC")
    assert not Transport._is_write("quickEnter/get-visits-to-region")
    assert not Transport._is_write("slow/get-slow-app")
    assert not Transport._is_write("regions/get-regions-list-2")


def test_apply_refuses_a_plan_containing_the_same_write_twice():
    """`already` is read once, so both copies would otherwise execute."""
    import pathlib
    import tempfile

    from wanderfill.api.errors import PlanInvalid
    from wanderfill.plan.apply import apply_plan
    twice = [Op(kind="mark_dare", bucket="apply", region=7),
             Op(kind="mark_dare", bucket="apply", region=7)]
    plan = Plan(account=7, ops=twice, basis={"fingerprint": "x"})
    c, _ = client({"user/status-quick": {"result": "OK", "uid": 7},
                   "maps/get-visited-regions-ids-simple": {"result": "OK", "ids": []},
                   "maps/get-visited-dare-ids-simple": {"result": "OK", "ids": []}})
    with tempfile.TemporaryDirectory() as td:
        with pytest.raises(PlanInvalid, match="twice"):
            apply_plan(c, plan, workdir=pathlib.Path(td), confirm=True)


def test_regions_touched_includes_trip_members():
    """create_trip names its regions in `regions`, not `region`."""
    from wanderfill.plan.model import regions_touched
    ops = [Op(kind="create_trip", regions=[{"id": 11, "quality": 3}, {"id": 12, "quality": 3}]),
           Op(kind="update_visit", region=5, visit_id=1),
           Op(kind="mark_kye", item=570)]
    assert regions_touched(ops) == {5, 11, 12}, "a qid must not be read as a region"


def test_year_only_visit_widens_to_a_whole_year_for_date_checks():
    """Comparing a YearOnly to a date raised TypeError and broke `evidence`."""
    from wanderfill.cli.main import _visit_window

    class V:
        def __init__(self, a, b): self.date_from, self.date_to = a, b

    assert _visit_window(V(YearOnly(2013), YearOnly(2013))) == (
        dt.date(2013, 1, 1), dt.date(2013, 12, 31))
    assert _visit_window(V(dt.date(2024, 1, 2), dt.date(2024, 1, 3))) == (
        dt.date(2024, 1, 2), dt.date(2024, 1, 3))
    assert _visit_window(V(None, None)) is None


def test_apply_verifies_writes_against_the_server():
    """"OK" is not evidence. Both historical incidents returned OK at the time."""
    from wanderfill.plan.apply import verify
    c, _ = client(_one_visit(quality=3, year_from=2024, month_from=1, day_from=1,
                             year_to=2024, month_to=1, day_to=2))
    good = verify(c, [Op(kind="update_visit", region=22, visit_id=11,
                         date_from="2024-01-01", date_to="2024-01-02", quality=3)])
    assert good["checked"] == 1 and good["mismatches"] == []

    bad = verify(c, [Op(kind="update_visit", region=22, visit_id=11,
                        date_from="2024-06-01", date_to="2024-06-02", quality=3)])
    assert bad["mismatches"], "a date the server did not store must be reported"


def test_verify_reports_a_duplicate_rather_than_passing():
    """Fifty duplicate visits were made once; this is the check that would see it."""
    from wanderfill.plan.apply import verify
    row = {"id": 1, "quality": 3, "trip_id": 99,
           "year_from": 2024, "month_from": 1, "day_from": 1,
           "year_to": 2024, "month_to": 1, "day_to": 1}
    c, _ = client({"quickEnter/get-visits-to-region":
                   {"result": "OK", "data": [row, {**row, "id": 2}]}})
    out = verify(c, [Op(kind="add_visit", region=5,
                        date_from="2024-01-01", date_to="2024-01-01", quality=3)])
    assert any("duplicate" in m for m in out["mismatches"])
    assert out["phantom_trips"], "the auto-created trip id must be recorded"


def _stored(**over):
    row = {"id": 1, "quality": 3, "trip_id": 900,
           "year_from": 2024, "month_from": 3, "day_from": 1,
           "year_to": 2024, "month_to": 3, "day_to": 2}
    return {**row, **over}


def _readback(rows, marked=None, header=None):
    replies = {"quickEnter/get-visits-to-region": {"result": "OK", "data": rows},
               "trips/get-trip": {"trip": header}}
    if marked is not None:
        replies["maps/get-visited-regions-ids-simple"] = {"result": "OK", "ids": marked}
    return client(replies)[0]


def test_a_transit_is_stored_but_does_not_mark_the_region():
    """Two regions were added once and the count moved by one: the other was a transit."""
    from wanderfill.plan.apply import verify
    add = Op(kind="add_visit", region=7, date_from="2024-03-01", date_to="2024-03-02", quality=1)
    out = verify(_readback([_stored(quality=1)], marked=[8]), [add])
    assert out["mismatches"] == []
    assert out["not_counted"] == [{"region": 7, "quality": 1,
                                   "why": "a transit-quality visit does not mark the region"}]

    # the same absence at quality 2 is not expected, and is said so
    add2 = Op(kind="add_visit", region=7, date_from="2024-03-01", date_to="2024-03-02", quality=2)
    out = verify(_readback([_stored(quality=2)], marked=[8]), [add2])
    assert out["not_counted"] == [] and "not in the visited list" in out["mismatches"][0]

    # and a transit inside a trip is the same transit
    trip = Op(kind="create_trip", date_from="2024-03-01", date_to="2024-03-02",
              regions=[{"id": 7, "quality": 1}])
    out = verify(_readback([_stored(quality=1)], marked=[8]), [trip], {trip.key: {"trip_id": 900}})
    assert out["mismatches"] == [] and out["not_counted"][0]["region"] == 7


def test_verify_compares_the_stored_quality_not_the_planned_one():
    """A visit that came back as 0 is "no visit" wearing the plan's dates."""
    from wanderfill.plan.apply import verify
    add = Op(kind="add_visit", region=7, date_from="2024-03-01", date_to="2024-03-02", quality=1)
    out = verify(_readback([_stored(quality=0)], marked=[]), [add])
    assert any("stored quality [0], planned 1" in m for m in out["mismatches"])
    assert out["not_counted"][0]["quality"] == 0, "reported as stored, not as planned"


def test_a_missing_visited_list_proves_nothing_either_way():
    """{"result": "OK"} with no ids is not an empty profile."""
    from wanderfill.plan.apply import verify
    for quality in (1, 3):
        add = Op(kind="add_visit", region=7, date_from="2024-03-01", date_to="2024-03-02",
                 quality=quality)
        out = verify(_readback([_stored(quality=quality)]), [add])
        assert out["mismatches"] == [] and out["not_counted"] == []
        assert "could not be read back" in out["unproven"][0]


def test_verify_reads_a_created_trip_back():
    """Trips once verified as "checked: 0" — create_trip had no read-back at all."""
    from wanderfill.plan.apply import verify
    region = {"id": 7, "quality": 3, "hidden": 0,
              "year_from": 2024, "month_from": 3, "day_from": 1,
              "year_to": 2024, "month_to": 3, "day_to": 2}
    op = Op(kind="create_trip", date_from="2024-03-01", date_to="2024-03-10", regions=[region])
    made = {op.key: {"result": "OK", "trip_id": 900}}
    clean = {"checked": 1, "mismatches": [], "phantom_trips": [], "not_counted": [],
             "unproven": []}

    header = {"date_from": "2024-03-01", "date_to": "2024-03-10"}
    assert verify(_readback([_stored()], marked=[7], header=header), [op], made) == clean

    # a visit that was already there, owned by another trip, is not the new one
    out = verify(_readback([_stored(trip_id=99)], marked=[7]), [op], made)
    assert "has no visit owned by trip 900" in out["mismatches"][0]

    # the new visit landed beside an older one for the same dates
    out = verify(_readback([_stored(), _stored(id=2, trip_id=None)], marked=[7]), [op], made)
    assert any("duplicate" in m for m in out["mismatches"])

    # stored under other dates than planned
    out = verify(_readback([_stored(day_to=5)], marked=[7]), [op], made)
    assert any("planned 2024-03-01..2024-03-02" in m for m in out["mismatches"])

    assert "has no visit" in verify(_readback([], marked=[7]), [op], made)["mismatches"][0]


def test_a_trip_whose_id_is_unknown_is_unproven_not_passed():
    """Matching by date alone cannot tell the new visit from one that predates it."""
    from wanderfill.plan.apply import verify
    op = Op(kind="create_trip", date_from="2024-03-01", date_to="2024-03-02",
            regions=[{"id": 7, "quality": 3}])
    out = verify(_readback([_stored(trip_id=99)], marked=[7]), [op])
    assert out["mismatches"] == []
    assert "could predate it" in out["unproven"][0]

    out = verify(_readback([_stored(), _stored(id=2, trip_id=None)], marked=[7]), [op])
    assert any("not all owned by one trip" in m for m in out["mismatches"])


def test_trip_region_dates_as_the_wire_carries_them():
    """Year-only and string-valued components are valid, and were read as missing."""
    from wanderfill.plan.apply import verify
    year_only = {"id": 7, "quality": 3, "year_from": 2013, "month_from": None, "day_from": None,
                 "year_to": 2013, "month_to": None, "day_to": None}
    op = Op(kind="create_trip", date_from="2013-01-01", date_to="2013-12-31", regions=[year_only])
    made = {op.key: {"trip_id": 900}}
    stored = _stored(year_from=2013, month_from=None, day_from=None,
                     year_to=2013, month_to=None, day_to=None)
    assert verify(_readback([stored], marked=[7]), [op], made)["mismatches"] == []

    strings = {"id": 7, "quality": 3, "year_from": "2024", "month_from": "3", "day_from": "1",
               "year_to": "2024", "month_to": "3", "day_to": "2"}
    op = Op(kind="create_trip", date_from="2024-03-01", date_to="2024-03-10", regions=[strings])
    assert verify(_readback([_stored()], marked=[7]), [op],
                  {op.key: {"trip_id": 900}})["mismatches"] == []


def test_a_trip_region_without_dates_is_held_to_the_trips():
    """Found by the trip's id, it verified clean whatever dates the server had stored."""
    from wanderfill.plan.apply import verify
    bare = Op(kind="create_trip", date_from="2024-03-01", date_to="2024-03-10",
              regions=[{"id": 7, "quality": 3}])
    header = {"date_from": "2024-03-01", "date_to": "2024-03-10"}

    def against(op=bare, **over):
        out = verify(_readback([_stored(**over)], marked=[7], header=header), [op],
                     {op.key: {"trip_id": 900}})
        return out["mismatches"], out["unproven"]

    assert against(day_to=10) == ([], [])
    for over in ({}, {"day_from": 2, "day_to": 10}, {"month_to": None, "day_to": None,
                                                     "day_from": 2}):
        (line,), unproven = against(**over)
        assert "region 7" in line and "2024-03-01..2024-03-10" in line and unproven == []
    none = dict.fromkeys(f"{part}_{side}" for part in ("year", "month", "day")
                         for side in ("from", "to"))
    for over in (none, none | {"year_from": 2024, "year_to": 2024},
                 {"month_to": None, "day_to": None}):
        mismatches, (line,) = against(**over)
        assert mismatches == [] and "region 7" in line and "not been measured" in line
    # Dates that are absent in so many words are no dates.
    empty = Op(kind="create_trip", date_from="2024-03-01", date_to="2024-03-10",
               regions=[{"id": 7, "quality": 3} | dict.fromkeys(none, "")])
    assert len(against(empty)[0]) == 1


@pytest.mark.parametrize("given,wrong", [
    ({"year_from": 2024, "month_from": 3, "day_from": 1}, ""),
    ({"year_from": 2024, "month_from": 3, "day_from": 5}, "2024-03-05..2024-03-02"),
    ({"year_from": 2024}, "2024..2024-03-02"),
    ({"year_to": 2024, "month_to": 3, "day_to": 2}, ""),
    ({"year_to": 2024, "month_to": 3, "day_to": 9}, "2024-03-01..2024-03-09"),
    ({"year_from": 2024, "month_from": 13, "day_from": 1, "year_to": 2024}, "2024-03-01..2024"),
])
def test_a_half_dated_trip_region_is_compared_end_by_end(given, wrong):
    """One end alone turned off both checks: not a span, and not a region without dates."""
    from wanderfill.plan.apply import verify
    op = Op(kind="create_trip", date_from="2024-03-01", date_to="2024-03-02",
            regions=[{"id": 7, "quality": 3} | given])
    header = {"date_from": "2024-03-01", "date_to": "2024-03-02"}
    made = {op.key: {"trip_id": 900}}
    out = verify(_readback([_stored()], marked=[7], header=header), [op], made)
    if wrong:
        (line,) = out["mismatches"]
        assert f"planned {wrong}" in line and "region 7" in line
    else:
        assert out["mismatches"] == []
    assert out["unproven"] == []
    # An end it does give must be there: a bare year, or nothing, is not it.
    for side in ("from", "to"):
        if f"year_{side}" in given and not wrong:
            for year in (2024, None):
                vague = {f"year_{side}": year, f"month_{side}": None, f"day_{side}": None}
                out = verify(_readback([_stored(**vague)], marked=[7], header=header), [op], made)
                assert len(out["mismatches"]) == 1 and out["unproven"] == []
    # The end it does not give is held to the trip's, as for a region that gives none.
    undated = dict.fromkeys(f"{p}_{w}" for p in ("year", "month", "day")
                            for w in ("from", "to") if f"year_{w}" not in given)
    if undated and not wrong:
        out = verify(_readback([_stored(**undated)], marked=[7], header=header), [op], made)
        assert out["mismatches"] == [] and "not been measured" in out["unproven"][0]



# ------------------------------------------- the Photos date window


def _photos_library(tmp_path, shots, importers=None):
    """A minimal Photos.sqlite: (utc datetime, offset seconds, lat, lon) per photo.

    ``importers`` is one bundle id per photo; without it the column is not there at
    all, as in an older library.
    """
    import sqlite3

    from wanderfill.sources.photos_app import APPLE_EPOCH_OFFSET

    db = tmp_path / "lib.photoslibrary" / "database" / "Photos.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID, ZDATECREATED, ZLATITUDE,
            ZLONGITUDE, ZDIRECTORY, ZFILENAME, ZFAVORITE, ZKIND, ZKINDSUBTYPE,
            ZTRASHEDSTATE, ZSAVEDASSETTYPE, ZMOMENT);
        CREATE TABLE ZADDITIONALASSETATTRIBUTES (ZASSET, ZTIMEZONEOFFSET,
            ZORIGINALFILENAME, ZCAMERACAPTUREDEVICE);
        CREATE TABLE ZMOMENT (Z_PK INTEGER PRIMARY KEY, ZTITLE);
    """)
    for pk, (utc, offset, lat, lon) in enumerate(shots, 1):
        ts = utc.replace(tzinfo=dt.timezone.utc).timestamp() - APPLE_EPOCH_OFFSET
        con.execute("INSERT INTO ZASSET VALUES (?,?,?,?,?,'d','f.jpg',0,0,0,0,0,NULL)",
                    (pk, f"u{pk}", ts, lat, lon))
        con.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?,?,'f.jpg',0)",
                    (pk, offset))
    if importers is not None:
        con.execute("ALTER TABLE ZADDITIONALASSETATTRIBUTES"
                    " ADD COLUMN ZIMPORTEDBYBUNDLEIDENTIFIER")
        for pk, (importer, _) in enumerate(zip(importers, shots, strict=True), 1):
            con.execute("UPDATE ZADDITIONALASSETATTRIBUTES"
                        " SET ZIMPORTEDBYBUNDLEIDENTIFIER = ? WHERE ZASSET = ?", (importer, pk))
    con.commit()
    con.close()
    return db.parents[1]


@pytest.fixture
def mac_on_tahiti_time(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Pacific/Tahiti")
    time.tzset()
    # a no-op tzset would let the test pass without testing anything
    assert time.timezone == 10 * 3600
    yield
    monkeypatch.undo()
    time.tzset()


def test_since_is_the_photos_date_not_the_macs(tmp_path, mac_on_tahiti_time):
    """2 Nov 2026: with the Mac on UTC-10, `since=29 Oct` cut at 10:00 UTC and
    dropped the morning of 29 Oct in Sydney (UTC+11) whole, so a plan could not
    see the first day of the stay it was extending."""
    from wanderfill.sources.photos_app import PhotosAppSource

    sydney = 11 * 3600
    lib = _photos_library(tmp_path, [
        (dt.datetime(2026, 10, 28, 12, 30), sydney, -33.86, 151.21),  # 28 Oct 23:30 local
        (dt.datetime(2026, 10, 29, 1, 43), sydney, -33.92, 151.17),   # 29 Oct 12:43 local
        (dt.datetime(2026, 10, 30, 10, 55), sydney, -33.94, 151.17),  # 30 Oct 21:55 local
        (dt.datetime(2026, 10, 30, 13, 30), sydney, -33.86, 151.21),  # 31 Oct 00:30 local
    ])
    src = PhotosAppSource(library=lib, since=dt.date(2026, 10, 29),
                          until=dt.date(2026, 10, 30))
    days = sorted({p.date for p in src.points()})
    assert days == [dt.date(2026, 10, 29), dt.date(2026, 10, 30)]
    taken = [a.taken for a in src.assets()]
    assert taken == [dt.datetime(2026, 10, 29, 12, 43), dt.datetime(2026, 10, 30, 21, 55)]


def test_unset_bounds_are_open(tmp_path, mac_on_tahiti_time):
    """No `since` reaches a scanned 1965 print; no `until` reaches a photo whose
    own clock is already tomorrow by this Mac's."""
    from wanderfill.sources.photos_app import PhotosAppSource

    tomorrow = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=5)
    lib = _photos_library(tmp_path, [
        (dt.datetime(1965, 6, 1, 12, 0), 0, 51.5, -0.12),
        (tomorrow, 14 * 3600, 1.87, -157.4),  # Kiritimati, UTC+14
    ])
    assert len(list(PhotosAppSource(library=lib).assets())) == 2


# ------------------------------------------- who a photo came from

# One of each, in this order: camera, AirDrop, glasses, no importer, empty importer.
IMPORTERS = ["com.apple.camera", "com.apple.sharingd", "com.facebook.stella", None, ""]


def _mixed_shots():
    """Five photos on five days; u6 and u7 share a day and a rounded coordinate."""
    noon = dt.datetime(2024, 5, 1, 12)
    return [(noon + dt.timedelta(days=i), 7200, 48.0 + i, 16.123456) for i in range(5)] + [
        (noon + dt.timedelta(days=5), 7200, 60.00012, 16.5),
        (noon + dt.timedelta(days=5, hours=1), 7200, 60.00034, 16.5)]


def _mixed_library(tmp_path):
    return _photos_library(tmp_path, _mixed_shots(),
                           [*IMPORTERS, "com.apple.camera", "com.apple.sharingd"])


def test_a_library_without_the_importer_column_reads_as_unknown(tmp_path):
    """An older library records no importer at all. It must still open, and a filter
    on it must not look like a library with no photos in it."""
    from wanderfill.sources import PhotosAppSource

    lib = _photos_library(tmp_path, _mixed_shots())
    src = PhotosAppSource(library=lib)
    assert src.importer_column is None  # not read yet
    assets = list(src.assets())
    assert len(assets) == 7 and src.importer_column is False
    assert {(a.importer, a.origin) for a in assets} == {(None, "unknown")}

    own = PhotosAppSource(library=lib, origins=frozenset({"own-camera"}))
    assert list(own.assets()) == [] and own.importer_column is False
    own = PhotosAppSource(library=lib, origins=frozenset({"own-camera"}))
    assert list(own.points()) == [] and own.importer_column is False
    unknown = PhotosAppSource(library=lib, origins=frozenset({"unknown"}))
    assert list(unknown.assets()) == assets
    assert list(unknown.points()) == list(src.points())


def test_unknown_is_never_own_camera(tmp_path):
    """No importer is no statement. Reading it as the owner's camera would claim for
    them every photo that arrived before Photos began recording where from."""
    import sqlite3

    from wanderfill.sources import PhotosAppSource

    lib = _mixed_library(tmp_path)
    src = PhotosAppSource(library=lib)
    assets = {a.uuid: a for a in src.assets()}
    assert src.importer_column is True
    assert {u: a.origin for u, a in assets.items()} == {
        "u1": "own-camera", "u2": "airdrop", "u3": "other", "u4": "unknown", "u5": "unknown",
        "u6": "own-camera", "u7": "airdrop"}
    assert assets["u3"].importer == "com.facebook.stella"
    assert assets["u4"].importer is None and assets["u5"].importer is None

    # A photo with no attributes row at all has no importer either.
    con = sqlite3.connect(lib / "database" / "Photos.sqlite")
    con.execute("DELETE FROM ZADDITIONALASSETATTRIBUTES WHERE ZASSET = 1")
    con.commit()
    con.close()
    own = PhotosAppSource(library=lib, origins=frozenset({"own-camera"}))
    assert [a.uuid for a in own.assets()] == ["u6"]
    assert [p.date for p in own.points()] == [dt.date(2024, 5, 6)]


def test_points_and_assets_apply_the_same_origin_filter(tmp_path):
    """Two readings that disagree prove a region the importer never claimed."""
    from itertools import combinations

    from wanderfill.sources import PhotosAppSource

    lib = _mixed_library(tmp_path)
    everything = list(PhotosAppSource(library=lib).assets())
    names = ("own-camera", "airdrop", "other", "unknown")
    for n in range(1, len(names) + 1):
        for chosen in map(frozenset, combinations(names, n)):
            src = PhotosAppSource(library=lib, origins=chosen)
            assets = list(src.assets())
            assert assets == [a for a in everything if a.origin in chosen]
            weights = {}
            for a in assets:
                weights[(a.date, *a.key)] = weights.get((a.date, *a.key), 0) + 1
            assert {(p.date, p.lat, p.lon): p.weight for p in src.points()} == weights
    # The window still applies beside the filter.
    src = PhotosAppSource(library=lib, since=dt.date(2024, 5, 2),
                          origins=frozenset({"own-camera"}))
    assert [a.uuid for a in src.assets()] == ["u6"]
    assert [(p.date, p.weight) for p in src.points()] == [(dt.date(2024, 5, 6), 1)]


def test_an_unknown_origin_name_is_refused(tmp_path):
    """A typo that filters everything out reads as a library with nothing in it, and
    so does an empty set."""
    from wanderfill.sources import PhotosAppSource

    lib = _mixed_library(tmp_path)
    for bad in (frozenset({"own-camera", "own_camera"}), "own-camera", frozenset(), ""):
        with pytest.raises(ValueError) as exc:
            PhotosAppSource(library=lib, origins=bad)
        for name in ("own-camera", "airdrop", "other", "unknown"):
            assert name in str(exc.value)
    src = PhotosAppSource(library=lib)
    for bad in (frozenset({"camera"}), frozenset()):
        src.origins = bad  # set afterwards, it is still refused
        for read in (src.points, src.assets):
            with pytest.raises(ValueError, match="own-camera"):
                list(read())


def test_no_filter_reads_exactly_what_it_read_before(tmp_path, mac_on_tahiti_time):
    """Every plan and dossier so far was built from the unfiltered reading."""
    from wanderfill.sources import DayPoint, PhotosAppSource

    day = [dt.date(2024, 5, 1) + dt.timedelta(days=i) for i in range(6)]
    points = [DayPoint(day[i], 48.0 + i, 16.123, source="photos.app") for i in range(5)]
    points.append(DayPoint(day[5], 60.0, 16.5, source="photos.app", weight=2))
    old = ("uuid", "taken", "lat", "lon", "directory", "filename", "original_name", "place",
           "selfie", "favorite", "video")
    rows = [(f"u{i}", (utc + dt.timedelta(hours=2)), lat, lon, "d", "f.jpg", "f.jpg", "",
             False, False, False) for i, (utc, _, lat, lon) in enumerate(_mixed_shots(), 1)]
    for name, lib in (("with", _mixed_library(tmp_path / "with")),
                      ("without", _photos_library(tmp_path / "without", _mixed_shots()))):
        src = PhotosAppSource(library=lib)
        assert list(src.points()) == points, name
        assert [tuple(getattr(a, f) for f in old) for a in src.assets()] == rows, name


def test_taken_utc_orders_photos_across_a_date_line_flight(tmp_path, mac_on_tahiti_time):
    """Auckland at 10:00 on the 2nd is before Tahiti at 16:00 on the 1st. The wall
    clock, which dates both correctly, puts them in the wrong order."""
    from wanderfill.sources import PhotosAppSource

    lib = _photos_library(tmp_path, [
        (dt.datetime(2026, 11, 1, 21, 0), 13 * 3600, -37.008, 174.785),   # 2 Nov 10:00
        (dt.datetime(2026, 11, 2, 2, 0), -10 * 3600, -17.553, -149.607),  # 1 Nov 16:00
    ])
    assets = list(PhotosAppSource(library=lib).assets())
    assert [a.uuid for a in sorted(assets, key=lambda a: a.taken)] == ["u2", "u1"]
    flown = sorted(assets, key=lambda a: a.taken_utc)
    assert [a.uuid for a in flown] == ["u1", "u2"]
    assert flown[0].taken_utc == dt.datetime(2026, 11, 1, 21, 0, tzinfo=dt.timezone.utc)
    assert flown[0].taken_utc.utcoffset() == dt.timedelta(0)
    assert flown[1].taken_utc - flown[0].taken_utc == dt.timedelta(hours=5)
    assert [a.taken for a in flown] == [dt.datetime(2026, 11, 2, 10), dt.datetime(2026, 11, 1, 16)]
    assert all(a.taken.tzinfo is None for a in flown)


def test_a_photo_asset_still_builds_from_its_old_positions():
    from wanderfill.sources import PhotoAsset

    a = PhotoAsset("u", dt.datetime(2024, 5, 1, 12), 48.0, 16.0, "d", "f.jpg", "o.jpg", "Wien",
                   True, False, False)
    assert (a.place, a.selfie, a.importer, a.taken_utc, a.origin) == (
        "Wien", True, None, None, "unknown")

# trip headers: update-visit moves the visit, never the trip around it
# --------------------------------------------------------------------------

def _row(rid=1266, quality=5, a="2026-10-31", b="2026-11-12", hidden=0):
    y1, m1, d1 = map(int, a.split("-"))
    y2, m2, d2 = map(int, b.split("-"))
    return {"id": rid, "quality": quality, "hidden": hidden, "date_from": a, "date_to": b,
            "year_from": y1, "month_from": m1, "day_from": d1,
            "year_to": y2, "month_to": m2, "day_to": d2}


def _trip(trip_id, date_from, date_to, rows=None, description="kept"):
    return {"result": "OK", "trip": {
        "id": trip_id, "date_from": date_from, "date_to": date_to, "year": None,
        "description": description, "regions": rows or [_row()]}}


def _visits(*rows):
    """rows: (visit_id, trip_id, quality, date_from, date_to)"""
    out = []
    for vid, tid, q, a, b in rows:
        y1, m1, d1 = map(int, a.split("-"))
        y2, m2, d2 = map(int, b.split("-"))
        out.append({"id": vid, "trip_id": tid, "quality": q, "year_from": y1, "month_from": m1,
                    "day_from": d1, "year_to": y2, "month_to": m2, "day_to": d2})
    return {"result": "OK", "data": out}


def _listing(*trips):
    return {"result": "OK", "trips": [
        {"id": tid, "date_from": a, "date_to": b, "regions": [{"id": r} for r in regs]}
        for tid, a, b, regs in trips]}


OLD = _visits((100, 7, 5, "2026-10-31", "2026-11-12"), (50, None, 3, "2025-10-22", "2025-10-30"))
NOT_FOUND = {"result": "ERROR", "result_description": "Trip not found."}
STALE = (7, "2026-10-31", "2026-11-02", [1266])
FIXED = ("2026-10-31", "2026-11-12")


def _sync(get_trip, visits, listings):
    t = SequencedTransport({"trips/get-trip": get_trip,
                            "quickEnter/get-visits-to-region": visits,
                            "trips/get-trips-for-year-app": listings})
    return NomadMania(token="fake", transport=t), t


def _posts(t):
    return [f for a, f in t.sent if a == "trips/update-trip"]


def test_trip_header_lags_update_visit_and_is_resynced_like_the_site():
    """Society Islands trip 501536 read 31 Oct - 2 Nov around a 31 Oct - 12 Nov visit."""
    c, t = _sync([_trip(7, "2026-10-31", "2026-11-02"), _trip(7, *FIXED)],
                 [OLD, OLD], [_listing(STALE)])
    out = c.sync_trip_header(7)
    (fields,) = _posts(t)
    assert fields["trip_id"] == 7
    assert (fields["date_from"], fields["date_to"]) == FIXED
    assert fields["regions"] == "[object Object]"
    assert fields["description"] == "kept"
    assert json.loads(fields["regions_json"])[0]["quality"] == 5, "the site would send 3"
    assert out["now"] == FIXED and out["visits"] == {1266: 100}


def test_update_trip_501536_came_back_as_a_new_trip_and_a_new_visit():
    """The first live save deleted trip 501536 + visit 14138557 and created
    547375 + 14213716. The read-back must follow the new id, not call it lost."""
    new = _visits((101, 8, 5, *FIXED), (50, None, 3, "2025-10-22", "2025-10-30"))
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"), NOT_FOUND, _trip(8, *FIXED)],
                 [OLD, new],
                 [_listing(STALE)] + [_listing((8, *FIXED, [1266]))] * 2)
    out = c.sync_trip_header(7)
    assert (out["trip_id"], out["old_trip_id"], out["visits"]) == (8, 7, {1266: 101})


def test_update_trip_a_preexisting_twin_is_not_the_replacement():
    """Trip 9 already had this span and region; it must not pose as the new trip."""
    twin = (9, *FIXED, [1266])
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"), NOT_FOUND],
                 [OLD], [_listing(STALE, twin), _listing(twin), _listing(twin)])
    with pytest.raises(VerificationFailed, match="0 NEW trip"):
        c.sync_trip_header(7)


def test_update_trip_old_visit_left_standalone_beside_the_new_one():
    orphan = _visits((101, 8, 5, *FIXED), (100, None, 5, *FIXED),
                     (50, None, 3, "2025-10-22", "2025-10-30"))
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"), NOT_FOUND, _trip(8, *FIXED)],
                 [OLD, orphan], [_listing(STALE)] + [_listing((8, *FIXED, [1266]))] * 2)
    with pytest.raises(VerificationFailed, match="outside the trip changed"):
        c.sync_trip_header(7)


def test_update_trip_unrelated_visit_lost():
    lost = _visits((101, 8, 5, *FIXED))
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"), NOT_FOUND, _trip(8, *FIXED)],
                 [OLD, lost], [_listing(STALE)] + [_listing((8, *FIXED, [1266]))] * 2)
    with pytest.raises(VerificationFailed, match="outside the trip changed"):
        c.sync_trip_header(7)


def test_update_trip_replacement_visit_downgraded():
    worse = _visits((101, 8, 3, *FIXED), (50, None, 3, "2025-10-22", "2025-10-30"))
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"), NOT_FOUND, _trip(8, *FIXED)],
                 [OLD, worse], [_listing(STALE)] + [_listing((8, *FIXED, [1266]))] * 2)
    with pytest.raises(VerificationFailed, match="visit 101 reads"):
        c.sync_trip_header(7)


def test_update_trip_hidden_or_description_change_is_caught():
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"),
                  _trip(7, *FIXED, rows=[_row(hidden=1)])], [OLD, OLD], [_listing(STALE)])
    with pytest.raises(VerificationFailed, match="regions changed"):
        c.sync_trip_header(7)
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"), _trip(7, *FIXED, description="")],
                 [OLD, OLD], [_listing(STALE)])
    with pytest.raises(VerificationFailed, match="description changed"):
        c.sync_trip_header(7)


def test_update_trip_refuses_when_trip_row_and_visit_disagree_on_quality():
    """The body carries the row's quality; sending it would rewrite the visit."""
    c, t = _sync([_trip(7, "2026-10-31", "2026-11-02", rows=[_row(quality=3)])],
                 [OLD], [_listing(STALE)])
    with pytest.raises(ApiError, match="trip row says q3"):
        c.sync_trip_header(7)
    assert not _posts(t)


def test_update_trip_refusals_send_nothing():
    c, t = _sync([_trip(7, *FIXED)], [], [])
    with pytest.raises(ApiError, match="already spans"):
        c.sync_trip_header(7)
    year_only = {**_row(), "date_from": None, "date_to": None}
    c2, t2 = _sync([_trip(7, None, None, rows=[year_only])], [], [])
    with pytest.raises(ApiError, match="year-only"):
        c2.sync_trip_header(7)
    assert not _posts(t) and not _posts(t2)


def test_sync_header_refuses_to_shorten_either_end():
    """Two trips were found longer than their regions (2026-11-23). The save deletes
    the trip, so a re-span would have thrown the longer header away for good."""
    for header in (
        ("2026-10-30", "2026-11-12"),  # starts before its region
        ("2026-10-31", "2026-11-14"),  # ends after it
        ("2026-10-30", "2026-11-14"),
        ("2026-10-30", "2026-11-02"),  # would shorten the start, lengthen the end
        ("2026-11-02", "2026-11-14"),  # would lengthen the start, shorten the end
    ):
        c, t = _sync([_trip(7, *header)], [OLD], [_listing((7, *header, [1266]))])
        with pytest.raises(ApiError, match=r"trip 7 .*shorten") as info:
            c.sync_trip_header(7)
        assert type(info.value) is ApiError and info.value.action == "trips/update-trip"
        for day in (*header, *FIXED):
            assert day in str(info.value)
        assert not _posts(t), header


def test_sync_header_refuses_when_a_row_and_its_visit_disagree_on_dates():
    """The body is built from the trip's rows; a stale row would overwrite the visit,
    and the read-back that compares them runs after the old visit is gone."""
    def one(**over):
        row = {"id": 100, "trip_id": 7, "quality": 5, "year_from": 2026, "month_from": 10,
               "day_from": 31, "year_to": 2026, "month_to": 11, "day_to": 12}
        return {"result": "OK", "data": [row | over]}

    no_from = {"year_from": None, "month_from": None, "day_from": None}
    no_to = {"year_to": None, "month_to": None, "day_to": None}
    for visits_, says in (
        (one(day_from=30), "2026-10-30"),
        (one(day_to=13), "2026-11-13"),
        (one(day_to=11), "2026-11-11"),
        (one(month_from=None, day_from=None), "2026"),  # year only
        (one(**no_from), "None"),
        (one(**no_to), "None"),
        (one(**no_from, **no_to), "None"),
    ):
        c, t = _sync([_trip(7, "2026-10-31", "2026-11-02")], [visits_], [_listing(STALE)])
        with pytest.raises(ApiError, match="trip 7, region 1266: trip row says 2026-1") as info:
            c.sync_trip_header(7)
        assert type(info.value) is ApiError and info.value.action == "trips/update-trip"
        assert "says 2026-10-31..2026-11-12, visit 100 says " in str(info.value)
        assert says in str(info.value).split("visit 100 says ")[1]
        assert not _posts(t)


def test_sync_header_reads_every_region_before_it_decides():
    """The stale row may be the second one."""
    rows = [_row(1390, 3, "2026-10-27", "2026-10-30"), _row(1232, 3, "2026-10-25", "2026-10-27")]
    c, t = _sync([_trip(7, "2026-10-27", "2026-10-29", rows=rows)],
                 [_visits((200, 7, 3, "2026-10-27", "2026-10-30")),
                  _visits((201, 7, 3, "2026-10-24", "2026-10-27"))],
                 [_listing((7, "2026-10-27", "2026-10-29", [1390, 1232]))])
    with pytest.raises(ApiError, match="region 1232"):
        c.sync_trip_header(7)
    assert not _posts(t)


def test_sync_header_refuses_a_header_or_a_row_it_cannot_read():
    """The header goes out from the ISO dates, the rows from year/month/day."""
    start = ("2026-10-31", "2026-11-02")
    for trip in (
        _trip(7, None, None),
        _trip(7, None, "2026-11-02"),
        _trip(7, "2026-10-31", None),
        _trip(7, "2026-10-31", ""),
        _trip(7, "20261031", "2026-11-02"),
        _trip(7, "2026-W44-6", "2026-11-02"),
        _trip(7, "2026-10-31", "2026-11-02T00:00:00"),
        _trip(7, 2026, "2026-11-02"),
        _trip(7, "2026-10-31", "2026-02-30"),
        _trip(7, *start, rows=[_row() | {"date_to": "20261112"}]),
        _trip(7, *start, rows=[_row() | {"date_to": "12 Nov 2026"}]),
        _trip(7, *start, rows=[_row() | {"date_to": 20261112}]),
        _trip(7, *start, rows=[_row() | {"day_to": 11}]),
        _trip(7, *start, rows=[_row() | {"year_from": 2025}]),
        _trip(7, *start, rows=[_row() | {"month_from": None, "day_from": None}]),
        _trip(7, *start, rows=[_row() | {"day_to": 32}]),
        _trip(7, *start, rows=[_row() | {"day_to": "x"}]),
        _trip(7, *start, rows=[{k: v for k, v in _row().items() if k != "day_to"}]),
    ):
        c, t = _sync([trip], [OLD], [_listing(STALE)])
        with pytest.raises(ApiError, match="trip 7") as info:
            c.sync_trip_header(7)
        assert type(info.value) is ApiError and info.value.action == "trips/update-trip"
        assert not _posts(t), trip


def test_sync_header_is_not_a_plan_op():
    """update-trip deletes the trip and its visits; a plan never deletes."""
    from pathlib import Path
    from typing import get_args

    import wanderfill.plan.apply as apply_mod
    import wanderfill.plan.model as model_mod
    from wanderfill.plan.model import validate

    kinds = ("sync_trip_header", "sync_header", "update_trip")
    assert not [k for k in get_args(model_mod.OpKind)
                if k in kinds or "sync" in k or "header" in k]
    for kind in kinds:
        assert "unknown kind" in validate(_vplan(_vop(kind, item=7)))[0]
    for mod in (apply_mod, model_mod):
        source = Path(mod.__file__).read_text()
        assert "sync_trip_header" not in source and "update-trip" not in source


class YearListing(SequencedTransport):
    """``get-trips-for-year-app`` answered per year, and differently after the save."""

    def __init__(self, sequences, before, after):
        super().__init__(sequences)
        self.before, self.after = before, after

    def webapi(self, action, **fields):
        if action != "trips/get-trips-for-year-app":
            return super().webapi(action, **fields)
        self.sent.append((action, fields))
        trips = self.after if _posts(self) else self.before
        return _listing(*(t for years, t in trips if fields["year"] in years))


def test_sync_header_across_new_year():
    """The old and the new header can be in different years, and nothing says
    which of its years the server lists a trip under."""
    def case(old, new, before, after, get_trip=NOT_FOUND):
        row = [_row(a=new[0], b=new[1])]
        t = YearListing({
            "trips/get-trip": [_trip(7, *old, rows=row), get_trip, _trip(8, *new, rows=row)],
            "quickEnter/get-visits-to-region": [_visits((100, 7, 5, *new)),
                                                _visits((101, 8, 5, *new))],
        }, [(years, (7, *old, [1266])) for years in before],
           [(years, (tid, *(old if tid == 7 else new), [1266])) for tid, years in after])
        return NomadMania(token="fake", transport=t), t

    into = (("2026-12-28", "2026-12-31"), ("2026-12-28", "2027-01-05"))
    back = (("2027-01-02", "2027-01-05"), ("2026-12-30", "2027-01-05"))
    for old, new, was in ((*into, {2026}), (*back, {2027})):
        for now in ({2026}, {2027}, {2026, 2027}):  # by start, by end, or under both
            c, t = case(old, new, [was], [(8, now)])
            out = c.sync_trip_header(7)
            assert (out["trip_id"], out["visits"]) == (8, {1266: 101}), (old, now)
            assert len(_posts(t)) == 1

    # a span of three years, the old trip listed only under the middle one
    old, new = ("2025-02-01", "2025-03-01"), ("2024-12-30", "2026-01-02")
    c, t = case(old, new, [{2025}], [(7, {2025}), (8, {2024})])
    with pytest.raises(VerificationFailed, match="gone but the listing still has it"):
        c.sync_trip_header(7)
    c, t = case(old, new, [{2025}], [(8, {2025})])
    assert c.sync_trip_header(7)["trip_id"] == 8

    # two different new trips are still two, whatever years they are listed under
    c, t = case(*into, [{2026}], [(8, {2026, 2027}), (9, {2026, 2027})])
    with pytest.raises(VerificationFailed, match="2 NEW trip"):
        c.sync_trip_header(7)


def test_update_trip_write_error_is_not_resent_and_reports_candidates():
    class Boom(SequencedTransport):
        def webapi(self, action, **fields):
            if action == "trips/update-trip":
                self.sent.append((action, fields))
                raise ApiError(action, "no answer", {})
            return super().webapi(action, **fields)

    t = Boom({"trips/get-trip": [_trip(7, "2026-10-31", "2026-11-02")],
              "quickEnter/get-visits-to-region": [OLD],
              "trips/get-trips-for-year-app": [_listing(STALE), _listing((8, *FIXED, [1266]))]})
    with pytest.raises(ApiError) as info:
        NomadMania(token="fake", transport=t).sync_trip_header(7)
    assert len(_posts(t)) == 1
    assert info.value.replacement_candidates == [8]


def test_update_trip_whose_readback_errors_is_a_failed_verification():
    """The trip may already be gone and re-created; "the server refused" is wrong."""
    error = {"result": "ERROR", "result_description": "Internal error."}
    start = _trip(7, "2026-10-31", "2026-11-02")
    for get_trip, visits_, listings in (
        ([start, error], [OLD], [_listing(STALE)]),
        ([start, {"result": "OK"}], [OLD], [_listing(STALE)]),
        ([start, {"result": "OK", "trip": {"id": 7}}], [OLD], [_listing(STALE)]),
        ([start, NOT_FOUND], [OLD], [_listing(STALE), error]),
        ([start, NOT_FOUND], [OLD], [_listing(STALE), _listing(), error]),
        ([start, NOT_FOUND, error], [OLD],
         [_listing(STALE)] + [_listing((8, *FIXED, [1266]))] * 2),
        ([start, _trip(7, *FIXED)], [OLD, error], [_listing(STALE)]),
    ):
        c, t = _sync(get_trip, visits_, listings)
        with pytest.raises(VerificationFailed):
            c.sync_trip_header(7)
        assert len(_posts(t)) == 1


def test_update_trip_gone_answering_unauthorised_is_followed():
    new = _visits((101, 8, 5, *FIXED), (50, None, 3, "2025-10-22", "2025-10-30"))
    c, _ = _sync([_trip(7, "2026-10-31", "2026-11-02"),
                  {"result": "ERROR", "result_description": "Unauthorised. "}, _trip(8, *FIXED)],
                 [OLD, new], [_listing(STALE)] + [_listing((8, *FIXED, [1266]))] * 2)
    assert c.sync_trip_header(7)["trip_id"] == 8


def test_update_trip_two_regions_span_min_to_max():
    rows = [_row(1390, 3, "2026-10-27", "2026-10-30"), _row(1232, 3, "2026-10-25", "2026-10-27")]
    v1390 = _visits((200, 7, 3, "2026-10-27", "2026-10-30"))
    v1232 = _visits((201, 7, 3, "2026-10-25", "2026-10-27"))
    c, t = _sync([_trip(7, "2026-10-27", "2026-10-29", rows=rows),
                  _trip(7, "2026-10-25", "2026-10-30", rows=rows)],
                 [v1390, v1232, v1390, v1232], [_listing((7, "2026-10-27", "2026-10-29",
                                                          [1390, 1232]))])
    c.sync_trip_header(7)
    (fields,) = _posts(t)
    assert (fields["date_from"], fields["date_to"]) == ("2026-10-25", "2026-10-30")


def test_update_trip_is_a_single_attempt_write():
    assert Transport._is_write("trips/update-trip")


class TripServer(FakeTransport):
    """Holds trip 7 and its visits, and answers update-trip the way the server did:
    the trip and its visits are gone, and new ones with new ids are there."""

    def __init__(self, rows=None, header=("2026-10-31", "2026-11-02"), description="kept",
                 on_save=None, on_read=None):
        super().__init__({"user/status-quick": {"result": "OK", "uid": 79597}})
        rows = rows or [_row()]
        for r in rows:
            r.setdefault("region_name", f"Region number {r['id']}")
        self.trips = {7: _trip(7, *header, rows=rows, description=description)["trip"]}
        self.visits = {}
        for n, r in enumerate(rows):
            mine = (100 + n, 7, r["quality"], r["date_from"] or "2026-10-31",
                    r["date_to"] or "2026-11-12")
            other = (50 + n, None, 3, "2025-10-22", "2025-10-30")
            self.visits[r["id"]] = _visits(mine, other)["data"]
        self.on_save, self.on_read = on_save, on_read

    def webapi(self, action, **fields):
        if action == "user/status-quick":
            return super().webapi(action, **fields)
        self.sent.append((action, fields))
        if action == "trips/update-trip":
            return (self.on_save or TripServer.replace)(self, fields)
        if self.on_read:
            self.on_read(self, action, fields)
        if action == "trips/get-trip":
            if fields["trip_id"] not in self.trips:
                raise ApiError(action, "Trip not found.", NOT_FOUND)
            return json.loads(json.dumps({"result": "OK", "trip": self.trips[fields["trip_id"]]}))
        if action == "quickEnter/get-visits-to-region":
            return {"result": "OK", "data": [dict(v) for v in self.visits[fields["region"]]]}
        assert action == "trips/get-trips-for-year-app", action
        return _listing(*((t["id"], t["date_from"], t["date_to"], [r["id"] for r in t["regions"]])
                          for t in self.trips.values()))

    def replace(self, fields):
        old = self.trips.pop(fields["trip_id"])
        self.trips[8] = old | {"id": 8, "date_from": fields["date_from"],
                               "date_to": fields["date_to"]}
        for rows in self.visits.values():
            for v in rows:
                if v["trip_id"] == 7:
                    v.update(id=v["id"] + 1000, trip_id=8)
        return {"result": "OK", "said": "whatever it says"}


def _sync_cli(monkeypatch, tmp_path, t, *argv, trip="7"):
    from wanderfill.cli import main as cli

    monkeypatch.setattr(cli, "_client", lambda args: NomadMania(token="fake", transport=t))
    return cli.main(["sync-header", trip, *argv, "--workdir", str(tmp_path)])


SYNC = ("--confirm", "--reason", "owner asked for trip 7")
TWO = (_row(1390, 3, "2026-10-27", "2026-10-30"), _row(1232, 4, "2026-10-25", "2026-10-27"))


def _two():
    return TripServer([dict(r) for r in TWO], ("2026-10-27", "2026-10-29"))


def test_sync_header_refuses_when_the_trip_changed_since_the_preview():
    """What was confirmed is the trip that was shown, not whatever has its id at send time."""
    def row(**over):
        return lambda t: t.trips[7]["regions"][0].update(over)

    def both(t, **over):  # row and visit agree with each other, not with the preview
        t.trips[7]["regions"][0].update(over, date_to=f"2026-11-{over.get('day_to', 12):02}")
        t.visits[1266][0].update({k: v for k, v in over.items() if k != "date_to"})

    def another_region(t):
        t.trips[7]["regions"].append(_row(5))
        t.visits[5] = _visits((105, 7, 5, *FIXED))["data"]

    for change in (
        lambda t: t.trips[7].update(date_to="2026-11-03"),
        lambda t: t.trips[7].update(date_from="2026-11-01"),
        lambda t: t.trips[7].update(date_to="2026-11-12"),    # already right by now
        lambda t: t.trips[7].update(date_to="2026-11-30"),    # longer than its region by now
        lambda t: t.trips[7].update(date_to=None),
        lambda t: t.trips[7].update(description="rewritten"),
        lambda t: t.trips[7].update(regions=[]),
        row(hidden=1),
        row(quality=2),                                       # and not its visit
        row(date_to=None),
        lambda t: both(t, quality=2),
        lambda t: both(t, day_to=13),
        another_region,
        lambda t: t.visits[1266][0].update(id=999),
        lambda t: t.visits[1266][0].update(trip_id=None),
        lambda t: t.visits[1266].append(dict(t.visits[1266][0], id=999)),
    ):
        t = TripServer()
        c = NomadMania(token="fake", transport=t)
        shown = c.preview_trip_header(7)
        change(t)
        with pytest.raises(PrecisionLoss, match=r"trip 7 .*preview"):
            c.sync_trip_header(7, expect_rows=shown.expect_rows)
        assert not _posts(t)
    # ...and the same preview of an unchanged trip is good, as often as it is read
    t = TripServer()
    c = NomadMania(token="fake", transport=t)
    shown = c.preview_trip_header(7).expect_rows
    assert shown == c.preview_trip_header(7).expect_rows
    assert c.sync_trip_header(7, expect_rows=shown)["trip_id"] == 8


def test_sync_header_expect_rows_is_the_trip_and_then_every_region_with_its_visit():
    t = _two()
    c = NomadMania(token="fake", transport=t)
    assert c.preview_trip_header(7).expect_rows == [
        ("2026-10-27", "2026-10-29", "kept"),
        (1232, 4, 0, "2026-10-25", "2026-10-27", 101),
        (1390, 3, 0, "2026-10-27", "2026-10-30", 100),
    ]
    # an empty or cut-short one is a trip that does not match, never "no check"
    for wrong in ([], c.preview_trip_header(7).expect_rows[:-1]):
        with pytest.raises(PrecisionLoss):
            c.sync_trip_header(7, expect_rows=wrong)
    assert not _posts(t)


def test_sync_header_dry_run_prints_that_every_id_will_change(monkeypatch, tmp_path, capsys):
    t = _two()
    assert _sync_cli(monkeypatch, tmp_path, t) == 0
    said = capsys.readouterr().out
    assert "2026-10-27 → 2026-10-29" in said and "2026-10-25 → 2026-10-30" in said
    for part in ("trip 7", "visit 100", "visit 101", "1390", "1232", "Region number 1390",
                 "2026-10-27 → 2026-10-30", "2026-10-25 → 2026-10-27", "quality 3", "quality 4",
                 "deletes", "EVERY trip and visit id", "rebuilt", "Dry run. Nothing was sent"):
        assert part in said, part
    assert "visit 50" not in said, "visits outside the trip keep their ids"
    assert not _posts(t) and _journal_lines(tmp_path) == []


def _year_only():
    row = _row() | {"date_from": None, "date_to": None}
    return TripServer([row], (None, None))


def _with(change, *args, **kw):
    t = TripServer(*args, **kw)
    change(t)
    return t


@pytest.mark.parametrize("confirm", [(), SYNC], ids=["preview", "confirmed"])
@pytest.mark.parametrize("make, says", [
    (lambda: TripServer(header=FIXED), "already spans"),
    (lambda: TripServer(header=("2026-10-30", "2026-11-12")), "shorten"),
    (_year_only, "year-only"),
    (lambda: _with(lambda t: t.trips[7].update(regions=[])), "no regions"),
    (lambda: _with(lambda t: t.visits[1266][0].update(quality=3)), "trip row says q5"),
    (lambda: _with(lambda t: t.visits[1266][0].update(day_to=13)), "visit 100 says"),
    (lambda: _with(lambda t: t.visits[1266].pop(0)), "exactly one visit"),
    (lambda: _with(lambda t: t.visits[1266].append(dict(t.visits[1266][0], id=9))),
     "exactly one visit"),
    (lambda: _with(lambda t: t.trips[7]["regions"].append(_row())), "exactly one visit"),
    (lambda: _with(lambda t: t.trips.clear()), "Trip not found"),
    (lambda: _with(lambda t: t.trips[7].update(regions="none")), "cannot be read"),
    (lambda: _with(lambda t: t.trips[7]["regions"][0].update(quality=None)), "cannot be read"),
    (lambda: _with(lambda t: t.trips[7]["regions"][0].pop("id")), "cannot be read"),
    (lambda: _with(lambda t: t.visits[1266][0].pop("quality")), "cannot be read"),
])
def test_sync_header_previews_every_refusal(monkeypatch, tmp_path, capsys, make, says, confirm):
    """Nobody is asked to confirm a save that is then refused."""
    t = make()
    assert _sync_cli(monkeypatch, tmp_path, t, *confirm) == 2
    said = capsys.readouterr().out
    assert says in said and "nothing was sent" in said and "Re-run with --confirm" not in said
    assert not _posts(t) and _journal_lines(tmp_path) == []


def test_sync_header_confirm_needs_a_reason(monkeypatch, tmp_path, capsys):
    for reason in ((), ("--reason", ""), ("--reason", "  ")):
        t = TripServer()
        assert _sync_cli(monkeypatch, tmp_path, t, "--confirm", *reason) == 2
        assert "--reason" in capsys.readouterr().out
        assert not _posts(t) and _journal_lines(tmp_path) == []


def test_sync_header_journals_the_whole_trip_before_the_write(monkeypatch, tmp_path, capsys):
    at_send = []

    def save(t, fields):
        at_send.extend(_journal_lines(tmp_path))
        return t.replace(fields)

    t = _two()
    t.on_save = save
    was = json.loads(json.dumps(t.trips[7]))
    assert _sync_cli(monkeypatch, tmp_path, t, *SYNC) == 0
    said = capsys.readouterr().out
    (opened,) = at_send
    assert opened["phase"] == "open" and opened["kind"] == "sync_trip_header"
    assert opened["key"] == "sync_trip_header:7" and "owner asked for trip 7" in opened["label"]
    assert opened["snapshot"]["trip"] == was, "header, description and every row as read"
    assert sorted((v["id"], v["region"], v["quality"], v["date_from"], v["date_to"], v["trip_id"])
                  for v in opened["snapshot"]["visits"]) == [
        (50, 1390, 3, "2025-10-22", "2025-10-30", None),
        (51, 1232, 3, "2025-10-22", "2025-10-30", None),
        (100, 1390, 3, "2026-10-27", "2026-10-30", 7),
        (101, 1232, 4, "2026-10-25", "2026-10-27", 7),
    ]
    _, done = _journal_lines(tmp_path)
    assert done["phase"] == "done" and done["error"] == "" and done["key"] == opened["key"]
    assert (done["result"]["old_trip_id"], done["result"]["trip_id"]) == (7, 8)
    assert sorted(map(tuple, (v.values() for v in done["result"]["visits"]))) == [
        (1232, 101, 1101), (1390, 100, 1100)]
    assert [sorted(v) for v in done["result"]["visits"]] == [["new", "old", "region"]] * 2
    for part in ("trip 7 → 8", "visit 100 → 1100", "visit 101 → 1101", "record", "deletes-79597"):
        assert part in said.lower(), part
    assert len(_posts(t)) == 1


def test_sync_header_changed_between_the_preview_and_the_lock_sends_nothing(
        monkeypatch, tmp_path, capsys):
    seen = []

    def somebody_else(t, action, fields):
        seen.append(action)
        if action == "trips/get-trip" and seen.count(action) == 2:
            t.trips[7]["description"] = "rewritten"

    t = TripServer(on_read=somebody_else)
    assert _sync_cli(monkeypatch, tmp_path, t, *SYNC) == 1
    assert not _posts(t)
    assert "refused, nothing changed" in capsys.readouterr().out
    lines = _journal_lines(tmp_path)
    assert [e["phase"] for e in lines] == ["open", "done"] and "preview" in lines[1]["error"]


def _lost_after(land):
    def save(t, fields):
        from wanderfill.api.errors import UnknownWriteOutcome
        if land:
            t.replace(fields)
        raise UnknownWriteOutcome("no answer")
    return save


def _ok_but(then):
    def save(t, fields):
        t.replace(fields)
        then(t)
        return {"result": "OK"}
    return save


def _downgrade(t):
    t.visits[1266][0].update(quality=3)


def _blind(t):
    def on_read(t, action, fields):
        raise ApiError(action, "Internal error.", {})
    t.on_read = on_read


def _blind_to_visits(t):
    def on_read(t, action, fields):
        if action == "quickEnter/get-visits-to-region":
            raise ApiError(action, "Internal error.", {})
    t.on_read = on_read


@pytest.mark.parametrize("save, found", [
    (_lost_after(land=True), "[8]"),
    (_lost_after(land=False), "none found"),
    (_ok_but(_downgrade), "[8]"),
    (_ok_but(_blind_to_visits), "[8]"),
    (_ok_but(_blind), "unreadable"),
    (_ok_but(lambda t: t.trips.pop(8)), "none found"),
    (_ok_but(lambda t: t.trips.update({9: t.trips[8] | {"id": 9}})), "[8, 9]"),
], ids=["lost, landed", "lost", "downgraded", "visits unread", "nothing read", "no new trip",
        "two new trips"])
def test_sync_header_that_cannot_prove_the_save_stays_open_and_says_what_is_known(
        monkeypatch, tmp_path, capsys, save, found):
    """The trip may be gone: what is left of it is the saved copy and the replacement."""
    from wanderfill.plan.apply import CLOSE_BY_HAND

    t = TripServer(on_save=save)
    assert _sync_cli(monkeypatch, tmp_path, t, *SYNC) == 1
    said = capsys.readouterr().out
    assert [e["phase"] for e in _journal_lines(tmp_path)] == ["open"]
    (line,) = [x for x in said.splitlines() if "replacement" in x]
    assert found in line
    assert CLOSE_BY_HAND in said and "snapshot" in said
    assert str(tmp_path / "deletes-79597.ndjson") in said
    assert "done; server confirms" not in said
    assert [a for a, _ in t.sent if Transport._is_write(a)] == ["trips/update-trip"]
    # and nothing more is sent for the account until it is closed by hand
    again = TripServer()
    assert _sync_cli(monkeypatch, tmp_path, again, *SYNC) == 2
    assert CLOSE_BY_HAND in capsys.readouterr().out and not _posts(again)
    assert _run_delete(monkeypatch, tmp_path, FakeClient(), "dare", "1142", *SYNC) == 2


def test_sync_header_interrupted_in_the_save_stays_open(monkeypatch, tmp_path, capsys):
    def save(t, fields):
        raise KeyboardInterrupt

    t = TripServer(on_save=save)
    assert _sync_cli(monkeypatch, tmp_path, t, *SYNC) == 130
    assert [e["phase"] for e in _journal_lines(tmp_path)] == ["open"]
    said = capsys.readouterr().out
    assert "not looked for" in said and all(x in said for x in CLOSING)
    assert len(_posts(t)) == 1


def test_sync_header_whose_done_line_will_not_go_in_still_prints_the_new_ids(
        monkeypatch, tmp_path, capsys):
    """The old ids are dead by then, and the journal does not have the new ones."""
    from wanderfill.plan import apply as ap

    def full(self, op, result, error=""):
        raise OSError("disk full")

    monkeypatch.setattr(ap.Journal, "note", full)
    with pytest.raises(OSError, match="disk full"):
        _sync_cli(monkeypatch, tmp_path, TripServer(), *SYNC)
    said = capsys.readouterr().out
    assert "trip 7 → 8" in said and "visit 100 → 1100" in said
    assert "not known" not in said and all(x in said for x in CLOSING)


def test_sync_header_refused_by_the_server_closes_only_if_the_trip_is_still_as_it_was(
        monkeypatch, tmp_path, capsys):
    """The save is a deletion and a creation; an ERROR may come from between them."""
    from wanderfill.api.errors import AuthError, TransportError

    def error(exc, first=lambda t, fields: None):
        def save(t, fields):
            first(t, fields)
            raise exc
        return save

    said_error = ApiError("trips/update-trip", "Database error.", {})
    gone = error(said_error, lambda t, f: t.trips.pop(7))
    visits_gone = error(said_error, lambda t, f: t.visits[1266].pop(0))
    other_visits_gone = error(said_error, lambda t, f: t.visits[1266].pop(1))
    landed = error(said_error, lambda t, f: t.replace(f))
    both = error(said_error, lambda t, f: t.trips.update(
        {8: t.trips[7] | {"id": 8, "date_to": FIXED[1]}}))
    blind = error(said_error, lambda t, f: _blind(t))
    rejected = AuthError("server rejected the token (403)")
    for save, phases in (
        (error(said_error), ["open", "done"]),
        (error(TransportError("refused: 400")), ["open", "done"]),
        (error(rejected), ["open", "done"]),
        (error(rejected, lambda t, f: _blind(t)), ["open", "done"]),
        (error(rejected, lambda t, f: t.replace(f)), ["open"]),
        (gone, ["open"]), (visits_gone, ["open"]), (other_visits_gone, ["open"]),
        (landed, ["open"]), (both, ["open"]), (blind, ["open"]),
    ):
        for f in tmp_path.glob("deletes-*"):
            f.unlink()
        t = TripServer(on_save=save)
        assert _sync_cli(monkeypatch, tmp_path, t, *SYNC) == 1
        said = capsys.readouterr().out
        lines = _journal_lines(tmp_path)
        assert [e["phase"] for e in lines] == phases, said
        assert ("nothing changed" in said) == (phases != ["open"])
        assert ("replacement" in said) == (phases == ["open"])
        assert len(_posts(t)) == 1


def test_sync_header_holds_the_account_lock_and_meets_an_open_apply(monkeypatch, tmp_path, capsys):
    from wanderfill.plan.apply import account_lock

    t = TripServer()
    with account_lock([tmp_path], 79597):
        assert _sync_cli(monkeypatch, tmp_path, t, *SYNC) == 2
    assert "another run" in capsys.readouterr().err
    _open_line(tmp_path / "journal-79597-aaaaaaaaaaaa.ndjson")
    assert _sync_cli(monkeypatch, tmp_path, t, *SYNC) == 2
    assert not _posts(t) and _journal_lines(tmp_path) == []


def test_sync_header_takes_exactly_one_trip_id(monkeypatch, tmp_path, capsys):
    """Rule 2 names one trip per run. No list, no range, no flag that widens it."""
    import argparse

    from wanderfill.cli import main as cli

    parser = cli.build_parser()
    (sub,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    actions = {a.dest: a for a in sub.choices["sync-header"]._actions}
    assert set(actions) == {"help", "trip_id", "confirm", "reason", "workdir"}
    assert actions["trip_id"].nargs is None and actions["trip_id"].type is int
    assert actions["confirm"].const is True and actions["confirm"].nargs == 0
    assert not actions["reason"].required
    for argv in ([], ["7", "8"], ["7,8"], ["7-8"], ["all"], ["7", "--all"], ["trip", "7"]):
        with pytest.raises(SystemExit) as info:
            parser.parse_args(["sync-header", *argv])
        assert info.value.code == 2
    assert parser.parse_args(["sync-header", "7"]).trip_id == 7
    kind = next(a for a in sub.choices["delete"]._actions if a.dest == "kind")
    assert list(kind.choices) == ["visit", "trip", "region", "dare", "kye"]
    capsys.readouterr()
    said = " ".join((parser.epilog + cli.__doc__).split())
    assert "except through `delete`:" not in said
    assert said.count("sync-header") >= 3


def test_rule_2_names_the_sync_header_exception_and_its_limits():
    from pathlib import Path

    text = (Path(__file__).parent.parent / "AGENTS.md").read_text()
    rule = " ".join(text.split("\n2. ")[1].split("\n3. ")[0].split())
    for part in ("`wanderfill sync-header <trip_id>`", "approved by the owner on 2026-11-24",
                 "One trip per run", "trip id", "preview", "never as a plan op",
                 "never in a loop", "deletes the trip and its visits"):
        assert part in rule, part
    # the rest of it says what it said
    assert rule.startswith(
        "**Never delete anything you did not create in this session.** Not visits, not trips, "
        "not ticks. If something looks redundant, report it and stop. The one exception is "
        "`wanderfill delete <kind> <id>`, and only when the account owner has named the specific "
        "id and said it is wrong. One id per run, `--confirm` from them, and the preview shown "
        'first. "This region looks wrong" is not an id, a list of ids is not one id, and a '
        "deletion is never a plan op.")


def test_sync_trip_header_has_no_caller_but_the_sync_header_command():
    """Not a plan, not an apply, not a loop: the approved exception is this one command."""
    import ast
    from pathlib import Path

    import wanderfill

    root = Path(wanderfill.__file__).parent
    uses, posts = [], []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for n in ast.walk(fn):
                if isinstance(n, ast.Attribute) and n.attr == "sync_trip_header":
                    uses.append((path.name, fn.name))
                if isinstance(n, ast.Call) and any(
                        isinstance(a, ast.Constant) and a.value == "trips/update-trip"
                        for a in n.args) and getattr(n.func, "attr", "") == "webapi":
                    posts.append((path.name, fn.name))
        whole = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
                 and n.value == "sync_trip_header"]
        # the journal's kind; getattr(client, "sync_trip_header") would be a second one
        assert len(whole) <= (path.name == "main.py"), path
        assert not any(isinstance(loop, (ast.For, ast.While, ast.comprehension)) and any(
            isinstance(n, ast.Attribute) and n.attr == "sync_trip_header" for n in ast.walk(loop))
            for loop in ast.walk(tree)), path
    assert set(uses) == {("main.py", "cmd_sync_header"), ("main.py", "run_sync")}
    assert len(uses) == 2, "one call, seen from the command and from the closure inside it"
    assert posts == [("client.py", "sync_trip_header")]


# --------------------------------------------------------------------------
# a malformed plan is refused whole, before its first request
# --------------------------------------------------------------------------

def _vplan(*ops, **kw):
    return Plan(account=7, ops=list(ops), basis={"fingerprint": "x"}, **kw)


def _vop(kind="add_visit", **kw):
    return Op(kind=kind, bucket="apply", **({"label": "the op"} | kw))


def _vtrip(**kw):
    base = {"date_from": "2024-01-01", "date_to": "2024-01-05",
            "regions": [{"id": 1, "quality": 3}, {"id": 2, "quality": 3}]}
    return _vop("create_trip", **(base | kw))


REJECTED = {
    "schema 99": lambda: _vplan(_vop(region=1, quality=3), schema=99),
    "unknown kind": lambda: _vplan(_vop("delete_visit", region=1)),
    "unknown bucket": lambda: _vplan(Op(kind="mark_dare", bucket="maybe", region=1)),
    "add_visit without region": lambda: _vplan(_vop(quality=3)),
    "add_visit without quality": lambda: _vplan(_vop(region=1)),
    "quality 0": lambda: _vplan(_vop(region=1, quality=0)),
    "quality 7": lambda: _vplan(_vop(region=1, quality=7)),
    "a bool is not an id": lambda: _vplan(_vop(region=True, quality=3)),
    "update_visit without visit_id": lambda: _vplan(_vop("update_visit", region=1, quality=3)),
    "trip with no regions": lambda: _vplan(_vtrip(regions=[])),
    "trip region twice": lambda: _vplan(_vtrip(regions=[{"id": 1, "quality": 3}] * 2)),
    "trip region without id": lambda: _vplan(_vtrip(regions=[{"quality": 3}])),
    "trip region without quality": lambda: _vplan(_vtrip(regions=[{"id": 1}])),
    "trip without header dates": lambda: _vplan(_vtrip(date_from=None)),
    "trip header a bare year": lambda: _vplan(_vtrip(date_from="2024", date_to="2024")),
    "trip region month without day": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024, "month_from": 2}])),
    "trip region month without year": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "month_from": 2, "day_from": 3}])),
    "trip region 30 February": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024, "month_from": 2, "day_from": 30}])),
    "trip region ends before it starts": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024, "month_from": 3, "day_from": 2,
         "year_to": 2024, "month_to": 3, "day_to": 1}])),
    "trip region with a start and no end": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024, "month_from": 1, "day_from": 2}])),
    "trip region with an end and no start": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_to": 2024, "month_to": 1, "day_to": 2}])),
    "trip region with a start year only": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024, "year_to": ""}])),
    "trip region from a day to a year": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024, "month_from": 1, "day_from": 2,
         "year_to": 2024}])),
    "trip region from a year to a day": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024,
         "year_to": 2024, "month_to": 1, "day_to": 2}])),
    "mark_kye without item": lambda: _vplan(_vop("mark_kye")),
    "mark_tcc without item": lambda: _vplan(_vop("mark_tcc")),
    "tick_series without item": lambda: _vplan(_vop("tick_series", series=22)),
    "tick_series without series": lambda: _vplan(_vop("tick_series", item=5)),
    "mark_dare without region": lambda: _vplan(_vop("mark_dare")),
    "unparseable date": lambda: _vplan(_vop(region=1, quality=3, date_from="March 2024")),
    "start after end": lambda: _vplan(_vop(region=1, quality=3, date_from="2024-05-02",
                                     date_to="2024-05-01")),
    "year after year": lambda: _vplan(_vop(region=1, quality=3, date_from="2025", date_to="2024")),
    "year after a date": lambda: _vplan(_vop(region=1, quality=3, date_from="2025",
                                       date_to="2024-05-01")),
    "year past what YearOnly takes": lambda: _vplan(_vop(region=1, quality=3, date_from="2101")),
    "year before what YearOnly takes": lambda: _vplan(
        _vop(region=1, quality=3, date_from="1899", date_to="1899")),
    "a bad date on a mark_dare": lambda: _vplan(_vop("mark_dare", region=1, date_from="bad")),
    "a date that is not text": lambda: _vplan(_vop("mark_kye", item=1, date_to=2024)),
    "trip region year out of range": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2101}])),
    "trip region year huge": lambda: _vplan(_vtrip(regions=[
        {"id": 1, "quality": 3, "year_from": 2024, "month_from": 10**30, "day_from": 1}])),
    "allow_vaguer as text": lambda: _vplan(_vop(region=1, quality=3, allow_vaguer="false")),
    "an empty string as undated": lambda: _vplan(_vop(region=1, quality=3, date_from="")),
    "a compact date": lambda: _vplan(_vop(region=1, quality=3, date_from="20240101")),
    "an ISO week date": lambda: _vplan(_vop(region=1, quality=3, date_from="2024-W01-1")),
    "a day with no zero padding": lambda: _vplan(
        _vop(region=1, quality=3, date_from="2024-1-1")),
    "a year in other digits": lambda: _vplan(
        _vop(region=1, quality=3, date_from="\u0662\u0660\u0662\u0664")),
    "a date in other digits": lambda: _vplan(_vop(
        region=1, quality=3, date_from="\u0662\u0660\u0662\u0664-01-01")),
    "a date with a space": lambda: _vplan(_vop(region=1, quality=3, date_from=" 2024-01-01")),
    "a trip header, compact": lambda: _vplan(_vtrip(date_from="20240101")),
    "same add, other quality": lambda: _vplan(
        _vop(region=1, quality=2, date_from="2024-01-01", date_to="2024-01-02"),
        _vop(region=1, quality=3, date_from="2024-01-01", date_to="2024-01-02")),
    "one visit updated twice": lambda: _vplan(
        _vop("update_visit", region=1, visit_id=9, quality=3, date_from="2024"),
        _vop("update_visit", region=1, visit_id=9, quality=4, date_from="2025")),
    "one key twice": lambda: _vplan(_vop("mark_dare", region=1), _vop("mark_dare", region=1)),
}


@pytest.mark.parametrize("name", REJECTED)
def test_plan_validation_rejects(name):
    from wanderfill.plan.model import validate

    plan = REJECTED[name]()
    problems = validate(plan)
    assert problems, name
    if name not in ("schema 99",):
        assert any("ops[" in m for m in problems), problems


def test_plan_validation_names_the_op():
    from wanderfill.plan.model import validate

    (msg,) = validate(_vplan(_vop("mark_dare", region=1), _vop("mark_kye", label="Q 12")))
    assert "ops[1]" in msg and "mark_kye" in msg and "Q 12" in msg


def test_plan_validation_accepts_year_only_and_undated_visits():
    from wanderfill.plan.model import validate

    plan = _vplan(
        _vop(region=1, quality=1),
        _vop(region=2, quality=6, date_from="2013", date_to="2013"),
        _vop(region=3, quality=3, date_from="2013", date_to="2014-02-01"),
        _vop("update_visit", region=4, visit_id=8, quality=3, date_from="2020-01-01",
           date_to="2020-01-01"),
        _vtrip(regions=[{"id": 5, "quality": 2},
                       {"id": 6, "quality": 3, "year_from": 2024, "year_to": 2024},
                       {"id": 7, "quality": 3, "year_from": "2024", "month_from": "1",
                        "day_from": "2", "year_to": 2024, "month_to": 1, "day_to": 2}]),
        _vop("mark_dare", region=9), _vop("mark_kye", item=9), _vop("mark_tcc", item=3),
        _vop("tick_series", series=22, item=5),
        Op(kind="add_visit", bucket="review"),  # not executed, so not checked for content
    )
    assert validate(plan) == []


def test_an_invalid_late_op_sends_nothing(tmp_path):
    """A malformed op was found only when reached, after the earlier writes had landed."""
    from wanderfill.api.errors import PlanInvalid
    from wanderfill.plan.apply import apply_plan

    c, t = client({"user/status-quick": {"result": "OK", "uid": 7}})
    plan = _vplan(_vop("mark_dare", region=1), _vop(region=2))  # the add_visit has no quality
    with pytest.raises(PlanInvalid, match=r"ops\[1\]"):
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert t.sent == []
    with pytest.raises(PlanInvalid):  # a dry run reports it too
        apply_plan(c, plan, workdir=tmp_path)
    assert t.sent == [] and list(tmp_path.iterdir()) == []


def test_dry_run_apply_reports_invalid_plans(monkeypatch, tmp_path, capsys):
    from wanderfill.cli import main as cli

    c, t = client()
    monkeypatch.setattr(cli, "_client", lambda args: c)
    path = _vplan(_vop(region=2)).save(tmp_path / "p.json")
    assert cli.main(["apply", str(path), "--workdir", str(tmp_path / "w")]) == 1
    assert "ops[0]" in capsys.readouterr().out and t.sent == []
    good = _vplan(_vop("mark_dare", region=1)).save(tmp_path / "good.json")
    assert cli.main(["apply", str(good), "--workdir", str(tmp_path / "w")]) == 0


def test_check_lists_validation_problems_as_blockers(monkeypatch, tmp_path, capsys):
    from wanderfill.cli import main as cli

    c, t = client()
    monkeypatch.setattr(cli, "_client", lambda args: c)
    path = _vplan(_vop(region=2)).save(tmp_path / "p.json")
    assert cli.main(["check", str(path), "--workdir", str(tmp_path)]) == 1
    assert "ops[0]" in capsys.readouterr().out and t.sent == []


def test_show_still_prints_an_invalid_plan_and_says_why(tmp_path, capsys):
    from wanderfill.cli import main as cli

    path = _vplan(_vop(region=2), schema=99).save(tmp_path / "p.json")
    assert cli.main(["show", str(path), "-v"]) == 0
    out = capsys.readouterr().out
    assert "schema 99" in out and "ops[0]" in out


@pytest.mark.parametrize("text", [
    "{", "[]", '{"ops": []}', '{"account": 7, "ops": {}}', '{"account": 7, "ops": [3]}',
    '{"account": 7, "ops": [{"kind": "mark_dare", "sneaky": 1}]}',
    '{"account": 7, "surprise": 1}', '{"account": 7, "ops": [{"bucket": "apply"}]}',
])
def test_a_plan_file_that_cannot_load_says_so_plainly(tmp_path, text):
    from wanderfill.api.errors import PlanInvalid

    path = tmp_path / "p.json"
    path.write_text(text)
    with pytest.raises(PlanInvalid) as e:
        Plan.load(path)
    assert str(path) in str(e.value)


@pytest.mark.parametrize("late", [
    pytest.param({"kind": "add_visit", "region": 2, "quality": 3, "date_from": "2101"},
                 id="year out of YearOnly's range"),
    pytest.param({"kind": "mark_dare", "region": 3, "date_from": "bad"},
                 id="a bad date on a kind that does not use it"),
    pytest.param({"kind": "add_visit", "region": 2, "quality": 3, "allow_vaguer": "false"},
                 id="allow_vaguer as text"),
])
def test_a_late_op_execution_would_choke_on_sends_nothing(tmp_path, late):
    """Execution parses every op's dates; validation used to parse some of them."""
    from wanderfill.api.errors import PlanInvalid
    from wanderfill.plan.apply import apply_plan

    c, t = client({"user/status-quick": {"result": "OK", "uid": 7}})
    plan = _vplan(_vop("mark_dare", region=1), Op(bucket="apply", **late))
    with pytest.raises(PlanInvalid, match=r"ops\[1\]"):
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert t.sent == []


@pytest.mark.parametrize("value,ok", [
    ("false", False), ("true", False), (0, False), (1, False), (None, False),
    (True, True), (False, True),
])
def test_allow_vaguer_must_be_a_real_bool(value, ok):
    """A non-empty string is truthy, and client.update_visit then erases precision."""
    from wanderfill.plan.model import validate

    plan = _vplan(_vop("update_visit", region=1, visit_id=2, quality=3, allow_vaguer=value))
    assert (validate(plan) == []) is ok


@pytest.mark.parametrize("make", [
    pytest.param(lambda d: d / "nope.json", id="missing"),
    pytest.param(lambda d: d, id="a directory"),
])
def test_a_plan_path_that_cannot_be_read_says_so_plainly(tmp_path, make):
    from wanderfill.api.errors import PlanInvalid

    path = make(tmp_path)
    with pytest.raises(PlanInvalid) as e:
        Plan.load(path)
    assert str(path) in str(e.value)


WRONG = [None, 0, 1, -1, 1.5, True, "", "x", "2024-01-01", [], [1], [[]], {}, {"a": 1},
         [{"id": [1], "quality": {}}], [{"id": 1, "quality": None}, {"id": 1, "quality": 2}]]


@pytest.mark.parametrize("field", [
    "kind", "bucket", "label", "region", "visit_id", "series", "item", "date_from", "date_to",
    "quality", "regions", "allow_vaguer", "confidence", "method", "evidence"])
@pytest.mark.parametrize("base", [
    {"kind": "mark_dare", "region": 1},
    {"kind": "add_visit", "region": 1, "quality": 3, "date_from": "2024"},
    {"kind": "create_trip", "date_from": "2024-01-01", "date_to": "2024-01-02",
     "regions": [{"id": 1, "quality": 3, "year_from": 2024}]},
], ids=lambda b: b["kind"])
def test_validate_never_raises_on_a_wrong_typed_field(base, field):
    from wanderfill.plan.model import validate

    for value in WRONG:
        op = Op(**({"bucket": "apply"} | base | {field: value}))
        assert isinstance(validate(Plan(account=7, ops=[op])), list), (field, value)
        assert isinstance(validate(Plan(account=7, ops=[
            Op(kind="mark_dare", bucket="apply", region=2), op])), list), (field, value)


@pytest.mark.parametrize("field", ["account", "basis", "schema", "params", "sources"])
def test_validate_never_raises_on_a_wrong_typed_plan_field(field):
    from wanderfill.plan.model import validate

    for value in WRONG:
        plan = Plan(account=7, ops=[Op(kind="mark_dare", bucket="apply", region=1)])
        setattr(plan, field, value)
        assert isinstance(validate(plan), list), (field, value)


def test_a_loaded_plan_with_regions_on_a_mark_dare_is_a_problem_not_a_crash(tmp_path):
    from wanderfill.plan.model import validate

    for regions in ("null", "1", "[1]"):
        path = tmp_path / "p.json"
        path.write_text('{"account": 7, "ops": [{"kind": "mark_dare", "bucket": "apply", '
                        f'"region": 1, "regions": {regions}}}]}}')
        assert validate(Plan.load(path))


def test_show_renders_malformed_values_and_still_prints_the_problems(tmp_path, capsys):
    from wanderfill.cli import main as cli

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"account": 7, "ops": [
        {"kind": "create_trip", "bucket": "apply", "regions": 42, "evidence": 5},
        {"kind": "create_trip", "bucket": "apply", "regions": [1, None], "evidence": "x"},
        {"kind": "mark_dare", "bucket": "apply", "region": [1], "label": None}]}))
    assert cli.main(["show", str(path), "-v"]) == 0
    assert "INVALID" in capsys.readouterr().out


@pytest.mark.parametrize("text", [
    "", "20240101", "2024-W01-1", "2024-1-1", " 2024", "2024 ", "\u0662\u0660\u0662\u0664",
    "\u0662\u0660\u0662\u0664-01-01", "2024-01-01T00:00", "0000", "2101", 2024, 1.5,
])
def test_execution_reads_one_spelling_per_date(text):
    """Two spellings of one date hashed to two keys and ran as the same write twice."""
    from wanderfill.plan.apply import _when
    from wanderfill.plan.model import parse_when

    with pytest.raises(ValueError):
        parse_when(text)
    with pytest.raises(ValueError):
        _when(text)


def test_the_canonical_spellings_still_parse():
    from wanderfill.plan.model import parse_when

    assert parse_when(None) is None
    assert parse_when("2013") == YearOnly(2013)
    assert parse_when("2024-02-29") == dt.date(2024, 2, 29)


def test_two_spellings_of_one_visit_are_no_longer_both_valid(tmp_path):
    from wanderfill.api.errors import PlanInvalid
    from wanderfill.plan.apply import apply_plan
    from wanderfill.plan.model import validate

    for a, b in ((None, ""), ("2024-01-01", "20240101")):
        two = [_vop(region=1, quality=3, date_from=x, date_to=x) for x in (a, b)]
        assert validate(_vplan(*two))
    c, t = client({"user/status-quick": {"result": "OK", "uid": 7}})
    plan = _vplan(_vop("mark_dare", region=1), _vop(region=2, quality=3, date_from="20240101"))
    with pytest.raises(PlanInvalid, match=r"ops\[1\]"):
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert t.sent == []


NESTED = [10**30, 10**5000, "9" * 5000, "\u0662\u0660\u0662\u0664", 2024.0, float("nan"),
          True, [[1]], ["2024"], {"a": 1}, "", "1" * 11]


@pytest.mark.parametrize("field", [
    "id", "quality", "hidden", "year_from", "month_from", "day_from", "year_to", "month_to",
    "day_to"])
def test_validate_never_raises_on_a_wrong_typed_trip_region_field(field):
    from wanderfill.plan.model import validate

    for value in NESTED:
        region = {"id": 1, "quality": 3, "year_from": 2024, "month_from": 1, "day_from": 2,
                  "year_to": 2024, "month_to": 1, "day_to": 2}
        region[field] = value
        plan = Plan(account=7, ops=[_vtrip(regions=[region])])
        assert isinstance(validate(plan), list), (field, str(value)[:20])


def test_show_prints_the_problems_of_a_huge_numeric_trip_region_part(tmp_path, capsys):
    from wanderfill.cli import main as cli

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"account": 7, "ops": [{
        "kind": "create_trip", "bucket": "apply", "date_from": "2024-01-01",
        "date_to": "2024-01-02",
        "regions": [{"id": 1, "quality": 3, "year_from": "9" * 5000}]}]}))
    assert cli.main(["show", str(path), "-v"]) == 0
    assert "INVALID" in capsys.readouterr().out


def test_short_ascii_numeric_strings_in_trip_region_parts_are_still_accepted():
    from wanderfill.plan.model import validate

    region = {"id": 1, "quality": 3, "year_from": "2024", "month_from": "1", "day_from": "02",
              "year_to": "2024", "month_to": "01", "day_to": "3"}
    assert validate(Plan(account=7, ops=[_vtrip(regions=[region])])) == []


# --------------------------------------------------------------------------
# a trip's key covers what is sent
# --------------------------------------------------------------------------

JAN = {"year_from": 2026, "month_from": 1, "day_from": 1,
       "year_to": 2026, "month_to": 1, "day_to": 2}
TRIP_REGION = {"id": 11, "quality": 3, "hidden": 0} | JAN


def _jan(*regions, **kw):
    return _vtrip(**({"date_from": "2026-01-01", "date_to": "2026-01-02",
                      "regions": [dict(r) for r in regions] or [dict(TRIP_REGION)]} | kw))


@pytest.mark.parametrize("change", [
    {"year_from": 2025}, {"month_from": 2}, {"day_from": 2}, {"year_to": 2027},
    {"month_to": 2}, {"day_to": 3}, {"month_from": None, "day_from": None},
    dict.fromkeys(JAN),
], ids=str)
def test_trip_keys_differ_when_only_a_regions_dates_differ(change):
    """A trip re-planned with a region's dates corrected was skipped as already done."""
    from wanderfill.plan.model import validate

    a, b = _jan(TRIP_REGION), _jan(TRIP_REGION | change)
    assert a.key != b.key and a.legacy_key == b.legacy_key
    if change == {"day_from": 2}:
        assert validate(_vplan(a, b)) == [], "the old key is not a duplicate"


def test_trip_keys_differ_when_only_hidden_differs():
    from wanderfill.plan.model import validate

    a, b = _jan(TRIP_REGION), _jan(TRIP_REGION | {"hidden": 1})
    assert a.key != b.key and a.legacy_key == b.legacy_key
    assert validate(_vplan(a, b)) == []


def test_trip_key_ignores_region_order():
    """Order inside the list or a region, and how a number is spelled, change nothing sent."""
    other = {"id": 2, "quality": 4, "year_from": 2026}
    want = _jan(TRIP_REGION, other).key
    assert _jan(other, TRIP_REGION).key == want
    assert _jan(dict(reversed(TRIP_REGION.items())), other).key == want
    # Two regions with one id are refused, but their order still must not count.
    twin = TRIP_REGION | {"hidden": None, "month_from": None, "day_from": None}
    assert _jan(TRIP_REGION, twin).key == _jan(twin, TRIP_REGION).key


@pytest.mark.parametrize("field", ["id", *JAN])
def test_trip_key_reads_text_digits_as_the_number(field):
    region = TRIP_REGION | {"id": 11, "month_from": 11, "month_to": 11, "day_to": 12}
    want = _jan(region).key
    assert _jan(region | {field: str(region[field])}).key == want
    if not field.startswith(("id", "year")):
        assert _jan(region | {field: f"{region[field]:03}"}).key == want


def test_trip_key_has_one_spelling_of_absent():
    plain = {k: v for k, v in TRIP_REGION.items() if k not in ("hidden", *JAN)}
    want = _jan(plain).key
    for same in ({"hidden": h} for h in (0, "0", None)):
        assert _jan(plain | same).key == want, same
    for empty in (None, ""):
        assert _jan(plain | dict.fromkeys(JAN, empty)).key == want, repr(empty)
    assert _jan(plain | {"hidden": 1}).key == _jan(plain | {"hidden": "1"}).key != want


@pytest.mark.parametrize("hidden", [None, 0, 1, "0", "1"])
def test_a_trip_region_may_be_hidden_or_not(hidden):
    from wanderfill.plan.model import validate

    op = _jan(TRIP_REGION | {"hidden": hidden}, {"id": 12, "quality": 3})
    assert validate(_vplan(op)) == []
    assert len(op.key) == len(op.legacy_key) == 16


def test_a_trip_region_hidden_by_anything_else_is_refused():
    """It went to the server as it stood, and the key could not say what it meant there."""
    from wanderfill.plan.model import validate

    for hidden in (True, False, "", "x", "01", "true", 2, -1, 0.0, 1.0, 1.5, float("nan"), [],
                   [1], {"a": 1}, 10**30, 10**5000, "9" * 5000, "\u0661", "\ud83d"):
        (why,) = validate(_vplan(_jan(TRIP_REGION | {"hidden": hidden})))
        assert "ops[0]" in why and "regions[0]" in why and "hidden" in why


@pytest.mark.parametrize("field", ["year", "dates", "Hidden", "visit_id", "year_from ", "name", 5])
def test_a_trip_region_field_outside_the_key_is_refused(field):
    """Region dicts are sent as they stand, so two trips could differ in what reached the
    server and share a key."""
    from wanderfill.plan.model import validate

    for value in (None, 2026):
        (why,) = validate(_vplan(_jan({"id": 12, "quality": 3}, TRIP_REGION | {field: value})))
        assert "ops[0]" in why and "regions[1]" in why and repr(field) in why


def test_every_field_a_trip_region_may_carry_is_in_the_key():
    from wanderfill.plan.model import TRIP_REGION_FIELDS, validate

    assert set(TRIP_REGION_FIELDS) == set(TRIP_REGION)
    assert validate(_vplan(_jan())) == []
    for field in TRIP_REGION_FIELDS:
        assert _jan(TRIP_REGION | {field: 5}).key != _jan().key, field


GOLDEN = {
    "add_visit": "79c8eaaa53229ef7",
    "update_visit": "d7a05a5109773bc2",
    "mark_dare": "81bbe4792ea14391",
    "mark_kye": "42cab0614eae52d7",
    "mark_tcc": "722f39f054a6a4fe",
    "tick_series": "643df695b5e5a487",
}


def test_other_keys_did_not_move():
    """The journals on disk name these; a moved key re-sends a write that landed."""
    days = {"date_from": "2026-01-01", "date_to": "2026-01-02"}
    ops = [
        Op(kind="add_visit", region=5, quality=3, **days),
        Op(kind="update_visit", region=5, visit_id=9, quality=3, **days),
        Op(kind="mark_dare", region=7),
        Op(kind="mark_kye", item=570),
        Op(kind="mark_tcc", item=64),
        Op(kind="tick_series", series=5, item=2903),
    ]
    assert {o.kind: o.key for o in ops} == GOLDEN
    assert {o.legacy_key for o in ops} == {None}


def test_legacy_trip_key_is_the_old_hash():
    op = _jan()
    assert op.legacy_key == "8ae84238652ef19e" != op.key


def _done_before(workdir, *keys, trip=None, phase="done", rows=()):
    """A plan of one trip, its journal holding ``keys``, its client, and what it sent."""
    from wanderfill.plan.model import basis_of, fingerprint

    c, t = client(STATE | {"trips/new-trip": {"result": "OK", "trip_id": 900},
                           READ: visits(*rows)})
    plan = Plan(account=7, ops=[trip or _jan()],
                basis={"fingerprint": fingerprint(basis_of(c.snapshot([11]), [11]))})
    path = workdir / f"journal-7-{fingerprint(plan.basis)[:12]}.ndjson"
    path.write_text("".join(json.dumps({"phase": phase, "key": k, "kind": "create_trip"}) + "\n"
                            for k in keys), encoding="utf-8")
    t.sent.clear()
    return plan, path, c, t


def _writes(t):
    return [a for a, _ in t.sent if Transport._is_write(a)]


@pytest.mark.parametrize("change", [{}, {"day_to": 1}], ids=["same", "corrected"])
def test_a_trip_done_under_the_old_key_is_refused_not_resent(tmp_path, change):
    """The old key cannot say whether the trip it stands for is this one or a corrected
    one: sending could duplicate it, and skipping could drop the correction."""
    from wanderfill.plan.apply import apply_plan

    trip = _jan(TRIP_REGION | change)
    plan, path, c, t = _done_before(tmp_path, trip.legacy_key, trip=trip)
    plan.ops.insert(0, _vop("mark_dare", region=1))  # refused before this one is sent, too
    before = path.read_bytes()
    with pytest.raises(DriftError) as info:
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    for wanted in (str(path), trip.legacy_key, "the op", "ops[1]", "older",
                   "rebuild the plan from the live profile", "without this trip"):
        assert wanted in str(info.value)
    assert _writes(t) == [] and path.read_bytes() == before


def test_a_rebuilt_plan_that_records_when_it_was_taken_has_its_own_journal(tmp_path):
    """The way out of the refusal, so its journal cannot stop a second copy of the trip."""
    from wanderfill.plan.apply import apply_plan

    trip = _jan()
    plan, _, c, t = _done_before(tmp_path, trip.legacy_key)
    with pytest.raises(DriftError, match="older"):
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    rebuilt = Plan(account=7, ops=plan.ops, basis=plan.basis | {"taken_at": "2026-11-24"})
    assert apply_plan(c, rebuilt, workdir=tmp_path, confirm=True).succeeded == 1
    assert _writes(t) == ["trips/new-trip"]


def test_a_trip_done_under_its_own_key_is_skipped_whatever_the_old_key_says(tmp_path):
    from wanderfill.plan.apply import apply_plan

    trip = _jan()
    for n, keys in enumerate(([trip.key], [trip.legacy_key, trip.key])):
        work = tmp_path / str(n)
        work.mkdir()
        plan, _, c, t = _done_before(work, *keys, rows=[_stored(**JAN)])
        report = apply_plan(c, plan, workdir=work, confirm=True)
        assert (report.skipped, report.attempted) == (1, 0) and _writes(t) == []


def test_only_this_plans_apply_ops_are_held_to_the_old_key(tmp_path):
    from wanderfill.plan.apply import apply_plan

    other = _jan(TRIP_REGION | {"id": 12})
    plan, _, c, t = _done_before(tmp_path, other.legacy_key)
    other.bucket = "review"
    plan.ops.append(other)
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == 1
    assert _writes(t) == ["trips/new-trip"]


def test_a_trip_open_under_the_old_key_blocks_and_can_be_closed(tmp_path):
    from wanderfill.plan.apply import apply_plan

    old = _jan().legacy_key
    plan, path, c, t = _done_before(tmp_path, old, phase="open")

    def close(error):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"phase": "done", "key": old, "error": error}) + "\n")

    with pytest.raises(DriftError, match=f"never confirmed: {old}") as info:
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert str(path) in str(info.value) and t.sent == ACCOUNT_READ
    still_open = path.read_bytes()
    close("")  # it landed
    with pytest.raises(DriftError, match="older"):
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert _writes(t) == []
    path.write_bytes(still_open)
    close("did not land")
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == 1
    assert _writes(t) == ["trips/new-trip"]


def test_check_reports_a_trip_done_under_the_old_key(monkeypatch, tmp_path, capsys):
    from wanderfill.cli import main as cli

    trip = _jan()
    for n, (keys, rc) in enumerate((([trip.legacy_key], 1), ([trip.legacy_key, trip.key], 0))):
        work = tmp_path / str(n)
        work.mkdir()
        plan, path, c, t = _done_before(work, *keys, rows=[_stored(**JAN)] if not rc else ())
        saved = plan.save(tmp_path / "p.json")
        monkeypatch.setattr(cli, "_client", lambda args, c=c: c)
        assert cli.main(["check", str(saved), "--workdir", str(work)]) == rc
        out = capsys.readouterr().out
        assert (str(path) in out and trip.legacy_key in out) == bool(rc)
        assert ("1 op(s) already applied" in out) == (not rc)
        assert _writes(t) == []


# --------------------------------------------------------------------------
# a missing list is not an empty profile, and nothing is sent a second time
# --------------------------------------------------------------------------

REGIONS_READ = "maps/get-visited-regions-ids-simple"
JAN_DAYS = {"date_from": "2026-01-01", "date_to": "2026-01-02"}


def _rebuilt(*ops, rows=(), replies=None):
    """A plan of ``ops`` built from this very profile, its client, and what it sent."""
    from wanderfill.plan.model import basis_of, fingerprint, regions_touched

    c, t = client(STATE | {READ: visits(*rows), "trips/new-trip": {"result": "OK", "trip_id": 901}}
                  | TCC_ROWS | (replies or {}))
    regions = sorted(regions_touched(ops))
    snap = c.snapshot(regions, tcc=any(o.kind == "mark_tcc" for o in ops))
    basis = {"fingerprint": fingerprint(basis_of(snap, regions)), "taken_at": "2026-11-24"}
    plan = Plan(account=7, ops=list(ops), basis=basis)
    t.sent.clear()
    return plan, c, t


def _refused(plan, c, t, workdir, exc=DriftError):
    """Apply, which must raise, send no write and leave no journal. Returns the message."""
    from wanderfill.plan.apply import apply_plan

    with pytest.raises(exc) as info:
        apply_plan(c, plan, workdir=workdir, confirm=True)
    assert _writes(t) == [] and not list(workdir.glob("journal-*"))
    return str(info.value)


def _checked(monkeypatch, capsys, plan, c, tmp_path, rc):
    from wanderfill.cli import main as cli

    monkeypatch.setattr(cli, "_client", lambda args: c)
    saved = plan.save(tmp_path / "p.json")
    capsys.readouterr()
    assert cli.main(["check", str(saved), "--workdir", str(tmp_path / "none")]) == rc
    return capsys.readouterr().out


def _jan_add(**kw):
    return _vop(**({"region": 11, "quality": 3} | JAN_DAYS | kw))


@pytest.mark.parametrize("owner", [None, 900], ids=["standalone", "trip-owned"])
@pytest.mark.parametrize("op", [
    pytest.param(lambda: _jan_add(), id="add_visit"),
    pytest.param(lambda: _jan(), id="create_trip"),
    pytest.param(lambda: _jan({"id": 11, "quality": 3}), id="dates from the header"),
    pytest.param(lambda: _jan({"id": 12, "quality": 3}, TRIP_REGION | dict.fromkeys(JAN, "")),
                 id="second region, empty dates"),
])
def test_a_rebuilt_plan_does_not_send_a_trip_that_is_already_there(
        monkeypatch, tmp_path, capsys, op, owner):
    """It carries the trip in its own basis, so drift passes, and its journal is new."""
    plan, c, t = _rebuilt(op(), rows=[_stored(**JAN, trip_id=owner, quality=1)])
    if len(plan.ops[0].regions) == 2:  # only region 11 holds the visit
        real = t.webapi
        monkeypatch.setattr(t, "webapi", lambda a, **f: visits() if f.get("region") == 12
                            else real(a, **f))
        from wanderfill.plan.model import basis_of, fingerprint
        plan.basis["fingerprint"] = fingerprint(basis_of(c.snapshot([11, 12]), [11, 12]))
    message = _refused(plan, c, t, tmp_path)
    for wanted in (plan.ops[0].kind, "the op", "region 11", "2026-01-01..2026-01-02",
                   "Nothing was sent"):
        assert wanted in message
    assert "region 12" not in message
    out = _checked(monkeypatch, capsys, plan, c, tmp_path, 1)
    assert "BLOCKERS" in out and "region 11" in out and _writes(t) == []
    assert "drift:   unchanged" in out, "which is why drift alone did not stop it"


def test_an_op_done_in_this_plans_journal_is_not_its_own_double(tmp_path):
    from wanderfill.plan.apply import Journal, apply_plan
    from wanderfill.plan.model import fingerprint

    plan, c, t = _rebuilt(_jan(), rows=[_stored(**JAN)])
    Journal(tmp_path / f"journal-7-{fingerprint(plan.basis)[:12]}.ndjson").note(plan.ops[0], "OK")
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).skipped == 1
    assert _writes(t) == []


@pytest.mark.parametrize("ops", [
    pytest.param(lambda: [_jan_add(label="a"), _jan(label="b")], id="an add and a trip region"),
    pytest.param(lambda: [_jan(label="a"), _jan(TRIP_REGION | {"quality": 4}, label="b")],
                 id="two trips"),
    pytest.param(lambda: [_jan({"id": 11, "quality": 3}, label="a"),
                          _jan(TRIP_REGION | {"day_to": "02"}, label="b", date_to="2026-01-09")],
                 id="header dates and own dates"),
])
def test_two_ops_for_one_region_and_dates_are_refused(monkeypatch, tmp_path, capsys, ops):
    """Their keys differ, so nothing compared a trip region with an add, or two trips."""
    from wanderfill.plan.apply import doubles

    plan, c, t = _rebuilt(_vop("mark_dare", region=1), *ops())
    (line,) = doubles({}, plan.ops)
    assert "'a'" in line and "'b'" in line and "region 11" in line
    assert line in _refused(plan, c, t, tmp_path)
    assert line in _checked(monkeypatch, capsys, plan, c, tmp_path, 1)


def test_the_double_guard_ignores_undated_year_only_and_overlapping_visits(tmp_path):
    """Two real visits can share a year or no date at all, and stays can overlap."""
    from wanderfill.plan.apply import apply_plan, doubles

    year = {"year_from": 2026, "month_from": None, "day_from": None,
            "year_to": 2026, "month_to": None, "day_to": None}
    rows = [_stored(**dict.fromkeys(JAN)), _stored(**year), _stored(**JAN | {"day_to": 3}),
            _stored(**JAN | {"year_from": 2025}), _stored(**year | JAN | dict.fromkeys(
                ("month_to", "day_to")))]
    ops = [_jan_add(), _vop(region=11, quality=3),
           _vop(region=11, quality=3, date_from="2026", date_to="2026"),
           _vop(region=11, quality=3, date_from="2026-01-01", date_to="2026"),
           _jan(TRIP_REGION | year), _jan(TRIP_REGION | {"day_to": 5}),
           _jan(TRIP_REGION | {"id": 12})]
    plan, c, t = _rebuilt(*ops, rows=rows)
    assert doubles(c.snapshot([11, 12]), ops) == []
    t.sent.clear()
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == len(ops)


def test_an_update_visit_is_not_a_double_of_itself(tmp_path):
    from wanderfill.plan.apply import apply_plan

    op = _vop("update_visit", region=11, visit_id=1, quality=4, **JAN_DAYS)
    plan, c, t = _rebuilt(op, rows=[_stored(**JAN)])
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == 1
    assert _writes(t) == ["quickEnter/update-visit"]


BAD_LISTS = [{"result": "OK"}, {"result": "OK", "LIST": None}, {"result": "OK", "LIST": {}},
             {"result": "OK", "LIST": ["x"]}, {"result": "OK", "LIST": [None]},
             {"result": "OK", "LIST": [True]}, {"result": "OK", "LIST": [1.5]}, []]


def _bad(reply, field):
    return {field if k == "LIST" else k: v for k, v in reply.items()} if reply else reply


@pytest.mark.parametrize("reply", BAD_LISTS, ids=str)
@pytest.mark.parametrize("action,field", [
    (REGIONS_READ, "ids"), ("maps/get-visited-dare-ids-simple", "ids"), (READ, "data")])
def test_snapshot_refuses_a_reply_without_its_list(
        monkeypatch, tmp_path, capsys, action, field, reply):
    """Planning and applying both read it as an empty profile, so drift passed."""
    from wanderfill.plan.model import basis_of, fingerprint

    plan, c, t = _rebuilt(_jan_add(), _vop("mark_dare", region=1))
    plan.basis = {"fingerprint": fingerprint(basis_of({}, [11]))}  # planned against the same reply
    t.replies[action] = _bad(reply, field)
    assert action in _refused(plan, c, t, tmp_path, VerificationFailed)
    assert not list(tmp_path.glob("snapshot-*"))
    out = _checked(monkeypatch, capsys, plan, c, tmp_path, 1)
    assert "BLOCKERS" in out and action in out and "unchanged since planning" not in out


def test_a_strict_read_answered_with_html_is_a_wanderfill_error(monkeypatch, tmp_path, capsys):
    """A captive portal's page: ``json.loads`` raised a bare ValueError."""
    for op, broken in ((_jan_add(), REGIONS_READ), (_vop("mark_kye", item=5), "kye/get-kye"),
                       (_vop("tick_series", series=22, item=5), "V2/")):
        plan, c, t = _rebuilt(op, replies={"kye/get-kye": {"visited": []}})

        def reply(real, broken=broken):
            def call(path, /, **fields):
                return json.loads("<html>") if path == broken else real(path, **fields)
            return call

        monkeypatch.setattr(t, "webapi", reply(t.webapi))
        monkeypatch.setattr(t, "ajax_json", reply(t.ajax_json))
        _refused(plan, c, t, tmp_path, VerificationFailed)
        assert "BLOCKERS" in _checked(monkeypatch, capsys, plan, c, tmp_path, 1)


def test_check_reports_a_list_the_server_refuses_as_a_blocker(monkeypatch, tmp_path, capsys):
    for op, broken in ((_jan_add(), READ), (_vop("mark_kye", item=5), "kye/get-kye")):
        plan, c, t = _rebuilt(op, replies={"kye/get-kye": {"visited": []}})
        t.replies[broken] = {"result": "ERROR", "result_description": "Database hiccup."}
        _refused(plan, c, t, tmp_path, ApiError)
        out = _checked(monkeypatch, capsys, plan, c, tmp_path, 1)
        assert "BLOCKERS" in out and "Database hiccup." in out


def _tick_replies(ids=(570, "571")):
    return {"V2/": {"title": "Capitals", "visited": list(ids)},
            "kye/get-kye": {"visited": list(ids)},
            "maps/get-visited-dare-ids-simple": {"result": "OK", "ids": list(ids)}}


TICKED = {
    "series": lambda: _vop("tick_series", series=1, item=570),
    "series, a string in the list": lambda: _vop("tick_series", series=1, item=571),
    "kye": lambda: _vop("mark_kye", item=570),
    "kye, a string in the list": lambda: _vop("mark_kye", item=571),
    "dare": lambda: _vop("mark_dare", region=570),
    "dare, a string in the list": lambda: _vop("mark_dare", region=571),
    "tcc": lambda: _vop("mark_tcc", item=325),
}


@pytest.mark.parametrize("name", TICKED)
def test_apply_skips_an_item_that_is_already_ticked(monkeypatch, tmp_path, capsys, name):
    """The series endpoint is a toggle: sending a tick again may undo it."""
    from wanderfill.cli import main as cli
    from wanderfill.plan.apply import apply_plan

    plan, c, t = _rebuilt(TICKED[name](), replies=_tick_replies())
    seen = []
    report = apply_plan(c, plan, workdir=tmp_path / "w", confirm=True,
                        on_progress=lambda *a: seen.append(a))
    assert _writes(t) == [] and not any(a == "my_series/" for a, _ in t.sent)
    assert report.already == ["the op"]
    assert (report.attempted, report.succeeded, report.skipped) == (0, 0, 1)
    assert seen == [(1, 1, plan.ops[0], True)]
    assert not list((tmp_path / "w").glob("journal-*"))
    assert (report.verified["checked"], report.verified["mismatches"]) == (1, [])

    assert "1 op(s) already ticked" in _checked(monkeypatch, capsys, plan, c, tmp_path, 0)
    path = plan.save(tmp_path / "p.json")
    assert cli.main(["apply", str(path), "--confirm", "--workdir", str(tmp_path / "w2")]) == 0
    out = capsys.readouterr().out
    assert "already ticked, not sent: the op" in out and _writes(t) == []


def test_a_tick_is_sent_once_however_the_plan_spells_it(tmp_path):
    """Dates are in every op's key and reach no tick, so two keys can be one toggle."""
    from wanderfill.plan.apply import apply_plan

    plan, c, t = _rebuilt(_vop("tick_series", series=1, item=5),
                          _vop("tick_series", series=1, item=5, date_from="2024", label="again"),
                          _vop("tick_series", series=2, item=6), replies=_tick_replies())
    report = apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert [f["series"] for a, f in t.sent if a == "my_series/"] == [1, 2]
    assert report.already == ["again"] and report.succeeded == 2


@pytest.mark.parametrize("op,action,field", [
    (lambda: _vop("tick_series", series=1, item=5), "V2/", "visited"),
    (lambda: _vop("mark_kye", item=5), "kye/get-kye", "visited"),
])
@pytest.mark.parametrize("reply", BAD_LISTS, ids=str)
def test_a_tick_list_that_cannot_be_read_sends_nothing(
        monkeypatch, tmp_path, capsys, op, action, field, reply):
    """Read as empty, it says nothing is ticked, and the toggle goes out."""
    plan, c, t = _rebuilt(_vop("mark_dare", region=1), op(), replies=_tick_replies())
    t.replies[action] = _bad(reply, field)
    assert action in _refused(plan, c, t, tmp_path, VerificationFailed)
    assert "BLOCKERS" in _checked(monkeypatch, capsys, plan, c, tmp_path, 1)


def test_a_tcc_row_whose_id_is_not_a_number_is_a_failed_read():
    c, _ = client({"quickEnter/get-regions": {"data": {"tcc": [{"id": "x", "visited": 1}]}}})
    with pytest.raises(VerificationFailed):
        c.tcc()
    c, _ = client({"quickEnter/get-regions": {"data": {"tcc": [{"visited": 1}]}}})
    with pytest.raises(VerificationFailed):
        c.tcc()


def test_ticked_before_reads_each_list_once():
    from wanderfill.plan.apply import ticked_before

    ops = [f() for f in TICKED.values()] + [_vop("tick_series", series=2, item=1), _jan_add()]
    c, t = client(STATE | TCC_ROWS | _tick_replies())
    snap = c.snapshot([], tcc=True)
    t.sent.clear()
    both = {570, 571}
    want = {("series", 1): both, ("series", 2): both, ("kye", 0): both, ("dare", 0): both,
            ("tcc", 0): {325}}
    assert ticked_before(c, ops, snap) == want
    assert [a for a, _ in t.sent] == ["V2/", "kye/get-kye", "V2/"], "DARE and TCC: the snapshot's"
    assert ticked_before(c, ops) == want


def test_a_tick_only_plan_does_not_read_every_region(monkeypatch, tmp_path, capsys):
    """``regions or None`` turned "no regions" into all 398 of them."""
    from wanderfill.plan.apply import apply_plan

    plan, c, t = _rebuilt(_vop("mark_kye", item=5), _vop("tick_series", series=1, item=5),
                          replies=_tick_replies() | {REGIONS_READ: {"ids": [1, 2, 3]}})
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == 2
    _checked(monkeypatch, capsys, plan, c, tmp_path, 0)
    assert READ not in [a for a, _ in t.sent]


def test_the_snapshot_file_keeps_the_ticks_it_is_about_to_change(tmp_path):
    """The undo information: which of these were there before the run."""
    from wanderfill.plan.apply import apply_plan

    plan, c, _ = _rebuilt(_vop("tick_series", series=22, item=5), _vop("mark_kye", item=5),
                          _vop("mark_dare", region=5), _vop("mark_tcc", item=64),
                          replies=_tick_replies())
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == 4
    (path,) = tmp_path.glob("snapshot-*.json")
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert snap["ticked"] == {"series": {"22": [570, 571]}, "kye": [570, 571]}
    assert snap["visited_dare"] == [570, 571] and snap["visited_tcc"] == [325]


def test_a_fingerprint_made_before_this_change_still_matches():
    """Plans on disk were fingerprinted from snapshots of every region."""
    from wanderfill.plan.model import basis_of, fingerprint

    old = {"visited_regions": [5, 6], "visited_dare": [7], "visits": {"5": [
        {"id": 1, "date_from": "2026-01-01", "date_to": "2026-01-02", "quality": 3}]}}
    golden = "091eb3c2162171bedddce8c38be944a1"
    assert fingerprint(basis_of(old, [5])) == golden
    assert fingerprint(basis_of(old | {"ticked": {"kye": [1]}}, [5])) == golden
    c, _ = client(STATE | {REGIONS_READ: {"ids": [6, 5]}, READ: visits(_stored())})
    every, none = c.snapshot(), c.snapshot([])
    assert set(every["visits"]) == {"5", "6"} and none["visits"] == {}
    assert fingerprint(basis_of(every, [])) == fingerprint(basis_of(none, []))


def _jan_update(**kw):
    return _vop("update_visit", **({"region": 11, "visit_id": 2, "quality": 3} | JAN_DAYS | kw))


ELSEWHERE = {"id": 2, "day_from": 8, "day_to": 9, **{k: JAN[k] for k in (
    "year_from", "month_from", "year_to", "month_to")}}


@pytest.mark.parametrize("rows,ops", [
    pytest.param(lambda: [_stored(**JAN), _stored(**ELSEWHERE)], lambda: [_jan_update()],
                 id="onto a visit that is there"),
    pytest.param(lambda: [_stored(**JAN, trip_id=None), _stored(**dict.fromkeys(JAN), id=2)],
                 lambda: [_jan_update()], id="an undated visit, given a standalone one's dates"),
    pytest.param(lambda: [_stored(**ELSEWHERE)], lambda: [_jan_add(), _jan_update()],
                 id="after an add"),
    pytest.param(lambda: [_stored(**ELSEWHERE)], lambda: [_jan_update(), _jan_add()],
                 id="before an add"),
    pytest.param(lambda: [_stored(**ELSEWHERE)], lambda: [_jan_update(), _jan()],
                 id="before a trip"),
    pytest.param(lambda: [_stored(**ELSEWHERE), _stored(**ELSEWHERE | {"id": 3})],
                 lambda: [_jan_update(), _jan_update(visit_id=3)], id="two updates"),
    pytest.param(lambda: [_stored(**JAN), _stored(**ELSEWHERE)],
                 lambda: [_jan_update(visit_id=1, date_from="2026-01-03", date_to="2026-01-03"),
                          _jan_add()], id="an add onto dates an update vacates"),
])
def test_an_update_that_moves_a_visit_onto_another_is_refused(
        monkeypatch, tmp_path, capsys, rows, ops):
    """update-visit makes a second copy as readily as add-visit does."""
    plan, c, t = _rebuilt(_vop("mark_dare", region=1), *ops(), rows=rows())
    message = _refused(plan, c, t, tmp_path)
    assert "region 11 for 2026-01-01..2026-01-02" in message and "Nothing was sent" in message
    out = _checked(monkeypatch, capsys, plan, c, tmp_path, 1)
    assert "BLOCKERS" in out and "region 11 for 2026-01-01..2026-01-02" in out


def test_an_update_that_keeps_its_dates_may_have_a_twin(tmp_path):
    """Twins that are there already have to stay editable, quality included."""
    from wanderfill.plan.apply import apply_plan

    twins = [_stored(**JAN), _stored(**JAN, id=2)]
    plan, c, t = _rebuilt(_jan_update(quality=4), _jan_update(visit_id=1, quality=4), rows=twins)
    assert apply_plan(c, plan, workdir=tmp_path / "a", confirm=True).succeeded == 2
    assert _writes(t) == ["quickEnter/update-visit"] * 2
    # Moving one away from the other, or making it vaguer, copies nothing.
    plan, c, t = _rebuilt(_jan_update(date_to="2026-01-03"), rows=twins)
    assert apply_plan(c, plan, workdir=tmp_path / "b", confirm=True).succeeded == 1
    plan, c, t = _rebuilt(_jan_update(date_from="2026", date_to="2026", allow_vaguer=True),
                          rows=twins)
    assert apply_plan(c, plan, workdir=tmp_path / "c", confirm=True).succeeded == 1


def test_an_update_of_a_visit_that_is_not_there_is_left_to_the_client(tmp_path):
    from wanderfill.plan.apply import apply_plan, doubles

    op = _jan_update(visit_id=99)
    plan, c, t = _rebuilt(op, rows=[_stored(**JAN)])
    assert doubles(c.snapshot([11]), [op, _jan_update(visit_id=98)]) == []
    with pytest.raises(PrecisionLoss, match="refusing to write blind"):
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert _writes(t) == []


@pytest.mark.parametrize("field,op,key", [
    ("visited_dare", lambda: _vop("mark_dare", region=570), ("dare", 0)),
    ("visited_tcc", lambda: _vop("mark_tcc", item=570), ("tcc", 0)),
])
def test_ticked_before_reads_a_snapshots_ids_as_strictly_as_the_servers(field, op, key):
    """``["570"]`` was not seen as ticked, and a list of anything was accepted."""
    from wanderfill.plan.apply import ticked_before

    c, t = client()
    for ids in (["570"], [570], []):
        assert ticked_before(c, [op()], {field: ids}) == {key: {570} if ids else set()}
    for bad in (["not-an-id"], [None], [True], [5.0], ["570", ""], {"570": 1}, "570", 570):
        with pytest.raises(VerificationFailed):
            ticked_before(c, [op()], {field: bad})
    assert t.sent == []


@pytest.mark.parametrize("body", [b"<html>", b"", b"\xff\xfe"])
def test_a_read_answered_with_html_is_a_wanderfill_error(monkeypatch, tmp_path, capsys, body):
    """A captive portal's page on ``user/status-quick`` was a traceback."""
    from wanderfill.api.errors import WanderfillError
    from wanderfill.cli import main as cli
    from wanderfill.plan.apply import apply_plan

    t, calls = _answering(monkeypatch, body)
    c = NomadMania(token="fake", transport=t)
    for read in (c.account_id, lambda: c.series(1), lambda: c.visits_for_region(1),
                 lambda: apply_plan(c, _dare_plan(), workdir=tmp_path / "w", confirm=True)):
        with pytest.raises(WanderfillError, match="not JSON"):
            read()
    for strict in (c.kye_ticked, lambda: c.series_ticked(1), lambda: c.snapshot([])):
        with pytest.raises(VerificationFailed, match="not JSON"):
            strict()
    assert not any(Transport._is_write(r.full_url) for r in calls)
    assert not (tmp_path / "w").exists()
    monkeypatch.setattr(cli, "_client", lambda args: c)
    path = _dare_plan().save(tmp_path / "p.json")
    assert cli.main(["check", str(path), "--workdir", str(tmp_path / "w")]) == 2
    assert "not JSON" in capsys.readouterr().err


# --------------------------------------------------------------------------
# what landed is read back, however the run ended
# --------------------------------------------------------------------------

DARE_READ = "maps/get-visited-dare-ids-simple"


def _live(t, *, lose=None, error=None, lands=False):
    """Make the fake server keep the ticks it is sent, and lose one write.

    ``lose`` is the id in the write that raises ``error``, with or without
    the tick having landed first.
    """
    webapi, ajax_text = t.webapi, t.ajax_text
    lists = {"quickEnter/updateMQP": (DARE_READ, "ids", "region"),
             "kye/set-kye": ("kye/get-kye", "visited", "qid"),
             "my_series/": ("V2/", "visited", "item")}

    def write(real, path, fields):
        read, field, name = lists.get(path, (None, None, "region"))
        lost = error is not None and (fields.get(name) == lose or path == lose)
        if read and (not lost or lands):
            t.replies[read][field].append(fields[name])
        out = real(path, **fields)
        if lost:
            raise error
        return out

    def send(path, /, **fields):
        return (write(webapi, path, fields) if Transport._is_write(path)
                else webapi(path, **fields))

    t.webapi = send
    t.ajax_text = lambda path, /, **fields: write(ajax_text, path, fields)


def _four_ticks():
    return [_vop("mark_dare", region=1, label="a"), _vop("mark_kye", item=2, label="b"),
            _vop("tick_series", series=1, item=3, label="c"), _vop("mark_kye", item=4, label="d")]


def _verify_file(workdir):
    (path,) = workdir.glob("verify-*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _run_failing(plan, c, workdir, **kw):
    """Apply a plan that must raise; what it raised, and its journal."""
    from wanderfill.plan.apply import Journal, apply_plan

    with pytest.raises(BaseException) as info:
        apply_plan(c, plan, workdir=workdir, confirm=True, **kw)
    (path,) = workdir.glob("journal-*.ndjson")
    return info.value, Journal(path)


def _apply_cli(monkeypatch, capsys, plan, c, tmp_path, workdir="cli"):
    from wanderfill.cli import main as cli

    monkeypatch.setattr(cli, "_client", lambda args: c)
    path = plan.save(tmp_path / "p.json")
    capsys.readouterr()
    rc = cli.main(["apply", str(path), "--confirm", "--workdir", str(tmp_path / workdir)])
    return rc, capsys.readouterr()


def _lost(message="answer lost"):
    from wanderfill.api.errors import UnknownWriteOutcome

    return UnknownWriteOutcome(message)


@pytest.mark.parametrize("make,left_open,rc", [
    (lambda: _lost(), True, 2),
    (lambda: KeyboardInterrupt("ctrl-c"), True, 130),
    (lambda: RuntimeError("a bug of ours"), True, None),
    (lambda: ApiError("my_series/", "refused"), False, 2),
], ids=["lost", "interrupted", "bug", "refused"])
def test_a_failure_at_op_three_still_verifies_the_first_two(
        monkeypatch, tmp_path, capsys, make, left_open, rc):
    """An exception at op N skipped the read-back, so N-1 landed writes went unread."""
    error = make()
    args = error.args
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=error)
    exc, journal = _run_failing(plan, c, tmp_path / "w")
    assert exc is error and exc.args == args and exc.__cause__ is None
    assert any(f.name == "send" or f.name == "write" for f in _frames(exc)), "its own traceback"
    report = exc.report
    assert (report.attempted, report.succeeded, report.failed) == (3, 2, 1)
    seen = _verify_file(tmp_path / "w")
    assert seen == json.loads(json.dumps(report.verified))
    assert seen["partial"] is True and seen["checked"] == 2
    assert seen["mismatches"] == [] and seen["unproven"] == []
    assert (seen["uncertain"] is not None) == left_open
    assert journal.unresolved() == ({plan.ops[2].key} if left_open else set())
    assert journal.done_keys() == {op.key for op in plan.ops[:2]}

    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=make())
    if rc is None:
        with pytest.raises(RuntimeError, match="a bug of ours"):
            _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
        said = capsys.readouterr()
    else:
        got, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
        assert got == rc
    assert "attempted 3, succeeded 2, failed 1" in said.out
    assert ("UNCERTAIN" in said.out) == left_open
    assert (plan.ops[2].key in said.out) == left_open


def _frames(exc):
    import traceback

    return traceback.extract_tb(exc.__traceback__)


@pytest.mark.parametrize("lands", [True, False])
@pytest.mark.parametrize("name", ["series", "kye", "dare"])
def test_verifying_an_uncertain_op_does_not_close_it(tmp_path, name, lands):
    """Being in the list now is a fact about the list; a human closes the entry."""
    op = {"series": _vop("tick_series", series=1, item=3), "kye": _vop("mark_kye", item=3),
          "dare": _vop("mark_dare", region=3)}[name]
    plan, c, t = _rebuilt(op, replies=_tick_replies())
    _live(t, lose=3, error=_lost(), lands=lands)
    _, journal = _run_failing(plan, c, tmp_path)
    seen = _verify_file(tmp_path)
    assert seen["checked"] == 0 and seen["mismatches"] == [], "it is not counted as landed"
    assert (seen["uncertain"]["key"], seen["uncertain"]["kind"]) == (op.key, op.kind)
    (line,) = seen["uncertain"]["now"]
    assert ("is in the list now" if lands else "is not in the list now") in line
    assert journal.unresolved() == {op.key} and journal.done_keys() == set()
    assert [json.loads(x)["phase"] for x in journal.path.read_text().splitlines()] == ["open"]


@pytest.mark.parametrize("there", [True, False])
@pytest.mark.parametrize("op,write", [
    (lambda: _jan_add(), "quickEnter/add-visit"), (lambda: _jan(), "trips/new-trip"),
    (lambda: _jan({"id": 11, "quality": 3}), "trips/new-trip"),
], ids=["add_visit", "create_trip", "dates from the header"])
def test_an_uncertain_visit_is_reported_as_a_hint(
        monkeypatch, tmp_path, capsys, op, write, there):
    """A visit with the right dates may be the site's own, made meanwhile by hand."""
    plan, c, t = _rebuilt(op())
    _live(t, lose=write, error=_lost())
    send = t.webapi

    def lands(path, /, **fields):
        if there and path == write:
            t.replies[READ] = visits(_stored(**JAN))
        return send(path, **fields)

    t.webapi = lands
    exc, journal = _run_failing(plan, c, tmp_path / "w")
    (line,) = exc.report.verified["uncertain"]["now"]
    assert f"a visit with these dates is {'there' if there else 'not there'}" in line
    assert "whether this run made it is not known" in line and "2026-01-01..2026-01-02" in line
    assert exc.report.verified["phantom_trips"] == []
    assert journal.unresolved() == {plan.ops[0].key}

    t.replies[READ] = visits()
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 2 and line in said.out and "answer lost" in said.err


def test_an_uncertain_update_says_what_the_visit_reads_now(tmp_path):
    plan, c, t = _rebuilt(_jan_update(quality=4), rows=[_stored(**ELSEWHERE)])
    _live(t, lose="quickEnter/update-visit", error=_lost())
    exc, journal = _run_failing(plan, c, tmp_path / "a")
    (line,) = exc.report.verified["uncertain"]["now"]
    assert "reads 2026-01-08..2026-01-09 quality 3" in line
    assert "planned 2026-01-01..2026-01-02 quality 4" in line
    assert journal.unresolved() == {plan.ops[0].key}


def test_an_answer_the_journal_could_not_take_is_in_the_uncertain_report(monkeypatch, tmp_path):
    """The disk filled up between the server's OK and the ``done`` line."""
    from wanderfill.plan import apply as ap

    plan, c, _ = _rebuilt(_jan())

    def full(self, op, result, error=""):
        raise OSError("disk full")

    monkeypatch.setattr(ap.Journal, "note", full)
    exc, journal = _run_failing(plan, c, tmp_path)
    assert isinstance(exc, OSError)
    assert exc.report.verified["uncertain"]["answer"]["trip_id"] == 901
    assert journal.unresolved() == {plan.ops[0].key}


def test_a_journal_that_will_not_close_does_not_replace_the_refusal(monkeypatch, tmp_path):
    from wanderfill.plan import apply as ap

    refusal = ApiError("quickEnter/updateMQP", "no")
    plan, c, t = _rebuilt(_vop("mark_dare", region=1))
    _live(t, lose=1, error=refusal)
    monkeypatch.setattr(ap.Journal, "note", lambda *a, **kw: 1 / 0)
    exc, journal = _run_failing(plan, c, tmp_path)
    assert exc is refusal and journal.unresolved() == {plan.ops[0].key}
    assert any("ZeroDivisionError" in e for e in exc.report.errors)


def test_a_progress_printer_that_fails_does_not_lose_the_run(tmp_path):
    """A closed pipe (``apply … | head``) is an exception after a write that landed."""
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t)

    def progress(i, total, op, ok):
        if i == 2:
            raise BrokenPipeError("gone")

    exc, journal = _run_failing(plan, c, tmp_path / "a", on_progress=progress)
    assert isinstance(exc, BrokenPipeError)
    assert exc.report.verified["checked"] == 2 and exc.report.verified["uncertain"] is None
    assert journal.unresolved() == set()

    # ...and it does not hide the error of a write that failed.
    error = _lost()
    plan, c, t = _rebuilt(*_four_ticks()[1:], replies=_tick_replies())
    _live(t, lose=3, error=error)
    exc, journal = _run_failing(plan, c, tmp_path / "b", on_progress=progress)
    assert exc is error and journal.unresolved() == {plan.ops[1].key}


def test_an_entry_that_never_reached_the_journal_is_not_uncertain(monkeypatch, tmp_path):
    """Nothing is sent before the ``open`` line is on the disk."""
    from wanderfill.plan import apply as ap

    plan, c, t = _rebuilt(*_four_ticks()[:2], replies=_tick_replies())
    _live(t)
    real = ap.Journal.opening

    def opening(self, op, snapshot=None):
        if op is plan.ops[1]:
            raise OSError("disk full")
        real(self, op, snapshot)

    monkeypatch.setattr(ap.Journal, "opening", opening)
    exc, _ = _run_failing(plan, c, tmp_path)
    assert isinstance(exc, OSError) and _writes(t) == ["quickEnter/updateMQP"]
    assert exc.report.verified["checked"] == 1 and exc.report.verified["uncertain"] is None


def test_an_interrupt_after_the_done_line_is_not_uncertain(monkeypatch, tmp_path):
    """The journal says what is open, not how far this run's bookkeeping got."""
    from wanderfill.plan import apply as ap

    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t)
    real = ap.Journal.note

    def note(self, op, result, error=""):
        real(self, op, result, error)
        if op is plan.ops[1]:
            raise KeyboardInterrupt

    monkeypatch.setattr(ap.Journal, "note", note)
    exc, journal = _run_failing(plan, c, tmp_path)
    assert isinstance(exc, KeyboardInterrupt) and journal.unresolved() == set()
    assert exc.report.verified["checked"] == 2 and exc.report.verified["uncertain"] is None


def test_a_journal_torn_by_the_failure_leaves_the_op_uncertain(monkeypatch, tmp_path):
    from wanderfill.plan import apply as ap

    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t)
    real = ap.Journal.note

    def note(self, op, result, error=""):
        if op is plan.ops[1]:
            with self.path.open("a") as fh:
                fh.write('{"phase": "do')
            raise KeyboardInterrupt
        real(self, op, result, error)

    monkeypatch.setattr(ap.Journal, "note", note)
    exc, _ = _run_failing(plan, c, tmp_path)
    assert isinstance(exc, KeyboardInterrupt)
    assert exc.report.verified["checked"] == 1
    assert exc.report.verified["uncertain"]["key"] == plan.ops[1].key
    assert any("line 4" in e for e in exc.report.errors)


def test_a_partial_read_back_covers_what_was_there_already(tmp_path):
    """Ticked before, or done in this plan's journal: skipped, and still read back."""
    from wanderfill.plan.apply import apply_plan

    ops = [_vop("mark_kye", item=570, label="ticked"), _vop("mark_kye", item=1),
           *_four_ticks()[1:]]
    plan, c, t = _rebuilt(*ops, replies=_tick_replies())
    _live(t, lose=3, error=_lost())
    exc, journal = _run_failing(plan, c, tmp_path)
    assert exc.report.already == ["ticked"] and exc.report.verified["checked"] == 3
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": "done", "key": ops[3].key, "error": "did not land"}))
    (tmp_path / next(p.name for p in tmp_path.glob("verify-*"))).unlink()
    t.webapi, t.ajax_text = FakeTransport.webapi.__get__(t), FakeTransport.ajax_text.__get__(t)
    _live(t, lose=4, error=_lost("again"))
    with pytest.raises(type(_lost()), match="again") as info:
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    seen = info.value.report.verified
    assert seen["checked"] == 4 and seen["mismatches"] == []


@pytest.mark.parametrize("broken", ["verify", "uncertain", "file", "network"])
def test_a_readback_failure_does_not_replace_the_original_error(
        monkeypatch, tmp_path, capsys, broken):
    import pathlib

    from wanderfill.plan import apply as ap

    error = _lost()

    def boom(*a, **kw):
        raise RuntimeError("the read-back broke")

    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=error)
    if broken == "verify":
        monkeypatch.setattr(ap, "verify", boom)
    elif broken == "uncertain":
        monkeypatch.setattr(ap, "_what_is_there_now", boom)
    elif broken == "file":
        real = pathlib.Path.write_text
        monkeypatch.setattr(pathlib.Path, "write_text", lambda self, *a, **kw: (
            boom() if self.name.startswith("verify-") else real(self, *a, **kw)))
    else:
        send = t.webapi

        def dead(path, /, **fields):
            if any(a == "my_series/" for a, _ in t.sent):
                raise RuntimeError("the read-back broke")
            return send(path, **fields)

        t.webapi = dead
    exc, journal = _run_failing(plan, c, tmp_path)
    assert exc is error and exc.__cause__ is None
    assert journal.unresolved() == {plan.ops[2].key}
    said = [exc.report] if broken == "file" else [exc.report, _verify_file(tmp_path)]
    for where in said:
        assert "the read-back broke" in str(where)
    if broken != "file":
        assert said[1]["partial"] is True and said[1]["uncertain"]["key"] == plan.ops[2].key
        assert {"checked", "mismatches", "phantom_trips", "not_counted", "unproven"} <= set(said[1])


def _interrupting(path_or_call, when=lambda: True):
    """Wrap a callable so that it is Ctrl-C instead, once, while ``when`` holds."""
    fired = []

    def call(*a, **kw):
        if not fired and when():
            fired.append(1)
            raise KeyboardInterrupt("in the read-back")
        return path_or_call(*a, **kw)

    call.fired = fired
    return call


def _interrupt_read(t, action, after):
    """Ctrl-C in the first read of ``action`` once ``after`` has been sent."""
    read = t.webapi
    stop = _interrupting(read, lambda: any(a == after for a, _ in t.sent))

    def webapi(path, /, **fields):
        return (stop if path == action else read)(path, **fields)

    t.webapi = webapi
    return stop.fired


ENDINGS = {"lost": (lambda: _lost(), True, 2), "refused": (lambda: ApiError("x", "no"), False, 2),
           "interrupted": (lambda: KeyboardInterrupt("first"), True, 130)}


@pytest.mark.parametrize("where", ["verify", "announcement", "uncertain", "journal", "file",
                                   "file twice"])
@pytest.mark.parametrize("ending", ENDINGS)
def test_an_interrupt_in_the_read_back_does_not_hide_the_error_it_followed(
        monkeypatch, tmp_path, capsys, ending, where):
    """The second Ctrl-C replaced "the answer was lost" and left no verify file."""
    import pathlib

    from wanderfill.plan import apply as ap

    make, left_open, rc = ENDINGS[ending]
    error = make()
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=error)
    kw = {}
    if where == "verify":
        fired = _interrupt_read(t, "kye/get-kye", "my_series/")
    elif where == "announcement":
        kw["on_read_back"] = stop = _interrupting(print)
    elif where == "uncertain":
        monkeypatch.setattr(ap, "_what_is_there_now", stop := _interrupting(ap._what_is_there_now))
    elif where == "journal":
        stop = _interrupting(ap.Journal.unresolved, lambda: any(
            a == "my_series/" for a, _ in t.sent))
        monkeypatch.setattr(ap.Journal, "unresolved", lambda self: stop(self))
    else:
        real, hits = pathlib.Path.write_text, []

        def write_text(self, *a, **kw):
            if self.name.startswith("verify-") and len(hits) < 1 + (where == "file twice"):
                hits.append(1)
                raise KeyboardInterrupt("in the read-back")
            return real(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "write_text", write_text)
        stop = write_text
        stop.fired = hits
    exc, journal = _run_failing(plan, c, tmp_path / "w", **kw)
    if where != "verify":
        fired = stop.fired
    assert fired or (where == "uncertain" and not left_open)
    assert exc is error and (exc.args[:1], exc.__cause__) == (error.args[:1], None)
    assert any(f.name == "write" for f in _frames(exc))
    assert journal.unresolved() == ({plan.ops[2].key} if left_open else set())
    seen = exc.report.verified
    if where == "file twice":
        assert any("could not be written" in e for e in exc.report.errors)
    else:
        assert _verify_file(tmp_path / "w") == json.loads(json.dumps(seen))
    assert seen["partial"] is True and (seen["uncertain"] is not None) == left_open
    missed = [u for u in seen["unproven"] if u.startswith("NOT READ BACK")]
    want = {"verify": [1], "announcement": [0, 1], "journal": [0, 1]}.get(where, [])
    assert len(missed) == len(want)
    assert all(plan.ops[n].key in line for n, line in zip(want, missed, strict=True))
    assert seen["checked"] == 2 - len(want) and seen["mismatches"] == []
    if left_open:
        not_read = where in ("verify", "announcement", "journal", "uncertain")
        assert ("NOT READ BACK" in seen["uncertain"]["now"][0]) == not_read

    # The exit code is the error's, through the CLI.
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=make())
    _interrupt_read(t, "kye/get-kye", "my_series/")
    got, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert got == rc and "NOT READ BACK" in said.out
    assert "attempted 3, succeeded 2, failed 1" in said.out


@pytest.mark.parametrize("ending", ["lost", "refused"])
@pytest.mark.parametrize("where", ["on_progress", "note before", "note after"])
def test_an_interrupt_in_the_cleanup_does_not_replace_the_write_error(
        monkeypatch, tmp_path, ending, where):
    from wanderfill.plan import apply as ap

    make, left_open, _ = ENDINGS[ending]
    error = make()
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=error)

    def progress(i, total, op, ok):
        if not ok and where == "on_progress":
            raise KeyboardInterrupt

    real = ap.Journal.note

    def note(self, op, result, error=""):
        if error and where == "note before":
            raise KeyboardInterrupt
        real(self, op, result, error)
        if error and where == "note after":
            raise KeyboardInterrupt

    monkeypatch.setattr(ap.Journal, "note", note)
    exc, journal = _run_failing(plan, c, tmp_path, on_progress=progress)
    assert exc is error
    opened = left_open or where == "note before"  # nobody closed it, so it stays open
    assert journal.unresolved() == ({plan.ops[2].key} if opened else set())
    lines = journal.path.read_text().splitlines()
    assert len(lines) == 5 + (not opened), "the entry is closed once or not at all"
    seen = _verify_file(tmp_path)
    assert seen["checked"] == 2 and (seen["uncertain"] is not None) == opened


def test_an_interrupt_in_a_finished_runs_read_back_is_raised_after_the_file(
        monkeypatch, tmp_path, capsys):
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t)
    # The KYE list is first needed by op 2; op 3 and op 4 come after it.
    assert _interrupt_read(t, "kye/get-kye", "my_series/") == []
    exc, journal = _run_failing(plan, c, tmp_path / "w")
    assert isinstance(exc, KeyboardInterrupt) and exc.args == ("in the read-back",)
    assert exc.report.succeeded == 4 and journal.unresolved() == set()
    seen = _verify_file(tmp_path / "w")
    assert seen == json.loads(json.dumps(exc.report.verified))
    assert seen["partial"] is True and seen["uncertain"] is None and seen["checked"] == 1
    assert [sum(op.key in u for u in seen["unproven"]) for op in plan.ops] == [0, 1, 1, 1]

    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t)
    _interrupt_read(t, "kye/get-kye", "my_series/")
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 130 and "succeeded 4" in said.out and said.out.count("NOT READ BACK") == 3


def test_verify_called_on_its_own_still_raises_an_interrupt():
    from wanderfill.plan.apply import verify

    c, t = client(_tick_replies())
    t.webapi = _interrupting(t.webapi)
    with pytest.raises(KeyboardInterrupt):
        verify(c, [_vop("mark_kye", item=5)])


def test_an_interrupt_while_the_report_is_printed_does_not_replace_the_error(
        monkeypatch, tmp_path, capsys):
    from wanderfill.cli import main as cli

    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=_lost())
    monkeypatch.setattr(cli, "_print_apply_report", _interrupting(cli._print_apply_report))
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 2 and "answer lost" in said.err


def test_the_read_back_says_what_it_reads_before_it_starts(monkeypatch, tmp_path, capsys):
    """Minutes of silence on a dead network, with nothing to say what it was doing."""
    plan, c, t = _rebuilt(*_four_ticks(), _jan(), replies=_tick_replies())
    _live(t)
    _, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    (line,) = [x for x in said.out.splitlines() if x.startswith("Reading back")]
    assert "5 op(s)" in line and "1 region(s)" in line and "3 list(s)" in line
    assert said.out.index(line) < said.out.index("attempted")

    def broken(line):
        raise BrokenPipeError

    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t)
    from wanderfill.plan.apply import apply_plan
    report = apply_plan(c, plan, workdir=tmp_path / "b", confirm=True, on_read_back=broken)
    assert report.verified["checked"] == 4 and report.verified["unproven"] == []


def test_phantom_trips_are_printed_and_do_not_change_the_exit_code(
        monkeypatch, tmp_path, capsys):
    """add-visit makes one every time, and only the verify file said which."""
    plan, c, t = _rebuilt(_jan_add())
    send = t.webapi

    def lands(path, /, **fields):
        if path == "quickEnter/add-visit":
            t.replies[READ] = visits(_stored(**JAN))
            t.replies[REGIONS_READ] = {"ids": [11]}
        return send(path, **fields)

    t.webapi = lands
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 0 and "trip 900" in said.out and "region 11" in said.out


def test_a_full_run_says_it_is_not_partial(monkeypatch, tmp_path, capsys):
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t)
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 0 and "UNCERTAIN" not in said.out and "NOT READ BACK" not in said.out
    assert _verify_file(tmp_path / "cli") == {
        "checked": 4, "mismatches": [], "phantom_trips": [], "not_counted": [], "unproven": [],
        "partial": False, "uncertain": None}


def test_verify_reads_each_series_once():
    """24 series ticks were 150 reads: the whole list again for every item."""
    from wanderfill.plan.apply import verify

    ops = [_vop("tick_series", series=s, item=n) for s in (1, 2) for n in (570, 571, 5)]
    ops += [_vop("mark_kye", item=n) for n in (570, 571, 5)]
    ops += [_vop("mark_dare", region=n) for n in (570, 571, 5)]
    c, t = client(_tick_replies() | {REGIONS_READ: {"ids": [11, 12]}, READ: visits(_stored(**JAN))})
    out = verify(c, ops)
    assert sorted(a for a, _ in t.sent) == sorted(["V2/", "V2/", "kye/get-kye", DARE_READ])
    assert out["checked"] == 12
    assert out["mismatches"] == ["series 1 item 5 did not stick", "series 2 item 5 did not stick",
                                 "KYE quadrant 5 did not stick", "DARE area 5 did not stick"]
    t.sent.clear()
    verify(c, [_jan_add(), _jan_update(visit_id=1), _jan(), _jan(TRIP_REGION | {"id": 12})])
    assert sorted(a for a, _ in t.sent) == sorted([READ, READ, REGIONS_READ])


def test_a_list_that_cannot_be_read_is_asked_for_once(tmp_path):
    from wanderfill.plan.apply import verify

    c, t = client({"kye/get-kye": {"result": "ERROR", "result_description": "hiccup"}})
    out = verify(c, [_vop("mark_kye", item=n) for n in (1, 2, 3)])
    assert len(t.sent) == 1 and len(out["unproven"]) == 3
    assert all("hiccup" in u for u in out["unproven"]) and out["mismatches"] == []


@pytest.mark.parametrize("reply", BAD_LISTS, ids=str)
@pytest.mark.parametrize("op,action,field", [
    (lambda: _vop("tick_series", series=1, item=5), "V2/", "visited"),
    (lambda: _vop("mark_kye", item=5), "kye/get-kye", "visited"),
    (lambda: _vop("mark_dare", region=5), DARE_READ, "ids"),
    (lambda: _jan_add(), READ, "data"),
    (lambda: _jan_update(), READ, "data"),
    (lambda: _jan(), READ, "data"),
], ids=["series", "kye", "dare", "add", "update", "trip"])
def test_verify_calls_a_missing_list_unproven(op, action, field, reply):
    """Read as empty, it said a write that landed "did not stick"."""
    from wanderfill.plan.apply import verify

    op = op()
    c, _ = client({action: _bad(reply, field),
                   "trips/get-trip": {"trip": {"date_from": "2026-01-01",
                                               "date_to": "2026-01-02"}}})
    out = verify(c, [op], {op.key: {"trip_id": 900}})
    assert out["checked"] == 1 and out["mismatches"] == [] and out["phantom_trips"] == []
    (line,) = out["unproven"]
    assert action in line


def test_unproven_exits_one(monkeypatch, tmp_path, capsys):
    """"The read-back could not tell" exited 0, like a run that was proven."""
    plan, c, t = _rebuilt(_vop("mark_kye", item=5), replies=_tick_replies())
    _live(t)
    send = t.webapi

    def forgets(path, /, **fields):
        if path == "kye/set-kye":
            t.replies["kye/get-kye"] = {"result": "OK"}
            return {"result": "OK"}
        return send(path, **fields)

    t.webapi = forgets
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 1 and "UNPROVEN: KYE quadrant 5" in said.out


def test_a_mismatch_exits_one_and_every_one_is_printed(monkeypatch, tmp_path, capsys):
    ops = [_vop("mark_kye", item=n) for n in range(1, 31)]
    plan, c, _ = _rebuilt(*ops, replies=_tick_replies())
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 1 and said.out.count("MISMATCH") == 30


def test_not_counted_alone_exits_zero(monkeypatch, tmp_path, capsys):
    plan, c, t = _rebuilt(_jan_add(quality=1))
    send = t.webapi

    def lands(path, /, **fields):
        if path == "quickEnter/add-visit":
            t.replies[READ] = visits(_stored(**JAN, quality=1))
        return send(path, **fields)

    t.webapi = lands
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert rc == 0 and "not counted: region 11" in said.out


def test_verify_reads_a_created_trips_header():
    """A trip that owns the right visits can still be dated wrong itself."""
    from wanderfill.plan.apply import verify

    op = _jan(date_to="2026-01-09")
    made = {op.key: {"trip_id": "900"}}

    def against(header, results=made):
        c, t = client({READ: visits(_stored(**JAN)), REGIONS_READ: {"ids": [11]},
                       "trips/get-trip": header})
        return verify(c, [op], results), [f for a, f in t.sent if a == "trips/get-trip"]

    good = {"date_from": "2026-01-01", "date_to": "2026-01-09"}
    out, reads = against({"trip": good})
    assert out["mismatches"] == [] and out["unproven"] == [] and reads == [{"trip_id": 900}]
    out, _ = against({"trip": good | {"date_to": "2026-01-02"}})
    (line,) = out["mismatches"]
    assert "2026-01-01..2026-01-02" in line and "2026-01-01..2026-01-09" in line
    for bad in ({"result": "OK"}, {"trip": None}, {"trip": {}}, [], None, {"trip": "x"},
                {"trip": good | {"date_to": None}}, {"trip": good | {"date_to": "2026"}},
                {"trip": good | {"date_from": 20260101}},
                {"result": "ERROR", "result_description": "Unauthorised."}):
        out, _ = against(bad)
        assert out["mismatches"] == [], bad
        (line,) = out["unproven"]
        assert "header" in line
    out, reads = against({"trip": good}, {})
    assert reads == [] and "could predate it" in out["unproven"][0]


def test_one_unreadable_region_does_not_discard_the_report(tmp_path):
    """One bad reply raised out of verify, and the other 149 checks went with it."""
    from wanderfill.api.errors import TransportError
    from wanderfill.plan.apply import verify

    ops = [_jan_add(), _jan_add(region=12), _jan_update(region=13, visit_id=1),
           _jan(TRIP_REGION | {"id": 14}, TRIP_REGION | {"id": 12}, TRIP_REGION | {"id": 15}),
           _vop("mark_kye", item=570), _vop("mark_kye", item=5)]
    for error in (TransportError("dead"), ApiError(READ, "hiccup"), AttributeError("shape"),
                  TypeError("shape"), KeyError("shape"), ValueError("shape")):
        c, t = client(_tick_replies() | {READ: visits(_stored(**JAN)),
                                         REGIONS_READ: {"ids": [11, 12, 13, 14, 15]},
                                         "trips/get-trip": {"trip": JAN_DAYS}})
        send = t.webapi

        def broken(path, /, send=send, error=error, **fields):
            if fields.get("region") == 12:
                raise error
            return send(path, **fields)

        t.webapi = broken
        out = verify(c, ops, {ops[3].key: {"trip_id": 900}})
        assert out["checked"] == 6
        assert out["mismatches"] == ["KYE quadrant 5 did not stick"]
        assert len(out["unproven"]) == 2 and all("region 12" in u for u in out["unproven"])
        assert [p["region"] for p in out["phantom_trips"]] == [11]


def test_an_unreadable_visited_list_is_unproven_whatever_broke():
    from wanderfill.api.errors import TransportError
    from wanderfill.plan.apply import verify

    for error in (TransportError("dead"), KeyError("ids")):
        c, t = client({READ: visits(_stored(**JAN))})
        send = t.webapi

        def broken(path, /, send=send, error=error, **fields):
            if path == REGIONS_READ:
                raise error
            return send(path, **fields)

        t.webapi = broken
        out = verify(c, [_jan_add(), _jan_add(region=12)])
        assert out["mismatches"] == [] and out["not_counted"] == []
        assert len(out["unproven"]) == 1 and "could not be read back" in out["unproven"][0]


def test_an_op_verify_cannot_check_is_unproven_and_the_rest_are_checked():
    from wanderfill.plan.apply import verify

    c, _ = client(_tick_replies())
    out = verify(c, [_jan({"quality": 3}), _vop("mark_kye", item=5)])
    assert out["mismatches"] == ["KYE quadrant 5 did not stick"]
    assert any("KeyError" in u for u in out["unproven"])


# --------------------------------------------------------------------------
# what the reviews of the whole change found
# --------------------------------------------------------------------------

WHOLE_ROW = _stored(**JAN)
BAD_ROWS = {
    **{f"no {field}": {k: v for k, v in WHOLE_ROW.items() if k != field} for field in WHOLE_ROW},
    "only an id": {"id": 101},
    **{f"{field} {value!r}": WHOLE_ROW | {field: value} for field, value in (
        ("id", None), ("id", "x"), ("id", True), ("id", 1.5), ("quality", None), ("quality", ""),
        ("quality", "good"), ("trip_id", "x"), ("trip_id", True), ("trip_id", []),
        ("year_from", "x"), ("month_to", 13), ("day_from", True), ("day_to", 1.5),
        ("year_from", None), ("year_to", 0))},
    "not an object": 101, "null": None,
}


@pytest.mark.parametrize("name", BAD_ROWS)
def test_a_visit_row_with_a_field_missing_is_not_an_undated_good_visit(
        monkeypatch, tmp_path, capsys, name):
    """``{"id": 101}`` read as an undated quality 3, and could be written back as one."""
    from wanderfill.plan.apply import verify

    bad = visits(BAD_ROWS[name])
    plan, c, t = _rebuilt(_jan_add(**{"date_from": "2026-05-01", "date_to": "2026-05-02"}))
    t.replies[READ] = bad
    assert READ in _refused(plan, c, t, tmp_path, VerificationFailed)
    assert "BLOCKERS" in _checked(monkeypatch, capsys, plan, c, tmp_path, 1)
    out = verify(c, plan.ops)
    assert out["mismatches"] == [] and READ in out["unproven"][0]
    # ...and the read an update is checked against: nothing is sent.
    with pytest.raises(PrecisionLoss, match="blind"):
        c.update_visit(1, 11, dt.date(2026, 1, 1), dt.date(2026, 1, 2), quality=3)
    assert _writes(t) == []


@pytest.mark.parametrize("over", [
    {}, dict.fromkeys(JAN), {"trip_id": None}, {"id": "1", "quality": "3", "trip_id": "9"},
    dict.fromkeys(("month_from", "day_from", "month_to", "day_to")), {"hidden": 0},
], ids=str)
def test_a_whole_visit_row_is_still_read(over):
    c, _ = client({READ: visits(WHOLE_ROW | over)})
    (visit,) = c._visits_strict(11)
    assert visit == Visit.from_api(WHOLE_ROW | over, 11)


# the write action, and an op that sends it as the third of four
PLAN_WRITES = {
    "add_visit": ("quickEnter/add-visit", lambda: _jan_add(region=13)),
    "update_visit": ("quickEnter/update-visit", lambda: _vop(
        "update_visit", region=13, visit_id=1, quality=3, **JAN_DAYS)),
    "create_trip": ("trips/new-trip", lambda: _jan(TRIP_REGION | {"id": 13})),
    "mark_dare": ("quickEnter/updateMQP", lambda: _vop("mark_dare", region=3)),
    "mark_kye": ("kye/set-kye", lambda: _vop("mark_kye", item=3)),
    "mark_tcc": ("quickEnter/updateTCC", lambda: _vop("mark_tcc", item=64)),
}
NOT_OK = [{}, {"status": 1}, {"result": None}, {"result": "ok"}, {"result": ["OK"]},
          {"result": "FAIL", "trip_id": 900, "visit_id": 900}]
NO_ID = [{}, {"trip_id": None, "visit_id": None}, {"trip_id": "", "visit_id": ""},
         {"trip_id": 0, "visit_id": 0}, {"trip_id": True, "visit_id": True},
         {"trip_id": "x", "visit_id": "x"}, {"trip_id": [9], "visit_id": [9]},
         {"trip_id": -1, "visit_id": -1}]
UNUSABLE = [(kind, answer) for kind in PLAN_WRITES for answer in NOT_OK]
UNUSABLE += [(kind, {"result": "OK"} | ids) for kind in ("add_visit", "create_trip")
             for ids in NO_ID]


@pytest.mark.parametrize("kind,answer", UNUSABLE, ids=str)
def test_a_write_answered_without_ok_stops_the_run_and_stays_open(tmp_path, kind, answer):
    """Any JSON object counted as success: op 4 went out on top of an op 3 nobody knows."""
    from wanderfill.api.errors import UnknownWriteOutcome

    action, op = PLAN_WRITES[kind]
    ops = [_vop("mark_kye", item=1), _vop("tick_series", series=1, item=2), op(),
           _vop("tick_series", series=1, item=4)]
    rows = [_stored(**JAN)] if kind == "update_visit" else ()
    plan, c, t = _rebuilt(*ops, rows=rows, replies=_tick_replies())
    real, seen = t.webapi, []

    def webapi(path, /, **fields):
        if path == action:
            seen.append(fields)
            if fields.get("qid") != 1:
                t.sent.append((path, fields))
                return answer
        return real(path, **fields)

    t.webapi = webapi
    exc, journal = _run_failing(plan, c, tmp_path)
    assert isinstance(exc, UnknownWriteOutcome) and action in str(exc)
    assert journal.unresolved() == {ops[2].key}
    assert journal.done_keys() == {ops[0].key, ops[1].key}
    assert [f["item"] for a, f in t.sent if a == "my_series/"] == [2], "op 4 is not sent"
    assert exc.report.verified["uncertain"]["key"] == ops[2].key


@pytest.mark.parametrize("kind", PLAN_WRITES)
def test_an_error_answer_is_still_a_refusal(tmp_path, kind):
    action, op = PLAN_WRITES[kind]
    op = op()
    error = {"result": "ERROR", "result_description": "Missing params"}
    plan, c, _ = _rebuilt(op, rows=[_stored(**JAN)] if kind == "update_visit" else (),
                          replies=_tick_replies() | {action: error})
    exc, journal = _run_failing(plan, c, tmp_path)
    assert type(exc) is ApiError and journal.unresolved() == set()


@pytest.mark.parametrize("trip_id", [900, "900"])
def test_the_recorded_answers_are_enough(tmp_path, trip_id):
    from wanderfill.plan.apply import apply_plan

    plan, c, _ = _rebuilt(_jan(), _jan_add(region=12), replies={
        "trips/new-trip": {"result": "OK", "trip_id": trip_id},
        "quickEnter/add-visit": {"result": "OK", "visit_id": str(trip_id)}})
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == 2


@pytest.mark.parametrize("method", sorted(UNDO_METHODS))
def test_a_delete_answered_without_ok_is_settled_by_its_read_back(method):
    """What these endpoints answer was not recorded; the read-back is the proof."""
    call, read, before = _undo_calls()[method]
    gone = {"delete_trip": GONE, "unmark_dare": {"ids": []}, "unmark_kye": {"visited": []},
            }.get(method, visits())
    writes_ = ("quickEnter/delete-visit", "quickEnter/set-not-visited", "trips/delete-trip",
               "quickEnter/updateMQP", "kye/set-kye")
    t = SequencedTransport({read: [before, gone], YEAR: [{"trips": []}]},
                           {action: {} for action in writes_})
    call(NomadMania(token="fake", transport=t))


def _syncs(monkeypatch):
    """Every directory fsynced from now on, in order; the caller adds what it sends."""
    import os
    import stat

    events = []
    real = os.fsync

    def fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            events.append((st.st_dev, st.st_ino))
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return events


def _ino(path):
    st = path.stat()
    return st.st_dev, st.st_ino


def test_a_new_journals_directory_entry_is_on_the_disk_before_the_request(monkeypatch, tmp_path):
    """The line was synced and the file's name was not: a power cut could take the file."""
    from wanderfill.plan.apply import Journal

    events = _syncs(monkeypatch)
    real = FakeTransport.webapi

    def webapi(self, action, **fields):
        if Transport._is_write(action):
            events.append("sent")
        return real(self, action, **fields)

    monkeypatch.setattr(FakeTransport, "webapi", webapi)
    work = tmp_path / "a" / "b"
    report, _ = _apply(work)
    assert report.succeeded == 1
    before = events[:events.index("sent")]
    for made in (tmp_path, tmp_path / "a", work):
        assert _ino(made) in before, made
    events.clear()
    (path,) = work.glob("journal-*")
    Journal(path).opening(Op(kind="mark_dare", region=2))
    assert events == [], "only for a new journal"

    # A deletion's journal too.
    events.clear()
    work = tmp_path / "d"
    fake = FakeClient()
    fake.unmark_dare = lambda dare_id: events.append("sent")
    _run_delete(monkeypatch, work, fake, "dare", "1142", "--confirm", "--reason", "x")
    before = events[:events.index("sent")]
    assert _ino(tmp_path) in before and _ino(work) in before


def _dir_sync_fails(monkeypatch, number, *, call="fsync", only=None):
    """Make opening, or syncing, a directory fail; a file's own sync still works."""
    import os
    import stat

    real_open, real_fsync = os.open, os.fsync

    def open_(path, flags, *a, **kw):
        if call == "open" and only in (None, path):
            raise OSError(number, os.strerror(number), str(path))
        return real_open(path, flags, *a, **kw)

    def fsync(fd):
        st = os.fstat(fd)
        if (call == "fsync" and stat.S_ISDIR(st.st_mode)
                and (only is None or _ino(only) == (st.st_dev, st.st_ino))):
            raise OSError(number, os.strerror(number))
        real_fsync(fd)

    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "fsync", fsync)


@pytest.mark.parametrize("name", ["EINVAL", "ENOTSUP", "EOPNOTSUPP", "EBADF"])
def test_a_directory_that_cannot_be_synced_does_not_stop_the_run(monkeypatch, tmp_path, name):
    """Some network filesystems refuse the sync."""
    import errno

    _dir_sync_fails(monkeypatch, getattr(errno, name))
    report, _ = _apply(tmp_path / "w")
    assert report.succeeded == 1


def test_a_platform_without_o_directory_syncs_none(monkeypatch, tmp_path):
    """Windows cannot open a directory."""
    import errno
    import os

    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    _dir_sync_fails(monkeypatch, errno.EIO, call="open")
    report, _ = _apply(tmp_path / "w")
    assert report.succeeded == 1


@pytest.mark.parametrize("call,name", [("fsync", "EIO"), ("fsync", "ENOSPC"), ("fsync", "EROFS"),
                                       ("fsync", "EDQUOT"), ("fsync", "EACCES"), ("open", "EIO"),
                                       ("open", "EACCES"), ("open", "EPERM"), ("open", "EISDIR"),
                                       ("open", "EMFILE"), ("open", "EINVAL")])
@pytest.mark.parametrize("where", ["journal", "work directory"])
def test_a_directory_sync_that_fails_stops_the_run_before_it_sends(
        monkeypatch, tmp_path, capsys, call, name, where):
    """Every error was taken for "cannot", a failing disk included, and the write went out
    with a journal that might not be there afterwards."""
    import errno

    work = tmp_path / "w"
    if where == "journal":
        work.mkdir()
    bad = work if where == "journal" else tmp_path
    _dir_sync_fails(monkeypatch, getattr(errno, name), call=call, only=bad)
    for again in (1, 2):  # nothing of the first attempt lets the second one past
        exc, sent = _apply(work)
        assert isinstance(exc, DriftError), again
        assert str(bad) in str(exc) and "Nothing was sent" in str(exc)
        assert not [a for a, _ in sent if Transport._is_write(a)]
        assert all(p.read_bytes() == b"" for p in work.glob("journal-*")), "no open line"
        if where == "journal":
            assert exc.report.verified["uncertain"] is None
    fake = FakeClient()
    assert _run_delete(monkeypatch, work, fake, *UNMARK) in (1, 2)
    said = capsys.readouterr()
    assert fake.calls == [] and str(bad) in said.out + said.err
    assert "Nothing was sent" in said.out + said.err and "left OPEN" not in said.out
    assert _journal_lines(work) == []


class _DeadReadBack(SequencedTransport):
    """The DARE list can be read until the write, and never after it."""

    def webapi(self, action, **fields):
        from wanderfill.api.errors import TransportError

        if action == DARE_READ and _writes(self):
            raise TransportError("wifi")
        return super().webapi(action, **fields)


UNMARK = ("dare", "1142", "--confirm", "--reason", "typo")


def _unmark(monkeypatch, tmp_path, capsys, t):
    c = NomadMania(token="fake", transport=t)
    capsys.readouterr()
    rc = _run_delete(monkeypatch, tmp_path, c, *UNMARK)
    return rc, [e["phase"] for e in _journal_lines(tmp_path)], capsys.readouterr()


DELETE_STATE = {"user/status-quick": {"result": "OK", "uid": 79597}, DARE_READ: {"ids": [1142]}}
CLOSING = ('"phase": "done"', '"error"', "Read the server back")


@pytest.mark.parametrize("code,closed", [(403, True), (401, True), (400, True), (500, False),
                                         (302, False), (408, False)])
def test_a_delete_closes_on_the_refusals_an_apply_closes_on(
        monkeypatch, tmp_path, capsys, code, closed):
    """A 403 on the write left it open, and blocked the account over nothing."""
    real, calls = _answering(monkeypatch, code)

    class WritesForReal(FakeTransport):
        def webapi(self, action, **fields):
            write = Transport._is_write(action)
            return (real if write else super()).webapi(action, **fields)

    rc, phases, said = _unmark(monkeypatch, tmp_path, capsys, WritesForReal(DELETE_STATE))
    assert rc == 1 and len(calls) == 1
    assert phases == (["open", "done"] if closed else ["open"])
    assert all((x in said.out) != closed for x in CLOSING)
    if closed:
        assert _journal_lines(tmp_path)[1]["error"]


def test_a_dead_read_back_after_a_delete_stays_open(monkeypatch, tmp_path, capsys):
    """It is a TransportError underneath, which on the write itself would close."""
    rc, phases, said = _unmark(monkeypatch, tmp_path, capsys, _DeadReadBack({}, DELETE_STATE))
    assert rc == 1 and phases == ["open"]
    assert all(x in said.out for x in CLOSING) and "deletes-79597" in said.out


@pytest.mark.parametrize("error", [RuntimeError("a bug of ours"), KeyboardInterrupt()], ids=repr)
def test_a_delete_that_ends_unforeseen_says_how_to_close_its_entry(
        monkeypatch, tmp_path, capsys, error):
    fake = FakeClient(outcome=error)
    if isinstance(error, KeyboardInterrupt):
        assert _run_delete(monkeypatch, tmp_path, fake, *UNMARK) == 130
    else:
        with pytest.raises(RuntimeError, match="a bug of ours"):
            _run_delete(monkeypatch, tmp_path, fake, *UNMARK)
    said = capsys.readouterr()
    assert all(x in said.out for x in CLOSING)
    assert [e["phase"] for e in _journal_lines(tmp_path)] == ["open"]


def test_a_delete_whose_done_line_will_not_go_in_says_so(monkeypatch, tmp_path, capsys):
    from wanderfill.plan import apply as ap

    def full(self, op, result, error=""):
        raise OSError("disk full")

    monkeypatch.setattr(ap.Journal, "note", full)
    for outcome in (None, ApiError("quickEnter/updateMQP", "no")):
        for f in tmp_path.glob("deletes-*"):
            f.unlink()
        with pytest.raises(OSError, match="disk full"):
            _run_delete(monkeypatch, tmp_path, FakeClient(outcome=outcome), *UNMARK)
        said = capsys.readouterr().out
        assert all(x in said for x in CLOSING)


def test_every_command_says_the_same_about_an_open_entry(monkeypatch, tmp_path, capsys):
    from wanderfill.plan.apply import CLOSE_BY_HAND

    _open_line(tmp_path / "journal-79597-aaaaaaaaaaaa.ndjson")
    assert _run_delete(monkeypatch, tmp_path, FakeClient(), *UNMARK) == 2
    assert CLOSE_BY_HAND in capsys.readouterr().out
    _open_line(tmp_path / "journal-7-aaaaaaaaaaaa.ndjson")
    exc, _ = _apply(tmp_path)
    assert CLOSE_BY_HAND in str(exc)
    plan, c, t = _rebuilt(*_four_ticks(), replies=_tick_replies())
    _live(t, lose=3, error=_lost())
    monkeypatch.setattr("wanderfill.cli.main.DEFAULT_WORKDIR", tmp_path / "none")
    _, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    assert CLOSE_BY_HAND in said.out and "journal-7-" in said.out


@pytest.mark.parametrize("command", [["apply"], ["apply", "--confirm"], ["check"], ["show"]])
def test_a_plan_file_that_cannot_load_exits_one(monkeypatch, tmp_path, capsys, command):
    """It exited 2, which is an error that ended or refused a run; this is not a plan."""
    from wanderfill.cli import main as cli

    c, t = client()
    monkeypatch.setattr(cli, "_client", lambda args: c)
    path = tmp_path / "p.json"
    for text in ("{", '{"account": 7, "surprise": 1}', None):
        path.unlink() if text is None else path.write_text(text)
        assert cli.main([command[0], str(path), *command[1:]]) == 1
        assert str(path) in capsys.readouterr().err
    assert t.sent == []


def _stateful(plan_ops, rows_after):
    """A plan on a server that keeps the visit a trip or an add makes."""
    plan, c, t = _rebuilt(*plan_ops)
    real = t.webapi

    def webapi(path, /, **fields):
        if path in ("trips/new-trip", "quickEnter/add-visit"):
            t.replies[READ] = visits(rows_after)
            t.replies[REGIONS_READ] = {"ids": [11]}
        return real(path, **fields)

    t.webapi = webapi
    t.replies["trips/get-trip"] = {"trip": JAN_DAYS}
    return plan, c, t


@pytest.mark.parametrize("op", [
    pytest.param(lambda: _jan(), id="trip"),
    pytest.param(lambda: _jan({"id": 11, "quality": 3}), id="trip, dates from the header"),
    pytest.param(lambda: _jan_add(), id="add"),
])
def test_a_trip_deleted_after_it_landed_is_not_skipped_as_done(monkeypatch, tmp_path, capsys, op):
    """Applied, deleted by the owner, applied again: the profile matches the plan's basis
    once more, the journal says done, and the run "finished" without a trip."""
    from wanderfill.plan.apply import Journal, apply_plan

    plan, c, t = _stateful([op()], _stored(**JAN, trip_id=901))
    first = apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert first.succeeded == 1 and first.verified["mismatches"] == []
    (path,) = tmp_path.glob("journal-*")
    t.sent.clear()
    with pytest.raises(DriftError, match="live state changed"):  # it is there, and done
        apply_plan(c, plan, workdir=tmp_path, confirm=True)

    t.replies[READ], t.replies[REGIONS_READ] = visits(), {"ids": []}  # the owner deletes it
    with pytest.raises(DriftError) as info:
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    for wanted in (str(path), "the op", plan.ops[0].kind, "2026-01-01..2026-01-02", "region 11",
                   "gone now", "new plan", "Nothing was sent"):
        assert wanted in str(info.value)
    assert _writes(t) == [] and Journal(path).unresolved() == set()

    from wanderfill.cli import main as cli
    monkeypatch.setattr(cli, "_client", lambda args: c)
    saved = plan.save(tmp_path / "p.json")
    capsys.readouterr()
    assert cli.main(["check", str(saved), "--workdir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "BLOCKERS" in out and "gone now" in out and str(path) in out and "unchanged" in out

    # The way out the message names: a new plan, with a journal of its own.
    again = Plan(account=7, ops=plan.ops, basis=plan.basis | {"taken_at": "later"})
    assert apply_plan(c, again, workdir=tmp_path, confirm=True).succeeded == 1


@pytest.mark.parametrize("name", TICKED)
def test_a_tick_taken_back_after_it_landed_is_not_skipped_as_done(
        monkeypatch, tmp_path, capsys, name):
    from wanderfill.plan.apply import Journal, apply_plan
    from wanderfill.plan.model import fingerprint

    op = TICKED[name]()
    plan, c, t = _rebuilt(op, _vop("tick_series", series=9, item=1), replies={
        "V2/": {"visited": []}, "kye/get-kye": {"visited": []}})
    if name == "tcc":
        op.item = 64  # in the list, and not ticked
    path = tmp_path / f"journal-7-{fingerprint(plan.basis)[:12]}.ndjson"
    Journal(path).note(op, {"result": "OK"})
    with pytest.raises(DriftError, match="gone now") as info:
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert str(path) in str(info.value) and _writes(t) == []
    assert not any(a == "my_series/" for a, _ in t.sent)


def test_a_half_applied_tick_plan_can_still_be_finished(monkeypatch, tmp_path, capsys):
    from wanderfill.plan.apply import apply_plan

    ops = [_vop("mark_kye", item=1), *_four_ticks()[1:]]
    plan, c, t = _rebuilt(*ops, replies=_tick_replies())
    _live(t, lose=3, error=_lost())
    _, journal = _run_failing(plan, c, tmp_path)
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": "done", "key": ops[2].key, "error": "did not land"}))
    t.webapi, t.ajax_text = FakeTransport.webapi.__get__(t), FakeTransport.ajax_text.__get__(t)
    _live(t)
    report = apply_plan(c, plan, workdir=tmp_path, confirm=True)
    assert (report.skipped, report.succeeded) == (2, 2)
    assert report.verified["mismatches"] == []


def test_updates_and_vague_visits_are_not_judged_gone(tmp_path):
    """Two real visits can share a year, and an update leaves nothing to look for."""
    from wanderfill.plan.apply import Journal, apply_plan
    from wanderfill.plan.model import fingerprint

    ops = [_vop(region=11, quality=3), _vop(region=11, quality=3, date_from="2026", date_to="2026"),
           _vop(region=11, quality=3, date_from="2026-01-01", date_to="2026"),
           _jan_update(),
           _jan(TRIP_REGION | dict.fromkeys(("month_from", "day_from", "month_to", "day_to")))]
    plan, c, t = _rebuilt(*ops)
    j = Journal(tmp_path / f"journal-7-{fingerprint(plan.basis)[:12]}.ndjson")
    for op in ops:
        j.note(op, {"result": "OK"})
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).skipped == len(ops)
    assert _writes(t) == []


def test_an_op_skipped_as_ticked_is_not_printed_as_ok(monkeypatch, tmp_path, capsys):
    plan, c, t = _rebuilt(_vop("mark_kye", item=570, label="there"),
                          _vop("mark_kye", item=5, label="new"), replies=_tick_replies())
    _live(t)
    rc, said = _apply_cli(monkeypatch, capsys, plan, c, tmp_path)
    there, new = (next(x for x in said.out.splitlines() if x.startswith("  [") and w in x)
                  for w in ("there", "new"))
    assert rc == 0 and "skip" in there and " ok " not in there and " ok " in new


def _broken_dirs(tmp_path):
    """Directories that cannot be read or locked, whoever runs the suite."""
    a_file = tmp_path / "a-file"
    a_file.write_text("")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    return a_file, loop


@pytest.mark.parametrize("n", [0, 1], ids=["a file", "a link to itself"])
def test_a_directory_that_cannot_be_used_is_a_refusal_not_a_traceback(
        monkeypatch, tmp_path, capsys, n):
    bad = _broken_dirs(tmp_path)[n]
    exc, sent = _apply(tmp_path / "w", also=(bad,))
    assert isinstance(exc, DriftError) and str(bad) in str(exc) and sent == ACCOUNT_READ
    exc, sent = _apply(bad)
    assert isinstance(exc, DriftError) and str(bad) in str(exc) and sent == ACCOUNT_READ
    exc, sent = _apply(bad / "below")
    assert isinstance(exc, DriftError) and str(bad) in str(exc) and sent == ACCOUNT_READ

    fake = FakeClient()
    assert _run_delete(monkeypatch, bad, fake, *UNMARK) == 2
    assert fake.calls == [] and str(bad) in capsys.readouterr().err
    from wanderfill.cli import main as cli
    c, _ = client(STATE)
    monkeypatch.setattr(cli, "_client", lambda args: c)
    plan = _dare_plan().save(tmp_path / "p.json")
    assert cli.main(["check", str(plan), "--workdir", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "BLOCKERS" in out and str(bad) in out


def test_a_directory_that_cannot_be_listed_or_locked_is_a_refusal(monkeypatch, tmp_path):
    import errno
    import pathlib

    from wanderfill.plan.apply import account_lock, open_entries

    fcntl = pytest.importorskip("fcntl")

    def denied(self):
        raise PermissionError(errno.EACCES, "Permission denied")
        yield

    with monkeypatch.context() as m:
        m.setattr(pathlib.Path, "iterdir", denied)
        with pytest.raises(DriftError, match="Permission denied") as info:
            open_entries([tmp_path], 7)
        assert str(tmp_path) in str(info.value)
    (tmp_path / "journal-7-aaaaaaaaaaaa.ndjson").mkdir()  # a journal that cannot be read
    with pytest.raises(DriftError, match="journal-7-a"):
        open_entries([tmp_path], 7)

    def no_locks(*a):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)
    with pytest.raises(DriftError, match="No locks available") as info:
        with account_lock([tmp_path], 7):
            raise AssertionError("not reached")
    assert str(tmp_path) in str(info.value)


def test_an_error_under_the_lock_is_not_taken_for_the_lock_failing(tmp_path):
    from wanderfill.plan.apply import account_lock

    with pytest.raises(OSError, match="disk full") as info:
        with account_lock([tmp_path], 7):
            raise OSError("disk full")
    assert not isinstance(info.value, DriftError)


def test_check_counts_only_what_would_be_sent(monkeypatch, tmp_path, capsys):
    """A finished plan of nine ops printed "9 already applied", then "9 would execute"."""
    from wanderfill.cli import main as cli
    from wanderfill.plan.apply import Journal
    from wanderfill.plan.model import fingerprint

    ops = [_vop("mark_kye", item=570, label="done"), _vop("mark_kye", item=571, label="ticked"),
           _vop("tick_series", series=1, item=5),
           _vop("tick_series", series=1, item=5, date_from="2024", label="the same tick"),
           _jan_add()]
    plan, c, t = _rebuilt(*ops, replies=_tick_replies())
    journal = Journal(tmp_path / f"journal-7-{fingerprint(plan.basis)[:12]}.ndjson")
    journal.note(ops[0], {"result": "OK"})
    monkeypatch.setattr(cli, "_client", lambda args: c)
    saved = plan.save(tmp_path / "p.json")

    def check(rc):
        capsys.readouterr()
        assert cli.main(["check", str(saved), "--workdir", str(tmp_path)]) == rc
        return capsys.readouterr().out

    out = check(0)
    assert "1 op(s) already applied" in out and "2 op(s) already ticked" in out
    assert "Left to send: 2 of 5 op(s), in 1 region(s)." in out and "would execute" not in out
    t.replies["V2/"] = {"result": "OK"}  # the list is gone: nobody knows what is ticked
    assert "Left to send: at most 3 of 5 op(s)" in check(1)
    t.replies["V2/"] = {"visited": [5]}
    for op in ops[2:]:
        journal.note(op, {"result": "OK"})
    t.replies[READ] = visits(_stored(**JAN))  # what a finished plan finds
    assert "Left to send: 0 of 5 op(s), in 0 region(s)." in check(1)
    journal.path.write_text("torn")
    assert "Left to send: at most" in check(1)


def test_check_calls_everything_at_most_when_the_profile_cannot_be_read(
        monkeypatch, tmp_path, capsys):
    plan, c, t = _rebuilt(_jan_add(), _vop("mark_kye", item=5), replies=_tick_replies())
    t.replies[REGIONS_READ] = {"result": "OK"}
    out = _checked(monkeypatch, capsys, plan, c, tmp_path, 1)
    assert "Left to send: at most 2 of 2 op(s)" in out


def test_the_old_key_guidance_is_said_once(monkeypatch, tmp_path, capsys):
    """A paragraph for every trip: thirty trips, thirty copies of it."""
    from wanderfill.cli import main as cli
    from wanderfill.plan.apply import apply_plan
    from wanderfill.plan.model import basis_of, fingerprint

    trips = [_jan(TRIP_REGION | {"id": n}, label=f"trip {n}") for n in (11, 12, 13)]
    plan, path, c, _ = _done_before(tmp_path, *(op.legacy_key for op in trips), trip=trips[0])
    plan.ops += trips[1:]
    snap = c.snapshot([11, 12, 13])
    plan.basis = {"fingerprint": fingerprint(basis_of(snap, [11, 12, 13]))}
    path.rename(tmp_path / f"journal-7-{fingerprint(plan.basis)[:12]}.ndjson")
    with pytest.raises(DriftError) as info:
        apply_plan(c, plan, workdir=tmp_path, confirm=True)
    monkeypatch.setattr(cli, "_client", lambda args: c)
    saved = plan.save(tmp_path / "p.json")
    assert cli.main(["check", str(saved), "--workdir", str(tmp_path)]) == 1
    for said in (str(info.value), capsys.readouterr().out):
        assert all(f"'trip {n}'" in said for n in (11, 12, 13))
        assert all(op.legacy_key in said for op in trips)
        assert said.count("rebuild the plan from the live profile") == 1
        assert said.count("Read the server back") == 1


@pytest.mark.parametrize("name", ["deletes-7.ndjson", "verify-20261124T120000Z.json",
                                  "journal-7-aaaaaaaaaaaa.ndjson", "snapshot-x.json", ".lock-7"])
def test_what_a_run_leaves_behind_is_never_committed(name):
    """A --workdir inside the repository: a deletion's journal holds the whole record."""
    import fnmatch
    import pathlib

    ignored = pathlib.Path(__file__).parents[1].joinpath(".gitignore").read_text().split("\n")
    assert any(fnmatch.fnmatch(name, pattern) for pattern in ignored if pattern)


# region 658 on the live profile: two standalone visits read like this
YEAR_ZERO = {"year_from": 0, "month_from": None, "day_from": None,
             "year_to": 0, "month_to": None, "day_to": None}


def _ends(year, month, day):
    return {f"{unit}_{side}": v for side in ("from", "to")
            for unit, v in (("year", year), ("month", month), ("day", day))}


@pytest.mark.parametrize("parts", [(0, None, None), ("0", None, None), (0, 0, 0), ("0", "0", "0"),
                                   (None, 0, 0), (0, None, 0)], ids=str)
def test_a_year_of_zero_is_a_visit_with_no_date(tmp_path, parts):
    """The strict reader refused a real region, and with it every plan that touches one.

    Nothing but zeroes and nulls has nothing in it to lose; plans fingerprinted
    when it read as no date still match (the number was made by 255e0a8).
    """
    from wanderfill.plan.apply import apply_plan
    from wanderfill.plan.model import basis_of, fingerprint

    row = WHOLE_ROW | {"trip_id": None} | _ends(*parts)
    c, _ = client(STATE | {REGIONS_READ: {"ids": [658]}, READ: visits(row)})
    (strict,), (lenient,) = c._visits_strict(658), c.visits_for_region(658)
    assert strict == lenient and (strict.date_from, strict.date_to) == (None, None)
    basis = fingerprint(basis_of(c.snapshot([658]), [658]))
    assert basis == "4f25a6313e82eb66a379ad68946d4157"
    # One end alone is judged alone.
    half = row | {k: v for k, v in WHOLE_ROW.items() if k.endswith("_to")}
    assert Visit.from_api(half, 658) == NomadMania(
        token="fake", transport=FakeTransport({READ: visits(half)}))._visits_strict(658)[0]
    # An old plan beside such a visit applies.
    plan = Plan(account=7, ops=[_jan_add(region=658)], basis={"fingerprint": basis})
    assert apply_plan(c, plan, workdir=tmp_path, confirm=True).succeeded == 1


@pytest.mark.parametrize("parts", [(0, 5, None), (0, None, 6), (0, 5, 6), ("0", 5, 6), (2024, 0, 0),
                                   (2024, 0, 5), (2024, 5, 0), (None, 5, 0), (None, 0, 5),
                                   (2024, 0, None), (2024, None, 0), ("2024", "0", None),
                                   (2024, None, "0"), ("2024", "0", "0"), ("0", "5", None)],
                         ids=str)
@pytest.mark.parametrize("side", ["from", "to"])
def test_a_zero_beside_a_real_part_is_still_unreadable(parts, side):
    """The lenient reader drops the whole date, or takes 2024-0 for the year alone, and
    an update posted that year back over whatever the zero stood for."""
    row = WHOLE_ROW | {k: v for k, v in _ends(*parts).items() if k.endswith(side)}
    c, t = client({READ: visits(row)})
    with pytest.raises(VerificationFailed, match=f"{side} date"):
        c._visits_strict(11)
    for days in ((YearOnly(2024), YearOnly(2024)), (dt.date(2026, 1, 1), dt.date(2026, 1, 2))):
        with pytest.raises(PrecisionLoss, match="blind"):
            c.update_visit(1, 11, *days, quality=3, allow_vaguer=True)
    assert _writes(t) == []


@pytest.mark.parametrize("parts", [(2024, None, None), ("2024", None, None)], ids=str)
def test_a_year_with_no_month_or_day_is_still_a_bare_year(parts):
    c, t = client({READ: visits(WHOLE_ROW | _ends(*parts))})
    (visit,) = c._visits_strict(11)
    assert (visit.date_from, visit.date_to) == (YearOnly(2024), YearOnly(2024))
    c.update_visit(1, 11, dt.date(2024, 1, 1), dt.date(2024, 1, 2), quality=3)
    assert _writes(t) == ["quickEnter/update-visit"]


@pytest.mark.parametrize("given", [{}, JAN_DAYS, {"date_from": "2013", "date_to": "2013"},
                                   {"date_from": "2026-01-01"}], ids=str)
def test_updating_a_year_zero_visit_posts_no_zero_and_invents_no_date(tmp_path, given):
    """update-visit replaces the record: an end the op leaves out goes as empty fields,
    which is what an undated visit has always been sent back as."""
    from wanderfill.plan.apply import apply_plan

    op = _vop("update_visit", region=658, visit_id=1, quality=2, **given)
    plan, c, t = _rebuilt(op, rows=[_stored(**YEAR_ZERO, trip_id=None, quality=3)],
                          replies={REGIONS_READ: {"ids": [658]}})
    report = apply_plan(c, plan, workdir=tmp_path, confirm=True)
    ((_, sent),) = writes(t, "quickEnter/update-visit")
    assert sent["quality"] == 3, "and the stored quality is kept"
    for side in ("from", "to"):
        end = given.get(f"date_{side}")
        want = [int(p) for p in end.split("-")] if end else []
        got = [sent[f"{unit}_{side}"] for unit in ("year", "month", "day")]
        assert got == want + [""] * (3 - len(want))
    if not given:
        assert report.verified["mismatches"] == [] and report.verified["unproven"] == []


def test_a_run_after_a_failed_parent_sync_syncs_the_parent_before_it_writes(monkeypatch, tmp_path):
    """The directory is there by then, so nothing says this run did not make it durable."""
    import errno

    work = tmp_path / "w"
    with monkeypatch.context() as m:
        _dir_sync_fails(m, errno.EIO, only=tmp_path)
        exc, _ = _apply(work)
        assert isinstance(exc, DriftError) and str(tmp_path) in str(exc)
    assert work.is_dir()
    for run in ("apply", "delete"):
        events = _syncs(monkeypatch)
        real = FakeTransport.webapi

        def webapi(self, action, real=real, events=events, **fields):
            if Transport._is_write(action):
                events.append("sent")
            return real(self, action, **fields)

        if run == "apply":
            monkeypatch.setattr(FakeTransport, "webapi", webapi)
            report, _ = _apply(work)
            assert report.succeeded == 1
        else:
            fake = FakeClient()
            fake.unmark_dare = lambda dare_id, events=events: events.append("sent")
            assert _run_delete(monkeypatch, work, fake, *UNMARK) == 0
        before = events[:events.index("sent")]
        assert _ino(work) in before and _ino(tmp_path) in before, run


def test_a_work_directory_named_by_a_link_or_relatively_syncs_its_real_parent(
        monkeypatch, tmp_path):
    from wanderfill.plan.apply import Journal

    work = tmp_path / "real" / "w"
    work.mkdir(parents=True)
    (tmp_path / "link").symlink_to(work)
    monkeypatch.chdir(work)
    import pathlib
    for n, spelled in enumerate((tmp_path / "link", pathlib.Path("."))):
        events = _syncs(monkeypatch)
        Journal(spelled / f"journal-7-{n}.ndjson").opening(Op(kind="mark_dare", region=1))
        assert _ino(work) in events and _ino(work.parent) in events


@pytest.mark.parametrize("ours", [True, False], ids=["made by this run", "made by another"])
def test_no_directory_is_ever_removed(monkeypatch, tmp_path, ours):
    """Between "is it there" and mkdir another run can make it, and was losing it."""
    import errno
    import pathlib

    work = tmp_path / "a" / "w"
    real = pathlib.Path.mkdir

    def mkdir(self, *a, **kw):
        if not ours and self == work:
            real(self, parents=True, exist_ok=True)
        return real(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "mkdir", mkdir)
    _dir_sync_fails(monkeypatch, errno.EIO)
    exc, _ = _apply(work)
    assert isinstance(exc, DriftError) and "Nothing was sent" in str(exc)
    assert work.is_dir()
