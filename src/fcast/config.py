"""Application settings, loaded from environment variables and an optional `.env` file."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    telegram_token: SecretStr | None = None
    telegram_chat_id: str | None = None
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
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
