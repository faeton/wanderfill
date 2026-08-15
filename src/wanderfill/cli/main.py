"""wanderfill command line.

The command set encodes the safety model rather than merely documenting it:

    whoami     confirm the token works and which account it belongs to
    export     read-only dump of the whole profile (also the rollback reference)
    resolve    coordinates -> region ids, with the stale-id repair
    evidence   index the photo library against what you claim — for verification
    sweep      show what each trip-segmentation setting produces
    plan       write a plan file. Never touches the server's state.
    show       print a plan for a human to read
    apply      execute a plan file, and only a plan file

There is deliberately no command that computes and writes in one step.

``evidence`` is the odd one out and deliberately so: it is the only command
whose output is aimed at the user rather than at the server. Everything else
here answers "what should my profile say"; that one answers "and could I prove
it if they asked".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import __version__
from ..api.client import NomadMania
from ..api.errors import WanderfillError
from ..evidence import (
    MIN_SERIAL_DAYS,
    MIN_SERIAL_SHOTS,
    MIN_SERIAL_SPOTS,
    MIN_SERIAL_SPREAD_KM,
    MIN_SINGLE_DAY_HOURS,
    MIN_SMALL_SPOTS,
    SMALL_REGION_KM,
)

DEFAULT_WORKDIR = Path.home() / ".local" / "share" / "wanderfill"


def _client(args) -> NomadMania:
    token = os.environ.get("NM_TOKEN") or ""
    if not token:
        sys.exit(
            "No token. Open nomadmania.com while logged in, run\n"
            "    localStorage.getItem('token')\n"
            "in the browser console, and put the value in NM_TOKEN.\n"
            "Never paste it into an issue, a gist, or a chat with a model."
        )
    return NomadMania(token)


# ---------------------------------------------------------------- commands


def cmd_whoami(args) -> int:
    c = _client(args)
    print(json.dumps(c.status(), indent=1, ensure_ascii=False)[:2000])
    return 0


def cmd_export(args) -> int:
    c = _client(args)
    print("reading catalogue and profile state...", file=sys.stderr)
    regions = c.regions()
    visited = sorted(c.visited_region_ids())
    data = {
        "account": c.account_id(),
        "regions_in_catalogue": len(regions),
        "visited_regions": visited,
        "visited_dare": sorted(c.visited_dare_ids()),
        "countries": c.countries(),
    }
    if args.full:
        print(f"reading visits for {len(visited)} regions...", file=sys.stderr)
        data["visits"] = {
            str(r): [
                {
                    "id": v.id,
                    "from": v.date_from.isoformat() if v.date_from else None,
                    "to": v.date_to.isoformat() if v.date_to else None,
                    "quality": v.quality, "trip_id": v.trip_id,
                }
                for v in c.visits_for_region(r)
            ]
            for r in visited
        }
    out = Path(args.out) if args.out else None
    text = json.dumps(data, indent=1, ensure_ascii=False, default=str)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out} ({len(text):,} bytes)")
    else:
        print(text)
    return 0


def cmd_sweep(args) -> int:
    """Show the trip-segmentation trade-off instead of choosing it silently."""
    from ..geo.resolve import load_coord_map
    from ..plan.segment import sweep
    from ..sources.files import load

    track = load(args.track)
    day_regions: dict = {}
    # Reads both cache shapes: the flat "lat,lon" -> id map, and the richer one
    # `evidence` writes. Two commands that disagree about a cache file is a
    # silently empty result, not an error.
    mapping = load_coord_map(args.regions)
    for p in track.points:
        hit = mapping.get(p.key)
        if hit and hit.region:
            day_regions.setdefault(p.date, set()).add(hit.region)
    rows = sweep(day_regions)
    print(f"{'gap':>4} {'cap':>6} {'trips':>7} {'longest':>8} {'max regions':>12}")
    for r in rows:
        print(
            f"{r['gap_days']:>4} {r['cap_days']:>6} {r['trips']:>7} "
            f"{r['longest_days']:>8} {r['most_regions']:>12}"
        )
    print(
        "\nNo setting here is correct. A cap of 30 days is arbitrary and deliberate:\n"
        "without one, a continuously nomadic year becomes a single 465-day 'trip'."
    )
    return 0


def cmd_evidence(args) -> int:
    """Index the photo library against the profile, for verification day.

    Read-only on both sides: it reads NomadMania to learn what you claim, reads
    the Photos library to learn what you can prove, and writes only local
    files. There is no ``--apply`` because there is nothing to apply.
    """
    import datetime as dt

    from ..evidence import (
        Rules,
        Shot,
        build,
        collect_documents,
        render_applescript,
        render_json,
        render_markdown,
        slug,
    )
    from ..geo.resolve import (
        RegionResolver,
        coord_map_params,
        load_coord_map,
        save_coord_map,
        small_regions,
        valid_only,
    )
    from ..sources.photos_app import DEFAULT_LIBRARY, PhotosAppSource

    def when(text: str | None):
        return dt.date.fromisoformat(text) if text else None

    if args.exhibits < 1:
        sys.exit("--exhibits must be at least 1: a dossier with no exhibits proves nothing")

    rules = Rules(
        shots=args.min_shots,
        days=args.min_days,
        spread_km=args.min_spread_km,
        spots=args.min_spots,
        single_day_hours=args.min_hours,
        small_spots=args.min_small_spots,
    )

    c = _client(args)
    account = c.account_id()
    print(f"account {account}: reading catalogue and claimed regions...", file=sys.stderr)
    catalogue = c.regions()
    claimed = c.visited_region_ids()

    library = Path(args.library) if args.library else DEFAULT_LIBRARY
    src = PhotosAppSource(library=library, since=when(args.since), until=when(args.until))
    print(f"reading {library}...", file=sys.stderr)
    assets = list(src.assets())
    if not assets:
        sys.exit("no geotagged photos in that library or date range")
    coords = sorted({a.key for a in assets})
    print(f"{len(assets):,} photos, {len(coords):,} distinct coordinates", file=sys.stderr)

    cache_path = Path(args.cache)
    settings = {"zoom": args.zoom, "tolerance_deg": args.tolerance}
    known = load_coord_map(cache_path) if cache_path.exists() else {}
    was = coord_map_params(cache_path) if cache_path.exists() else {}
    if known and was and was != settings:
        print(
            f"cache was built at {was} and you asked for {settings} — "
            "re-resolving everything rather than mixing two answers",
            file=sys.stderr,
        )
        known = {}
    # A cache outlives catalogues: regions get split and renumbered.
    known = valid_only(known, catalogue)
    todo = [c_ for c_ in coords if c_ not in known]
    if todo and not args.no_resolve:
        resolver = RegionResolver(catalogue, zoom=args.zoom, tolerance_deg=args.tolerance)
        print(
            f"resolving {len(todo):,} coordinates against the live polygons "
            f"(tiles are cached; the first run is the slow one)...",
            file=sys.stderr,
        )

        def tick(i, total):
            if i % 200 == 0 or i == total:
                print(f"  {i:,}/{total:,}", file=sys.stderr)

        known.update(resolver.resolve_many(todo, on_progress=tick))
        save_coord_map(cache_path, known, settings)
        print(f"cache: {cache_path}", file=sys.stderr)
    elif todo:
        print(f"{len(todo):,} coordinates are unresolved and --no-resolve is set", file=sys.stderr)

    shots_by_region: dict[int, list[Shot]] = {}
    unplaced = 0
    for a in assets:
        r = known.get(a.key)
        if r is None or r.region is None:
            unplaced += 1
            continue
        shots_by_region.setdefault(r.region, []).append(
            Shot(
                asset=a,
                region=r.region,
                near_boundary=not r.confident,
                distance_deg=r.distance_deg,
            )
        )

    visits: dict = {}
    if args.check_dates:
        targets = sorted(r for r in shots_by_region if r in claimed)
        print(f"reading visit dates for {len(targets)} regions...", file=sys.stderr)
        for rid in targets:
            # An undated visit says nothing about when, so it cannot disagree
            # with a photo. It is still a visit, and still counts.
            visits[rid] = [
                (v.date_from, v.date_to)
                for v in c.visits_for_region(rid)
                if v.date_from and v.date_to
            ]

    # Which regions are too small to walk a kilometre across? Only regions whose
    # photos are already clustered can possibly need the answer, so this measures
    # a handful of polygons rather than 1,381 of them.
    from ..evidence import diameter_km, gradable

    candidates = {}
    for rid, shots in shots_by_region.items():
        inside = gradable(shots)
        if inside and diameter_km(inside) < rules.spread_km:
            candidates[rid] = (inside[0].asset.lat, inside[0].asset.lon)
    small: set[int] = set()
    if candidates and not args.no_resolve:
        print(f"measuring {len(candidates)} tightly-clustered regions...", file=sys.stderr)
        small = small_regions(
            RegionResolver(catalogue, zoom=args.zoom).reader,
            candidates,
            threshold_km=args.small_region_km,
        )
        if small:
            print(
                f"  {len(small)} are genuinely too small to cross: "
                + ", ".join(str(catalogue.get(r, {}).get("name", r))[:28] for r in sorted(small)),
                file=sys.stderr,
            )

    documents = collect_documents(args.documents) if args.documents else {}
    if documents:
        print(
            f"indexed {sum(len(v) for v in documents.values())} filed documents "
            f"across {len(documents)} regions",
            file=sys.stderr,
        )

    dossier = build(
        shots_by_region,
        catalogue,
        claimed,
        account=account,
        library=library,
        exhibits=args.exhibits,
        visits=visits,
        documents=documents,
        rules=rules,
        small=small,
        assets_seen=len(assets),
        assets_unplaced=unplaced,
        params={
            "since": args.since, "until": args.until, "zoom": args.zoom,
            "tolerance_deg": args.tolerance, "exhibits": args.exhibits,
            "checked_dates": bool(args.check_dates),
        },
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.md").write_text(render_markdown(dossier), encoding="utf-8")
    (out / "evidence.json").write_text(
        json.dumps(render_json(dossier), indent=1, ensure_ascii=False), encoding="utf-8"
    )
    script = out / "export-from-photos.applescript"
    script.write_text(render_applescript(dossier, out / "files"), encoding="utf-8")

    copied = 0
    if args.copy:
        import shutil

        for r in dossier.regions:
            if not r.exhibits:
                continue
            folder = out / "files" / f"{r.region:04d}-{slug(r.name)}"
            for s in r.exhibits:
                origin = s.asset.original_path(library)
                if not origin.exists():
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                dest = folder / f"{s.date.isoformat()}-{s.asset.uuid[:8]}{origin.suffix}"
                if not dest.exists():
                    shutil.copy2(origin, dest)
                copied += 1

    counts = dossier.counts()
    proj = dossier.projection()
    missing = sum(
        1 for r in dossier.regions for s in r.exhibits if not s.asset.on_disk(library)
    )
    print(f"\nwrote {out}/index.md and evidence.json")
    if copied:
        print(f"copied {copied} originals into {out}/files")
    print(
        f"\nclaimed regions {len(dossier.claimed)}: "
        f"{counts['strong']} strong, {counts['serial']} serial, "
        f"{counts['thin']} thin, {counts['none']} with nothing"
    )
    print(
        f"if they drew {proj['sample']} regions: ~{proj['even']} photo-backed on an "
        f"even draw, ~{proj['weighted']} if it leans to your thinnest half "
        f"(they ask for {proj['needed']})"
    )
    if proj["excluded_date_conflict"]:
        print(
            f"  {proj['excluded_date_conflict']} region(s) excluded from both: "
            "their photos contradict the dates on the profile"
        )
    if dossier.unclaimed_with_evidence:
        print(
            f"{len(dossier.unclaimed_with_evidence)} regions have photos but no visit "
            "on the profile — see the dossier, then plan them properly"
        )
    if missing:
        print(
            f"{missing} exhibits are in iCloud rather than on this disk. "
            f"Run:\n  osascript {script}"
        )
    print(
        "\nThis wrote nothing to NomadMania. Nothing here is a claim — it is an "
        "index of what you can already back up."
    )
    return 0


def cmd_show(args) -> int:
    from ..plan.model import Plan

    plan = Plan.load(args.plan)
    print(f"plan for account {plan.account}, made {plan.created}")
    print(f"tool {plan.tool}, schema {plan.schema}")
    if plan.params:
        print(f"params: {json.dumps(plan.params)}")
    print("\ncounts by kind and bucket:")
    for k, v in sorted(plan.counts().items()):
        print(f"  {k:28} {v:>5}")
    print(f"\n{len(plan.to_apply())} op(s) are in the apply bucket and would execute.")
    if args.verbose:
        for op in plan.ops[: args.limit]:
            print(f"  [{op.bucket:6}] {op.kind:13} {op.label[:60]:62} {op.method}")
    return 0


def cmd_apply(args) -> int:
    from ..plan.apply import apply_plan
    from ..plan.model import Plan

    c = _client(args)
    plan = Plan.load(args.plan)
    ops = plan.to_apply()
    print(f"plan {args.plan}: {len(ops)} op(s) in the apply bucket")
    if not args.confirm:
        print(
            "\nDry run. Nothing was sent.\n"
            "Re-run with --confirm to execute. Ops left in the 'review' bucket are\n"
            "never executed — edit the plan file to promote them."
        )
        return 0

    def progress(i, total, op, ok):
        mark = "ok " if ok else "ERR"
        print(f"  [{i:4}/{total}] {mark} {op.kind:13} {op.label[:56]}")

    report = apply_plan(
        c, plan,
        workdir=Path(args.workdir),
        confirm=True,
        max_writes=args.max_writes,
        on_progress=progress,
    )
    print(
        f"\nattempted {report.attempted}, succeeded {report.succeeded}, "
        f"failed {report.failed}, skipped {report.skipped}"
    )
    for e in report.errors[:20]:
        print(f"  error: {e}")
    return 1 if report.failed else 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wanderfill",
        description="Import your location history into NomadMania. Unofficial, unaffiliated.",
        epilog="Token comes from NM_TOKEN. This tool never deletes anything.",
    )
    p.add_argument("--version", action="version", version=f"wanderfill {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="check the token and show the account").set_defaults(
        func=cmd_whoami
    )

    e = sub.add_parser("export", help="read-only dump of the profile")
    e.add_argument("--out", help="write here instead of stdout")
    e.add_argument("--full", action="store_true", help="include every visit record (slow)")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("sweep", help="show trip-segmentation options")
    s.add_argument("track", help="CSV/GPX/GeoJSON location history")
    s.add_argument("regions", help='JSON map of "lat,lon" -> region id')
    s.set_defaults(func=cmd_sweep)

    ev = sub.add_parser(
        "evidence",
        help="index your photo library against your claimed regions",
        description=(
            "Build a proof dossier: for every region you claim, which photos in "
            "your library can back it up, and which regions have nothing. Reads "
            "both sides, writes only local files."
        ),
    )
    ev.add_argument("--out", default="evidence", help="directory for the dossier")
    ev.add_argument("--library", help="path to a .photoslibrary (default: the standard one)")
    ev.add_argument("--since", help="YYYY-MM-DD, earliest photo to consider")
    ev.add_argument("--until", help="YYYY-MM-DD, latest photo to consider")
    ev.add_argument(
        "--cache",
        default=str(DEFAULT_WORKDIR / "coords.json"),
        help="coordinate -> region cache, reused between runs and by sweep",
    )
    ev.add_argument("--exhibits", type=int, default=5, help="photos to shortlist per region")
    # The grading thresholds. Defaults are a guess about how a person travels,
    # and a wrong guess is why a region you remember well reads as thin.
    ev.add_argument("--min-shots", type=int, default=MIN_SERIAL_SHOTS,
                    help="photos needed inside the region")
    ev.add_argument("--min-days", type=int, default=MIN_SERIAL_DAYS,
                    help="days needed, on the plain route")
    ev.add_argument("--min-spread-km", type=float, default=MIN_SERIAL_SPREAD_KM,
                    help="km between the two furthest photos")
    ev.add_argument("--min-spots", type=int, default=MIN_SERIAL_SPOTS,
                    help="distinct ~1 km spots, for a region seen in one day")
    ev.add_argument("--min-hours", type=float, default=MIN_SINGLE_DAY_HOURS,
                    help="hours a single day's photos must span, so a drive-through "
                         "does not read as a visit")
    ev.add_argument("--min-small-spots", type=int, default=MIN_SMALL_SPOTS,
                    help="distinct ~110 m spots for a region too small to cross "
                         "(Vatican, Monaco, Gibraltar)")
    ev.add_argument("--small-region-km", type=float, default=SMALL_REGION_KM,
                    help="a region measuring less than this across may use the "
                         "small-region route instead of the km threshold")
    ev.add_argument("--zoom", type=int, default=10, help="tile zoom for the polygon lookup")
    ev.add_argument("--tolerance", type=float, default=0.30, help="nearest-polygon tolerance, deg")
    ev.add_argument(
        "--documents",
        help=(
            "directory of non-photo evidence, one folder per region named "
            "<region-id>-anything: bills, stamps, tickets, diary scans"
        ),
    )
    ev.add_argument(
        "--check-dates",
        action="store_true",
        help="also read your visit dates and flag regions whose photos fall outside them",
    )
    ev.add_argument(
        "--copy",
        action="store_true",
        help="copy the shortlisted originals that are on this disk into <out>/files",
    )
    ev.add_argument(
        "--no-resolve",
        action="store_true",
        help="use only the coordinate cache; do not fetch tiles",
    )
    ev.set_defaults(func=cmd_evidence)

    sh = sub.add_parser("show", help="print a plan file")
    sh.add_argument("plan")
    sh.add_argument("-v", "--verbose", action="store_true")
    sh.add_argument("--limit", type=int, default=40)
    sh.set_defaults(func=cmd_show)

    a = sub.add_parser("apply", help="execute a plan file")
    a.add_argument("plan")
    a.add_argument("--confirm", action="store_true", help="actually write")
    a.add_argument("--max-writes", type=int, default=200)
    a.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    a.set_defaults(func=cmd_apply)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except WanderfillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
