"""Link scoring: how much a card could profit from an upcoming promo. Pure functions.

A card is linked to a promo when it *is* a leaked player or shares a club, league or nation
with the leaked cards (chemistry links drive demand). The score multiplies link strength,
timing, leak confidence, price vs. 7-day mean and market activity; weights come from the
settings so they can be tuned with the backtest later.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from fcast.analysis.stats import PriceStats
from fcast.config import Settings
from fcast.sources.futbin_locator import slugify
from fcast.totw.names import clubs_match

LINK_LABELS = {"player": "Spieler", "club": "Verein", "league": "Liga", "nation": "Nation"}


@dataclass(frozen=True)
class PromoWeights:
    player: float = 1.0
    club: float = 0.6
    league: float = 0.35
    nation: float = 0.3
    threshold: float = 45.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "PromoWeights":
        return cls(
            player=settings.promo_weight_player,
            club=settings.promo_weight_club,
            league=settings.promo_weight_league,
            nation=settings.promo_weight_nation,
            threshold=settings.promo_prebuy_threshold,
        )

    def of(self, link_type: str) -> float:
        return float(getattr(self, link_type, 0.0))


@dataclass(frozen=True)
class PromoInfo:
    promo_id: int
    name: str
    starts_at: datetime
    ends_at: datetime | None
    confidence: float
    links: tuple[tuple[str, str], ...]  # (link_type, value); player values are FUTBIN slugs


@dataclass(frozen=True)
class CardInfo:
    ea_id: int
    name: str
    slug: str | None  # FUTBIN slug of the card, if known
    club: str | None
    league: str | None
    nation: str | None
    play: float | None = None  # play value 0-100, if known


@dataclass
class PromoMatch:
    promo: PromoInfo
    card: CardInfo
    score: float
    link_strength: float
    timing: float
    days_to_start: float
    reasons: list[str] = field(default_factory=list)


def _same_name(a: str | None, b: str) -> bool:
    return bool(a) and slugify(a or "") == slugify(b)


def link_strength(
    card: CardInfo, promo: PromoInfo, weights: PromoWeights
) -> tuple[float, list[str]]:
    """Strongest link plus a small bonus for each additional kind of link."""
    found: dict[str, str] = {}
    for link_type, value in promo.links:
        if link_type in found:
            continue
        if link_type == "player" and value in (card.slug, slugify(card.name)):
            found["player"] = value
        elif link_type == "club" and clubs_match(card.club, value):
            found["club"] = value
        elif link_type == "league" and card.league and clubs_match(card.league, value):
            found["league"] = value
        elif link_type == "nation" and _same_name(card.nation, value):
            found["nation"] = value
    if not found:
        return 0.0, []
    strengths = sorted((weights.of(t) for t in found), reverse=True)
    strength = min(1.0, strengths[0] + 0.1 * (len(strengths) - 1))
    return strength, [_reason(t, v) for t, v in found.items()]


def _reason(link_type: str, value: str) -> str:
    if link_type == "player":
        return "ist selbst im Leak"
    article = "gleicher" if link_type == "club" else "gleiche"
    return f"{article} {LINK_LABELS[link_type]} wie der Leak ({value})"


def timing_factor(promo: PromoInfo, now: datetime) -> tuple[float, float]:
    """(factor, days until start). Best when the promo starts in 2-7 days."""
    days = (promo.starts_at - now).total_seconds() / 86400
    if days < 0:
        running = promo.ends_at is None or now <= promo.ends_at
        return (0.4 if running else 0.0), days
    if days < 2:
        return 0.7, days
    if days <= 7:
        return 1.0, days
    if days <= 14:
        return 0.6, days
    return 0.0, days


def price_factor(stats: PriceStats | None) -> float:
    """Cheap relative to the 7-day mean is better; no data is neutral-ish."""
    if stats is None or stats.deviation_pct is None:
        return 0.8
    return min(1.2, max(0.4, 1 - stats.deviation_pct / 25))


def liquidity_factor(stats: PriceStats | None) -> float:
    if stats is None or stats.changes_per_day is None:
        return 0.7
    return 0.5 + 0.5 * min(stats.changes_per_day / 12, 1.0)


def score_card(
    card: CardInfo,
    promo: PromoInfo,
    stats: PriceStats | None,
    now: datetime,
    weights: PromoWeights,
) -> PromoMatch | None:
    strength, reasons = link_strength(card, promo, weights)
    if strength <= 0:
        return None
    timing, days = timing_factor(promo, now)
    if timing <= 0:
        return None
    score = 100 * strength * timing * promo.confidence * price_factor(stats)
    score *= liquidity_factor(stats)
    if card.play is not None:  # strong cards profit more from a promo hype
        score *= 0.8 + 0.4 * card.play / 100
        if card.play >= 75:
            reasons.append(f"starke Karte (Spielwert {card.play:.0f})")
    if stats is not None and stats.deviation_pct is not None and stats.deviation_pct < -5:
        reasons.append(f"{stats.deviation_pct:+.0f} % unter Ø 7 Tage".replace(".", ","))
    return PromoMatch(
        promo=promo,
        card=card,
        score=round(min(score, 100.0), 1),
        link_strength=strength,
        timing=timing,
        days_to_start=days,
        reasons=reasons,
    )


def best_matches(
    cards: Sequence[tuple[CardInfo, PriceStats | None]],
    promos: Sequence[PromoInfo],
    now: datetime,
    weights: PromoWeights,
) -> list[PromoMatch]:
    """Strongest promo per card, best first."""
    best: dict[int, PromoMatch] = {}
    for card, stats in cards:
        for promo in promos:
            match = score_card(card, promo, stats, now, weights)
            if match is not None and (
                card.ea_id not in best or match.score > best[card.ea_id].score
            ):
                best[card.ea_id] = match
    return sorted(best.values(), key=lambda m: (-m.score, m.card.name))


def is_prebuy(match: PromoMatch, weights: PromoWeights) -> bool:
    """PROMO_PREBUY: strong enough and the promo starts in 2-7 days."""
    return match.score >= weights.threshold and 2 <= match.days_to_start <= 7
