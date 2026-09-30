from pathlib import Path

import pytest
from pydantic import ValidationError

from fcast.config import Platform, Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "FCAST_PLATFORM",
        "FCAST_DB_PATH",
        "FCAST_NTFY_TOKEN",
        "FCAST_NTFY_URL",
        "FCAST_COLLECT_INTERVAL_MIN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.platform is Platform.CONSOLE
    assert settings.db_path == Path("data/fcast.db")
    assert settings.ntfy_token is None
    assert settings.ntfy_url is None
    assert settings.ntfy_topic == "fcast"
    assert settings.collect_interval_min == 30


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FCAST_PLATFORM", "pc")
    monkeypatch.setenv("FCAST_DB_PATH", "/data/test.db")
    monkeypatch.setenv("FCAST_NTFY_TOKEN", "secret-token")
    monkeypatch.setenv("FCAST_NTFY_URL", "http://ntfy.local")
    monkeypatch.setenv("FCAST_COLLECT_INTERVAL_MIN", "15")

    settings = Settings(_env_file=None)

    assert settings.platform is Platform.PC
    assert settings.db_url == "sqlite:////data/test.db"
    assert settings.ntfy_token is not None
    assert settings.ntfy_token.get_secret_value() == "secret-token"
    assert "secret-token" not in repr(settings)
    assert settings.ntfy_url == "http://ntfy.local"
    assert settings.collect_interval_min == 15


def test_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FCAST_PLATFORM=pc\nFCAST_COLLECT_INTERVAL_MIN=60\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.platform is Platform.PC
    assert settings.collect_interval_min == 60


@pytest.mark.parametrize(
    ("key", "value"), [("FCAST_PLATFORM", "switch"), ("FCAST_COLLECT_INTERVAL_MIN", "0")]
)
def test_invalid_values(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["7:00", "24:00", "07:60", "abc"])
def test_invalid_quiet_hours(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FCAST_QUIET_HOURS_START", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
