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
    share      draw your UN / UN+ / NM numbers as an image. Reads only.
    history    where was I, and when: visa forms, ESTA, tax days, absences. Reads only.

There is deliberately no command that computes and writes in one step.

``evidence`` is the odd one out and deliberately so: it is the only command
whose output is aimed at the user rather than at the server. Everything else
here answers "what should my profile say"; that one answers "and could I prove
it if they asked".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from .. import __version__
from ..api.client import NomadMania, YearOnly
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
# Kept here rather than imported so ``--help`` does not load the history module.
TAX_YEAR_CHOICES = (
    "calendar", "uk", "au", "nz", "in", "za", "us", "es", "pt", "de", "fr", "it", "nl",
)


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file. Deliberately tiny, deliberately not a dependency.

    Handles the two shapes people actually write — ``NM_TOKEN=x`` and
    ``export NM_TOKEN='x'`` — plus comments and blank lines. It does not do
    interpolation, multi-line values or ``${OTHER}`` expansion: this file holds
    one secret, and a parser with features is a parser with surprises.

    Never raises. A malformed line is skipped, because failing to start over a
    stray character in a file the user cannot see the contents of is a bad
    half-hour.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Unreadable, or saved in some other encoding by an editor that meant
        # well. Either way it is not a token, and it is not worth dying over.
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        key, sep, value = s.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def find_token() -> tuple[str, str]:
    """The token and where it came from, searched in order of explicitness.

    The environment wins, because an explicitly exported variable is somebody
    saying "this one, now" and a file on disk is not. After that: ``.env`` in
    the working directory, then in the repository root, so running from a
    subdirectory still works.

    The source is returned so the CLI can *say* which one it used. A token
    coming from a file the user forgot they wrote is exactly how the wrong
    account gets written to, and the plan protocol's account check is the last
    line of defence, not the first.
    """
    env = os.environ.get("NM_TOKEN")
    if env:
        return env, "NM_TOKEN in the environment"
    here = Path.cwd()
    for folder in (here, *here.parents):
        candidate = folder / ".env"
        if candidate.exists():
            token = read_dotenv(candidate).get("NM_TOKEN", "")
            if token:
                return token, str(candidate)
        if (folder / ".git").exists():
            break  # do not climb out of the repository looking for secrets
    return "", ""


def _client(args) -> NomadMania:
    token, source = find_token()
    if not token:
        sys.exit(
            "No token. Open nomadmania.com while logged in, run\n"
            "    localStorage.getItem('token')\n"
            "in the browser console, and put the value in NM_TOKEN — either\n"
            "exported in your shell, or in a .env file next to this repo:\n"
            "    printf \"NM_TOKEN='...'\\n\" > .env && chmod 600 .env\n"
            "Never paste it into an issue, a gist, or a chat with a model."
        )
    if source != "NM_TOKEN in the environment" and not getattr(args, "quiet", False):
        print(f"token from {source}", file=sys.stderr)
    return NomadMania(token)


def _resolve_coords(args, catalogue: dict, coords) -> dict:
    """Coordinates -> :class:`Resolution`, through the shared cache.

    ``evidence`` and ``history`` both resolve a library against the live
    polygons, and both must read and write the cache the same way — a cache
    built at one zoom and read at another mixes two answers silently.
    Uses ``args.cache``, ``args.zoom``, ``args.tolerance`` and
    ``args.no_resolve``.
    """
    from ..geo.resolve import (
        RegionResolver,
        coord_map_params,
        load_coord_map,
        save_coord_map,
        valid_only,
    )

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
    return known


# ---------------------------------------------------------------- commands


def cmd_whoami(args) -> int:
    """Which account does this token belong to?

    Answered with ``status-quick`` rather than ``status``, because ``status``
    replies ``{result, status, admin}`` and no account id at all. The id is the
    whole question: now that a token can come from a file, "the token works" is
    not the same as "the token is the account you think it is".
    """
    c = _client(args)
    print(json.dumps(c.status_quick(), indent=1, ensure_ascii=False)[:2000])
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


def _visit_window(v) -> tuple[dt.date, dt.date] | None:
    """A visit's date range as real dates, widening a bare year to the whole year.

    An undated visit says nothing about when, so it cannot disagree with a photo
    and returns None. A year-only visit says a great deal — it is just imprecise,
    and the honest window is 1 January to 31 December.
    """
    def lo(d):
        return dt.date(d.year, 1, 1) if isinstance(d, YearOnly) else d

    def hi(d):
        return dt.date(d.year, 12, 31) if isinstance(d, YearOnly) else d

    if not v.date_from or not v.date_to:
        return None
    return (lo(v.date_from), hi(v.date_to))


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
        small_regions,
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

    known = _resolve_coords(args, catalogue, coords)

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
            # A year-only visit says "some time in 2013", which as a window is
            # the whole year. Comparing a YearOnly to a date raises TypeError,
            # and dropping it instead would silently report a date conflict for
            # every photo in a year the profile does record.
            visits[rid] = [
                w for w in (_visit_window(v) for v in c.visits_for_region(rid)) if w
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


def cmd_history(args) -> int:
    """Where was I, and when — for a visa form, ESTA, a tax year, or plain recall.

    Read-only on NomadMania: the country list, the region catalogue and flags,
    and every visit record (standalone and trip-owned). Reads the Photos
    library and/or a track file locally. Writes ``history-<query>.md``,
    ``.csv`` and ``.json`` into ``--out`` and nothing else, anywhere.

    The output is a dated movement history. It stays on this machine; the
    person copies from it into the form themselves.
    """
    from .. import history as hi
    from ..countries import (
        UNMAPPED,
        GroupError,
        UnmappedCountries,
        build_country_map,
        groups_used,
    )
    from ..countries import parse_groups as groups

    today = dt.date.today()
    q = args.query
    try:
        include = groups(args.countries) if getattr(args, "countries", None) else None
        exclude = groups(args.exclude) if getattr(args, "exclude", None) else set()
        home = groups(args.home) if getattr(args, "home", None) else None
        since = hi.parse_when(getattr(args, "since", None), today)
        until = hi.parse_when(getattr(args, "until", None), today) or today
        if q == "where":
            since, until = hi.parse_period(args.period, today)
    except (GroupError, ValueError) as exc:
        sys.exit(f"history: {exc}")
    if q == "check" and not include:
        sys.exit("history check: --countries is required, e.g. --countries vwp-restricted")
    if since and since > until:
        sys.exit(f"history: the window starts ({since}) after it ends ({until})")

    c = _client(args)
    print("reading the country list and region catalogue...", file=sys.stderr)
    catalogue = c.regions()
    flags = c.region_years()
    rows = {rid: {**catalogue.get(rid, {}), **flags.get(rid, {})} for rid in {*catalogue, *flags}}
    try:
        cmap = build_country_map(c.countries(), rows)
    except UnmappedCountries as exc:
        sys.exit(f"history: {exc}")

    points: list[hi.Point] = []
    if not args.no_photos:
        from ..sources.photos_app import DEFAULT_LIBRARY, PhotosAppSource

        library = Path(args.library) if args.library else DEFAULT_LIBRARY
        if library.exists():
            print(f"reading {library}...", file=sys.stderr)
            for a in PhotosAppSource(library=library).assets():
                points.append(hi.Point(a.date, a.lat, a.lon, None, "photos", a.uuid, a.taken))
        elif args.library:
            sys.exit(f"history: no Photos library at {library}")
    if args.track:
        from ..sources.files import load

        for i, p_ in enumerate(load(args.track).points):
            points.append(hi.Point(p_.date, p_.lat, p_.lon, None, "track", f"track:{i}"))

    if points:
        coords = sorted({(round(p_.lat, 3), round(p_.lon, 3)) for p_ in points})
        print(f"{len(points):,} points, {len(coords):,} distinct coordinates", file=sys.stderr)
        known = _resolve_coords(args, catalogue, coords)

        def region(p_):
            r = known.get((round(p_.lat, 3), round(p_.lon, 3)))
            return r.region if r is not None else None

        points = [
            hi.Point(p_.date, p_.lat, p_.lon, region(p_), p_.source, p_.ref, p_.at)
            for p_ in points
        ]

    visits = []
    if not args.no_profile:
        claimed = sorted(c.visited_region_ids())
        print(f"reading every visit record for {len(claimed)} regions...", file=sys.stderr)
        for i, rid in enumerate(claimed, 1):
            visits.extend(c.visits_for_region(rid))
            if i % 100 == 0:
                print(f"  {i}/{len(claimed)}", file=sys.stderr)

    if not points and not visits:
        sys.exit("history: no sources — give --track, a Photos library, or drop --no-profile")

    ledger = hi.build_ledger(points, visits, cmap, bridge=args.bridge, fast_kmh=args.fast_kmh)
    territories = args.territories or hi.DEFAULT_TERRITORIES.get(q, "separate")

    if q == "absences" and not home:
        homes = {cmap.place(r).iso for r in c.home_regions()} - {UNMAPPED}
        if not homes:
            sys.exit("history absences: give --home; the profile has no homebase set")
        home = homes
        print(f"home from your NomadMania homebase: {', '.join(sorted(home))}", file=sys.stderr)

    used = groups_used(getattr(args, "countries", None)) + groups_used(
        getattr(args, "exclude", None)) + groups_used(getattr(args, "home", None))
    hdr = hi.header(
        ledger, command="wanderfill " + " ".join(getattr(args, "argv", None) or ["history", q]),
        since=since, until=until, territories=territories,
        groups=hi.describe_groups(dict.fromkeys(used)), today=today,
    )
    if q == "days":
        start = None
        if args.tax_year_start:
            m, d = (int(x) for x in args.tax_year_start.split("-"))
            start = (m, d)
        report = hi.query_days(
            ledger, year=args.year or today.year, scheme=args.tax_year, start=start,
            territories=territories, today=today, hdr=hdr,
        )
    elif q == "absences":
        report = hi.query_absences(
            ledger, home=home, since=since or hi.parse_when("5y", today), until=until,
            territories=territories, hdr=hdr,
        )
    elif q == "check":
        report = hi.query_check(
            ledger, countries=include, exclude=exclude, since=since, until=until,
            territories=territories, hdr=hdr,
        )
    elif q == "where":
        report = hi.query_where(
            ledger, since=since, until=until, by=args.by, territories=territories, hdr=hdr,
        )
    else:
        report = hi.query_trips(
            ledger, since=since, until=until, include=include, exclude=exclude,
            first=getattr(args, "first", False), territories=territories, hdr=hdr,
            title="every trip" if q == "list" else "trips",
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"history-{q}"
    stem.with_suffix(".md").write_text(hi.render_markdown(report), encoding="utf-8")
    stem.with_suffix(".csv").write_text(hi.render_csv(report), encoding="utf-8")
    stem.with_suffix(".json").write_text(
        json.dumps(hi.render_json(report), indent=1, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"sources: {ledger.coverage_text()}")
    print(hi.render_text(report))
    print(f"\nwrote {stem}.md, .csv and .json")
    print(
        "This wrote nothing to NomadMania. It is a draft for you to check and copy — "
        "it does not decide what a form counts as a visit."
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
        # Everything a human needs to approve the write, not just its label.
        # Reviewing a plan from a summary that hides the dates, the visit id and
        # the quality is not review; it is assent.
        for op in plan.ops[: args.limit]:
            print(f"\n  [{op.bucket}] {op.kind}  {op.label}")
            bits = []
            if op.region is not None:
                bits.append(f"region={op.region}")
            if op.visit_id is not None:
                bits.append(f"visit={op.visit_id}")
            if op.series is not None:
                bits.append(f"series={op.series}")
            if op.item is not None:
                bits.append(f"item={op.item}")
            if op.date_from or op.date_to:
                bits.append(f"dates={op.date_from}..{op.date_to}")
            if op.quality is not None:
                bits.append(f"quality={op.quality}")
            if op.allow_vaguer:
                bits.append("ALLOW_VAGUER — this may erase precision")
            if op.regions:
                bits.append(f"{len(op.regions)} trip region(s): "
                            + ",".join(str(r.get('id')) for r in op.regions))
            print(f"      {'  '.join(bits)}")
            print(f"      confidence {op.confidence}  |  {op.method}")
            for e in op.evidence:
                print(f"        - {e}")
    elif plan.ops:
        print("Run with -v to see dates, ids, quality and the evidence behind each op.")
    return 0


def cmd_check(args) -> int:
    """Read-only: does this plan still match the profile it was built against?"""
    from ..plan.apply import Journal
    from ..plan.model import Plan, basis_of, fingerprint, regions_touched

    c = _client(args)
    plan = Plan.load(args.plan)
    ops = plan.to_apply()
    problems = []

    live = c.account_id()
    print(f"account: plan {plan.account}, logged in as {live}"
          f"{'  ✓' if live == plan.account else '  ✗ MISMATCH'}")
    if live != plan.account:
        problems.append("account mismatch")

    keys = [o.key for o in ops]
    if len(keys) != len(set(keys)):
        problems.append("the plan contains the same write twice")

    regions = sorted(regions_touched(ops))
    snap = c.snapshot(regions or None)
    expected = plan.basis.get("fingerprint")
    if not expected:
        problems.append("no basis fingerprint — apply will refuse this plan")
    else:
        now = fingerprint(basis_of(snap, regions))
        same = now == expected
        print(f"drift:   {'unchanged since planning  ✓' if same else 'STATE HAS MOVED  ✗'}")
        if not same:
            problems.append("live state drifted; re-plan")

    workdir = Path(args.workdir)
    journal = Journal(workdir / f"journal-{plan.account}-{fingerprint(plan.basis)[:12]}.ndjson")
    done, stuck = journal.done_keys(), journal.unresolved()
    if done:
        print(f"journal: {len(done)} op(s) already applied and would be skipped")
    if stuck:
        problems.append(f"{len(stuck)} op(s) started and never confirmed — reconcile by hand")

    print(f"\n{len(ops)} op(s) would execute across {len(regions)} region(s).")
    if problems:
        print("\nBLOCKERS:")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("No blockers. This wrote nothing.")
    return 0


def cmd_state(args) -> int:
    """Read-only view of the hand-ticked lists, which nothing derives for you.

    These exist because two of them were found sitting at or near zero on a
    profile with 391 regions: nothing fills KYE or a series in from your visits,
    so an unread list looks identical to a list you have nothing for.
    """
    c = _client(args)

    kye = c.kye()
    ticked, total = len(kye.get("visited", [])), kye.get("max", 0)
    if total:
        print(f"KYE           {ticked:>4} / {total}   ({100 * ticked / total:.0f}%)")
    else:
        print("KYE           unavailable")

    if args.series:
        s = c.series(args.series)
        print(f"series {args.series:<6} {s.get('score'):>4} / {s.get('max')}   {s.get('title')}")
    else:
        print("series: pass --series <id> for one list (1 = World Capitals, 22 = WHS).")

    print("\ncountries, by the server's stored YES and by recomputing the rule:")
    stored = {r["country_id"]: r for r in c.countries()}
    mine = c.yes_scores()
    tot_stored = sum(r.get("yes_stored", 0) for r in stored.values())
    tot_mine = sum(v["yes"] for v in mine.values())
    print(f"  stored (this is the ranking) {tot_stored}")
    print(f"  recomputed here              {tot_mine}")
    disagree = [v["country"] for cid, v in mine.items()
                if stored.get(cid, {}).get("yes_stored") != v["yes"]]
    if disagree:
        print(f"  {len(disagree)} disagree: {', '.join(sorted(disagree)[:12])}"
              f"{' …' if len(disagree) > 12 else ''}")
        print("  The stored field lags a batch job. Where they differ it is usually right.")
    undated = sorted(v["country"] for v in mine.values() if v["undated"])
    if undated:
        print(f"\n  marked visited but carrying no year "
              f"({len(undated)}): {', '.join(undated)}")
        print("  Each costs 8. Dating one only gains if the real year is recent —")
        print("  use yes_delta() rather than assuming.")
    return 0


def cmd_share(args) -> int:
    """A card of the headline numbers, sized for a story, a square post or a link.

    Read-only on the server side: settings, the country list, the region id
    list, DARE and KYE, and sixteen public map tiles. The writes are PNG files
    and a ``stats.json`` in ``--out``. Nothing is posted anywhere — sharing is
    the user's own act, afterwards, and this command has no way to do it.

    The numbers are the server's, never derived here. The map paints the
    regions the profile already has marked.
    """
    from .. import share as sh

    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("share needs Pillow: pip install 'wanderfill[share]'")

    c = _client(args)
    stats = sh.collect(c, name=args.name)
    if args.anonymous:
        stats.name = ""

    polygons = None
    if not args.no_map:
        try:
            polygons = sh.world_polygons(zoom=args.zoom)
        except ImportError:
            sh.warn("no map: the geo extra is missing (pip install 'wanderfill[geo]')")
        if polygons is not None and not polygons:
            sh.warn("no map: the tile server returned nothing; the card is drawn without it")
            polygons = None

    avatar = None
    if not args.no_avatar and not args.anonymous and stats.avatar:
        avatar = sh.fetch_avatar(stats.avatar)
        if avatar is None:
            sh.warn("avatar not fetched; drawing without it")

    fonts = sh.Fonts(display=args.font, text=args.font_text or args.font)
    sizes = list(sh.SIZES) if args.size == "all" else [args.size]
    written = sh.write_cards(
        stats, Path(args.out), sizes=sizes, theme=args.theme, polygons=polygons,
        avatar=avatar, fonts=fonts, handle=args.handle,
        show_rank=not args.no_rank, show_extras=not args.no_extras,
    )
    print(f"UN {stats.un} / {sh.UN_MEMBERS}   UN+ {stats.un_plus} / {stats.un_plus_total}"
          f"   NM {stats.nm} / {stats.nm_total}")
    if stats.rank:
        print(f"rank #{stats.rank} worldwide"
              + (f", #{stats.country_rank} in {stats.country}" if stats.country_rank else ""))
    print(f"fonts: {fonts.describe()}")
    for p in written:
        print(p)
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

    hs = sub.add_parser(
        "history",
        help="read-only: where was I, and when — visa forms, ESTA, tax days, absences",
        description=(
            "Answer travel-history questions from your profile and your location "
            "history, cross-checked. Writes local files only."
        ),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--track", help="CSV/GPX/GeoJSON day-level track")
    common.add_argument("--library", help="path to a .photoslibrary (default: the standard one)")
    common.add_argument("--no-photos", action="store_true", help="do not read the Photos library")
    common.add_argument("--no-profile", action="store_true", help="do not read NomadMania visits")
    common.add_argument("--bridge", type=int, default=7,
                        help="fill same-country gaps of up to N days, labelled 'bridged' (0 = off)")
    common.add_argument("--fast-kmh", type=float, default=200.0,
                        help="timed points faster than this are treated as airborne")
    common.add_argument("--territories", choices=("separate", "sovereign"),
                        help="list territories (Greenland, Canaries…) on their own, or fold "
                             "them into the sovereign (default: separate; sovereign for "
                             "days and absences)")
    common.add_argument("--out", default="history", help="directory for the output files")
    common.add_argument("--cache", default=str(DEFAULT_WORKDIR / "coords.json"))
    common.add_argument("--zoom", type=int, default=10)
    common.add_argument("--tolerance", type=float, default=0.30)
    common.add_argument("--no-resolve", action="store_true",
                        help="use only the coordinate cache; do not fetch tiles")
    window = argparse.ArgumentParser(add_help=False)
    window.add_argument("--since", help="YYYY-MM-DD, or 10y / 18m / 90d back from today")
    window.add_argument("--until", help="YYYY-MM-DD (default: today)")
    pick = argparse.ArgumentParser(add_help=False)
    pick.add_argument("--countries", help="only these: codes and groups, e.g. vwp-restricted,cu")
    pick.add_argument("--exclude", help="leave these out, e.g. eea,ch,uk,us (- subtracts)")

    hsub = hs.add_subparsers(dest="query", required=True)
    w = hsub.add_parser("where", parents=[common], help="a timeline of one period")
    w.add_argument("period", help="2024-03-01..2024-04-15, 2024-03, or 2024")
    w.add_argument("--by", choices=("region", "country"), default="region")
    t = hsub.add_parser("trips", parents=[common, window, pick],
                        help="countries with entry/exit dates (visa forms)")
    t.add_argument("--first", action="store_true", help="only the first trip to each country")
    hsub.add_parser("check", parents=[common, window, pick],
                    help="yes / no evidence for named countries (ESTA)")
    d = hsub.add_parser("days", parents=[common], help="days present per country in a tax year")
    d.add_argument("--year", type=int, help="the year the tax year starts in (default: this year)")
    d.add_argument("--tax-year", default="calendar", choices=sorted(TAX_YEAR_CHOICES))
    d.add_argument("--tax-year-start", help="MM-DD, for a tax year not in the list")
    a_ = hsub.add_parser("absences", parents=[common, window],
                         help="time away from a home country (residence, citizenship)")
    a_.add_argument("--home", help="home country code (default: your NomadMania homebase)")
    hsub.add_parser("list", parents=[common, window, pick], help="every trip, chronologically")
    hs.set_defaults(func=cmd_history)

    sh = sub.add_parser("show", help="print a plan file")
    sh.add_argument("plan")
    sh.add_argument("-v", "--verbose", action="store_true")
    sh.add_argument("--limit", type=int, default=40)
    sh.set_defaults(func=cmd_show)

    ck = sub.add_parser("check", help="read-only: is this plan still safe to apply?")
    ck.add_argument("plan")
    ck.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    ck.set_defaults(func=cmd_check)

    st = sub.add_parser("state", help="read-only: KYE, series and YES as the server has them")
    st.add_argument("--series", type=int, help="one series id, e.g. 1 for World Capitals")
    st.set_defaults(func=cmd_state)

    shr = sub.add_parser(
        "share",
        help="render your UN / UN+ / NM numbers as an image for stories and posts",
        description=(
            "Draw a card with your three headline counts, your rank and a map of "
            "every region you have marked. Reads the profile, writes PNG files. "
            "It never posts anything."
        ),
    )
    shr.add_argument("--out", default="share", help="directory for the images")
    shr.add_argument("--size", choices=("story", "square", "post", "all"), default="story",
                     help="story 1080x1920, square 1080x1080, post 1200x630, or all")
    shr.add_argument("--theme", choices=("night", "paper"), default="night")
    shr.add_argument("--name", help="show this name instead of the one on the profile")
    shr.add_argument("--handle", help="show this under the name instead of the profile URL")
    shr.add_argument("--anonymous", action="store_true", help="no name, no avatar")
    shr.add_argument("--no-avatar", action="store_true")
    shr.add_argument("--no-map", action="store_true", help="skip the tiles; numbers only")
    shr.add_argument("--no-rank", action="store_true", help="leave the rank line off")
    shr.add_argument("--no-extras", action="store_true", help="leave DARE and KYE off")
    shr.add_argument("--zoom", type=int, default=2, help="tile zoom for the map (2 = 16 tiles)")
    shr.add_argument("--font", help="a .ttf/.ttc for the numbers (default: what the OS has)")
    shr.add_argument("--font-text", help="a .ttf/.ttc for the small text")
    shr.add_argument("--quiet", action="store_true")
    shr.set_defaults(func=cmd_share)

    a = sub.add_parser("apply", help="execute a plan file")
    a.add_argument("plan")
    a.add_argument("--confirm", action="store_true", help="actually write")
    a.add_argument("--max-writes", type=int, default=200)
    a.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    a.set_defaults(func=cmd_apply)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.argv = list(argv) if argv is not None else sys.argv[1:]
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
