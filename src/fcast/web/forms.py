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


def parse_ea_id(text: str | None) -> int:
    digits = (text or "").strip()
    if not digits.isdigit() or int(digits) <= 0:
        raise ValueError("EA-ID muss eine positive Zahl sein")
    return int(digits)


def clean_text(text: str | None, max_length: int) -> str | None:
    value = (text or "").strip()
    return value[:max_length] or None
