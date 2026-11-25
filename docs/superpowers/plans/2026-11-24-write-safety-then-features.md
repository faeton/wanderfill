# Write safety, then features: Implementation Plan

Status: built and pushed, 2026-11-24. The boxes below were not kept. Tasks 10 and 11 went through
`2026-11-24-series-review.md`, Task 12 through `2026-11-24-trip-planner.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Phase 2 does not start until Phase 1 is merged and reviewed.

**Goal:** make it impossible for `apply`, `delete` and trip re-spans to lose track of a write, then turn the one-off series-review scripts into a command.

**Architecture:** Phase 1 tightens the existing modules (`transport`, `client`, `plan/apply`, `plan/model`, `grade`, `photos_app`, `tiles`) without changing the plan-file flow. Phase 2 adds one module, `plan/review.py`, that ends in an ordinary plan file.

**Tech Stack:** Python >= 3.10, stdlib core, pytest, ruff. No new dependency.

**Spec:** the "Decisions" section below. Sources: a working session's findings, and independent reviews by Codex and Grok on 2026-11-24.

## Status of each claim

- **Reproduced:** trip key collision; frozen fixes graded `ok`; a write that failed with an error is closed and can be re-sent; a torn `open` line disappears; schema 99 loads; two adds for one region and date range with different qualities both run.
- **Read in code:** everything else in Phase 1.
- **Not verified:** what the server does when a `state=1` toggle is repeated. The client's docstring says it can undo the tick; treat that as true. Also the 82% airport recall figure (one session's count).

## Decisions

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | HTTP 5xx after a write | Unknown outcome; journal stays open | The server may have committed. Re-sending a toggle may undo it. |
| 2 | Resume a half-applied plan | No. Verify what landed, then re-plan | Both reviewers; later ops were not planned against that state |
| 3 | Series/KYE ticks in the drift fingerprint | No | Kills resume of long tick plans. Reading ticks before the write and skipping the ticked ones covers the risk |
| 4 | Trip keys | Change `create_trip` only; refuse old trip journals | Aliasing the old key could skip a corrected trip; ignoring it could re-send one |
| 5 | `sync_trip_header` | Own command with `delete` discipline, never an `Op` | The server deletes the trip and its visits |
| 6 | Frozen GPS | Collapse consecutive identical raw coordinates, then grade as before | No new threshold. Held, never rejected |
| 7 | Importer | Field plus opt-in filter; missing is `unknown`, never own camera | Older libraries mostly have no importer |
| 8 | Review answers | One plan per series; blank is never "no"; no confidence number | AGENTS.md rule 5; invented numbers look measured |
| 9 | City extension of 2-10 km | Not built | 0 of 45 offers were answered yes |
| 10 | Airport rule | Candidate ranking only | 82% is recall on ticked items, not precision |

**Owner decision needed before Task 7:** rule 2 says nothing is deleted except through `wanderfill delete`. `sync-header` needs a named exception in AGENTS.md. If the answer is no, skip Task 7's command and keep only its client refusals.

## Global Constraints

- Core stays stdlib-only; extras are imported inside the function that needs them.
- `OpKind` gains no removing kind; `UNDO_METHODS` in the tests stays at five.
- A write gets exactly one attempt. New write endpoints need a `WRITE_HINTS` match in the same commit.
- Plan files and journals on disk stay readable. Keys of every kind except `create_trip` stay byte-identical.
- No cache reaches planning, applying, checking or deleting.
- The suite never touches the network or a token.
- Each trap gets behaviour, a test in `tests/test_quirks.py` named after the incident, and a README trap row.
- Commit on `main`, one commit per task. Push only when the owner asks. Codex and Grok review before the push.
- Do not change journals or plans in `workspace/` while another session may be applying.

## Review Focus

1. Wi-Fi drops halfway through a 150-op plan: completed ops are verified and printed, the lost one blocks the next run, nothing is re-sent (Tasks 1, 2, 6).
2. The owner deletes a trip and re-applies the old plan file: refused with a message naming the old journal (Task 4).
3. Two terminals run `apply --confirm` together: the second refuses before any write (Task 2).
4. The server answers `{"result":"OK"}` with the list missing: apply refuses before writing; verify says `unproven` and exits 1 (Tasks 5, 6).
5. A tile fails to load on hotel Wi-Fi: no coordinate is assigned to a neighbouring region, and the failure is not cached (Task 8).

---

# Phase 1: safety

> **Status, 2026-11-24:** Tasks 1-6, 7 (refusals and docs only), 8 and 9 are done in `e150985..eeffe9f`. The whole change was reviewed by Codex and Grok and their findings fixed; 1088 tests, also on Python 3.10. The owner then approved the rule 2 exception, and Task 7's command and `expect_rows` followed in `019634f..f086a09`, reviewed the same way: 1221 tests. It has been run live only as a dry run. What was built differs from the text below where a review or a live read showed it to be wrong; the commits say where.

## Task 1: an uncertain write stays uncertain

**Files:** modify `api/transport.py`, `api/client.py`, `plan/apply.py`; test `tests/test_quirks.py`.

**Interfaces produced:** `NomadMania._readback(self, action: str, fn: Callable[[], T]) -> T` re-raises any `WanderfillError` that is not already `VerificationFailed` as `VerificationFailed(action, ...)`.

- [ ] **Tests** (monkeypatch `urllib.request.urlopen` as `tests/test_cache.py` does):
  - `test_a_500_on_a_write_is_an_unknown_outcome`: 500, 502, 503, 504 and 408 raise `UnknownWriteOutcome`; 400 and 429 raise `TransportError`; `urlopen` is called once.
  - `test_a_500_on_a_read_is_still_retried`.
  - `test_a_write_answered_with_html_is_an_unknown_outcome`: body `b"<html>"` and body `b"[]"` raise `UnknownWriteOutcome` from `webapi("quickEnter/add-visit")`.
  - `test_a_series_toggle_that_does_not_say_ok_leaves_the_journal_open`: after `apply_plan`, `Journal.unresolved() == {op.key}`.
  - `test_an_unexpected_exception_leaves_the_journal_open`: `_execute` raising `RuntimeError` leaves the entry open; `ApiError("quickEnter/add-visit", "Missing params")` closes it.
  - `test_a_delete_whose_readback_errors_stays_open`: `delete-visit` answers OK, the next `get-visits-to-region` is an `ERROR` reply; `delete_visit` raises `VerificationFailed`. Parametrize over all five undo methods.
- [ ] Run `.venv/bin/pytest -q tests/test_quirks.py`; expect the new tests to fail.
- [ ] **Implement:**
  - `_post`: for non-idempotent calls, HTTP >= 500, 408 or any 3xx raises `UnknownWriteOutcome`, and no redirect is followed; other HTTP errors keep `TransportError`. (Redirects added after Codex's review: a captive portal's answer passed for the server's.)
  - `webapi` and `ajax_json`: for a write, undecodable or non-object JSON raises `UnknownWriteOutcome`.
  - `_execute`: a non-OK toggle raises `UnknownWriteOutcome`.
  - `apply_plan` closes an entry with an error only for `PrecisionLoss`, `AuthError`, `TransportError` and `ApiError` that is not `VerificationFailed`. Everything else re-raises with the entry open.
  - Every read after the write in the five undo methods and `sync_trip_header` goes through `_readback`. `delete_trip` keeps treating exactly `Unauthorised.` as the missing-trip answer; `sync_trip_header` keeps its two accepted answers.
- [ ] Run the full suite and `ruff check .`; expect all pass. README row. Commit `writes: an error after sending is not proof of failure`.

## Task 2: one account, one journal truth

**Files:** modify `plan/apply.py`, `cli/main.py` (`cmd_check`, `_undo`), `api/errors.py`; test `tests/test_quirks.py`.

**Interfaces produced:**
- `class JournalCorrupt(WanderfillError)`
- `open_entries(dirs: Iterable[Path], account: int) -> dict[Path, set[str]]`: file to open keys, over `journal-<account>-*.ndjson` and `deletes-<account>.ndjson` in each existing directory.
- `account_lock(dirs: Iterable[Path], account: int) -> ContextManager[None]`: `fcntl.flock(LOCK_EX | LOCK_NB)` on `.lock-<account>` in each directory, in sorted order; held raises `DriftError`; without `fcntl`, print a warning to stderr and continue.
- `apply_plan(..., also: Sequence[Path] = ())`: extra directories. The CLI passes `(DEFAULT_WORKDIR,)`; tests pass nothing, so the suite never touches the home directory.

Why two directories: on 2026-11-24 the applies' journals were in `--workdir workspace` and the deletion journal in the default `~/.local/share/wanderfill`. Scanning one misses the other.

- [ ] **Tests:**
  - `test_a_torn_journal_line_refuses_instead_of_vanishing`: cut the last 20 bytes of an `open` line; `unresolved()` and `done_keys()` raise `JournalCorrupt`.
  - `test_only_done_closes_an_open_entry`: a `"phase": "note"` line raises `JournalCorrupt`.
  - `test_replanning_does_not_hide_an_open_write`: an open entry in `journal-7-aaaaaaaaaaaa.ndjson` blocks `apply_plan` of a plan with another basis, and the message names that file.
  - `test_an_open_write_blocks_a_delete_and_the_reverse`, with the delete journal in the second directory.
  - `test_journals_from_before_the_phase_field_are_ignored`: `journal-<stamp>.ndjson`, with no account in its name, is not scanned.
  - `test_another_accounts_open_entry_does_not_block`.
  - `test_a_second_apply_is_refused_while_the_first_holds_the_lock`: only the account read is sent.
  - `test_journal_lines_reach_the_disk`: `os.fsync` is called once per append.
- [ ] Run; expect failures.
- [ ] **Implement.** The lock wraps everything in `apply_plan` after the account check, and everything in `_undo` after the reason check; `open_entries` is read inside it. `check` prints every open entry as a blocker. The journal `open` entry also stores `payload` (the op without provenance) and `done` stores the result as JSON, untruncated; readers accept the old shape.
- [ ] Full suite, ruff, README row. Commit `journal: open writes block the whole account`.

Limit to document: the lock and the scan cover the chosen `--workdir` and the default one. A journal in a third directory is not seen.

## Task 3: validate the whole plan before the first request

**Files:** modify `plan/model.py`, `plan/apply.py`, `cli/main.py` (`cmd_check`, `cmd_apply`); test `tests/test_quirks.py`.

**Interfaces produced:** `class PlanInvalid(WanderfillError, ValueError)`; `validate(plan: Plan) -> list[str]` (empty means valid).

- [ ] **Tests:** `test_plan_validation_rejects[...]`, one case each: schema 99; unknown kind; unknown bucket; `add_visit` without region or quality; quality 0 and 7; `update_visit` without `visit_id`; `create_trip` with no regions, a region twice, a region without id or quality; `mark_kye`, `mark_tcc` or `tick_series` without `item`; `tick_series` without `series`; unparseable date; start after end; two `add_visit` with the same region and dates but different quality; two `update_visit` on one `visit_id`; two ops with one key.
  - `test_plan_validation_accepts_year_only_and_undated_visits`.
  - `test_an_invalid_late_op_sends_nothing`: valid op, then invalid op; `t.sent == []`.
  - `test_dry_run_apply_reports_invalid_plans`: exit 1 without `--confirm`.
- [ ] Run; expect failures. **Implement**; only `apply`-bucket ops are checked for content, the whole file for schema. Replace the duplicate loop in `apply_plan`.
- [ ] Full suite, ruff. Run `validate` read-only over the existing `workspace/plan-*.json` and report any rejection instead of loosening a rule silently. README row. Commit `plan: refuse a malformed plan before sending any of it`.

## Task 4: trip keys cover what is sent

**Files:** modify `plan/model.py`, `plan/apply.py`; test `tests/test_quirks.py`.

**Interfaces produced:** `Op.key` (changed for `create_trip` only); `Op.legacy_key: str | None` (old hash for `create_trip`, else `None`).

- [ ] **Tests:**
  - `test_trip_keys_differ_when_only_a_regions_dates_differ`, and the same for `hidden`.
  - `test_trip_key_ignores_region_order` and treats `"11"` and `11` alike.
  - `test_other_keys_did_not_move`, with dates `2026-01-01..2026-01-02`:

```python
GOLDEN = {
    "add_visit": "79c8eaaa53229ef7",     # region=5, quality=3
    "update_visit": "d7a05a5109773bc2",  # region=5, visit_id=9, quality=3
    "mark_dare": "81bbe4792ea14391",     # region=7, no dates
    "mark_kye": "42cab0614eae52d7",      # item=570, no dates
    "mark_tcc": "722f39f054a6a4fe",      # item=64, no dates
    "tick_series": "643df695b5e5a487",   # series=5, item=2903, no dates
}
```

  - `test_legacy_trip_key_is_the_old_hash`: regions `[{"id": 11, "quality": 3, "hidden": 0, "year_from": 2026, "month_from": 1, "day_from": 1, "year_to": 2026, "month_to": 1, "day_to": 2}]` gives `legacy_key == "8ae84238652ef19e"` and a different `key`.
  - `test_a_trip_done_under_the_old_key_is_refused_not_resent`: `DriftError` naming the journal; no write.
- [ ] Run; expect failures. **Implement.** Trip material adds, per region sorted by id: `hidden` (default 0) and the six date parts as `int | None`. `description` stays out: `Op` cannot set it.
- [ ] Full suite, ruff, README row. Commit `plan: a trip's key covers its regions' dates`.

## Task 5: strict snapshots, and never tick what is ticked

**Files:** modify `api/client.py`, `plan/apply.py`, `cli/main.py` (`cmd_check`); test `tests/test_quirks.py`.

**Interfaces produced:**
- `NomadMania.series_ticked(series_id: int) -> set[int]` and `kye_ticked() -> set[int]`: strict; `VerificationFailed` if the list is missing.
- `ticked_before(client, ops) -> dict[tuple[str, int], set[int]]` in `plan/apply.py`; keys `("series", id)`, `("kye", 0)`, `("dare", 0)`, `("tcc", 0)`; one read per key.
- `ApplyReport.already: list[str]`.
- `doubles(snapshot: dict, ops: Sequence[Op]) -> list[str]` in `plan/apply.py`: one line per `add_visit` or `create_trip` region whose full dates equal a visit already in the snapshot for that region, or another op's for that region. `apply_plan` raises `DriftError` before any write; `check` lists each as a blocker.

Why the last one: Task 4's review showed that a plan rebuilt after its trip landed carries that trip in its own basis, so neither the journal nor drift stops a second copy; `verify` only reports it afterwards. Undated and year-only visits are not compared: two real visits can share them. A trip region without its own dates is compared by the trip's header.

- [ ] **Tests:**
  - `test_a_rebuilt_plan_does_not_send_a_trip_that_is_already_there`: nothing sent, for `create_trip` and for `add_visit`, and the same for a trip-owned existing visit.
  - `test_two_ops_for_one_region_and_dates_are_refused`: an add and a trip region, and two trips.
  - `test_the_double_guard_ignores_undated_year_only_and_overlapping_visits`.
  - `test_an_update_visit_is_not_a_double_of_itself`.
  - `test_snapshot_refuses_a_reply_without_its_list`: for visited regions, DARE and one region's visits; no write follows.
  - `test_apply_skips_an_item_that_is_already_ticked`: no toggle sent, label in `report.already`, nothing journalled. The same for KYE, DARE and TCC.
  - `test_a_tick_only_plan_does_not_read_every_region`: zero `get-visits-to-region` calls.
  - `test_the_snapshot_file_keeps_the_ticks_it_is_about_to_change`.
  - `test_a_fingerprint_made_before_this_change_still_matches`: `{"visited_regions": [5, 6], "visited_dare": [7], "visits": {"5": [{"id": 1, "date_from": "2026-01-01", "date_to": "2026-01-02", "quality": 3}]}}` with `[5]` gives `091eb3c2162171bedddce8c38be944a1`.
- [ ] Run; expect failures. **Implement.** `snapshot` uses `visited_region_ids(strict=True)`, `_dare_strict`, `_visits_strict`. `apply_plan` and `cmd_check` pass `regions` rather than `regions or None`. `basis_of` is unchanged. `ticked_before` takes DARE and TCC from the snapshot, which has just read them strictly, and reads only series and KYE.
- [ ] Full suite, ruff, README row. Commit `apply: a missing list is not an empty profile`.

## Task 6: verify everything that landed, once, and say so in the exit code

**Files:** modify `plan/apply.py`, `cli/main.py` (`cmd_apply`); test `tests/test_quirks.py`.

**Interfaces:** consumes Task 5's strict reads. Produces `exc.report: ApplyReport` on any exception leaving the apply loop, and `verify-<stamp>.json` with `"partial": true` and `"uncertain": {...} | null`.

- [ ] **Tests:**
  - `test_a_failure_at_op_three_still_verifies_the_first_two`: file written, original exception type raised, report printed by `cmd_apply`, exit code non-zero.
  - `test_verifying_an_uncertain_op_does_not_close_it`: `uncertain` holds the read-back; `unresolved()` still has the key.
  - `test_a_readback_failure_does_not_replace_the_original_error`.
  - `test_verify_reads_each_series_once`: 3 ops in one series, exactly 1 read; the same for KYE and DARE.
  - `test_verify_calls_a_missing_list_unproven`.
  - `test_unproven_exits_one`.
  - `test_verify_reads_a_created_trips_header`: a header other than the planned dates is a mismatch.
  - `test_one_unreadable_region_does_not_discard_the_report`.
- [ ] Run; expect failures. **Implement.** Group tick ops by list. Visit reads use `_visits_strict`; a `VerificationFailed` becomes that op's `unproven` line.
- [ ] Full suite, ruff, README row. Commit `verify: read partial runs back, and fail on unproven`.

## Task 7: contain the trip re-span (needs the owner's decision)

**Files:** modify `api/client.py` (`sync_trip_header`), `cli/main.py`, `AGENTS.md`, `README.md`; test `tests/test_quirks.py`.

**Interfaces:** `sync_trip_header(trip_id: int, *, expect_rows: Sequence[tuple] | None = None) -> dict`; CLI `wanderfill sync-header <trip_id> [--confirm --reason TEXT] [--workdir DIR]`.

- [ ] **Tests:**
  - `test_sync_header_refuses_to_shorten_either_end`: two trips that were longer than their regions, found 2026-11-23; nothing sent.
  - `test_sync_header_refuses_when_a_row_and_its_visit_disagree_on_dates`: nothing sent.
  - `test_sync_header_refuses_when_the_trip_changed_since_the_preview`: `PrecisionLoss`, nothing sent.
  - `test_sync_header_dry_run_prints_that_every_id_will_change`.
  - `test_sync_header_confirm_needs_a_reason` and `test_sync_header_journals_the_whole_trip_before_the_write`: into `deletes-<account>.ndjson`, through `_undo`.
  - `test_sync_header_is_not_a_plan_op`.
  - `test_sync_header_across_new_year`: old and replacement trips found when the years differ.
- [ ] Run; expect failures. **Implement.** Change the docstring's "Touches nothing else" to say it replaces the trip. Print old-to-new trip and visit ids.
- [ ] Add to AGENTS.md section 5: `update-visit` leaves the header behind; `update-trip` replaces every id; write a stay only after it is finished. Add the named exception to rule 2 in the owner's words.
- [ ] Full suite, ruff, README row. Commit `trips: sync-header, one trip at a time, journalled`.

## Task 8: a tile that failed is not an empty tile

**Files:** modify `geo/tiles.py`, `geo/resolve.py`, `cli/main.py` (`_resolve_coords`); test `tests/test_cache.py`.

**Interfaces produced:** `TileReader.failed: set[tuple[str, int, int, int]]` (added on network, 5xx or decode failure, removed on success); `Resolution.method == "tile_error"` with `region None`.

- [ ] **Tests:**
  - `test_a_failed_home_tile_is_not_answered_by_its_neighbour`: the point's own tile times out and the neighbour holds a polygon within tolerance; method is `tile_error`.
  - `test_a_failed_neighbour_does_not_spoil_an_interior_hit`.
  - `test_tile_errors_are_not_saved_to_the_coordinate_cache`.
  - `test_an_absent_tile_is_still_empty`: 404 is not a failure.
  - `test_history_and_evidence_say_how_many_points_could_not_be_read`.
- [ ] Run; expect failures. **Implement.** An interior hit wins. Otherwise any failed tile in the neighbourhood gives `tile_error`. `shapes` must not memoise a failed tile.
- [ ] Full suite, ruff, README row. Commit `tiles: a timeout is not open water`.

## Task 9: photo provenance and frozen fixes

**Files:** modify `sources/photos_app.py`, `grade.py`, `AGENTS.md` 6a; test `tests/test_quirks.py`.

**Interfaces produced:**
- `PhotoAsset.importer: str | None = None`, `PhotoAsset.taken_utc: dt.datetime | None = None` (aware), and property `PhotoAsset.origin -> Literal["own-camera", "airdrop", "other", "unknown"]`, from `com.apple.camera`, `com.apple.sharingd`, any other value, and missing.
- `PhotosAppSource.origins: frozenset[str] | None = None`, applied to both `assets()` and `points()`.
- `Verdict.repeated: int = 0`, the photos collapsed.

- [ ] **Tests:**
  - `test_a_library_without_the_importer_column_reads_as_unknown` (the existing fixture has no such column).
  - `test_unknown_is_never_own_camera` and `test_points_and_assets_apply_the_same_origin_filter`.
  - `test_no_filter_reads_exactly_what_it_read_before`.
  - `test_taken_utc_orders_photos_across_a_date_line_flight`.
  - `test_grade_holds_a_frozen_fix`: 12 timed fixes at `(-17.553412, -149.606731)` over 2.75 h give `ok False`, `repeated == 11`, and a reason containing "one coordinate repeated".
  - `test_grade_sees_the_jump_after_a_frozen_fix`: the same plus Los Angeles 20 minutes later is held as airborne.
  - `test_grade_holds_one_coordinate_on_two_dates`.
  - `test_grade_does_not_collapse_rounded_or_untimed_points`: 3-decimal and day-level input gives the same verdict as before.
  - `test_grade_accepts_a_stay_that_moves_a_little`: 10+ jittering fixes over 2 h stay `ok`.
- [ ] Run; expect failures. **Implement.** Select the column only if `PRAGMA table_info(ZADDITIONALASSETATTRIBUTES)` lists it, else `NULL`. Collapse when both fixes are timed, consecutive, exactly equal, and either coordinate has more than 4 decimals. Days, points and span count the collapsed list (first of each run). A leg is counted only where the coordinate changes, timed from the last photo of the run. `evidence.grade` is not touched.
- [ ] Full suite, ruff, README row. Commit `photos: keep the importer; grade: a repeated coordinate is one observation`.

## Phase 1 gate

- [ ] `.venv/bin/pytest -q` and `.venv/bin/ruff check .` clean; `docs/nomadmania-api.md` and README updated.
- [ ] Independent Codex and Grok reviews of the whole diff; each finding fixed or answered.
- [ ] `open_entries` run read-only over both existing journal directories: no `JournalCorrupt`, no open entry (true of the old reader on 2026-11-24). Then `wanderfill check` on one finished plan.
- [ ] Owner says go before the first live apply on the new code, and before any push.

---

## Left for later, from the Phase 1 reviews

- `history`: `_airborne_flags` is diluted by a frozen fix as `grade` was, and takes speed from local time, not UTC. Do this before Task 10 reuses any of it.
- `evidence --no-resolve` says "No region here was small enough" though nothing was measured.
- An interrupt inside the verify file's own write can leave `partial` false or the file short.
- A row with a year and month but no day reads as a bare year in both readers. None of 946 live rows has that shape.
- What `updateMQP`, the delete endpoints and `update-trip` answer on success is not recorded. Record it at the next live use.
- DARE and TCC ticks are in the fingerprint, so a half-applied DARE or TCC plan is refused by drift and has to be rebuilt.
- KYE: the site's own page script paints ids above 612 (613-648) as one group worth one point. Not verified against the server; it may explain the quadrants that appeared without a tick.
- The package is installed editable, so an apply in another session runs whatever is checked out here. On 2026-11-24 one did, between two gates. Use a worktree for Phase 2, or keep other sessions from applying.

---

# Phase 2: features

## Task 10: `wanderfill series review`

**Files:** create `src/wanderfill/plan/review.py`, `src/wanderfill/plan/review_page.html` (package data); modify `cli/main.py`, `plan/model.py` (hatch already ships files inside the package); test `tests/test_review.py`.

**Interfaces produced:**
- `Evidence(kind: Literal["ground_photo", "fast_photo", "document", "memory"], detail: str)`; `Op.proof: list[dict]` (default empty, not in the key).
- `Candidate(series, item, series_name, name, km, at, photos, days, proof)`; `Manifest(account, format=1, params, candidates)`, `Manifest.sha256 -> str` over canonical JSON.
- `find_candidates(assets, reader, catalogue, ticked, *, radius_km=2.0, fast_kmh=120.0, skip=frozenset({-2, 12, 17, 60}), on_progress=None) -> list[Candidate]`
- `render_page(manifest: Manifest) -> str`; `load_answers(path, manifest) -> dict[str, Literal["y", "n", "q"]]`; `plan_for_series(manifest, answers, series_id, client) -> Plan`
- CLI: `series review --since DATE [--until DATE] [--radius KM] [--origin NAME]... --out DIR` and `series plan MANIFEST ANSWERS --series ID --out FILE`.

Rules: own camera by default. Fast legs use `taken_utc` over the full photo sequence, before any filtering by place. Photos after `--until` are ignored, which is how an unfinished stay is held back. The recall curve at 0.2, 0.5, 1 and 2 km is printed per series before the page is written, with the number of already-ticked items that had no photo. `memory` proof comes only from a separate free-text field the owner fills, never from clicking yes.

Two more rules:
- **Spoofed GPS.** A coordinate can be spoofed, and nothing in it shows that. `--not-evidence FILE` takes the owner's list of `{from, to, bbox, why}` spans; photos inside one are never proof and the page says how many were left out. A day whose own-camera photos are in two places no ground or air leg joins at a possible speed is offered last, marked `conflict`.
- **No list without photos.** The page shows only objects with a photo. Unticked objects without one are never printed, which would be the prompting AGENTS.md rule 7 forbids; a place the owner names unprompted goes in as `memory` through `series plan --named`, which takes the owner's words and shows no candidates.

- [ ] **Tests:** a photo in a `--not-evidence` span is never proof and is counted; `--named` prints no suggestions and refuses an item it cannot match exactly; no answer preselected; names containing `</script>` and `"` are escaped; the page has no `http` resource or `fetch`; blank items are absent from the export; `no` never becomes an op; unknown item, duplicate item, wrong account, wrong format and wrong manifest hash are each refused; a regenerated manifest invalidates old answers; already-ticked items are dropped on the live read; `series plan` without `--series` is refused; a failed tile is reported as incomplete coverage and listed in `params`; a photo next to a fast leg is excluded; own-camera default excludes `unknown` and prints how many; `confidence` stays 0.0.
- [ ] Run; expect failures. **Implement.** Progress is printed every 200 tiles. Tile bytes are already cached, so a rerun after Task 8 retries only failures; no second cache.
- [ ] Full suite, ruff; README section; AGENTS.md section 6. Commit `series: review candidates offline, plan one series at a time`.

## Task 11: airport candidates

**Files:** modify `plan/review.py`, `plan/series.py` docstring; test `tests/test_review.py`.

- [ ] **Tests:** a ground photo within 2 km is offered; a fast photo alone is never offered; the airport nearest to a fast photo is not inferred; one-photo candidates and those over 1 km are marked `thin` and sorted last; photos of a layover feed airports and no other series; `unknown` origin is offered only with `--origin unknown` and labelled; the printed report has ticked found / ticked with no photo / offered.
- [ ] Run; expect failures. **Implement** as ranking inside `find_candidates` for series 5. Keep `match_series` unchanged; correct its docstring: 25% was a day-level measurement.
- [ ] Full suite, ruff. Commit `series: rank airports by photos on the ground`.

## Task 12: trip planner, spec first

No code in this plan. Write `docs/superpowers/specs/<date>-trip-planner-design.md` and get the owner's yes.

- [ ] The spec must settle: photo runs per region and the allowed gap; "none strictly inside the polygon" versus "no photo"; first visits standalone, repeats in trips; flight cuts as proposals; layovers; a finished stay needs a later own-camera photo elsewhere or the owner's word; overlap with every existing visit including trip-owned, year-only and undated ones; quality asked, never defaulted; reuse of `segment` and `split_first_and_repeat`.

---

## Not building

- Automatic retry or resume of visit and trip writes; deletions or re-spans as plan ops; automatic header syncing.
- Drift blocking on whole tick lists.
- Automatic marking by distance, default importer filtering, the 2-10 km city extension.
- Mail or ticket parsing. A ticket is a booking; it can be typed in as `document` proof.
- A second tile cache.
