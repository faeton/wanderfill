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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from ..api.client import YearOnly
from ..api.errors import PlanInvalid

SCHEMA = 1

OpKind = Literal["add_visit", "update_visit", "create_trip", "mark_dare",
                 "mark_kye", "mark_tcc", "tick_series"]
Bucket = Literal["apply", "review", "reject"]
# What an entry of ``Op.proof`` can rest on. Named, because a ``confidence`` number
# would say of an owner's answer what nothing measured.
PROOF_KINDS = frozenset({"ground_photo", "fast_photo", "document", "memory"})


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
    # Objects with a ``kind`` from PROOF_KINDS and a ``detail``, both text.
    proof: list[dict] = field(default_factory=list)

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
        as already-applied. A trip's regions are in with their own dates and
        ``hidden`` for the same reason. Provenance fields — label, evidence,
        proof, confidence, method — stay out, because changing the reasoning does
        not change the write.
        """
        if self.kind == "create_trip":
            rows = [_region_row(r) for r in self.regions]
            # The rest as text: absent parts are None, which has no order.
            return self._hash(sorted(rows, key=lambda row: (row[0], json.dumps(row))))
        return self._hash(self._ids_and_qualities())

    @property
    def legacy_key(self) -> str | None:
        """What earlier versions called this trip, or ``None`` for every other kind.

        It covered only each region's id and quality, so a trip and the same
        trip with its dates corrected share it. Journals on disk hold it, and
        ``apply`` refuses such a trip: the key cannot say which of them landed.
        """
        return self._hash(self._ids_and_qualities()) if self.kind == "create_trip" else None

    def _ids_and_qualities(self) -> list[tuple]:
        return sorted((r.get("id", 0), r.get("quality")) for r in self.regions)

    def _hash(self, regions: list) -> str:
        material = json.dumps(
            [self.kind, self.region, self.visit_id, self.series, self.item,
             self.date_from, self.date_to, self.quality, self.allow_vaguer, regions],
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
        except OSError as exc:  # missing, unreadable, a directory
            raise PlanInvalid(f"{path}: cannot be read ({exc.strerror or exc})") from None
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

_PARTS = ("year", "month", "day")
_DIGITS = frozenset("0123456789")
# Everything a trip region may carry. Region dicts are sent as they stand, and this
# is also what the key is made of, so nothing reaches the server outside the key.
TRIP_REGION_FIELDS = ("id", "quality", "hidden",
                      *(f"{p}_{which}" for which in ("from", "to") for p in _PARTS))


def _r(value: object) -> str:
    """``repr`` that cannot raise or fill a screen: an int of 5,000 digits has no repr."""
    try:
        text = repr(value)
    except ValueError:
        return "<a number too large to print>"
    return text if len(text) <= 60 else text[:57] + "..."


def parse_when(text: object):
    """A plan's date string, as either a real date or a bare year; the one parser.

    ``"2013"`` is not a shorthand for 1 January 2013. It is the profile saying
    "some time in 2013", which the API stores natively and which YES reads
    exactly as well as a precise date. Expanding it to a January the traveller
    never claimed would invent evidence, so it stays a :class:`YearOnly`.

    ``apply`` executes with this and :func:`validate` judges with this, so the
    year range (``YearOnly``'s own) and the date forms cannot drift apart.
    Raises ``ValueError`` for anything it cannot read, and for a non-string.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError(f"{_r(text)} is not text")
    if len(text) == 4 and all(c in _DIGITS for c in text):
        return YearOnly(int(text))
    # One spelling per date. Python's own parser takes "20240101" and, from 3.11,
    # week dates too, and two spellings of one day hash to two keys, so the
    # duplicate check cannot see them and the read-back cannot match them.
    # `isdigit` is not the test either: it accepts digits of other scripts.
    if len(text) == 10 and (day := dt.date.fromisoformat(text)).isoformat() == text:
        return day
    raise ValueError(f"{_r(text)} is not YYYY-MM-DD or a bare YYYY")


def _is_id(value: object) -> bool:
    """An int that is not a bool: ``True`` is ``1`` to Python and to no server."""
    return type(value) is int


def _when_of(text: object, *, full: bool = False) -> tuple[str, Any] | str | None:
    """A plan date as ``("date", date)``, ``("year", int)`` or ``None``; an error is a ``str``.

    ``full`` accepts only a precise day.
    """
    try:
        got = parse_when(text)
    except ValueError as exc:
        return f"{_r(text)} cannot be used as a date ({exc})"
    if got is None:
        return None
    if isinstance(got, YearOnly):
        return f"{_r(text)} is not YYYY-MM-DD" if full else ("year", got.year)
    return ("date", got)


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
    # Short and ASCII: the site's forms send numbers as text, not 5,000 digits of it.
    if isinstance(value, str) and len(value) <= 10 and value and set(value) <= _DIGITS:
        return int(value)
    return None


def _part(value: object) -> object:
    """A region's number as it counts: ``"02"`` is 2, and absent, null and empty are ``None``.

    Anything else comes back as it is, so that two values nobody can read do not
    become the same one.
    """
    if value is None or value == "":
        return None
    n = _number(value)
    return value if n is None else n


def _region_row(r: dict) -> list:
    """What one trip region puts in the trip's key, however its dict was spelled."""
    row = {f: _part(r.get(f)) for f in TRIP_REGION_FIELDS}
    row["hidden"] = row["hidden"] or 0
    return list(row.values())


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
    try:
        YearOnly(nums["year"])  # the range execution and the read-back both enforce
        if "month" not in nums:
            return ("year", nums["year"]), None
        return ("date", dt.date(nums["year"], nums["month"], nums["day"])), None
    except (ValueError, OverflowError) as exc:
        return None, f"{', '.join(f'{k}={_r(v)}' for k, v in nums.items())} is not usable ({exc})"


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
        return [*out, f"needs a non-empty list of regions, not {_r(op.regions)}"]
    seen: set[int] = set()
    for n, r in enumerate(op.regions):
        at = f"regions[{n}]"
        if not isinstance(r, dict):
            out.append(f"{at} is not an object")
            continue
        if not _is_id(r.get("id")):
            out.append(f"{at} has no integer id")
        elif r["id"] in seen:
            out.append(f"{at}: region {_r(r['id'])} is listed twice")
        else:
            seen.add(r["id"])
        out += [f"{at} has a field {_r(f)}; a trip region may carry only "
                f"{', '.join(TRIP_REGION_FIELDS)}" for f in r if f not in TRIP_REGION_FIELDS]
        h = r.get("hidden")
        if not (h is None or h in ("0", "1") or (_is_id(h) and h in (0, 1))):
            out.append(f"{at}: hidden must be 0, 1 or absent, not {_r(h)}")
        q = r.get("quality")
        if not _is_id(q) or not 1 <= q <= 6:
            out.append(f"{at} needs a quality from 1 to 6, not {_r(q)}")
        sides = {w: _side_of(r, w) for w in ("from", "to")}
        bad = [f"{at}: {why}" for _, why in sides.values() if why]
        out += bad
        a, b = sides["from"][0], sides["to"][0]
        if not bad:
            # One end, or a day against a year, is a date the read-back cannot hold
            # the server to.
            if (a is None) != (b is None):
                out.append(f"{at} has only its {'from' if a else 'to'} date; a region gives both "
                           "ends or neither")
            elif a and b and a[0] != b[0]:
                out.append(f"{at} has a full date at one end and a bare year at the other")
            elif a and b and _backwards(a, b):
                out.append(f"{at} starts after it ends")
    return out


def _proof_problems(proof: object) -> list[str]:
    if not isinstance(proof, list):
        return [f"proof must be a list of objects, not {_r(proof)}"]
    out = []
    for n, p in enumerate(proof):
        kind, detail = (p.get("kind"), p.get("detail")) if isinstance(p, dict) else (None, None)
        if not isinstance(kind, str) or kind not in PROOF_KINDS:
            out.append(f"proof[{n}] must be an object whose kind is one of "
                       f"{', '.join(sorted(PROOF_KINDS))}, not {_r(p)}")
        elif not isinstance(detail, str):
            out.append(f"proof[{n}] needs a detail in text, not {_r(detail)}")
    return out


_INT_FIELDS = ("region", "visit_id", "series", "item", "quality")


def _op_problems(op: Op) -> list[str]:
    """What is wrong with one apply-bucket op on its own, as short phrases.

    Every field is type-checked whether or not the kind uses it, because every
    one reaches the op's key, and some reach the server (``allow_vaguer`` as the
    string ``"false"`` is truthy, and permits erasing a date). ``proof`` reaches
    neither, and is checked because a person approves the write on it.
    """
    out: list[str] = []
    for name in _INT_FIELDS:
        v = getattr(op, name)
        if v is not None and not _is_id(v):
            out.append(f"{name} must be an integer or absent, not {_r(v)}")
    if type(op.allow_vaguer) is not bool:
        out.append(f"allow_vaguer must be true or false, not {_r(op.allow_vaguer)}")
    if not isinstance(op.regions, list) or not all(isinstance(r, dict) for r in op.regions):
        out.append(f"regions must be a list of objects, not {_r(op.regions)}")
    out += _proof_problems(op.proof)
    if out:
        return out

    def need(field_name: str) -> None:
        if getattr(op, field_name) is None:
            out.append(f"needs an integer {field_name}")

    if op.kind == "create_trip":
        return _trip_problems(op)
    # Execution parses both dates of every kind, so every kind's are judged.
    if why := _span_problem(op.date_from, op.date_to):
        out.append(f"dates: {why}")
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
        if op.quality is None or not 1 <= op.quality <= 6:
            out.append(f"needs a quality from 1 to 6, not {_r(op.quality)}")
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
    if not isinstance(plan.basis, dict):
        out.append(f"plan basis must be an object, not {_r(plan.basis)}")
    if type(plan.schema) is not int or plan.schema != SCHEMA:
        out.append(f"plan schema is {_r(plan.schema)}; this version reads schema {SCHEMA} only")
    if not _is_id(plan.account):
        out.append(f"plan account must be an integer, not {_r(plan.account)}")

    kinds, buckets = get_args(OpKind), get_args(Bucket)
    seen_key: dict[str, int] = {}
    adds: dict[tuple, int] = {}
    updated: dict[int, int] = {}

    for i, op in enumerate(plan.ops):
        who = f"ops[{i}] {op.kind} {_r(op.label)}"
        if op.kind not in kinds:
            out.append(f"{who}: unknown kind")
            continue
        if op.bucket not in buckets:
            out.append(f"{who}: unknown bucket {_r(op.bucket)}")
            continue
        if op.bucket != "apply":
            continue
        if own := _op_problems(op):
            out += [f"{who}: {p}" for p in own]
            continue

        try:
            key = op.key
        except Exception as exc:  # a shape _op_problems does not reach, e.g. mixed region ids
            out.append(f"{who}: its regions cannot be keyed ({exc})")
            continue
        if key in seen_key:
            out.append(f"{who}: the same write twice: it has the same key {key} as "
                       f"ops[{seen_key[key]}]. Remove one and re-plan.")
            continue
        seen_key[key] = i
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
