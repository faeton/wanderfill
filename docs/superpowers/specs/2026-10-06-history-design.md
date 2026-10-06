# `wanderfill history` — where was I, and when

Status: design, approved in conversation 2026-10-06. Awaiting written-spec review.

## Purpose

Answer travel-history questions that other people ask the traveller, from the
same sources the importer already reads:

- **visa forms** — "countries visited in the last 10 years, excluding
  EU/EEA/UK/US, with the date of the first trip to each" (UK, Schengen, Canada…)
- **ESTA / DS-160** — "have you been to Iran, Iraq, Syria … since 1 March 2011?"
- **tax residency** — days present per country per tax year, against 183
- **residence / citizenship** — absences from a home country, max days out in
  any 12 months
- **plain recall** — "where was I between March and April 2024?"

Read-only on NomadMania. Writes only local files. Never submits anything.

### The asymmetry that shapes everything

On NomadMania the dangerous error is *claiming too much*. On a visa form it is
the reverse: **omission is "deception"**, and a UK refusal for deception can
carry a ten-year ban. So this command must never quietly drop a country it
cannot date, never drop an overflight it is unsure of, and never turn "no
data" into "no". AGENTS.md rules 6 and 7 still hold: nothing is invented
either. Uncertainty is printed, never resolved.

## Commands

```
wanderfill history where    2024-03-01..2024-04-15 [--by region|country]
wanderfill history trips    --since 10y [--until DATE] [--exclude GROUPS] [--first]
wanderfill history check    --countries vwp-restricted --since 2011-03-01
wanderfill history days     --year 2025 [--tax-year calendar|uk|au|es|…] [--tax-year-start MM-DD]
wanderfill history absences --home ES --since 5y
wanderfill history list     --since 2016
```

Common flags: `--since/--until` (ISO date or `Ny` relative to today),
`--countries GROUPS`, `--exclude GROUPS`, `--territories separate|sovereign`
(default `separate`), `--bridge N` (default 7), `--fast-kmh` (default from
`grade.FAST_KMH`), `--library`, `--track`, `--no-profile`, `--cache`, `--out`
(default `history/`, gitignored).

Each query prints a table and writes `history-<query>.md` (paste-ready),
`.csv` and `.json` into `--out`. Every output starts with a **header block**:
the exact command, today's date, the window, the expanded country groups, the
flags in force (bridge, fast-kmh, territories), and each source's coverage
(first/last date it can see).

## Architecture

```
sources (photos, track.csv) ──► points ──► resolver (existing, live tiles) ──► region ──┐
profile visits (quickEnter/get-visits-to-region, all of them) ─────────────────────────┤
                                                                                       ▼
                                    countries.py (region → NM country → sovereign/ISO)
                                                                                       ▼
                                          history.py: Ledger (date × region rows)
                                                                                       ▼
                         queries: where · trips · check · days · absences · list
                                                                                       ▼
                                           render: md · csv · json (+ header)
```

### Units

- **`src/wanderfill/countries.py`** — the country vocabulary.
  - Maps NM region → NM country (via `flag1`, then `flag2`, as `yes_scores`
    does) → **sovereign** + ISO 3166 alpha-2, with an explicit override table
    for known misses (Greenland, Åland, Faroe, Canaries, Gibraltar, Hong Kong,
    Macau, Puerto Rico, Réunion and the other French DOM/COM, …).
  - Checked on every run against `slow/get-slow-app`: every NM country must
    resolve to an ISO code, or the run stops and says the table needs a look —
    same discipline as `NON_UN` in `share.py`.
  - **Groups**, each with members, a source note and an `as_of` date:
    `eu`, `eea`, `efta`, `schengen`, `cta`, `uk-form` (US, CA, AU, NZ, CH +
    EEA), `vwp-restricted` (IR, IQ, SY, SD, LY, SO, YE, KP, CU), `five-eyes`,
    `gcc`, `asean`, `mercosur`, `cis`, `commonwealth`, `un`, `un+`.
  - `parse_groups("eea,ch,uk,us,-ie")` → set of ISO codes. Groups and codes
    mix; leading `-` subtracts; unknown names are an error, never ignored.
    Used by `--countries`, `--exclude`, `--home`.

- **`src/wanderfill/history.py`** — the ledger and the queries. Pure: takes
  already-resolved inputs, no network, no filesystem. This is what tests hit.
  - `LedgerRow(date, region, country_iso, sovereign_iso, kind, sources, refs)`
    with `kind ∈ {observed, bridged, airborne}` and `sources ⊆ {photos, track,
    profile}`; `refs` are asset uuids / track line numbers / visit ids.
  - `UndatedClaim(region, country, year | None, visit_id)` — profile visits
    with only a year or no date at all. Kept beside the ledger, never expanded
    into days.
  - `Coverage(source, first, last)` per source.
  - `build_ledger(points, visits, resolver_map, countries, rules) -> Ledger`.
  - One function per query returning a plain dataclass result; renderers are
    separate functions taking that result.

- **`src/wanderfill/cli/main.py`** — `history` subparser with the six
  sub-subcommands; loads sources, resolver cache and profile exactly as
  `cmd_evidence` does (reuse, not copy: factor the shared loading into a
  helper both commands call).

### Building the ledger

1. **Photos / track points** resolve per point (per photo, not per day) to a
   region through the existing resolver and coordinate cache, against the
   live tiles. A three-country drive day yields three rows. Unresolved points
   (ocean, flights) stay unresolved and are counted in the header.
2. **Speed.** Consecutive *timestamped* points whose implied speed exceeds
   `--fast-kmh` mark their country-day `airborne` — unless the same
   country-day also has slow points, in which case it is `observed`. Day-level
   track rows have no clock and are never marked airborne (stated in the
   header). Reuse `grade.py`; do not re-implement.
3. **Profile visits** — every visit, standalone and trip-owned (never filter
   `trip_id is None`). A visit with full dates expands to one row per day of
   its range with `sources={profile}`. A year-only or undated visit becomes an
   `UndatedClaim`.
4. **Merge** rows for the same (date, region): union the sources.
5. **Bridge.** Per country, a gap of ≤ `--bridge` days whose two edge days are
   both that country (and no other country is observed inside the gap) is
   filled with `bridged` rows. A gap whose edges are different countries is
   never filled; it becomes an **open edge** on the trips either side.

### Queries

- **`where`** — rows in the period, collapsed into runs of consecutive days
  with the same set of regions (or countries with `--by country`). Days with
  no rows print as `no data`; bridged and airborne days are labelled. Undated
  claims whose year overlaps the period are listed underneath.

- **`trips`** — a trip is a maximal run of `observed`+`bridged` days in one
  country (by sovereign or territory per `--territories`). Each trip: entry,
  exit, open-edge ranges ("entered between Mar 3 and Mar 9"), observed /
  bridged day counts, sources, and per-source first/last date with an
  `agree / disagree / one-source` flag. `--first` keeps the earliest trip per
  country and prints the total trip count beside it. Countries with only
  `airborne` days are listed separately as **"probably overflight/transit —
  you decide"**. Undated claims inside the window appear as rows with an empty
  date and **CHECK**. Filtered by `--countries` / `--exclude`.

- **`check`** — per listed country: `yes` with the evidence behind it, or
  **"no evidence in sources covering <years>"**. Never a bare `no`. A country
  whose only evidence is airborne or an undated claim prints `CHECK`.

- **`days`** — per country per tax year: `observed`, `bridged`, `airborne`
  columns, and `unobserved` for the year overall. A day in two countries
  counts in both (convention stated in the output). Tax years: `calendar`,
  `uk` (06-04 → 05-04), `au` (07-01 → 06-30), `es`/`pt`/`de`/… = calendar,
  or `--tax-year-start MM-DD`. Totals against 183 with a warning when the
  country is within ±unobserved of the threshold ("180 observed + 10 no data
  is not under 183").

- **`absences`** — `--home` is required, or taken from `user/get-settings`
  `homebase` → its country. Never inferred from the modal region. Every
  interval between home days is an absence, with days out, countries seen,
  and whether its edges are observed. **Days with no data are not home**:
  they are reported as their own column, never assumed either way. Rolling
  total: max days out in any 365-day window, and per-year totals.

- **`list`** — every trip, all countries, chronologically.

## Cross-checking and coverage (rule 6)

For each trip and each country the output shows what each source says. A
source that **cannot see** a date (photos library starts 2018; track ends last
month; profile visit undated) is reported as *not visible*, never as absence.
`check` and `days` use coverage explicitly: "no evidence" is always qualified
by which years the sources could see.

Disagreements between sources are reported, never resolved. Correcting a
profile date is a write and goes through the plan protocol.

## Privacy

Output is a dated movement history. `history/` is added to `.gitignore`. The
command never sends output anywhere. The coordinate cache is the same one
`evidence` uses.

## AGENTS.md §6c — "Answering travel-history questions"

New section, for an LLM helping somebody fill in a form:

- **Quote the form's question, then show the command and the expanded
  groups.** The person checks the mapping (e.g. that "EU/EEA" became `eea`,
  and that Switzerland is or is not in it) as well as the answer.
- **Pick the query by the question's unit:** list of countries with dates →
  `trips`; yes/no on named countries → `check`; day counts → `days`; time
  away from one country → `absences`; "where was I" → `where`.
- **Rules 6 and 7 apply in full.** Never fill an open edge or an undated row;
  never ask "so you left on the 5th?". Ask what they independently remember;
  "I don't know" is final, and the form gets "approximately" or the range.
- **Omission is the dangerous error.** Never drop a CHECK row or an airborne
  row to tidy the answer. Present them; the person decides.
- **Never submit and never type into the form.** The output is a draft the
  person copies; they sign the declaration.
- **No "you're fine".** Tax residency and absence limits are decided by law
  and the authority. Report counts and margins only.
- **The output stays local.** Never paste it into another model, a gist, an
  issue or a message.
- Group membership changes; the `as_of` dates are printed — if a form's date
  predates a change (Croatia joined Schengen 2023, BG/RO 2025), say so.

README gets a short "Answering travel-history questions" section with two
examples (visa form, tax days).

## Testing

`tests/test_history.py`, no network, hand-built inputs (same style as
`test_share.py`):

- `parse_groups`: mixing, subtraction, unknown name raises, `eea` ⊇ `eu`.
- country table: territory → sovereign override (Greenland → DK), and the
  run-time check fails loudly when an NM country has no ISO code.
- ledger: per-point resolution gives multiple countries in one day; merge
  unions sources; profile range expands per day; year-only visit becomes an
  `UndatedClaim`, not days; trip-owned visits are included.
- speed: fast pair marks airborne; slow point same day keeps it observed;
  day-level rows never airborne.
- bridging: same-country gap ≤ N bridged; > N not; different-country edges
  never bridged and produce open edges; a third country inside the gap blocks
  the bridge.
- `trips --first` keeps earliest and reports count; undated claim in window
  appears as CHECK; airborne-only country goes to the overflight list.
- `check`: "no" is always qualified by coverage.
- `days`: UK tax year boundaries (Apr 5 / Apr 6); two-country day counts in
  both; near-183 warning.
- `absences`: no-data days are neither home nor away; rolling 365-day max.
- `where`: run collapsing, `no data` days, `--by country`.
- CLI smoke test of argument parsing for each sub-subcommand.

## Out of scope (v1)

- Passport stamps / boarding passes / email confirmations as sources.
- Filling any form, or any browser automation.
- Any write to NomadMania.
- Legal interpretation (what counts as "visited", airside transit rules per
  country) — the output marks the candidates; the person decides.
