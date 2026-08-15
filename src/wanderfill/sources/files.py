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
from dataclasses import dataclass
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
            for row in reader:
                date = _as_date(row[c_date])
                if date is None:
                    continue
                try:
                    lat, lon = float(row[c_lat]), float(row[c_lon])
                except (TypeError, ValueError):
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
    return Track(points=list(src.points()), origin=str(p))
