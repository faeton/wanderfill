# Series review: Implementation Plan (Phase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** turn "photos near unticked series objects" into a local page the owner answers, and the answers into one plan file per series, without ever proposing a place no photo shows.

**Architecture:** `plan/review.py` is pure: photos, tiles and ticked lists in, a manifest out; a manifest and answers in, a plan out. `plan/review_page.html` is a static template. `cli/main.py` only loads inputs and writes files. Nothing here writes to the profile; the plan goes through `check` and `apply` like any other.

**Tech Stack:** Python >= 3.10 stdlib; `geo` extra for tiles, imported where needed. No new dependency.

**Spec:** this file and Phase 2 of `2026-11-24-write-safety-then-features.md`, which it replaces in detail. Built in the worktree `wanderfill-phase2` on branch `phase2`.

## Decisions

| # | Question | Decision |
|---|---|---|
| 1 | What is offered | Only unticked objects within the radius (default 2 km) of a **ground** photo |
| 2 | Which photos | `origins={"own-camera"}` by default; `--origin` widens it and every candidate names the origin of its proof |
| 3 | Fast photos | Speeds over the whole own sequence by `taken_utc`, before any place filter. A run of identical raw coordinates is one place, entered at its first photo and left from its last. Its speed is measured forward and back in time, each to the nearest place at least 1 km away. Nearer places are jitter and are looked past, so that a burst from a window is not, but only while all their photos are within 10 minutes (`burst_minutes`) of the place: from its last photo forward, from its first back. At a nearby photo farther off there is no speed in that direction, so a landing or departure and the minutes around it are fast and the stay is not. Over 150 km/h in either direction, every photo of the place is fast. A gap long enough to make a flight slow on average hides it, which is why the owner answers |
| 4 | Impossible photos | Measured as in 3 and with the same reach, over 1,200 km/h and at least 100 km means one end is wrong, and so does no time at all between two places at least 1 km apart. Both ends are `conflict`, which outranks fast: never proof alone, marked, and offered last: a series lists its candidates with no `conflict` and no `one_spot` (one raw coordinate on two or more days) first, and each part by distance |
| 5 | Owner's spans | `--spans FILE`: JSON list of `{from, to, bbox: [w, s, e, n], why, use}`; `use` is `"none"` (spoofed, not theirs) or `"airports"` (layover: proof for series 5 only) |
| 6 | Unfinished stay | `--until DATE`, by the photo's own clock |
| 7 | Answers | `y`, `n`, `q`; nothing preselected; blank is not exported and never means no; `n` never becomes an op |
| 8 | Binding | Answers carry the manifest's sha256, account and format; a different manifest refuses them |
| 9 | Plans | One series per plan file. `confidence` stays 0.0; evidence is typed in `Op.proof` |
| 10 | No list without photos | The page and the CLI never print unticked objects that have no photo, not even the nearest |
| 11 | Named places | `series named` takes one exact catalogue name and the owner's words, prints no suggestions, proof kind `memory` |
| 12 | Privacy | Output holds coordinates and dates: default directory `review/`, git-ignored; page has no network access, not even a map link |

## Global Constraints

- All of Phase 1's. In particular: no cache on this path, `OpKind` unchanged, tick state is always 1, `Op.key` unchanged by `proof`.
- The suite never touches the network, a token, the home directory or a Photos library.
- A failed tile is incomplete coverage: counted, named in the page header and in the manifest, never "nothing there".
- Commits on branch `phase2`, one per task, no AI attribution. Never push.

## Review Focus

1. The owner answers on one day, the photos change, the page is rebuilt, the old answers are used: refused.
2. An object name contains `</script>`, quotes, or non-Latin text: the page works and the text is inert.
3. Two tabs or a reload: answers kept per manifest, never mixed with another page's.
4. A tile times out: the page says coverage is incomplete instead of looking finished.
5. A photo is in a spoofed span or fast: it never becomes proof, and the page says how many were left out and why.

---

## Task 10a: candidates

**Files:** create `src/wanderfill/plan/review.py`, `tests/test_review.py`; modify `plan/model.py` (`Op.proof`).

**Interfaces produced:**
- `Op.proof: list[dict] = []`, not in the key; `validate` accepts only a list of objects with string `kind` in `{"ground_photo", "fast_photo", "document", "memory"}` and string `detail`.
- `Span(first: date, last: date, west, south, east, north, why: str, use: Literal["none", "airports"])`; `load_spans(path) -> list[Span]`, raising `PlanInvalid` on anything malformed; a box with `west > east` crosses the antimeridian.
- `Shot(taken, taken_utc, lat, lon, origin, kind: Literal["ground", "fast", "conflict", "untimed"], use: Literal["all", "airports", "none"], late: bool = False)`; `untimed` has no UTC time and its `use` is always `"none"`; `late` is after `until`
- `grade_shots(assets: Iterable[PhotoAsset], spans: Sequence[Span], *, until: date | None = None, fast_kmh=150.0, min_leg_km=1.0, impossible_kmh=1200.0, impossible_km=100.0, burst_minutes=10.0) -> list[Shot]`
- `Candidate(series, item, series_name, name, lat, lon, km, at, photos, days, origins, fast_near, conflict, thin, one_spot)`; `one_spot` means two or more ground photos on two or more days, all with one raw coordinate: a flag, not an exclusion
- `harvest(shots, reader, *, radius_km, zoom=9, on_progress=None) -> tuple[dict[tuple[int, int], SeriesPoint], int]` (objects by (series, item), count of tiles that could not be read)
- `find_candidates(shots, objects, catalogue, ticked, *, radius_km=2.0, skip=SKIP_SERIES) -> list[Candidate]`; `SKIP_SERIES = frozenset({-2, 12, 17, 60})`
- `recall(shots, objects, ticked, radii=(0.2, 0.5, 1.0, 2.0), *, catalogue) -> dict[int, dict]` per series: ticked total, ticked with an object, found per radius, ticked with no photo near. Like `find_candidates`, it raises `PlanInvalid` for a catalogue series with no entry in `ticked`: a list that was not read is not an empty one.

`km` is to the nearest ground shot; `photos` and `days` count ground shots within the radius; `thin` means one photo, or farther than 1 km.

- [x] **Tests:** one per rule in Decisions 1-6 and 10; plus: `taken_utc` missing means the photo cannot be shown to be slow, so it is not ground; two photos 4 s and 60 m apart are ground; a frozen run before a jump is fast, every photo of it; a shot exactly at `--until` counts, the next day does not; a date-line flight is ordered by UTC; antimeridian span and radius; an object ticked under a string id is not offered; a skipped series is not offered and is counted; an object with only fast or conflict shots is not offered; the same item id in two series are two candidates; a failed tile is counted and the rest still offered; order is stable.
- [x] RED, implement, GREEN, ruff, commit `series: candidates from photos on the ground`.

## Task 10b: the page and the answers

**Files:** create `src/wanderfill/plan/review_page.html`; modify `plan/review.py`; test `tests/test_review.py`; `.gitignore` gains `review/`.

**Interfaces produced:**
- `Manifest(account: int, created: str, params: dict, coverage: dict, left_out: dict, candidates: list[Candidate], format: int = 1)`; `Manifest.sha256 -> str` over canonical JSON (sorted keys, no whitespace, `ensure_ascii=False`) of everything but `created`; `save(path)`, `load(path)` raising `PlanInvalid`.
- `render_page(manifest) -> str`
- `load_answers(path, manifest) -> dict[tuple[int, int], Literal["y", "n", "q"]]`; file shape `{"format": 1, "account": int, "manifest": sha256, "answers": {"<series>:<item>": "y"}}`.

The page: candidates grouped by day and region-free place (series, then distance), filters by series, distance and answered; per item three buttons and the facts (distance, local time, photos, days, origin, `thin`, `conflict`, fast photos near); a header with account, dates, radius, what was left out and why, unread tiles, and recall per series; counts of y / n / q / blank; download and copy of the answers file. Plain English. A Content-Security-Policy meta of `default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'`. Keyboard operable, light and dark. State in `localStorage` under the manifest hash, and a working page if storage is unavailable. Data goes in as JSON with `<`, `>`, `&`, U+2028 and U+2029 escaped, and is written with `textContent`.

- [x] **Tests:** nothing preselected; hostile names inert; no `http`, `//`, `fetch`, `import`, `src=`, `href=` in the rendered page outside the data; the CSP present; hash independent of `created` and of candidate dict order, dependent on every other field; load refuses an altered candidate; answers refuse: other hash, other account, other format, unknown item, duplicate key in the JSON, a value other than y/n/q, not an object; blank items are absent; unread tiles and left-out counts appear. If `node` is available a test runs the page's own export function; otherwise it is skipped, and the export logic is also covered by a pure-Python check of the embedded data.
- [x] RED, implement, GREEN, ruff, commit `series: a page to answer, and answers bound to it`.

## Task 10c: the commands and the plan

**Files:** modify `cli/main.py`, `plan/review.py`, `README.md`, `AGENTS.md` section 6, `docs/nomadmania-api.md` if needed; test `tests/test_review.py`.

**Interfaces produced:**
- `plan_for_series(manifest, answers, series_id, client) -> Plan`
- `plan_for_named(series_id, name, said, client) -> Plan`
- CLI: `series review --since DATE [--until DATE] [--radius KM] [--origin NAME]... [--spans FILE] [--series ID]... [--library PATH] [--out DIR]`; `series plan MANIFEST ANSWERS --series ID [--plan FILE]`; `series named --series ID --name TEXT --said TEXT [--plan FILE]`.

`review` prints recall per series, counts by distance, coverage and what was left out, then the paths; exit 0, or 3 when a tile could not be read (files still written). `plan` re-reads the account and the series strictly, drops items already ticked and names them, refuses an item no longer in the series, writes a plan with `basis` (fingerprint, `series_ticked`, `taken_at`), `sources` (manifest and answers sha256, origins), `params`, `proof` per op, every op in bucket `apply`, and prints "one series; read it with `show -v`, then `check`". `named` matches the exact catalogue name, case- and space-sensitive; no match or more than one refuses without printing any other name.

- [x] **Tests:** review writes only its two files and sends no write; plan covers exactly the `y` items of one series; another account refused; a series id absent from the manifest refused; an item gone from the live series refused; already ticked dropped and named; `--series` required; fingerprint equals `fingerprint(basis_of(client.snapshot([]), []))`; `validate(plan) == []`; `confidence == 0.0`; `named` refuses near matches and prints no catalogue name; `--said` empty refused; the importer column missing says so and exits 2 instead of writing an empty page; a library path that does not exist is a plain error; no cached client.
- [x] RED, implement, GREEN, ruff, docs, commit `series: review, plan and named`.

## Task 11: airports

**Files:** modify `plan/review.py`, `plan/series.py` docstring, `README.md`; test `tests/test_review.py`.

- Series 5 shots may use spans with `use: "airports"`; no other series may.
- A fast shot never offers an airport and the nearest airport to one is not inferred; `fast_near` is shown as a fact only.
- `thin` sorts last, after `conflict`-free candidates; the page groups thin airports under their own heading.
- `recall` for series 5 is printed first, with ticked airports that have no photo near, so nobody reads the list as complete.
- `match_series` is unchanged. Its docstring says the 25% was a day-level measurement, that per-photo distance can do better, and that either is recall, not precision.

- [x] **Tests** for each line above. RED, implement, GREEN, ruff, commit `series: airports by photos on the ground`.

## Task 12: trip planner spec

No code. `docs/superpowers/specs/2026-11-24-trip-planner-design.md`, answering the list in Phase 2 of the first plan, with each open choice stated as a recommendation and its cost. The owner decides before any code.

## Gate

- [x] Suite and ruff on Python 3.10 and the venv's Python; whole-change review by Codex and Grok; findings fixed.
- [x] The controller runs `series review` read-only on a real library into a private directory, opens the page, and runs `series plan` and `check` on a synthetic answer without applying. Results stay out of the repository.
- [x] Fast-forward `main`; push only on the owner's yes.

## Not building

Auto-marking; a list of unticked objects without photos; map links or tiles in the page; mail parsing; a 2-10 km city rule; un-ticking.
