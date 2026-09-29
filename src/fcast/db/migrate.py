"""Programmatic access to the Alembic migrations shipped inside the package."""

from alembic import command
from alembic.config import Config

SCRIPT_LOCATION = "fcast.db:migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)
