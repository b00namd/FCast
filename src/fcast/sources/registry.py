"""Builds the list of active price sources from the settings."""

from fcast.config import Settings
from fcast.sources.base import PriceSource
from fcast.sources.manual import ManualSource


def build_sources(settings: Settings) -> list[PriceSource]:
    """Sources in priority order; the collector uses the first one that has a price."""
    sources: list[PriceSource] = []
    if settings.manual_csv is not None:
        sources.append(ManualSource(settings.manual_csv, settings.platform, settings.tz))
    return sources
