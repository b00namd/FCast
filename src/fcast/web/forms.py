"""Parsing of user input from dashboard forms."""

import re
from decimal import Decimal, InvalidOperation

_SUFFIX_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([kKmM])$")
_MULTIPLIERS = {"k": 1_000, "m": 1_000_000}
NBSP = chr(0xA0)


def parse_coins(text: str | None) -> int | None:
    """Coins as typed by a user: "1.200.000", "1,200,000", "1 200 000", "1.2M", "45k", "950".

    Empty input returns None. Raises ValueError for anything else or non-positive values.
    """
    if text is None or not text.strip():
        return None
    cleaned = text.strip().replace(NBSP, " ")
    match = _SUFFIX_RE.match(cleaned.replace(" ", ""))
    if match is not None:
        try:
            value = int(Decimal(match[1].replace(",", ".")) * _MULTIPLIERS[match[2].lower()])
        except InvalidOperation as exc:  # pragma: no cover - regex guarantees a number
            raise ValueError(f"invalid amount: {text!r}") from exc
    else:
        digits = re.sub(r"[\s.,']", "", cleaned)
        if not digits.isdigit():
            raise ValueError(f"invalid amount: {text!r}")
        value = int(digits)
    if value <= 0:
        raise ValueError("amount must be positive")
    return value


# Card links that contain the EA card (resource) id.
_EA_ID_LINKS = (
    re.compile(r"fut\.gg/players/[^/]+/\d{2}-(\d+)"),  # .../players/231747-kylian-mbappe/27-231747/
    re.compile(r"futnext\.com/(?:[a-z]{2}/)?players/[^/]+/(\d+)"),  # .../players/mbappe/231747
)


def parse_ea_id(text: str | None) -> int:
    """EA card id from a number or a FUT.GG/FUTNext card link."""
    value = (text or "").strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    for pattern in _EA_ID_LINKS:
        match = pattern.search(value)
        if match is not None and int(match[1]) > 0:
            return int(match[1])
    if "futbin.com" in value:
        raise ValueError(
            "Ein FUTBIN-Link enthält keine EA-ID. Bitte den FUT.GG-Link der Karte einfügen "
            "(FUTBIN-Link gehört ins Feld darunter)."
        )
    raise ValueError("EA-ID: Zahl oder FUT.GG-/FUTNext-Link der Karte einfügen")


def clean_text(text: str | None, max_length: int) -> str | None:
    value = (text or "").strip()
    return value[:max_length] or None
