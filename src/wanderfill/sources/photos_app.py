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

TWO READINGS OF THE SAME LIBRARY
--------------------------------
``points()`` aggregates to day-and-coordinate, which is what the importer wants:
it does not care which photo put you in Potosí, only that something did.
``assets()`` keeps every photo separate, with its identity, because the other
job this library can do is *evidence* — see :mod:`wanderfill.evidence`. A
dossier that says "you were in Potosí" is worthless; one that names five files
is the thing a verifier accepts.

macOS DETAILS
-------------
- Coordinates live in ``ZASSET.ZLATITUDE`` / ``ZLONGITUDE``. Assets with no
  location carry ``-180.0``, not NULL, so filtering on NULL alone returns the
  entire library and every point is nonsense.
- ``ZDATECREATED`` is a Core Data timestamp: seconds since 2001-01-01.
- ``ZTRASHEDSTATE`` marks assets in Recently Deleted; they are excluded.
- ``ZSAVEDASSETTYPE = 12`` is a *syndicated* asset — a photo somebody sent you
  in Messages, which the library shows but you did not take. It carries their
  coordinates. Excluded from both readings: it is evidence that a friend was
  somewhere, and claiming it is exactly the kind of thing verification exists
  to catch.
- ``ZKIND = 0, ZKINDSUBTYPE = 10`` is a screenshot. A screenshot of a map is
  not a place you have been, so those go too.
- ``ZADDITIONALASSETATTRIBUTES.ZCAMERACAPTUREDEVICE = 1`` means the front
  camera. That is as close as a database can get to "selfie", and NomadMania
  counts a selfie as stronger proof than an ordinary photo.
- ``ZMOMENT.ZTITLE`` is Photos' own reverse-geocoded place name ("Potosí").
  Free, offline, and far better than a coordinate for labelling an exhibit.
- Originals live at ``<library>/originals/<bucket>/<filename>`` — but only if
  they are downloaded. With iCloud "Optimize Mac Storage" a large fraction of
  the library is thumbnails only, so every consumer has to cope with a file
  that is catalogued and absent.
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

MINE = """
  a.ZLATITUDE BETWEEN -90 AND 90
  AND a.ZLONGITUDE BETWEEN -180 AND 180
  AND NOT (a.ZLATITUDE = 0 AND a.ZLONGITUDE = 0)
  AND a.ZTRASHEDSTATE = 0
  AND COALESCE(a.ZSAVEDASSETTYPE, 0) != 12
  AND NOT (a.ZKIND = 0 AND a.ZKINDSUBTYPE = 10)
"""
"""The photos that are actually yours and actually places.

Kept in one string because ``points()`` and ``assets()`` disagreeing about
which photos count would mean the dossier proves a region the importer never
claimed, or the reverse. Columns are qualified because ``assets()`` joins two
tables that also have a ``ZTRASHEDSTATE``.

Note what is *not* filtered here: latitude zero on its own. The missing-location
sentinel is ``-180``, and the equator runs through Ecuador, Kenya, Indonesia and
five other countries somebody may well be claiming. Only Null Island — exactly
(0, 0), the classic bad fix — is dropped.
"""

LOCAL = "a.ZDATECREATED + ? + COALESCE(aa.ZTIMEZONEOFFSET, 0)"
"""Wall-clock time where the photo was taken.

``ZDATECREATED`` is absolute; the offset that made it a clock reading is stored
separately. Using the absolute time alone dates every evening photo in Tokyo to
the previous day and every early morning in Los Angeles to the next one, which
for a tool whose entire output is "you were in this region on this date" is not
a rounding detail. It also has to match between ``points()`` and ``assets()``,
or the importer and the dossier disagree about which day a border was crossed.
"""

QUERY = f"""
SELECT date({LOCAL}, 'unixepoch')                 AS day,
       ROUND(a.ZLATITUDE, ?)                      AS lat,
       ROUND(a.ZLONGITUDE, ?)                     AS lon,
       COUNT(*)                                   AS assets
FROM ZASSET a
LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
WHERE {MINE}
  AND a.ZDATECREATED >= ?
  AND a.ZDATECREATED < ?
GROUP BY day, lat, lon
ORDER BY day, lat, lon
"""

ASSET_QUERY = f"""
SELECT a.ZUUID                                    AS uuid,
       {LOCAL}                                    AS ts,
       a.ZLATITUDE                                AS lat,
       a.ZLONGITUDE                               AS lon,
       a.ZDIRECTORY                               AS directory,
       a.ZFILENAME                                AS filename,
       aa.ZORIGINALFILENAME                       AS original_name,
       COALESCE(aa.ZCAMERACAPTUREDEVICE, 0)       AS capture_device,
       COALESCE(a.ZFAVORITE, 0)                   AS favorite,
       a.ZKIND                                    AS kind,
       m.ZTITLE                                   AS place
FROM ZASSET a
LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
LEFT JOIN ZMOMENT m ON m.Z_PK = a.ZMOMENT
WHERE {MINE}
  AND a.ZDATECREATED >= ?
  AND a.ZDATECREATED < ?
ORDER BY ts
"""


@dataclass(frozen=True)
class PhotoAsset:
    """One photo, kept whole because a dossier has to name files.

    ``on_disk`` is the field people trip over. Photos catalogues everything and
    stores only what fits: with iCloud optimisation on, roughly half a library
    is a thumbnail whose original lives on Apple's servers. The exhibit is still
    real — it just has to be downloaded before it can be sent to anyone.
    """

    uuid: str
    taken: dt.datetime
    lat: float
    lon: float
    directory: str
    filename: str
    original_name: str = ""
    place: str = ""
    selfie: bool = False
    favorite: bool = False
    video: bool = False

    @property
    def date(self) -> dt.date:
        return self.taken.date()

    @property
    def key(self) -> tuple[float, float]:
        """The same rounding ``DayPoint`` uses, so one resolve serves both."""
        return (round(self.lat, 3), round(self.lon, 3))

    def original_path(self, library: str | Path) -> Path:
        return Path(library) / "originals" / self.directory / self.filename

    def on_disk(self, library: str | Path) -> bool:
        return self.original_path(library).exists()

    @property
    def applescript_id(self) -> str:
        """What Photos' own scripting dictionary calls this asset.

        Verified against a live library: ``id of media item 1`` answers
        ``<UUID>/L0/001``, and a lookup by the bare UUID fails. This is the
        difference between an export script that runs and one that raises -1728
        sixty times.
        """
        return f"{self.uuid}/L0/001"


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

    def _read(self, sql: str, params: tuple) -> list[tuple]:
        with tempfile.TemporaryDirectory(prefix="wanderfill-photos-") as tmp:
            db = self._copy_database(Path(tmp))
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()

    def _bounds(self) -> tuple[float, float]:
        lo = _apple_ts(self.since or dt.date(1970, 1, 1))
        hi = _apple_ts((self.until or dt.date.today()) + dt.timedelta(days=1))
        return lo, hi

    def assets(self) -> Iterator[PhotoAsset]:
        """Every qualifying photo, one row each, with its identity intact."""
        lo, hi = self._bounds()
        rows = self._read(ASSET_QUERY, (APPLE_EPOCH_OFFSET, lo, hi))
        for (
            uuid, ts, lat, lon, directory, filename,
            original_name, capture_device, favorite, kind, place,
        ) in rows:
            yield PhotoAsset(
                uuid=str(uuid),
                # `ts` is ALREADY wall-clock where the photo was taken: the query
                # adds ZTIMEZONEOFFSET to make it so. Plain fromtimestamp() would
                # then apply *this machine's* offset on top, shifting every photo
                # by the local UTC offset and moving near-midnight ones to the
                # wrong day. `points()` gets this right via SQLite's UTC-based
                # date(), so the bug also made the two readings of one library
                # disagree about which day a photo belongs to.
                taken=dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).replace(tzinfo=None),
                lat=float(lat),
                lon=float(lon),
                directory=str(directory or ""),
                filename=str(filename or ""),
                original_name=str(original_name or filename or ""),
                place=str(place or ""),
                selfie=int(capture_device or 0) == 1,
                favorite=bool(favorite),
                video=int(kind or 0) == 1,
            )

    def points(self) -> Iterator[DayPoint]:
        lo, hi = self._bounds()
        rows = self._read(QUERY, (APPLE_EPOCH_OFFSET, self.precision, self.precision, lo, hi))
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


def load_photo_assets(
    since: dt.date | None = None,
    until: dt.date | None = None,
    library: Path | None = None,
) -> list[PhotoAsset]:
    """Every photo as its own record — the input to :mod:`wanderfill.evidence`."""
    src = PhotosAppSource(library=library or DEFAULT_LIBRARY, since=since, until=until)
    return list(src.assets())
