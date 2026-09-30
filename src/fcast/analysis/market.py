"""Supply-side metrics derived from the lowest BIN listings."""

from collections.abc import Sequence


def effective_price(listings: Sequence[int], outlier_gap_pct: float) -> int | None:
    """Market price from the lowest BINs, ignoring a lone underpriced listing.

    If the cheapest listing is more than `outlier_gap_pct` below the second one, it is most
    likely a mistake or a snipe that disappears within seconds; the second listing is then the
    realistic price. Returns None when there are no listings (extinct).
    """
    ordered = sorted(listings)
    if not ordered:
        return None
    if len(ordered) >= 2 and ordered[0] < ordered[1] * (1 - outlier_gap_pct / 100):
        return ordered[1]
    return ordered[0]


def supply_gap_pct(listings: Sequence[int]) -> float | None:
    """Relative gap between the two cheapest listings in percent (thin supply indicator)."""
    ordered = sorted(listings)
    if len(ordered) < 2:
        return None
    return (ordered[1] - ordered[0]) / ordered[0] * 100


def headroom_pct(price: int, range_max: int | None) -> float | None:
    """How far the price may still rise before hitting EA's maximum, in percent."""
    if range_max is None or price <= 0:
        return None
    return max(0.0, (range_max - price) / price * 100)
