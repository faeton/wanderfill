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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..api.client import NomadMania, YearOnly
from ..api.errors import AccountMismatch, DriftError
from .model import Op, Plan, basis_of, fingerprint


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

    def opening(self, op: Op) -> None:
        """Record the intent BEFORE the request goes out.

        A crash between "server accepted" and "we wrote it down" leaves no trace
        if the journal is only written afterwards, and the retry then duplicates
        a write that already landed. An unmatched ``open`` entry is the visible
        form of "we do not know whether this happened", which is the honest
        state and the one a human can act on.
        """
        self._append({"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "phase": "open", "key": op.key, "kind": op.kind, "label": op.label})

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
        """Ops that were started and never finished — a human decision, not a retry."""
        opened, closed = set(), set()
        if not self.path.exists():
            return set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            (opened if e.get("phase") == "open" else closed).add(e.get("key"))
        return opened - closed


@dataclass
class ApplyReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


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
    # Only kinds that actually touch visit records contribute to the snapshot.
    # `region` on a mark_kye op is a 10x10 quadrant id, not an NM region id, and
    # snapshotting it would fetch some unrelated region's visits and then fail
    # the drift check for a reason that has nothing to do with the plan.
    VISIT_KINDS = {"add_visit", "update_visit", "create_trip"}
    regions = sorted({o.region for o in ops if o.region and o.kind in VISIT_KINDS})
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
            journal.note(op, result)
            report.succeeded += 1
        except Exception as exc:
            journal.note(op, None, error=str(exc))
            report.failed += 1
            report.errors.append(f"{op.kind} {op.label}: {exc}")
        if on_progress:
            on_progress(i, len(ops), op, ok)

    return report


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
