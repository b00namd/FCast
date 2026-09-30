"""TOTW candidate score from real-life performances. Pure functions, weights in the config.

With OpenLigaDB only goals and team results are known, so the score favours scorers;
ratings from a paid API can be added later as another component.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from fcast.config import Settings
from fcast.totw.openligadb import Match


@dataclass(frozen=True)
class TotwWeights:
    goal: float = 3.0
    penalty_goal: float = 2.0
    brace_bonus: float = 2.0
    hattrick_bonus: float = 3.0
    win: float = 1.0
    league_factors: tuple[tuple[str, float], ...] = (("bl1", 1.0), ("bl2", 0.6), ("bl3", 0.35))

    @classmethod
    def from_settings(cls, settings: Settings) -> "TotwWeights":
        factors = []
        for item in settings.totw_league_factors.split(","):
            league, _, factor = item.partition(":")
            if league.strip() and factor.strip():
                factors.append((league.strip(), float(factor)))
        return cls(
            goal=settings.totw_weight_goal,
            penalty_goal=settings.totw_weight_penalty_goal,
            brace_bonus=settings.totw_weight_brace,
            hattrick_bonus=settings.totw_weight_hattrick,
            win=settings.totw_weight_win,
            league_factors=tuple(factors),
        )

    def league_factor(self, league: str) -> float:
        return dict(self.league_factors).get(league, 0.3)


@dataclass
class Candidate:
    key: str  # source player id or name
    name: str
    team: str
    league: str
    goals: int = 0
    penalty_goals: int = 0
    matches: int = 0
    wins: int = 0
    score: float = 0.0
    details: list[str] = field(default_factory=list)

    @property
    def reasons(self) -> str:
        parts = []
        if self.goals:
            goal_text = f"{self.goals} Tor{'e' if self.goals != 1 else ''}"
            if self.penalty_goals:
                goal_text += f" ({self.penalty_goals} Elfer)"
            parts.append(goal_text)
        if self.goals >= 3:
            parts.append("Hattrick")
        elif self.goals == 2:
            parts.append("Doppelpack")
        if self.wins:
            parts.append("Sieg" if self.wins == 1 else f"{self.wins} Siege")
        return " · ".join(parts)


def score_candidates(
    matches: Iterable[Match], weights: TotwWeights, min_goals: int = 1
) -> list[Candidate]:
    """One candidate per scorer; own goals do not count. Strongest first."""
    by_player: dict[str, Candidate] = {}
    per_match_goals: dict[tuple[str, int], int] = defaultdict(int)
    for match in matches:
        if not match.finished:
            continue
        for goal in match.goals:
            if goal.own_goal or not goal.scorer:
                continue
            key = f"{match.league}:{goal.scorer_id}" if goal.scorer_id else goal.scorer
            team = match.team_name(goal.team_id) or ""
            candidate = by_player.setdefault(
                key, Candidate(key=key, name=goal.scorer, team=team, league=match.league)
            )
            candidate.goals += 1
            candidate.penalty_goals += int(goal.penalty)
            if per_match_goals[(key, match.ext_id)] == 0:
                candidate.matches += 1
                if match.result_for(goal.team_id) == "win":
                    candidate.wins += 1
            per_match_goals[(key, match.ext_id)] += 1
            minute = f"{goal.minute}'" if goal.minute is not None else ""
            candidate.details.append(
                f"{minute} vs. {match.away if team == match.home else match.home}".strip()
            )

    for (key, _), goals in per_match_goals.items():
        candidate = by_player[key]
        if goals >= 3:
            candidate.score += weights.hattrick_bonus + weights.brace_bonus
        elif goals == 2:
            candidate.score += weights.brace_bonus
    for candidate in by_player.values():
        open_play = candidate.goals - candidate.penalty_goals
        candidate.score += open_play * weights.goal + candidate.penalty_goals * weights.penalty_goal
        candidate.score += candidate.wins * weights.win
        candidate.score = round(candidate.score * weights.league_factor(candidate.league), 2)

    found = [c for c in by_player.values() if c.goals >= min_goals]
    return sorted(found, key=lambda c: (-c.score, -c.goals, c.name))
