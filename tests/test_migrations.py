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
    "portfolio_listings",
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


def test_portfolio_listings_migration_keeps_current_listings(tmp_path: Path) -> None:
    from sqlalchemy import text

    url = f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}"
    migrate.upgrade(url, "e2f7b9a4c618")
    engine = create_db_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO players (id, ea_id, updated_at) VALUES (1, 7, '2026-10-01')")
            )
            connection.execute(
                text(
                    "INSERT INTO portfolio (player_id, buy_price, bought_at, sell_price, status) "
                    "VALUES (1, 1000, '2026-10-01', 1500, 'listed'), "
                    "(1, 900, '2026-10-01', NULL, 'holding')"
                )
            )
        migrate.upgrade(url)
        with engine.begin() as connection:
            rows = connection.execute(
                text("SELECT position_id, price FROM portfolio_listings")
            ).all()
            assert [tuple(r) for r in rows] == [(1, 1500)]
            # Unknown buy price is allowed now.
            connection.execute(
                text(
                    "INSERT INTO portfolio (player_id, buy_price, bought_at, status) "
                    "VALUES (1, NULL, '2026-10-02', 'holding')"
                )
            )
        migrate.downgrade(url, "e2f7b9a4c618")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM portfolio")).scalar() == 2
    finally:
        engine.dispose()
