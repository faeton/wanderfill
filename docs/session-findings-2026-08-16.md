# Findings, 2026-08-16

A day's work on one real profile. Recorded because most of it was learned by
getting something wrong first, and the wrong version is the one that sounds
plausible next time.

---

## 1. YES is batch-computed, and reading it once will fool you

**What happened.** At 02:00 `slow/get-slow-app` returned `yes: 8` for all 100
visited countries whose real scores spanned 0–15, and `41` — the account's exact
age — for the 93 unvisited ones and three visited ones. Region-level years were
correct throughout and did not move the field. Conclusion drawn, and written into
`docs/nomadmania-api.md` as fact: *stale aggregate, nothing can move it.*

At 03:00, after a batch run: **189 of 196 countries matching the published rule**,
profile total 4736 → 4036.

**What it actually is.** The job runs and it is right. `8` is not a stale
constant — it is the score for **a country marked visited whose year is unknown**.
Before the batch catches up, every visited country is in that state as far as the
aggregate is concerned, which is what produced the uniform 8.

That fills the hole in NomadMania's published rule, which states what a *never*
visited country scores but not a visited-undated one:

| state | score |
|---|---|
| visited this or last calendar year | 0 |
| visited before that | years since |
| **visited, no year recorded** | **8** |
| never visited | your age |

**The consequence inverts the obvious advice.** An undated country costs 8, not
your age, so backfilling is worth far less than it looks. Whether it gains or
loses depends entirely on the year: with an undated country at 8 in 2026, a 2026
or 2025 visit gains 8, a 2019 visit gains 1, 2018 breaks even, and anything
earlier is a loss — Myanmar went 8 → 13 by being dated to 2013.

**"Eight years" is a table, not a law**, and the break-even moves every January.
Use `yes_delta()`; do not carry the slogan around. It can also be worth exactly
nothing: YES reads a country's *most recent* visit, so dating one region of a
country that already has a later year elsewhere changes the score by zero.

**Rule for next time:** read, write, wait for the batch, read again. Never
conclude from one reading. Where a local recomputation and the server disagree,
the server is usually right — its territory handling (Greenland under Denmark,
Åland under Finland) beats matching regions to countries by flag.

---

## 2. Three remembered dates out of five were wrong

The user supplied dates from memory for countries their profile had marked
visited but never dated. Checked against the evidence:

| country | remembered | evidence |
|---|---|---|
| Kenya | 10 Mar 2026 | ✅ exact |
| Kosovo | 18 Aug 2023 | ✅ exact |
| Bahrain | 10 Mar 2025 | ~ nearest photo 9 Mar 2025 |
| Monaco | 3–4 Aug 2026 | ❌ those photos are in France; Monaco itself is 24 Jan 2026 |
| CAR | 19 Apr 2026 | ❌ Mozambique and KwaZulu-Natal that day |
| Luxembourg | 2–3 Jul 2023 | ❌ Switzerland and Italy both days |
| Myanmar | ~Jul 2013 | ❌ all 17 covered July 2013 days are Ukraine |

**Memory is not a source on a profile like this.** Every remembered date has to be
checked, and a confident month is not evidence of anything.

---

## 3. Check `track.csv` as well as the Photos library

The local Photos library holds almost nothing geotagged before 2018. Reading only
that source produced the claim *"the library cannot speak to 2013 at all"*, and a
contradicted Myanmar date was written on the strength of it.

`workspace/data/track.csv` — 4,868 day-rows, 2009–2026 — covers those years. The
two sources answer different questions and both are needed:

- **`track.csv`** is day-level: one row per day per city. Good for *which country
  on which day*, useless for a drive across three borders.
- **The Photos library** is per-photo. Good for a driving day, blind before 2018.

Bounding boxes are a shortlist, never a claim: a "Myanmar" box returned 329
photos that all geocoded to **Thailand**, and an "Oman" box returned 769 that were
all **UAE**. Always resolve candidates through `location/get-region` (`share=0`).

---

## 4. Do not offer the user a corridor and call their agreement evidence

Luxembourg has never been photographed, in either source. The route either side of
the gap — Flanders and Nord-Pas-de-Calais on 30 June, Alsace and Rhineland-
Palatinate on 1 July — is a drive that would naturally pass through it.

The assistant built that corridor, offered it to the user as a question, and
treated the yes as evidence. An outside review named this correctly: it is the
interpolation ban with a human rubber stamp. The legitimate form asks what the
user independently remembers and accepts *"I don't know"* as an answer; the
illegitimate form proposes the date and waits for a tired yes.

---

## 5. Visits have three date shapes, not two

892 visit records on this profile: **764** fully dated, **115** with no dates at
all, **13** carrying a year with null month and day. Year-only is a real, natively
stored shape.

Reading it as undated — which any `all()` check over the six components does —
is quietly destructive: the next read-modify-write posts the record back with its
year blanked. `Visit.from_api` had this bug. It now returns `YearOnly`.

Year-only is also the *right* representation for "some time in 2013": YES reads
only the year, so a remembered year scores exactly what a manufactured day would,
without claiming a day.

---

## 6. KYE was unclaimed, and its geometry is the cleanest on the site

See the KYE section in `nomadmania-api.md` for the endpoints and the grid. On
this profile it read **0 of 434** while the user's own coordinates fell inside
**105** cells — nothing had filled it in, and nobody had ever clicked the map.

**It was later called "manual" here, and that claim did not survive the day** —
see §8. Something added seven quadrants nobody in this session ticked.

**Outcome: 93 marked, 12 held.** The rule applied, and it is the transferable
part:

> **apply** — the cell holds coordinates from 2 or more distinct days; or from a
> single day but 10+ points spanning 2+ hours at ground speed.
> **review** — everything else, plus any cell where most consecutive timestamped
> pairs imply travel faster than **200 km/h**.

The speed test is the one that earned its place. A point-count test would have
marked all twelve. What the speed test caught:

| cell | what it really was |
|---|---|
| −10/−20S −180/−170W and −170/−160W | 1,100 km apart, 2.5 h — one Pacific flight |
| 20/10N 40/50E "Hadhramaut" | 1.1 km in 13 seconds — 305 km/h over Yemen |
| 30/20N −100/−90W | Houston airport, Terminal E, 26 minutes |
| 20/10N −100/−90W | Mexico City airport, 41 minutes |
| three Atlantic/Pacific boxes | single points over open water, land place-tags |

Held is not rejected. Two of the twelve are very likely real — St Petersburg sits
inside `70/60N 30/40E` by 0.002° of latitude, and the Patagonian coast box is on
a known route to Ushuaia — but a single undated track point is not the same
evidence as a day on the ground, and the user decides.

**A caution about reading the gap.** 163 mainland cells are missing, but that
number overstates it. Pre-2018 coverage is thin and some countries were never
photographed at all — Luxembourg has no coordinate in either source despite being
a genuine visit. Missing cells mean missing *geotags* as often as missing travel.

---

## 6a. The namespace bug the drift check caught

Worth recording because it lasted four minutes and would otherwise be invisible.

`mark_kye` was first written with the quadrant id in `Op.region`. But `apply_plan`
builds its pre-write snapshot from `{o.region for o in ops}` and treats those as
NomadMania region ids — so it fetched visits for unrelated regions, and the
fingerprint failed. **The apply refused rather than writing.**

qids and region ids are different namespaces that overlap numerically. Nothing
would have raised; the writes would have gone to the right KYE cells while the
snapshot and drift record described the wrong thing entirely. The fix: quadrant
ids live in `Op.item`, as series items do, and the snapshot only collects regions
from ops whose kind actually touches visit records.

The drift check that caught it was added the same afternoon, in response to a
review that called it decorative because it failed open. It was not decorative an
hour later.

---

## 7. Safety defects found by review, and fixed

An independent code review found four real ones that the tests did not cover:

- **`op.quality or 3`** in the applier — a missing quality became 3, and so did an
  explicit `0`. This is incident #1 from AGENTS.md §9 reintroduced verbatim.
  `update_visit` now takes `max()` against the live record, so a downgrade is
  structurally impossible rather than a documented caution.
- **Drift was blind to what these plans change.** The fingerprint covered visited
  region and DARE ids only, so a plan that re-dates a visit could overwrite a
  correction made in the meantime. It now covers the visit records — id, dates,
  quality — for the regions the plan touches, and **fails closed** when a plan has
  no basis at all.
- **`Op.key` omitted `visit_id` and `quality`**, so two different updates could
  collide and a journal entry for one would mark the other done.
- **The journal was per-run and written after the request.** A crash between
  server acceptance and the write left no trace, and a resumed apply re-sent
  everything. It is now keyed to the plan, write-ahead, and refuses to start when
  a previous run left an op opened and unconfirmed.

The general lesson: every one of these was a *documented* caution that had not
been turned into behaviour. A comment telling the caller to be careful is not a
safeguard.

---

## 8. A second review round, and the defect the first round created

Two independent reviews were run against the state produced by the first round.
Both found real problems, and the most serious was introduced *by* the earlier
fixes rather than surviving them.

### The write path retried writes

`Transport._post` retried every request three times on a connection failure.
That is right for a read and catastrophic for a write: if NomadMania accepted an
`add-visit` and the response was lost on the way back, it was sent again, and
again — duplicate visits and phantom trips, with one `open` line in the journal.
**This defeated the write-ahead journal added the same morning**, because the
retries happen *below* it.

Reads retry now; writes get one attempt. A write that gets no answer raises
`UnknownWriteOutcome`, distinct from `TransportError`:

- `TransportError` — the server answered and refused. Definitely did not happen.
- `UnknownWriteOutcome` — no answer came back. **We do not know.**

Only the second leaves its journal entry open, which blocks the next run until a
human reconciles. "It failed" and "we never found out" needed different types.

### Three more from the same review

- **Duplicate ops in one plan both executed.** `already` was read once, so the
  first success could not suppress the second. Now a structural check runs
  before any network call at all.
- **Apply continued after a failure**, running later ops against a profile no
  longer in the state the plan assumed. Now fail-stop.
- **Drift ignored `create_trip` regions**, which live in `Op.regions`, not
  `Op.region` — a whole trip's worth of regions sat outside the check.

### And two the docs had been lying about

**`yes_scores()` still implemented the disproven rule** — undated countries
scored as the traveller's age, not 8. The library the docs told you to trust
would have reported an undated country as costing 41, and after the next batch
`yes_stored=8` against a recomputed `41` would have looked like a server bug.
That is exactly how the first round's wrong conclusion started. Fixed, and the
recomputed total now matches the server exactly: 4036 against 4036, where before
it was 4206.

**`evidence --check-dates` crashed on year-only visits** — `TypeError: '<=' not
supported between YearOnly and date` — a regression introduced by the same
morning's `YearOnly` work. A bare year now widens to 1 January–31 December.

### Photo timestamps were shifted by the machine's timezone

`assets()` called `datetime.fromtimestamp()` on a value the SQL had *already*
converted to local wall-clock, re-applying the local offset. On the machine this
was found on that is two hours, enough to move a near-midnight photo to the wrong
day — and it made `assets()` and `points()` disagree about which day a photo
belongs to, since `points()` goes through SQLite's UTC-based `date()`.

Spans and speeds were unaffected (both endpoints shift together), so the grading
conclusions stand, but every clock time reported from a dossier before this fix
was off by the local offset.

### KYE may not be purely manual after all

Hours after 93 quadrants were ticked deliberately, the count read **100**. Six of
the seven additions are boxes this account has *no coordinate inside*, in
countries it has visited. Either the user ticked them by hand, or something
server-side derives quadrants from region visits on a delay, as YES does.

Nobody has distinguished the two, and the docs no longer claim it is manual. This
is the second time in one day that a confident claim about a NomadMania field
came from a single reading.

### What the reviews say about this repo's habit

The first round's lesson was *documented cautions that were never turned into
behaviour*. The second round's is narrower and worse: **a fix can create the
defect it was written to prevent.** The retry bug and the `yes_scores()` staleness
were both produced by the same morning that added the journal and the YES
correction. The counter-move is not more care; it is that every claim now has a
test, and the speed rule that lived in prose is `grade.py`.
