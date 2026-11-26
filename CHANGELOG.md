# Changelog

What changed for somebody driving a profile with this package. The commit
history has the rest.

## 2026-11-25

NomadMania changed three things under the package. Each is written down here
and checked against the live API on 2026-11-25.

- **ATLAS replaced UN+.** On 11 November 2026 NomadMania replaced UN+ (265
  "UN countries and territories") with ATLAS, 137 "Additional Territories,
  Lands and States" that contain no UN member. UN is the 193 members.
  `wanderfill share` now draws ATLAS: `score_atlas` out of the profile's own
  `atlas_count`. It refuses a profile that lacks either. The API still returns
  `score_unp`, but the site no longer shows UN+, so the card leaves it out.
  `Stats.un_plus` and `Stats.un_plus_total` are now `Stats.atlas` and
  `Stats.atlas_total`, and `stats.json` changed with them. The `UN_PLUS_TOTAL`
  constant is gone.
- **The website stopped asking for Visit Quality**, starting with the trip
  entry of December 2025: "transit is not a visit". The API still stores and
  returns a quality on every visit. `QUALITY`, `MARKS_REGION_FROM` and
  `not_counted` behave as before. AGENTS.md §5 and the client docstring now say
  that the scale belongs to the API and the page no longer shows it.
- **The server placed some ticks itself.** When regions were split in 2022, a
  tick on the old region moved to the first-named new region. AGENTS.md §6a
  now tells you to list those regions before a verification and hand them to
  `evidence`, as a report. Nobody has published how visits moved in the 2026
  revision.
- The catalogue holds 1381 regions. Nothing in the code hardcodes that number:
  `share` and the rest read it live. The docs now give the date it was read.
