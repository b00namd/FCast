"""OpenLigaDB (api.openligadb.de): open, community-run football data for German leagues.

No key needed. Provides fixtures, results and goals with scorer; no assists or ratings.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fcast.sources.base import SourceError
from fcast.sources.http import PoliteHttpClient

BASE_URL = "https://api.openligadb.de"
LEAGUE_NAMES = {"bl1": "Bundesliga", "bl2": "2. Bundesliga", "bl3": "3. Liga"}
FINAL_RESULT_TYPE = 2  # "Endergebnis" (after 90 minutes)


@dataclass(frozen=True)
class Goal:
    scorer_id: int | None
    scorer: str
    team_id: int | None
    minute: int | None
    penalty: bool
    own_goal: bool


@dataclass(frozen=True)
class Match:
    ext_id: int
    league: str
    season: int
    matchday: int
    kickoff: datetime  # aware UTC
    home_id: int
    home: str
    away_id: int
    away: str
    finished: bool
    home_goals: int | None
    away_goals: int | None
    goals: tuple[Goal, ...]

    def team_name(self, team_id: int | None) -> str | None:
        if team_id == self.home_id:
            return self.home
        if team_id == self.away_id:
            return self.away
        return None

    def result_for(self, team_id: int | None) -> str | None:
        """'win', 'draw' or 'loss' for a team, None if unfinished or unknown team."""
        if not self.finished or self.home_goals is None or self.away_goals is None:
            return None
        if team_id == self.home_id:
            own, other = self.home_goals, self.away_goals
        elif team_id == self.away_id:
            own, other = self.away_goals, self.home_goals
        else:
            return None
        return "win" if own > other else "loss" if own < other else "draw"


def _kickoff(raw: dict[str, Any]) -> datetime:
    value = raw["matchDateTimeUTC"].replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def parse_matches(data: list[dict[str, Any]], league: str) -> list[Match]:
    matches = []
    for raw in data:
        final = next(
            (
                r
                for r in raw.get("matchResults") or []
                if r.get("resultTypeID") == FINAL_RESULT_TYPE
            ),
            None,
        )
        goals = tuple(
            Goal(
                scorer_id=g.get("goalGetterID") or None,
                scorer=(g.get("goalGetterName") or "").strip(),
                team_id=g.get("scoringTeamId"),
                minute=g.get("matchMinute"),
                penalty=bool(g.get("isPenalty")),
                own_goal=bool(g.get("isOwnGoal")),
            )
            for g in raw.get("goals") or []
        )
        matches.append(
            Match(
                ext_id=int(raw["matchID"]),
                league=league,
                season=int(raw["leagueSeason"]),
                matchday=int(raw["group"]["groupOrderID"]),
                kickoff=_kickoff(raw),
                home_id=int(raw["team1"]["teamId"]),
                home=raw["team1"]["teamName"],
                away_id=int(raw["team2"]["teamId"]),
                away=raw["team2"]["teamName"],
                finished=bool(raw.get("matchIsFinished")),
                home_goals=final["pointsTeam1"] if final else None,
                away_goals=final["pointsTeam2"] if final else None,
                goals=goals,
            )
        )
    return matches


class OpenLigaDbClient:
    def __init__(self, client: PoliteHttpClient) -> None:
        self._client = client

    async def current_matchday(self, league: str) -> int:
        data = await self._client.get_json(f"{BASE_URL}/getcurrentgroup/{league}")
        try:
            return int(data["groupOrderID"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"unexpected OpenLigaDB answer for {league}") from exc

    async def matchday(self, league: str, season: int, matchday: int) -> list[Match]:
        data = await self._client.get_json(f"{BASE_URL}/getmatchdata/{league}/{season}/{matchday}")
        if not isinstance(data, list):
            raise SourceError(f"unexpected OpenLigaDB answer for {league} {season}/{matchday}")
        return parse_matches(data, league)

    async def aclose(self) -> None:
        await self._client.aclose()
