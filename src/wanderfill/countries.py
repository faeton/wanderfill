"""Countries as forms name them: ISO codes, the groups forms use, and territories.

``wanderfill history`` answers questions somebody else wrote — a visa form, a
tax authority, ESTA — and those questions speak ISO countries and named blocs
("excluding the EU/EEA"), not NomadMania regions. This module is the one place
that translates between the two.

THREE THINGS THAT GO WRONG HERE

* **A group is a claim about membership on a date.** Croatia joined Schengen in
  2023, Bulgaria and Romania in 2025. A form filled in for travel back to 2016
  is asking about a different Schengen. Every group carries ``as_of`` and a
  source note, and the CLI prints the expansion, so the person can see that
  "EEA" became thirty specific codes and check them against the form.
* **Territories.** NomadMania's 196 countries are sovereign states; Greenland,
  the Canaries or Hong Kong are *regions* inside one of them. A region whose own
  flag matches no country but whose second flag does is a territory of that
  second one. It is labelled, never dropped: a Greenland trip that silently
  becomes "Denmark" can be missed by somebody reading a "Denmark?" answer, and
  a Greenland trip that silently vanishes is an omission on a legal form.
* **Names.** NomadMania spells countries its own way. The ISO table below is
  keyed by folded name with aliases, and :func:`validate` refuses to run if any
  of the live 196 fails to map — the same discipline as ``NON_UN`` in
  ``share.py``. A country that maps to nothing would otherwise fall out of every
  answer without a sound.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

# --------------------------------------------------------------------- ISO

# ISO 3166-1 alpha-2 -> canonical English name, then aliases NomadMania or a
# form may use. 193 UN members plus XK, TW, PS (NomadMania's 196).
ISO: dict[str, tuple[str, ...]] = {
    "AF": ("Afghanistan",),
    "AL": ("Albania",),
    "DZ": ("Algeria",),
    "AD": ("Andorra",),
    "AO": ("Angola",),
    "AG": ("Antigua and Barbuda", "Antigua"),
    "AR": ("Argentina",),
    "AM": ("Armenia",),
    "AU": ("Australia",),
    "AT": ("Austria",),
    "AZ": ("Azerbaijan",),
    "BS": ("Bahamas", "The Bahamas"),
    "BH": ("Bahrain",),
    "BD": ("Bangladesh",),
    "BB": ("Barbados",),
    "BY": ("Belarus",),
    "BE": ("Belgium",),
    "BZ": ("Belize",),
    "BJ": ("Benin",),
    "BT": ("Bhutan",),
    "BO": ("Bolivia",),
    "BA": ("Bosnia and Herzegovina", "Bosnia & Herzegovina", "Bosnia"),
    "BW": ("Botswana",),
    "BR": ("Brazil",),
    "BN": ("Brunei", "Brunei Darussalam"),
    "BG": ("Bulgaria",),
    "BF": ("Burkina Faso",),
    "BI": ("Burundi",),
    "CV": ("Cabo Verde", "Cape Verde"),
    "KH": ("Cambodia",),
    "CM": ("Cameroon",),
    "CA": ("Canada",),
    "CF": ("Central African Republic", "CAR"),
    "TD": ("Chad",),
    "CL": ("Chile",),
    "CN": ("China",),
    "CO": ("Colombia",),
    "KM": ("Comoros",),
    "CG": ("Republic of the Congo", "Congo", "Congo-Brazzaville", "Congo Republic"),
    "CD": (
        "Democratic Republic of the Congo",
        "DR Congo",
        "Congo DR",
        "DRC",
        "Congo-Kinshasa",
        "Congo (DRC)",
        "Congo, Democratic Republic",
    ),
    "CR": ("Costa Rica",),
    "CI": ("Côte d'Ivoire", "Ivory Coast", "Cote d'Ivoire"),
    "HR": ("Croatia",),
    "CU": ("Cuba",),
    "CY": ("Cyprus",),
    "CZ": ("Czechia", "Czech Republic"),
    "DK": ("Denmark",),
    "DJ": ("Djibouti",),
    "DM": ("Dominica",),
    "DO": ("Dominican Republic",),
    "EC": ("Ecuador",),
    "EG": ("Egypt",),
    "SV": ("El Salvador",),
    "GQ": ("Equatorial Guinea",),
    "ER": ("Eritrea",),
    "EE": ("Estonia",),
    "SZ": ("Eswatini", "Swaziland"),
    "ET": ("Ethiopia",),
    "FJ": ("Fiji",),
    "FI": ("Finland",),
    "FR": ("France",),
    "GA": ("Gabon",),
    "GM": ("Gambia", "The Gambia"),
    "GE": ("Georgia",),
    "DE": ("Germany",),
    "GH": ("Ghana",),
    "GR": ("Greece",),
    "GD": ("Grenada",),
    "GT": ("Guatemala",),
    "GN": ("Guinea",),
    "GW": ("Guinea-Bissau", "Guinea Bissau"),
    "GY": ("Guyana",),
    "HT": ("Haiti",),
    "HN": ("Honduras",),
    "HU": ("Hungary",),
    "IS": ("Iceland",),
    "IN": ("India",),
    "ID": ("Indonesia",),
    "IR": ("Iran",),
    "IQ": ("Iraq",),
    "IE": ("Ireland",),
    "IL": ("Israel",),
    "IT": ("Italy",),
    "JM": ("Jamaica",),
    "JP": ("Japan",),
    "JO": ("Jordan",),
    "KZ": ("Kazakhstan",),
    "KE": ("Kenya",),
    "KI": ("Kiribati",),
    "KP": ("North Korea", "Korea, North", "DPRK", "Korea DPR"),
    "KR": ("South Korea", "Korea, South", "Korea", "Republic of Korea"),
    "KW": ("Kuwait",),
    "KG": ("Kyrgyzstan", "Kyrgyz Republic"),
    "LA": ("Laos", "Lao PDR"),
    "LV": ("Latvia",),
    "LB": ("Lebanon",),
    "LS": ("Lesotho",),
    "LR": ("Liberia",),
    "LY": ("Libya",),
    "LI": ("Liechtenstein",),
    "LT": ("Lithuania",),
    "LU": ("Luxembourg",),
    "MG": ("Madagascar",),
    "MW": ("Malawi",),
    "MY": ("Malaysia",),
    "MV": ("Maldives",),
    "ML": ("Mali",),
    "MT": ("Malta",),
    "MH": ("Marshall Islands",),
    "MR": ("Mauritania",),
    "MU": ("Mauritius",),
    "MX": ("Mexico",),
    "FM": ("Micronesia", "Federated States of Micronesia"),
    "MD": ("Moldova",),
    "MC": ("Monaco",),
    "MN": ("Mongolia",),
    "ME": ("Montenegro",),
    "MA": ("Morocco",),
    "MZ": ("Mozambique",),
    "MM": ("Myanmar", "Burma"),
    "NA": ("Namibia",),
    "NR": ("Nauru",),
    "NP": ("Nepal",),
    "NL": ("Netherlands", "The Netherlands", "Holland"),
    "NZ": ("New Zealand",),
    "NI": ("Nicaragua",),
    "NE": ("Niger",),
    "NG": ("Nigeria",),
    "MK": ("North Macedonia", "Macedonia"),
    "NO": ("Norway",),
    "OM": ("Oman",),
    "PK": ("Pakistan",),
    "PW": ("Palau",),
    "PA": ("Panama",),
    "PG": ("Papua New Guinea",),
    "PY": ("Paraguay",),
    "PE": ("Peru",),
    "PH": ("Philippines",),
    "PL": ("Poland",),
    "PT": ("Portugal",),
    "QA": ("Qatar",),
    "RO": ("Romania",),
    "RU": ("Russia", "Russian Federation"),
    "RW": ("Rwanda",),
    "KN": ("Saint Kitts and Nevis", "St Kitts and Nevis", "St. Kitts and Nevis"),
    "LC": ("Saint Lucia", "St Lucia", "St. Lucia"),
    "VC": (
        "Saint Vincent and the Grenadines",
        "St Vincent and the Grenadines",
        "St. Vincent and the Grenadines",
        "Saint Vincent",
    ),
    "WS": ("Samoa",),
    "SM": ("San Marino",),
    "ST": ("São Tomé and Príncipe", "Sao Tome and Principe", "Sao Tome"),
    "SA": ("Saudi Arabia",),
    "SN": ("Senegal",),
    "RS": ("Serbia",),
    "SC": ("Seychelles",),
    "SL": ("Sierra Leone",),
    "SG": ("Singapore",),
    "SK": ("Slovakia",),
    "SI": ("Slovenia",),
    "SB": ("Solomon Islands",),
    "SO": ("Somalia",),
    "ZA": ("South Africa",),
    "SS": ("South Sudan",),
    "ES": ("Spain",),
    "LK": ("Sri Lanka",),
    "SD": ("Sudan",),
    "SR": ("Suriname",),
    "SE": ("Sweden",),
    "CH": ("Switzerland",),
    "SY": ("Syria",),
    "TJ": ("Tajikistan",),
    "TZ": ("Tanzania",),
    "TH": ("Thailand",),
    "TL": ("Timor-Leste", "East Timor", "Timor Leste"),
    "TG": ("Togo",),
    "TO": ("Tonga",),
    "TT": ("Trinidad and Tobago", "Trinidad & Tobago"),
    "TN": ("Tunisia",),
    "TR": ("Türkiye", "Turkey", "Turkiye"),
    "TM": ("Turkmenistan",),
    "TV": ("Tuvalu",),
    "UG": ("Uganda",),
    "UA": ("Ukraine",),
    "AE": ("United Arab Emirates", "UAE"),
    "GB": ("United Kingdom", "UK", "Great Britain", "Britain"),
    "US": ("United States", "USA", "United States of America", "US"),
    "UY": ("Uruguay",),
    "UZ": ("Uzbekistan",),
    "VU": ("Vanuatu",),
    "VA": ("Vatican City", "Vatican", "Holy See"),
    "VE": ("Venezuela",),
    "VN": ("Vietnam", "Viet Nam"),
    "YE": ("Yemen",),
    "ZM": ("Zambia",),
    "ZW": ("Zimbabwe",),
    # NomadMania's three beyond the UN.
    "XK": ("Kosovo",),
    "TW": ("Taiwan",),
    "PS": ("Palestine", "State of Palestine", "Palestinian Territory", "Palestinian Territories"),
}
NON_UN_ISO = frozenset({"XK", "TW", "PS"})
# In the table so a form can name it, but in neither the UN nor NomadMania's 196:
# the Vatican is a NomadMania *region*, and which country it sits under is the
# live data's business, not this table's.
NOT_COUNTED = frozenset({"VA"})


def fold(name: str) -> str:
    """Accents off, case off, punctuation off, 'saint' and 'st' the same."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = ["saint" if w == "st" else w for w in s.split() if w not in ("the", "and", "of")]
    return "".join(words)


_BY_NAME: dict[str, str] = {}
for _code, _names in ISO.items():
    for _n in _names:
        _BY_NAME[fold(_n)] = _code


def iso_for(name: str) -> str | None:
    return _BY_NAME.get(fold(name))


def name_of(code: str) -> str:
    return ISO[code][0] if code in ISO else code


# -------------------------------------------------------------------- groups


@dataclass(frozen=True)
class Group:
    members: frozenset[str]
    as_of: str
    note: str


_EU = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
)
_EFTA = frozenset({"IS", "LI", "NO", "CH"})
_EEA = _EU | {"IS", "LI", "NO"}

GROUPS: dict[str, Group] = {
    "eu": Group(_EU, "2026-10", "27 EU member states"),
    "eea": Group(_EEA, "2026-10", "EU + Iceland, Liechtenstein, Norway (not Switzerland)"),
    "efta": Group(_EFTA, "2026-10", "Iceland, Liechtenstein, Norway, Switzerland"),
    "schengen": Group(
        (_EU - {"IE", "CY"}) | _EFTA,
        "2026-10",
        "29 states; Croatia from 2023-01-01, Bulgaria and Romania fully from 2025-01-01; "
        "not Ireland or Cyprus",
    ),
    "cta": Group(
        frozenset({"GB", "IE"}),
        "2026-10",
        "UK + Ireland (Isle of Man and the Channel Islands are not NM countries)",
    ),
    "uk-form": Group(
        _EEA | {"US", "CA", "AU", "NZ", "CH"},
        "2026-10",
        "what the UK visitor form asks about separately: Australia, Canada, New Zealand, "
        "USA, Switzerland and the EEA",
    ),
    "vwp-restricted": Group(
        frozenset("IR IQ SY SD LY SO YE KP CU".split()),
        "2026-10",
        "ESTA: travel on or after 2011-03-01 — except Cuba, which counts from 2021-01-12",
    ),
    "five-eyes": Group(frozenset({"US", "GB", "CA", "AU", "NZ"}), "2026-10", "Five Eyes"),
    "gcc": Group(frozenset("SA AE QA KW BH OM".split()), "2026-10", "Gulf Cooperation Council"),
    "asean": Group(
        frozenset("BN KH ID LA MY MM PH SG TH VN TL".split()),
        "2026-10",
        "11 members; Timor-Leste from 2025-10-26",
    ),
    "mercosur": Group(
        frozenset("AR BR PY UY BO".split()),
        "2026-10",
        "full members; Bolivia from 2024, Venezuela suspended and excluded",
    ),
    "cis": Group(
        frozenset("AM AZ BY KZ KG MD RU TJ UZ".split()),
        "2026-10",
        "members as listed by the CIS; Turkmenistan associate, Georgia and Ukraine left",
    ),
    "commonwealth": Group(
        frozenset(
            "AG AU BS BD BB BZ BW BN CM CA CY DM SZ FJ GA GM GH GD GY IN JM KE KI LS MW MY "
            "MV MT MU MZ NA NR NZ NG PK PG RW KN LC VC WS SC SL SG SB ZA LK TZ TO TT TV UG "
            "GB VU ZM TG".split()
        ),
        "2026-10",
        "56 members; Gabon and Togo from 2022",
    ),
    "un": Group(frozenset(ISO) - NON_UN_ISO - NOT_COUNTED, "2026-10", "193 UN member states"),
    "un+": Group(
        frozenset(ISO) - NOT_COUNTED, "2026-10", "NomadMania's 196: UN + Kosovo, Taiwan, Palestine"
    ),
}
# Single-country conveniences that forms write as words.
ALIASES = {"uk": "GB", "usa": "US", "us": "US"}


class GroupError(ValueError):
    pass


def parse_groups(text: str | Iterable[str] | None) -> set[str]:
    """``"eea,ch,uk,us,-ie"`` -> a set of ISO codes.

    Groups and codes mix. A leading ``-`` subtracts, applied after all the
    additions so the order of the list does not matter. An unknown name is an
    error, never ignored: a typo in ``--exclude`` that silently excluded
    nothing would be harmless, but one in ``--countries`` would turn a
    question into "no" by reading the wrong list.
    """
    if text is None:
        return set()
    parts = text.split(",") if isinstance(text, str) else list(text)
    plus: set[str] = set()
    minus: set[str] = set()
    for raw in parts:
        tok = raw.strip()
        if not tok:
            continue
        target = minus if tok.startswith("-") else plus
        target |= _expand(tok.lstrip("-+").strip())
    return plus - minus


def _expand(tok: str) -> set[str]:
    low = tok.lower()
    if low in GROUPS:
        return set(GROUPS[low].members)
    if low in ALIASES:
        return {ALIASES[low]}
    if tok.upper() in ISO:
        return {tok.upper()}
    code = iso_for(tok)
    if code:
        return {code}
    raise GroupError(f"unknown country or group {tok!r}; groups: {', '.join(sorted(GROUPS))}")


def groups_used(text: str | None) -> list[str]:
    """Names of groups mentioned in a ``--countries`` / ``--exclude`` value."""
    if not text:
        return []
    return [
        t.strip().lstrip("-+").lower()
        for t in text.split(",")
        if t.strip().lstrip("-+").lower() in GROUPS
    ]


# ------------------------------------------------------- region -> country


UNMAPPED = "??"


@dataclass(frozen=True)
class Place:
    """Where a NomadMania region sits, in the vocabulary a form uses."""

    iso: str  # sovereign country, ISO alpha-2 — or UNMAPPED
    country: str  # display name of that country
    territory: str = ""  # region name when the region is a territory of ``iso``
    region: int = 0  # set only for an unmapped region, to keep each one apart

    def label(self, territories: str = "separate") -> str:
        if self.territory and territories == "separate":
            return f"{self.territory} ({self.country})"
        return self.country

    def key(self, territories: str = "separate") -> str:
        if self.iso == UNMAPPED:
            # Two unmapped regions are not one country. Sharing a key would
            # merge them into one trip and let a bridge run between them.
            return f"{UNMAPPED}/{self.region}"
        if self.territory and territories == "separate":
            return f"{self.iso}/{self.territory}"
        return self.iso


@dataclass
class CountryMap:
    """Region id -> :class:`Place`, built from live rows and checked on build."""

    places: dict[int, Place] = field(default_factory=dict)
    region_names: dict[int, str] = field(default_factory=dict)

    def place(self, region: int) -> Place:
        p = self.places.get(region)
        if p is not None:
            return p
        # Never drop an unmapped region: it becomes its own "country", loudly.
        name = self.region_names.get(region, f"region {region}")
        return Place(UNMAPPED, f"unmapped: {name}", region=region)

    def region_name(self, region: int) -> str:
        return self.region_names.get(region, f"region {region}")


class UnmappedCountries(RuntimeError):
    pass


def validate(country_rows: list[dict]) -> dict[str, str]:
    """NM country name -> ISO, or raise naming every country that maps to nothing."""
    out, missing = {}, []
    for r in country_rows:
        code = iso_for(str(r.get("country", "")))
        if code is None:
            missing.append(str(r.get("country")))
        else:
            out[str(r["country"])] = code
    if missing:
        raise UnmappedCountries(
            "these NomadMania countries have no ISO code in countries.py, so any "
            "visit to them would fall out of every answer: " + ", ".join(sorted(missing))
        )
    return out


def build_country_map(country_rows: list[dict], region_rows: dict[int, dict]) -> CountryMap:
    """Join regions to countries by flag, the way ``NomadMania.yes_scores`` does.

    ``country_rows`` is ``slow/get-slow-app`` (each with ``country`` and
    ``flag``); ``region_rows`` is ``region_years()`` or any region dict carrying
    ``flag1`` / ``flag2`` and ``name``. A region whose ``flag1`` is a country is
    in that country. One whose ``flag1`` matches nothing but whose ``flag2``
    does is a **territory** of the ``flag2`` country, labelled with its own
    name. One that matches neither stays unmapped and is reported, never
    dropped.
    """
    names = validate(country_rows)
    by_flag = {r["flag"]: r for r in country_rows if r.get("flag")}
    cmap = CountryMap()
    for rid, reg in region_rows.items():
        name = str(reg.get("name") or reg.get("region_name") or f"region {rid}")
        cmap.region_names[rid] = name
        own = by_flag.get(reg.get("flag1"))
        if own is not None:
            cmap.places[rid] = Place(names[own["country"]], name_of(names[own["country"]]))
            continue
        parent = by_flag.get(reg.get("flag2"))
        if parent is not None:
            code = names[parent["country"]]
            cmap.places[rid] = Place(code, name_of(code), territory=name)
    return cmap
