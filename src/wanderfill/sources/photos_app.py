"""The macOS Photos library as a location source.

WHY THIS MATTERS MORE THAN IT SOUNDS
------------------------------------
Server-side photo libraries only know what has been uploaded. Somebody who is
actually travelling — which is most of the time this tool is useful — has a
phone that has not synced for weeks, and a laptop whose Photos library is fully
up to date. The most recent trip, the one you most want to record, is exactly
the one the server has never seen.

So this reads the local library directly, read-only, and never touches the live
database: the file is copied first, WAL and all, and queried from the copy.

RESOLUTION
----------
Photos stores a coordinate per asset, so this yields per-photo points rather
than one point per day. That is deliberate. A day spent in one town is
compressible; a day driving from Czechia through Austria into South Tyrol is
not, and averaging it discards two of the three regions. Points are rounded to
about a hundred metres and deduplicated per day, so the volume stays sane
without collapsing movement.

macOS DETAILS
-------------
- Coordinates live in ``ZASSET.ZLATITUDE`` / ``ZLONGITUDE``. Assets with no
  location carry ``-180.0``, not NULL, so filtering on NULL alone returns the
  entire library and every point is nonsense.
- ``ZDATECREATED`` is a Core Data timestamp: seconds since 2001-01-01.
- ``ZTRASHEDSTATE`` marks assets in Recently Deleted; they are excluded.
- Reading the library may require Full Disk Access for your terminal. If the
  copy fails with a permissions error, that is what it is.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .base import DayPoint, Track

APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 in unix seconds

DEFAULT_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"

QUERY = """
SELECT date(ZDATECREATED + ?, 'unixepoch')        AS day,
       ROUND(ZLATITUDE, ?)                        AS lat,
       ROUND(ZLONGITUDE, ?)                       AS lon,
       COUNT(*)                                   AS assets
FROM ZASSET
WHERE ZLATITUDE BETWEEN -90 AND 90
  AND ZLATITUDE != 0
  AND ZTRASHEDSTATE = 0
  AND ZDATECREATED >= ?
  AND ZDATECREATED <= ?
GROUP BY day, lat, lon
ORDER BY day, lat, lon
"""


@dataclass
class PhotosAppSource:
    """Read a macOS Photos library.

    Parameters
    ----------
    library:
        Path to the ``.photoslibrary`` bundle. Defaults to the standard one.
    since / until:
        Optional date bounds. Reading only the tail of a library is the common
        case — you already imported everything up to some date.
    precision:
        Decimal places to round coordinates to. 3 is roughly 110 m.
    """

    library: Path = DEFAULT_LIBRARY
    since: dt.date | None = None
    until: dt.date | None = None
    precision: int = 3
    name: str = "photos.app"

    def _copy_database(self, into: Path) -> Path:
        """Copy the live database aside before reading it.

        The library is open in another process and has a WAL file holding the
        newest rows — precisely the rows a traveller cares about. Opening the
        original read-only would either miss them or need write access to the
        shared-memory file, so the honest move is to copy all three parts and
        read the copy.
        """
        src = self.library / "database" / "Photos.sqlite"
        if not src.exists():
            raise FileNotFoundError(f"no Photos database at {src}")
        dst = into / "Photos.sqlite"
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            side = src.with_name(src.name + suffix)
            if side.exists():
                shutil.copy2(side, dst.with_name(dst.name + suffix))
        return dst

    def points(self) -> Iterator[DayPoint]:
        lo = _apple_ts(self.since or dt.date(1970, 1, 1))
        hi = _apple_ts((self.until or dt.date.today()) + dt.timedelta(days=1))
        with tempfile.TemporaryDirectory(prefix="wanderfill-photos-") as tmp:
            db = self._copy_database(Path(tmp))
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    QUERY,
                    (APPLE_EPOCH_OFFSET, self.precision, self.precision, lo, hi),
                ).fetchall()
            finally:
                con.close()
        for day, lat, lon, assets in rows:
            yield DayPoint(
                date=dt.date.fromisoformat(day),
                lat=float(lat),
                lon=float(lon),
                source=self.name,
                weight=int(assets),
            )


def _apple_ts(day: dt.date) -> float:
    return dt.datetime.combine(day, dt.time.min).timestamp() - APPLE_EPOCH_OFFSET


def load_photos(
    since: dt.date | None = None,
    until: dt.date | None = None,
    library: Path | None = None,
) -> Track:
    """Convenience wrapper: the tail of the local Photos library as a Track."""
    src = PhotosAppSource(library=library or DEFAULT_LIBRARY, since=since, until=until)
    return Track(points=list(src.points()), origin=str(src.library))
