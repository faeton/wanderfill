"""``wanderfill share`` — a card of your numbers, sized for a story, a post or a link.

What it writes
--------------
PNG files on the local disk, and a small ``stats.json`` beside them saying
what the picture claims. Nothing goes to the server: every request here is a
read, and none of them carries a coordinate, so ``share=0`` does not even
arise. Publishing the picture is your decision, made afterwards, elsewhere.

What the numbers are
--------------------
The card shows the three figures people compare on NomadMania, read from the
same endpoints the site's own pages use:

- **UN** — countries visited among the 193 UN member states.
- **UN+** — the same over NomadMania's list of 196, which is the 193 plus
  Kosovo, Taiwan and Palestine. Those three are named in ``NON_UN``
  rather than inferred, and the reader checks they are present, so a
  renamed row fails loudly instead of quietly shifting the UN count.
- **NM** — regions visited out of the live catalogue (1381 at the time of
  writing; read, not hardcoded).

They are the server's own counts. This module never derives a visit from
anything, so a card can only ever show what the profile already says.
Rank, country rank and the name come from ``user/get-settings``; DARE and
KYE from their own lists. The map paints every region the profile has
marked, from the same polygon tiles the site draws.

Why the map costs a first-run wait
----------------------------------
Sixteen tiles at zoom 2 carry 1,377 of the 1,381 regions. They are cached
on disk by :class:`wanderfill.geo.tiles.TileReader`, so the first card takes
a few seconds and the next takes none. The four missing at that zoom are
too small to show at 1080 pixels wide anyway; nothing is drawn for them and
nothing is claimed for them either — the number comes from the id list, not
the polygons.

Fonts are looked for on the machine, in a preference order that favours the
faces macOS ships with, then the common Linux ones, and finally Pillow's
built-in face so the command never fails for want of a font. Pass ``--font``
to override.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .api.client import NomadMania
from .api.errors import ApiError

if TYPE_CHECKING:
    from PIL import ImageFont

# NomadMania's country list is 196 long: the 193 UN member states plus these.
# They are what makes "UN+" a plus. Named on purpose — see the module docstring.
NON_UN = frozenset({"Kosovo", "Taiwan", "Palestinian Territory"})
UN_MEMBERS = 193

AVATAR_URL = "https://nomadmania.com/img/avatars/{avatar}"
PROFILE_URL = "nomadmania.com/profile/{uid}"

# Output formats: name -> (width, height). Instagram stories and reels covers
# are 9:16; a feed post is square; "post" is the 1.91:1 that link previews
# and X/LinkedIn cards want.
SIZES: dict[str, tuple[int, int]] = {
    "story": (1080, 1920),
    "square": (1080, 1080),
    "post": (1200, 630),
}

# The world, minus Antarctica, in a projection that happens to come out 2:1
# for exactly this band. Regions wholly south of the floor are not drawn.
LAT_FLOOR, LAT_CEIL = -58.0, 84.0

# Draw at this multiple and downsample: Pillow's polygon fill is not
# anti-aliased, and at 1080 wide the coastlines otherwise look like stairs.
SUPERSAMPLE = 2


# ------------------------------------------------------------------ stats


@dataclass
class Stats:
    """What the card says. Serialised beside the images so the claim is legible."""

    uid: int
    name: str
    un: int
    un_plus: int
    un_plus_total: int
    nm: int
    nm_total: int
    rank: int | None = None
    country_rank: int | None = None
    country: str | None = None
    dare: int | None = None
    kye: int | None = None
    kye_total: int | None = None
    slow11: int | None = None
    slow31: int | None = None
    slow101: int | None = None
    yes: int | None = None
    avatar: str | None = None
    generated: str = field(default_factory=lambda: dt.date.today().isoformat())
    visited_regions: list[int] = field(default_factory=list)

    @property
    def profile_url(self) -> str:
        return PROFILE_URL.format(uid=self.uid)


def un_counts(rows: list[dict]) -> tuple[int, int, int]:
    """(UN, UN+, UN+ total) from ``slow/get-slow-app`` rows.

    Refuses rather than guesses if the three non-UN rows are not all present
    by name: a list that has been renamed or extended would otherwise put a
    wrong number in the biggest type on the card.
    """
    names = {r["country"] for r in rows}
    missing = NON_UN - names
    if missing:
        raise ApiError(
            "slow/get-slow-app",
            f"expected non-UN rows not found: {sorted(missing)}; "
            "the country list has changed and NON_UN in share.py needs a look",
            {"count": len(rows)},
        )
    if len(rows) - len(NON_UN) != UN_MEMBERS:
        raise ApiError(
            "slow/get-slow-app",
            f"{len(rows)} countries minus {len(NON_UN)} non-UN is not {UN_MEMBERS}",
            {"count": len(rows)},
        )
    visited = [r for r in rows if r.get("visited")]
    un_plus = len(visited)
    un = sum(1 for r in visited if r["country"] not in NON_UN)
    return un, un_plus, len(rows)


def collect(c: NomadMania, *, name: str | None = None) -> Stats:
    """Read everything the card needs. Reads only; no request carries a coordinate."""
    settings = c.settings()
    uid = int(settings.get("user_id") or c.account_id())
    rows = c.countries()
    un, un_plus, total = un_counts(rows)
    catalogue = c.regions()
    visited = sorted(c.visited_region_ids())

    kye_ticked = kye_total = None
    try:
        kye = c.kye()
        kye_ticked, kye_total = len(kye.get("visited", [])), int(kye.get("max") or 0) or None
    except ApiError:
        pass
    try:
        dare = len(c.visited_dare_ids())
    except ApiError:
        dare = None

    if name is None:
        name = " ".join(
            p for p in (settings.get("first_name"), settings.get("last_name")) if p
        ).strip()

    def opt_int(key: str) -> int | None:
        v = settings.get(key)
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Stats(
        uid=uid,
        name=name or "",
        un=un,
        un_plus=un_plus,
        un_plus_total=total,
        nm=len(visited),
        nm_total=len(catalogue),
        rank=opt_int("rank"),
        country_rank=opt_int("country_rank"),
        country=settings.get("country") or None,
        dare=dare,
        kye=kye_ticked,
        kye_total=kye_total,
        slow11=sum(int(r.get("slow11") or 0) for r in rows),
        slow31=sum(int(r.get("slow31") or 0) for r in rows),
        slow101=sum(int(r.get("slow101") or 0) for r in rows),
        yes=sum(int(r.get("yes_stored") or 0) for r in rows),
        avatar=settings.get("avatar") or None,
        visited_regions=visited,
    )


def fetch_avatar(avatar: str, timeout: float = 15.0) -> bytes | None:
    """The profile picture, from the public path the site serves it at. None on any failure."""
    if not avatar or "/" in avatar or ".." in avatar:
        return None
    req = urllib.request.Request(
        AVATAR_URL.format(avatar=avatar), headers={"User-Agent": "wanderfill"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if not r.headers.get("Content-Type", "").startswith("image/"):
                return None
            return r.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


# -------------------------------------------------------------------- map


def miller_y(lat: float) -> float:
    """Miller cylindrical: a Mercator that does not blow up at the poles."""
    phi = math.radians(max(-89.0, min(89.0, lat)))
    return 1.25 * math.log(math.tan(math.pi / 4 + 0.4 * phi))


def world_polygons(zoom: int = 2, cache_dir: str | Path | None = None) -> list[tuple[int, list]]:
    """Every region polygon on the planet as ``(region_id, [ring, …])``.

    The first ring of each entry is the exterior, the rest are holes, each a
    list of ``(lon, lat)``. Needs the ``geo`` extra; raises ImportError
    without it so the caller can drop the map rather than the whole card.
    """
    from .geo.tiles import TileReader

    reader = TileReader(cache_dir=cache_dir)
    out: list[tuple[int, list]] = []
    n = 2**zoom
    for x in range(n):
        for y in range(n):
            for props, geom in reader.polygons("regions", zoom, x, y):
                rid = props.get("id")
                if rid is None:
                    continue
                geoms = getattr(geom, "geoms", None) or [geom]
                for g in geoms:
                    if g.geom_type != "Polygon" or g.is_empty:
                        continue
                    rings = [list(g.exterior.coords)] + [list(i.coords) for i in g.interiors]
                    out.append((int(rid), rings))
    return out


# ----------------------------------------------------------------- themes


@dataclass(frozen=True)
class Theme:
    name: str
    bg_top: tuple[int, int, int]
    bg_bottom: tuple[int, int, int]
    glow: tuple[int, int, int]
    ink: tuple[int, int, int]
    muted: tuple[int, int, int]
    land: tuple[int, int, int, int]
    visited: tuple[int, int, int, int]
    visited_glow: tuple[int, int, int, int]
    accent: tuple[int, int, int]
    rule: tuple[int, int, int, int]


THEMES: dict[str, Theme] = {
    # Deep night: reads well over any photo a story gets layered on, and the
    # amber visited-regions glow is the thing people screenshot.
    "night": Theme(
        name="night",
        bg_top=(9, 12, 34),
        bg_bottom=(28, 24, 68),
        glow=(72, 52, 140),
        ink=(248, 246, 240),
        muted=(158, 160, 190),
        land=(255, 255, 255, 26),
        visited=(255, 179, 71, 235),
        visited_glow=(255, 150, 60, 90),
        accent=(255, 179, 71),
        rule=(255, 255, 255, 34),
    ),
    # Paper: warm off-white with terracotta, for feeds that are mostly light.
    "paper": Theme(
        name="paper",
        bg_top=(250, 247, 240),
        bg_bottom=(240, 233, 220),
        glow=(255, 236, 210),
        ink=(28, 27, 33),
        muted=(120, 116, 110),
        land=(28, 27, 33, 22),
        visited=(201, 84, 52, 235),
        visited_glow=(201, 84, 52, 70),
        accent=(201, 84, 52),
        rule=(28, 27, 33, 30),
    ),
}


# ------------------------------------------------------------------ fonts

# (path, preferred style names in order). Styles are matched against what the
# file reports, so a .ttc with several faces picks the right one.
FONT_CANDIDATES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "display": [
        ("/System/Library/Fonts/Avenir Next.ttc", ("Heavy", "Bold", "Demi Bold")),
        ("/System/Library/Fonts/HelveticaNeue.ttc", ("Bold", "Medium")),
        ("/System/Library/Fonts/Helvetica.ttc", ("Bold",)),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", ()),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", ()),
        ("C:/Windows/Fonts/segoeuib.ttf", ()),
        ("C:/Windows/Fonts/arialbd.ttf", ()),
    ],
    "text": [
        ("/System/Library/Fonts/Avenir Next.ttc", ("Medium", "Regular")),
        ("/System/Library/Fonts/HelveticaNeue.ttc", ("Regular",)),
        ("/System/Library/Fonts/Helvetica.ttc", ("Regular",)),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", ()),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", ()),
        ("C:/Windows/Fonts/segoeui.ttf", ()),
        ("C:/Windows/Fonts/arial.ttf", ()),
    ],
}


class Fonts:
    """Resolves the two faces once, then hands out sizes."""

    def __init__(self, display: str | None = None, text: str | None = None):
        self.display = self._pick("display", display)
        self.text = self._pick("text", text)

    @staticmethod
    def _pick(role: str, override: str | None) -> tuple[str, int] | None:
        from PIL import ImageFont

        if override:
            return (override, 0)
        for path, styles in FONT_CANDIDATES[role]:
            if not Path(path).exists():
                continue
            if not styles:
                return (path, 0)
            for index in range(24):
                try:
                    f = ImageFont.truetype(path, 12, index=index)
                except OSError:
                    break
                _family, style = f.getname()
                if style in styles:
                    return (path, index)
            # every face in the collection was something else; take the first
            return (path, 0)
        return None

    def get(self, role: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        from PIL import ImageFont

        spec = self.display if role == "display" else self.text
        if spec:
            try:
                return ImageFont.truetype(spec[0], size, index=spec[1])
            except OSError:
                pass
        try:
            return ImageFont.load_default(size)
        except TypeError:  # Pillow < 10.1 has no sized default
            return ImageFont.load_default()

    def describe(self) -> str:
        d = self.display[0] if self.display else "Pillow built-in"
        t = self.text[0] if self.text else "Pillow built-in"
        return f"display={d} text={t}"


# ----------------------------------------------------------------- render


def _gradient(size: tuple[int, int], top, bottom, glow, glow_at: tuple[float, float]):
    from PIL import Image, ImageFilter

    w, h = size
    # vertical gradient, one column stretched — cheaper than a per-pixel loop
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    img = col.resize((w, h))
    # a soft radial glow behind the map, so the card is not a flat slab
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    from PIL import ImageDraw

    d = ImageDraw.Draw(layer)
    cx, cy = int(w * glow_at[0]), int(h * glow_at[1])
    r = int(min(w, h) * 0.55)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*glow, 110))
    layer = layer.filter(ImageFilter.GaussianBlur(int(min(w, h) * 0.18)))
    img = img.convert("RGBA")
    img.alpha_composite(layer)
    return img


def draw_map(
    polygons: list[tuple[int, list]],
    visited: set[int],
    box: tuple[int, int, int, int],
    theme: Theme,
):
    """Paint the world into ``box`` (x0, y0, x1, y1) and return an RGBA layer that size."""
    from PIL import Image, ImageDraw, ImageFilter

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    y_top, y_bot = miller_y(LAT_CEIL), miller_y(LAT_FLOOR)

    def project(lon: float, lat: float) -> tuple[float, float]:
        px = (lon + 180.0) / 360.0 * w
        py = (y_top - miller_y(lat)) / (y_top - y_bot) * h
        return px, py

    land = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    seen = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dl, ds = ImageDraw.Draw(land), ImageDraw.Draw(seen)

    for rid, rings in polygons:
        ext = rings[0]
        if max(lat for _lon, lat in ext) < LAT_FLOOR:
            continue  # Antarctica, and a few sub-Antarctic islands
        is_visited = rid in visited
        target, colour = (ds, theme.visited) if is_visited else (dl, theme.land)
        pts = [project(lon, lat) for lon, lat in ext]
        if len(pts) < 3:
            continue
        target.polygon(pts, fill=colour)
        for hole in rings[1:]:
            hp = [project(lon, lat) for lon, lat in hole]
            if len(hp) >= 3:
                target.polygon(hp, fill=(0, 0, 0, 0))

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(land)
    if visited:
        glow = seen.copy()
        # recolour the glow layer, keep its alpha as the mask
        tint = Image.new("RGBA", (w, h), theme.visited_glow)
        tint.putalpha(glow.getchannel("A").point(lambda a: a * theme.visited_glow[3] // 255))
        tint = tint.filter(ImageFilter.GaussianBlur(max(2, w // 120)))
        out.alpha_composite(tint)
        out.alpha_composite(seen)
    return out


def _text_width(draw, text: str, font, tracking: int = 0) -> int:
    if tracking:
        return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)
    return int(draw.textlength(text, font=font))


def _draw_text(draw, xy, text: str, font, fill, *, anchor: str = "la", tracking: int = 0):
    """Draw with optional letter-spacing. Anchor is Pillow's, horizontal part honoured."""
    x, y = xy
    if not tracking:
        draw.text((x, y), text, font=font, fill=fill, anchor=anchor)
        return
    width = _text_width(draw, text, font, tracking)
    if anchor[0] == "m":
        x -= width / 2
    elif anchor[0] == "r":
        x -= width
    v = anchor[1]
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill, anchor="l" + v)
        x += draw.textlength(ch, font=font) + tracking


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", "\u2009")  # thin space: 1 381 rather than 1,381


def _circle_avatar(data: bytes, diameter: int):
    from PIL import Image, ImageDraw, ImageOps

    try:
        import io

        img = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    img = ImageOps.fit(img, (diameter, diameter), method=Image.LANCZOS)
    mask = Image.new("L", (diameter * 4, diameter * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter * 4 - 1, diameter * 4 - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.LANCZOS)
    img.putalpha(mask)
    return img


def _fit(draw, fonts: Fonts, role: str, text: str, size: int, max_w: float, tracking=0):
    """The largest font at or below ``size`` that draws ``text`` inside ``max_w``."""
    while size > 8:
        f = fonts.get(role, size)
        if _text_width(draw, text, f, tracking) <= max_w:
            return f, size
        size = int(size * 0.94)
    return fonts.get(role, size), size


def _wrap(draw, items: list[str], font, sep: str, max_w: float) -> list[str]:
    """Pack ``items`` into as few lines as fit, keeping each item whole."""
    lines: list[str] = []
    cur = ""
    for it in items:
        cand = it if not cur else cur + sep + it
        if cur and _text_width(draw, cand, font) > max_w:
            lines.append(cur)
            cur = it
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def _map_box(W: int, H: int, x0: float, x1: float, y0: float, y1: float):
    """The largest 2:1 box inside the given fractional bounds, centred in them."""
    bw, bh = (x1 - x0) * W, (y1 - y0) * H
    w = min(bw, bh * 2)
    h = w / 2
    cx, cy = (x0 + x1) / 2 * W, (y0 + y1) / 2 * H
    return (int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2))


def _extras(stats: Stats, show_rank: bool, show_extras: bool) -> list[str]:
    out: list[str] = []
    if show_rank and stats.rank:
        out.append(f"#{stats.rank} worldwide")
    if show_rank and stats.country_rank and stats.country:
        out.append(f"#{stats.country_rank} in {stats.country}")
    if show_extras:
        if stats.dare:
            out.append(f"DARE {stats.dare}")
        if stats.kye and stats.kye_total:
            out.append(f"KYE {stats.kye}/{stats.kye_total}")
    return out


def render(
    stats: Stats,
    *,
    size: str = "story",
    theme: str = "night",
    polygons: list[tuple[int, list]] | None = None,
    avatar: bytes | None = None,
    fonts: Fonts | None = None,
    handle: str | None = None,
    show_rank: bool = True,
    show_extras: bool = True,
):
    """Compose one card. Pure: takes data in, hands a PIL image back, touches nothing."""
    from PIL import Image, ImageDraw

    if size not in SIZES:
        raise ValueError(f"size must be one of {sorted(SIZES)}")
    th = THEMES[theme]
    fonts = fonts or Fonts()
    S = SUPERSAMPLE
    W, H = (v * S for v in SIZES[size])
    landscape = W > H
    img = _gradient((W, H), th.bg_top, th.bg_bottom, th.glow, (0.72 if landscape else 0.5, 0.42))
    draw = ImageDraw.Draw(img)
    visited = set(stats.visited_regions)
    cols = [
        ("UN", _fmt(stats.un), f"of {UN_MEMBERS}"),
        ("UN+", _fmt(stats.un_plus), f"of {stats.un_plus_total}"),
        ("NM", _fmt(stats.nm), f"of {_fmt(stats.nm_total)}"),
    ]
    sep = "  \u00b7  "
    when = dt.date.fromisoformat(stats.generated).strftime("%B %Y")
    margin = int(W * 0.075)

    if landscape:
        return _render_landscape(
            img, draw, stats, th, fonts, cols, sep, when, margin, polygons, visited, avatar,
            handle, show_rank, show_extras,
        )

    # Portrait: header, map, three numbers, a line of extras, footer. Everything
    # is a fraction of the height so the story and the square share decisions.
    # Stories keep inside the band Instagram does not cover with its own chrome
    # (~13% top, ~18% bottom).
    if size == "story":
        y_head, map_band, y_nums, y_extra, y_foot = 0.155, (0.225, 0.50), 0.605, 0.705, 0.815
        number_px, name_px = int(H * 0.095), int(H * 0.024)
    else:
        y_head, map_band, y_nums, y_extra, y_foot = 0.075, (0.165, 0.535), 0.695, 0.815, 0.93
        number_px, name_px = int(H * 0.125), int(H * 0.036)
    label_px, small_px = int(number_px * 0.24), int(name_px * 0.78)
    f_label, f_name, f_small = (
        fonts.get("display", label_px), fonts.get("display", name_px), fonts.get("text", small_px)
    )
    inner = W - 2 * margin

    if polygons:
        box = _map_box(W, H, 0.06, 0.94, *map_band)
        img.alpha_composite(draw_map(polygons, visited, box, th), (box[0], box[1]))
        draw = ImageDraw.Draw(img)

    _header(img, draw, stats, th, avatar, handle, f_name, f_small, margin, int(H * y_head), name_px)

    # the three numbers share one size: the widest value decides it
    col_w = inner / 3
    widest = max((v for _l, v, _o in cols), key=len)
    f_num, _ = _fit(draw, fonts, "display", widest, number_px, col_w * 0.84)
    ny = int(H * y_nums)
    for i, (label, value, of) in enumerate(cols):
        cx = margin + col_w * (i + 0.5)
        _draw_text(draw, (cx, ny - int(number_px * 0.82)), label, f_label, th.accent,
                   anchor="ms", tracking=int(label_px * 0.18))
        draw.text((cx, ny), value, font=f_num, fill=th.ink, anchor="ms")
        draw.text((cx, ny + int(small_px * 1.5)), of, font=f_small, fill=th.muted, anchor="ms")
    for i in (1, 2):
        x = margin + col_w * i
        draw.line((x, ny - int(number_px * 0.88), x, ny + int(small_px * 1.9)),
                  fill=th.rule, width=max(1, S))

    extras = _extras(stats, show_rank, show_extras)
    if extras:
        ey = int(H * y_extra)
        for j, line in enumerate(_wrap(draw, extras, f_small, sep, inner)):
            draw.text((W / 2, ey + j * int(small_px * 1.6)), line, font=f_small, fill=th.muted,
                      anchor="ms")

    fy = int(H * y_foot)
    _draw_text(draw, (W / 2, fy), "NOMADMANIA", f_label, th.muted, anchor="ms",
               tracking=int(label_px * 0.25))
    draw.text((W / 2, fy + int(small_px * 1.4)), when, font=f_small, fill=th.muted, anchor="ms")

    return img.resize(SIZES[size], Image.LANCZOS).convert("RGB")


def _header(img, draw, stats, th, avatar, handle, f_name, f_small, x, y, name_px):
    av = _circle_avatar(avatar, int(name_px * 2.4)) if avatar else None
    if av:
        img.alpha_composite(av, (x, y - av.height // 2))
        x += av.width + int(name_px * 0.6)
    if stats.name:
        draw.text((x, y), stats.name, font=f_name, fill=th.ink, anchor="lm")
        y += int(name_px * 0.95)
    draw.text((x, y), handle or stats.profile_url, font=f_small, fill=th.muted, anchor="lm")


def _render_landscape(img, draw, stats, th, fonts, cols, sep, when, margin, polygons, visited,
                      avatar, handle, show_rank, show_extras):
    """1.91:1 — text column on the left, map on the right."""
    from PIL import Image, ImageDraw

    W, H = img.size
    col_right = 0.47  # fraction of the width the text column may use
    name_px = int(H * 0.058)
    number_px = int(H * 0.13)
    label_px, small_px = int(number_px * 0.24), int(name_px * 0.78)
    f_label, f_name, f_small = (
        fonts.get("display", label_px), fonts.get("display", name_px), fonts.get("text", small_px)
    )
    col_w = W * col_right - margin

    if polygons:
        box = _map_box(W, H, 0.50, 0.97, 0.12, 0.88)
        img.alpha_composite(draw_map(polygons, visited, box, th), (box[0], box[1]))
        draw = ImageDraw.Draw(img)

    _header(img, draw, stats, th, avatar, handle, f_name, f_small, margin, int(H * 0.14), name_px)

    # numbers stacked, label and "of N" to the right of each
    widest = max((v for _l, v, _o in cols), key=len)
    f_num, num_px = _fit(draw, fonts, "display", widest, number_px, col_w * 0.62)
    step = int(H * 0.16)
    base = int(H * 0.39)
    num_w = _text_width(draw, widest, f_num)
    for i, (label, value, of) in enumerate(cols):
        yy = base + i * step
        draw.text((margin, yy), value, font=f_num, fill=th.ink, anchor="ls")
        vx = margin + num_w + int(label_px * 0.9)
        _draw_text(draw, (vx, yy - int(num_px * 0.46)), label, f_label, th.accent,
                   anchor="ls", tracking=int(label_px * 0.12))
        draw.text((vx, yy - int(num_px * 0.04)), of, font=f_small, fill=th.muted, anchor="ls")

    extras = _extras(stats, show_rank, show_extras)
    lines = _wrap(draw, extras, f_small, sep, col_w) if extras else []
    # two lines of extras at most, above a footer on the last baseline
    y = int(H * 0.80)
    for line in lines[:2]:
        draw.text((margin, y), line, font=f_small, fill=th.muted, anchor="ls")
        y += int(small_px * 1.35)
    draw.text((margin, int(H * 0.955)), f"NomadMania \u00b7 {when}", font=f_small,
              fill=th.muted, anchor="ls")

    return img.resize((W // SUPERSAMPLE, H // SUPERSAMPLE), Image.LANCZOS).convert("RGB")


def write_cards(
    stats: Stats,
    out: Path,
    *,
    sizes: list[str],
    theme: str,
    polygons: list[tuple[int, list]] | None,
    avatar: bytes | None,
    fonts: Fonts | None = None,
    handle: str | None = None,
    show_rank: bool = True,
    show_extras: bool = True,
) -> list[Path]:
    """Render each size to ``out`` and write ``stats.json`` beside them. Local files only."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size in sizes:
        img = render(
            stats, size=size, theme=theme, polygons=polygons, avatar=avatar, fonts=fonts,
            handle=handle, show_rank=show_rank, show_extras=show_extras,
        )
        path = out / f"nomadmania-{stats.uid}-{size}-{theme}.png"
        img.save(path, "PNG", optimize=True)
        written.append(path)
    claim: dict[str, Any] = asdict(stats)
    claim["visited_regions"] = len(stats.visited_regions)  # the count, not the list
    claim["files"] = [p.name for p in written]
    (out / "stats.json").write_text(json.dumps(claim, indent=1, ensure_ascii=False) + "\n")
    written.append(out / "stats.json")
    return written


def warn(msg: str) -> None:
    print(f"share: {msg}", file=sys.stderr)
