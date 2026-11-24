"""The plan file — the spine of this package.

One rule shapes the whole design: **computation never writes, and writing never
computes.** A plan sits between them.

    resolve -> segment -> PLAN (a file) -> [a human reads it] -> apply -> journal

From that single primitive you get dry-run, review, idempotency, resumability,
reproducibility and a diff between two runs. ``apply`` accepts nothing except a
plan file: there is no compute-and-write command, and there is no flag that
creates one.

Two fields exist purely to prevent the two worst outcomes. ``account`` is the
uid the plan was generated for, and applying it while logged in as anyone else
is refused — writing one person's travel history onto another person's profile
is the incident nothing else recovers from. ``basis`` fingerprints live state at
plan time, so a profile that changed underneath forces a re-plan instead of a
last-write-wins.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from ..api.errors import PlanInvalid

SCHEMA = 1

OpKind = Literal["add_visit", "update_visit", "create_trip", "mark_dare",
                 "mark_kye", "mark_tcc", "tick_series"]
Bucket = Literal["apply", "review", "reject"]


@dataclass
class Op:
    """One intended write, with the evidence that justifies it."""

    kind: OpKind
    bucket: Bucket = "review"
    region: int | None = None
    visit_id: int | None = None
    series: int | None = None
    item: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    quality: int | None = None
    regions: list[dict] = field(default_factory=list)
    # Blurring or clearing a date is refused by the client unless the plan says
    # so here, in writing, where a human reviewing the file can see it.
    allow_vaguer: bool = False
    label: str = ""
    confidence: float = 0.0
    method: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Content hash used for idempotency.

        Covers everything that changes what reaches the server, and nothing
        about how the op was derived — so re-running the same intent against a
        journal is a no-op, while a *different* write is never mistaken for one
        already done.

        ``visit_id`` and ``quality`` are in here deliberately. Without them, two
        updates to different visit records in the same region on the same dates
        hash identically, and a journal entry for the first makes the second
        look complete; likewise a re-plan that raises a quality would be skipped
        as already-applied. Provenance fields — label, evidence, confidence,
        method — stay out, because changing the reasoning does not change the
        write.
        """
        material = json.dumps(
            [self.kind, self.region, self.visit_id, self.series, self.item,
             self.date_from, self.date_to, self.quality, self.allow_vaguer,
             sorted((r.get("id", 0), r.get("quality")) for r in self.regions)],
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]


@dataclass
class Plan:
    account: int
    ops: list[Op] = field(default_factory=list)
    basis: dict[str, Any] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    created: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    schema: int = SCHEMA
    tool: str = "wanderfill/0.1.0"

    # -- io ---------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(json.dumps(asdict(self), indent=1, ensure_ascii=False), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> Plan:
        """Read a plan file, or raise :class:`PlanInvalid` saying what is wrong with it.

        Only the *shape* is checked here: a file that is not JSON, has a field
        this version does not know, or lacks its account. What the ops say is
        :func:`validate`'s business, so that ``show`` can still open a plan that
        is wrong.
        """
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError as exc:  # not JSON, or not UTF-8
            raise PlanInvalid(f"{path}: not a readable plan file ({exc})") from None
        if not isinstance(raw, dict):
            raise PlanInvalid(f"{path}: a plan file is a JSON object, not {type(raw).__name__}")
        raw_ops = raw.pop("ops", [])
        if not isinstance(raw_ops, list):
            raise PlanInvalid(f"{path}: 'ops' must be a list, not {type(raw_ops).__name__}")
        try:
            ops = []
            for i, o in enumerate(raw_ops):
                if not isinstance(o, dict):
                    raise PlanInvalid(f"{path}: ops[{i}] is not an object")
                ops.append(Op(**o))
            return cls(ops=ops, **raw)
        except TypeError as exc:  # an unknown or missing field
            raise PlanInvalid(f"{path}: {exc}") from None

    # -- reading ----------------------------------------------------------

    def to_apply(self) -> list[Op]:
        """Only ops a human has left in the apply bucket ever execute."""
        return [o for o in self.ops if o.bucket == "apply"]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.ops:
            out[f"{o.kind}:{o.bucket}"] = out.get(f"{o.kind}:{o.bucket}", 0) + 1
        return out


def fingerprint(state: dict) -> str:
    """A stable hash of live server state, for detecting drift between plan and apply."""
    material = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def regions_touched(ops: Iterable[Op]) -> set[int]:
    """Every NomadMania region an op can change, including trip members.

    A ``create_trip`` names its regions in ``Op.regions``, not in ``Op.region``,
    so deriving targets from ``o.region`` alone leaves a whole trip's worth of
    regions outside the drift check — the exact regions the trip is about to
    write visits into.

    ``mark_kye``, ``mark_tcc`` and ``tick_series`` contribute nothing: their
    ids live in ``item`` and are a different namespace that merely overlaps
    numerically.
    """
    out: set[int] = set()
    for op in ops:
        if op.kind in ("add_visit", "update_visit") and op.region:
            out.add(int(op.region))
        elif op.kind == "create_trip":
            for r in op.regions:
                if r.get("id"):
                    out.add(int(r["id"]))
    return out


def basis_of(snapshot: dict, regions: Iterable[int]) -> dict:
    """The live state a plan is allowed to assume, reduced to a comparable form.

    Which region ids are visited is not enough. A plan whose whole purpose is to
    *re-date an existing visit* changes nothing about that set, so a fingerprint
    over it alone cannot tell "nobody touched this" from "the user corrected
    these dates by hand an hour ago" — and the apply would then overwrite the
    correction with the older intent. So the visit records for the regions the
    plan touches go in too: id, dates and quality, which is everything an update
    can destroy.

    Regions the plan does not touch are excluded on purpose. Travel recorded
    elsewhere while a plan sat on disk is not a reason to refuse it.
    """
    visits = snapshot.get("visits", {})
    basis = {
        "visited": sorted(snapshot.get("visited_regions", [])),
        "dare": sorted(snapshot.get("visited_dare", [])),
        "visits": {
            str(r): sorted(
                (v.get("id"), str(v.get("date_from")), str(v.get("date_to")), v.get("quality"))
                for v in visits.get(str(r), [])
            )
            for r in sorted(set(regions))
        },
    }
    # Only in a snapshot taken for a TCC plan, so every other plan's fingerprint
    # is what it was before TCC existed.
    if "visited_tcc" in snapshot:
        basis["tcc"] = sorted(snapshot["visited_tcc"])
    return basis


# -- validation ---------------------------------------------------------------

_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PARTS = ("year", "month", "day")


def _is_id(value: object) -> bool:
    """An int that is not a bool: ``True`` is ``1`` to Python and to no server."""
    return type(value) is int


def _when_of(text: object, *, full: bool = False) -> tuple[str, Any] | str | None:
    """A plan date as ``("date", date)``, ``("year", int)`` or ``None``; an error is a ``str``.

    The forms are the ones ``apply._when`` executes: ``YYYY-MM-DD``, a bare
    ``YYYY``, or nothing. ``full`` accepts only the first.
    """
    if text is None:
        return None
    if isinstance(text, str):
        if _ISO_DAY.fullmatch(text):
            try:
                return ("date", dt.date.fromisoformat(text))
            except ValueError:
                pass
        elif not full and len(text) == 4 and text.isdecimal() and int(text) > 0:
            return ("year", int(text))
    expected = "YYYY-MM-DD" if full else "YYYY-MM-DD or a bare YYYY"
    return f"{text!r} is not {expected}"


def _backwards(a: tuple[str, Any], b: tuple[str, Any]) -> bool:
    """Start after end, where both are precise, and by year where either is not."""
    if a[0] == b[0]:
        return a[1] > b[1]
    year = lambda w: w[1].year if w[0] == "date" else w[1]  # noqa: E731
    return year(a) > year(b)


def _span_problem(start: object, end: object) -> str | None:
    pair = [_when_of(start), _when_of(end)]
    for w in pair:
        if isinstance(w, str):
            return w
    if None not in pair and _backwards(*pair):
        return f"starts {start} after it ends {end}"
    return None


def _number(value: object) -> int | None:
    """An int, or the digits of one: the site's own forms send both."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _side_of(region: dict, which: str) -> tuple[tuple[str, Any] | None, str | None]:
    """One end of a trip region's own dates: the year is the anchor, month and day a pair."""
    got = {p: region.get(f"{p}_{which}") for p in _PARTS}
    given = {p: v for p, v in got.items() if v is not None and v != ""}
    if not given:
        return None, None
    names = ", ".join(f"{p}_{which}" for p in given)
    if "year" not in given:
        return None, f"has {names} but no year_{which}"
    if ("month" in given) != ("day" in given):
        return None, f"month_{which} and day_{which} come together, not one without the other"
    nums = {p: _number(v) for p, v in given.items()}
    if None in nums.values():
        return None, f"{names} must be whole numbers"
    if "month" not in nums:
        return (("year", nums["year"]), None) if nums["year"] > 0 else (None, "year must be > 0")
    try:
        return ("date", dt.date(nums["year"], nums["month"], nums["day"])), None
    except ValueError:
        return None, f"{nums['year']}-{nums['month']}-{nums['day']} is not a real date"


def _trip_problems(op: Op) -> list[str]:
    out = []
    # client.create_trip calls .isoformat() on both, and the site needs both.
    for name in ("date_from", "date_to"):
        if (w := _when_of(getattr(op, name), full=True)) is None:
            out.append(f"{name} is required (YYYY-MM-DD)")
        elif isinstance(w, str):
            out.append(f"{name}: {w}")
    if not out and _backwards(_when_of(op.date_from), _when_of(op.date_to)):
        out.append(f"starts {op.date_from} after it ends {op.date_to}")
    if not isinstance(op.regions, list) or not op.regions:
        return [*out, "has no regions"]
    seen: set[int] = set()
    for n, r in enumerate(op.regions):
        at = f"regions[{n}]"
        if not isinstance(r, dict):
            out.append(f"{at} is not an object")
            continue
        if not _is_id(r.get("id")):
            out.append(f"{at} has no integer id")
        elif r["id"] in seen:
            out.append(f"{at}: region {r['id']} is listed twice")
        else:
            seen.add(r["id"])
        q = r.get("quality")
        if not _is_id(q) or not 1 <= q <= 6:
            out.append(f"{at} needs a quality from 1 to 6, not {q!r}")
        sides = {w: _side_of(r, w) for w in ("from", "to")}
        bad = [f"{at}: {why}" for _, why in sides.values() if why]
        out += bad
        a, b = sides["from"][0], sides["to"][0]
        if not bad and a and b and _backwards(a, b):
            out.append(f"{at} starts after it ends")
    return out


def _op_problems(op: Op) -> list[str]:
    """What is wrong with one apply-bucket op on its own, as short phrases."""
    out: list[str] = []

    def need(field_name: str) -> None:
        if not _is_id(getattr(op, field_name)):
            out.append(f"needs an integer {field_name}")

    if op.kind == "create_trip":
        return _trip_problems(op)
    if op.kind in ("add_visit", "update_visit", "mark_dare"):
        need("region")
    if op.kind == "update_visit":
        need("visit_id")
    if op.kind in ("mark_kye", "mark_tcc", "tick_series"):
        need("item")
    if op.kind == "tick_series":
        need("series")
    if op.kind in ("add_visit", "update_visit"):
        # 0 is "no visit", and an absent quality is a broken plan, not a default 3.
        if not _is_id(op.quality) or not 1 <= op.quality <= 6:
            out.append(f"needs a quality from 1 to 6, not {op.quality!r}")
        if why := _span_problem(op.date_from, op.date_to):
            out.append(f"dates: {why}")
    return out


def validate(plan: Plan) -> list[str]:
    """Everything wrong with a plan that can be known without the server.

    Empty means valid. Pure: no network, no clock. The schema and each op's kind
    and bucket are checked for the whole file; only ops a human has left in the
    ``apply`` bucket are checked for content and for duplicating each other,
    since only those execute. Each message names the op by its index in the file,
    its kind and its label, so a person can find it.

    This exists because a malformed op used to fail when it was reached, after
    every earlier op had already been written to a real profile.
    """
    out: list[str] = []
    if type(plan.schema) is not int or plan.schema != SCHEMA:
        out.append(f"plan schema is {plan.schema!r}; this version reads schema {SCHEMA} only")
    if not _is_id(plan.account):
        out.append(f"plan account must be an integer, not {plan.account!r}")

    kinds, buckets = get_args(OpKind), get_args(Bucket)
    seen_key: dict[str, int] = {}
    adds: dict[tuple, int] = {}
    updated: dict[int, int] = {}

    for i, op in enumerate(plan.ops):
        who = f"ops[{i}] {op.kind} {op.label!r}"
        if op.kind not in kinds:
            out.append(f"{who}: unknown kind")
            continue
        if op.bucket not in buckets:
            out.append(f"{who}: unknown bucket {op.bucket!r}")
            continue
        if op.bucket != "apply":
            continue
        if own := _op_problems(op):
            out += [f"{who}: {p}" for p in own]
            continue

        if op.key in seen_key:
            out.append(f"{who}: the same write twice: it has the same key {op.key} as "
                       f"ops[{seen_key[op.key]}]. Remove one and re-plan.")
            continue
        seen_key[op.key] = i
        if op.kind == "add_visit":
            same = (op.region, op.date_from, op.date_to)
            if same in adds:
                out.append(f"{who}: adds region {op.region} for {op.date_from}..{op.date_to} "
                           f"a second time (ops[{adds[same]}]), which would duplicate the visit")
            adds.setdefault(same, i)
        elif op.kind == "update_visit":
            if op.visit_id in updated:
                out.append(f"{who}: visit {op.visit_id} is already updated by "
                           f"ops[{updated[op.visit_id]}]; the second would replace the first")
            updated.setdefault(op.visit_id, i)
    return out
