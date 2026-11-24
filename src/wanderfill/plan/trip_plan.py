"""From the trip list, the owner's answers and the live profile to one plan file.

The computing is in :mod:`wanderfill.plan.trips`, and what the owner was shown in
:mod:`wanderfill.plan.trip_list`. Here the profile is read, for the list and again for the
plan, with the one strict reading that ``apply`` takes its snapshot with; nothing is cached,
and nothing is sent. The second reading must show what the first did: every visit of every
region on the list, and every trip over the same days (decision 11 of the design).

A plan has ``create_trip`` ops and no others. A row of one is a run the owner gave a quality
to, with the run's own first and last day.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..api.client import MARKS_REGION_FROM, QUALITY, NomadMania, Visit
from ..api.errors import AccountMismatch, DriftError, PlanInvalid
from .model import Op, Plan, basis_of, fingerprint, parse_when, shown, validate
from .trip_list import ListedRun, PhotoTime, TripList, trips_rows, visits_rows
from .trips import Answers, AnyRun, NotMade, TripDraft, TripRow, group

MAX_ROWS = 50
AGAIN = "No plan was made; run `trips review` again."


# -- the profile, read live ---------------------------------------------------------------


def visits_in(snapshot: Mapping) -> dict[int, list[Visit]]:
    """The visits of a :meth:`~wanderfill.api.client.NomadMania.snapshot`, by id.

    They are what :func:`~wanderfill.plan.trips.judge` takes. One reading serves it and the
    list; and in one order, so that a server that gives them in another makes no other list.
    """
    out: dict[int, list[Visit]] = {}
    for region, rows in snapshot["visits"].items():
        visits = [Visit(**row | {k: parse_when(row[k]) for k in ("date_from", "date_to")})
                  for row in rows]
        out[int(region)] = sorted(visits, key=lambda v: v.id)
    return out


def trips_listed(client: NomadMania, runs: Iterable[AnyRun]) -> list[dict]:
    """The trips listed from the year before the first run to the year after the last.

    A trip over New Year is listed under one of its years, and nothing says which.
    """
    runs = list(runs)
    if not runs:
        return []
    first, last = min(r.first for r in runs).year - 1, max(r.last for r in runs).year + 1
    listing = client.trip_listing((f"{first:04}", f"{last:04}"))
    return [row for rows in listing.values() for row in rows]


def _changed(client: NomadMania, trip_list: TripList, snapshot: Mapping) -> list[str]:
    """What on the profile is not what the list shows, in words."""
    names = {r.region: _named(r) for r in trip_list.runs}
    now = visits_rows(snapshot["visits"], names)
    out = [f"the visits of {names[int(region)]}" for region, rows in now.items()
           if rows != trip_list.visits[region]]
    was, there = ({t["id"]: t for t in trips} for trips in (
        trip_list.trips, trips_rows(trips_listed(client, trip_list.runs), trip_list.runs)))
    return out + [f"trip {id}" for id in sorted(was.keys() | there)
                  if was.get(id) != there.get(id)]


# -- the plan ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TripPlan:
    """A plan, what it says nothing of, and which regions it marks that are not marked now."""

    plan: Plan
    not_made: tuple[NotMade, ...]
    marks: frozenset[int]


def plan_for_trips(
    trip_list: TripList,
    answers: Answers,
    client: NomadMania,
    *,
    answers_sha256: str | None = None,
    max_rows: int = MAX_ROWS,
) -> Plan:
    """The plan of the trips that ``answers`` make of ``trip_list``, which may have no ops.

    The account, the visits of every region on the list and the trips over its days are read
    live. Another account raises :class:`AccountMismatch`, and a region or a trip that is not
    as the list shows it :class:`DriftError`, which names them. Over ``max_rows`` region
    visits, and answers the page could not have written, raise :class:`PlanInvalid`.
    """
    return plan_trips(trip_list, answers, client, answers_sha256=answers_sha256,
                      max_rows=max_rows).plan


def plan_trips(
    trip_list: TripList,
    answers: Answers,
    client: NomadMania,
    *,
    answers_sha256: str | None = None,
    max_rows: int = MAX_ROWS,
) -> TripPlan:
    """:func:`plan_for_trips`, with what the owner is told beside the plan."""
    drafts, not_made = group(trip_list.runs, trip_list.boundaries, answers)
    count = sum(len(d.rows) for d in drafts)
    if count > max_rows:
        raise PlanInvalid(f"these answers make {count} region visits, and one plan takes "
                          f"{max_rows}; answer fewer of them, or say so with --max-rows")
    if (account := client.account_id()) != trip_list.account:
        raise AccountMismatch(
            f"the list was made for account {trip_list.account}, logged in as {account}")
    # One reading of every region on the list: what is compared with the list is then also
    # what the plan's fingerprint is taken of. Of that only the planned regions are in it.
    snap = client.snapshot({r.region for r in trip_list.runs})
    if changed := _changed(client, trip_list, snap):
        raise DriftError("not as it was when the list was made: " + "; ".join(changed)
                         + f". {AGAIN}")
    planned = {row.run.region for d in drafts for row in d.rows}
    sources: list[dict] = [{"kind": "trips", "sha256": trip_list.sha256}]
    if answers_sha256:
        sources.append({"kind": "answers", "sha256": answers_sha256})
    plan = Plan(
        account=account,
        ops=[_op(d, trip_list, answers) for d in drafts],
        basis={
            "fingerprint": fingerprint(basis_of(snap, planned)),
            "taken_at": snap["taken_at"],  # gives a plan built again a journal of its own
        },
        params={**trip_list.params, "no_track": trip_list.no_track},
        sources=[*sources, *trip_list.sources],
    )
    if problems := validate(plan):
        raise PlanInvalid("the plan would not be valid: " + "; ".join(problems))
    marks = {row.run.region for d in drafts for row in d.rows
             if row.quality >= MARKS_REGION_FROM} - set(snap["visited_regions"])
    return TripPlan(plan, tuple(not_made), frozenset(marks))


def _named(run: ListedRun) -> str:
    return f"{shown(run.name, 'region')} ({run.region})"


def _op(draft: TripDraft, trip_list: TripList, answers: Answers) -> Op:
    names = ", ".join(shown(row.run.name, f"region {row.run.region}") for row in draft.rows)
    evidence = [f"The owner answered on list {trip_list.sha256[:12]}:"]
    for row in draft.rows:
        marks = "" if row.quality >= MARKS_REGION_FROM else ", which does not mark the region"
        evidence.append(f"{_named(row.run)}, {row.run.first} to {row.run.last}: "
                        f"{row.quality} {QUALITY[row.quality]}{marks}")
    ids = [r.id for r in trip_list.runs]
    inside = trip_list.runs[ids.index(draft.rows[0].run.id):ids.index(draft.rows[-1].run.id)]
    for run in inside:
        if answers["runs"].get(run.id, {}).get("a") == "no":
            evidence.append(f"{_named(run)}, {run.first} to {run.last}: left out by the owner")
    return Op(
        "create_trip", bucket="apply", date_from=draft.date_from.isoformat(),
        date_to=draft.date_to.isoformat(), regions=[_region(row) for row in draft.rows],
        label=f"{draft.date_from} to {draft.date_to}: {names}", confidence=0.0,
        method="trips review: ground photos, owner's answers", evidence=evidence,
        proof=[entry for row in draft.rows for entry in _proof(row, trip_list)])


def _region(row: TripRow) -> dict:
    ends = ((side, part, getattr(when, part))
            for side, when in (("from", row.run.first), ("to", row.run.last))
            for part in ("year", "month", "day"))
    return {"id": row.run.region, "quality": row.quality,
            **{f"{part}_{side}": n for side, part, n in ends}}


# -- what it rests on ------------------------------------------------------------------------


def _count(n: int, one: str) -> str:
    return f"{n} {one}" + ("" if n == 1 else "s")


def _clock(photo: PhotoTime) -> str:
    minutes = round(photo.zone / 60)
    hours, rest = divmod(abs(minutes), 60)
    zone = f"UTC{'-' if minutes < 0 else '+'}{hours}" + (f":{rest:02}" if rest else "")
    return f"{photo.at:%Y-%m-%d %H:%M} ({zone})"


def _visit(row: Mapping) -> str:
    ends = [row[k] or "no date" for k in ("date_from", "date_to")]
    when = "no dates" if ends == ["no date"] * 2 else " to ".join(ends)
    owner = "" if row["trip_id"] is None else f" in trip {row['trip_id']}"
    return f"visit {row['id']}, {when}, quality {row['quality']}{owner}"


def _days(days: Iterable[dt.date]) -> str:
    return ", ".join(map(str, days))


def _proof(row: TripRow, trip_list: TripList) -> list[dict]:
    """What the page showed of one run, and the owner's own words if it has any."""
    run = row.run
    known = trip_list.visits[str(run.region)]
    parts = [
        f"{run.region} {shown(run.name, 'region')}: {run.first} to {run.last}",
        f"photos on {_count(len(run.days), 'day')}"
        + (f", none on {_days(run.silent)}" if run.silent else ""),
        f"{_count(run.photos, 'photo')}, from {_clock(run.first_photo)} to "
        f"{_clock(run.last_photo)}",
        f"evidence: {run.evidence}",
        *run.flags, *run.reasons,
        f"on the profile: {_count(len(known), 'visit')} of this region"
        + "".join(f"; {_visit(v)}" for v in known),
    ]
    out = [{"kind": "ground_photo", "detail": shown("; ".join(parts))}]
    if row.words:
        out.append({"kind": "memory", "detail": row.words})
    return out


__all__ = [
    "MAX_ROWS",
    "TripPlan",
    "plan_for_trips",
    "plan_trips",
    "trips_listed",
    "visits_in",
]
