"""FastAPI application: dashboard plus collector/scheduler in one process."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fcast import __version__
from fcast.collector.service import Collector, create_scheduler
from fcast.config import Settings, get_settings
from fcast.web import routes
from fcast.web.chem import GROUP_LABELS, chem_group
from fcast.web.security import SameOriginMiddleware, require_auth

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
EMPTY = chr(0x2013)  # en dash shown for missing values


def format_coins(value: int | None) -> str:
    return EMPTY if value is None else f"{value:,}".replace(",", ".")


def format_pct(value: float | None) -> str:
    if value is None:
        return EMPTY
    return f"{value:+.1f} %".replace(".", ",")


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    tz = settings.tz

    def format_dt(value: datetime | None, fmt: str = "%d.%m. %H:%M") -> str:
        return EMPTY if value is None else value.astimezone(tz).strftime(fmt)

    templates.env.filters["coins"] = format_coins
    templates.env.filters["pct"] = format_pct
    templates.env.filters["dt"] = format_dt
    templates.env.filters["chem_group"] = chem_group
    templates.env.filters["chem_group_label"] = lambda name: GROUP_LABELS[chem_group(name)]
    templates.env.globals["version"] = __version__
    templates.env.globals["platform_label"] = settings.platform.value.upper()
    return templates


def create_app(
    settings: Settings | None = None,
    collector: Collector | None = None,
    run_scheduler: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.web_password is None or not settings.web_password.get_secret_value():
        raise RuntimeError("FCAST_WEB_PASSWORD must be set to start the dashboard")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = collector or Collector(settings)
        app.state.collector = active
        app.state.collect_task = None
        app.state.background_tasks = set()
        scheduler: Any = None
        if run_scheduler:
            scheduler = create_scheduler(
                active, settings.collect_interval_min, settings.collect_jitter_s
            )
            scheduler.start()
            logger.info("collector scheduled every %d min", settings.collect_interval_min)
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            pending = [app.state.collect_task, *app.state.background_tasks]
            for task in pending:
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            await active.aclose()

    app = FastAPI(
        title="FCast",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.templates = build_templates(settings)
    app.add_middleware(SameOriginMiddleware)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(routes.public)
    app.include_router(routes.pages, dependencies=[Depends(require_auth)])
    return app
