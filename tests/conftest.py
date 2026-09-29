from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from fcast.config import get_settings
from fcast.db import models  # noqa: F401  (registers tables)
from fcast.db.base import Base
from fcast.db.session import create_db_engine, create_session_factory


@pytest.fixture
def session() -> Iterator[Session]:
    """Session on a fresh in-memory database built from the ORM metadata."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as db_session:
        yield db_session
    engine.dispose()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the application settings at a temporary database file."""
    path = tmp_path / "fcast.db"
    monkeypatch.setenv("FCAST_DB_PATH", str(path))
    monkeypatch.setenv("FCAST_SOURCES", "")  # tests never talk to real websites
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()
