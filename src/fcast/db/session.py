"""Engine and session factories."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def create_db_engine(url: str) -> Engine:
    """Create an SQLite engine with foreign keys enforced.

    In-memory databases share one connection so all sessions see the same data.
    File databases get their parent directory created and use WAL for concurrent reads.
    """
    parsed = make_url(url)
    in_memory = parsed.database in (None, "", ":memory:")
    kwargs: dict[str, Any] = {}
    if in_memory:
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    elif parsed.database is not None:
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if not in_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    with factory() as session, session.begin():
        yield session
