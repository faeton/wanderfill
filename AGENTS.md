# AGENTS.md — running this against a real NomadMania profile

You are an AI agent operating on somebody's real, publicly-ranked travel profile.
Everything you write is visible to a community that cares about the numbers being
honest. Read this whole file before the first request.

Symlink `CLAUDE.md` → `AGENTS.md` so Claude Code picks it up automatically.

---

## 0. The rules that override everything else

1. **Never claim a place without evidence.** No interpolation between two points,
   no "they must have crossed X to get from A to B", no "they were in the country
   so mark the capital". This is a scored competition. An inferred region is a
   fabricated score, and it is the user who gets accused of cheating, not you.
2. **Never delete anything you did not create in this session.** Not visits, not
   trips, not ticks. If something looks redundant, report it and stop.
3. **Compute, then plan, then apply — never in one step.** Write a plan file, show
   it, get a human yes, then execute it. No "and while I was there I also…".
4. **The token is a year-long full-power credential.** Never print it, never write
   it into a file that gets committed, never paste it into an issue, a gist, or a
   message to another model. Read it from the environment and keep it there.
5. **Ask before the first write of each kind.** Adding visits, creating trips,
   ticking series, marking DARE and marking KYE are five different blast radii.
   Approval for one is not approval for the next. Series are not one kind either:
   World Capitals and Art Museums are different questions.
6. **Open every source that covers the year before you write a date.** Not the
   freshest one, not the tidiest one — *all* of them. A date written from a
   source that cannot see that year is unverified, whatever it agreed with.
   User memory is a hypothesis, not a source: of five dates one user supplied
   from recall, three were contradicted by their own archive.
7. **Never propose the missing place, date or route.** Do not show the gap and
   the days either side and ask "so, Luxembourg on the 30th?" Ask what they
   independently remember, and treat "I don't know" as a final answer. **A yes
   to a corridor you drew is still your interpolation** — it has a human
   signature on it, which is worse than none, because now it looks sourced.

Rules 6 and 7 exist because 1–5 were all followed and the writes were still
wrong. A plan file, a human yes and a no-delete policy do not stop you from
politely constructing a fabrication.

---

## 1. Getting authenticated

There are two token conventions on this site and you need the same token for both.

Ask the user to run this in the browser console on `nomadmania.com`:

```js
localStorage.getItem('token')
```

That is a UUID-shaped bearer valid for a year. Put it in the environment as
`NM_TOKEN`, or in a `chmod 600` `.env` beside the repo — `wanderfill` reads the
environment first, then `.env`, and stops at the repository root. Nowhere else.
Do not offer to log in for them — that means handling a password, which you must
not do.

If a user pastes a token into the conversation, say so plainly and tell them to
rotate it by logging out and back in. It is in the transcript now; nothing you
do afterwards takes it back.

If the browser shows them logged out, the site can be re-authenticated by writing
the token back into `localStorage` and into a `token` cookie, then reloading.

**Verify before doing anything else:**

```bash
curl -s -X POST https://nomadmania.com/webapi/user/status-quick \
  -H "NMTOKEN: $NM_TOKEN" -H "LANG: en" -H "platform: web" -d ""
```

Use `status-quick`, not `status`. The latter returns only
`{result, status, admin}` with no account id at all, and the account id is what
a plan has to be bound to.

---

## 2. The three surfaces

| Surface | Base | Auth | What lives there |
|---|---|---|---|
| Modern API | `nomadmania.com/webapi/<module>/<action>` | `NMTOKEN` header | regions, visits, trips, DARE, countries, geocoding |
| Legacy AJAX | `nomadmania.com/ajax/V2/`, `/ajax/my_series/` | `token` **form field** | all 69 series: WHS, TCC, … — **not KYE**, which has its own module |
| Tiles | `maps.nomadmania.travel/tiles/<layer>/{z}/{x}/{y}.pbf` | none | current polygons and every series object's coordinates |

All API calls are `POST` with `application/x-www-form-urlencoded`. Tiles are
gzipped MVT with no honest `Content-Encoding` — sniff for `1f 8b` and
decompress.

If you cannot find an endpoint for a feature, **do not guess function names**.
Check whether the page is an iframe: the series API was invisible for an hour
because `/series_single/22/` is a shell around `/nm_pages/series_single.html`.
Fetch the inner document and grep it.

---

## 3. Read the current state first, always

Never plan against a cached assumption. Snapshot before you think.

```
maps/get-visited-regions-ids-simple      -> flat id list, cheapest state check
maps/get-visited-dare-ids-simple         -> same for DARE (trust this one)
regions/get-regions-list-2               -> the live 1381-region catalogue
quickEnter/get-visits-to-region {region} -> EVERY visit record for one region
trips/get-trips-for-year-app {year}      -> what trips already exist
slow/get-slow-app                        -> 196 countries, visited flags, YES, SLOW
user/get-settings                        -> incl. `homebase` / `homebase2`
```

`user/get-settings` is worth reading before you segment anything: `homebase` and
`homebase2` are the regions the user has *told* NomadMania they live in. That is
a statement, and it beats inferring home from whichever region dominates a year.

**`quickEnter/get-visits-to-region` returns both standalone and trip-owned
visits and both kinds count.** Filtering to `trip_id is None` is the single
most expensive mistake available here — it created 50 duplicate visits.

---

## 4. Getting a location history in

Any source reduces to the same shape: `date, lat, lon`, one row per day
minimum. Photo libraries, GPX, Google Timeline, a CSV the user typed by hand.
Deduplicate to distinct coordinates before geocoding — a 4,868-day track is
usually only ~2,000 distinct points.

**Sources answer different questions. Open all of them, and know what each
cannot say.**

| source | resolution | blind spot |
|---|---|---|
| local Photos library | per photo, timestamped | thin or empty before iCloud; has *yesterday* |
| a day-level track (`track.csv`) | one row per day per city | no intra-day movement, so no speed; city labels are suburb-level and inconsistently spelled |
| user memory | a hypothesis | wrong about three dates in five, on one profile |

The freshest source matters for a trip happening *now*: a phone that has not
synced means the server-side library stops weeks ago and the newest trip is
missing entirely. But "check the freshest first" is exactly wrong for old
travel — a library that begins in 2018 has nothing to say about 2013, and
reading only that one produced "no evidence either way" for a year the
day-level track covered in full. **A source that cannot see the year is not
evidence of absence.**

**On the road, resolve per photo, not per day.** One point per day is fine for a
stay and wrong for a drive: a day crossing three countries averages into one
region and the other two vanish. Never interpolate between points either — what
was crossed unphotographed stays unclaimed.

### Geocoding

```
location/get-region  {lat, lng, share:0}
```

**`share=0` is mandatory.** Without it this endpoint publishes the coordinate as
the user's live location. Geocoding an archive without it writes a nonsense
travel diary in public.

**`share` also changes the response shape.** With `share=0` the answer is
`{"result":"OK","region":181}`; without it, `{"nm":{"id":181},"dare":…}`. If
every coordinate comes back unresolved, you are parsing the wrong shape — do not
go looking for a data problem. And `-1` means open water, not a stale id.

### Repairing what comes back

Roughly **14% of the ids this returns no longer exist.** The geocoder runs on an
older polygon set than the catalogue. So:

1. Check every returned id against `regions/get-regions-list-2`.
2. For any id that is absent, re-resolve the coordinate by point-in-polygon
   against `tiles/regions/{z}/{x}/{y}.pbf` at z≈10–11.
3. For coastal points that fall in no polygon, widen to a 3×3 tile neighbourhood
   and take the nearest polygon within a stated tolerance.
4. Points still unresolved are **open ocean or flights. Leave them unresolved.**
   Do not snap them to the nearest land.

---

## 5. Writing regions, visits and trips

### The data model

A region has many visits. A visit is either standalone or owned by a trip, and
**both count toward the region's visit total.** Quality is 0–6 and **0 is "no
visit", not transit**: 0 no visit, 1 transit, 2 minimal visit, 3 good visit,
4 worked here, 5 lived here, 6 travelguru. Sending 0 believing it means transit
writes a downgrade that looks deliberate; `QUALITY` in `client.py` is the list.

### The four traps, in the order you will hit them

**`quickEnter/add-visit` auto-creates a single-region trip with an empty
description.** You cannot turn this off. Record the trip id it produces so the
debris is reconcilable later.

**`quickEnter/update-visit` replaces the whole record.** Any field you omit is
overwritten, not preserved. Always read-modify-write, and take `max()` of the
existing qualities so an update can never downgrade a "lived here" to a
"good visit".

**`trips/new-trip` requires a field it never reads.** Send *both*:

```python
payload = {
    "description": "",
    "date_from": start.isoformat(),
    "date_to":   end.isoformat(),
    # the server checks this key exists before it reads regions_json;
    # the website itself sends "[object Object],[object Object]" by accident
    "regions": ",".join("[object Object]" for _ in regions),
    "regions_json": json.dumps(regions),
}
```

**Trips plus existing standalone visits double-count.** If every region already
has a standalone visit and you then create trips covering every journey, the
totals inflate by the number of first visits. Either the trips own everything
(requires deleting the standalone records — usually forbidden), or each region's
*first* journey stays standalone and only *repeats* go into trips. Pick one,
state the trade-off, and say plainly what it costs: in the second option some
trips list fewer regions than the journey really covered.

### Trip segmentation is ill-posed — say so

For a continuously nomadic person there is no home to return to and therefore no
natural trip boundary. Do not pretend otherwise. What works:

- home = the modal region for that calendar year
- a trip is a run of away-days; a gap of more than N days ends it
- a hard cap on trip length, or single "trips" run to 465 days and 101 regions

Sweep the parameters, show the user the resulting trip counts at each setting,
and let them choose. Record the chosen parameters in the plan.

---

## 6. DARE, series and the side-lists

**DARE** is a binary flag per area, no dates and no counts:
`quickEnter/updateMQP {region: <dare_id>, visits: 1}`. Only ever send `1`.
Match areas by point-in-polygon against `tiles/dare/`. Beware:
`get-regions-mqp` returns `visited` as *the area's own id as a string*, not a
boolean — `"1142"` means visited, `"0"` means not. Use
`maps/get-visited-dare-ids-simple` instead.

**Series** (69 of them, including UNESCO WHS = id 22, Know Your Earth, TCC) live
on the legacy surface:

```
read   POST /ajax/V2/
       action=getData & type=seriesSingle & id=<series> & lang=en & token=<token>
       -> { title, items2:[…], visited:[ids], score, max, isos }

write  POST /ajax/my_series/
       action=toggle & item=<object> & state=0|1 & series=<series> & token=<token>
       -> the literal string "OK"
```

The read returns no coordinates. Get them from `tiles/series2/` where every
object of every series is a Point carrying its own `id` and its `series_id` —
so one tile harvest geocodes all 69 series at once. The layer is not decimated
between z9 and z15 — but it **is** below that, so do not read "use a coarse
zoom" as licence to go coarser still: a global z4 sweep found 31 of the 211
World Capitals, where z6 found 113. **z9 is the only zoom shown to be complete**
— harvest there, and cut the cost by fetching only the tiles the track actually
passes through rather than the globe.

**Series need more caution than regions.** A day-level track puts one point per
city; being within a few kilometres of a monument is evidence of being in the
area, not of having seen the thing. Grade candidates by distance, show the user
the counts at several radii, validate recall against what they have already
ticked by hand, and let them choose the threshold. Do not pick it for them.

**KYE is not a series and has its own module.** "Know Your Earth" is not filled
in promptly from regions or trips — a profile with hundreds of regions can sit at
zero, and one did. But it is **not established that it is purely manual**: hours
after 93 quadrants were ticked, seven more had appeared, six of them boxes the
account has no coordinate inside. Read the count immediately before and after any
write, and do not tell the user nothing else can move it. Read `kye/get-kye`, write
`kye/set-kye {qid, visited:1}`, and never send `0` — un-ticking is a deletion.

A quadrant is a 10°×10° box, so membership is *arithmetic on a coordinate*: no
polygons, no stale ids, no tolerance to choose. That makes it the cleanest list
on the site, and it removes every excuse for guessing. It does **not** answer
whether a coordinate is a visit, and that is where the whole difficulty moves.

**Grade by speed, not only by count.** A photo from seat 27A sits in a cell as
convincingly as a week on the ground. Compute the implied speed between
consecutive timestamped points and treat a cell whose legs mostly exceed ~200
km/h as an aircraft. On the first run this caught two mid-Pacific boxes 1,100 km
apart 2.5 hours apart, a flight over Yemen at 305 km/h, and two airport layovers
that a point-count test would have waved through. Hold those for the user; do
not reject them, and do not mark them.

**Countries are derived, not stored.** Marking regions updates the country
counts, UN, UN+, SLOW and YES automatically. There is nothing to write.

**YES = "Years Elapsed Since"**, summed over 196 countries, lower better. Read
`docs/nomadmania-api.md` before quoting a number, because two things about it are
counterintuitive and both were got wrong the first time:

- It is **batch-computed and lags**. Read it once and a flat value across every
  visited country will look like a broken field; it is the batch not having run.
  Read, write, wait, read again — never conclude from one reading.
- A country marked visited with **no year scores 8**, not the age. So dating an
  old visit makes YES *worse*: break-even is eight years back. Check before
  proposing any backfill, and say plainly that dating is a hedge rather than a
  gain.

---

## 6a. Evidence — the half of the job that is not a write

The user is not only building a score, they are building something they may
have to **defend**. NomadMania verifies its highly ranked travellers by hand: a
committee names a random sample — 60 regions, or ~40 countries — and asks for
proof. At least 45 of the 60 have to be *Class 1*, and the clock is six months.
Refusing, or failing, can freeze the profile, delete regions wholesale, or turn
the account into a "Ghost User" outside every ranking. Mandatory in the top 50
for regions and the top 100 for countries; re-run after five years, or sooner if
a region count jumps.

Class 1, in their words: selfies with a prominent landmark, serial photos within
the region, dated diary entries, hotel bills in the traveller's name, ATM
withdrawals with a location and date. Class 2 — ordinary photos, a friend
vouching, a described route — covers the remaining fifteen at most.

This changes what a good run looks like. **Every region you help somebody claim
is a region they may be asked to prove.** So:

- Run `wanderfill evidence` *before* proposing writes, not after. If a region
  has no evidence in the library, say so at plan time rather than letting the
  user find out from a committee.
- The dossier is **read-only on both sides** and needs no approval to produce.
  It reads the profile, reads the library, writes local files. There is no
  `--apply` because there is nothing to apply.
- `--check-dates` compares photo dates against the visit dates on the profile.
  A disagreement is a *report*, never an edit: correcting a visit is a write,
  and writes go through a plan like everything else.
- Never grade a document you have not opened, and never open one you were not
  asked to. Filed paperwork is indexed by filename and left alone.
- **Do not tell somebody they are ready.** Report the projection as a range —
  an even draw and a draw leaning to their thinnest regions — and say plainly
  that acceptance is the committee's decision. The failure mode is not a wrong
  number, it is a traveller who stops gathering evidence because a tool said a
  word it had no standing to say.
- Read the timestamps, not only the coordinates. Eight photos across 34 km look
  like coverage of a region until they turn out to span two and a half minutes,
  which is a plane window. Thresholds are guesses about how a specific person
  travels: expose them as flags, print the ones used, and let the user move
  them rather than arguing that a region they remember is thin.
- Exemptions get **measured, not inferred**. "This region is too small to walk
  a kilometre across" must come from the polygon; deducing it from clustered
  photos hands the same exemption to two days in one hotel.
- A region with no photographic evidence is **not** a region to remove. Say what
  is missing and let the user decide; deleting is theirs, and rule 2 stands.

The technical parts that are easy to get wrong:

- The local Photos library is mostly **not on the disk**. With iCloud
  optimisation a large share of assets are thumbnails, and the original is on
  Apple's servers. Export through Photos itself — the AppleScript id format is
  `<UUID>/L0/001`, and a bare UUID fails with `-1728`.
- `ZSAVEDASSETTYPE = 12` is a photo somebody **sent** the user, carrying *their*
  coordinates. Screenshots are `ZKIND = 0, ZKINDSUBTYPE = 10`. Both are excluded
  at the query, and both would otherwise manufacture evidence for travel that
  did not happen.
- Attribute exhibits against the **live tiles**, and carry the
  nearest-polygon flag through to the output. Offering a photo taken across a
  border as proof of the region on this side of it is precisely the accusation
  the whole protocol exists to avoid.

---

## 7. The plan-then-apply protocol

Produce a plan file. Show a summary table. Wait for a yes. Then apply.

The plan should record: the ops with their kind, ids, dates and quality; the
evidence behind each one; the confidence; a fingerprint of the live state at
plan time; hashes of the source files; the segmentation parameters; and the
account id it was generated for.

**Which field holds the id depends on the kind, and the namespaces overlap.**
Putting an id in the wrong one is silent: the write goes to the right object
while the snapshot and drift record describe something else entirely.

| kind | id field | namespace |
|---|---|---|
| `add_visit`, `update_visit` | `region` | NomadMania region id |
| `create_trip` | `regions` (a list of dicts) | region ids — **not** `region` |
| `mark_dare` | `region` | DARE area id |
| `mark_kye` | `item` | 10°×10° quadrant id |
| `tick_series` | `series` + `item` | series id + object id |

Region 570 and quadrant 570 are both valid and unrelated. Copy an existing op of
the same kind rather than inventing the fields; `regions_touched()` in
`plan/model.py` is the one place that knows this mapping.

At apply time:

- Refuse if the authenticated account id differs from the plan's.
- Refuse if live state has drifted since planning — re-plan instead.
- Snapshot everything first, and refuse to start if the snapshot fails.
- Cap the number of writes per run.
- Journal every request and its response, including the id of any phantom trip
  created behind your back.
- Rate-limit to human speed: ~0.1–0.2 s between writes, no concurrency on the
  write path.
- **Never retry a write.** A read may be retried; a write that got no answer has
  an unknown outcome, and sending it again is how duplicate visits and phantom
  trips are made. The transport enforces this — writes get one attempt and raise
  `UnknownWriteOutcome`, which leaves the journal entry open on purpose so the
  next run refuses to start until a human has reconciled it.

Then **verify**: `apply` now calls `verify()` itself and writes a `verify-*.json`
beside the journal. Read it. A response saying `OK` is not evidence — both
historical incidents here returned `OK` at the time and were found by reading the
server back. Report the real numbers, including the ones that came out wrong.

Before applying anything you did not build in this session, run
`wanderfill check <plan>`: it is read-only and reports account mismatch, drift,
duplicate ops and unconfirmed journal entries without sending a write.

---

## 8. Scripts should explain themselves

Every script gets a docstring that says what it writes, why it is safe, how the
numbers were derived, and what it costs. Somebody — possibly the user, possibly
you in an hour — has to audit a script that mutates a real profile, and a bare
`api("quickEnter/add-visit", d)` in a loop is not auditable.

Include a `--apply` flag. Default to a dry run that prints the plan and exits.

---

## 9. When you get it wrong

You will. The two failures from the first run were a hardcoded `quality: 3` that
downgraded a "lived here", and a `trip_id is None` filter that created 50
duplicate visits.

Both were caught by verifying against the server rather than trusting the
responses. So: audit after every phase, surface what broke before the user finds
it, propose the narrowest possible repair, and get explicit approval before
deleting anything — including your own mess.
