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
   trips, not ticks. If something looks redundant, report it and stop. There are
   two exceptions, and only these. The first is `wanderfill delete <kind> <id>`,
   and only when the account owner has named the specific id and said it is
   wrong. One id per run, `--confirm` from them, and the preview shown first.
   "This region looks wrong" is not an id, a list of ids is not one id, and a
   deletion is never a plan op.
   The second, approved by the owner on 2026-11-24, is
   `wanderfill sync-header <trip_id>`. It widens a trip's own dates to span its
   regions, and to do that the server deletes the trip and its visits and creates
   the same ones again, so every id in it changes. One trip per run, only when the
   account owner has asked for that trip id, with the preview shown to them first
   and `--confirm` from them; never as a plan op, never in a loop.
3. **Compute, then plan, then apply — never in one step.** Write a plan file, show
   it, get a human yes, then execute it. No "and while I was there I also…".
4. **The token is a year-long full-power credential.** Never print it, never write
   it into a file that gets committed, never paste it into an issue, a gist, or a
   message to another model. Read it from the environment and keep it there.
5. **Ask before the first write of each kind.** Adding visits, creating trips,
   ticking series, marking DARE, marking KYE and ticking TCC are six different
   blast radii.
   Approval for one is not approval for the next. Series are not one kind either:
   World Capitals and Art Museums are different questions.
6. **Open every source that covers the year before you write a date.** Not the
   freshest one, not the tidiest one — *all* of them. A date written from a
   source that cannot see that year is unverified, whatever it agreed with.
   User memory is a hypothesis, not a source: a date supplied from recall can be
   contradicted by the archive.
7. **Never propose the missing place, date or route.** Do not show the gap and
   the days either side and ask "so, that country on the 30th?" Ask what they
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
| Legacy AJAX | `nomadmania.com/ajax/V2/`, `/ajax/my_series/` | `token` **form field** | all 69 series: WHS, capitals, … — **not KYE** or **TCC**, which have their own |
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
slow/get-slow-app                        -> 196 SLOW/YES countries, visited flags (not ATLAS)
profile/get-profile-data {profile_id}    -> the numbers the profile page shows: UN, ATLAS, NM, ranks
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
| user memory | a hypothesis | can be wrong, however sure it sounds |

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
5. A tile that timed out or answered 500 is not an empty tile. The point is
   `tile_error`, whatever polygon is next door; it has no region, is not cached,
   and the next run asks again. Do not call it ocean, and do not plan from a run
   that has any near the place in question. A wrong `tile_nearest` cached by a
   version that did not know this looks like a right one: `--refresh` re-asks
   only points with no region, so delete the coordinate cache to re-ask them all.

---

## 5. Writing regions, visits and trips

### The data model

A region has many visits. A visit is either standalone or owned by a trip, and
**both count toward the region's visit total.** Quality is 0–6 and **0 is "no
visit", not transit**: 0 no visit, 1 transit, 2 minimal visit, 3 good visit,
4 worked here, 5 lived here, 6 travelguru. Sending 0 believing it means transit
writes a downgrade that looks deliberate; `QUALITY` in `client.py` is the list.

**A transit does not mark the region.** A region whose best visit is quality 1
has the visit and is still absent from the visited list and the region total:
two adds, at quality 2 and quality 1, moved one profile's count by one. Say
that at plan time, before the user picks a quality, not after the count comes
back one short. `verify()` reports it as `not_counted`, for a standalone visit
and for a region inside a trip alike.

**The site no longer asks for a quality; the API still stores one.** Since the
trip entry of December 2025 the website shows no Visit Quality section for 2025
trips ("Visited or Not Visited", and transit is not a visit), while
`get-visits-to-region` still returns `quality` on every record and `add-visit`
still takes it. So the scale above is still the API's, and a profile edited on
the website may carry whatever the site sends in its place. Read it; do not
assume it was chosen.

### The five traps, in the order you will hit them

**`quickEnter/add-visit` auto-creates a single-region trip with an empty
description.** You cannot turn this off. Record the trip id it produces so the
debris is reconcilable later.

**`quickEnter/update-visit` replaces the whole record.** Any field you omit is
overwritten, not preserved. Always read-modify-write, and take `max()` of the
existing qualities so an update can never downgrade a "lived here" to a
"good visit".

**`update-visit` leaves the trip's header behind.** It moves a trip-owned visit
and not the trip's own `date_from` and `date_to`, so a stay written early and
extended later reads shorter on the trip than on its region. Nothing edits that
header in place: `trips/update-trip` *replaces* the trip, so the old one and its
visits are deleted and the id of the trip and of every visit in it changes. So
**write a stay only after it is finished.** For one that was not,
`wanderfill sync-header <trip_id>` is rule 2's second exception (see "Undoing,
when the owner asks"). It refuses to shorten a header or to send a row whose
dates are not its visit's, and it is not a plan op. A header that lags is a
thing to report, not a reason to run it.

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
trips list fewer regions than the journey really covered. **This is about journeys
whose regions are on the profile already.** A journey whose runs overlap no existing
visit goes into one trip, first visits included; nothing is counted twice, for there
is nothing to count it with.

### Undoing, when the owner asks

There is one path off the profile, and one command that replaces (the end of
this section): `wanderfill delete <kind> <id>` with kinds
`visit` (needs `--region`), `trip` (takes its visits with it), `region`
(`set-not-visited`: every visit on it, needs `--all-visits`), `dare` and `kye`.
Each shows a preview (which says so, and exits 2, if an open or unreadable journal
entry would refuse it), sends nothing without `--confirm --reason "..."`, journals
an `open` entry carrying the full record to `deletes-<account>.ndjson` before
the request, and reads the server back. Prefer `visit` over `region`: the
narrowest cut that fixes the mistake. A trip-owned visit needs
`--allow-trip-owned`, because it leaves the trip too, and `add_visit` cannot
put it back *into* that trip afterwards.

What the read-back accepts is narrow on purpose. A trip is gone only if
`get-trip` fails with exactly `Unauthorised.` **and** the year's listing no
longer carries it; a response with the list field missing is an error, not an
empty list. An entry closes by itself on the refusals that close an apply's (§7),
a 403 on the write included. If proof fails, or the answer is lost, it stays open
— and **any** open entry, in a delete journal or an apply's, blocks every further
deletion and apply for that account until the owner has read the server and
closed it by hand, by appending a `done` line with that key (and an `error` if
the write did not land: a plain `done` means it did). The tool never rewrites a
journal, and neither do you: do not close one for them.
None of this is in the plan model, and it must not be: a plan is what the tool
computed, and a deletion is what the owner decided.

`wanderfill sync-header <trip_id>` is rule 2's second exception. It removes
nothing for good, but the server deletes the trip and its visits and creates new
ones, so every trip and visit id in it dies and any saved plan naming one has to
be rebuilt. Raising it with the owner means showing them the preview first, as
with `delete`: run it without `--confirm`, show what it prints, and stop. It
shares `delete`'s journal, lock, open-entry block, `--confirm` and `--reason`;
its `open` entry carries the whole trip and every visit in its regions. The
preview makes every refusal that reads can find, and the write refuses if the
trip is not the one that was shown. A refusal, `ERROR`, 403 or any other, closes
its entry only if the trip, its visits and the listing of its years then read as
they did before, because the save is a deletion and a creation and a refusal can
come from between them; any other trip that changed leaves it open too. What it
prints at the end, old id to new id, goes into the record. If it could not prove
the save, show the owner the replacement candidates and the journal path it
printed: it is not run again, and nothing is written to repair it.

### Trip segmentation is ill-posed — say so

For a continuously nomadic person there is no home to return to and therefore no
natural trip boundary. Do not pretend otherwise. What works:

- home = the modal region for that calendar year
- a trip is a run of away-days; a gap of more than N days ends it
- a hard cap on trip length, or single "trips" run to 465 days and 101 regions

Sweep the parameters, show the user the resulting trip counts at each setting,
and let them choose. Record the chosen parameters in the plan.

That is `segment` and `wanderfill sweep`, on a day-level track. For photos use
`wanderfill trips review`, next. It asks for no home and has no cap: it shows the
stays, suggests where a trip may end, and the owner answers.

### Trips from photos go through a page the owner answers

As for series. Neither command writes to the profile, and the plan has `create_trip` ops
and no others.

1. `wanderfill trips review --since DATE` with `--track FILE` for every day-by-day
   track that covers those dates (rule 6), or `--no-track`, which is the owner saying
   that there is none. It reads the photos, the map, the visits and the trips, and
   writes a page and its list into `review/trips/`. Read out what it prints: the runs by
   status and reason, the gap table, what was left out, the sources with their first and
   last day, the trips that are there already, the tiles not read. Run `evidence` first
   (6a): a region with a thin run is one the owner may be asked to prove. Exit 3 means
   that something was not read (a tile, a photo's place, a row of a track) and the list is
   incomplete: say so, and run it again. `plan` says it once more. A
   plan from an incomplete list is allowed only if the owner, told that it is, wants it.
2. **Hand the page to the owner and stop.** They open it, answer and save the file. Never
   fill in an answer, a word, a cut or a date, never edit the list or the answers, press
   nothing for them and suggest nothing: rule 7. Blank is not "no" and not a quality: it
   holds the whole trip it is in. "I don't remember" is final: do not ask about it.
3. `wanderfill trips plan LIST ANSWERS` reads the profile again and makes one plan. Show
   it with `show -v`, then `check`, then the owner's yes to this plan, then `apply`.
   One kind, trips, and one approval (rule 5). It prints how many regions will be marked
   and how many visits will only be stored: say both before the yes. A refusal (another
   account, another list, a visit or a trip over the same days that is not as the list
   shows it; a trip with no dates is over no day and refuses nothing) means a new `review`, not a repair of the files. It refuses more than 50
   region visits; `--max-rows` is the owner's to say.

A run is days of photos strictly inside one region, taken on the ground by the owner's
own camera with a clock that can be the local one. It is not a visit: only the profile
and the owner know those. Every other photo proves nothing and may still stand between
two days.

**Flight days.** One day beside a run, at each end at most, is in it if it has a photo at
speed strictly inside the region, and nothing was seen elsewhere in between: the airport.
It can be a day too many, after a night flight with a position that is still the old one,
so the page names the day and the owner can leave it out. A day with photos beside an
impossible jump only is out, for such a photo is the old place as often as not; the owner
can include it, where the stay with it would pass every check. Whether either was a day
there is the owner's alone: do not answer it, do not press "Leave this day out" or "Include
this day", do not say which is likelier, and do not pass `--no-flight-days` unless they ask.
Read out both counts. Neither button releases anything that holds a run.

What holds a run, and what releases it:

| reason | what it means | released by |
|---|---|---|
| `open` | no photo on a later day shows them in another region, so the stay may not be over | their words, or a later review |
| `brief` | one photo, or all within 15 minutes, or all at one coordinate | their words |
| `layover?` | under 24 hours, with a fast photo before and after | their words |
| `undated` | a visit of the region with no date, or only a year, may be this one | their words, that say why it is another visit |
| `overlap` | it shares a day with a visit that is there | nothing |
| `continuation` | nothing shows that they left between it and a visit or another run | nothing |
| `not read` | a map tile on one of its days or beside them did not arrive | a new review |
| `stale map` | a region that is not in the catalogue, on those days | a new review |
| `across` | it goes over `--since` or `--until` | a new review with other dates |
| `clock` | its photos have two clocks over 12 hours apart | nothing: words cannot correct a date |

The words are the owner's own and unprompted. Do not write them, improve them or offer
any. Do not ask leading questions about a held run: not "so you had left by then?", not
"that was another visit, was it not?". "The page has a field for it" is all there is to
say. What nothing releases goes on by hand, or waits.

**Finished stays only.** An `open` run goes into a plan on their own words that it is
over, and not because the trip would look better with it: a stay that is extended later
leaves its trip's header behind (above). A layover is theirs to judge: some count one,
most do not. Do not cut or join anything for them either: where a new trip starts is
asked on the page.

**A region is in a trip once.** Two stays in one region with other regions between them are
two visits. A journey through one hub (A, B, A) is not made until the
owner presses "A new trip starts here" somewhere between the two stays in A, also where
nothing suggests a cut. The owner picks where; do not pick it for them, and do not
propose a place.

`--gap-days`, `--fast-kmh`, `--spans` and `--until` are theirs to choose, as for series.
With a larger gap more days without a photo are inside a stay, and the gap table says
what that changes.

---

## 6. DARE, series and the side-lists

**DARE** is a binary flag per area, no dates and no counts:
`quickEnter/updateMQP {region: <dare_id>, visits: 1}`. `mark_dare` only ever
sends `1`; `0` un-marks and lives in `unmark_dare` / `wanderfill delete dare`,
one area per run, under rule 2.
Match areas by point-in-polygon against `tiles/dare/`. Beware:
`get-regions-mqp` returns `visited` as *the area's own id as a string*, not a
boolean — `"1142"` means visited, `"0"` means not. Use
`maps/get-visited-dare-ids-simple` instead.

**Series** (69 of them, including UNESCO WHS = id 22 and World Capitals = id 1) live
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
ticked by hand, and let them choose the threshold. Do not pick it for them. That is about a shortlist
from a track. For series from photos, next, a radius only filters the page: it is never
a yes to the items inside it, whoever chose it, and each item has its own answer.

**Series from photos go through a page the owner answers.** Three commands, none of
which writes to the profile:

1. `wanderfill series review --since DATE` reads the photos, the tiles and the live
   series, and writes a page and its list into `review/`. Read out the counts it
   prints, the recall, what was left out and the tiles that were not read. Exit 3
   means the list is incomplete: say so, and run it again before planning. A plan from
   an incomplete list is allowed only if the owner, told that it is incomplete, wants
   it, for the answers are to what was shown.
2. **Hand the page to the owner and stop.** They open it, answer and save the file.
   Never fill an answer in, edit the list or the answers, press a button for them, or
   suggest one: rule 7. An item with no answer is not a "no" and not a "yes", and "I
   don't remember" is final: do not ask about it. A later page may show such an item
   again. Hand that page over as it is: do not remove the item, do not turn the old
   answer into "no", and do not carry any answer over to it.
3. `wanderfill series plan LIST ANSWERS --series ID` makes the plan for one series.
   Only "I was there" becomes a tick. One series per plan and one approval per series,
   rule 5: show it with `show -v`, then `check`, then a yes to that series. An
   item that is ticked by now is dropped and named, also if its name has changed. A
   refusal (another account, another list, an item that is gone, or one that is not
   ticked and has another name now) means a new `review`, not a repair of the files.

What the marks on an item mean. They are facts for the owner, not grades for you.
`thin`: one photo, or none within a kilometre. `conflict`: a photo of that day, here,
is at one end of a jump nobody makes, so a coordinate is wrong. `one spot`: on two days
or more every photo has exactly one coordinate, as a false or a stale position does.
`fast`: photos near were taken at travel speed, and do not count. Within a series the
items with `conflict` or `one spot` come last, and of the airports the thin ones come
after the others and before those. The ticked airports with no photo near are named: they
show what the list misses, and are no questions.

What the flags mean. `--origin` other than `own-camera` lets in photos that may be
somebody else's, so only when the owner asks. `--spans` is the owner's file of
stretches that prove nothing (`use: none`) or an airport only (`use: airports`); you
may write down what they say, and do not decide it. `--until` leaves out a stay that
is not over. `--radius` is theirs to choose, and 20 km at most.

`wanderfill series named --series ID --name TEXT --said TEXT` is for a place the owner
named without being asked. `--said` holds their own words and nobody else's, and a yes
to a place you named is not that (rule 7). The name has to match the catalogue letter
for letter. When it does not, do not go looking in the catalogue for what they may have
meant: ask them for the exact name.

Never print or read out items that no photo is near, not even the nearest, and not
from `client.series()` either. The page names one kind of them, the ticked airports
with no photo near, so that its airports are not taken for all of them; the terminal
says only how many, and you read out no more than it does.

**KYE is not a series and has its own module.** "Know Your Earth" is not filled
in promptly from regions or trips — a profile with hundreds of regions can sit at
zero, and one did. But it is **not established that it is purely manual**: hours
after 93 quadrants were ticked, seven more had appeared, six of them boxes the
account has no coordinate inside. Read the count immediately before and after any
write, and do not tell the user nothing else can move it. Read `kye/get-kye`, write
`kye/set-kye {qid, visited:1}`. `0` un-ticks and is a deletion: it lives in
`unmark_kye` / `wanderfill delete kye`, one quadrant per run, under rule 2.

A quadrant is a 10°×10° box, so membership is *arithmetic on a coordinate*: no
polygons, no stale ids, no tolerance to choose. That makes it the cleanest list
on the site, and it removes every excuse for guessing. It does **not** answer
whether a coordinate is a visit, and that is where the whole difficulty moves.

**Grade by speed, not only by count.** A photo from seat 27A sits in a cell as
convincingly as a week on the ground. Compute the implied speed between
consecutive timestamped points and treat a cell whose legs mostly exceed ~200
km/h as an aircraft. That catches two mid-ocean boxes 1,100 km apart 2.5 hours apart, a
flight at 305 km/h, and airport layovers that a point-count test would
wave through. Hold those for the user; do
not reject them, and do not mark them.

**Give `grade` the raw coordinate, not a rounded one.** A phone can repeat one
stale fix for hours in flight, and twelve copies of it look like a day on the
ground. `grade.grade` counts consecutive photos with exactly one coordinate once
and reports how many in `Verdict.repeated`, but only where a coordinate has more
than four decimals: rounded and day-level points repeat anyway and each of them
still counts. Speed is judged only between coordinates that differ, rounded or
not, so standing still does not outvote the flight that follows. A cell held for
a repeated coordinate may equally be a phone lying still, so it is a hold like
the others. Order the photos by `PhotoAsset.taken_utc`; the local clock runs
backwards across the date line.

**TCC is not a series either.** The Travelers' Century Club list has 330
destinations and NomadMania derives almost all of them from your regions — no
endpoint lists those, only `score_tcc` on the profile. The exceptions are the
16 that no region covers (seven Antarctic claims, Akrotiri and Dhekelia,
Lampedusa, Midway, Wake, Lord Howe…), which ride along in
`quickEnter/get-regions` as `data.tcc` rows and are ticked by hand:
`quickEnter/updateTCC {region: <tcc_id>, visits: 1}`. The field says `region`
and holds a TCC id; `mark_tcc` refuses anything not in that list of 16. The page
un-ticks with `visits: -1`, which is a deletion and is not in the client: a wrong
tick has to be undone by the owner on `/regions/`, so ask first. A TCC plan's drift
basis includes the ticked ids, and an unreadable list goes under `unproven`.

Antarctic claims overlap: Argentina 25–74°W, the UK 20–80°W, Chile 53–90°W. A
landing at 64°W is inside all three. Which ones the person ticks is their call;
say where the photos are, not which flags to plant.

**Countries are derived, not stored.** Marking regions updates the country
counts, UN, ATLAS, SLOW and YES automatically. There is nothing to write.

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
  user find out from a committee. `trips review` grades each run in the same way
  and puts `evidence thin` on the page; the grade holds nothing, and it is of that
  run's proof only: a flight day, and a day beside a jump that the owner included, add
  nothing to it. Those days are theirs to leave out or include, and never yours.
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
- **Some ticks were placed by the server.** When NomadMania split a region in
  2022, a tick on the old region moved to the *first-named* new one
  (`static/changes_2022.pdf`), and regions moved to M@P arrived there marked.
  We found no published account of how visits moved in the 2026 revision. So a marked region
  can carry a tick the owner never set, sometimes with the old visit's date or
  none. Before a verification, list the marked "first" regions of each split
  and their visits, and give them to `evidence`. It is a report: whether one
  was theirs is the owner's to say, and removing it is rule 2's.
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
- **That filter does not catch everything that is not theirs.** An AirDropped
  photo (`ZIMPORTEDBYBUNDLEIDENTIFIER = com.apple.sharingd`), a Telegram or
  Messages save, and a Meta-glasses import all pass it. On one library the
  glasses importer stamped a stale coordinate from another country on photos
  taken a sea away, and AirDrop put a second person in one capital while the
  camera was still in another country. Before a write, split the days by
  `PhotoAsset.origin` (`own-camera`, `airdrop`, `other` or `unknown`; the bundle
  id itself is `importer`): when the own camera (`com.apple.camera`) is
  somewhere else the same day, the other photo is not evidence.
  `PhotosAppSource(origins=frozenset({"own-camera"}))` reads one origin only, in
  `points()` and `assets()` alike. Nothing uses it by default: `evidence` and
  `history` read every origin. Until about 2024 Photos recorded no bundle id
  but did record the capture source, `ZIMPORTEDBY`: a photo with no bundle id, a
  1 (back camera) or 2 (front) and no mark of sharing is `own-camera`, and on one
  library that was 43,774 photos of 2019–2024, one iPhone model a year. What is
  left is `unknown`, which is never `own-camera`: no capture source, AirDrop's 8
  and 13, an app's 3, or a shared-library, contributor's or shared-album photo.
  A library with neither column is all `unknown`, so this is still a check to
  run, not a filter to apply blind: there it would drop the owner's own photos.
  `records_origin` is `False` when the library has neither column; a
  filtered reading that came back empty from one says nothing. An `unknown`
  photo is not shown to be anybody's. An AirDrop is not the owner's either,
  even when the owner says they mostly AirDrop between their own phones: on that
  library about two thirds of the AirDropped photos came from camera models
  the owner never used.
- Attribute exhibits against the **live tiles**, and carry the
  nearest-polygon flag through to the output. Offering a photo taken across a
  border as proof of the region on this side of it is precisely the accusation
  the whole protocol exists to avoid.
- **"could not be read" is not "nothing there".** Photos whose map tiles did not
  arrive are in no grade. Run it again before telling anybody a region is thin
  or has nothing.

---

## 6b. Sharing — the one output aimed at a feed, not the server

`wanderfill share` draws the UN / ATLAS / NM numbers, rank, DARE, KYE and a map
of marked regions as PNG cards. It is **read-only on the server side** and
needs no plan: the numbers are the server's own counts, read from the same
endpoints the site uses, and the map is the visited id list painted onto the
public tiles. Nothing here derives a visit, and nothing here posts anything —
the user does that, afterwards, themselves.

Two things to hold to:

- **Never make a card from anything but the live profile.** Not from a plan
  that has not been applied, not from a draft count, not from "what it will
  be once the batch runs". A picture of an unapplied plan is a claim with no
  server behind it.
- **Quote the profile's own numbers; never recompute one.** Every figure on
  the card is `score_*` from `profile/get-profile-data {profile_id}`, the
  request the profile page makes. UN is the 193 member states. ATLAS
  ("Additional Territories, Lands and States", 137 on launch) replaced UN+ on
  11 November 2026 and holds no UN member; its score is `score_atlas` and its
  size the profile's own `atlas_count`, both read, never remembered. Neither
  is the 196 rows of `slow/get-slow-app`, which is the SLOW/YES list:
  counting visited rows of the 196 printed 104 for a profile whose page said
  121 UN+. The API still answers `score_unp` after the site dropped UN+; a
  field that is still there is not a list that is still shown, so it is not
  on the card.

## 6c. Travel history — answering a form somebody else wrote

`wanderfill history` answers questions from outside NomadMania: visa forms,
ESTA, tax residency, residence and citizenship applications, and plain "where
was I in March 2024". It is **read-only on the server** and needs no plan. Use
it when somebody pastes a form question and asks you to help answer it.

**The asymmetry flips here.** Everywhere else in this file the danger is
claiming too much. On a visa form the danger is leaving something out:
omission is "deception", and a UK refusal for deception can carry a ten-year
ban. Rules 6 and 7 still hold in full — nothing gets invented either. So the
whole job is presenting uncertainty, never resolving it.

**1. Quote the question, then map it — out loud.** Pick the query by the unit
the question asks for, and show the person the command *and* the expanded
groups before showing them the answer. They are checking the mapping as much
as the result: whether "EU/EEA" in their form includes Switzerland (it does not
— that is `efta`), whether "the last ten years" means from today or from the
application date.

| the form asks | query |
|---|---|
| countries visited, with dates | `history trips --since 10y --exclude …` |
| first trip to each country | `history trips … --first` |
| "have you been to X since …?" | `history check --countries X --since …` |
| days in a country in a tax year | `history days --year … --tax-year …` |
| time away from one country | `history absences --home …` |
| where was I in a period | `history where <period>` |

Groups: `eu`, `eea`, `efta`, `schengen`, `cta`, `uk-form`, `vwp-restricted`,
`five-eyes`, `gcc`, `asean`, `mercosur`, `cis`, `commonwealth`, `un`, `un+`.
They mix with ISO codes and names, and `-` subtracts: `eea,ch,uk,us,-ie`.
Membership changes — Croatia joined Schengen in 2023, Bulgaria and Romania in
2025 — and every output prints the `as_of`. If the form's window predates a
change, say so.

**2. Never drop a row to make the answer tidier.** Three kinds look droppable
and none of them are:

- **CHECK** — a profile visit with only a year, or no date. Present it. Ask
  what the person independently remembers; "I don't know" is a final answer,
  and the form then gets "approximately" or the year, not a date you chose.
- **probably overflight or transit** — seen only at aircraft speed. Some forms
  count an airside layover, most do not. That is the person's call.
- **territories** — the Canaries, Hong Kong, Bermuda. Kept by default even
  when the sovereign is excluded. Whether the form counts them is the
  person's call.
- **unmapped** — a region the live data ties to no country. Greenland, French
  Polynesia and Somaliland carry no second flag, so they print as
  `unmapped: Greenland – …` rather than under Denmark, France or Somalia, and
  `--territories sovereign` does not fold them in. Read every unmapped row
  against the question: ESTA asks about Somalia, and Somaliland will not
  answer to that name. Antarctica and the poles are unmapped because nobody
  owns them.

**3. Never fill an open edge.** "Entered between Mar 3 and Mar 9" stays a
range. Do not ask "so, the 5th?" — that is rule 7, and a yes to a date you
proposed is still your date. `bridged` days are labelled for the same reason:
they are a reasonable reading of a same-country gap, not an observation, and
the person should know which days are which.

**4. Read the coverage line before saying "no".** `check` prints "no evidence
in sources covering 2018 → 2026". If the question is about 2011 and the
library starts in 2018, the honest answer is "the sources cannot see that", and
you say so. A source that cannot see a year is not evidence of absence (§4).
**"Not read" is not "no evidence".** When a map tile did not arrive, or
`--no-resolve` skipped the point, the day is *not read*: not present, not away,
not "no data". Every query lists those days on their own, a placed day with an
unread point on it included, and while there is one in the window `check`
answers CHECK instead of "no evidence", `days` gives no "under 183", `absences`
marks the run "not settled", and a trip beside one has no exact edge. Run it
again, without `--no-resolve`, before copying anything to a form. Do not answer
around those days, and do not ask the person to fill them in (rule 7).

**5. Disagreement is a report.** When the profile and the photos give
different dates for a trip, show both. Correcting the profile is a write and
goes through §7 — it is not something to do on the way to a visa form.

**Every day inside a dated profile visit counts as observed**, whatever the
photos say about that day. One profile visit spanned twelve days across two
airport transits; `history` counted every one of them while the camera spent
ten in other countries. For a visa form that errs toward listing; for `days`
it inflates a tax count. Do not lean on the `agree` / `disagree` column to
catch it: that compares only the first and last day each source gives, so a
conflict in the middle of a stay still reads `agree`. Photos received from
somebody else count as presence too (§6a). A country that appears for a day or
two inside a stay elsewhere, or a long stay the person does not recognise,
deserves a look at where it came from before it goes on a form.

**6. Do not say "you're fine".** Tax residency, the 183-day test and absence
limits for residence or citizenship are decided by law and by the authority.
Report counts and margins — `days` prints "could reach 183: 175 counted + 12
days with no data" for exactly this reason — and stop there.

**7. Never type into the form and never submit it.** The output is a draft the
person reads and copies. They sign the declaration.

**A form reads the profile live.** The visit cache cannot see every website
edit (a same-year delete-and-re-add leaves its fingerprint unchanged), so the
form queries — `trips`, `check`, `days`, `absences`, `list` — ignore it unless
`--cached` is passed, and only `where` uses it by default. A report that used
it says so in its header; do not copy from one into a form. Never pass
`--cached` to save traffic on a form question.

**8. The output stays here.** `history/` is a dated record of where somebody
slept. Do not paste it into another model, a gist, an issue or a message, and
do not move it out of the gitignored directory.

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
| `mark_tcc` | `item` | TCC destination id (one of the 16 hand-entered) |
| `tick_series` | `series` + `item` | series id + object id |

Region 570 and quadrant 570 are both valid and unrelated. Copy an existing op of
the same kind rather than inventing the fields; `regions_touched()` in
`plan/model.py` is the one place that knows this mapping. A region in a trip's
`regions` may carry only `id`, `quality`, `hidden` (0 or 1), and
`year_from`/`month_from`/`day_from`, `year_to`/`month_to`/`day_to`: any other
field would be sent without being in the trip's key, so `validate` refuses it.
The dates are all absent, or both ends at one precision: two full dates or two
bare years.

At apply time:

- Refuse a plan that does not validate, before the first request and before
  `--confirm` matters: `plan.model.validate` checks the schema and, for every
  apply-bucket op, its ids, quality 1–6, dates, and duplicates. `check`, `apply`
  (dry run too) and `show` all print its problems.
- Refuse if the authenticated account id differs from the plan's.
- Refuse if another run holds the account's lock, or if any of the account's
  journals has an open entry or a line that cannot be read. Re-planning does not
  get past this. A directory that cannot be read, locked or put on the disk
  is refused too. The lock and the scan cover the chosen `--workdir` and the default
  `~/.local/share/wanderfill`, if it exists: a journal in a third directory is not
  seen, so keep one account to one `--workdir`. The tool never rewrites a journal,
  and neither do you: the owner appends the `done` line, or repairs the line that
  cannot be read, after reading the server.
- Refuse if live state has drifted since planning — re-plan instead.
- Refuse a trip this plan's journal has done only under the key older versions
  gave it, which left out the regions' dates (`Op.legacy_key`). Neither send it
  nor skip it: read the server back, then rebuild the plan from the live profile,
  without that trip if it is already there. A rebuilt plan whose basis records
  when it was taken has its own journal, and drift compares with what that plan
  saw, so neither would stop a second copy; the next rule stops only one with
  the same full dates. `check` lists the refusal too.
- Refuse a visit, added on its own or by a trip or moved by an `update_visit`,
  whose region and new full dates match another visit already on the profile,
  trip-owned or not, or another op in the plan. `check` lists each one. An update
  that leaves the dates as they are is not compared, so twins that are there
  already can still be edited. Dates an update in the same plan moves a visit
  away from still count as taken. Undated and year-only visits, and dates that
  merely overlap, are not compared: look for those yourself.
- Refuse an op this plan's journal has done whose result is gone from the profile:
  a tick that is not in its list, or a visit added on its own or by a trip whose
  region has none with those full dates. Somebody took it back. It is not skipped
  and not sent: if it is wanted again, build a new plan, with the time it was taken
  in its basis. Updates, undated and year-only visits are not judged. `check` lists
  each one.
- Snapshot everything first, and refuse to start if the snapshot fails. A reply
  without its list, with an id that is not a number, or with a visit that lacks a
  field, is a failure and not an empty profile or an undated visit.
- Read every series, KYE, DARE and TCC list the plan ticks before the first
  write, and never send an item that is already in it: a series tick is a toggle,
  and a repeat may undo it. Such an op is skipped, not journalled, and printed as
  `skip` and as `already ticked, not sent`. The series and KYE lists as they were are in the
  snapshot file under `ticked`.
- Refuse a tick whose item is not in its series now: the same read has the series'
  items, and one that cannot be read refuses too. Ids are compared and names are not,
  so **an item that was renamed is not detected**. An op this plan's journal has done
  is judged by its tick alone, as above. `check` lists each id.
- Cap the number of writes per run.
- Journal every request and its response, including the id of any phantom trip
  created behind your back.
- Rate-limit to human speed: ~0.1–0.2 s between writes, no concurrency on the
  write path.
- **Never retry a write.** A read may be retried; a write that got no answer has
  an unknown outcome, and sending it again is how duplicate visits and phantom
  trips are made. The transport enforces this — writes get one attempt and raise
  `UnknownWriteOutcome`, which leaves the journal entry open on purpose so the
  next run refuses to start until the owner has closed it. A
  redirect (no request with the token follows one: a redirected write is an
  unknown outcome, a redirected read an error), a 5xx, a 408, a body that is not JSON,
  an object without `result: OK` (or without the `trip_id` or `visit_id` a trip or
  an added visit is answered with) and a toggle that does not say `OK` are no
  answer either. Only a refusal
  closes an entry — ours before sending, or the server's own (401/403, another
  4xx, an `ERROR` body); any other exception, a bug in this package included,
  leaves it open.

Then **verify**: `apply` now calls `verify()` itself and writes a `verify-*.json`
beside the journal. Read it. A response saying `OK` is not evidence — both
historical incidents here returned `OK` at the time and were found by reading the
server back. Report the real numbers, including the ones that came out wrong.
`apply` prints every mismatch; `not_counted` lists
visits that are stored but leave their region unmarked. A trip is read back
region by region, by the trip id the server answered: one visit per region,
its planned dates and quality, and no second visit beside it for those dates.
The dates of the trip itself are read too. A region with no dates of its own in
the plan is held to the trip's, and one with a single end (`validate` refuses it
now) end by end: other full dates are a mismatch, and absent or
year-only ones are `unproven`, because what the server stores for such a region
has not been measured.
`unproven` is what the read-back could not establish either way — a list, a
region's visits or a trip that could not be read, or that came back without what
was asked for, or a trip whose id is not known to this run. It
is not a pass; read the server yourself before reporting the write as done.
One thing that cannot be read does not stop the rest from being checked, and
each list, region and trip is read once, after all the writes.

A run that ends early — an error, or Ctrl-C — reads back too: what it wrote, and
what was done or ticked already, with `"partial": true` in the file. `uncertain`
is the op whose journal entry is still open and what the server shows there now,
or null when nothing is open. **It is a hint, not proof, and it closes nothing.**
For a visit or a trip it can only match dates, so "a visit with these dates is
there" may be somebody else's, and "not there" is not a licence to send again:
a human decides. The original error is then raised as it was. Ctrl-C during any
read-back stops the reading: what was not checked is `unproven` as `NOT READ BACK`,
the file is written with `"partial": true`, and the error that ended the run is
raised, not the Ctrl-C, unless the run had finished. The trips `add-visit` makes
on its own are printed and are not an error.

Exit codes of `apply --confirm`: 0 when nothing came out different or unproven
(`not_counted` alone is still 0); 1 on a mismatch, on `unproven`, on a verify file
that could not be written, and on a plan that is not valid or a file that is not a
plan (`check` and `show` exit 1 on that file too); 2 on an error that
ended or refused the run, a failed op included; 130 on Ctrl-C. An error the package
did not foresee is a traceback, after the report.

Before applying anything you did not build in this session, run
`wanderfill check <plan>`: it is read-only and reports how many ops are left to
send, net of those done or ticked already, and account mismatch, drift,
duplicate ops, second copies, done ops that are gone, items that are not in their
series, lists that cannot be read, and every open or unreadable journal entry of
the account without sending a write.

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

---

## 10. Changing the package itself

Sections 0–9 are for driving a profile. This one is for editing the code that
drives it.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/pytest -q                                   # whole suite, ~35s
.venv/bin/pytest tests/test_quirks.py -k update_visit  # one trap at a time
.venv/bin/ruff check .                                 # line-length 100, py310
.venv/bin/wanderfill whoami                            # the only check that needs a token
```

**The suite never touches the network and never reads a token.** `FakeTransport`
in `tests/test_quirks.py` answers every call from a dict. A test that needs a
live profile is a test nobody runs, so the traps are asserted against the request
that *would* have been sent — `test_create_trip_sends_both_region_fields` checks
the junk `regions` field is in the body, not that a trip appeared.

The ruff `ignore` list in `pyproject.toml` is four deliberate decisions with
their reasons written beside them. Read them before silencing a fifth.

### The shape

```
sources/ ──► geo/ ──► plan/{model,segment,series,trips} ──► PLAN FILE ──► plan/apply ──► api/
  parse      resolve        compute                    (on disk)     execute     POST
```

Left to right, and the invariant is structural rather than a convention:
**nothing left of the plan file writes, and `plan/apply.py` computes nothing.**
`api/` is the only module that speaks to NomadMania.

| module | holds |
|---|---|
| `api/transport.py` | the two surfaces, the rate limiter, the retry decision |
| `api/client.py` | one method per endpoint, each quirk encoded as *behaviour* |
| `plan/model.py` | `Op`, `Plan`, `Op.key`, `regions_touched`, `basis_of` |
| `plan/apply.py` | the preconditions, the account lock, the journal, `verify()` |
| `plan/segment.py` | trips; `home=` is the parameter the module turns on |
| `plan/series.py` | shortlists and the recall curve, never an auto-marker |
| `plan/manifest.py` | the one form in which a list is hashed, saved and loaded; an answer names that hash |
| `plan/trips.py` | runs, reasons, boundaries and the trips the answers make. Pure; never answered for the owner |
| `plan/trip_list.py`, `plan/trips_page.html` | what the owner was shown, the page, the answers; the page and `group` have one rule |
| `plan/trip_plan.py` | the profile read again, and the plan of `create_trip` ops |
| `geo/resolve.py` | the three-stage repair for the geocoder's ~14% dead ids |
| `geo/tiles.py` | gzip sniffing, polygons, the series harvest, and which tiles `failed` |
| `grade.py` | is-this-coordinate-a-visit, by speed and time spread, a repeated coordinate counted once |
| `evidence.py` | read-only. `plan/trips.py` and `plan/trip_list.py` import it for the grade of a run; `plan/apply.py` and `api/` do not |
| `share.py` | a read-only leaf; nothing in the write path imports it |
| `api/cache.py` | the slow-link cache for `history`, `evidence` and `share` only |

### Six things that are load-bearing and do not look it

- **`CLAUDE.md` is a symlink to `AGENTS.md`.** Writing one rewrites the other.
- **`Transport.WRITE_HINTS` decides what may be retried**, by substring on the
  action name. A new write endpoint whose name misses every hint becomes
  silently retryable, and a retried write is how duplicates are made. Add the
  hint in the same commit as the method.
- **`regions_touched()` is the only place that knows which op kind puts its id in
  which field.** A KYE quadrant id and a region id overlap numerically, so
  reading one as the other snapshots an unrelated region and fails drift for the
  wrong reason. Section 7 has the table; that function is its implementation.
- **The core is stdlib-only.** `shapely`, `mapbox_vector_tile`, `rich` and
  `PIL` are extras, and every one of their imports sits inside the function that
  needs it. Hoisting one to module level breaks the bare install.
- **A cache reaches only `history`, `evidence` and `share`.** `_client(args,
  cached=True)` is the one switch, and `test_only_reporting_commands_get_a_cache`
  reads the source to check nobody else flips it. Planning, applying, checking,
  deleting and exporting against a cache would verify against yesterday.
  `client._many` runs reads four at a time; a write must never go through it.
- **Two tests guard the ethos rather than a behaviour** —
  `test_the_undo_methods_are_exactly_these_five` and
  `test_deletions_are_not_plan_ops`. A sixth removing method on the client, or a
  delete op kind, fails the suite on purpose. If a change seems to need one, that
  is the conversation in rule 2, not a fixture to update.

### Where new work goes

A trap found against the live API gets three things and not two: the behaviour in
`api/client.py`, a test in `tests/test_quirks.py` named after the incident, and a
row in the README's trap table. A comment saying "be careful" is the one outcome
the client's design rule forbids.

`workspace/` is gitignored and holds one real account's tracks, plans, logs and
one-off scripts. It is not the package and its scripts are not examples; anything
there that becomes generally useful moves into `src/wanderfill` with the personal
data stripped. See `workspace/WORKSPACE.md`.
