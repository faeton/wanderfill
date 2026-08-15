"""wanderfill command line.

The command set encodes the safety model rather than merely documenting it:

    whoami     confirm the token works and which account it belongs to
    export     read-only dump of the whole profile (also the rollback reference)
    resolve    coordinates -> region ids, with the stale-id repair
    sweep      show what each trip-segmentation setting produces
    plan       write a plan file. Never touches the server's state.
    show       print a plan for a human to read
    apply      execute a plan file, and only a plan file

There is deliberately no command that computes and writes in one step.
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
                    "id": v.id, "from": v.date_from.isoformat(), "to": v.date_to.isoformat(),
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
    from ..plan.segment import sweep
    from ..sources.files import load

    track = load(args.track)
    day_regions: dict = {}
    mapping = json.loads(Path(args.regions).read_text(encoding="utf-8"))
    for p in track.points:
        key = f"{round(p.lat, 3)},{round(p.lon, 3)}"
        rid = mapping.get(key)
        if rid:
            day_regions.setdefault(p.date, set()).add(int(rid))
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
