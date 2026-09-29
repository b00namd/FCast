"""FCast - price analysis and alerting for the EA FC Ultimate Team transfer market."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("fcast")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0"
