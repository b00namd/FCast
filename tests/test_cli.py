import pytest
from typer.testing import CliRunner

from fcast import __version__
from fcast.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"fcast {__version__}"


def test_config_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from fcast.config import get_settings

    monkeypatch.setenv("FCAST_TELEGRAM_TOKEN", "super-secret")
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["config"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0
    assert "super-secret" not in result.output
    assert "platform" in result.output
