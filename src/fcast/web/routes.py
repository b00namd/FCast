"""Dashboard routes. Pages render full templates; HTMX requests get partials."""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from fcast.analysis import service as analysis
from fcast.analysis import signals as sig
from fcast.collector.service import JOB_ID, Collector
from fcast.config import Platform, Settings
from fcast.db import repositories as repo
from fcast.db.base import utcnow
from fcast.db.models import Player
from fcast.db.session import session_scope
from fcast.sources import futbin
from fcast.web import forms, views

logger = logging.getLogger(__name__)

public = APIRouter()
pages = APIRouter()

MANUAL_SOURCE = "manual"
FormStr = Annotated[str, Form()]
OptionalFormStr = Annotated[str | None, Form()]


# --- helpers ---------------------------------------------------------------


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _collector(request: Request) -> Collector:
    collector: Collector = request.app.state.collector
    return collector


@contextmanager
def _db(request: Request) -> Iterator[Session]:
    with session_scope(_collector(request).session_factory) as session:
        yield session


def _render(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, template, context, status_code=status_code
    )
    return response


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _player_or_404(session: Session, ea_id: int) -> Player:
    player = repo.get_player_by_ea_id(session, ea_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Spieler nicht gefunden")
    return player


# --- health ----------------------------------------------------------------


@public.get("/health")
def health(request: Request) -> JSONResponse:
    try:
        with _db(request) as session:
            session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health check failed")
        return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
    last = _collector(request).last_result
    return JSONResponse(
        {
            "status": "ok",
            "collector_running": _collector(request).running,
            "last_run": last.finished_at.isoformat() if last and last.finished_at else None,
        }
    )


# --- overview --------------------------------------------------------------


@pages.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/prices", status_code=303)


@pages.get("/prices", response_class=HTMLResponse)
def prices(request: Request) -> HTMLResponse:
    settings = _settings(request)
    now = utcnow()
    with _db(request) as session:
        rows = views.price_overview(session, settings.platform, now)
        analyses = {a.player.ea_id: a for a in analysis.analyze_watchlist(session, settings, now)}
        return _render(
            request, "prices.html", {"rows": rows, "analyses": analyses, "nav": "prices"}
        )


@pages.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request, rule: str | None = None) -> HTMLResponse:
    selected = next((r for r in sig.Rule if r.value == rule), None)
    with _db(request) as session:
        found = analysis.current_signals(session, _settings(request), utcnow(), selected)
        return _render(
            request,
            "signals.html",
            {
                "signals": found,
                "rules": list(sig.Rule),
                "labels": sig.RULE_LABELS,
                "selected": selected,
                "nav": "signals",
            },
        )


# --- watchlist -------------------------------------------------------------


def _watch_rows(session: Session) -> list[tuple[Any, dict[str, str]]]:
    entries = repo.list_watchlist(session, active_only=False)
    return [(entry, repo.list_source_refs(session, entry.player)) for entry in entries]


def _row_context(session: Session, ea_id: int) -> dict[str, Any]:
    player = _player_or_404(session, ea_id)
    entry = repo.get_watch(session, player)
    if entry is None:
        raise HTTPException(status_code=404, detail="Nicht auf der Watchlist")
    return {"entry": entry, "refs": repo.list_source_refs(session, player)}


class WatchInput:
    """Validated watchlist form values; `errors` lists problems in German."""

    def __init__(
        self,
        name: str | None,
        buy: str | None,
        sell: str | None,
        futbin_url: str | None,
        note: str | None,
    ) -> None:
        self.errors: list[str] = []
        self.name = forms.clean_text(name, 100)
        self.note = forms.clean_text(note, 500)
        self.buy = self._coins("Ziel-Kaufpreis", buy)
        self.sell = self._coins("Ziel-Verkaufspreis", sell)
        self.futbin_ref: str | None = None
        if futbin_url and futbin_url.strip():
            try:
                self.futbin_ref = futbin.normalize_ref(futbin_url)
            except ValueError:
                self.errors.append("FUTBIN-Link ungültig (erwartet …/27/player/<id>/<name>)")
        self.warning = None
        if self.buy is not None and self.sell is not None and self.sell * 0.95 <= self.buy:
            self.warning = "Verkaufsziel liegt nach 5 % Steuer nicht über dem Kaufziel."

    def _coins(self, label: str, value: str | None) -> int | None:
        try:
            return forms.parse_coins(value)
        except ValueError:
            self.errors.append(f"{label}: ungültiger Betrag „{value}“")
            return None


def _apply_watch(session: Session, player: Player, data: WatchInput, update_refs: bool) -> None:
    if data.name:
        repo.upsert_player(session, player.ea_id, repo.PlayerDetails(name=data.name))
    entry = repo.get_watch(session, player)
    active = entry.active if entry is not None else True
    repo.set_watch(session, player, target_buy=data.buy, target_sell=data.sell, note=data.note)
    if not active:
        repo.set_watch_active(session, player, False)
    if data.futbin_ref is not None:
        repo.set_source_ref(session, player, futbin.SOURCE_NAME, data.futbin_ref)
    elif update_refs:
        repo.remove_source_ref(session, player, futbin.SOURCE_NAME)


@pages.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request) -> HTMLResponse:
    with _db(request) as session:
        return _render(
            request,
            "watchlist.html",
            {"rows": _watch_rows(session), "form": {}, "errors": [], "nav": "watchlist"},
        )


@pages.post("/watchlist", response_model=None)
def watchlist_add(
    request: Request,
    ea_id: FormStr,
    name: OptionalFormStr = None,
    buy: OptionalFormStr = None,
    sell: OptionalFormStr = None,
    futbin_url: OptionalFormStr = None,
    note: OptionalFormStr = None,
) -> Response:
    form = {
        "ea_id": ea_id,
        "name": name,
        "buy": buy,
        "sell": sell,
        "futbin_url": futbin_url,
        "note": note,
    }
    data = WatchInput(name, buy, sell, futbin_url, note)
    errors = list(data.errors)
    try:
        parsed_id = forms.parse_ea_id(ea_id)
    except ValueError as exc:
        errors.insert(0, str(exc))
    with _db(request) as session:
        if not errors:
            player = repo.upsert_player(session, parsed_id)
            _apply_watch(session, player, data, update_refs=False)
            repo.set_watch_active(session, player, True)
            session.flush()
            message = f"{player.display_name} gespeichert."
            if data.warning:
                message += f" Hinweis: {data.warning}"
            form = {}
        else:
            message = None
        if not _is_htmx(request):
            if errors:
                return _render(
                    request,
                    "watchlist.html",
                    {
                        "rows": _watch_rows(session),
                        "form": form,
                        "errors": errors,
                        "nav": "watchlist",
                    },
                    status_code=422,
                )
            return RedirectResponse("/watchlist", status_code=303)
        return _render(
            request,
            "partials/watch_form.html",
            {
                "form": form,
                "errors": errors,
                "message": message,
                "rows": _watch_rows(session),
                "oob_rows": not errors,
            },
        )


@pages.get("/watchlist/{ea_id}/row", response_class=HTMLResponse)
def watch_row(request: Request, ea_id: int) -> HTMLResponse:
    with _db(request) as session:
        return _render(request, "partials/watch_row.html", _row_context(session, ea_id))


@pages.get("/watchlist/{ea_id}/edit", response_class=HTMLResponse)
def watch_edit(request: Request, ea_id: int) -> HTMLResponse:
    with _db(request) as session:
        context = _row_context(session, ea_id) | {"errors": []}
        return _render(request, "partials/watch_row_edit.html", context)


@pages.post("/watchlist/{ea_id}", response_class=HTMLResponse)
def watch_update(
    request: Request,
    ea_id: int,
    name: OptionalFormStr = None,
    buy: OptionalFormStr = None,
    sell: OptionalFormStr = None,
    futbin_url: OptionalFormStr = None,
    note: OptionalFormStr = None,
) -> HTMLResponse:
    data = WatchInput(name, buy, sell, futbin_url, note)
    with _db(request) as session:
        context = _row_context(session, ea_id)
        if data.errors:
            return _render(
                request, "partials/watch_row_edit.html", context | {"errors": data.errors}
            )
        _apply_watch(session, context["entry"].player, data, update_refs=True)
        session.flush()
        return _render(request, "partials/watch_row.html", _row_context(session, ea_id))


@pages.post("/watchlist/{ea_id}/toggle", response_class=HTMLResponse)
def watch_toggle(request: Request, ea_id: int) -> HTMLResponse:
    with _db(request) as session:
        context = _row_context(session, ea_id)
        entry = context["entry"]
        repo.set_watch_active(session, entry.player, not entry.active)
        return _render(request, "partials/watch_row.html", context)


# --- player detail ---------------------------------------------------------


@pages.get("/players/{ea_id}", response_class=HTMLResponse)
def player_page(request: Request, ea_id: int) -> HTMLResponse:
    settings = _settings(request)
    now = utcnow()
    with _db(request) as session:
        detail = views.player_detail(session, ea_id, settings.platform, now)
        if detail is None:
            raise HTTPException(status_code=404, detail="Spieler nicht gefunden")
        result = analysis.analyze_player(session, detail.player, settings, now)
        return _render(
            request,
            "player.html",
            {
                "d": detail,
                "a": result,
                "profiles": {
                    "hours": result.hours,
                    "weekdays": result.weekdays,
                },
                "nav": "prices",
                "error": request.query_params.get("error"),
            },
        )


@pages.post("/players/{ea_id}/prices", response_model=None)
def player_add_price(request: Request, ea_id: int, price: FormStr) -> Response:
    platform: Platform = _settings(request).platform
    try:
        value = forms.parse_coins(price)
    except ValueError:
        value = None
    if value is None:
        return RedirectResponse(f"/players/{ea_id}?error=price", status_code=303)
    with _db(request) as session:
        player = _player_or_404(session, ea_id)
        repo.add_snapshot(session, player, platform, value, MANUAL_SOURCE, utcnow())
    return RedirectResponse(f"/players/{ea_id}", status_code=303)


# --- status ----------------------------------------------------------------


def _status_context(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    collector = _collector(request)
    task: asyncio.Task[Any] | None = request.app.state.collect_task
    running = collector.running or (task is not None and not task.done())
    scheduler = request.app.state.scheduler
    job = scheduler.get_job(JOB_ID) if scheduler is not None else None
    now = utcnow()
    configured = settings.web_sources + ([MANUAL_SOURCE] if settings.manual_csv else [])
    with _db(request) as session:
        statuses = {status.source: status for status in repo.list_source_statuses(session)}
        sources = [
            {
                "name": name,
                "enabled": name in configured,
                "status": statuses.get(name),
                "paused": (
                    statuses.get(name) is not None
                    and statuses[name].paused_until is not None
                    and statuses[name].paused_until > now  # type: ignore[operator]
                ),
            }
            for name in dict.fromkeys([*configured, *statuses])
        ]
    return {
        "running": running,
        "result": collector.last_result,
        "next_run": job.next_run_time if job is not None else None,
        "sources": sources,
        "settings": settings,
    }


@pages.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    return _render(request, "status.html", _status_context(request) | {"nav": "status"})


@pages.get("/status/panel", response_class=HTMLResponse)
def status_panel(request: Request) -> HTMLResponse:
    return _render(request, "partials/status_panel.html", _status_context(request))


@pages.post("/status/collect", response_model=None)
async def status_collect(request: Request) -> Response:
    collector = _collector(request)
    task: asyncio.Task[Any] | None = request.app.state.collect_task
    if not collector.running and (task is None or task.done()):
        request.app.state.collect_task = asyncio.create_task(collector.run_once())
    if not _is_htmx(request):
        return RedirectResponse("/status", status_code=303)
    return _render(request, "partials/status_panel.html", _status_context(request))


@pages.post("/status/sources/{name}/resume", response_model=None)
def status_resume(request: Request, name: str) -> Response:
    with _db(request) as session:
        repo.resume_source(session, name.lower())
    if not _is_htmx(request):
        return RedirectResponse("/status", status_code=303)
    return _render(request, "partials/status_panel.html", _status_context(request))
