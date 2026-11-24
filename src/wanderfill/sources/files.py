"""File-based sources: CSV, GPX/KML/GeoJSON, and EXIF folders.

GPX comes first on purpose. It covers Garmin, OsmAnd, Gaia, Komoot, Wikiloc,
GPSLogger and Strava *exports* without touching anybody's API — the highest
value per line of code in this package.
"""

from __future__ import annotations

import csv as _csv
import datetime as dt
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .base import DayPoint, Track


def _as_date(value: str) -> dt.date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(value[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass
class CsvSource:
    """A CSV with at least a date and a coordinate.

    Column names are guessed from the usual spellings, so an export from a photo
    library, a spreadsheet somebody maintained by hand, and the output of
    another importer all work without configuration. This is also the escape
    hatch: when the resolver gets something wrong, a person edits a CSV.
    """

    path: Path
    name: str = "csv"
    skipped: int = field(default=0, init=False)

    DATE = ("date", "day", "timestamp", "time", "when")
    LAT = ("lat", "latitude", "y")
    LON = ("lon", "lng", "long", "longitude", "x")
    PLACE = ("city", "place", "town", "locality", "name")
    COUNTRY = ("country", "nation")

    def _pick(self, header: Iterable[str], options: Iterable[str]) -> str | None:
        lower = {h.lower().strip(): h for h in header}
        for want in options:
            if want in lower:
                return lower[want]
        return None

    def points(self) -> Iterator[DayPoint]:
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            reader = _csv.DictReader(fh)
            header = reader.fieldnames or []
            c_date = self._pick(header, self.DATE)
            c_lat = self._pick(header, self.LAT)
            c_lon = self._pick(header, self.LON)
            c_place = self._pick(header, self.PLACE)
            c_country = self._pick(header, self.COUNTRY)
            if not (c_date and c_lat and c_lon):
                raise ValueError(f"{self.path}: need date, lat and lon columns; saw {header}")
            self.skipped = 0
            for row in reader:
                date = _as_date(row[c_date])
                try:
                    lat, lon = float(row[c_lat]), float(row[c_lon])
                except (TypeError, ValueError):
                    date = None
                if date is None:
                    self.skipped += any((row[c] or "").strip() for c in (c_date, c_lat, c_lon))
                    continue
                yield DayPoint(
                    date=date, lat=lat, lon=lon,
                    place=(row.get(c_place) or "") if c_place else "",
                    country=(row.get(c_country) or "") if c_country else "",
                    source=f"{self.name}:{self.path.name}",
                )


@dataclass
class GpxSource:
    """GPX 1.0/1.1 track points and waypoints, collapsed to one point per day.

    A day's worth of GPS is hundreds of fixes in the same place as far as a
    region is concerned, so the median-ish middle fix is kept and the rest
    become the point's weight.
    """

    path: Path
    name: str = "gpx"

    def points(self) -> Iterator[DayPoint]:
        tree = ET.parse(self.path)
        by_day: dict[dt.date, list[tuple[float, float]]] = defaultdict(list)
        for el in tree.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag not in ("trkpt", "wpt", "rtept"):
                continue
            try:
                lat, lon = float(el.attrib["lat"]), float(el.attrib["lon"])
            except (KeyError, ValueError):
                continue
            when = None
            for child in el:
                if child.tag.rsplit("}", 1)[-1] == "time" and child.text:
                    when = _as_date(child.text)
            if when is None:
                continue
            by_day[when].append((lat, lon))
        for day, fixes in sorted(by_day.items()):
            fixes.sort()
            lat, lon = fixes[len(fixes) // 2]
            yield DayPoint(date=day, lat=lat, lon=lon,
                           source=f"{self.name}:{self.path.name}", weight=len(fixes))


@dataclass
class GeoJsonSource:
    """GeoJSON features with a date property. Also reads Google's newer exports."""

    path: Path
    name: str = "geojson"
    date_keys = ("date", "time", "timestamp", "startTime", "endTime")

    def points(self) -> Iterator[DayPoint]:
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        feats = data.get("features", data if isinstance(data, list) else [])
        for f in feats:
            props = f.get("properties", f) or {}
            date = None
            for k in self.date_keys:
                if props.get(k):
                    date = _as_date(str(props[k]))
                    if date:
                        break
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates")
            if not date or not coords:
                continue
            while isinstance(coords[0], (list, tuple)):
                coords = coords[0]
            yield DayPoint(date=date, lat=float(coords[1]), lon=float(coords[0]),
                           place=str(props.get("name", "")),
                           source=f"{self.name}:{Path(self.path).name}")


# -- every place of every day --------------------------------------------------------

_MAX_DAYS = 1000  # of one feature; one that is over more is a mistake in the file


def load_every(path: str | Path) -> Track:
    """A track with every distinct place of every day in it, and not one for a day.

    :func:`load` keeps the middle fix of a day, which is right for a stay and loses the one
    fix somewhere else. Here a place that is lost is a day that seems to say nothing. What
    cannot be read as a day and a place is counted in ``skipped``: a row, a feature, a fix.
    A GPX waypoint or route point with no time is a bookmark or a plan, and is neither.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return load(p)
    if suffix in (".gpx", ".xml"):
        found, skipped = _gpx_every(p)
    elif suffix in (".json", ".geojson"):
        found, skipped = _geojson_every(p)
    else:
        raise ValueError(f"no parser for {p.name}; supported: .csv .gpx .json .geojson")
    points = [DayPoint(day, lat, lon, source=p.name) for day, lat, lon in dict.fromkeys(found)]
    return Track(points=points, origin=str(p), skipped=skipped)


_Fix = tuple[dt.date, float, float]


def _gpx_every(path: Path) -> tuple[list[_Fix], int]:
    out: list[_Fix] = []
    skipped = 0
    for el in ET.parse(path).iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("trkpt", "wpt", "rtept"):
            continue
        times = [c.text or "" for c in el if c.tag.rsplit("}", 1)[-1] == "time"]
        if not times and tag != "trkpt":
            continue
        day = _as_date(times[-1]) if times else None
        try:
            lat, lon = float(el.attrib["lat"]), float(el.attrib["lon"])
        except (KeyError, ValueError):
            day = None
        if day is None:
            skipped += 1
        else:
            out.append((day, lat, lon))
    return out, skipped


def _geojson_every(path: Path) -> tuple[list[_Fix], int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    features = data if isinstance(data, list) else data["features"]
    out: list[_Fix] = []
    skipped = 0
    for feature in features:
        days, good = _feature_days(feature)
        places: list[tuple[float, float]] = []
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        good &= _positions(geometry.get("coordinates") if isinstance(geometry, dict) else None,
                           places)
        skipped += not good
        out += [(day, lat, lon) for day in days for lat, lon in places]
    return out, skipped


def _feature_days(feature: object) -> tuple[list[dt.date], bool]:
    """Every day of a feature, from the first that it gives to the last, and whether all of
    what it gives could be read."""
    props = feature.get("properties", feature) if isinstance(feature, dict) else None
    if not isinstance(props, dict):
        return [], False
    given = [_as_date(str(props[k])) for k in GeoJsonSource.date_keys if props.get(k)]
    days = [d for d in given if d]
    start, end = (_as_date(str(props.get(k) or "")) for k in ("startTime", "endTime"))
    good = bool(days) and len(days) == len(given) and not (start and end and start > end)
    if days and (span := (max(days) - min(days)).days) <= _MAX_DAYS:
        return [min(days) + dt.timedelta(n) for n in range(span + 1)], good
    return days, False


def _positions(value: object, out: list[tuple[float, float]]) -> bool:
    """Every position in a GeoJSON coordinate array, however deep, and whether all were one."""
    if not isinstance(value, list) or not value:
        return False
    if any(isinstance(v, list) for v in value):
        return all([_positions(v, out) for v in value])  # all of them, and not up to the first
    if len(value) < 2 or any(type(v) not in (int, float) for v in value[:2]):
        return False
    out.append((float(value[1]), float(value[0])))
    return True


def load(path: str | Path) -> Track:
    """Pick a parser by extension and read the file."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        src = CsvSource(p)
    elif suffix in (".gpx", ".xml"):
        src = GpxSource(p)
    elif suffix in (".json", ".geojson"):
        src = GeoJsonSource(p)
    else:
        raise ValueError(f"no parser for {p.name}; supported: .csv .gpx .json .geojson")
    points = list(src.points())
    return Track(points=points, origin=str(p), skipped=getattr(src, "skipped", 0))
