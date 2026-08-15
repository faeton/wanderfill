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
| Legacy AJAX | `nomadmania.com/ajax/V2/`, `/ajax/my_series/` | `token` **form field** | all 69 series — WHS, KYE, TCC, and the rest |
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

The layer is **not decimated by zoom**: a test box over Rome returned the same
116 objects at z9 as at z15. Harvest at a coarse zoom and save thousands of
requests — 4,090 tiles instead of 8,959, for identical results.

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
1312, 208 Hungary → 1376, 87 Austria → 1378, 12 Greece → 1594. Validate every
id, and repair the failures with point-in-polygon against the region tiles.

### Series proximity is weak evidence

Measured against 392 objects a user had already ticked by hand: a 1 km radius
re-finds 33% of them while offering 2,770 candidates; 10 km re-finds 84% while
offering 9,839. Venue series pack hundreds of objects into one city centre and a
day-level track carries one point per city, so there is no honest threshold.
Match cities by **name** and airports by **distance**, and validate any rule
against what is already ticked before letting it add anything.

---

## Derived scores

Countries, UN, UN+, SLOW and YES are all computed from marked regions. There is
nothing to write.

- **YES** — "Years Elapsed Since": the sum, over every country visited, of the
  years since the last visit. **Lower is better.** Filling in old dates makes it
  worse, which surprises people; say so before doing it.
- **SLOW** — countries where 11 / 31 / 101 days were spent.
- **DEEP** — depth of coverage within countries.
- **KYE** — "Know Your Earth", a 448-item geographic list.
- **DARE** — extreme-travel areas, 1701 in the catalogue, marked binary.

`user/get-settings` also carries `homebase` and `homebase2` — the user's
declared home regions. Trip segmentation otherwise has to guess home from the
modal region of each year, so when these are set they are strictly better
evidence.

---

## robots.txt

`Disallow: /webapi/`. That is a crawler directive; it does not bind an
authenticated first-party client acting on its own account. It is still a clear
statement of intent, and it should shape behaviour: no unauthenticated access,
no enumerating reads, no indexing, no touching anyone else's data.
