"""Matching player names across sources ("P. Schick" vs. FUTBIN "patrik-schick")."""

import re

from fcast.sources.futbin_locator import slugify

_INITIAL_RE = re.compile(r"^([A-Za-zÀ-ÿ])\.$")
# Tokens that say nothing about a club's identity.
# fmt: off
_CLUB_STOPWORDS = {
    "fc", "sv", "sc", "vfb", "vfl", "tsg", "fsv", "sg", "ev", "bv", "ssv", "de", "cf", "club",
    "1", "04", "05", "1846", "1848", "1860", "1899", "1900", "borussia", "eintracht",
}
# fmt: on


def name_parts(name: str) -> tuple[list[str], list[str]]:
    """Split a display name into (initials, full tokens): 'P. Schick' -> (['p'], ['schick'])."""
    initials: list[str] = []
    tokens: list[str] = []
    for part in name.split():
        match = _INITIAL_RE.match(part)
        if match is not None:
            initials.append(slugify(match[1]))
        else:
            tokens.extend(t for t in slugify(part).split("-") if t)
    return initials, tokens


def matches_slug(name: str, slug: str) -> bool:
    """True if a (possibly abbreviated) name fits a FUTBIN slug.

    Every full name token must appear in the slug; initials must start one of the other
    slug tokens. 'P. Schick' fits 'patrik-schick', 'Miguel Gutiérrez' fits
    'miguel-gutierrez-ortega', but 'H. Kane' does not fit 'harry-maguire'.
    """
    initials, tokens = name_parts(name)
    if not tokens:
        return False
    slug_tokens = slug.split("-")
    if not all(token in slug_tokens for token in tokens):
        return False
    rest = [t for t in slug_tokens if t not in tokens]
    return all(any(t.startswith(initial) for t in rest) for initial in initials)


def last_name(name: str) -> str | None:
    _, tokens = name_parts(name)
    return tokens[-1] if tokens else None


def club_tokens(name: str) -> set[str]:
    return {t for t in slugify(name).split("-") if len(t) >= 3 and t not in _CLUB_STOPWORDS}


def clubs_match(a: str | None, b: str | None) -> bool:
    """'Bayer 04 Leverkusen' ~ 'Leverkusen', 'FC Bayern München' ~ 'FC Bayern Munich' (bayern)."""
    if not a or not b:
        return False
    return bool(club_tokens(a) & club_tokens(b))
