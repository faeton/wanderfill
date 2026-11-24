# Trip planner implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** `wanderfill trips review` and `trips plan`: from the owner's photos, through a page the owner answers, to one plan file of `create_trip` ops.

**Architecture:** pure computation in `plan/trips.py`; list, page and answers in `plan/trip_list.py` and `plan/trips_page.html`; shared file binding in `plan/manifest.py`; the CLI only wires. Nothing here writes to the server.

**Tech Stack:** Python 3.10 stdlib, existing `geo` extra for tiles.

**Spec:** `docs/superpowers/specs/2026-11-24-trip-planner-design.md`. Decision numbers below are its.

## Global Constraints

- Core stays stdlib-only; extras imported inside the function that needs them. Python 3.10, line length 100.
- OpKind gains no kind; UNDO_METHODS stays at five; op keys, plan schema and journal format unchanged. `plan/apply.py` is not changed.
- No cache reaches planning. No new endpoint. The suite never touches the network, a token or the home directory.
- The repo is public: no text or test value describes a real person's profile, trips, places, dates or ids.
- AGENTS.md rules 1, 6 and 7: no interpolation, every source, nothing proposed.
- Docs and docstrings are not left untrue. Each API trap found gets behaviour, a test in `tests/test_quirks.py`, and a README trap row.

## Review Focus

- A two-week journey, five regions, one silent day, a same-day transfer, a slow-looking flight home to an open stay: one five-region trip after the owner's answers (Task 4's acceptance test).
- A region already visited and undated, revisited: held until words, then planned once.
- A journey half written already: the written runs are dropped, the rest is cut around them.
- Photos after a long hole in the same region: continuation, never a second visit.
- A day-border photo with zone 0 at longitude 150: no proof of a date.

## Task 1: foundations

**Files:** create `plan/manifest.py`; modify `plan/review.py`, `sources/photos_app.py`, `geo/resolve.py`; tests beside the existing ones.

- `plan/manifest.py`: `canonical`, `sha256_of`, `fields`, `whole`, `number`, `text`, `date`, `read_json`, `read_bytes`, `json_of`, `escape_data` moved out of `review.py` under public names; `review.py` uses them; no behaviour change, series hashes byte-identical (pin one in a test before moving).
- `PhotoAsset.zone: int | None = None` (last field), seconds, `None` when the library has no offset; `zone_fits(zone, lon) -> bool` in `sources/photos_app.py` (decision 2b).
- `review.Shot` gains `uuid: str = ""` and `zone: int | None = None`, last fields, carried by `grade_shots`.
- `Resolution.stale: bool = False`, set by `from_tiles` when a polygon with an id outside the catalogue covers the point and none inside it does; `resolve_many` keeps it; saved coordinate maps unchanged. Nothing existing behaves otherwise.

- [ ] RED, implement, GREEN, ruff, commit `trips: foundations`.

## Task 2: runs, reasons, boundaries

**Files:** create `plan/trips.py`, `tests/test_trips.py`.

**Produces:** frozen dataclasses and pure functions:
- `Place(region: int | None, state: Literal["inside", "edge", "nearest", "nowhere", "unread", "stale"])`; `place(resolve: Callable[[float, float], Resolution], lat, lon) -> Place` (2a; neighbours asked only for an interior point).
- `Obs(shot: Shot, place: Place)` with `proof: bool` (2, 2b).
- `Run(region, first, last, days: tuple[date, ...], silent: tuple[date, ...], shots: tuple[Obs, ...], reasons: tuple[str, ...], flags: tuple[str, ...], status: Literal["offered", "held", "fixed", "there"])` with `id`; `held` is released by words, `fixed` by nothing.
- `find_runs(obs, *, gap_days=2, track=()) -> list[Run]` (3, 3a); `judge(runs, obs, visits, *, since, until) -> list[Run]` (4-7b; `visits: dict[int, list[Visit]]`); `boundaries(runs, *, gap_days, obs) -> list[Boundary]` with `Boundary(before, after, suggestions: tuple[str, ...], forced: bool)` (8, 8a); `sweep(obs, visits, ...)` (13).
- `group(runs, boundaries, answers) -> tuple[list[TripDraft], list[NotMade]]` (8-10).

- [ ] **Tests:** one per decision row, boundary values included; order of photos does not matter; every barrier kind; coast days compatible; continuation against a run and against a visit; transit-only, year-only, one-ended, year 0, undated; date-line clocks; open, brief, layover; region twice; `no` in the middle; forced cuts.
- [ ] RED, implement, GREEN, ruff, commit `trips: runs, reasons and boundaries`.

## Task 3: the list, the page, the answers

**Files:** create `plan/trip_list.py`, `plan/trips_page.html`, `tests/trips_page_dom.js`; modify `tests/test_trips.py`.

**Produces:** `TripList` (as `Manifest`: hash, `save`, `load`, refuses anything altered), `render_page(trip_list) -> str`, `load_answers(path, trip_list) -> Answers`. The page's grouping and the Python `group` are the same rules: a test runs both on the same answers through `node` and compares.

- [ ] **Tests:** as Task 10b of the series plan, plus: text fields only where words are taken; quality 1's note; greyed runs take no quality; the preview agrees with `group`; hostile region names and words inert.
- [ ] RED, implement, GREEN, ruff, commit `trips: a page to answer`.

## Task 4: the commands, the plan, the docs

**Files:** modify `cli/main.py`, `plan/trips.py`, README, AGENTS.md (5, 6a, 7, 10), `docs/nomadmania-api.md` if needed; `tests/test_trips.py`.

- `plan_for_trips(trip_list, answers, client) -> Plan` (11, plan section). `trips review`, `trips plan`; files as `series review` (0600, hash in the name, no partial pair, no replacing a plan).
- README: a section with one made-up journey. AGENTS.md: how an agent runs it, hands the page over and stops; never answers, words or cuts for the owner; which reasons words release; `evidence` before the claim; first visits in one trip; the module table in section 10.
- The acceptance test in Review Focus, from photos with a fake resolver to `validate(plan) == []` and `apply_plan` against the fake transport.

- [ ] RED, implement, GREEN, ruff, docs, commit `trips: review and plan`.

## Gate

- [ ] Both Pythons, ruff; whole-change review by Codex and Grok; findings fixed once.
- [ ] The controller runs `trips review` read-only on a real library and profile, checks that stays already on the profile come out as already there, and drives the page in a browser; a synthetic answer goes to `plan` and `check`, never `apply`; results stay out of the repository.
- [ ] Fast-forward `main`; push on the owner's yes.
