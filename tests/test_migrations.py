from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from fcast.db import (
    migrate,
    models,  # noqa: F401  (registers tables)
)
from fcast.db.base import Base
from fcast.db.session import create_db_engine

EXPECTED_TABLES = {
    "players",
    "price_snapshots",
    "watchlist",
    "portfolio",
    "promos",
    "promo_links",
    "alerts_log",
    "source_refs",
    "source_status",
    "market_state",
    "app_settings",
}


def test_upgrade_on_fresh_database(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'sub' / 'fresh.db').as_posix()}"
    migrate.upgrade(url)

    engine = create_db_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert tables >= EXPECTED_TABLES
        assert "alembic_version" in tables

        # The migrated schema must match the ORM models exactly.
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert diff == []
    finally:
        engine.dispose()


def test_upgrade_is_idempotent_and_downgrade_works(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}"
    migrate.upgrade(url)
    migrate.upgrade(url)
    migrate.downgrade(url, "base")

    engine = create_db_engine(url)
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()
