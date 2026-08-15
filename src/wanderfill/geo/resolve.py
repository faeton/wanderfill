"""Turning coordinates into region ids you can actually write.

The reverse geocoder is convenient and partly wrong. Measured against the live
catalogue on a 4,868-day track, roughly **14% of the ids it returns no longer
exist** — Cyprus had been split, Portugal renumbered, Hungary and Austria
reorganised. Writing those ids does nothing useful and reading them back gives a
score that is quietly too low.

So resolution happens in three stages, and every result says which stage
produced it. A caller can then treat a tile-interior hit and a
nearest-polygon-within-tolerance hit differently, which matters when the output
is a plan somebody has to approve.

Points that survive all three stages unresolved are left unresolved. They are
flights and open ocean. Snapping them to the nearest land would invent travel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .tiles import TileReader, neighbourhood

Method = Literal["geocoder", "tile_interior", "tile_nearest", "unresolved"]


@dataclass(frozen=True)
class Resolution:
    lat: float
    lon: float
    region: int | None
    method: Method
    distance_deg: float = 0.0

    @property
    def confident(self) -> bool:
        """True when the point is inside a polygon rather than merely near one."""
        return self.method in ("geocoder", "tile_interior")


class RegionResolver:
    """Resolve coordinates against the live region set.

    Parameters
    ----------
    catalogue:
        ``client.regions()`` — the authority on which ids exist. A geocoder
        answer absent from here is discarded, not trusted.
    zoom:
        Tile zoom for the polygon lookup. z10-11 is the sweet spot: fine enough
        that polygons are not badly simplified, coarse enough that a continent
        does not cost ten thousand requests.
    tolerance_deg:
        How far outside every polygon a point may fall and still be attributed
        to the nearest one. Coastal photographs land in the sea often enough
        that zero tolerance loses real travel; too much tolerance invents it.
    """

    def __init__(
        self,
        catalogue: dict[int, dict],
        *,
        reader: TileReader | None = None,
        zoom: int = 10,
        tolerance_deg: float = 0.30,
        layer: str = "regions",
    ):
        self.catalogue = catalogue
        self.reader = reader or TileReader()
        self.zoom = zoom
        self.tolerance = tolerance_deg
        self.layer = layer

    # ------------------------------------------------------------------

    def from_geocoder(self, raw: dict) -> int | None:
        """Pull a region id out of a ``location/get-region`` reply, if it is real."""
        nm = raw.get("nm") or {}
        rid = nm.get("id") if isinstance(nm, dict) else None
        if rid is None:
            return None
        rid = int(rid)
        return rid if rid in self.catalogue else None

    def from_tiles(self, lat: float, lon: float) -> Resolution:
        """Point-in-polygon against the live tiles, then nearest within tolerance."""
        from shapely.geometry import Point

        pt = Point(lon, lat)
        best_id, best_dist = None, float("inf")
        for x, y in neighbourhood(lat, lon, self.zoom):
            for props, geom in self.reader.polygons(self.layer, self.zoom, x, y):
                rid = props.get("id") or props.get("region_id")
                if rid is None:
                    continue
                rid = int(rid)
                if rid not in self.catalogue:
                    continue
                if geom.covers(pt):
                    return Resolution(lat, lon, rid, "tile_interior", 0.0)
                d = geom.distance(pt)
                if d < best_dist:
                    best_id, best_dist = rid, d
        if best_id is not None and best_dist <= self.tolerance:
            return Resolution(lat, lon, best_id, "tile_nearest", best_dist)
        return Resolution(lat, lon, None, "unresolved", best_dist if best_id else 0.0)

    def resolve(self, lat: float, lon: float, geocoded: dict | None = None) -> Resolution:
        """Full pipeline: trust the geocoder only when its answer still exists."""
        if geocoded is not None:
            rid = self.from_geocoder(geocoded)
            if rid is not None:
                return Resolution(lat, lon, rid, "geocoder", 0.0)
        return self.from_tiles(lat, lon)
