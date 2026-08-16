# NomadMania's private API — field notes

Reverse-engineered on 15 August 2026 against a live account. Unofficial,
incomplete, and certain to drift. Nothing here was obtained by anything other
than reading a logged-in browser session and the JavaScript it loads.

**Scope, deliberately limited.** This documents only what an account holder
needs to read and write *their own* data. There is no user enumeration, no
other-profile access, and no admin surface here, and there won't be. The line
between interoperability documentation and an abuse catalogue is where this
project's legitimacy sits.

---

## Three surfaces

| Surface | Base | Auth | Carries |
|---|---|---|---|
| Modern API | `nomadmania.com/webapi/<module>/<action>` | `NMTOKEN` header | regions, visits, trips, DARE, countries, geocoding |
| Legacy AJAX | `nomadmania.com/ajax/V2/`, `/ajax/my_series/` | `token` **form field** | all 69 series — WHS, TCC, and the rest. **Not KYE**, which has its own module |
| Tiles | `maps.nomadmania.travel/tiles/<layer>/{z}/{x}/{y}.pbf` | none | current polygons, and every series object's coordinate |

Every API call is a `POST` with an `application/x-www-form-urlencoded` body.

### Authentication

A bearer token from `localStorage.getItem('token')` on nomadmania.com, mirrored
into a `token` cookie. Issued by `user/login`, valid for a year. After first
login the password is never needed — which is convenient, and exactly why the
token should be treated as one.

```
NMTOKEN: <uuid>
LANG: en
platform: web
```

### Failure convention

Errors arrive with HTTP 200 and a body:

```json
{"result": "ERROR", "result_description": "Missing params: regions."}
```

So status codes tell you almost nothing. Parse the body.

---

## Modern API

### Catalogue

| Endpoint | Notes |
|---|---|
| `regions/get-regions-list-2` | The live catalogue, 1381 regions. The authority on which ids exist. |
| `regions/get-megaregions` | 28 mega-regions the map is organised by. |
| `quickEnter/get-regions` `{megaregion}` | Regions within one mega-region. |

### Reading your own state

| Endpoint | Returns |
|---|---|
| `maps/get-visited-regions-ids-simple` | flat id list — the cheapest state check available |
| `maps/get-visited-dare-ids-simple` | same for DARE; **trust this** over `get-regions-mqp` |
| `maps/get-visited-countries-ids-simple` | same for countries |
| `quickEnter/get-visits-to-region` `{region}` | every visit record for one region |
| `trips/get-trips-for-year-app` `{year}` | trip index by year |
| `trips/get-trip` `{trip_id}` | one trip with its regions |
| `slow/get-slow-app` | 196 countries: visited flag, `visited_regions`, `slow11/31/101`, per-country `yes` |
| `user/status` | `{result, status, admin}` — **no account id** |
| `user/status-quick` | the same plus `uid`, `messages`. Use this for identity. |
| `user/get-settings` | `user_id`, `homebase`, `homebase2`, and the rest of the profile |

A visit record looks like this:

```json
{
  "id": 13132624, "trip_id": null, "quality": 5,
  "year_from": 2015, "month_from": 3, "day_from": 4,
  "year_to": 2016, "month_to": 11, "day_to": 20
}
```

`quality` runs 0–6: no visit, transit, minimal visit, good visit, worked here,
lived here, travelguru.

### Writing

| Endpoint | Params | Notes |
|---|---|---|
| `quickEnter/add-visit` | `region, quality, year_from…day_to` | **auto-creates a trip** — see traps |
| `quickEnter/update-visit` | `id, region, quality, year_from…day_to` | **full replacement** — see traps |
| `quickEnter/updateMQP` | `region, visits` | binary DARE mark |
| `trips/new-trip` | `description, date_from, date_to, regions, regions_json` | needs **both** region fields |
| `trips/update-trip` | as above plus `trip_id` | |
| `trips/delete-trip` | `trip_id` | removes the trip *and* the visits it owns |

### Geocoding

```
location/get-region  {lat, lng, share}
→ {"result":"OK","nm":{"id":29},"dare":false,"country":"IT"}
```

**`share=0` is not optional.** This endpoint doubles as the live-location
beacon; called without it, it publishes the coordinate you asked about as your
current position. Geocoding an archive without `share=0` writes a fictional
travel diary in public.

**And `share` silently changes the response shape.** With `share=0` you get

```json
{"result": "OK", "region": 181}
```

without it you get the `nm`/`dare`/`country` form above. Parse only the second
and every coordinate comes back unplaced — which reads like a data problem, not
a parsing one. It also means `share=0` costs you the `dare` and `country`
fields; DARE membership has to come from the tiles instead.

---

## Legacy AJAX — the series

The series pages are not part of the modern app. `/series_single/<id>/` is a
shell page whose content lives in an **iframe** pointing at
`/nm_pages/series_single.html`. Grepping the outer page's 55 script bundles for
"series" finds nothing, because the logic is one document down. That cost an
hour; check for iframes before concluding an API does not exist.

```
read   POST /ajax/V2/
       action=getData & type=seriesSingle & id=<series> & lang=en & token=<token>

       → { title, description, items2:[…], visited:[ids],
           score, max, isos, links }

write  POST /ajax/my_series/
       action=toggle & item=<object> & state=0|1 & series=<series> & token=<token>
       → the literal string "OK"
```

`toggle2` exists for series that track two states per object. An object may
belong to several series at once, so ticking it moves more than one score.

Note the token travels in the **body** here, not in a header. Two auth
conventions on one site.

Known series ids: `22` World Heritage Sites (1273 objects), `1` World Capitals,
`3` European Cities, `4` Cities of the Americas, `5` Airports, `6` African
Cities, `7` Cities of Asia and Oceania, `20` Castles Palaces Forts, `21`
Religious Temples, `25` Architectural Delights, `58` Urban Legends, `73` Art
Museums. `series/get-list` on the modern surface enumerates all 69.

---

## Tiles

```
https://maps.nomadmania.travel/nomadmania-maps.json     the style
https://maps.nomadmania.travel/tiles/countries/{z}/{x}/{y}.pbf   z0-12
https://maps.nomadmania.travel/tiles/regions/{z}/{x}/{y}.pbf     z0-12
https://maps.nomadmania.travel/tiles/dare/{z}/{x}/{y}.pbf        z0-12
https://maps.nomadmania.travel/tiles/series2/{z}/{x}/{y}.pbf     z3-17
```

Bodies are **gzipped** without a `Content-Encoding` worth trusting — sniff for
`1f 8b` and decompress before decoding the MVT.

Two properties make this the most useful surface of the three:

**It is authoritative where the API is stale.** The region layer carries the
current ids; the reverse geocoder does not.

**`series2` geocodes every series at once.** Each object of every series appears
as a Point tagged with its own `id` — the same id `my_series/toggle` wants — and
its `series_id`:

```json
{"id": 78955, "series_id": 58, "name": "Rome: Trevi Fountain",
 "series_name": "Urban Legends", "no_visited": 3884}
```

The layer is **not decimated between z9 and z15**: a test box over Rome returned
the same 116 objects at either. Harvesting at z9 rather than z15 saves thousands
of requests — 4,090 tiles instead of 8,959, for identical results.

**That does not extend to arbitrarily coarse zooms, and reading it as "use the
coarsest zoom you like" is wrong.** A global sweep at **z4** returned **31** of
the 211 World Capitals; the same series harvested at **z6** returned **113**.
Somewhere below z9 the layer starts dropping points. z7 and z8 were never
measured, and neither was any series other than World Capitals, so **z9 is the
only zoom demonstrated to be complete** — treat anything coarser as unproven
rather than as a saving.

The cheaper trick is not a coarser zoom but **fewer tiles**: harvest at z9 but
only the tiles the user's own track passes through. For one profile that was 195 tiles at
z6 instead of 4,096 for the whole globe, and it cannot miss anything, because a
series object the user was never near is not a candidate anyway.

---

## Traps

Every one of these was hit for real, and each cost a wrong write or a wrong
number.

### `add-visit` silently creates a trip

Each new visit is wrapped in an auto-created single-region trip with an empty
description. You cannot switch it off. Forty-two of them appeared unannounced on
the first run.

### A visit counts twice as easily as once

A visit counts whether it is standalone **or** owned by a trip. Filtering
existing visits by `trip_id is None` therefore makes any region whose visit
lives inside an auto-created trip look empty — and a second visit gets added.
That mistake produced 50 duplicate visits.

The corollary matters when building trips: if every region already has a
standalone visit and you then create trips covering every journey, the totals
inflate by the number of first visits. 729 real visits became 999. Either the
trips own everything (which requires deletions), or each region's *first*
journey stays standalone and only *repeats* go into trips.

### `new-trip` requires a field it never reads

The site builds a trip object in JavaScript and pushes it through
`URLSearchParams`, so `regions` arrives as the literal string
`[object Object],[object Object]` while the real payload travels in
`regions_json`. The server checks the first key **exists** before it reads the
second. Send both.

```python
payload = {
    "description": "",
    "date_from": start.isoformat(),
    "date_to": end.isoformat(),
    "regions": ",".join("[object Object]" for _ in regions),
    "regions_json": json.dumps(regions),
}
```

### `update-visit` is a replacement, not a patch

Omitted fields are overwritten, not preserved. A hardcoded `quality: 3` turned a
region marked *lived here* into *good visit*. Always read-modify-write, and take
the maximum of existing and intended quality so an update can never downgrade.

### `visited` is not a boolean

`get-regions-mqp` returns `visited` as the area's own id rendered as a string —
`"1142"` when visited, `"0"` when not. Reading it as a boolean reports a false
zero. Use `maps/get-visited-dare-ids-simple`.

### The geocoder runs on stale polygons

Roughly **14%** of the ids `location/get-region` returns no longer exist in
`regions/get-regions-list-2`. Observed: 463 Cyprus → 1592/1593, 49 Portugal →
1312, 208 Hungary → 1376, 87 Austria → 1378/1379, 12 Greece → 1594, 1387
Switzerland → 1494/1508. Validate every id, and repair the failures with
point-in-polygon against the region tiles.

Note that one dead id can map to *several* live ones — Austria and Switzerland
were both split — so the repair has to be per-coordinate, not a lookup table.

`-1` is not a stale id; it is the server saying the point is over open water.
Leave those unresolved rather than snapping them to the nearest land.

### Series proximity is weak evidence

Measured against 392 objects a user had already ticked by hand: a 1 km radius
re-finds 33% of them while offering 2,770 candidates; 10 km re-finds 84% while
offering 9,839. Venue series pack hundreds of objects into one city centre and a
day-level track carries one point per city, so there is no honest threshold.
Match cities by **name** and airports by **distance**, and validate any rule
against what is already ticked before letting it add anything.

---

## KYE — the one list with no inference in it

**Know Your Earth** is not a series and is not on the legacy surface. It has its
own module, and the page describes it as manual — *"Mark quadrants as visited by
clicking the map."*

**It may not be entirely manual, and this is unresolved.** Nothing derives it
promptly: a profile with 391 regions and 103 countries read 0/434. But hours
after 93 quadrants were ticked deliberately, the count read 100 — and six of the
seven additions were boxes the account has **no coordinate inside**, in countries
it has visited (Greenland, Russia, Tajikistan, Brazil, Queensland, the Pacific).
Either the user ticked them by hand in the browser, or something server-side
derives quadrants from region visits on a delay, exactly as YES does. Nobody has
distinguished the two. **Do not tell a user KYE is purely manual**, and re-read
the count before and after any write rather than assuming your ticks are the only
ones that moved it. Nothing derives it from your regions,
so a profile with 391 regions and 103 countries can sit at **0**, as this one
did until it was filled in deliberately.

```
read   POST /webapi/kye/get-kye
       -> { result, visited: [qid…], max: 434, regions: [{qid, name}…] }

write  POST /webapi/kye/set-kye   { qid, visited: 0|1 }

shape  GET  /static/json/kye.json          the quadrant geometry the map draws
```

Cells are **10°×10° graticule boxes**, named `"50/40N  10/20E"` — the numbers
already carry their sign, so parsing is just the four integers. 469 rows are
returned against a `max` of 434, because the polar rows share names and are
grouped: `markQ` in the page JS special-cases `qid > 612`, toggling those as one.

**This is the only list on the site where a claim needs no judgement.** Regions
need polygons and have a 14% stale-id problem; series need a distance threshold
somebody has to choose; DARE needs point-in-polygon. A KYE cell is arithmetic on
a coordinate — inside the box or not. So the usual caveats about interpolation do
not apply, and the *evidence* is as strong as evidence gets.

One caveat does survive, and it is the same one as everywhere else: **a
coordinate is not a visit.** A photo taken from seat 27A puts a coordinate in a
mid-ocean cell, and on this profile several single-point cells are exactly that.
Grade by point count and by time spread, show the counts at several thresholds,
and let the user pick — the rule from §6 for series applies unchanged.

### Finding the endpoint, since it took three wrong turns

`/kye/` is a JPEG. The page is `/earth/`, and its API is invisible in that HTML
because the app is injected: `nm-toolkit.js` reads `target-engine-page="my-earth"`
off a div, fetches `/wp_pages/my-earth` for the markup, then `/wp_pages/my-earth.js`
for the behaviour. **That is the general pattern for this site** — `/wp_pages/<key>`
and `/wp_pages/<key>.js` for modern pages, `/nm_pages/<page>` for older ones.
Grep the `.js`, do not guess function names against the API.

---

## YES, in full

**This is the only YES section. If you find another one anywhere in this repo
that disagrees with it, this one is right and the other is a leftover** — that
happened once already, and an agent read the stale copy.

**YES = "Years Elapsed Since".** For each of the 196 countries (193 UN members
plus Palestine, Taiwan and Kosovo), score it:

| situation | score |
|---|---|
| visited in the current calendar year | 0 |
| visited in the previous calendar year | 0 — an explicit "gift" |
| visited before that | current year − year of last visit |
| **marked visited, no year recorded** | **8** (measured, not published — see below) |
| never visited | the traveller's age in years |

Sum all 196. **Lower is better** — it measures how *recently* you travel, not
how widely. Nothing is computed for anyone under 20.

### The undated row is measured, not documented

NomadMania publishes the first, second, third and fifth rows. It says nothing
about a country marked visited whose year is unknown. On one profile, across one
batch run, those read **8** — before the batch every visited country read 8, and
afterwards only the genuinely undated ones did. `UNDATED_YES` in `client.py` is
that number.

That is one account on one night. It has not been checked against a second
profile, a different age, or a different year. **Do not present it as the rule**,
and if a recomputation ever disagrees with `yes_stored` on undated countries,
suspect this constant before suspecting the server.

### Consequences, in the order they bite

1. **Undated costs 8, not your age.** The gap between "claimed but undated" and
   "dated recently" is 8 points; between "never been" and "dated recently" it is
   your whole age. Backfilling is therefore worth far less than it looks, and
   travelling somewhere new is worth far more.

2. **Dating a visit can gain or lose, and you must compute which.** Use
   `yes_delta(current, year)`. With an undated country at 8 in 2026: a 2026 or
   2025 visit gains 8, a 2019 visit gains 1, **2018 breaks even, and anything
   earlier is a loss** — Myanmar went 8 → 13 by being dated to 2013. The
   break-even moves every January, so do not carry "eight years" around as a rule.

3. **It may be worth nothing at all.** YES reads a country's *most recent* visit.
   Dating one region of a country that already has a later year elsewhere changes
   the country score by zero. Check `yes_scores()[cid]["last_year"]` first.

4. **A remembered year beats an invented day, and costs nothing.** YES reads only
   the year, so a `YearOnly` visit scores exactly what a precise date would.
   There is never a scoring reason to manufacture a day.

### The stored field lags — read it twice

`slow/get-slow-app`'s `yes` is batch-computed. It is **correct**, but between a
write and the next batch it reports the old world, and a single reading of it
misleads badly: at 02:00 it returned a flat 8 for all 100 visited countries; at
03:00, after a batch, 189 of 196 matched the rule above and the total had fallen
4736 → 4036. An afternoon went into concluding from the first reading that the
field was inert.

So: **read, write, wait for the batch, read again.** `region_years()` is the live
signal in between. `yes_scores()` recomputes the rule as a cross-check, but where
the two disagree the server is usually right — its territory handling (Greenland
under Denmark, Åland under Finland) beats matching regions to countries by flag.

Seven countries still disagree on the profile this was measured on, and nobody
has enumerated them. If you are relying on the recomputation, do that first.

---

## Derived scores

Countries, UN, UN+, SLOW and YES are all computed from marked regions. There is
nothing to write.

- **YES** — "Years Elapsed Since". Per country: 0 if visited this calendar year,
  0 if visited last calendar year (an explicit "gift"), otherwise the years
  since the last visit; and **your age in years for a country never visited**.
  Sum over all 196. **Lower is better.** Filling in old dates makes it worse,
  which surprises people; say so before doing it. A country marked visited but
  carrying no year anywhere scores the full age too, so it costs exactly as much
  as never having gone.

  See **YES, in full** above — the whole account lives there, including the
  fact that the stored field lags and that an undated country scores 8. Nothing
  about YES is repeated here on purpose.

- **SLOW** — countries where 11 / 31 / 101 days were spent.
- **DEEP** — depth of coverage within countries.
- **KYE** — "Know Your Earth". 434 scoring quadrants; see its own section above.
- **DARE** — extreme-travel areas, 1701 in the catalogue, marked binary.

`user/get-settings` also carries `homebase` and `homebase2` — the user's
declared home regions. Trip segmentation otherwise has to guess home from the
modal region of each year, so when these are set they are strictly better
evidence.

---

## Verification has no endpoint — do not go looking for one

Worth stating because an hour was spent looking. Badges are visible on a
profile, but **verification itself is out of band**: a committee picks a random
sample of what you claim — 60 regions, ~40 countries — and you answer by email
with documents, inside three months for countries and six for regions. Regional
verification wants at least 45 of the 60 answers to be *Class 1* evidence
(selfies with a landmark, serial photos within the region, dated diary entries,
hotel bills in your name, ATM slips with a place and a date); Class 2 — ordinary
photos, a friend vouching, a described route — covers the rest.

There is no `verification/*` module, nothing to upload, and nothing to poll. The
only thing a client can usefully do is what `wanderfill evidence` does: index
the user's own evidence against the regions they claim, locally, so the answer
exists before the question does.

The one API-adjacent consequence: verification is a *reason to be careful about
what gets written*, not merely to be careful about writing. A region added
without evidence is a region that has to be defended later by the account
holder, in person, to people who do this professionally.

---

## robots.txt

`Disallow: /webapi/`. That is a crawler directive; it does not bind an
authenticated first-party client acting on its own account. It is still a clear
statement of intent, and it should shape behaviour: no unauthenticated access,
no enumerating reads, no indexing, no touching anyone else's data.
