"""Builds the list of active price sources from the settings."""

from collections.abc import Callable

from fcast.config import Settings
from fcast.sources import futbin
from fcast.sources.base import PriceSource
from fcast.sources.http import PoliteHttpClient
from fcast.sources.manual import ManualSource

# (ea_id, source name) -> stored external reference, e.g. a FUTBIN page path
RefLookup = Callable[[int, str], str | None]


def make_http_client(settings: Settings) -> PoliteHttpClient:
    return PoliteHttpClient(
        user_agent=settings.user_agent,
        min_interval=settings.http_min_interval_s,
        cache_ttl=settings.http_cache_ttl_s,
        max_retries=settings.http_max_retries,
    )


def build_sources(settings: Settings, lookup: RefLookup) -> list[PriceSource]:
    """Local sources are always read; web sources share the load per player."""
    sources: list[PriceSource] = []
    if settings.manual_csv is not None:
        sources.append(ManualSource(settings.manual_csv, settings.platform, settings.tz))
    for name in settings.web_sources:
        if name == futbin.SOURCE_NAME:
            sources.append(
                futbin.FutbinSource(
                    make_http_client(settings),
                    lambda ea_id: lookup(ea_id, futbin.SOURCE_NAME),
                )
            )
    return sources
