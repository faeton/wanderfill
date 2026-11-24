# `wanderfill trips`: photos to a trip plan

Status: design, 2026-11-24. The owner answered questions 1, 2, 4 and 6 as recommended; 3 and 5 are open. The answers settle the design only: building it needs a separate yes.
File and line numbers are of commit `41dbf15`, under `src/wanderfill/`.

## Purpose

Turn the owner's photos into questions ("these photos are inside this region on these days"), and the answers into one plan file of `create_trip` ops for `check` and `apply`. The planner sends no write.

The dangerous error is claiming too much: a region, day or visit that did not happen, or one that is already on the profile. A missed visit can be added later. A wrong one can only be removed by the owner, one id at a time. So every unclear case is **held**: printed with the reason, not in the plan.

## Commands

```
wanderfill trips review --since DATE [--until DATE] [--gap-days N] [--fast-kmh N]
        [--origin NAME]... [--spans FILE] [--stays FILE] [--home FILE]
        (--track FILE | --no-track) [--library PATH] [--out DIR]
wanderfill trips plan MANIFEST ANSWERS [--plan FILE]
```

`review` writes a manifest and an offline page into `review/trips/` (`review/` is git-ignored already), and prints the gap table, what was left out and why, and the paths. Exit 0, or 3 when a tile could not be read (files are still written). `plan` reads the profile again and writes one plan file.

## Architecture

```
photos -> grade_shots (one origin at a time) -> tiles -> runs per region -> manifest + page
profile visits and trips, read strictly -------------------^                     |
                                    owner's answers -> trips plan -> PLAN FILE -> check -> apply
```

- `plan/trips.py`, pure: shots, resolutions, visits in; runs, cuts, manifest out. No network, no files.
- `plan/trips_page.html`: static, same rules as `review_page.html` (CSP, no links, nothing preselected).
- `plan/manifest.py`: `_canonical`, `_read_json`, `_fields` and the hash binding, moved out of `plan/review.py`. The series `Manifest` cannot be reused itself: its parameters and candidates are series ones (`plan/review.py:614-645`) and an answer is only `y`, `n` or `q` (612).
- `cli/main.py` loads inputs and writes files. The client has no cache.

## Decisions

Status: **R** is a recommendation that follows from the rules or the code. **Qn** is the owner's choice, question n.

| # | Question | Recommendation | Reason | Cost if wrong | |
|---|---|---|---|---|---|
| 1 | What is a run | The days with an own-camera `ground` shot strictly inside one region. Two days join when at most `gap_days` apart (default 2, so one silent day) and no ground shot is inside another region on the silent days. Dates are the first and last photo day | Edge days are always observed. Same meaning of `gap_days` as `segment` (`plan/segment.py:244`). The "nothing seen elsewhere" rule resembles `history`'s, which also bridges nothing across an overflight inside the gap, or from an edge day with more than one country | Too small: one stay becomes two visits. Too large: days claimed in a place already left | Q1 |
| 1a | Joining runs a longer gap or a day elsewhere keeps apart | Only by `--stays FILE`: the owner's `{region, from, to, why}`, written unprompted; both ends must be photo days of that region. A row also releases a `layover?` run whose dates it gives; it never overrides a span. The page has no join button | Showing a gap and asking "there all the time?" is what rule 7 forbids | More work for a base with overnight excursions | R |
| 2 | Region evidence states | Four: **inside**; **none strictly inside** (nearest-polygon only); **not read** (tile failed); **no photo**. Only *inside* makes a run | `tile_nearest` reaches 0.30 degrees, 33 km of latitude (`geo/resolve.py:77, 158`) | A neighbour across the border is claimed | R |
| 2a | Strictly inside | `tile_interior` from the raw coordinate, and the eight points 100 m around it are in the same region or in none (a failed tile there is *not read*). Otherwise the shot is `edge`: it does not start, end or make a run, and is counted | `covers` includes the border (`geo/resolve.py:150`); the shared coordinate cache is rounded to about 110 m (`sources/photos_app.py:228`); tile outlines are simplified | Tiny regions, and islets whose outline is drawn small, have only `edge` shots: held, shown as such | R |
| 2b | "None inside" and "no photo" are printed for | Regions with a visit on the profile in the window, as a report. Nothing else | A list of unvisited regions without photos is a prompt | None | R |
| 3 | First visits and repeats | Every new run of a journey goes into one `create_trip`, first visit or not. `add_visit` is not used, for a one-region journey either | Totals inflate only when a trip repeats a visit that exists; decision 6 holds those. And `add_visit` is not standalone: the server wraps it in its own trip (`api/client.py:727-730`, `plan/apply.py:1052-1056`), so the split leaves one phantom trip per first visit and a real trip without them | If a duplicate slips through, one region counts twice; `verify` reports it | Q2 |
| 3a | If Q2 is answered "split" | Two plan files, the second built after the first is applied | One approval per kind (rule 5). Applying the first marks regions, and the fingerprint covers all marked ids (`plan/model.py:218-221`) | An extra round | R |
| 4 | Where a trip is cut | At runs, never inside one. The tool only proposes cuts, with the fact: `fast leg` (over 200 km/h, `grade.FAST_KMH`, between the last ground shot of one run and the first of the next), `silence` (over `gap_days`), `home` (stated), `long` (over 30 days). The owner answers cut or same; blank holds both sides. Places with no proposal between them are one trip | Segmentation has no right answer (AGENTS 5). A cut by day count inside a stay writes two visits for one | A wrong cut makes two trips of one; no region or date changes | R |
| 4a | Ground speed for proof | 120 km/h, as in series review; `--fast-kmh` raises it, and it is in the manifest | One rule for both reviews. A landing aircraft must not pass | Photos from a motorway or a fast train are not proof; a region seen only from them is held | Q3 |
| 4b | Layovers | A ground shot in an `airports` span is no proof of a region. A run under 24 h with fast shots or a fast leg on both sides, and no span, is flagged `layover?`. Both are in the held list with the facts, not answerable | `airports` means "in the airport and nowhere else" (`plan/review.py:72-75`). Whether an airside stop is a visit is the owner's call (AGENTS 6: hold, not reject, not mark) | A real short stay between two flights is not offered until the owner lists it in `--stays`, or takes the span out | Q4 |
| 5 | Finished stay | Finished when an own-camera ground shot is strictly inside another region, with `use` `all`, dated more than `gap_days` after the run's last day. Otherwise `open`, and held until the owner types in their own words that it is over. A trip is planned only when all its runs are finished | After that date a return starts a new run, so this run can no longer grow. A day trip, or a phone that has not synced, does not finish a stay. A stay extended later leaves a stale header that only `sync-header`, a delete and recreate, fixes | Too strict: the newest trip waits. Too loose: every id in the trip changes later | Q5 |
| 6 | Existing visits, `trip_id` ignored, every row | Same full dates: dropped, named. Any shared day, or within `gap_days` of the run: held, with the visit id. Year-only or one-ended and its years include the run, or no date: held until the owner answers "a different visit". Same against other runs in the plan | `doubles` compares only equal full dates (`plan/apply.py:357`), so overlaps are the planner's work. An undated record may be this very visit | Too loose: a duplicate. Too strict: a repeat visit waits for one answer | R |
| 6a | Which visits are read | `client.snapshot(candidate regions)`, by explicit id | `snapshot(None)` reads only marked regions (`api/client.py:1646-1647`), and one whose only visit is a transit is not marked | A transit is written twice | R |
| 6b | Existing trips whose header overlaps a planned one | Shown with id and dates; the planned trip is held until the owner answers "a separate trip". Nothing is added to an existing trip | Adding to a trip is `update-trip`, a delete and recreate | Two trips for one journey | R |
| 6c | A visit that ends before the photos do | Reported. Never an `update_visit` | Moves the visit, not the trip's header | None | R |
| 7 | Quality | Asked per run, 1 to 6 with the site's words, nothing selected, no "all the same" button. 0 is not offered: "not a visit" is its own answer and never an op. Beside 1: "stored, but does not mark the region" | 0 is no visit; 1 does not mark (`api/client.py:85`). A bulk button is a default under another name | Slower page | Q6 |
| 8 | `segment()` | Not used to build trips, and not changed. It stays for `sweep`, the option table | It gives a region one span, first day to last, over days elsewhere or silent (`plan/segment.py:252-259`), and cuts inside a stay (`244-249`). Both break rule 1 here, and `sweep` users rely on both | Two ways to group exist; the docstring must say which is for writing | R |
| 8a | `split_first_and_repeat()` | Not used under Q2's recommendation. If "split": add `seen=` with every region that has a visit, and take the header from the regions left | `seen` starts empty (`plan/segment.py:349`), so a region on the profile becomes "first"; the header keeps days whose regions were removed (359) | First visits written a second time | Q2 |
| 8b | Home | `away_test` and `HomeWindow` are reused. Home is `None` or the owner's file, never `"infer"` (the default, `plan/segment.py:212`) | Inferred home removes the most visited region. A settings home is a hint | A home day inside a trip: shown as a `home` cut | R |
| 9 | One region twice in a trip | The second run is held with the reason, or the owner cuts the trip between them | `validate` refuses it (`plan/model.py:394-395`), and the server's answer was not measured | Some real repeats wait | R |

## Input

- **Photos.** Own camera by default; `--origin` widens it, each origin is graded alone, and every run names its origins. Runs are built over the whole library and then selected by `--since` and `--until`; a run across either date is held, not cut. `grade_shots(until=...)` is not used: a late shot gets `use` `none` (`plan/review.py:295-299`), so it could not finish a stay, spoofed or not; but the specific reason it was left out, its span or its speed, is lost behind "after the date".
- **Other sources (rule 6).** `--track FILE` or an explicit `--no-track`, recorded in the manifest with each source's first and last day. A track row in another region on a run's day flags `sources disagree` and holds the run. A day-level row never makes, extends or finishes a run: it has no clock.
- **Profile.** Strict reads only. Account id, `regions()` live, visits per decision 6a, trips of every year touched. A missing list stops the command.
- **No words from mail, tickets or place names.**

## Spoofed and frozen coordinates

Use `grade_shots` and `--spans` as they are. Only `kind == "ground"` with `use == "all"` is proof. Runs show `fast_near`, and flags that leave nothing out:

- `conflict`: an impossible photo of one of its days is in the region.
- `one_spot`: two days or more, one raw coordinate.
- `thin`: `evidence.grade` says so. Say it at plan time (AGENTS 6a); never "ready".
- `clock?`: an edge photo whose offset is 0, or over 3.5 h from its longitude. A missing zone is read as UTC (`sources/photos_app.py:124`), so an edge day may be one off.

What none of this sees: a photo in the air hours from both neighbours (`plan/review.py:233-235`). Alone it is `thin`; with other photos in the region it may evade both checks, and then only the owner's answer stops it. Only needed change: `Shot.uuid: str = ""`, last field, so that a run can name its photos and reuse `evidence.grade`.

## Unread tiles and stale ids

- `tile_error` is not a place and not "nothing there". A run with one on any of its days, or within `gap_days` of an edge, is held as `not read`; so are the trips either side of it. Counted in the page header; exit 3.
- Resolve raw coordinates with `from_tiles`. No geocoder, and no coordinate cache: `Resolution.confident` accepts `geocoder` (`geo/resolve.py:45`), and old caches hold it. Tile bytes are cached as today.
- A point inside a polygon whose id is not in the catalogue falls through to the nearest neighbour (`geo/resolve.py:147-149, 158`). Not inside, so no run; but it must be reported as `stale map` and hold its days, or a silent day looks empty.

## The page and the answers

Runs in time order, grouped by the current cuts. Per run: region, dates, photo days, photos, hours, origins, flags, evidence grade, existing visits. Between runs: the proposed cut and its fact. Held items in their own list with one reason each.

Answers file: format, account, manifest sha256, and by stable id: run (`1`..`6`, `no`, `?`), cut (`cut`, `same`), doubtful visit (`different`), overlapping trip (`separate`), open stay (the owner's text). Blank is absent and never means no. Anything else is refused, as in `load_answers`. The run id is region and both dates, so a run that changed has no answer.

## The plan file

`plan` keeps a trip when every run in it has an answer, the cuts on both sides are answered and nothing in it is held. `no` runs are left out. A held run between two others holds that whole trip: leaving it out would write a trip with a hole.

- One `create_trip` per trip: header is min and max of its rows; every row has both full dates (`plan/model.py:411-415`), quality as answered, no `hidden`. Local dates may run backwards across the date line: use min and max, never swap silently.
- `proof`: one entry per run, region id first in `detail`, since a row may carry no other field (`plan/model.py:243`). Owner's words are `memory`. `confidence` stays 0.0.
- `basis`: fingerprint of `basis_of(snapshot, regions)` over exactly the planned regions, plus `taken_at`. Moving an op to `review` by hand makes `apply` refuse with drift only if it changes that set (`plan/apply.py:585, 632, 652`), which it does not where the other ops touch the same regions: do not rely on it, change the answers and rebuild.
- `params`: gap, speeds, window, home file hash, the sweep rows. `sources`: manifest, answers, spans, stays and track hashes.
- `plan` prints trips, regions, how many will and will not mark a region, and what was held. One `create_trip` counts as one write under `max_writes`, whatever its size (`plan/apply.py:660`), so `plan` refuses over 50 region rows without `--max-rows`.

## What it never does

- Write, update or delete anything; call `sync-header`; produce any op but `create_trip`.
- Name a region no photo is strictly inside, except as decision 2b reports on the profile itself.
- Draw a line, corridor or route between two runs, or fill a day with another region.
- Suggest a date, a quality, or "you were there until". Dates are photo days.
- Treat no photo as no visit, or a gap as travel.
- Use an inferred home, a day-level row or somebody else's photo as proof.
- Split, shorten or merge what is already on the profile.

## AGENTS.md rules it must hold

0.1 (decisions 1, 2), 0.2 (never), 0.3 (review, plan, apply apart), 0.5 (one op kind, so one approval), 0.6 (`--track` or `--no-track`), 0.7 (1a, 2b, 5), 4 (unread tiles), 5 (quality, finished stays, transit), 6a (grade shown before the claim), 7 (basis, drift, verify). AGENTS 5 needs one new sentence if Q2 is approved; README trap rows for decisions 1, 2a and 6a.

## Tests (`tests/test_trips.py`, fake resolver, no shapely needed)

1. Runs: one silent day joins, two do not; a ground photo elsewhere on the silent day splits; a same-day excursion does not; first and last day are photo days; fast, conflict, untimed, `use` `none` and `airports` shots make nothing; photo order does not matter; a date-line flight.
2. Places: nearest-only never makes a run; `edge`; tile error holds its neighbours and exits 3; stale id is `stale map`; raw coordinate, not the rounded key; an old `geocoder` cache entry is not read.
3. Finished: a later photo within `gap_days` is not enough; a later photo in a span, fast, or of another origin is not; text finishes and lands in `memory`; one open run holds the trip.
4. Overlap: full, partial, touching, year-only, one-ended, year 0, undated, trip-owned and standalone; a transit-only region's visits are read; overlapping header; a run against another run.
5. Trips: no cut inside a run; blank cut holds both sides; a region twice; `home=None`; a held run in the middle holds the trip; header equals its rows.
6. Quality: nothing preselected; absent, 0 and 7 refused; `no` and `?` never an op; transit counted as not marking.
7. Binding: other hash, account, format, unknown id, duplicate key refused; hostile region names inert; no network on the page.
8. Plan: `validate(plan) == []`; fingerprint equals a fresh snapshot's; only `create_trip`; no write sent; no cached client; second `plan` after `apply` is all "already there".
9. Rule 7: rendered page and stdout of a library with a hole name no region, day or route in the hole.

## Tasks

| Task | Size | Depends |
|---|---|---|
| T1 move manifest helpers to `plan/manifest.py`; `Shot.uuid`; no behaviour change | S | Phase 2 merged |
| T2 places: raw resolve, `edge`, `stale map`, `not read` | M | T1 |
| T3 runs, flags, finished, `--stays` | M | T2 |
| T4 overlap against visits and trips (decision 6) | M | T3 |
| T5 cuts and grouping; docstrings in `segment.py` | S | T3 |
| T6 manifest, page, answers | L | T4, T5 |
| T7 `trips review`, `trips plan`, docs, AGENTS, README | M | T6 |
| T8 Codex and Grok review; read-only run on a real library; first apply is one finished trip, read back by the owner | S | T7 |

S is under half a day, M a day, L two days.

## For the owner to decide (1, 2, 4 and 6 answered as recommended)

1. **Gap.** (answered: as recommended) May one silent day inside a region join two photo days? Recommended: yes, `gap_days` 2; the table shows 1, 2, 3, 5 before any answer.
2. **First visits.** (answered: as recommended) One trip with every new region, or first visits apart and only repeats in trips (the rule AGENTS 5 has now for backfills)? Recommended: one trip, only for regions with no possibly overlapping visit; keep the split for backfills over existing standalone visits.
3. **Ground speed for proof.** (open) 120 km/h as in series review, or 200 km/h as in `grade.py`? Recommended: 120, raised per run of the command for rail or motorway trips.
4. **Layovers.** (answered: as recommended) Held and not answerable, or answerable with a quality? Recommended: held; a real visit is listed in `--stays` in the owner's words.
5. **Owner's word that a stay is over.** (open) Typed text, or a button? Recommended: typed text, kept as `memory` proof; dates stay the photo days.
6. **Quality in bulk.** (answered: as recommended) One choice per run only, or also "same for this trip"? Recommended: per run only; look again after the first real use.
