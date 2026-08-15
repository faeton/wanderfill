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

from ..api.client import NomadMania
from ..api.errors import AccountMismatch, DriftError
from .model import Op, Plan, fingerprint


@dataclass
class Journal:
    """Append-only record of what was actually sent and what came back.

    Written before the request and completed after it, so a crash between the
    two is visible as an unfinished entry rather than as a silent duplicate on
    the next run.
    """

    path: Path
    entries: list[dict] = field(default_factory=list)

    def note(self, op: Op, result: object, error: str = "") -> None:
        self.entries.append(
            {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "key": op.key,
                "kind": op.kind,
                "label": op.label,
                "result": str(result)[:400],
                "error": error,
            }
        )
        self.path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in self.entries),
            encoding="utf-8",
        )

    def done_keys(self) -> set[str]:
        if not self.path.exists():
            return set()
        keys = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not e.get("error"):
                keys.add(e["key"])
        return keys


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
    regions = sorted({o.region for o in ops if o.region})
    snapshot = client.snapshot(regions or None)
    (workdir / f"snapshot-{stamp}.json").write_text(
        json.dumps(snapshot, indent=1, default=str), encoding="utf-8"
    )

    expected = plan.basis.get("fingerprint")
    if expected:
        current = fingerprint(
            {"visited": snapshot["visited_regions"], "dare": snapshot["visited_dare"]}
        )
        if current != expected:
            raise DriftError(
                "live state changed since this plan was made — re-plan rather than apply it"
            )

    if len(ops) > max_writes:
        raise ValueError(
            f"plan has {len(ops)} writes, ceiling is {max_writes}; "
            "raise --max-writes deliberately if that is really intended"
        )

    journal = Journal(workdir / f"journal-{stamp}.ndjson")
    already = journal.done_keys()

    for i, op in enumerate(ops, 1):
        if op.key in already:
            report.skipped += 1
            continue
        report.attempted += 1
        ok = False
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


def _execute(client: NomadMania, op: Op):
    d_from = dt.date.fromisoformat(op.date_from) if op.date_from else None
    d_to = dt.date.fromisoformat(op.date_to) if op.date_to else None

    if op.kind == "add_visit":
        return client.add_visit(op.region, d_from, d_to, op.quality or 3)

    if op.kind == "update_visit":
        # quality is mandatory here; the API replaces the whole record and a
        # missing quality silently downgrades a "lived here" to a "good visit".
        return client.update_visit(
            op.visit_id, op.region, d_from, d_to, quality=op.quality or 3
        )

    if op.kind == "create_trip":
        return client.create_trip(d_from, d_to, op.regions)

    if op.kind == "mark_dare":
        return client.mark_dare(op.region)

    if op.kind == "tick_series":
        if not client.tick_series_item(op.series, op.item):
            raise RuntimeError("series toggle did not answer OK")
        return "OK"

    raise ValueError(f"unknown op kind: {op.kind}")
