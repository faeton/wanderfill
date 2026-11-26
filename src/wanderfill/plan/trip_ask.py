"""The questions of a trip list in a conversation, and the owner's replies to them.

The page is one way to answer a :class:`~wanderfill.plan.trip_list.TripList`. This is the
other: :func:`questions` is what the page shows, as data an agent can put to the owner in a
chat, and :func:`record` writes the owner's answers into the same answers file the page saves,
with the owner's reply kept verbatim beside them.

Nothing here reads the profile or sends anything. Nothing here answers either: :func:`record`
takes answers that somebody gives it, and refuses a file that ``trips plan`` would refuse.
Whoever calls it is bound by AGENTS.md rule 7: the answers and the words are the owner's own.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..api.client import MARKS_REGION_FROM, QUALITY
from ..api.errors import PlanInvalid
from .model import _r
from .trip_list import _TAKES, ANSWERS_FORMAT, TripList, in_view, read_answers, said_problem
from .trips import ONE, Answers, _one_will_do, _says, in_focus, kind_of, stretches


def questions(trip_list: TripList, answers: Answers | None = None) -> dict:
    """What still takes an answer, by the group it is in, and what was answered.

    A group is a stretch the list shows (all of them, or those of its focus), as
    :func:`~wanderfill.plan.trips.group` would make it with no cut answered. A run is asked
    while :func:`missing` says something of it: no answer, or a quality on a held run with no
    words where words are needed. ``no`` and ``?`` are final. Each run has the facts the page
    shows and its answer so far. Every place between two runs the list shows that is not
    forced is asked until it is answered, also the one before a stay that may not be over,
    which starts a group of its own: blank there is a new trip while that stay has no words,
    and the same trip once it has them.
    """
    answers = answers or {"runs": {}, "cuts": {}}
    focus = trip_list.params.get("focus")
    parts = (in_focus(trip_list.runs, trip_list.boundaries, focus) if focus
             else stretches(trip_list.runs, trip_list.boundaries))
    shown_runs, shown_cuts = in_view(trip_list)
    open_cuts = [b for b in shown_cuts if not b.forced and b.id not in answers["cuts"]]
    groups = []
    for part in parts:
        members = [trip_list.runs[n] for n in part]
        ids = {r.id for r in members}
        runs = []
        for r in members:
            row = answers["runs"].get(r.id, {})
            if _TAKES[r.status] and (lack := missing(r, row)):
                runs.append({**_run(r, trip_list), "missing": lack, "answer": dict(row)})
        cuts = [{**_cut(b), "between_groups": b.before not in ids}
                for b in open_cuts if b.after in ids]
        if runs or cuts:
            groups.append({"first": str(members[0].first), "last": str(members[-1].last),
                           "runs": runs, "cuts": cuts,
                           "also": [_also(r) for r in members if not _TAKES[r.status]]})
    tiles, photos, rows = trip_list.not_read
    return {
        "list": trip_list.sha256, "account": trip_list.account,
        "complete": not any(trip_list.not_read),
        "not_read": {"tiles": tiles, "photos": photos, "track_rows": rows},
        "shown": len(shown_runs), "groups": groups,
        "answered": {"runs": dict(answers["runs"]), "cuts": dict(answers["cuts"])},
        "quality": {str(q): word for q, word in QUALITY.items() if q},
        "marks_from": MARKS_REGION_FROM,
    }


def missing(run, row: Mapping) -> list[str]:
    """What an answer to ``run`` still lacks for :func:`~wanderfill.plan.trips.group`."""
    a = row.get("a")
    if a is None:
        return ["answer"]
    if a in ("no", "?"):
        return []
    if run.status == "held" and not _says(row.get("words", "")) and not (
            a == ONE and _one_will_do(run)):
        return ["words"]
    return []


def _run(run, trip_list: TripList) -> dict:
    takes = sorted(_TAKES[run.status], key=lambda a: (not a.isdigit(), a))
    return {
        "id": run.id, "region": run.region, "name": run.name, "status": run.status,
        "first": str(run.first), "last": str(run.last),
        "days": len(run.days), "silent": [str(d) for d in run.silent],
        "photos": run.photos,
        "first_photo": _clock(run.first_photo), "last_photo": _clock(run.last_photo),
        "evidence": run.evidence, "flags": list(run.flags), "reasons": list(run.reasons),
        "takes": takes,
        "needs_words": run.status == "held" and not _one_will_do(run),
        "one_needs_no_words": run.status == "held" and _one_will_do(run),
        "flight_days": [str(d) for d in run.flight],
        "includable_days": [str(d) for d in run.includable],
        "visits": trip_list.visits.get(str(run.region), []),
    }


def _also(run) -> dict:
    return {"id": run.id, "name": run.name, "status": run.status,
            "why": sorted({kind_of(x) for x in run.reasons})}


def _cut(edge) -> dict:
    return {"id": edge.id, "before": edge.before, "after": edge.after,
            "suggestions": list(edge.suggestions)}


def _clock(photo) -> str:
    minutes = round(photo.zone / 60)
    hours, rest = divmod(abs(minutes), 60)
    zone = f"UTC{'-' if minutes < 0 else '+'}{hours}" + (f":{rest:02}" if rest else "")
    return f"{photo.at:%Y-%m-%d %H:%M} ({zone})"


def record(
    path: str | Path,
    trip_list: TripList,
    said: str,
    runs: Mapping[str, Mapping] = {},
    cuts: Mapping[str, str] = {},
    *,
    replace: bool = False,
) -> Answers:
    """Put answers the owner gave in a conversation into the answers file at ``path``.

    ``said`` is their reply as they wrote it, and is kept beside every answer taken from it.
    ``runs`` maps a run id to its answer as the page writes one (``a``, ``words``, ``out``,
    ``in``); ``cuts`` a cut id to ``"cut"`` or ``"same"``. The file is made if it is not
    there. Only the fields given change; one that is there with another value is refused
    unless ``replace``, which is the owner changing their mind. Every reply is kept, in the
    order given: an earlier one stays as the record of what was said then. What is written is
    read back by
    :func:`~wanderfill.plan.trip_list.read_answers` before it replaces the file, so a file
    that ``trips plan`` would refuse is never left behind. Returns the answers in it now.
    """
    if problem := said_problem(said):
        raise PlanInvalid(f"the owner's reply cannot be kept: {problem}")
    if not runs and not cuts:
        raise PlanInvalid("nothing to record: name a run or a cut that the reply answers")
    def change(raw: dict) -> None:
        for key, value in runs.items():
            row = raw["runs"].setdefault(key, {})
            for field, new in value.items():
                if field in row and row[field] != new and not replace:
                    raise PlanInvalid(f"runs: {_r(key)} has {field} {_r(row[field])} already; "
                                      "--replace if the owner changed it")
                row[field] = new
        for key, value in cuts.items():
            if key in raw["cuts"] and raw["cuts"][key] != value and not replace:
                raise PlanInvalid(f"cuts: {_r(key)} is {_r(raw['cuts'][key])} already; "
                                  "--replace if the owner changed it")
            raw["cuts"][key] = value
        entry: dict = {"text": said}
        if runs:
            entry["runs"] = list(runs)
        if cuts:
            entry["cuts"] = list(cuts)
        raw["said"] = [*raw.get("said", []), entry]

    empty = {"format": ANSWERS_FORMAT, "account": trip_list.account,
             "manifest": trip_list.sha256, "runs": {}, "cuts": {}}
    return update_answers(path, empty, change, lambda f: read_answers(f, trip_list)[0])


def update_answers(path: str | Path, empty: dict, change, check):
    """Read the answers file (or ``empty``), ``change`` it, check it, and put it in place.

    One writer at a time: a second one is refused while the first holds the lock. The new
    file is written beside the old one under a name of its own and checked by ``check``
    before it replaces it; on any failure it is removed and the old file stays as it was.
    """
    import fcntl
    import tempfile

    p = Path(path)
    with open(p.with_name(p.name + ".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlanInvalid(f"{p} is being written by another run; try again") from None
        if p.exists():
            check(p)  # what is there is a file the plan takes, or nothing goes
            raw = json.loads(p.read_text(encoding="utf-8"))
        else:
            raw = json.loads(json.dumps(empty))
        change(raw)
        fd, name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".draft")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(raw, ensure_ascii=False, indent=1))
            try:
                out = check(Path(name))
            except PlanInvalid as exc:  # say it of the file the owner knows, not the draft
                raise PlanInvalid(str(exc).replace(name, str(p))) from None
            os.replace(name, p)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return out


def pairs(specs: Sequence[str], what: str) -> dict[str, str]:
    """``ID=VALUE`` as the command line gives them, each id once. The value may hold ``=``."""
    out: dict[str, str] = {}
    for spec in specs:
        key, sep, value = spec.partition("=")
        if not sep or not key or not value:
            raise PlanInvalid(f"{_r(spec)} is not ID={what}")
        if key in out:
            raise PlanInvalid(f"{_r(key)} is given twice; give it once")
        out[key] = value
    return out


def days_of(specs: Sequence[str]) -> dict[str, list[str]]:
    """``ID=DAY`` pairs, by run id, each day once and in the order of the days."""
    out: dict[str, list[str]] = {}
    for spec in specs:
        key, sep, d = spec.partition("=")
        if not sep or not key or not d:
            raise PlanInvalid(f"{_r(spec)} is not ID=YYYY-MM-DD")
        if d in out.get(key, []):
            raise PlanInvalid(f"{_r(key)}: {_r(d)} is given twice")
        out.setdefault(key, []).append(d)
    return {key: sorted(given) for key, given in out.items()}


__all__ = ["days_of", "missing", "pairs", "questions", "record", "update_answers"]
