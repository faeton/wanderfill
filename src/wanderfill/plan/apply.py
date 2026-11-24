"""Executing a plan, carefully.

Everything here exists because of something that actually went wrong the first
time this was done by hand. The preconditions are not ceremony.

There is no delete code path in this module. Not behind a flag, not behind a
confirmation — absent. Cleaning up records the tool did not create is a
conversation with a human, not a feature.
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..api.client import MARKS_REGION_FROM, NomadMania, Visit, YearOnly, strict_ids
from ..api.errors import (
    AccountMismatch,
    ApiError,
    AuthError,
    DriftError,
    JournalCorrupt,
    PlanInvalid,
    PrecisionLoss,
    TransportError,
    UnknownWriteOutcome,
    VerificationFailed,
    WanderfillError,
)
from .model import (
    TRIP_REGION_FIELDS,
    Op,
    Plan,
    basis_of,
    fingerprint,
    parse_when,
    regions_touched,
    validate,
)

# The Op fields that say why, not what: they stay out of `Op.key` and the journal's payload.
PROVENANCE = frozenset({"label", "confidence", "method", "evidence", "proof"})

# What to do about an entry that stays open. One text, wherever one is met.
CLOSE_BY_HAND = (
    'Read the server back. Then the account owner appends a line to that journal for each key, '
    '{"phase": "done", "key": "<the key>", "error": ""}, with "error" set if it did not land.'
)


_CANNOT_SYNC_DIR = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}


def _sync_dir(path: Path) -> None:
    """Put a directory's entries on the disk, where the system can.

    Windows has no way to open a directory and some network filesystems cannot
    sync one; there it does nothing, and the file's own lines are synced anyway.
    Any other error, one on opening included, is a :class:`DriftError`.
    """
    if not hasattr(os, "O_DIRECTORY"):
        return
    cannot: set[int] = set()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        cannot = _CANNOT_SYNC_DIR
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno not in cannot:
            raise DriftError(
                f"{path} could not be put on the disk ({exc.strerror or exc}), so a journal below "
                "it might not be there afterwards. Nothing was sent, and no entry was opened."
            ) from None


def make_workdir(path: Path) -> None:
    """Create the work directory, and put every directory it made on the disk.

    Nothing is removed if that fails: another run may have made the same one.
    The next run finds it there, and the first line of a new journal syncs the
    work directory and its parent whoever made them. A deeper ancestor that a
    failed run left unsynced is not synced again.
    """
    try:
        made = [d for d in (path, *path.parents) if not d.exists()]
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DriftError(
            f"{path} cannot be used as the work directory ({exc.strerror or exc}); "
            "nothing was sent."
        ) from None
    for d in made:
        _sync_dir(d.parent)


@dataclass
class Journal:
    """Append-only record of what was actually sent and what came back.

    Written before the request and completed after it, so a crash between the
    two is visible as an unfinished entry rather than as a silent duplicate on
    the next run.
    """

    path: Path

    def _append(self, entry: dict) -> None:
        """One line, on the disk before this returns. A new file's name is, before the line.

        So if a directory cannot be synced there is no line: an empty file says
        nothing is open, and the next run syncs both again. The parent is one of
        them because an earlier run may have made the directory and stopped there.

        A lone surrogate in a server's answer has no UTF-8 form; as an escape
        it is the same JSON, and the ``done`` line of a write that landed is
        not lost to it.
        """
        text = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        line = text.encode("utf-8", "backslashreplace")
        with self.path.open("a+b") as fh:
            if not fh.tell():
                where = self.path.parent.resolve()  # "." and a link have another parent
                _sync_dir(where)
                _sync_dir(where.parent)
            else:
                # Entries are closed by hand, and an editor may leave out the
                # last newline. Appending to that line would corrupt both.
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = b"\n" + line
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _entries(self) -> list[dict]:
        """Every line, or :class:`JournalCorrupt`: a skipped line is a hidden write.

        Split on the newline byte only. ``str.splitlines`` also cuts at U+2028
        and its kin, which JSON leaves unescaped inside a value, and a line torn
        in the middle of a character is not valid text at all.
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise _unusable(self.path, "read", exc) from None
        out = []
        for n, line in enumerate(raw.split(b"\n"), 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line.decode("utf-8"))
                bad = not (isinstance(e, dict) and isinstance(e.get("key"), str)
                           and e.get("phase") in ("open", "done"))
            except ValueError:  # not JSON, or not UTF-8
                bad = True
            if bad:
                raise JournalCorrupt(
                    f"{self.path}: line {n} is not an 'open' or 'done' entry with a key, so "
                    "whether a write is unfinished is unknown. Read the server back, and "
                    "the account owner repairs that line by hand; nothing is sent for this "
                    "account until then."
                )
            out.append(e)
        return out

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
        the one that may never get written. ``payload`` is there for the same
        reason: the key is a hash, and the plan file may be gone by the time
        somebody has to find out what was sent.
        """
        entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "phase": "open", "key": op.key, "kind": op.kind, "label": op.label}
        if isinstance(op, Op):
            entry["payload"] = {k: v for k, v in asdict(op).items() if k not in PROVENANCE}
        if snapshot is not None:
            entry["snapshot"] = snapshot
        self._append(entry)

    def note(self, op: Op, result: object, error: str = "") -> None:
        self._append({"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "phase": "done", "key": op.key, "kind": op.kind, "label": op.label,
                      "result": result, "error": error})

    def done_keys(self) -> set[str]:
        """Keys already applied, across every previous run of this plan.

        Reading only the current run's file made this useless: a resumed apply
        re-sent everything. The journal is therefore keyed to the plan, not to
        the moment of running it.
        """
        return {e["key"] for e in self._entries()
                if e["phase"] == "done" and not e.get("error")}

    def unresolved(self) -> set[str]:
        """Ops that were started and never finished — a human decision, not a retry.

        Read in order, not as two sets: a key that went ``open → done → open``
        is unresolved, because the *latest* attempt is the one with no answer.
        Computing ``opened - closed`` over the whole file said the opposite,
        which mattered once a key could be reused — a deletion refused on the
        first try and lost on the second looked fine.
        """
        pending: set[str] = set()
        for e in self._entries():
            if e["phase"] == "open":
                pending.add(e["key"])
            else:
                pending.discard(e["key"])
        return pending


def _unusable(path: Path, what: str, exc: OSError) -> DriftError:
    return DriftError(
        f"{path} cannot be {what} ({exc.strerror or exc}), so whether another write of this "
        "account is open or under way is unknown; nothing more is sent."
    )


def _distinct(dirs: Iterable[Path]) -> list[Path]:
    """The directories that exist, each once however it was spelled, in a fixed order."""
    found: dict[tuple[int, int], Path] = {}
    for d in dirs:
        try:
            st = d.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _unusable(d, "read", exc) from None
        found.setdefault((st.st_dev, st.st_ino), d)
    return sorted(found.values(), key=lambda d: d.resolve())


def open_entries(dirs: Iterable[Path], account: int) -> dict[Path, set[str]]:
    """Every write of this account that was started and never confirmed, by journal.

    Reads every apply and delete journal of the account in ``dirs``, not only
    the one the current plan would use: that name comes from the plan's basis,
    so re-planning alone moved an open write out of sight. Journals with no
    account in their name predate the phase field and are not read. A journal
    in a directory not listed here is not seen.
    """
    out = {}
    for d in _distinct(dirs):
        # Not glob(): it answers "nothing" for a directory it may not read.
        try:
            paths = sorted(d.iterdir())
        except OSError as exc:
            raise _unusable(d, "read", exc) from None
        for path in paths:
            if path.name != f"deletes-{account}.ndjson" and not (
                path.name.startswith(f"journal-{account}-") and path.suffix == ".ndjson"
            ):
                continue
            if stuck := Journal(path).unresolved():
                out[path] = stuck
    return out


def describe_open(entries: dict[Path, set[str]]) -> list[str]:
    """One line per journal: which file, and which keys a human has to settle."""
    return [f"{path}: {len(keys)} write(s) started and never confirmed: "
            f"{', '.join(sorted(keys))}" for path, keys in entries.items()]


OLD_KEY_IS_NOT_KNOWN = (
    "That key leaves out the regions' dates, so whether it is this trip is not known. "
    "Read the server back, then rebuild the plan from the live profile, without this trip if it "
    "is already there: a rebuilt plan whose basis records when it was taken has its own journal, "
    "and drift compares with what that plan saw, so neither would stop a second copy, and only "
    "one with the same two full dates is refused."
)


def trips_under_old_keys(journal: Journal, plan: Plan) -> list[str]:
    """One line per trip this plan's journal has done under :attr:`Op.legacy_key` only.

    That key left out the regions' dates, so the trip it stands for is this one
    or one that differs from it. Sending would duplicate the first and skipping
    would drop the second, so neither is done.
    """
    done = journal.done_keys()
    return [
        f"{journal.path}: ops[{i}] {op.kind} {op.label!r}: a trip with these regions was "
        f"written under the older key {op.legacy_key}"
        for i, op in enumerate(plan.ops)
        if op.bucket == "apply" and op.legacy_key in done and op.key not in done
    ]


def _full_days(start: object, end: object) -> tuple[str, str] | None:
    """Both ends as ISO days, or ``None`` if either is absent, vague or unreadable."""
    try:
        days = [parse_when(start), parse_when(end)]
    except ValueError:
        return None
    if not all(isinstance(d, dt.date) for d in days):
        return None
    return days[0].isoformat(), days[1].isoformat()


def _new_visits(op: Op, stored: dict[str, list[dict]]) -> Iterator[tuple[int, tuple[str, str]]]:
    """Each region, and the full dates it would have a visit for because of ``op``.

    An update counts only if it changes the dates of a visit that is there: one
    that keeps them adds nothing, whatever twin the visit may have already.
    """
    if op.kind == "add_visit":
        if op.region is not None and (days := _full_days(op.date_from, op.date_to)):
            yield int(op.region), days
    elif op.kind == "create_trip":
        for r in op.regions:
            span = _region_span(r) if _has_own_dates(r) else (op.date_from, op.date_to)
            if span and (days := _full_days(*span)):
                yield int(r["id"]), days
    elif op.kind == "update_visit" and (days := _full_days(op.date_from, op.date_to)):
        for v in stored.get(str(op.region), []):
            if v.get("id") == op.visit_id and _stored_days(v) != days:
                yield int(op.region), days


def _stored_days(visit: dict) -> tuple[str, str] | None:
    return _full_days(visit.get("date_from"), visit.get("date_to"))


def doubles(snapshot: dict, ops: Sequence[Op]) -> list[str]:
    """One line per region visit in ``ops`` that is on the profile, or in ``ops``, already.

    A plan rebuilt after its trip landed has that trip in its own basis and a
    journal of its own, so neither stops a second copy; ``verify`` would only
    report it afterwards. Pass the ops that would be *sent*: one already done in
    this plan's journal is on the profile because of it.

    Only full dates are compared. Two real visits can share a year, or no date
    at all, so those are not evidence of a copy. A trip region without dates of
    its own takes the trip's header. Whether a trip owns the existing visit does
    not matter: both kinds count. An update that moves a visit onto the dates of
    another makes a copy too. Existing visits are taken as the snapshot has them,
    so dates that another op in the plan moves a visit away from still count.
    """
    out = []
    stored = snapshot.get("visits", {})
    planned: dict[tuple, Op] = {}
    for op in ops:
        for region, days in _new_visits(op, stored):
            there = [v for v in stored.get(str(region), []) if _stored_days(v) == days]
            first = planned.setdefault((region, days), op)
            if there:
                v = there[0]
                owner = f"trip {v['trip_id']}" if v.get("trip_id") else "standalone"
                why = f"is on the profile already (visit {v.get('id')}, {owner})"
            elif first is not op:
                why = f"is in the plan already, from {first.kind} {first.label!r}"
            else:
                continue
            out.append(f"{op.kind} {op.label!r}: region {region} for {days[0]}..{days[1]} {why}; "
                       "sending it would make a second copy")
    return out


def tick_of(op: Op) -> tuple[tuple[str, int], int] | None:
    """Which hand-ticked list ``op`` ticks, as :func:`ticked_before` keys it, and the id."""
    if op.kind == "tick_series":
        return ("series", op.series), op.item
    if op.kind == "mark_dare":
        return ("dare", 0), op.region
    if op.kind in ("mark_kye", "mark_tcc"):
        return (op.kind.removeprefix("mark_"), 0), op.item
    return None


def ticked_before(
    client: NomadMania, ops: Iterable[Op], snapshot: dict | None = None
) -> dict[tuple[str, int], set[int]]:
    """What is ticked now in every list ``ops`` would tick, one strict read per list.

    Keys are ``("series", id)``, ``("kye", 0)``, ``("dare", 0)`` and ``("tcc", 0)``.
    DARE and TCC come from ``snapshot`` where it holds them, since it has just
    read them strictly; TCC alone is 29 requests.
    """
    held = {"dare": "visited_dare", "tcc": "visited_tcc"}
    read = {"series": client.series_ticked, "kye": lambda _: client.kye_ticked(),
            "dare": lambda _: client._dare_strict(), "tcc": lambda _: client.visited_tcc_ids()}
    out: dict[tuple[str, int], set[int]] = {}
    for op in ops:
        tick = tick_of(op)
        if tick is None or tick[0] in out:
            continue
        name, n = key = tick[0]
        have = (snapshot or {}).get(held.get(name))
        out[key] = read[name](n) if have is None else strict_ids(held[name], have)
    return out


def gone(
    snapshot: dict, ticked: dict[tuple[str, int], set[int]], done: Iterable[Op], journal: Path
) -> list[str]:
    """One line per op the journal has as done whose result is not on the profile now.

    Skipping it as done would end a run "finished" without it. Somebody took it
    back since, or it never was there; whether it is wanted again is theirs to
    say, in a new plan. Judged: ticks, and the visits :func:`_new_visits` has
    full dates for. An ``update_visit`` leaves nothing of its own to look for,
    and an undated or year-only visit cannot be told from another one.
    """
    stored = snapshot.get("visits", {})
    out = []
    for op in done:
        missing = []
        if tick := tick_of(op):
            if tick[1] not in ticked[tick[0]]:
                missing.append(f"{_tick_name(op)} is not ticked")
        elif op.kind != "update_visit":
            missing = [f"region {region} has no visit for {days[0]}..{days[1]}"
                       for region, days in _new_visits(op, stored)
                       if not any(_stored_days(v) == days for v in stored.get(str(region), []))]
        if missing:
            out.append(f"{journal}: {op.kind} {op.label!r} (key {op.key}) landed earlier, as this "
                       f"journal has it done, and is gone now: {'; '.join(missing)}")
    return out


GONE_IS_NOT_DONE = (
    "If it is wanted again, build a new plan with the time it was taken in its basis, so that "
    "it has a journal of its own."
)


@contextmanager
def account_lock(dirs: Iterable[Path], account: int) -> Iterator[None]:
    """Hold the right to write to ``account``, or raise :class:`DriftError` at once.

    Two runs that both read the journals before either has written to them both
    find nothing open. The lock dies with the process, so a crash never leaves
    one behind; the ``.lock-*`` file itself stays, because removing it would let
    a third run lock a different file of the same name. Directories that do not
    exist are not created for this, so two runs whose ``--workdir`` differ meet
    only where they share an existing directory.
    """
    try:
        import fcntl
    except ImportError:
        fcntl = None
        print("warning: no file locking on this system; make sure nothing else is "
              "writing to this account", file=sys.stderr)
    held = []
    try:
        for d in _distinct(dirs) if fcntl else ():
            path = d / f".lock-{account}"
            try:
                held.append(path.open("ab"))
                fcntl.flock(held[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DriftError(
                    f"another run is writing to account {account} (it holds {path}). "
                    "Wait for it to finish; nothing was sent."
                ) from None
            except OSError as exc:
                raise _unusable(path, "locked", exc) from None
        yield
    finally:
        for fh in held:
            fh.close()


@dataclass
class ApplyReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    # Labels of the ops skipped because the item was ticked already. Nothing was
    # sent or journalled for them; they are in `skipped` too.
    already: list[str] = field(default_factory=list)
    # What the server actually says now: `verify()`'s report, plus "partial" and
    # "uncertain" as in the verify file.
    verified: dict = field(default_factory=dict)


def _proves_nothing_changed(exc: BaseException) -> bool:
    """Whether a failed op may be journalled as closed, and so be sent again.

    Only a refusal counts: ours before sending, or the server's own. An answer
    we could not read, a read-back that failed, a bug in our own parsing after
    the request went out — all may sit on top of a write that landed. Named as
    a list of what closes, not of what stays open, so an exception nobody
    thought of leaves the entry open.
    """
    if isinstance(exc, VerificationFailed):
        return False
    return isinstance(exc, (PrecisionLoss, AuthError, TransportError, ApiError))


def apply_plan(
    client: NomadMania,
    plan: Plan,
    *,
    workdir: Path,
    confirm: bool = False,
    max_writes: int = 200,
    on_progress: Callable[[int, int, Op, bool], None] | None = None,
    also: Sequence[Path] = (),
    on_read_back: Callable[[str], None] | None = None,
    on_skip: Callable[[int, int, Op], None] | None = None,
) -> ApplyReport:
    """Run the apply-bucket ops of a plan, after the preconditions pass.

    ``also`` is where else this account's journals may be. The lock and the scan
    for open writes cover ``workdir`` and these, and no other directory.
    ``on_read_back`` is given one line to show before the read-back starts.
    ``on_skip`` is called in place of ``on_progress`` for an op whose item is
    ticked already.

    Preconditions, in order, each of which aborts rather than warns:

    1. The whole plan must validate (:func:`~wanderfill.plan.model.validate`): its
       schema, and every apply-bucket op's ids, quality, dates and duplicates.
       It needs no network, so it comes first and a dry run reports it too; a
       malformed op used to be found only when reached, after earlier ones had
       been written.
    2. ``confirm`` must be true. Without it this prints and exits — the default
       is always the safe one.
    3. The logged-in account must match the plan's. This is the guard against
       writing somebody's history onto the wrong profile.
    4. No other run may hold the account's lock, from here to the read-back.
    5. No write may be open in any of the account's journals, this plan's or not.
    6. A snapshot must be taken successfully, and strictly: a reply without its
       list is not an empty profile. If state cannot be captured, it cannot be
       compared afterwards, so the run does not start.
    7. Live state must match the fingerprint taken at plan time. A profile that
       moved needs a new plan, not an optimistic write.
    8. The op count must fit under ``max_writes``. A ceiling means a logic bug
       cannot rewrite an entire profile in one go.
    9. No trip may be done in this plan's journal under the key earlier versions
       gave it and not under its own.
    10. No visit the plan would add, or move to other dates, may be on the profile,
        or in the plan, already with the same region and full dates
        (:func:`doubles`).
    11. Every hand-ticked list the plan ticks must be readable
        (:func:`ticked_before`). A series tick is a toggle and a repeat may undo
        it, so an item in its list already is skipped: nothing is sent or
        journalled for it, its label goes in ``ApplyReport.already``, and
        ``on_skip`` is told, or ``on_progress`` with ``True``. The series and KYE
        lists go into the snapshot file, which is where to look to undo the ticks.
    12. Nothing this plan's journal has as done may be gone from the profile
        (:func:`gone`): a tick, or a visit added with full dates.

    Whatever ends the loop, what landed is read back (:func:`verify`) and
    ``verify-<stamp>.json`` written. After an exception or Ctrl-C the file says
    ``"partial": true``, covers the ops that are done or ticked already, and
    has under ``"uncertain"`` what the server shows for the op whose journal entry
    is still open, or null if none is. That is for the human who closes the
    entry; nothing here does. The exception is then raised as it was, with the
    :class:`ApplyReport` as its ``report``. A read-back that fails is recorded
    in the report and raises nothing. Ctrl-C during it ends the reads: what was
    not checked is listed as ``NOT READ BACK``, the file is written with
    ``"partial": true``, and then the error that ended the run is raised, or the
    interrupt itself if the run had finished.
    """
    report = ApplyReport()
    ops = plan.to_apply()

    if problems := validate(plan):
        raise PlanInvalid(
            f"plan is not valid, so nothing was sent ({len(problems)} problem(s)):\n  "
            + "\n  ".join(problems)
        )

    if not confirm:
        report.skipped = len(ops)
        return report

    live_account = client.account_id()
    if live_account != plan.account:
        raise AccountMismatch(
            f"plan was built for account {plan.account}, logged in as {live_account}"
        )

    make_workdir(workdir)
    dirs = (workdir, *also)
    with account_lock(dirs, plan.account):
        if stuck := open_entries(dirs, plan.account):
            raise DriftError(
                "; ".join(describe_open(stuck)) + ". Whether they reached the server is "
                "unknown, so sending anything more could duplicate them. " + CLOSE_BY_HAND
            )
        return _apply_locked(client, plan, ops, report, workdir, max_writes, on_progress,
                             on_read_back, on_skip)


def _apply_locked(
    client: NomadMania,
    plan: Plan,
    ops: list[Op],
    report: ApplyReport,
    workdir: Path,
    max_writes: int,
    on_progress: Callable[[int, int, Op, bool], None] | None,
    on_read_back: Callable[[str], None] | None,
    on_skip: Callable[[int, int, Op], None] | None,
) -> ApplyReport:
    """The part of :func:`apply_plan` that needs the lock, from snapshot to read-back."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # `regions_touched` knows which kinds carry a NomadMania region id and which
    # carry something else: a mark_kye op's id is a 10x10 quadrant, a different
    # namespace that merely overlaps numerically. Snapshotting one as a region
    # fetches an unrelated region's visits and fails drift for the wrong reason.
    regions = sorted(regions_touched(ops))
    wants_tcc = any(o.kind == "mark_tcc" for o in ops)
    snapshot = client.snapshot(regions, tcc=wants_tcc)

    def keep_snapshot() -> None:
        (workdir / f"snapshot-{stamp}.json").write_text(
            json.dumps(snapshot, indent=1, default=str), encoding="utf-8"
        )

    keep_snapshot()

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
    if old := trips_under_old_keys(journal, plan):
        raise DriftError("; ".join(old) + f". {OLD_KEY_IS_NOT_KNOWN} Nothing was sent.")
    done = journal.done_keys()
    to_send = [op for op in ops if op.key not in done]
    if twice := doubles(snapshot, to_send):
        raise DriftError("; ".join(twice) + ". Nothing was sent.")

    # Also of the ops that are done: a tick somebody took back is not done any more.
    ticked = ticked_before(client, ops, snapshot)
    if lost := gone(snapshot, ticked, [op for op in ops if op.key in done], journal.path):
        raise DriftError("; ".join(lost) + f". {GONE_IS_NOT_DONE} Nothing was sent.")
    lists: dict[str, Any] = {}
    for (name, n), ids in ticked.items():
        if name == "series":
            lists.setdefault("series", {})[str(n)] = sorted(ids)
        elif name == "kye":  # DARE and TCC are in the snapshot already
            lists["kye"] = sorted(ids)
    if lists:
        snapshot["ticked"] = lists  # beside what `basis_of` reads, not part of it
        keep_snapshot()

    # What the server answered, per op, for this run only. verify() needs the
    # trip id a create_trip was given: without it a visit that was already
    # there is indistinguishable from the one the trip was meant to create.
    results: dict[str, Any] = {}

    def landed(op: Op) -> bool:
        tick = tick_of(op)
        return op.key in done or (tick is not None and tick[1] in ticked.get(tick[0], ()))

    # Every Ctrl-C of the read-back: none may hide the error that ended the run.
    interrupts: list[KeyboardInterrupt] = []

    def insist(step: Callable[[], Any]) -> Any:
        """A short local step; an interrupt in it is kept and the step is done again."""
        try:
            return step()
        except KeyboardInterrupt as exc:
            interrupts.append(exc)
            return step()

    def read_back(which: Sequence[Op], *, partial: bool, uncertain: Op | None = None) -> None:
        """Fill ``report.verified`` and write the file, whatever fails.

        The first interrupt ends the reads. What was not checked by then is
        ``NOT READ BACK``, and the file says so.
        """
        reads = _Reads(client)
        seen = reads.out
        cut = "interrupted" if interrupts else ""
        if not cut:
            try:
                if on_read_back and (which or uncertain):
                    with suppress(Exception):  # only a message
                        on_read_back(_reading(which, uncertain, results))
                seen = verify(client, which, results, reads)
            except KeyboardInterrupt as exc:
                interrupts.append(exc)
                cut = "interrupted"
            except Exception as exc:
                cut = repr(exc)
        if cut:
            seen["unproven"].append(f"the read-back was cut short ({cut})")
            seen["unproven"] += [f"NOT READ BACK: {o.kind} {o.label!r} (key {o.key})"
                                 for o in which[seen["checked"]:]]
        waiting = None
        if uncertain:
            now = ["NOT READ BACK (interrupted); whether it landed is not known"]
            try:
                if not interrupts:
                    now = _what_is_there_now(reads, uncertain)
            except KeyboardInterrupt as exc:
                interrupts.append(exc)
            except Exception as exc:
                now = [f"could not be read back ({exc!r}); whether it landed is not known"]
            waiting = {
                "key": uncertain.key, "kind": uncertain.kind, "label": uncertain.label,
                "journal": str(journal.path), "answer": results.get(uncertain.key), "now": now,
            }
        seen |= {"partial": partial or bool(interrupts), "uncertain": waiting}
        report.verified = seen
        path = workdir / f"verify-{stamp}.json"
        try:
            text = json.dumps(seen, indent=1, default=str)
            insist(lambda: path.write_text(text, encoding="utf-8"))
        except (Exception, KeyboardInterrupt) as exc:
            report.errors.append(f"{path} could not be written ({exc!r})")

    def read_back_partial(op: Op) -> None:
        # What is done and what is open comes from the journal: an interrupt
        # can fall between its line and this run's bookkeeping.
        left_open = {op.key} - done
        try:
            finished, left_open = insist(lambda: (journal.done_keys(), journal.unresolved()))
            done.update(finished)
        except (Exception, KeyboardInterrupt) as exc:
            report.errors.append(f"journal: {str(exc) or repr(exc)}")
        read_back(
            [o for o in ops if landed(o)],
            partial=True,
            uncertain=next((o for o in ops if o.key in left_open), None),
        )

    try:
        for i, op in enumerate(ops, 1):
            if op.key in done:
                report.skipped += 1
                continue
            tick = tick_of(op)
            if tick and tick[1] in ticked[tick[0]]:
                report.skipped += 1
                report.already.append(op.label or f"{op.kind} {tick[1]}")
                if on_skip:
                    on_skip(i, len(ops), op)
                    continue
            else:
                _send(client, op, i, len(ops), journal, report, results, on_progress)
                done.add(op.key)
                if tick:  # a plan can spell one tick two ways, under two keys
                    ticked[tick[0]].add(tick[1])
            if on_progress:
                on_progress(i, len(ops), op, True)
    except (Exception, KeyboardInterrupt) as exc:
        exc.report = report  # type: ignore[union-attr]
        with suppress(KeyboardInterrupt):  # one that falls between the steps above
            read_back_partial(op)
        raise
    try:
        read_back(ops, partial=False)
    except KeyboardInterrupt as exc:
        interrupts.append(exc)
    if interrupts:
        interrupts[0].report = report  # type: ignore[attr-defined]
        raise interrupts[0]
    return report


def _reading(which: Sequence[Op], uncertain: Op | None, results: dict[str, Any]) -> str:
    """One line for the person who is about to wait for it."""
    ops = [*which, uncertain] if uncertain else which
    lists = {tick[0] for o in ops if (tick := tick_of(o))}
    trips = sum(_trip_id(results.get(o.key)) is not None for o in which if o.kind == "create_trip")
    return (f"Reading back {len(ops)} op(s): the visits of {len(regions_touched(ops))} region(s), "
            f"{len(lists)} list(s), {trips} trip(s). Ctrl-C stops the reading.")


def _send(
    client: NomadMania,
    op: Op,
    i: int,
    total: int,
    journal: Journal,
    report: ApplyReport,
    results: dict[str, Any],
    on_progress: Callable[[int, int, Op, bool], None] | None,
) -> None:
    """One op, between its ``open`` and ``done`` lines. Raises what ended it, unchanged."""
    report.attempted += 1
    try:
        journal.opening(op)
        results[op.key] = _execute(client, op)
        journal.note(op, results[op.key])
        report.succeeded += 1
    except (Exception, KeyboardInterrupt) as exc:
        report.failed += 1
        report.errors.append(f"{op.kind} {op.label}: {str(exc) or repr(exc)}")
        try:
            # Anything that does not prove the write was refused is deliberately
            # NOT journalled as done. The entry stays open, which is what blocks
            # the next run from retrying a write that may have landed.
            if _proves_nothing_changed(exc):
                journal.note(op, None, error=str(exc))
            if on_progress:
                on_progress(i, total, op, False)
        except (Exception, KeyboardInterrupt) as also:
            # The error of the write is the one to raise. The line is not tried
            # again: an entry it did not close stays open, which is safe.
            report.errors.append(f"and then: {also!r}")
        # Stop on the first failure of any kind. Continuing runs later ops
        # against a profile that is no longer in the state the plan assumed,
        # and the ops after a failure are the least reviewed of the batch.
        raise


class _Reads:
    """What the server says after the writes: each thing read once, and only if asked for.

    Every read happens after every write, so one answer serves all the ops. A read
    that fails, in whatever way, is a :class:`VerificationFailed` for each op that
    needs it and is not tried again.
    """

    def __init__(self, client: NomadMania):
        self._client = client
        self.out = _blank()  # verify's report, to keep if something cuts verify short
        self._seen: dict[tuple, Any] = {}

    def _once(self, key: tuple, read: Callable[[], Any]) -> Any:
        if key not in self._seen:
            try:
                self._seen[key] = read()
            except VerificationFailed as exc:
                self._seen[key] = exc
            except Exception as exc:
                why = str(exc) if isinstance(exc, WanderfillError) else repr(exc)
                self._seen[key] = VerificationFailed("read-back", why)
        got = self._seen[key]
        if isinstance(got, VerificationFailed):
            raise got
        return got

    def visits(self, region: int) -> list[Visit]:
        return self._once(("visits", region), lambda: self._client._visits_strict(region))

    def marked(self) -> set[int]:
        return self._once(("marked",), lambda: self._client.visited_region_ids(strict=True))

    def ticked(self, op: Op) -> set[int]:
        """The list a tick op ticks; TCC is 29 requests."""
        where, _ = tick_of(op)  # type: ignore[misc]
        return self._once(where, lambda: ticked_before(self._client, [op])[where])

    def header(self, trip_id: int) -> tuple[str, str]:
        def read() -> tuple[str, str]:
            trip = self._client.trip(trip_id)["trip"]
            if days := _full_days(trip["date_from"], trip["date_to"]):
                return days
            raise VerificationFailed(
                "trips/get-trip",
                f"dates {str(trip['date_from'])[:30]!r}..{str(trip['date_to'])[:30]!r} "
                "cannot be read",
            )
        return self._once(("trip", trip_id), read)


def _blank() -> dict[str, Any]:
    return {
        "checked": 0, "mismatches": [], "phantom_trips": [], "not_counted": [], "unproven": [],
    }


def _tick_name(op: Op) -> str:
    _, n = tick_of(op)  # type: ignore[misc]
    return {"mark_kye": "KYE quadrant", "mark_tcc": "TCC destination", "mark_dare": "DARE area",
            }.get(op.kind, f"series {op.series} item") + f" {n}"


def _what_is_there_now(reads: _Reads, op: Op) -> list[str]:
    """What the server shows where ``op`` would have written, for the human who closes it.

    Worded as what is there, never as whether ``op`` landed. A visit found by its
    dates may have been put there by somebody else, and one not found yet may
    still appear.
    """
    def by_dates(region: int, days: tuple[str | None, str | None]) -> str:
        what = f"region {region}, {days[0]}..{days[1]}"
        try:
            visits = reads.visits(region)
        except VerificationFailed as exc:
            return f"{what}: the visits could not be read back ({exc})"
        there = any(_dates(v) == days for v in visits)
        return (f"{what}: a visit with these dates is {'there' if there else 'not there'}; "
                "whether this run made it is not known")

    header = (op.date_from, op.date_to)
    if op.kind == "add_visit":
        return [by_dates(op.region, header)]
    if op.kind == "create_trip":
        return [by_dates(int(r["id"]), _region_span(r) or header) for r in op.regions]
    if op.kind == "update_visit":
        what = f"visit {op.visit_id} in region {op.region}"
        try:
            live = next((v for v in reads.visits(op.region) if v.id == op.visit_id), None)
        except VerificationFailed as exc:
            return [f"{what}: the visits could not be read back ({exc})"]
        if live is None:
            return [f"{what}: not there"]
        return [f"{what}: reads {_iso_of(live.date_from)}..{_iso_of(live.date_to)} quality "
                f"{live.quality}; planned {op.date_from}..{op.date_to} quality {op.quality}"]
    try:
        held = tick_of(op)[1] in reads.ticked(op)  # type: ignore[index]
    except VerificationFailed as exc:
        return [f"{_tick_name(op)}: the list could not be read back ({exc})"]
    return [f"{_tick_name(op)} is {'' if held else 'not '}in the list now"]


def verify(
    client: NomadMania, ops: Sequence[Op], results: dict[str, Any] | None = None,
    reads: _Reads | None = None,
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

    Every list, region and trip header is read once, after all the writes, and
    strictly: a reply without its list, a list with something else in it and a
    read that fails are ``unproven`` for the ops that need them, and the other
    ops are still checked. ``reads`` shares what has been read already.
    """
    results = results or {}
    reads = reads or _Reads(client)
    out = reads.out

    def counted(region: int, stored: Sequence[int], what: str) -> None:
        """A stored visit either marks its region, or is a transit that does not."""
        try:
            marked = reads.marked()
        except VerificationFailed as exc:
            line = (f"the visited list could not be read back ({exc}), so whether the "
                    "regions written now count is not known")
            if line not in out["unproven"]:
                out["unproven"].append(line)
            return
        if region in marked:
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

    def visits(region: int, what: str) -> list[Visit] | None:
        try:
            return reads.visits(region)
        except VerificationFailed as exc:
            out["unproven"].append(f"{what}: the visits could not be read back ({exc})")
            return None

    def check(op: Op) -> None:
        if op.kind == "update_visit":
            stored = visits(op.region, f"visit {op.visit_id} in region {op.region}")
            if stored is None:
                return
            live = next((v for v in stored if v.id == op.visit_id), None)
            if live is None:
                out["mismatches"].append(f"visit {op.visit_id} is gone from region {op.region}")
                return
            if _dates(live) != (op.date_from, op.date_to):
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
            stored = visits(op.region, f"region {op.region}, {op.date_from}..{op.date_to}")
            if stored is None:
                return
            live = [v for v in stored if _dates(v) == (op.date_from, op.date_to)]
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
            check_trip(op)

        elif tick := tick_of(op):
            try:
                held = reads.ticked(op)
            except VerificationFailed as exc:
                out["unproven"].append(
                    f"{_tick_name(op)}: the list could not be read back ({exc})"
                )
            else:
                if tick[1] not in held:
                    out["mismatches"].append(f"{_tick_name(op)} did not stick")

    def check_trip(op: Op) -> None:
        # One trip with the planned dates, and each region in it carrying exactly
        # one visit. With the trip id the server answered, the visit is found by
        # ownership, so one that was already there cannot stand in for it. Without
        # the id it can only be found by its dates, and that is said out loud.
        trip = f"trip {op.date_from}..{op.date_to}"
        trip_id = _trip_id(results.get(op.key))
        owners: set[int | None] = set()
        for r in op.regions:
            region = int(r["id"])
            stored = visits(region, f"{trip}: region {region}")
            if stored is None:
                continue
            ends = _region_ends(r)
            want = (ends[0] or op.date_from, ends[1] or op.date_to)
            if trip_id is not None:
                mine = [v for v in stored if v.trip_id == trip_id]
                found_by = f"owned by trip {trip_id}"
            else:
                mine = [v for v in stored if _dates(v) == want]
                found_by = f"for {want[0]}..{want[1]}"
            if not mine:
                out["mismatches"].append(f"{trip}: region {region} has no visit {found_by}")
                continue
            # Each end on its own. One the plan gives must be stored as it is; one it
            # does not give is the trip's, as far as the plan can say.
            sides = [(got, planned, end) for v in mine for got, planned, end in
                     zip((v.date_from, v.date_to), want, ends, strict=True)]
            stored_as = [_dates(v) for v in mine]
            if any(_iso_of(got) != planned for got, planned, end in sides
                   if end or isinstance(got, dt.date)):
                out["mismatches"].append(
                    f"{trip}: region {region} stored {stored_as}, planned {want[0]}..{want[1]}"
                    + ("" if all(ends) else ", the trip's where it has no date of its own")
                )
            elif not all(end or isinstance(got, dt.date) for got, _, end in sides):
                out["unproven"].append(
                    f"{trip}: region {region} has no dates of its own in the plan and is "
                    f"stored as {stored_as}; what the server stores for such a region has "
                    "not been measured"
                )
            same = [v for v in stored if _dates(v) == _dates(mine[0])]
            if len(same) > 1 or len(mine) > 1:
                out["mismatches"].append(
                    f"{trip}: region {region} now has {max(len(same), len(mine))} visits "
                    f"for {_dates(mine[0])[0]}..{_dates(mine[0])[1]} — a duplicate"
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
            return
        try:
            header = reads.header(trip_id)
        except VerificationFailed as exc:
            out["unproven"].append(
                f"{trip}: the header of trip {trip_id} could not be read back ({exc})"
            )
        else:
            if header != (op.date_from, op.date_to):
                out["mismatches"].append(
                    f"{trip}: the header of trip {trip_id} reads {header[0]}..{header[1]}"
                )

    for op in ops:
        try:
            check(op)
        except Exception as exc:
            out["unproven"].append(f"{op.kind} {op.label!r}: could not be checked ({exc!r})")
        out["checked"] += 1  # only now: an interrupt leaves this op unchecked

    return out


def _iso_of(day) -> str | None:
    return day.isoformat() if day else None


def _dates(v: Visit) -> tuple[str | None, str | None]:
    return _iso_of(v.date_from), _iso_of(v.date_to)


def _trip_id(result: object) -> int | None:
    try:
        return int(result["trip_id"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None


def _has_own_dates(r: dict) -> bool:
    return any(r.get(f) not in (None, "") for f in TRIP_REGION_FIELDS
               if f not in ("id", "quality", "hidden"))


def _region_ends(r: dict) -> tuple[str | None, str | None]:
    """The dates one trip region was planned with, as ``_iso_of`` would print them.

    ``None`` for an end the plan does not give. A year with no month or day is a
    :class:`YearOnly` on the profile, not 1 January, and the wire carries numbers
    as strings as often as not.
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

    return side("from"), side("to")


def _region_span(r: dict) -> tuple[str, str] | None:
    start, end = _region_ends(r)
    return (start, end) if start and end else None


def _when(text: str | None):
    """A plan's date string, as either a real date or a bare year: see ``parse_when``."""
    return parse_when(text)


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

    if op.kind == "mark_tcc":
        # `item`, like KYE: TCC 64 is Akrotiri and Dhekelia, region 64 is not.
        return client.mark_tcc(op.item)

    if op.kind == "tick_series":
        if not client.tick_series_item(op.series, op.item):
            # Not a refusal. A toggle sent again can undo the tick it repeats.
            raise UnknownWriteOutcome(
                f"series {op.series} item {op.item}: toggle sent, the answer was not OK. "
                "Read the series back before running anything again."
            )
        return "OK"

    raise ValueError(f"unknown op kind: {op.kind}")
