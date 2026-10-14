"""A disk cache for the read-only commands, so a slow link is not paid twice.

What it holds, and why each is safe to hold:

  catalogue   ``regions/get-regions-list-2``, 173 KB plain. NomadMania changes
              its region list a few times a year, and a stale id is already
              caught downstream (``geo.resolve.valid_only``). Kept 30 days.

  visits      every visit record per region, per account. These change whenever
              anybody edits the profile, so an entry is reused only while the
              region's live summary from ``region_years()`` — visit count, first
              and last year, best quality — still matches the one it was saved
              under, and for at most 7 days. A summary costs 29 requests for the
              whole profile; the records it vouches for cost one per region.

What it will never hold is anything a write is planned or checked against.
``plan``, ``apply``, ``check`` and ``delete`` build their client without a
cache, so a drift check or a deletion read-back always sees the server. That
is enforced where clients are made (``cli.main._client``), not by convention
inside this module.

The visit files are a dated record of where somebody was; they are written
``0600`` into the per-user work directory and never into the repository.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

CATALOGUE_MAX_AGE = dt.timedelta(days=30)
VISITS_MAX_AGE = dt.timedelta(days=7)

# The fields of a ``region_years()`` row that move when a region's visits do.
SUMMARY_FIELDS = (
    "no_of_visits", "first_visited_in_year", "last_visited_in_year", "best_visit_quality",
)


def summary_of(row: dict | None) -> list | None:
    """A region's visit fingerprint, or None when the row cannot vouch for anything."""
    if not row or not all(k in row for k in SUMMARY_FIELDS):
        return None
    return [row[k] for k in SUMMARY_FIELDS]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp(text: str) -> dt.datetime:
    """A saved timestamp, always timezone-aware so it compares with ``_now()``."""
    t = dt.datetime.fromisoformat(text)
    if t.tzinfo is None:
        raise ValueError("naive timestamp")
    return t


def age_words(saved: dt.datetime, now: dt.datetime | None = None) -> str:
    s = ((now or _now()) - saved).total_seconds()
    if s < 3600:
        return f"{int(s // 60)} min old"
    if s < 2 * 86400:
        return f"{int(s // 3600)} h old"
    return f"{int(s // 86400)} days old"


class ReadCache:
    """JSON files under one directory. ``refresh=True`` reads nothing, writes everything."""

    def __init__(self, root: str | Path, *, refresh: bool = False):
        self.root = Path(root)
        self.refresh = refresh
        self.notes: list[str] = []  # one line per decision, for the CLI to print
        self.served_catalogue = False

    # -- plumbing ----------------------------------------------------------

    def _load(self, name: str) -> dict | None:
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, name: str, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{name}.")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self.root / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- the catalogue -----------------------------------------------------

    def catalogue(self) -> dict | None:
        if self.refresh:
            return None
        got = self._load("catalogue.json")
        try:
            saved = _stamp(got["saved"])
            data = got["data"]
            if not isinstance(data, dict) or not data:
                return None
        except (KeyError, TypeError, ValueError):
            return None  # a damaged file is a miss, not a crash
        if _now() - saved > CATALOGUE_MAX_AGE:
            return None
        self.served_catalogue = True
        self.notes.append(f"region catalogue from cache ({age_words(saved)}; --refresh re-reads)")
        return data

    def tile_only_ids(self) -> set[int]:
        """Region ids seen in tiles that a *live* catalogue also lacked.

        Remembered so they do not mark every later cached catalogue as stale.
        """
        got = self._load("tile-only.json")
        try:
            return {int(i) for i in (got or {}).get("ids", [])}
        except (TypeError, ValueError):
            return set()

    def note_tile_only(self, ids: set[int]) -> None:
        if ids:
            self._save("tile-only.json", {"ids": sorted(self.tile_only_ids() | set(ids))})

    def drop_catalogue(self) -> None:
        """Forget the catalogue: the live tiles have shown it is out of date."""
        (self.root / "catalogue.json").unlink(missing_ok=True)
        self.served_catalogue = False
        self.notes.append("region catalogue was stale (the tiles carry newer regions); re-read")

    def put_catalogue(self, data: dict) -> None:
        self._save("catalogue.json", {"saved": _now().isoformat(), "data": data})

    # -- visit records -----------------------------------------------------

    def _visits_name(self, account: int) -> str:
        return f"visits-{int(account)}.json"

    def visits(self, account: int, summaries: dict[int, list | None]) -> dict[int, list[dict]]:
        """Raw visit rows for every region whose summary still matches. Others are absent."""
        if self.refresh:
            return {}
        now = _now()
        out: dict[int, list[dict]] = {}
        for rid, entry in self._visit_entries(account).items():
            want = summaries.get(rid)
            if want is None or entry["summary"] != want:
                continue
            if now - entry["saved"] > VISITS_MAX_AGE:
                continue
            out[rid] = entry["rows"]
        return out

    def _visit_entries(self, account: int) -> dict[int, dict]:
        """The account's file, keeping only entries whose every field parses."""
        got = self._load(self._visits_name(account))
        out: dict[int, dict] = {}
        if not isinstance(got, dict):
            return out
        for key, entry in got.items():
            try:
                rows = entry["rows"]
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    continue
                out[int(key)] = {"summary": entry["summary"], "saved": _stamp(entry["saved"]),
                                 "rows": rows}
            except (KeyError, TypeError, ValueError):
                continue  # damaged entry: read the region live
        return out

    def put_visits(
        self, account: int, rows: dict[int, list[dict]], summaries: dict[int, list | None]
    ) -> None:
        """Merge freshly read regions into the account's file. Unsummarised ones are skipped."""
        # Rebuilt from the entries that parse, so one damaged entry is dropped
        # rather than carried forward or allowed to stop the write.
        got = {
            str(rid): {**e, "saved": e["saved"].isoformat()}
            for rid, e in self._visit_entries(account).items()
        }
        stamp = _now().isoformat()
        for rid, raw in rows.items():
            if summaries.get(rid) is not None:
                got[str(rid)] = {"summary": summaries[rid], "saved": stamp, "rows": raw}
        self._save(self._visits_name(account), got)


def describe(cache: ReadCache | None) -> list[str]:
    return list(cache.notes) if cache else []


__all__: list[Any] = [
    "CATALOGUE_MAX_AGE", "VISITS_MAX_AGE", "ReadCache", "age_words", "describe", "summary_of",
]
