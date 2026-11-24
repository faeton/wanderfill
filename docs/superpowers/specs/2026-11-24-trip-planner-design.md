# `wanderfill trips`: photos to a trip plan

Status: built, 2026-11-24. Second version, after design reviews by Codex and Grok. The owner's answers: one silent day joins; all new regions of a journey in one trip; 150 km/h; layovers held; a stay is over by a later photo elsewhere or the owner's typed words; quality per region stay.

## Purpose

Turn the owner's photos into questions ("these photos are inside this region on these days") and the answers into one plan file of `create_trip` ops for `check` and `apply`. The planner sends no write.

The dangerous error is claiming too much: a region, a day or a visit that did not happen, or one that is already on the profile. A missed visit can be added later; a wrong one only the owner can remove. So every unclear case is **held**: shown with its reason, and not in the plan unless the owner releases it in their own typed words, where a release is possible at all.

## Commands

```
wanderfill trips review --since DATE [--until DATE] [--gap-days N] [--fast-kmh N] [--spans FILE]
        [--no-flight-days] (--track FILE... | --no-track) [--library PATH] [--out DIR]
wanderfill trips plan LIST ANSWERS [--plan FILE] [--max-rows N]
```

`review` writes a list and an offline page into `review/trips/`, prints the gap table, what was left out and why, and the paths. Exit 0, or 3 when a tile or a row of a track could not be read (both files are written, and say so), or 2 on an error. `plan` reads the profile again and writes one plan file. It refuses with 1 what cannot be used (a list, answers, too many rows: `PlanInvalid`) and with 2 another account, a profile that is not as the list shows it, and any other error.

Three words are kept apart: an **observation** is one photo; a **run** is days of photos inside one region; an **episode** is one stay. Only the profile and the owner know episodes.

## Decisions

| # | Question | Decision | Cost if wrong |
|---|---|---|---|
| 1 | Which photos | Own camera only. No `--origin` in this version: somebody else's photo is not evidence. The whole library is graded, then runs are selected by `--since` and `--until` (a run across either date is held) | Years whose photos carry no importer cannot be planned |
| 2 | Proof | A shot that is `ground`, `use` `all`, strictly inside a region, with a clock that can be trusted | Some real photos do not count |
| 2a | Strictly inside | `tile_interior` from the raw coordinate, and the eight points 100 m around it are in the same region or in none. One in another region, or not read, makes it `edge` | Tiny regions may have only `edge` shots |
| 2b | Trusted clock | A recorded zone, from -12 h to +14 h, at most 4 hours from longitude / 15 (modulo 24). Otherwise the shot can be on the day before or after, and is not proof | A photo with a wrong zone is ignored |
| 3 | Run | The days with proof in one region. Two proof days join when every day between them is **compatible** (it has own-camera photos, all of them `ground`, `use` `all`, and placed in this region by interior, edge or nearest polygon) or **silent** (no own-camera photo of any kind, one with no coordinate included), with at most `gap_days` - 1 silent days in a row (default 2, so one). Anything else on a day between, a fast, conflicting, untimed, unplaced, unread or other-region photo included, or one with no coordinate, is a barrier | One stay shown as two runs; they are then a possible continuation (6), not two visits |
| 3a | Dates | First and last proof day, or the flight day beside it (16). Silent days inside a run are listed on the page as days with no photo | A day too short at either end |
| 4 | Finished | A proof shot in another region on a later day than the run's last. No later run of the same region can then join it. Otherwise the run is **open** | |
| 5 | Left | Two runs, or a run and a visit, of one region are separate episodes only if a proof shot in another region lies between them. A visit has no times: its last day counts as over two hours after midnight by the run's clock, and its first day as begun two hours before midnight. Photos that stop and start again prove nothing about leaving | A real return is held when the camera saw nothing between |
| 6 | Existing visits, every row of the region, trip-owned or not, transits too | Full dates equal: **already there**, dropped and named. A shared day: **overlap**, held. Not separate by 5: **continuation**, held. Year-only or one-ended (an absent end is open, never the same year) whose range can share a day with the run and shares none for certain, or no date at all: **undated**, held, released by words that say why it is another visit. Named: every overlap, the nearest continuation on each side, the first three undated and how many more | A repeat visit waits |
| 6a | Reading them | `client.snapshot` with the candidate regions by id, strictly; a transit-only region is not in the marked list | |
| 6b | Existing trips over the same days | Shown on the page, not a hold: no region is written twice because of 6. Trips with no dates are not shown; a visit inside one is still seen by 6 | Two trips for one journey |
| 7 | Held, and whether words release it | By words: **open**, **brief** (one photo, or all proof within 15 minutes, or on one raw coordinate), **layover?** (under 24 hours with a fast shot within 6 hours before and after), **undated**. Never: **overlap**, **continuation**, **not read** (an unread tile on a day of the run, or between its days or beside them), **stale map**, **across --since or --until**, **clock** (two clocks over 12 hours apart among its photos, because words cannot correct a date) | Never-released runs need another way: by hand, or a later review |
| 7a | Answers to a run | Quality 1 to 6 with the site's words, `no` (leave it out), `?`. Nothing selected. A held run that words can release takes a quality and words; one that nothing releases takes only `no` or blank | Slower |
| 7b | Flags that hold nothing | `conflict` (an impossible photo of one of its days in the region), `evidence thin` (`evidence.grade`), `fast near`, `track` (a day-level row in a region that has no photo that day). A flight day (16) is said on its own, with its photos | |
| 8 | Trips | Offered runs in time order. Between every two neighbours the page has a control: same trip, or a new trip starts here. It is blank, and must be answered, where there is a suggestion: a fast leg or fast shots between them, more than `gap_days` days between them, or a run already on the profile between them. Elsewhere it stands at same trip, and can be changed | A wrong cut makes two trips of one, or one of two; no region or date changes |
| 8a | Forced cuts | Before and after a run that is already there, and an overlap that is left out, whatever was answered; and an open run with no words never joins the trip before it | A journey written in parts stays in parts |
| 8b | A region twice in one trip | That trip is not planned, and the page says which two runs, so that the owner cuts between them | `create_trip` takes a region once |
| 9 | Which trips are planned | Those whose every suggested boundary is answered, and whose every run has an answer that counts: a quality where it is allowed, or `no`. `no` runs are left out, then the rest is the trip. A `?` or a blank holds the trip. No runs left: no trip | |
| 10 | Header | Min and max of the rows that are in it, without the flight days that were left out. Every row has both full dates, its quality, and no `hidden` | A left-out day inside stays inside the trip |
| 11 | Binding | The list's hash covers every run, every reason, and every visit row and trip header that was shown. `plan` reads them again, the trip headers for the years from one before the first run to one after the last; a region whose visits differ from the list refuses, and so does a changed trip over the same days: run `review` again | Re-review after an edit on the website |
| 12 | Sources | `--track FILE` (repeatable), or `--no-track`, which says there is none for these dates. Every distinct place of every day in it counts, not one a day. The list records each source, its first and last day and how many of its rows could not be read. A day-level row on a silent day in another region is a barrier (3); on a photo day it is only the `track` flag | |
| 13 | Sweep | The gap table counts runs, suggested cuts and held runs with this algorithm at 1, 2, 3 and 5 days. `segment()` is not used and not changed | |
| 14 | Stale map | `Resolution.stale`: the point is inside a polygon whose id is not in the catalogue. Not a region, not silent | |
| 15 | Profile | `add_visit` is never planned (the server wraps it in its own trip). `create_trip` only, one kind, one approval. AGENTS section 5 gets the sentence that a journey whose runs overlap no existing visit goes into one trip, first visits included | |
| 16 | Flight days | One local day beside a run, at either end and at most one at each, with a `fast` or `conflict` own-camera shot, `use` `all`, strictly inside the run's region, with a trusted clock: after the run's last proof in UTC and before any proof elsewhere, or before its first and after any. Every day between is silent, fewer than `gap_days` of them. A day in another run of the region, or that another could also have, is neither's. `first` and `last` then have it; proof, `days`, **brief**, **layover?** and the grade do not change, and everything that goes by dates goes by the wider ones. It is in unless the owner leaves it out on the page; that judges nothing again and so releases nothing. `--no-flight-days` turns it off for a list | After an overnight flight with a stale position, a day too many, which is shown and can be left out. Not asked: a photo in the air over a region that they had left by land, with no photo elsewhere |

## The page and the answers

As the series page: one local file, CSP, no link, nothing preselected, no bulk button, data as escaped JSON through `textContent`, one storage key per answer under the list's hash, download, copy, show. Runs in time order; the boundary control between them; already-there and never-released runs in place, greyed, so that the order is true. Per run: region, dates, photo days, silent days, photos, first and last photo time and the local clock's zone, evidence grade, flags, reasons, the existing visits of the region. Beside quality 1: "stored, but does not mark the region". A text field only where words are needed; 500 characters, control characters refused. The page shows the trips the answers would make, and why a trip is not made.

Answers: `{format, account, manifest, runs: {id: {"a": "1".."6"|"no"|"?", "words": text, "out": ["YYYY-MM-DD"]}}, cuts: {id: "cut"|"same"}}`. Run ids are `region:first:last`, cut ids are the two run ids. `out` has flight days of that run, in order, where a quality is taken; blank means that the day is in. Blank is absent. Duplicate keys, unknown ids, words where none are taken, a quality where only `no` is taken: refused.

## The plan

`proof` has one entry per run, `kind` `ground_photo` with what the page showed, region id first; the owner's words are a second entry of kind `memory`, verbatim. `confidence` 0.0. `basis`: fingerprint of `basis_of(snapshot, regions)` over exactly the planned regions, and `taken_at`. `params`, `sources` with the hashes of list, answers, spans, tracks. `validate(plan) == []`. `plan` prints trips, regions, how many will mark a region and how many will not, what is held and why, and refuses more than 50 region rows without `--max-rows`.

## What it never does

Write, update or delete; call `sync-header`; plan anything but `create_trip`. Name a region no photo is strictly inside, except existing visits. Draw a route, fill a day, suggest a date, a quality, or that a stay continued. Take no photo for no visit, or a gap for travel. Use somebody else's photo or a day-level row as proof. Join, split or shorten what is on the profile.

## Accepted limits

A photo in the air hours from its neighbours on both sides grades as ground; `brief` catches a lone one, the owner the rest. A layover longer than a day, or with no fast shot near, is not detected. A trip header changed on the profile between `plan` and `apply` is not seen by `apply` (its regions' visits are). A phone that has not synced makes a run look open or short.

A GPX or GeoJSON time in UTC dates its row by UTC, and the local day can be the one beside it. A photo with no coordinate is dated by its own clock, which nothing checks. A run on the profile that was written without its flight day is an overlap now, and not already there. A visit dated by the year, that can have the flight day, makes the run **undated** where it was a **continuation**.

## Not in this version

`--origin`, joining runs by hand, a home file, a 30-day cut, first visits apart from repeats, exceptions for an overlap, update of an existing visit or trip.
