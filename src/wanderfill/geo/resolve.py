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

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .tiles import TileReader, deg2tile, haversine_km, neighbourhood

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
        """Pull a region id out of a ``location/get-region`` reply, if it is real.

        The reply shape depends on the ``share`` parameter, which is a nasty
        little trap. Called with ``share=0`` — the only way this package ever
        calls it — the server answers::

            {"result": "OK", "region": 181}

        Called without it, as a browser session does, it answers::

            {"result": "OK", "nm": {"id": 181}, "dare": false, "country": "IS"}

        Reading only the second shape silently resolves *nothing*: every
        coordinate comes back unplaced, which looks like a data problem rather
        than a parsing one. Both shapes are handled here.

        Note that ``share=0`` also costs you the ``dare`` and ``country``
        fields. DARE membership has to come from the tiles instead.
        """
        rid = raw.get("region")
        if rid is None:
            nm = raw.get("nm") or {}
            rid = nm.get("id") if isinstance(nm, dict) else None
        if rid is None:
            return None
        rid = int(rid)
        # -1 is the server's way of saying "open water"
        return rid if rid in self.catalogue else None

    def from_tiles(self, lat: float, lon: float, ring: int = 1) -> Resolution:
        """Point-in-polygon against the live tiles, then nearest within tolerance.

        ``ring=0`` looks only in the tile the point falls in. That is wrong for
        coastal and border points — the nearest polygon can live next door —
        but it is right for the overwhelming majority, and it is nine times
        cheaper. :meth:`resolve_many` uses it as a first pass and widens only
        for the points it misses.
        """
        from shapely.geometry import Point

        pt = Point(lon, lat)
        best_id, best_dist = None, float("inf")
        for x, y in neighbourhood(lat, lon, self.zoom, ring=ring):
            for props, geom in self.reader.shapes(self.layer, self.zoom, x, y):
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

    def resolve_many(
        self,
        coords: Iterable[tuple[float, float]],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[tuple[float, float], Resolution]:
        """Resolve a whole library's worth of coordinates from tiles alone.

        Two things make this affordable. Points are visited in tile order, so
        the reader's memo holds the tile being asked about instead of thrashing.
        And the first pass looks only inside the point's own tile, widening to
        the ring only for the minority that miss — mostly coasts, borders and
        flights.

        The geocoder is not used here at all. It is one call per coordinate
        against somebody else's server, and about one in seven of its answers
        is an id the catalogue no longer has. The tiles are the current
        polygons, they are on a CDN, and after the first run they are on disk.
        """
        todo = sorted(set(coords), key=lambda c: deg2tile(c[0], c[1], self.zoom))
        out: dict[tuple[float, float], Resolution] = {}
        for i, (lat, lon) in enumerate(todo, 1):
            r = self.from_tiles(lat, lon, ring=0)
            if r.method != "tile_interior":
                r = self.from_tiles(lat, lon, ring=1)
            out[(lat, lon)] = r
            if on_progress:
                on_progress(i, len(todo))
        return out


# --------------------------------------------------------------- the cache


def region_extent_km(
    reader: TileReader,
    lat: float,
    lon: float,
    region: int,
    *,
    zoom: int = 8,
    layer: str = "regions",
) -> float:
    """Roughly how far across the region containing this point is.

    Needed to tell "this region is too small to walk a kilometre across" from
    "this person did not leave their hotel". Those look identical in a photo
    library — a handful of coordinates a few hundred metres apart — and only one
    of them is evidence of a visit.

    Measured as the diagonal of the polygon's bounding box in the tiles around
    a known interior point, at a coarse zoom. It is an over-estimate for a
    region shaped like an L or an archipelago, which is the safe direction: a
    region wrongly judged *large* keeps the strict kilometre rule.

    Returns 0.0 when the polygon cannot be found, which callers should read as
    "do not grant the small-region exemption".
    """
    x, y = deg2tile(lat, lon, zoom)
    west = south = float("inf")
    east = north = float("-inf")
    found = False
    for tx, ty in neighbourhood(lat, lon, zoom):
        for props, geom in reader.shapes(layer, zoom, tx, ty):
            rid = props.get("id") or props.get("region_id")
            if rid is None or int(rid) != region:
                continue
            found = True
            w, s, e, n = geom.bounds
            west, south = min(west, w), min(south, s)
            east, north = max(east, e), max(north, n)
    if not found:
        return 0.0
    del x, y
    return haversine_km(south, west, north, east)


def small_regions(
    reader: TileReader,
    candidates: dict[int, tuple[float, float]],
    *,
    threshold_km: float,
    zoom: int = 8,
) -> set[int]:
    """Which of these regions are genuinely too small to cross on foot."""
    return {
        rid
        for rid, (lat, lon) in candidates.items()
        if 0.0 < region_extent_km(reader, lat, lon, rid, zoom=zoom) <= threshold_km
    }


def coord_map_params(path: str | Path) -> dict:
    """The resolver settings a cache was built with, empty if it never said."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw.get("params", {}) if isinstance(raw, dict) else {}


def load_coord_map(path: str | Path) -> dict[tuple[float, float], Resolution]:
    """Read a coordinate cache, in either shape it can be written in.

    The flat ``{"lat,lon": id}`` form is what ``sweep`` has always read. The
    richer form also keeps *how* each point was resolved, which is the
    difference between "you were in this region" and "you were within 12 km of
    it and the tile said so" — a distinction a verifier will care about and a
    scoring engine will not.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    points = raw.get("points", raw) if isinstance(raw, dict) else {}
    out: dict[tuple[float, float], Resolution] = {}
    for key, value in points.items():
        try:
            lat_s, lon_s = key.split(",")
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            continue
        if isinstance(value, dict):
            rid = value.get("region")
            out[(lat, lon)] = Resolution(
                lat, lon,
                int(rid) if rid is not None else None,
                value.get("method", "geocoder"),
                float(value.get("distance_deg", 0.0)),
            )
        else:
            # -1 is the geocoder's word for open water, and it is truthy.
            # Reading it as a region id claims travel to region minus one.
            rid = int(value) if isinstance(value, (int, float, str)) and str(value).strip() else 0
            rid = rid if rid > 0 else None
            out[(lat, lon)] = Resolution(lat, lon, rid, "geocoder" if rid else "unresolved")
    return out


def valid_only(
    resolutions: dict[tuple[float, float], Resolution], catalogue: dict[int, dict]
) -> dict[tuple[float, float], Resolution]:
    """Drop region ids the live catalogue no longer has.

    A cache outlives the catalogue it was built against — regions get split and
    renumbered, which is the same drift that makes 14% of the geocoder's answers
    dead on arrival. Entries naming an id nobody has any more are *dropped*
    rather than blanked, so the caller resolves them again instead of inheriting
    a hole. Points legitimately resolved to nothing — ocean, mid-flight — are
    kept, because that answer does not go stale.
    """
    return {
        key: r
        for key, r in resolutions.items()
        if r.region is None or r.region in catalogue
    }


def save_coord_map(
    path: str | Path,
    resolutions: dict[tuple[float, float], Resolution],
    params: dict | None = None,
) -> Path:
    """Write the rich form, recording what produced it.

    ``params`` carries the zoom and tolerance the entries were resolved at. A
    cache is only as meaningful as the settings behind it: a point resolved at
    z10 with a 0.30° tolerance is not the same answer as one resolved at z11
    with none, and silently mixing the two makes a run unreproducible.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": 1,
        "params": params or {},
        "points": {
            f"{lat},{lon}": {
                "region": r.region,
                "method": r.method,
                "distance_deg": round(r.distance_deg, 6),
            }
            for (lat, lon), r in sorted(resolutions.items())
        },
    }
    p.write_text(json.dumps(body, indent=0, ensure_ascii=False), encoding="utf-8")
    return p
