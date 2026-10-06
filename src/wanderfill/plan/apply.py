"""Executing a plan, carefully.

Everything here exists because of something that actually went wrong the first
time this was done by hand. The preconditions are not ceremony.

There is no delete code path in this module. Not behind a flag, not behind a
confirmation — absent. Cleaning up records the tool did not create is a
conversation with a human, not a feature.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..api.client import MARKS_REGION_FROM, NomadMania, YearOnly
from ..api.errors import (
    AccountMismatch,
    DriftError,
    UnknownWriteOutcome,
    VerificationFailed,
)
from .model import Op, Plan, basis_of, fingerprint, regions_touched


@dataclass
class Journal:
    """Append-only record of what was actually sent and what came back.

    Written before the request and completed after it, so a crash between the
    two is visible as an unfinished entry rather than as a silent duplicate on
    the next run.
    """

    path: Path

    def _append(self, entry: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()

    def opening(self, op: Op, snapshot: object = None) -> None:
        """Record the intent BEFORE the request goes out.

        A crash between "server accepted" and "we wrote it down" leaves no trace
        if the journal is only written afterwards, and the retry then duplicates
        a write that already landed. An unmatched ``open`` entry is the visible
        form of "we do not know whether this happened", which is the honest
        state and the one a human can act on.

        ``snapshot`` is whatever the caller wants preserved *in full* before the
        write — for a deletion, the record as it was, so it can be put back.
        It goes in the open entry, not the done one, because the done entry is
        the one that may never get written.
        """
        entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "phase": "open", "key": op.key, "kind": op.kind, "label": op.label}
        if snapshot is not None:
            entry["snapshot"] = snapshot
        self._append(entry)

    def note(self, op: Op, result: object, error: str = "") -> None:
        self._append({"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "phase": "done", "key": op.key, "kind": op.kind, "label": op.label,
                      "result": str(result)[:400], "error": error})

    def done_keys(self) -> set[str]:
        """Keys already applied, across every previous run of this plan.

        Reading only the current run's file made this useless: a resumed apply
        re-sent everything. The journal is therefore keyed to the plan, not to
        the moment of running it.
        """
        if not self.path.exists():
            return set()
        keys, opened = set(), set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("phase") == "open":
                opened.add(e["key"])
            elif not e.get("error"):
                keys.add(e["key"])
        return keys

    def unresolved(self) -> set[str]:
        """Ops that were started and never finished — a human decision, not a retry.

        Read in order, not as two sets: a key that went ``open → done → open``
        is unresolved, because the *latest* attempt is the one with no answer.
        Computing ``opened - closed`` over the whole file said the opposite,
        which mattered once a key could be reused — a deletion refused on the
        first try and lost on the second looked fine.
        """
        if not self.path.exists():
            return set()
        pending: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("phase") == "open":
                pending.add(e.get("key"))
            else:
                pending.discard(e.get("key"))
        return pending


@dataclass
class ApplyReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    # Filled by `verify()` after the writes: what the server actually says now.
    verified: dict = field(default_factory=dict)


def apply_plan(
    client: NomadMania,
    plan: Plan,
    *,
    workdir: Path,
    confirm: bool = False,
    max_writes: int = 200,
    on_progress: Callable[[int, int, Op, bool], None] | None = None,
) -> ApplyReport:
    """Run the apply-bucket ops of a plan, after the preconditions pass.

    Preconditions, in order, each of which aborts rather than warns:

    1. ``confirm`` must be true. Without it this prints and exits — the default
       is always the safe one.
    2. The logged-in account must match the plan's. This is the guard against
       writing somebody's history onto the wrong profile.
    3. A snapshot must be taken successfully. If state cannot be captured, it
       cannot be compared afterwards, so the run does not start.
    4. Live state must match the fingerprint taken at plan time. A profile that
       moved needs a new plan, not an optimistic write.
    5. The op count must fit under ``max_writes``. A ceiling means a logic bug
       cannot rewrite an entire profile in one go.
    """
    report = ApplyReport()
    ops = plan.to_apply()

    # Structural validation first: it needs no network, no auth and no snapshot,
    # so a malformed plan fails before anything touches the profile. Two ops with
    # the same key would both run — `already` is read once, so the first success
    # cannot suppress the second — and a plan containing the same write twice is
    # a plan whose author is confused.
    seen: dict[str, Op] = {}
    for op in ops:
        if op.key in seen:
            raise ValueError(
                f"plan contains the same write twice: {op.kind} {op.label!r} and "
                f"{seen[op.key].label!r} share key {op.key}. Remove one and re-plan."
            )
        seen[op.key] = op

    if not confirm:
        report.skipped = len(ops)
        return report

    live_account = client.account_id()
    if live_account != plan.account:
        raise AccountMismatch(
            f"plan was built for account {plan.account}, logged in as {live_account}"
        )

    workdir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # `regions_touched` knows which kinds carry a NomadMania region id and which
    # carry something else: a mark_kye op's id is a 10x10 quadrant, a different
    # namespace that merely overlaps numerically. Snapshotting one as a region
    # fetches an unrelated region's visits and fails drift for the wrong reason.
    regions = sorted(regions_touched(ops))
    snapshot = client.snapshot(regions or None)
    (workdir / f"snapshot-{stamp}.json").write_text(
        json.dumps(snapshot, indent=1, default=str), encoding="utf-8"
    )

    expected = plan.basis.get("fingerprint")
    if not expected:
        # Failing open here made the check decorative: any plan without a basis
        # skipped drift entirely, which is precisely the plan least likely to
        # have been built carefully.
        raise DriftError(
            "plan has no basis fingerprint, so drift cannot be checked — re-plan. "
            "A plan that cannot say what it assumed is not safe to apply."
        )
    current = fingerprint(basis_of(snapshot, regions))
    if current != expected:
        raise DriftError(
            "live state changed since this plan was made — re-plan rather than apply it. "
            "This now covers the dates and qualities of the visits the plan touches, "
            "not only which regions are marked visited."
        )

    if len(ops) > max_writes:
        raise ValueError(
            f"plan has {len(ops)} writes, ceiling is {max_writes}; "
            "raise --max-writes deliberately if that is really intended"
        )

    # Keyed to the plan, not to the run, so a resumed apply can see what the
    # interrupted one already did.
    journal = Journal(workdir / f"journal-{plan.account}-{fingerprint(plan.basis)[:12]}.ndjson")
    already = journal.done_keys()
    stuck = journal.unresolved()
    if stuck:
        raise DriftError(
            f"{len(stuck)} op(s) in {journal.path.name} were started and never confirmed. "
            "Whether they reached the server is unknown, so retrying could duplicate them. "
            "Check those keys against the profile by hand before running this again."
        )

    # What the server answered, per op, for this run only. verify() needs the
    # trip id a create_trip was given: without it a visit that was already
    # there is indistinguishable from the one the trip was meant to create.
    results: dict[str, Any] = {}
    for i, op in enumerate(ops, 1):
        if op.key in already:
            report.skipped += 1
            continue
        report.attempted += 1
        ok = False
        journal.opening(op)
        try:
            result = _execute(client, op)
            ok = True
            results[op.key] = result
            journal.note(op, result)
            already.add(op.key)
            report.succeeded += 1
        except UnknownWriteOutcome as exc:
            # Deliberately NOT journalled as done. The entry stays open, which is
            # what blocks the next run from retrying a write that may have landed.
            report.failed += 1
            report.errors.append(f"{op.kind} {op.label}: {exc}")
            if on_progress:
                on_progress(i, len(ops), op, False)
            raise
        except Exception as exc:
            journal.note(op, None, error=str(exc))
            report.failed += 1
            report.errors.append(f"{op.kind} {op.label}: {exc}")
            # Stop on the first definite failure. Continuing runs later ops
            # against a profile that is no longer in the state the plan assumed,
            # and the ops after a failure are the least reviewed of the batch.
            if on_progress:
                on_progress(i, len(ops), op, False)
            raise
        if on_progress:
            on_progress(i, len(ops), op, ok)

    report.verified = verify(client, ops, results)
    (workdir / f"verify-{stamp}.json").write_text(
        json.dumps(report.verified, indent=1, default=str), encoding="utf-8"
    )
    return report


def verify(
    client: NomadMania, ops: Sequence[Op], results: dict[str, Any] | None = None
) -> dict:
    """Re-read what the plan touched and compare it with what the plan intended.

    A response saying ``OK`` is not evidence that the profile changed the way you
    meant. Both historical incidents here — a downgraded quality and fifty
    duplicate visits — produced nothing but ``OK`` at the time, and were found by
    reading the server back afterwards. Every apply in this session was verified
    by hand for that reason; doing it by hand is not a safeguard, it is a habit
    that will lapse.

    Returns a report rather than raising: by the time this runs the writes have
    already happened, so the useful thing is an accurate account of what is now
    true, including the parts that came out wrong.

    ``results`` maps ``Op.key`` to what the server answered when the op ran.
    It is how a created trip is told apart from visits that were already
    there; without it that question goes under ``unproven`` rather than being
    answered by resemblance. ``unproven`` is for anything the read-back could
    not establish either way. It is not a pass.
    """
    out: dict[str, Any] = {
        "checked": 0, "mismatches": [], "phantom_trips": [], "not_counted": [], "unproven": [],
    }
    results = results or {}
    marked: list[set[int] | None] = []  # read once, and only if an op needs it

    def counted(region: int, stored: Sequence[int], what: str) -> None:
        """A stored visit either marks its region, or is a transit that does not."""
        if not marked:
            try:
                marked.append(client.visited_region_ids(strict=True))
            except VerificationFailed as exc:
                marked.append(None)
                out["unproven"].append(
                    f"the visited list could not be read back ({exc}), so whether the "
                    "regions written now count is not known"
                )
        if marked[0] is None or region in marked[0]:
            return
        best = max(stored)
        if best < MARKS_REGION_FROM:
            out["not_counted"].append({
                "region": region, "quality": best,
                "why": "a transit-quality visit does not mark the region",
            })
        else:
            out["mismatches"].append(
                f"{what}: the visit is there at quality {best} "
                "but the region is not in the visited list"
            )

    for op in ops:
        if op.kind == "update_visit":
            out["checked"] += 1
            live = next(
                (v for v in client.visits_for_region(op.region) if v.id == op.visit_id), None
            )
            if live is None:
                out["mismatches"].append(f"visit {op.visit_id} is gone from region {op.region}")
                continue
            if _iso_of(live.date_from) != op.date_from or _iso_of(live.date_to) != op.date_to:
                out["mismatches"].append(
                    f"visit {op.visit_id}: wanted {op.date_from}..{op.date_to}, "
                    f"server has {_iso_of(live.date_from)}..{_iso_of(live.date_to)}"
                )
            if op.quality is not None and live.quality < op.quality:
                out["mismatches"].append(
                    f"visit {op.visit_id}: quality {live.quality} "
                    f"is below the intended {op.quality}"
                )

        elif op.kind == "add_visit":
            out["checked"] += 1
            live = [v for v in client.visits_for_region(op.region)
                    if _iso_of(v.date_from) == op.date_from and _iso_of(v.date_to) == op.date_to]
            if not live:
                out["mismatches"].append(
                    f"region {op.region}: no visit found for {op.date_from}..{op.date_to}"
                )
            elif len(live) > 1:
                out["mismatches"].append(
                    f"region {op.region}: {len(live)} visits now match {op.date_from}..{op.date_to}"
                    " — a duplicate, which is the incident this package exists for"
                )
            # add-visit wraps the new visit in a trip nobody asked for. Record it
            # so the debris is reconcilable rather than merely known about.
            for v in live:
                if v.trip_id:
                    out["phantom_trips"].append({"region": op.region, "trip_id": v.trip_id})
            # The stored quality is what counts, not the planned one: a visit
            # that came back as 0 is "no visit" wearing the plan's dates.
            if op.quality is not None and any(v.quality != op.quality for v in live):
                out["mismatches"].append(
                    f"region {op.region}: stored quality {[v.quality for v in live]}, "
                    f"planned {op.quality}"
                )
            # The visit being there does not mean the region counts. A transit
            # is stored and returned and leaves the region unmarked, so a plan
            # that "adds two regions" can honestly move the total by one.
            if live:
                counted(op.region, [v.quality for v in live], f"region {op.region}")

        elif op.kind == "create_trip":
            # One trip, and each region in it carrying exactly one visit. With
            # the trip id the server answered, the visit is found by ownership,
            # so one that was already there cannot stand in for it. Without the
            # id it can only be found by its dates, and that is said out loud.
            out["checked"] += 1
            trip = f"trip {op.date_from}..{op.date_to}"
            trip_id = _trip_id(results.get(op.key))
            owners: set[int | None] = set()
            for r in op.regions:
                region = int(r["id"])
                visits = client.visits_for_region(region)
                span = _region_span(r)

                def dates(v) -> tuple[str | None, str | None]:
                    return _iso_of(v.date_from), _iso_of(v.date_to)

                if trip_id is not None:
                    mine = [v for v in visits if v.trip_id == trip_id]
                    found_by = f"owned by trip {trip_id}"
                else:
                    want = span or (op.date_from, op.date_to)
                    mine = [v for v in visits if dates(v) == want]
                    found_by = f"for {want[0]}..{want[1]}"
                if not mine:
                    out["mismatches"].append(f"{trip}: region {region} has no visit {found_by}")
                    continue
                if span and any(dates(v) != span for v in mine):
                    out["mismatches"].append(
                        f"{trip}: region {region} stored {[dates(v) for v in mine]}, "
                        f"planned {span[0]}..{span[1]}"
                    )
                same = [v for v in visits if dates(v) == dates(mine[0])]
                if len(same) > 1 or len(mine) > 1:
                    out["mismatches"].append(
                        f"{trip}: region {region} now has {max(len(same), len(mine))} visits "
                        f"for {dates(mine[0])[0]}..{dates(mine[0])[1]} — a duplicate"
                    )
                want_quality = r.get("quality")
                if want_quality is not None and any(v.quality != want_quality for v in mine):
                    out["mismatches"].append(
                        f"{trip}: region {region} quality is "
                        f"{[v.quality for v in mine]}, planned {want_quality}"
                    )
                owners |= {v.trip_id for v in mine}
                counted(region, [v.quality for v in mine], f"{trip}: region {region}")
            if trip_id is None:
                if len(owners) > 1 or None in owners:
                    out["mismatches"].append(
                        f"{trip}: its visits are not all owned by one trip "
                        f"(owners: {sorted(owners, key=str)})"
                    )
                out["unproven"].append(
                    f"{trip}: the id the server gave it is not known to this run, so the "
                    "visits found were matched by date and could predate it"
                )

        elif op.kind == "mark_kye":
            out["checked"] += 1
            if op.item not in set(client.kye().get("visited", [])):
                out["mismatches"].append(f"KYE quadrant {op.item} did not stick")

        elif op.kind == "mark_dare":
            out["checked"] += 1
            if op.region not in client.visited_dare_ids():
                out["mismatches"].append(f"DARE area {op.region} did not stick")

        elif op.kind == "tick_series":
            out["checked"] += 1
            if op.item not in set(client.series(op.series).get("visited", [])):
                out["mismatches"].append(f"series {op.series} item {op.item} did not stick")

    return out


def _iso_of(day) -> str | None:
    return day.isoformat() if day else None


def _trip_id(result: object) -> int | None:
    try:
        return int(result["trip_id"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None


def _region_span(r: dict) -> tuple[str, str] | None:
    """The dates one trip region was planned with, as ``_iso_of`` would print them.

    ``None`` when the plan gave the region no dates of its own. A year with no
    month or day is a :class:`YearOnly` on the profile, not 1 January, and the
    wire carries numbers as strings as often as not.
    """
    def side(which: str) -> str | None:
        year, month, day = (r.get(f"{part}_{which}") for part in ("year", "month", "day"))
        try:
            if not year:
                return None
            if not month or not day:
                return YearOnly(int(year)).isoformat()
            return dt.date(int(year), int(month), int(day)).isoformat()
        except (TypeError, ValueError):
            return None

    start, end = side("from"), side("to")
    return (start, end) if start and end else None


def _when(text: str | None):
    """A plan's date string, as either a real date or a bare year.

    ``"2013"`` is not a shorthand for 1 January 2013. It is the profile saying
    "some time in 2013", which the API stores natively and which YES reads
    exactly as well as a precise date. Expanding it to a January the traveller
    never claimed would invent evidence, so it stays a :class:`YearOnly`.
    """
    if not text:
        return None
    if len(text) == 4 and text.isdigit():
        return YearOnly(int(text))
    return dt.date.fromisoformat(text)


def _execute(client: NomadMania, op: Op):
    d_from = _when(op.date_from)
    d_to = _when(op.date_to)

    if op.kind in ("add_visit", "update_visit"):
        # `op.quality or 3` was the bug here, and it is the documented incident
        # verbatim: None becomes 3, and so does an explicit 0, so a plan that
        # simply forgot the field would post a downgrade that looks deliberate.
        # An absent quality is a broken plan, not a default.
        if op.quality is None:
            raise ValueError(
                f"{op.kind} op {op.key} has no quality; update-visit replaces the "
                "whole record, so there is no safe value to assume"
            )

    if op.kind == "add_visit":
        return client.add_visit(op.region, d_from, d_to, op.quality)

    if op.kind == "update_visit":
        # The client also takes max() against the live record, so this cannot
        # downgrade even if the plan was built from a stale read.
        return client.update_visit(
            op.visit_id, op.region, d_from, d_to,
            quality=op.quality, allow_vaguer=op.allow_vaguer,
        )

    if op.kind == "create_trip":
        return client.create_trip(d_from, d_to, op.regions)

    if op.kind == "mark_dare":
        return client.mark_dare(op.region)

    if op.kind == "mark_kye":
        # `item` carries the quadrant id, as it does for series. It is NOT
        # `region`: a qid and an NM region id are different namespaces that
        # overlap numerically, and conflating them is silent and expensive.
        return client.mark_kye(op.item)

    if op.kind == "tick_series":
        if not client.tick_series_item(op.series, op.item):
            raise RuntimeError("series toggle did not answer OK")
        return "OK"

    raise ValueError(f"unknown op kind: {op.kind}")
