"""Alembic environment. The database URL comes from the Alembic config or FCast settings."""

from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext

from fcast.config import get_settings
from fcast.db import models  # noqa: F401  (registers all tables on Base.metadata)
from fcast.db.base import Base, UTCDateTime
from fcast.db.session import create_db_engine

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().db_url


def _render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    # Keep migrations independent of application code.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=_render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
