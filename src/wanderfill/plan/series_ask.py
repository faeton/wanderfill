"""The questions of a series list in a conversation, and the owner's replies to them.

As :mod:`wanderfill.plan.trip_ask`, for a :class:`~wanderfill.plan.review.Manifest`:
:func:`questions` is what the page shows, and :func:`record` writes the owner's answers into
the answers file the page saves, with their reply kept verbatim beside them.

Every item here has a photo of the owner near it: the list has no other kind, and nothing
here prints one that has none. Nothing is read from the profile, sent, or answered.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..api.errors import PlanInvalid
from .model import _r
from .review import (
    AIRPORT_SERIES,
    ANSWERS_FORMAT,
    Answer,
    Manifest,
    _key,
    _rank,
    read_answers,
)
from .trip_ask import update_answers
from .trip_list import said_problem

SAYS = {"y": "I was there", "n": "I was not there", "q": "I don't remember"}


def questions(
    manifest: Manifest, answers: Mapping[tuple[int, int], Answer] | None = None,
    series: int | None = None,
) -> dict:
    """The items that take an answer, by series in the list's order, and how many were answered.

    ``series`` keeps one series. Each item has the facts the page shows of it, and no answer.
    """
    answers = answers or {}
    by: dict[int, dict] = {}
    for c in sorted(manifest.candidates, key=_rank):
        if (series is not None and c.series != series) or (c.series, c.item) in answers:
            continue
        group = by.setdefault(c.series, {"series": c.series, "name": c.series_name,
                                         "airports": c.series in AIRPORT_SERIES, "items": []})
        group["items"].append({
            "id": _key(c), "item": c.item, "name": c.name, "km": c.km,
            "at": c.at.isoformat(timespec="minutes"), "photos": c.photos, "days": c.days,
            "origins": list(c.origins), "fast_near": c.fast_near,
            "marks": [m for m, on in (("conflict", c.conflict), ("one spot", c.one_spot),
                                      ("thin", c.thin)) if on],
        })
    return {"list": manifest.sha256, "account": manifest.account,
            "complete": not manifest.coverage["tiles_unread"],
            "tiles_not_read": manifest.coverage["tiles_unread"],
            "radius_km": manifest.params["radius_km"], "answers": SAYS,
            "answered": len(answers), "series": list(by.values())}


def record(
    path: str | Path,
    manifest: Manifest,
    said: str,
    items: Mapping[str, str],
    *,
    replace: bool = False,
) -> dict[tuple[int, int], Answer]:
    """Put the owner's answers from a conversation into the answers file at ``path``.

    ``items`` maps ``SERIES:ITEM`` to ``y``, ``n`` or ``q``; ``said`` is their reply, kept
    beside every answer taken from it, every reply in the order given. As
    :func:`wanderfill.plan.trip_ask.record`: made if it is not there, another answer to an
    item that has one is refused unless ``replace``, and what is written is read back by
    :func:`~wanderfill.plan.review.read_answers` before it replaces the file.
    """
    if problem := said_problem(said):
        raise PlanInvalid(f"the owner's reply cannot be kept: {problem}")
    if not items:
        raise PlanInvalid("nothing to record: name an item that the reply answers")
    def change(raw: dict) -> None:
        for key, value in items.items():
            old = raw["answers"].get(key)
            if old is not None and old != value and not replace:
                raise PlanInvalid(f"{_r(key)} is answered already ({_r(old)}); --replace if the "
                                  "owner changed it")
            raw["answers"][key] = value
        raw["said"] = [*raw.get("said", []), {"text": said, "items": list(items)}]

    empty = {"format": ANSWERS_FORMAT, "account": manifest.account,
             "manifest": manifest.sha256, "answers": {}}
    return update_answers(path, empty, change, lambda f: read_answers(f, manifest)[0])


__all__ = ["SAYS", "questions", "record"]
