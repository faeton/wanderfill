"""The one form in which a list is hashed, saved, loaded and shown to the owner.

An answer is bound to the hash of the list it answers, so there is one spelling of every
value, and one reading of it: what does not fit is refused, never guessed at. The checks
raise ``ValueError`` saying what is wrong, for the caller to say where; the file readers
raise :class:`PlanInvalid`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..api.errors import PlanInvalid
from .model import _r, parse_when

_ESCAPES = {ord(c): f"\\u{ord(c):04x}" for c in "<>&\u2028\u2029"}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def sha256_of(value: object) -> str:
    """The hash of a value's canonical JSON."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def escape_data(value: object) -> str:
    """Canonical JSON to put in a script element of a page.

    The five characters occur only inside strings of it, so escaping them changes no value,
    and nothing in a name can end the element it is in.
    """
    return canonical(value).translate(_ESCAPES)


def fields(row: object, wanted: Sequence[str], what: str, optional: Sequence[str] = ()) -> None:
    if not isinstance(row, Mapping):
        raise ValueError(f"{what} must be an object, not {_r(row)}")
    for name in wanted:
        if name not in row:
            raise ValueError(f"{what} has no {name!r}")
    for name in row:
        if name not in wanted and name not in optional:
            raise ValueError(f"{what} has a field {_r(name)}, which this version does not know")


def whole(value: object, what: str, *, low: int | None = None) -> int:
    # Within what JavaScript counts exactly: the page reads ids and writes them back.
    if type(value) is not int or abs(value) >= 2**53 or (low is not None and value < low):
        least = "" if low is None else f" of {low} or more"
        raise ValueError(f"{what} must be a whole number{least}, not {_r(value)}")
    return value


def number(value: object, what: str, *, low: float = 0.0, high: float = math.inf) -> float:
    if type(value) in (int, float):
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if math.isfinite(number) and low <= number <= high:
            return number + 0.0
    raise ValueError(f"{what} must be a number from {low:g} to {high:g}, not {_r(value)}")


def text(value: object, what: str) -> str:
    """``value`` with what UTF-8 cannot hold replaced: half a surrogate pair is no reason
    to refuse a list, and would be one to the file and to the hash."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, not {_r(value)}")
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def date(value: object, what: str) -> str:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value.isoformat()
    try:
        day = parse_when(value)
    except ValueError as exc:
        raise ValueError(f"{what}: {exc}") from None
    if not isinstance(day, dt.date):
        raise ValueError(f"{what}: {_r(value)} is not YYYY-MM-DD")
    return day.isoformat()


def read_json(path: str | Path, what: str) -> Any:
    return json_of(read_bytes(path, what), path, what)


def read_bytes(path: str | Path, what: str, *, limit: int | None = None) -> bytes:
    try:
        with Path(path).open("rb") as f:
            data = f.read() if limit is None else f.read(limit + 1)
    except OSError as exc:
        raise PlanInvalid(f"{path}: cannot be read ({exc.strerror or exc})") from None
    if limit is not None and len(data) > limit:
        raise PlanInvalid(f"{path}: over {limit // 1_000_000} MB, which is no {what}")
    return data


def json_of(data: bytes, path: str | Path, what: str) -> Any:
    def no_number(text: str) -> None:
        raise ValueError(f"{text} is not a number")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=no_repeats,
                          parse_constant=no_number)
    except (ValueError, RecursionError) as exc:  # not JSON, not UTF-8, nested without end
        raise PlanInvalid(f"{path}: not a readable {what} ({exc})") from None


def no_repeats(pairs: list[tuple[str, Any]]) -> dict:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:  # json keeps the last, and the owner may have read the first
            raise ValueError(f"an object gives {_r(key)} twice")
        out[key] = value
    return out
