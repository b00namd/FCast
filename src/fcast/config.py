"""Application settings, loaded from environment variables and an optional `.env` file."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
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

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
