"""Play value: how good a card is in the game (0-100), from its stats, PlayStyles and traits.

The value depends on the position: a striker needs pace and finishing, a centre back defending
and strength. 0-80 points come from the weighted stats (55 maps to 0, 95 to 80), up to 20 from
PlayStyle+ that matter in the position, skill moves, weak foot, AcceleRATE and height.

The weights are reasoned starting values, not certainties: `usage_agreement` checks them against
how often cards are really played (FUTBIN games), so they can be tuned with data.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from fcast.sources.base import CardAttributes

STRIKER, WINGER, PLAYMAKER, MIDFIELD, HOLDING, FULLBACK, DEFENDER, KEEPER = (
    "Sturm",
    "Flügel",
    "Spielmacher",
    "Mittelfeld",
    "Sechser",
    "Außenverteidiger",
    "Innenverteidiger",
    "Torwart",
)

GROUPS = {
    "ST": STRIKER,
    "CF": STRIKER,
    "LW": WINGER,
    "RW": WINGER,
    "LM": WINGER,
    "RM": WINGER,
    "CAM": PLAYMAKER,
    "CM": MIDFIELD,
    "CDM": HOLDING,
    "LB": FULLBACK,
    "RB": FULLBACK,
    "LWB": FULLBACK,
    "RWB": FULLBACK,
    "CB": DEFENDER,
    "GK": KEEPER,
}

WEIGHTS: dict[str, dict[str, float]] = {
    STRIKER: {
        "Acceleration": 3,
        "Sprint Speed": 2,
        "Finishing": 3,
        "Att. Position": 2,
        "Shot Power": 1.5,
        "Composure": 1.5,
        "Ball Control": 1.5,
        "Agility": 1.5,
        "Reactions": 1.5,
        "Dribbling": 1,
        "Strength": 1,
        "Long Shots": 1,
    },
    WINGER: {
        "Acceleration": 3,
        "Sprint Speed": 2.5,
        "Dribbling": 2.5,
        "Agility": 2,
        "Ball Control": 2,
        "Finishing": 1.5,
        "Crossing": 1,
        "Short Pass": 1,
        "Reactions": 1,
        "Composure": 1,
        "Long Shots": 1,
    },
    PLAYMAKER: {
        "Dribbling": 2,
        "Ball Control": 2,
        "Short Pass": 2,
        "Vision": 2,
        "Acceleration": 2,
        "Agility": 1.5,
        "Long Shots": 1.5,
        "Reactions": 1.5,
        "Composure": 1.5,
        "Finishing": 1,
        "Sprint Speed": 1,
    },
    MIDFIELD: {
        "Short Pass": 2.5,
        "Ball Control": 2,
        "Long Pass": 1.5,
        "Vision": 1.5,
        "Dribbling": 1.5,
        "Stamina": 1.5,
        "Reactions": 1.5,
        "Composure": 1.5,
        "Acceleration": 1.5,
        "Sprint Speed": 1,
        "Interceptions": 1,
        "Def. Aware": 1,
    },
    HOLDING: {
        "Interceptions": 2.5,
        "Def. Aware": 2.5,
        "Stand Tackle": 2,
        "Strength": 1.5,
        "Short Pass": 1.5,
        "Composure": 1.5,
        "Reactions": 1.5,
        "Stamina": 1,
        "Acceleration": 1,
        "Sprint Speed": 1,
        "Aggression": 1,
    },
    FULLBACK: {
        "Acceleration": 2.5,
        "Sprint Speed": 2.5,
        "Def. Aware": 2,
        "Stand Tackle": 2,
        "Interceptions": 1.5,
        "Stamina": 1.5,
        "Crossing": 1,
        "Short Pass": 1,
        "Agility": 1,
        "Reactions": 1,
    },
    DEFENDER: {
        "Def. Aware": 3,
        "Stand Tackle": 2.5,
        "Interceptions": 2,
        "Strength": 2,
        "Sprint Speed": 2,
        "Acceleration": 1.5,
        "Jumping": 1.5,
        "Heading Acc.": 1.5,
        "Reactions": 1.5,
        "Composure": 1,
        "Slide Tackle": 1,
    },
    KEEPER: {
        "Reflexes": 3,
        "Diving": 2,
        "Positioning": 2,
        "Handling": 1.5,
        "Kicking": 1,
        "Speed": 0.5,
    },
}

# PlayStyles that make a real difference in a position (PlayStyle+ counts most).
META_PLAYSTYLES: dict[str, frozenset[str]] = {
    STRIKER: frozenset(
        {
            "Finesse Shot",
            "Power Shot",
            "Quick Step",
            "Rapid",
            "Technical",
            "First Touch",
            "Low Driven Shot",
            "Chip Shot",
            "Acrobatic",
            "Precision Header",
        }
    ),
    WINGER: frozenset(
        {
            "Rapid",
            "Quick Step",
            "Technical",
            "Finesse Shot",
            "Trickster",
            "First Touch",
            "Flair",
            "Whipped Pass",
            "Low Driven Shot",
        }
    ),
    PLAYMAKER: frozenset(
        {
            "Technical",
            "Finesse Shot",
            "Incisive Pass",
            "Tiki Taka",
            "First Touch",
            "Quick Step",
            "Rapid",
            "Pinged Pass",
            "Power Shot",
        }
    ),
    MIDFIELD: frozenset(
        {
            "Tiki Taka",
            "Incisive Pass",
            "Pinged Pass",
            "Long Ball Pass",
            "Intercept",
            "Relentless",
            "Press Proven",
            "Technical",
            "First Touch",
        }
    ),
    HOLDING: frozenset(
        {
            "Intercept",
            "Anticipate",
            "Block",
            "Bruiser",
            "Jockey",
            "Relentless",
            "Tiki Taka",
            "Press Proven",
            "Aerial",
        }
    ),
    FULLBACK: frozenset(
        {
            "Rapid",
            "Quick Step",
            "Intercept",
            "Anticipate",
            "Jockey",
            "Whipped Pass",
            "Relentless",
            "Block",
        }
    ),
    DEFENDER: frozenset(
        {
            "Intercept",
            "Anticipate",
            "Block",
            "Bruiser",
            "Jockey",
            "Aerial",
            "Slide Tackle",
            "Quick Step",
            "Rapid",
        }
    ),
    KEEPER: frozenset({"Far Reach", "Footwork", "Deflector", "Rush Out", "Cross Claimer"}),
}

ATTACKING = {STRIKER, WINGER, PLAYMAKER}
BONUS_CAP = 20.0


@dataclass(frozen=True)
class PlayValue:
    score: float  # 0-100
    group: str  # position group (German)
    base: float  # weighted stat average
    reasons: tuple[str, ...]


def group_of(position: str | None) -> str | None:
    return GROUPS.get((position or "").upper())


def _weighted(stats: dict[str, int], weights: dict[str, float]) -> float | None:
    present = {name: w for name, w in weights.items() if name in stats}
    if sum(present.values()) < 0.6 * sum(weights.values()):
        return None  # too many stats missing for this position
    return sum(stats[name] * w for name, w in present.items()) / sum(present.values())


def play_value(attributes: CardAttributes | None, position: str | None) -> PlayValue | None:
    group = group_of(position)
    if attributes is None or group is None:
        return None
    base = _weighted(attributes.stats, WEIGHTS[group])
    if base is None:
        return None
    score = max(0.0, min((base - 55) / 40, 1.0)) * 80
    weights = WEIGHTS[group]
    top = sorted(
        (name for name in weights if name in attributes.stats),
        key=lambda name: (-attributes.stats[name] * weights[name], name),
    )[:3]
    reasons = [", ".join(f"{name} {attributes.stats[name]}" for name in top)]

    bonus = 0.0
    meta = META_PLAYSTYLES[group]
    plus_meta = [p for p in attributes.playstyles_plus if p in meta]
    plus_other = [p for p in attributes.playstyles_plus if p not in meta]
    normal_meta = [
        p for p in attributes.playstyles if p in meta and p not in attributes.playstyles_plus
    ]
    bonus += min(5 * len(plus_meta) + 2 * len(plus_other), 12)
    bonus += min(len(normal_meta), 4)
    if plus_meta or plus_other:
        reasons.append("PlayStyle+: " + ", ".join(plus_meta + plus_other))
    if group in ATTACKING and attributes.skills:
        bonus += {5: 3, 4: 2}.get(attributes.skills, 0)
    if group != KEEPER and attributes.weak_foot:
        bonus += {5: 3, 4: 2}.get(attributes.weak_foot, 0)
    if (attributes.skills or 0) >= 4 or (attributes.weak_foot or 0) >= 4:
        reasons.append(
            f"{attributes.skills or '-'}★ Skills / {attributes.weak_foot or '-'}★ schwacher Fuß"
        )
    if group in ATTACKING and attributes.accelerate == "Explosive":
        bonus += 2
        reasons.append("AcceleRATE Explosive")
    height = attributes.height_cm
    if group == DEFENDER and height is not None:
        if height >= 188:
            bonus += 2
            reasons.append(f"{height} cm groß")
        elif height <= 178:
            bonus -= 2
            reasons.append(f"nur {height} cm")
    if group == STRIKER and height is not None and height >= 188:
        bonus += 1
    score = max(0.0, min(score + min(bonus, BONUS_CAP), 100.0))
    return PlayValue(round(score, 1), group, round(base, 1), tuple(reasons))


def meta_score(play: float, usage_percentile: float | None) -> float:
    """Play value confirmed by real usage: 80 % play value, 20 % how much it is played."""
    if usage_percentile is None:
        return play
    return round(0.8 * play + 20 * usage_percentile, 1)


def percentiles(values: dict[int, float]) -> dict[int, float]:
    """Rank of each value among all values, 0 (lowest) to 1 (highest)."""
    if len(values) < 2:
        return {key: 0.5 for key in values}
    ordered = sorted(values.values())
    return {
        key: sum(1 for v in ordered if v < value) / (len(ordered) - 1)
        for key, value in values.items()
    }


def usage_agreement(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Spearman rank correlation between play value and games played (None if too few)."""
    if len(pairs) < 8:
        return None
    left = percentiles(dict(enumerate(p for p, _ in pairs)))
    right = percentiles(dict(enumerate(g for _, g in pairs)))
    xs = [left[i] for i in range(len(pairs))]
    ys = [right[i] for i in range(len(pairs))]
    try:
        return round(statistics.correlation(xs, ys), 2)
    except statistics.StatisticsError:
        return None
