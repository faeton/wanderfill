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
- ``ZADDITIONALASSETATTRIBUTES.ZIMPORTEDBYBUNDLEIDENTIFIER`` is the app that put
  the photo in the library, where Photos recorded it. The two filters above do
  not catch an AirDropped photo (``com.apple.sharingd``) or one saved by a
  messenger or a glasses importer, and those carry somebody else's coordinate or
  a stale one. ``PhotoAsset.origin`` says which it is and ``origins=`` filters on
  it; by default nothing is filtered. Older libraries have no importer, or not
  even the column, and a missing importer is ``unknown``: not evidence that it
  was somebody else's, and not the owner's camera on its own.
- ``ZADDITIONALASSETATTRIBUTES.ZIMPORTEDBY`` is the capture source Photos keeps
  beside it: 1 the back camera and 2 the front one of the system Camera on a
  device signed in to this iCloud library; 3 and 6 another app; the AirDropped
  rows of one library carried 8 and 13. Until about 2024 Photos filled this
  in and left the bundle id empty: on one library 43,774 photos from 2019 to 2024
  had no bundle id and a 1 or a 2, and their camera models were one iPhone a
  year (XS Max, 11 Pro, 12 Pro, 13 Pro, 14 Pro, 15 Pro Max), the same
  succession that went on under ``com.apple.camera`` afterwards. So a photo
  with no bundle id, a 1 or a 2, and no mark of sharing (no shared-library
  state or scope, no contributor, no shared-album owner, collection share or
  saved type 4) is ``own-camera``. One that is shared, or has any other value,
  stays ``unknown``. What this cannot see: a photo of another participant that
  Apple copied into the personal library when a shared library ended, if the
  copy kept the 1 or 2 and lost every mark, would read as the owner's. It was
  1 or 2 on another person's account, under the same iCloud library.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from .base import DayPoint, Track

APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 in unix seconds

DEFAULT_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"

Origin = Literal["own-camera", "airdrop", "other", "unknown"]
ORIGINS: frozenset[str] = frozenset(get_args(Origin))


def origin_of(importer: str | None, captured: object = False) -> Origin:
    """Where a photo came from, by the bundle id that put it in the library.

    ``captured`` is whether, with no bundle id recorded, the library says a camera
    of this library's own devices took it and nothing marks it as shared (see the
    module docstring). It is read only when the bundle id is missing: a bundle id
    always wins.

    ``assets()``, ``points()`` and ``PhotoAsset.origin`` all ask this one function,
    the queries included, so the two readings cannot come to differ about a photo.
    """
    if importer is None or importer == "":
        return "own-camera" if captured is True or captured == 1 else "unknown"
    if importer == "com.apple.camera":
        return "own-camera"
    if importer == "com.apple.sharingd":
        return "airdrop"
    return "other"


def zone_fits(zone: int | None, lon: float) -> bool:
    """Whether a clock ``zone`` seconds ahead of UTC can be the local one at ``lon``.

    At most four hours from the sun's, a day more or less apart being the same: the date
    line is not at 180 for everybody. A clock that does not fit can date a photo on the day
    before or after, so it dates nothing, and neither does one that was not recorded or is
    in no zone there is. What it cannot see is a clock from just across the date line, which
    shows the right hour of the wrong day.
    """
    if zone is None or not -12 * 3600 <= zone <= 14 * 3600:
        return False
    off = (zone - lon * 240 + 43200) % 86400 - 43200  # a degree is four minutes
    return abs(off) <= 4 * 3600


PLACED = """
  a.ZLATITUDE BETWEEN -90 AND 90
  AND a.ZLONGITUDE BETWEEN -180 AND 180
  AND NOT (a.ZLATITUDE = 0 AND a.ZLONGITUDE = 0)
"""
KEPT = """
  a.ZTRASHEDSTATE = 0
  AND COALESCE(a.ZSAVEDASSETTYPE, 0) != 12
  AND NOT (a.ZKIND = 0 AND a.ZKINDSUBTYPE = 10)
"""
MINE = f"{PLACED}  AND {KEPT}"
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

LOCAL = "a.ZDATECREATED + :epoch + COALESCE(aa.ZTIMEZONEOFFSET, 0)"
"""Wall-clock time where the photo was taken.

``ZDATECREATED`` is absolute; the offset that made it a clock reading is stored
separately. Using the absolute time alone dates every evening photo in Tokyo to
the previous day and every early morning in Los Angeles to the next one, which
for a tool whose entire output is "you were in this region on this date" is not
a rounding detail. It also has to match between ``points()`` and ``assets()``,
or the importer and the dossier disagree about which day a border was crossed.
"""

WINDOW = f"""
  AND a.ZDATECREATED >= :coarse_lo
  AND a.ZDATECREATED < :coarse_hi
  AND {LOCAL} >= :lo
  AND {LOCAL} < :hi
"""
"""``since`` / ``until`` are dates on the photo's own clock, so the cut is too.

The days are labelled in the photo's wall-clock time (``LOCAL``), so the window
has to be drawn in it as well. Cutting the absolute ``ZDATECREATED`` at *this
Mac's* midnight instead trims or pads the edge days by the gap between the two
zones: with the Mac at UTC-10, ``since=29 Mar`` cut at 10:00 UTC and dropped
that morning's photos at UTC+11 whole. The
``coarse_*`` pair is the same window widened by a day each way on the absolute
column, there only so the index still narrows the scan; offsets never exceed
14 hours, so it cannot exclude anything the exact test would keep.
"""

QUERY = f"""
SELECT date({LOCAL}, 'unixepoch')                 AS day,
       ROUND(a.ZLATITUDE, :precision)                      AS lat,
       ROUND(a.ZLONGITUDE, :precision)                     AS lon,
       COUNT(*)                                   AS assets
FROM ZASSET a
LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
WHERE {MINE}
{WINDOW}{{origin}}GROUP BY day, lat, lon
ORDER BY day, lat, lon
"""
"""``{origin}`` is empty unless ``origins`` is set. ``_read`` fills it in, and the
``{importer}`` and ``{captured}`` below, once it has seen which columns the library has."""

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
       m.ZTITLE                                   AS place,
       {{importer}}                               AS importer,
       {{captured}}                               AS captured,
       a.ZDATECREATED + :epoch                    AS utc,
       aa.ZTIMEZONEOFFSET                         AS zone,
       {{camera}}                                 AS camera
FROM ZASSET a
LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
LEFT JOIN ZMOMENT m ON m.Z_PK = a.ZMOMENT
{{camera_join}}WHERE {MINE}
{WINDOW}{{origin}}ORDER BY ts
"""


DATED_QUERY = f"""
SELECT a.ZUUID, date({LOCAL}, 'unixepoch'), {{importer}}, {{captured}}
FROM ZASSET a
LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
WHERE COALESCE(({PLACED}), 0) = 0
  AND {KEPT}
{WINDOW}{{origin}}ORDER BY 2, 1
"""
"""The photos that ``MINE`` leaves out for their coordinate and for nothing else."""


@dataclass(frozen=True)
class DatedPhoto:
    """A photo with no location: which it is, its own local day, and whose.

    It is evidence of no place. But its day is a day with a photo, and that is all it is for.
    """

    uuid: str
    date: dt.date
    origin: Origin
    by_capture: bool = False
    """``own-camera`` only because of the capture source, with no app recorded."""


@dataclass(frozen=True)
class PhotoAsset:
    """One photo, kept whole because a dossier has to name files.

    ``on_disk`` is the field people trip over. Photos catalogues everything and
    stores only what fits: with iCloud optimisation on, roughly half a library
    is a thumbnail whose original lives on Apple's servers. The exhibit is still
    real — it just has to be downloaded before it can be sent to anyone.

    ``taken`` is the clock where the photo was taken, and is what dates it. It does
    not order photos taken in different zones: over the date line the later photo
    can have the earlier clock. ``taken_utc`` is the same instant without the zone,
    and is the one to sort by or to take a speed from. ``zone`` is the seconds between
    the two, and ``None`` where the library recorded none: ``taken`` is then UTC, which is
    nobody's clock. Ask :func:`zone_fits` before believing the date.
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
    importer: str | None = None
    taken_utc: dt.datetime | None = None
    zone: int | None = None
    captured: bool = False
    camera: str | None = None

    @property
    def origin(self) -> Origin:
        """``unknown`` when no importer was recorded and no camera of this library took it.

        ``captured`` is that second statement: the capture source says this library's
        own camera, and nothing says shared. ``importer`` stays ``None`` for it, so the
        two can still be told apart.
        """
        return origin_of(self.importer, self.captured)

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
    origins:
        Read only photos of these origins (``own-camera``, ``airdrop``, ``other``,
        ``unknown``), in ``points()`` and ``assets()`` alike. ``None`` reads them
        all; an empty set is refused. Not a filter to apply blind: where neither a
        bundle id nor a camera capture source was recorded the photo is ``unknown``,
        so ``{"own-camera"}`` drops such photos of the owner's too.

    ``importer_column`` says whether the library has a bundle-id column, and
    ``capture_column`` whether it has the capture source (``ZIMPORTEDBY``); both are
    ``None`` until something has been read. ``records_origin`` is either of them.
    Print it beside an empty filtered reading: ``False`` means the library cannot
    say, not that it holds nothing.
    """

    library: Path = DEFAULT_LIBRARY
    since: dt.date | None = None
    until: dt.date | None = None
    precision: int = 3
    name: str = "photos.app"
    origins: frozenset[str] | None = None
    importer_column: bool | None = field(default=None, init=False, repr=False, compare=False)
    capture_column: bool | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def records_origin(self) -> bool | None:
        """Whether the library can say of any photo whose it is: a bundle id or a capture source."""
        if self.importer_column is None and self.capture_column is None:
            return None
        return bool(self.importer_column or self.capture_column)

    def __post_init__(self) -> None:
        self._origin_names()

    def _origin_names(self) -> list[str] | None:
        """The names to keep. A typo, or none at all, would read as an empty library."""
        if self.origins is None:
            return None
        if not self.origins or not ORIGINS.issuperset(self.origins):
            raise ValueError(
                f"origins={self.origins!r}: name at least one, each of them one of "
                f"{', '.join(get_args(Origin))}; None reads them all"
            )
        return sorted(self.origins)

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

    def _read(self, sql: str, params: dict) -> list[tuple]:
        names = self._origin_names()
        with tempfile.TemporaryDirectory(prefix="wanderfill-photos-") as tmp:
            db = self._copy_database(Path(tmp))
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                columns = con.execute("PRAGMA table_info(ZADDITIONALASSETATTRIBUTES)")
                self.importer_column = any(
                    c[1] == "ZIMPORTEDBYBUNDLEIDENTIFIER" for c in columns
                )
                importer = "aa.ZIMPORTEDBYBUNDLEIDENTIFIER" if self.importer_column else "NULL"
                captured = _captured(con)
                self.capture_column = captured != "0"
                origin = ""
                if names is not None:
                    con.create_function("origin_of", 2, origin_of)
                    keep = ", ".join(f"'{n}'" for n in names)  # four known names, checked above
                    origin = f"  AND origin_of({importer}, {captured}) IN ({keep})\n"
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
                has_model = "ZEXTENDEDATTRIBUTES" in tables and any(
                    c[1] == "ZCAMERAMODEL"
                    for c in con.execute("PRAGMA table_info(ZEXTENDEDATTRIBUTES)"))
                join = "LEFT JOIN ZEXTENDEDATTRIBUTES e ON e.ZASSET = a.Z_PK\n"
                camera, camera_join = ("e.ZCAMERAMODEL", join) if has_model else ("NULL", "")
                sql = sql.format(importer=importer, captured=captured, origin=origin,
                                 camera=camera, camera_join=camera_join)
                return con.execute(sql, params).fetchall()
            finally:
                con.close()

    def _bounds(self) -> dict[str, float]:
        """Named parameters for ``WINDOW``; see its docstring for why two pairs."""
        day = dt.timedelta(days=1)
        # Unset bounds are open. `date.today()` is this Mac's today, which can be
        # a day behind the clock of the place being photographed, and a scanned
        # print can carry a date before 1970.
        lo = _wall_clock(self.since) if self.since else float("-inf")
        hi = _wall_clock(self.until + day) if self.until else float("inf")
        return {
            "epoch": APPLE_EPOCH_OFFSET,
            "lo": lo,
            "hi": hi,
            "coarse_lo": lo - APPLE_EPOCH_OFFSET - day.total_seconds(),
            "coarse_hi": hi - APPLE_EPOCH_OFFSET + day.total_seconds(),
        }

    def assets(self) -> Iterator[PhotoAsset]:
        """Every qualifying photo, one row each, with its identity intact."""
        rows = self._read(ASSET_QUERY, self._bounds())
        for (
            uuid, ts, lat, lon, directory, filename,
            original_name, capture_device, favorite, kind, place, importer, captured, utc, zone,
            camera,
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
                importer=None if importer in (None, "") else str(importer),
                captured=captured == 1,
                taken_utc=dt.datetime.fromtimestamp(float(utc), dt.timezone.utc),
                zone=int(zone) if isinstance(zone, (int, float)) else None,
                camera=str(camera) if camera not in (None, "") else None,
            )

    def without_location(self) -> Iterator[DatedPhoto]:
        """The dated photos that have no coordinate, which the other two readings leave out."""
        for uuid, day, importer, captured in self._read(DATED_QUERY, self._bounds()):
            origin = origin_of(importer, captured)
            yield DatedPhoto(str(uuid), dt.date.fromisoformat(day), origin,
                             by_capture=origin == "own-camera" and importer in (None, ""))

    def points(self) -> Iterator[DayPoint]:
        rows = self._read(QUERY, {**self._bounds(), "precision": self.precision})
        for day, lat, lon, assets in rows:
            yield DayPoint(
                date=dt.date.fromisoformat(day),
                lat=float(lat),
                lon=float(lon),
                source=self.name,
                weight=int(assets),
            )


def by_capture_source(assets) -> dict[int, dict[str, int]]:
    """The photos that are ``own-camera`` by capture source only: year -> camera model -> count.

    The capture source says a camera of a device on this iCloud library, not whose
    hand held it. A partner's or a child's phone signed in to the same account
    looks the same. So the reviews print this, and a model that is not the owner's
    in a year is theirs to see and to turn off with ``--no-capture-source``.
    """
    out: dict[int, dict[str, int]] = {}
    for a in assets:
        if a.captured and a.importer is None:
            models = out.setdefault(a.date.year, {})
            name = a.camera or "(no model recorded)"
            models[name] = models.get(name, 0) + 1
    return out


CAMERA_SOURCES = (1, 2)
"""``ZIMPORTEDBY`` values that are this library's own camera: back, front."""


def _captured(con: sqlite3.Connection) -> str:
    """SQL that is 1 where the capture source says this library's camera and nothing says shared.

    Only for a photo with no bundle id (``origin_of`` reads it there and nowhere
    else). A library with no ``ZIMPORTEDBY`` column cannot say, and gets ``0``. A
    sharing column that is absent is a kind of sharing that library does not have.
    """
    def columns(table: str) -> set[str]:
        return {c[1] for c in con.execute(f"PRAGMA table_info({table})")}

    aa, a = columns("ZADDITIONALASSETATTRIBUTES"), columns("ZASSET")
    if "ZIMPORTEDBY" not in aa:
        return "0"
    parts = [f"COALESCE(aa.ZIMPORTEDBY, 0) IN {CAMERA_SOURCES}"]
    if "ZSHARECONTRIBUTORUSERIDENTIFIER" in aa:
        parts.append("aa.ZSHARECONTRIBUTORUSERIDENTIFIER IS NULL")
    # Every mark of sharing the schema has, each read where the column exists. Any
    # one of them keeps the photo `unknown`: a false `unknown` costs a photo of the
    # owner's, a false `own-camera` claims somebody else's place.
    if "ZLIBRARYSCOPESHARESTATE" in a:
        parts.append("COALESCE(a.ZLIBRARYSCOPESHARESTATE, 0) = 0")
    if "ZLIBRARYSCOPE" in a:
        parts.append("a.ZLIBRARYSCOPE IS NULL")
    if "ZCLOUDOWNERHASHEDPERSONID" in a:
        parts.append("a.ZCLOUDOWNERHASHEDPERSONID IS NULL")
    if "ZCOLLECTIONSHARE" in a:
        parts.append("a.ZCOLLECTIONSHARE IS NULL")
    if "ZSAVEDASSETTYPE" in a:
        parts.append("COALESCE(a.ZSAVEDASSETTYPE, 0) <> 4")  # a shared album's copy
    return "(CASE WHEN " + " AND ".join(parts) + " THEN 1 ELSE 0 END)"


def _wall_clock(day: dt.date) -> float:
    """Midnight starting ``day`` on a wall clock, in the units ``LOCAL`` yields."""
    return dt.datetime.combine(day, dt.time.min, dt.timezone.utc).timestamp()


def _apple_ts(day: dt.date) -> float:
    """This Mac's midnight as an absolute Photos timestamp.

    Not used by the package any more: it compares an absolute time against a
    machine-local midnight, which is the bug ``WINDOW`` fixes. Kept for the
    one-off scripts in ``workspace/`` that still import it.
    """
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
