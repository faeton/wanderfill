"""Comparing place names written by different people in different alphabets."""

from __future__ import annotations

import re
import unicodedata

_STRIP = re.compile(r"[^a-z]")
_SPLIT = re.compile(r"[/(),–—-]")


def fold(name: str) -> str:
    """Reduce a place name to something comparable.

    ``Nukuʻalofa`` and ``Nukualofa`` must match; so must ``Jönköping`` and
    ``Jonkoping``. Accents are folded, punctuation dropped, case removed.
    """
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return _STRIP.sub("", ascii_.lower())


def components(name: str) -> list[str]:
    """Split a compound object name into candidate place names.

    Series objects are often written as ``La Paz/El Alto (BO)`` or
    ``Austin/Round Rock (TX)``. Any component matching the track is evidence;
    fragments shorter than four letters are dropped as too collision-prone.
    """
    out = []
    for part in _SPLIT.split(name):
        folded = fold(part)
        if len(folded) > 3:
            out.append(folded)
    return out
