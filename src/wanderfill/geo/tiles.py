"""Reading NomadMania's vector tiles.

The tile server is the most useful of the three surfaces and the least obvious.
It is public, unauthenticated, and it is the only place that carries:

  * the CURRENT region polygons — the reverse geocoder runs on an older set
  * every DARE area's polygon
  * every SERIES object's coordinate, tagged with its own id and its series id

Two things bite immediately. The bodies are gzipped with no Content-Encoding
worth trusting, so you sniff the magic bytes. And the series layer is *not*
decimated by zoom — a test box over Rome returns the same objects at z9 as at
z15 — so harvesting can be done at a coarse zoom for a fraction of the requests.

Nothing from this module is redistributed. The polygons belong to NomadMania;
we fetch them at runtime and cache them in the user's own directory.
"""

from __future__ import annotations

import gzip
import math
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from ..api.transport import USER_AGENT

BASE = "https://maps.nomadmania.travel/tiles"
LAYERS = ("countries", "regions", "dare", "series2")


def deg2tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2.0 * n)
    return x, y


def tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """west, south, east, north in degrees."""
    n = 2**z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def neighbourhood(lat: float, lon: float, z: int, ring: int = 1) -> set[tuple[int, int]]:
    """The tile containing a point, plus a ring around it.

    Needed because a point near a tile edge can be closest to a polygon that
    lives in the neighbouring tile — the failure mode that loses coastal and
    border coordinates.
    """
    x, y = deg2tile(lat, lon, z)
    return {(x + dx, y + dy) for dx in range(-ring, ring + 1) for dy in range(-ring, ring + 1)}


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    R = 6371.0088
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class SeriesPoint:
    """One object of one series, geocoded."""

    id: int
    series_id: int
    name: str
    lat: float
    lon: float


class TileReader:
    """Fetches and decodes tiles, with an on-disk cache."""

    def __init__(self, cache_dir=None, timeout: float = 30.0):
        from pathlib import Path

        default = Path.home() / ".cache" / "wanderfill" / "tiles"
        self.cache = Path(cache_dir) if cache_dir else default
        self.cache.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def raw(self, layer: str, z: int, x: int, y: int) -> bytes:
        path = self.cache / layer / str(z) / str(x) / f"{y}.pbf"
        if path.exists():
            return path.read_bytes()
        url = f"{BASE}/{layer}/{z}/{x}/{y}.pbf"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except Exception:
            data = b""  # an absent tile is ordinary: most of the planet is empty
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def decode(self, layer: str, z: int, x: int, y: int) -> dict:
        data = self.raw(layer, z, x, y)
        if not data:
            return {}
        import mapbox_vector_tile

        try:
            return mapbox_vector_tile.decode(data)
        except Exception:
            return {}

    # -- what callers actually want ---------------------------------------

    def polygons(self, layer: str, z: int, x: int, y: int) -> Iterator[tuple[dict, object]]:
        """Yield (properties, shapely geometry) for one tile, in lon/lat degrees."""
        from shapely.affinity import affine_transform
        from shapely.geometry import shape

        tile = self.decode(layer, z, x, y)
        if not tile:
            return
        west, south, east, north = tile_bounds(x, y, z)
        for lyr in tile.values():
            extent = lyr.get("extent", 4096)
            sx = (east - west) / extent
            sy = (north - south) / extent
            matrix = [sx, 0, 0, sy, west, south]
            for f in lyr.get("features", []):
                geom = shape(f["geometry"])
                yield f.get("properties", {}), affine_transform(geom, matrix)

    def series_points(self, z: int, x: int, y: int) -> Iterator[SeriesPoint]:
        """Yield every series object in one tile.

        One harvest geocodes all 69 series at once, which is why this is the
        only practical way to match a track against World Heritage Sites, Know
        Your Earth, TCC and the rest.
        """
        tile = self.decode("series2", z, x, y)
        if not tile:
            return
        west, south, east, north = tile_bounds(x, y, z)
        for lyr in tile.values():
            extent = lyr.get("extent", 4096)
            for f in lyr.get("features", []):
                g = f.get("geometry", {})
                if g.get("type") != "Point":
                    continue
                px, py = g["coordinates"][0], g["coordinates"][1]
                p = f.get("properties", {})
                if "id" not in p:
                    continue
                yield SeriesPoint(
                    id=int(p["id"]),
                    series_id=int(p.get("series_id", 0)),
                    name=str(p.get("name", "")),
                    lat=south + (north - south) * (py / extent),
                    lon=west + (east - west) * (px / extent),
                )
