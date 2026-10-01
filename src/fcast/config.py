"""Application settings, loaded from environment variables and an optional `.env` file."""

import re
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_clock_time(value: str) -> bool:
    """True for a 24-hour "HH:MM" time."""
    return re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) is not None


class Platform(StrEnum):
    """Transfer market platform. PlayStation and Xbox share one console market."""

    CONSOLE = "console"
    PC = "pc"


class Settings(BaseSettings):
    """FCast configuration. Every field maps to an `FCAST_*` environment variable."""

    model_config = SettingsConfigDict(
        env_prefix="FCAST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    platform: Platform = Platform.CONSOLE
    db_path: Path = Path("data/fcast.db")
    # Daily database backup at `backup_time` (local); default directory next to the database.
    backup_dir: Path | None = None
    backup_time: str = "03:30"
    backup_keep: int = Field(default=14, ge=1, le=365)
    # Push alerts via ntfy (self-hosted). Without URL alerts are only logged.
    ntfy_url: str | None = None
    ntfy_topic: str = "fcast"
    ntfy_token: SecretStr | None = None
    # Base URL of the dashboard, used for "open in dashboard" buttons in alerts.
    dashboard_url: str | None = None

    # Alert defaults; the dashboard can override them (stored in the database).
    alert_cooldown_h: float = Field(default=6.0, ge=0)
    quiet_hours_start: str = "00:00"
    quiet_hours_end: str = "07:00"
    alert_min_profit: int = Field(default=0, ge=0)
    collect_interval_min: int = Field(default=30, ge=1, le=24 * 60)
    # Local timezone for user-facing times and CSV timestamps without offset.
    timezone: str = "Europe/Berlin"

    # Manual price source: CSV file that is read on every collector run.
    manual_csv: Path | None = None

    # Comma-separated web sources in priority order. Empty disables web access.
    sources: str = "futbin,futnext"
    # "priority": ask sources in the order above; "rotate": alternate per player and run.
    source_strategy: Literal["priority", "rotate"] = "priority"
    # How long a source is left alone after it answered 403/429 or a bot challenge.
    source_pause_h: int = Field(default=24, ge=1, le=24 * 30)
    # Random delay (seconds) added to each scheduled run so requests don't hit fixed times.
    collect_jitter_s: int = Field(default=180, ge=0, le=1800)

    # --- Analysis (Phase 4) ---
    # Cheapest listing more than this % below the second one counts as outlier.
    outlier_gap_pct: float = Field(default=15.0, gt=0, lt=100)
    # BUY_DIP: price at least dip_pct below the 7-day mean and expected profit after tax
    # (selling at the mean) of at least min_margin_pct of the buy price and min_profit coins.
    dip_pct: float = Field(default=10.0, gt=0, lt=100)
    min_margin_pct: float = Field(default=5.0, ge=0)
    min_profit: int = Field(default=500, ge=0)
    # OVERPRICE_CHANCE (ÜV): score 0-100 from weighted components; signal at >= threshold.
    uev_threshold: float = Field(default=60.0, ge=0, le=100)
    uev_weight_supply: float = Field(default=0.40, ge=0)
    uev_weight_trend: float = Field(default=0.25, ge=0)
    uev_weight_headroom: float = Field(default=0.15, ge=0)
    uev_weight_liquidity: float = Field(default=0.20, ge=0)
    # Listing markup over the market price if the supply gap gives no better target.
    uev_min_markup_pct: float = Field(default=5.0, ge=0)
    # Suggested markup over the last known price for extinct cards.
    uev_extinct_markup_pct: float = Field(default=20.0, ge=0)

    # --- TOTW prediction ---
    # OpenLigaDB league shortcuts to follow (bl1 = Bundesliga, bl2, bl3).
    totw_leagues: str = "bl1,bl2,bl3"
    # First TOTW release (local date) and weekly release time; FUTBIN numbers weeks the same.
    totw_first_release: date = date(2026, 9, 16)
    totw_release_time: str = "19:00"
    totw_weight_goal: float = Field(default=3.0, ge=0)
    totw_weight_penalty_goal: float = Field(default=2.0, ge=0)
    totw_weight_brace: float = Field(default=2.0, ge=0)
    totw_weight_hattrick: float = Field(default=3.0, ge=0)
    totw_weight_win: float = Field(default=1.0, ge=0)
    totw_league_factors: str = "bl1:1.0,bl2:0.6,bl3:0.35"
    # How many top candidates get their FC card looked up on FUTBIN per week.
    totw_card_lookups: int = Field(default=10, ge=0, le=30)
    # The best linked candidates are priced in the candidate pool until a few days after the
    # release, so the backtest can learn how TOTW candidates' cards react.
    totw_pool_top: int = Field(default=5, ge=0, le=30)

    # --- Leak & promo radar ---
    # RSS feeds for the leak inbox as "name=url" pairs (robots.txt checked per feed).
    leak_feeds: str = (
        "fifauteam=https://fifauteam.com/feed/,realsport101=https://realsport101.com/feed.xml"
    )
    # Candidate pool: cards from promo leaks are priced every `pool_interval_h` hours,
    # at most `pool_per_run` cards per collector run (spreads the requests out).
    pool_interval_h: float = Field(default=12.0, ge=1)
    pool_per_run: int = Field(default=4, ge=0, le=50)
    # Link scoring: strength of a shared player/club/league/nation with leaked cards.
    promo_weight_player: float = Field(default=1.0, ge=0, le=1)
    promo_weight_club: float = Field(default=0.6, ge=0, le=1)
    promo_weight_league: float = Field(default=0.35, ge=0, le=1)
    promo_weight_nation: float = Field(default=0.3, ge=0, le=1)
    promo_prebuy_threshold: float = Field(default=45.0, ge=0, le=100)

    # --- Potential radar (market scanner) ---
    # FUTBIN lists (Popular, New Players, current TOTW) are read every `radar_universe_h`
    # hours; up to `radar_list_limit` cards per list. Each collector run prices up to
    # `radar_per_run` radar cards that were not checked for `radar_interval_h` hours and looks
    # up at most `radar_resolve_per_run` new ones. 0 cards per run switches the scanner off.
    radar_per_run: int = Field(default=15, ge=0, le=60)
    radar_interval_h: float = Field(default=6.0, ge=1)
    radar_universe_h: float = Field(default=12.0, ge=1)
    radar_list_limit: int = Field(default=150, ge=10, le=300)
    radar_resolve_per_run: int = Field(default=5, ge=0, le=30)
    radar_fodder_h: float = Field(default=6.0, ge=1)
    radar_trend_min_pct: float = Field(default=5.0, gt=0)
    radar_usage_accel: float = Field(default=1.5, gt=1)
    radar_fodder_min_pct: float = Field(default=8.0, gt=0)
    radar_alert_score: float = Field(default=70.0, ge=0, le=100)

    # --- Holo pairs ---
    # Signal HOLO_SPREAD when the holo trades at least this much above the normal card.
    holo_min_spread_pct: float = Field(default=30.0, ge=0)
    # Above this spread nobody pays the holo price for the normal card (e.g. launch prices of a
    # fresh TOTW holo); the normal card must be scarce and have some price history.
    holo_max_spread_pct: float = Field(default=150.0, gt=0)
    holo_min_history_h: float = Field(default=48.0, ge=0)
    # Holo partners of watched cards are priced every `holo_interval_h` hours.
    holo_interval_h: float = Field(default=2.0, ge=0.5)
    # Holo versions looked up on FUTBIN per collector run.
    holo_pairs_per_run: int = Field(default=3, ge=0, le=20)

    # Web dashboard (Basic Auth). The dashboard refuses to start without a password.
    web_user: str = "fcast"
    web_password: SecretStr | None = None
    web_host: str = "0.0.0.0"  # noqa: S104  (container port, published on the LAN only)
    web_port: int = Field(default=8000, ge=1, le=65535)

    # Outgoing HTTP (only used by web sources whose terms allow automated access).
    http_contact: str | None = None
    http_min_interval_s: float = Field(default=3.0, ge=3.0)
    http_cache_ttl_s: int = Field(default=300, ge=0)
    http_max_retries: int = Field(default=4, ge=0, le=10)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("quiet_hours_start", "quiet_hours_end", "totw_release_time", "backup_time")
    @classmethod
    def _valid_clock_time(cls, value: str) -> str:
        if not is_clock_time(value):
            raise ValueError(f"expected HH:MM, got {value!r}")
        return value

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, value: str) -> str:
        known = {"futbin", "futnext"}
        names = [name.strip().lower() for name in value.split(",") if name.strip()]
        unknown = set(names) - known
        if unknown:
            raise ValueError(f"unknown sources: {', '.join(sorted(unknown))}")
        return ",".join(names)

    @property
    def web_sources(self) -> list[str]:
        return [name for name in self.sources.split(",") if name]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def user_agent(self) -> str:
        from fcast import __version__

        contact = f"; {self.http_contact}" if self.http_contact else ""
        return f"FCast/{__version__} (private price tracker{contact})"

    @property
    def backup_path(self) -> Path:
        return self.backup_dir or self.db_path.parent / "backups"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
