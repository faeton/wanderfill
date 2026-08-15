# wanderfill

**Import your location history into NomadMania.**

Your photo library already knows where you have been. `wanderfill` turns that
into a structured travel record — regions with real date ranges, trips, DARE
areas, series — and writes it to your NomadMania profile through a plan you read
and approve first.

> **Unofficial and unaffiliated.** NomadMania is a trademark of NomadMania OÜ.
> This project is not affiliated with, endorsed by, or supported by NomadMania.
> Please do not contact them about bugs in this tool.

---

## What it does

1. Reads a location history you already have — a photo library, a GPX folder, a
   CSV, a Timeline export.
2. Turns every coordinate into a NomadMania region, repairing the ids their
   reverse geocoder gets wrong.
3. Cuts the track into trips, showing you the options rather than picking one.
4. Writes a **plan file** — every intended change, with the evidence behind it.
5. You read the plan. Then, and only then, `apply` executes it.
6. Separately, and read-only: indexes the same library into a **proof dossier**,
   so that when NomadMania asks you to verify sixty regions you can answer.

The parts that are hard to get right — a geocoder that returns dead ids, an API
that double-counts, an endpoint that silently downgrades your data — are handled
in the library so you do not have to discover them the way they were discovered
here. [`docs/nomadmania-api.md`](docs/nomadmania-api.md) documents all of it.

### A worked example

The package was built while doing this once, properly, against one real archive
— a day-level photo track of 4,868 days, 2009 to 2026, from a self-hosted
[Immich](https://immich.app) library. That run is the example the docs refer to,
and it went:

| | before | after |
|---|---:|---:|
| Regions marked | 335 | 392 |
| Regions with real dates | 5 | 274 |
| Visits | 377 | 867 |
| Trips | 0 | 183 |
| DARE areas | 20 | 30 |

Four days out of 4,868 stayed unresolved. They are over open ocean, and the tool
left them alone rather than snapping them to the nearest land.

Your numbers will look nothing like these, and that is the point: the shape of
the work is the same whether you have seventeen years of photos or one holiday.

---

## Proving it — the other half of the problem

Getting the regions onto the profile is the easy half. NomadMania does not run
on trust: its highly ranked travellers are **verified**, and verification is a
human committee asking you to prove a random sample of what you claim.

| | when it happens | the sample | the bar |
|---|---|---|---|
| UN countries | mandatory in the top 100, or claiming all 193 | ~40 countries, weighted to the hard ones | all of them, within 3 months |
| NM regions | mandatory in the top 50, usually asked around 600 regions | 60 regions | **45 must be Class 1**, within 6 months |
| Supreme | optional, 600+ regions, a year after the first | 25 countries + 40 regions, aimed at the difficult ones | 95%, renewed every 11 months |

Failing is not just a missing badge: they reserve the right to freeze the
profile, delete as many regions as they see fit, or mark the account a **Ghost
User**, excluded from every ranking. Re-verification is forced after five years,
or immediately if a region count jumps.

Their **Class 1** list reads like a description of a geotagged photo library —
selfies with a prominent landmark, *serial photos within the region*, dated
diary entries, hotel bills in your name, ATM withdrawals with a location and a
date. **Class 2** — ordinary photos, a friend vouching, a map of the route — is
filler for the remaining fifteen.

So a traveller with fifteen years of photos, footage and paperwork already holds
the evidence. What they do not hold is an **index**. When sixty region names
arrive with a six-month clock, the work is finding five defensible photos for
each one across a hundred thousand assets, by hand, region by region. That is
what `wanderfill evidence` is for:

```bash
wanderfill evidence --check-dates --out evidence
wanderfill evidence --out evidence --copy --documents ./paperwork
```

It reads your claimed regions from NomadMania, reads the photo library locally,
attributes every photo to a region against the **live polygons**, and writes:

- **`index.md`** — every claimed region graded `strong` / `serial` / `thin` /
  `none`, the regions with nothing to show listed first, and per region a
  shortlist of exhibits chosen for *spread* — different days, different places
  inside the region — because that is what "serial photos" means. Search it by
  region name on the day the sample arrives.
- **`evidence.json`** — the same, with asset uuids, coordinates and file paths.
- **`export-from-photos.applescript`** — because most of a modern library is a
  thumbnail with the original in iCloud. This pulls the shortlisted originals
  down and lays them out one folder per region, ready to attach.
- with `--check-dates`, a table of **regions whose photos fall outside the visit
  dates on your profile** — a claim dated wrong reads as guessing, and that is
  exactly what a committee looking for records that "don't correspond to the
  reality on the ground" is looking for.
- with `--documents`, your own paperwork indexed alongside — drop the hotel bill
  into `paperwork/0292-iceland-south/` and it appears in that region's section.
  Filed documents are **listed, never graded**: a file called `hotel.pdf` could
  be anything, and a tool that scored it would be inventing proof.

It also runs the audit backwards, listing regions that have photos in them and
**no visit on your profile** — which is the importer's to-do list, not something
this command acts on.

### What it will not do

- **It writes nothing to NomadMania.** No plan, no `--apply`; there is nothing
  to apply. It is the one command whose output is aimed at you.
- **It does not decide anything is proven.** A photo proves a *camera* was
  somewhere. It can read Apple's front-camera flag, so it knows a selfie from an
  ordinary photo; it cannot see whether a landmark is in the frame.
- **A region graded `none` is not a region you did not visit.** Batteries die
  and film has no EXIF. It is a region whose proof is somewhere other than this
  library — and knowing which ones those are, before anyone asks, is the point.
- **Photos somebody sent you are excluded at the source**, along with
  screenshots. A friend's geotagged photo is evidence about their travel.

### What "serial photos" is taken to mean

A region earns `serial` on one of three routes, and every threshold is a flag
you can change — they are guesses about how *you* travel, and the dossier prints
the ones it used:

| route | test | flags |
|---|---|---|
| a stay | ≥3 photos inside the polygon, ≥1 km between the furthest two, over ≥2 days | `--min-shots` `--min-spread-km` `--min-days` |
| a day trip | the same, in one day, across ≥3 separate spots **spanning ≥2 hours** | `--min-spots` `--min-hours` |
| too small to cross | ≥2 spots on ≥2 days, for a region that measures under 8 km | `--min-small-spots` `--small-region-km` |

The hours matter more than they look. Eight photos across 34 km sounds like
serial coverage of a region until you read the timestamps and find they span two
and a half minutes — that is a plane window, and a verifier reading the same
timestamps will say so. On the run this was built against, the hours rule
reclassified fourteen regions, every one of them a drive-through or a flight.

The small-region route is granted by **measuring the polygon**, never by
noticing that the photos are clustered. Two days in one hotel in Nairobi looks
identical in the data to two days in Vatican City, and only one of them is a
visit.

### Two numbers, and deliberately no verdict

The command projects a 60-region draw twice: once assuming every claimed region
is equally likely, and once drawn from the least-photographed half of your
profile — because NomadMania say their sample "always includes some of the most
difficult countries on the planet", and difficult places are where photo
libraries are thinnest. The gap between those two numbers is the size of your
exposure.

There is no ready/not-ready flag, on purpose. A serial photo set is evidence you
can hand over; whether it *passes* turns on landmarks and faces and is the
committee's call. A tool that printed "ready" would be making a promise on their
behalf, and somebody would stop collecting evidence on the strength of it.
Regions whose photos contradict the dates on your own profile count toward
neither number.

---

## Risks — read this part

- This uses NomadMania's **undocumented internal API**. It can break at any time,
  without notice, and probably will.
- It writes to your **real, publicly-ranked profile**.
- **NomadMania may suspend accounts at their sole discretion.** That, rather than
  any legal exposure, is the real risk you are taking. Their terms make the
  account holder responsible for all activity under the account, and running this
  is your activity.
- The token is a **full-power credential valid for a year**. Do not paste it into
  an issue, a gist, or a chat with a language model.

---

## Ethos

These are constraints in the code, not aspirations in a document.

- **Your account only, your data only.** No reads of other users, no leaderboard
  scraping. Those endpoints are not in the client.
- **It never deletes.** v1 has no delete code path — not behind a flag, absent
  from the class. There is a test asserting this.
- **It never invents travel.** Only regions containing an observed point are
  claimed. No interpolation between points, no "you must have crossed X to get
  from A to B". This is a scored competition; an inferred region is a fabricated
  score, and it is you who gets accused of cheating, not the tool.
- **Rate-limited to human speed**, with an identifying User-Agent so NomadMania
  can rate-limit or block this specifically rather than a whole traffic shape.
- **No telemetry, ever.** Everything runs locally. Location history is among the
  most sensitive data a person has.
- **No bundled NomadMania data.** The region catalogue and polygons are theirs.
  They are fetched at runtime and cached in your own directory.
- **No hosted version.** Nothing that holds somebody else's token.

## Don't use this to cheat

This exists so you do not have to re-enter travel you actually did. It is built
so that claiming places you did not visit takes *more* effort than not doing so.
If that is what you came for, this is the wrong tool.

---

## Install

```bash
pip install "wanderfill[all]"
```

## Getting a token

Open nomadmania.com while logged in, and run this in the browser console:

```js
localStorage.getItem('token')
```

```bash
export NM_TOKEN='...'
wanderfill whoami
```

The password is never needed and this tool will never ask for it.

## Use

```bash
# read-only: dump the whole profile. Also your rollback reference.
wanderfill export --full --out profile.json

# read-only: which of your claimed regions could you actually prove?
wanderfill evidence --check-dates --out evidence

# see what each trip-segmentation setting produces before choosing one
wanderfill sweep track.csv regions.json

# read a plan
wanderfill show plan.json --verbose

# execute it — dry run by default
wanderfill apply plan.json
wanderfill apply plan.json --confirm
```

There is deliberately **no command that computes and writes in one step.**

## As a library

```python
from wanderfill import NomadMania

nm = NomadMania(token=os.environ["NM_TOKEN"])

catalogue = nm.regions()                     # the live 1381-region list
visited   = nm.visited_region_ids()
visits    = nm.visits_for_region(292)        # ALL of them — see below

# reverse geocode; share=0 is enforced, never optional
hit = nm.region_at(41.8902, 12.4922)
```

The most recent trip is usually the one no server has seen yet — the phone has
not synced and the self-hosted library stops weeks ago. The local Photos library
does have it:

```python
import datetime as dt
from wanderfill.sources import load_photos

track = load_photos(since=dt.date(2026, 8, 1))   # per-photo points, not per-day
```

Per-photo resolution matters on the road: a day driving from Czechia through
Austria into South Tyrol averages to one coordinate in one region, and the other
two are simply lost.

The same library read the other way — one record per photo, identity intact —
is what the dossier is built from:

```python
from wanderfill.evidence import build, render_markdown
from wanderfill.sources.photos_app import load_photo_assets

assets = load_photo_assets()          # uuid, timestamp, coordinate, selfie flag
```

## Do you have a home?

The single question that changes the output most, and the one every other trip
importer answers for you without asking.

```python
segment(days, home="infer")      # home = the region you spent most of that year in
segment(days, home=[292])        # home = these regions, stated
segment(days, home=None)         # no home. Every day is travel.
```

`home=None` is not an edge case. If you are genuinely nomadic, any other setting
silently removes the region you spent the most time in from your own trips.

`client.home_regions()` reads what you told NomadMania in your profile — but
that is a hint, not a fact. Settings go stale: a home set five years ago sits
there long after somebody stopped having one. The library will not use it unless
you pass it.

---

## The design, in one paragraph

**Computation never writes, and writing never computes.** A plan file sits
between them, carrying the ops, their evidence, a fingerprint of live state at
plan time, hashes of the sources, the segmentation parameters, and the account
id it was built for. From that one primitive you get dry-run, review,
idempotency, resume and reproducibility. `apply` takes a plan file and nothing
else; it refuses to run if the account differs, if state has drifted since
planning, if the snapshot fails, or if the op count exceeds a ceiling.

---

## The traps this package handles for you

Full detail in [docs/nomadmania-api.md](docs/nomadmania-api.md). Each of these
cost a wrong write or a wrong number the first time.

| Trap | What this package does |
|---|---|
| `add-visit` silently creates a phantom single-region trip | Documented on the method; the journal records the trip id |
| A visit counts whether standalone **or** trip-owned, so naive trips double-count | `visits_for_region` returns both kinds; `split_first_and_repeat` keeps totals honest |
| `trips/new-trip` requires a junk `regions` field before it will read `regions_json` | `create_trip` sends both, reproducing the site's own bug deliberately |
| `update-visit` replaces the whole record and silently downgrades `quality` | `quality` is keyword-only and required |
| `get-regions-mqp` returns `visited` as an id string, not a boolean | Not used; `visited_dare_ids` is the source of truth |
| `location/get-region` runs on stale polygons — ~14% of ids are dead | `RegionResolver` validates every id and repairs failures against live tiles |
| Vector tiles are gzipped with no usable `Content-Encoding` | `TileReader` sniffs the magic bytes |
| `location/get-region` doubles as a live-location beacon | `share=0` is hardcoded, not a parameter |

## Series: what the measurements actually showed

Ticking every series object within a few kilometres looks obvious and is wrong.
Scored against 392 objects a real user had already ticked by hand:

| radius | candidates offered | of the already-ticked, found |
|---:|---:|---:|
| 1 km | 2,770 | 33% |
| 3 km | 5,738 | 57% |
| 10 km | 9,839 | 84% |

No threshold is both honest and useful — series like *Art Museums* and *Markets*
pack hundreds of objects into one city centre, and a day-level track has one
point per city. So `wanderfill` treats series as a **shortlist generator** and
prints that recall curve every time.

The exceptions are series where being somewhere *is* the visit — World Capitals,
European Cities, Cities of the Americas, African Cities, Cities of Asia and
Oceania. Those match on **place name**, not distance. Airports get the opposite
rule, matching on distance only, because their objects are named after their
cities. Every rule is scored against what is already ticked before it may add
anything; on the original run, that gate stopped the Airports rule at 25% recall
on its own.

---

## Sources

| Source | Status |
|---|---|
| CSV (date, lat, lon, optional place) | supported — also the escape hatch for hand-fixing |
| GPX / XML | supported — covers Garmin, OsmAnd, Komoot, Gaia, and Strava *exports* |
| GeoJSON | supported |
| **macOS Photos.app** | supported — reads the local library, including a trip your phone has not synced yet |
| EXIF photo folders | planned |
| Immich API | planned |
| Google Timeline | planned, isolated, expect yearly breakage |
| Strava API | **won't**. Restrictive terms, wrong data shape. Export GPX instead. |
| Polarsteps API | **won't**. That means reverse-engineering a second private API. |

**Rule for contributors:** a new source is a parser that emits `DayPoint`. It
must not add an HTTP client for a third party's private API.

---

## For AI agents

[`AGENTS.md`](AGENTS.md) (symlinked as `CLAUDE.md`) is standing orders for a
model driving this against a real profile: how to get the token without touching
a password, which state to read before planning, each trap attached to the call
that causes it, when to stop and ask, and what to do after getting it wrong. It
is the most reusable thing in this repository.

---

## For NomadMania

If you would like this changed or taken down, open an issue or email the
maintainer and it will happen.

Two things this project commits to: it ships **none** of your data — no
catalogue, no polygons, no tiles in the repository, all fetched at runtime — and
it documents only what an account holder needs for their own data. Anything that
looks like a missing authorisation check gets reported privately and stays out of
the repo.

One free bug report, offered in good faith: **`location/get-region` is running on
a stale polygon set.** Roughly 14% of the region ids it returns no longer exist
in `regions/get-regions-list-2`. Observed cases include region 463 (Cyprus, now
1592/1593), 49 (Portugal → 1312), 208 (Hungary → 1376), 87 (Austria → 1378) and
12 (Greece → 1594).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
