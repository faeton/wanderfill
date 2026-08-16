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
from typing import Any, Literal

SCHEMA = 1

OpKind = Literal["add_visit", "update_visit", "create_trip", "mark_dare",
                 "mark_kye", "tick_series"]
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
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        ops = [Op(**o) for o in raw.pop("ops", [])]
        return cls(ops=ops, **raw)

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
    return {
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
