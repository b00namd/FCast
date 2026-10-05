"""Dashboard routes. Pages render full templates; HTMX requests get partials."""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from fcast.alerts.config import AlertConfig, load_alert_config, save_alert_config
from fcast.alerts.notifier import Notification, NotifierError
from fcast.analysis import cards
from fcast.analysis import service as analysis
from fcast.analysis import signals as sig
from fcast.analysis.uev import by_rating, uev_candidates
from fcast.backtest import engine
from fcast.backtest import service as bt
from fcast.backtest.curves import OFFSETS_H
from fcast.collector.job import apply_player_info
from fcast.collector.service import JOB_ID, Collector
from fcast.config import Platform, Settings, is_clock_time
from fcast.db import repositories as repo
from fcast.db.backup import list_backups
from fcast.db.base import utcnow
from fcast.db.models import (
    LeakItem,
    LinkType,
    Player,
    Promo,
    TotwActual,
    TotwPrediction,
)
from fcast.db.session import session_scope
from fcast.portfolio import coins as wallet
from fcast.portfolio import entries as entries_service
from fcast.portfolio import lookup as card_lookup
from fcast.portfolio import summary as portfolio_summary
from fcast.promos import service as promo_service
from fcast.radar import service as radar_service
from fcast.sources import futbin
from fcast.sources.base import PlayerInfo, PlayerNotFoundError, SourceError
from fcast.sources.futbin_locator import name_hints_from_futgg, slugify
from fcast.sources.registry import make_http_client
from fcast.totw.openligadb import LEAGUE_NAMES
from fcast.totw.service import hit_rate
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
        coins = wallet.balance(session)
        return _render(
            request,
            "signals.html",
            {
                "signals": found,
                "coins": coins,
                "too_expensive": [
                    not wallet.affordable(wallet.buy_price(signal), coins) for signal in found
                ],
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
    return {
        "entry": entry,
        "refs": repo.list_source_refs(session, player),
        "interval_choices": INTERVAL_CHOICES,
    }


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


INTERVAL_CHOICES = ((None, "jede Runde"), (120, "alle 2 h"), (360, "alle 6 h"), (720, "alle 12 h"))


def _interval(value: str | None, errors: list[str]) -> int | None:
    """Form value of the collect interval: "0" = every run, otherwise one of the choices."""
    if not value or value == "0":
        return None
    allowed = {str(minutes): minutes for minutes, _ in INTERVAL_CHOICES if minutes}
    if value not in allowed:
        errors.append(f"Ungültiger Takt „{value}“")
        return None
    return allowed[value]


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


class FutbinLookupError(Exception):
    pass


async def _resolve_futbin(request: Request, ref: str) -> PlayerInfo:
    """Card details (incl. EA id) for a FUTBIN link; one polite request."""
    settings = _settings(request)
    collector = _collector(request)
    with _db(request) as session:
        if futbin.SOURCE_NAME in repo.paused_sources(session, utcnow()):
            raise FutbinLookupError(
                "FUTBIN ist gerade pausiert (Sperre). Bitte EA-ID oder FUT.GG-Link angeben."
            )
    source = next((s for s in collector.sources if isinstance(s, futbin.FutbinSource)), None)
    temporary = source is None
    if source is None:
        source = futbin.FutbinSource(make_http_client(settings), lambda _: None, settings.platform)
    try:
        return await source.resolve_card(ref)
    except PlayerNotFoundError as exc:
        raise FutbinLookupError("FUTBIN-Seite nicht gefunden - Link prüfen.") from exc
    except SourceError as exc:
        raise FutbinLookupError(f"FUTBIN-Seite konnte nicht gelesen werden: {exc}") from exc
    finally:
        if temporary:
            await source.aclose()


async def _card_for(
    request: Request, ea_id_text: str | None, futbin_ref: str | None
) -> tuple[int | None, PlayerInfo | None, list[str]]:
    """EA id from the id/link field and/or the FUTBIN link, checked for consistency."""
    errors: list[str] = []
    ea_id: int | None = None
    if ea_id_text and ea_id_text.strip():
        try:
            ea_id = forms.parse_ea_id(ea_id_text)
        except ValueError as exc:
            return None, None, [str(exc)]
    info: PlayerInfo | None = None
    if futbin_ref is not None:
        try:
            info = await _resolve_futbin(request, futbin_ref)
        except FutbinLookupError as exc:
            if ea_id is None:
                return None, None, [str(exc)]
            # The EA id is known; the FUTBIN check is only a safety net.
            logger.warning("FUTBIN lookup failed for %s: %s", futbin_ref, exc)
        if info is not None and ea_id is not None and info.ea_id != ea_id:
            errors.append(
                f"Der FUTBIN-Link gehört zu einer anderen Karte (EA-ID {info.ea_id}) "
                f"als die angegebene EA-ID {ea_id}."
            )
        elif info is not None:
            ea_id = info.ea_id
    if ea_id is None and not errors:
        errors.append("Bitte EA-ID, FUT.GG-Link oder FUTBIN-Link angeben.")
    return ea_id, info, errors


def _start_futbin_discovery(request: Request, ea_id: int, id_text: str | None) -> bool:
    """Search the FUTBIN page of a new card in the background (keeps the form fast)."""
    collector = _collector(request)
    if collector._futbin() is None:
        return False
    hints = name_hints_from_futgg(id_text or "")
    tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
    task = asyncio.create_task(collector.discover_futbin_links(only=[ea_id], hints=hints))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


@pages.post("/watchlist", response_model=None)
async def watchlist_add(
    request: Request,
    ea_id: OptionalFormStr = None,
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
    parsed_id: int | None = None
    info: PlayerInfo | None = None
    if not errors:
        parsed_id, info, errors = await _card_for(request, ea_id, data.futbin_ref)
    with _db(request) as session:
        message = None
        if not errors and parsed_id is not None:
            player = repo.upsert_player(session, parsed_id)
            if info is not None:
                apply_player_info(session, info)
            _apply_watch(session, player, data, update_refs=False)
            repo.set_watch_active(session, player, True)
            session.flush()
            message = f"{player.display_name} gespeichert."
            if data.futbin_ref is None and _start_futbin_discovery(request, parsed_id, ea_id):
                message += " Der FUTBIN-Link wird im Hintergrund gesucht (dauert ca. 10-30 s)."
            if data.warning:
                message += f" Hinweis: {data.warning}"
            form = {}
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
async def watch_update(
    request: Request,
    ea_id: int,
    name: OptionalFormStr = None,
    buy: OptionalFormStr = None,
    sell: OptionalFormStr = None,
    futbin_url: OptionalFormStr = None,
    note: OptionalFormStr = None,
    interval: OptionalFormStr = None,
) -> HTMLResponse:
    data = WatchInput(name, buy, sell, futbin_url, note)
    errors = list(data.errors)
    interval_min = _interval(interval, errors)
    info: PlayerInfo | None = None
    with _db(request) as session:
        current = repo.get_source_ref(session, ea_id, futbin.SOURCE_NAME)
    if not errors and data.futbin_ref is not None and data.futbin_ref != current:
        _, info, errors = await _card_for(request, str(ea_id), data.futbin_ref)
    with _db(request) as session:
        context = _row_context(session, ea_id)
        if errors:
            return _render(request, "partials/watch_row_edit.html", context | {"errors": errors})
        if info is not None:
            apply_player_info(session, info)
        _apply_watch(session, context["entry"].player, data, update_refs=True)
        if interval is not None:
            repo.set_watch_interval(session, context["entry"].player, interval_min)
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
    backups = list_backups(settings.backup_path)
    return {
        "running": running,
        "result": collector.last_result,
        "next_run": job.next_run_time if job is not None else None,
        "sources": sources,
        "settings": settings,
        "last_backup": backups[0] if backups else None,
        "backup_count": len(backups),
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


# --- alerts ----------------------------------------------------------------


def _alerts_context(request: Request, **extra: Any) -> dict[str, Any]:
    settings = _settings(request)
    collector = _collector(request)
    with _db(request) as session:
        config = load_alert_config(session, settings)
        history = list(repo.list_alerts(session, limit=50))
    return {
        "config": config,
        "history": history,
        "channel": collector.alerts.notifier.name,
        "quiet_now": config.is_quiet(utcnow(), settings.tz),
        "nav": "alerts",
        "message": None,
        "errors": [],
    } | extra


@pages.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request) -> HTMLResponse:
    return _render(request, "alerts.html", _alerts_context(request))


@pages.post("/alerts/settings", response_class=HTMLResponse)
def alerts_save(
    request: Request,
    cooldown_h: FormStr,
    quiet_start: FormStr,
    quiet_end: FormStr,
    min_profit: OptionalFormStr = None,
    enabled: OptionalFormStr = None,
    buy_dip: OptionalFormStr = None,
    sell_target: OptionalFormStr = None,
    overprice: OptionalFormStr = None,
    system: OptionalFormStr = None,
    totw: OptionalFormStr = None,
    promo: OptionalFormStr = None,
    holo: OptionalFormStr = None,
    radar: OptionalFormStr = None,
) -> HTMLResponse:
    errors: list[str] = []
    try:
        cooldown = float(cooldown_h.replace(",", "."))
        if cooldown < 0:
            raise ValueError
    except ValueError:
        errors.append("Cooldown: Stunden als Zahl ≥ 0 angeben")
        cooldown = 0.0
    for label, value in (("Ruhezeit Beginn", quiet_start), ("Ruhezeit Ende", quiet_end)):
        if not is_clock_time(value):
            errors.append(f"{label}: Uhrzeit im Format HH:MM angeben")
    try:
        profit_value = forms.parse_coins(min_profit) or 0
    except ValueError:
        errors.append("Mindestprofit: ungültiger Betrag")
        profit_value = 0
    if errors:
        return _render(request, "alerts.html", _alerts_context(request, errors=errors), 422)
    config = AlertConfig(
        enabled=enabled is not None,
        cooldown_h=cooldown,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        min_profit=profit_value,
        buy_dip=buy_dip is not None,
        sell_target=sell_target is not None,
        overprice=overprice is not None,
        system=system is not None,
        totw=totw is not None,
        promo=promo is not None,
        holo=holo is not None,
        radar=radar is not None,
    )
    with _db(request) as session:
        save_alert_config(session, config)
    return _render(request, "alerts.html", _alerts_context(request, message="Gespeichert."))


@pages.post("/alerts/test", response_class=HTMLResponse)
async def alerts_test(request: Request) -> HTMLResponse:
    notifier = _collector(request).alerts.notifier
    try:
        await notifier.send(
            Notification(
                title="FCast Test",
                message="Test aus dem Dashboard - Alerts kommen an.",
                tags=("white_check_mark",),
            )
        )
        message, errors = f"Test über {notifier.name} gesendet.", []
    except NotifierError as exc:
        message, errors = None, [f"Senden fehlgeschlagen: {exc}"]
    return _render(request, "alerts.html", _alerts_context(request, message=message, errors=errors))


# --- TOTW prediction -----------------------------------------------------------


def _totw_context(request: Request, **extra: Any) -> dict[str, Any]:
    collector = _collector(request)
    service = collector.totw
    now = utcnow()
    week = service.upcoming(now)
    released = service.last_released(now)
    with _db(request) as session:
        predictions = list(
            session.scalars(
                select(TotwPrediction)
                .where(TotwPrediction.week == week.number)
                .order_by(TotwPrediction.rank)
            )
        )
        previous = None
        if released is not None:
            rate = hit_rate(session, released.number)
            actual = list(
                session.scalars(select(TotwActual).where(TotwActual.week == released.number))
            )
            previous = {"week": released, "rate": rate, "actual": actual}
    tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
    return {
        "week": week,
        "hours_left": max(0, int((week.release - now).total_seconds() // 3600)),
        "predictions": predictions,
        "previous": previous,
        "report": collector.last_totw,
        "running": any(not t.done() and t.get_name() == "totw" for t in tasks),
        "league_names": LEAGUE_NAMES,
        "nav": "totw",
    } | extra


@pages.get("/totw", response_class=HTMLResponse)
def totw_page(request: Request) -> HTMLResponse:
    return _render(request, "totw.html", _totw_context(request))


@pages.post("/totw/refresh", response_model=None)
async def totw_refresh(request: Request) -> Response:
    tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
    if not any(not t.done() and t.get_name() == "totw" for t in tasks):
        task = asyncio.create_task(_collector(request).refresh_totw(), name="totw")
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return RedirectResponse("/totw?started=1", status_code=303)


# --- potential radar ---------------------------------------------------------------------

RADAR_LISTS = {"popular": "Popular", "latest": "Neu", "totw": "TOTW"}


@pages.get("/radar", response_class=HTMLResponse)
def radar_page(request: Request) -> HTMLResponse:
    settings = _settings(request)
    now = utcnow()
    with _db(request) as session:
        hits = radar_service.hits(session, settings, now)
        coins = wallet.balance(session)
        return _render(
            request,
            "radar.html",
            {
                "nav": "radar",
                "hits": hits,
                "coins": coins,
                "too_expensive": [not wallet.affordable(hit.price, coins) for hit in hits],
                "agreement": cards.agreement(cards.card_values(session, settings, now)),
                "fodder": radar_service.fodder(session, settings, now),
                "status": radar_service.status(session, settings, now),
                "lists": RADAR_LISTS,
                "settings": settings,
            },
        )


# --- backtest ---------------------------------------------------------------------------

BACKTEST_RULES = {
    sig.Rule.BUY_DIP: "Kauf-Dip (BUY_DIP)",
    sig.Rule.OVERPRICE_CHANCE: "ÜV-Chance (OVERPRICE_CHANCE)",
    sig.Rule.TREND_START: "Trend-Start (Radar)",
    sig.Rule.PROMO_PREBUY: "Promo-Vorkauf (PROMO_PREBUY)",
}
SWEEP_HINTS = {
    sig.Rule.BUY_DIP: "dip_pct=5,10,15,20",
    sig.Rule.OVERPRICE_CHANCE: "uev_threshold=50,60,70",
    sig.Rule.TREND_START: "trend_target_pct=5,10,15",
    sig.Rule.PROMO_PREBUY: "entry_days=2,3,5,7",
}


def _form_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@pages.get("/backtest", response_class=HTMLResponse)
def backtest_page(
    request: Request,
    rule: str = sig.Rule.BUY_DIP.value,
    first: Annotated[str | None, Query(alias="from")] = None,
    last: Annotated[str | None, Query(alias="to")] = None,
    sweep: str | None = None,
    hold: str | None = None,
) -> HTMLResponse:
    settings = _settings(request)
    now = utcnow()
    errors: list[str] = []
    selected = sig.Rule.BUY_DIP
    try:
        selected = sig.Rule(rule.upper())
    except ValueError:
        errors.append(f"Unbekannte Regel {rule}")
    if selected not in engine.SUPPORTED:
        errors.append(f"{selected} lässt sich (noch) nicht backtesten")
        selected = sig.Rule.BUY_DIP
    params = engine.Params.from_settings(settings)
    if hold:
        try:
            params = replace(params, max_hold_h=max(1.0, float(hold.replace(",", "."))))
        except ValueError:
            errors.append("Haltedauer muss eine Zahl sein")
    report = None
    curves: list[Any] = []
    try:
        start, end = bt.window(_form_day(first), _form_day(last), settings.tz, now)
        sweep_name, values = bt.parse_sweep(selected, sweep) if sweep else (None, [])
    except ValueError as exc:
        errors.append(str(exc))
        start, end = bt.window(None, None, settings.tz, now)
        sweep_name, values = None, []
    with _db(request) as session:
        report = bt.run_backtest(
            session, settings, selected, start, end, params, sweep_name, values
        )
        curves = bt.promo_curves(session, settings, start, end) + bt.totw_curves(
            session, settings, start, end
        )
    chart = [
        {
            "label": f"{c.label} · {c.group}",
            "points": [[offset, value] for offset, (value, _) in c.average().items()],
        }
        for c in curves
        if c.cards
    ]
    return _render(
        request,
        "backtest.html",
        {
            "nav": "backtest",
            "rules": BACKTEST_RULES,
            "selected": selected,
            "form": {
                "from": start.astimezone(settings.tz).date().isoformat(),
                "to": end.astimezone(settings.tz).date().isoformat(),
                "sweep": sweep or "",
                "hold": hold or "",
            },
            "sweep_hint": SWEEP_HINTS[selected],
            "sweepable": engine.SWEEPABLE[selected],
            "report": report,
            "curves": curves,
            "offsets": OFFSETS_H,
            "chart": chart,
            "errors": errors,
        },
    )


# --- leak & promo radar ------------------------------------------------------------

CONFIDENCE_CHOICES = (
    (0.9, "sehr sicher"),
    (0.7, "wahrscheinlich"),
    (0.5, "Gerücht"),
    (0.3, "vage"),
)


def _start_task(request: Request, name: str, coro: Any) -> bool:
    tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
    if any(not t.done() and t.get_name() == name for t in tasks):
        coro.close()
        return False
    task = asyncio.create_task(coro, name=name)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


def _promo_rows(session: Session, now: datetime) -> list[Promo]:
    return [
        promo
        for promo in repo.list_promos(session)
        if promo.ends_at is None or promo.ends_at >= now - timedelta(days=3)
    ]


@pages.get("/promos", response_class=HTMLResponse)
def promos_page(request: Request) -> HTMLResponse:
    settings = _settings(request)
    now = utcnow()
    with _db(request) as session:
        return _render(
            request,
            "promos.html",
            {
                "inbox": promo_service.inbox(session),
                "promos": _promo_rows(session, now),
                "candidates": promo_service.candidates(session, settings, now)[:30],
                "now": now,
                "nav": "promos",
                "message": request.query_params.get("msg"),
            },
        )


@pages.post("/promos/leaks/sync", response_model=None)
async def promos_sync(request: Request) -> Response:
    _start_task(request, "leaks", _collector(request).refresh_leaks())
    return RedirectResponse("/promos?msg=Feeds+werden+abgerufen", status_code=303)


@pages.post("/promos/leaks/{leak_id}/dismiss", response_model=None)
def promos_dismiss(request: Request, leak_id: int) -> Response:
    with _db(request) as session:
        leak = session.get(LeakItem, leak_id)
        if leak is not None:
            leak.status = "dismissed"
    return RedirectResponse("/promos", status_code=303)


@pages.post("/promos/{promo_id}/delete", response_model=None)
def promos_delete(request: Request, promo_id: int) -> Response:
    with _db(request) as session:
        promo = session.get(Promo, promo_id)
        if promo is not None:
            repo.delete_promo(session, promo)
    return RedirectResponse("/promos?msg=Promo+gel%C3%B6scht", status_code=303)


def _new_promo_context(**extra: Any) -> dict[str, Any]:
    return {
        "form": {},
        "detection": None,
        "errors": [],
        "confidences": CONFIDENCE_CHOICES,
        "nav": "promos",
    } | extra


@pages.get("/promos/new", response_class=HTMLResponse)
def promos_new(request: Request, leak: int | None = None) -> HTMLResponse:
    form: dict[str, Any] = {"confidence": "0.7"}
    if leak is not None:
        with _db(request) as session:
            item = session.get(LeakItem, leak)
            if item is not None:
                form |= {
                    "text": f"{item.title}\n\n{item.summary}",
                    "name": item.title[:100],
                    "source": item.url,
                    "leak_id": str(item.id),
                    "confidence": "0.7" if item.is_leak else "0.5",
                }
    return _render(request, "promo_new.html", _new_promo_context(form=form))


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").replace("\n", ",").split(",") if part.strip()]


@pages.post("/promos/new", response_model=None)
async def promos_create(
    request: Request,
    action: FormStr,
    text: OptionalFormStr = None,
    name: OptionalFormStr = None,
    start: OptionalFormStr = None,
    end: OptionalFormStr = None,
    confidence: OptionalFormStr = None,
    source: OptionalFormStr = None,
    leak_id: OptionalFormStr = None,
    extra_players: OptionalFormStr = None,
    extra_leagues: OptionalFormStr = None,
    extra_nations: OptionalFormStr = None,
    extra_clubs: OptionalFormStr = None,
) -> Response:
    form_data = await request.form()
    form: dict[str, Any] = {
        "text": text, "name": name, "start": start, "end": end, "confidence": confidence,
        "source": source, "leak_id": leak_id, "extra_players": extra_players,
        "extra_leagues": extra_leagues, "extra_nations": extra_nations, "extra_clubs": extra_clubs,
    }  # fmt: skip
    collector = _collector(request)
    if action == "analyze":
        with _db(request) as session:
            detection = await promo_service.analyze_text(text or "", collector._futbin(), session)
        return _render(
            request, "promo_new.html", _new_promo_context(form=form, detection=detection)
        )

    errors: list[str] = []
    tz = _settings(request).tz
    starts_at = _parse_day(start, tz)
    ends_at = _parse_day(end, tz) if end else None
    if not (name or "").strip():
        errors.append("Name der Promo fehlt.")
    if starts_at is None:
        errors.append("Startdatum fehlt oder ist ungültig.")
    if end and ends_at is None:
        errors.append("Enddatum ist ungültig.")
    if starts_at and ends_at and ends_at < starts_at:
        errors.append("Das Ende liegt vor dem Start.")
    links: list[tuple[LinkType, str]] = []
    for link_type, field in (
        (LinkType.PLAYER, "player"), (LinkType.LEAGUE, "league"),
        (LinkType.NATION, "nation"), (LinkType.CLUB, "club"),
    ):  # fmt: skip
        values = [str(v) for v in form_data.getlist(field)]
        values += _split(form.get(f"extra_{field}s"))
        for value in dict.fromkeys(values):
            links.append((link_type, slugify(value) if link_type is LinkType.PLAYER else value))
    if not links:
        errors.append("Mindestens einen Spieler, eine Liga, Nation oder einen Verein wählen.")
    if errors or starts_at is None:
        return _render(request, "promo_new.html", _new_promo_context(form=form, errors=errors), 422)
    try:
        conf = min(1.0, max(0.0, float(confidence or 0.5)))
    except ValueError:
        conf = 0.5
    with _db(request) as session:
        promo = promo_service.create_promo(
            session,
            (name or "").strip()[:100],
            starts_at,
            ends_at,
            conf,
            (source or "").strip()[:200] or None,
            None,
            links,
            leak_id=int(leak_id) if leak_id and leak_id.isdigit() else None,
        )
        promo_id = promo.id
    if any(t is LinkType.PLAYER for t, _ in links):
        _start_task(request, f"pool-{promo_id}", collector.fill_promo_pool(promo_id))
    return RedirectResponse(
        "/promos?msg=Promo+gespeichert.+Karten+der+Spieler+werden+im+Hintergrund+gesucht.",
        status_code=303,
    )


def _parse_day(value: str | None, tz: Any) -> datetime | None:
    """A date from the form ("2026-10-09") at the usual release time 19:00 local."""
    try:
        day = date.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return datetime.combine(day, time(19, 0), tzinfo=tz).astimezone(UTC)


# --- ÜV shopping list --------------------------------------------------------------------


@pages.get("/uev", response_class=HTMLResponse)
def uev_page(
    request: Request,
    max_price: Annotated[int, Query(ge=1)] = 60_000,
    premium: Annotated[int, Query(ge=0)] = 500,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
    rating: Annotated[int | None, Query(ge=1)] = None,
) -> HTMLResponse:
    settings = _settings(request)
    now = utcnow()
    with _db(request) as session:
        values = cards.card_values(session, settings, now)
        candidates = uev_candidates(values, max_price=max_price, premium=premium, rating=rating)
        players = {
            player.id: player
            for player in session.scalars(
                select(Player).where(Player.id.in_([c.player_id for c in candidates]))
            )
        }
        return _render(
            request,
            "uev.html",
            {
                "nav": "uev",
                "groups": by_rating(candidates, limit),
                "players": players,
                "styles": {player_id: player.chem_style for player_id, player in players.items()},
                "max_price": max_price,
                "premium": premium,
                "limit": limit,
                "rating": rating,
                "ratings": sorted(
                    {c.rating for c in candidates if c.rating is not None}, reverse=True
                ),
            },
        )


# --- portfolio ---------------------------------------------------------------------------


def _portfolio_context(
    session: Session,
    settings: Settings,
    show_all: bool,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    summary = portfolio_summary.summarize(
        session, settings, None if show_all else portfolio_summary.OPEN_POSITIONS
    )
    return {
        "nav": "portfolio",
        "rows": list(reversed(summary.rows)),  # newest purchase first
        "realised": summary.realised,
        "tied_up": summary.tied_up,
        "unrealised": summary.unrealised,
        "show_all": show_all,
        "errors": errors or [],
        "coins": wallet.balance(session),
    }


@pages.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(
    request: Request,
    show_all: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    settings = _settings(request)
    with _db(request) as session:
        return _render(request, "portfolio.html", _portfolio_context(session, settings, show_all))


def _back_to_portfolio() -> RedirectResponse:
    return RedirectResponse("/portfolio", status_code=303)


def _amount(label: str, text: str | None, errors: list[str]) -> int | None:
    try:
        value = forms.parse_coins(text)
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return None
    if value is None:
        errors.append(f"{label} fehlt")
    return value


@pages.post("/portfolio/buy")
def portfolio_buy(
    request: Request,
    ea_id: OptionalFormStr = None,
    price: OptionalFormStr = None,
) -> Response:
    errors: list[str] = []
    card_id: int | None = None
    query: card_lookup.CardQuery | None = None
    try:
        card_id = forms.parse_ea_id(ea_id)
    except ValueError as exc:
        if ea_id and any(char.isalpha() for char in ea_id) and "://" not in ea_id:
            query = card_lookup.parse_query(ea_id)  # "Musiala 87", "Olise 91 TOTW"
        else:
            errors.append(str(exc))
    amount = _amount("Kaufpreis", price, errors)
    with _db(request) as session:
        player: Player | None = None
        if query is not None:
            found = card_lookup.find_local(session, query)
            try:
                player = card_lookup.pick(query, found)
            except card_lookup.CardLookupError:
                if found:
                    names = ", ".join(entries_service.describe(card) for card in found)
                    errors.append(
                        f"„{query.text}“ ist nicht eindeutig: {names}. Kartentyp ergänzen "
                        "(z. B. „Olise 91 TOTW“) oder die EA-ID verwenden."
                    )
                else:
                    errors.append(
                        f"Keine bekannte Karte passt zu „{query.text}“. EA-ID oder Link "
                        "verwenden oder die Karte erst auf die Watchlist setzen."
                    )
        elif card_id is not None:
            player = repo.get_player_by_ea_id(session, card_id)
            if player is None:
                errors.append(f"Karte {card_id} ist unbekannt - erst auf die Watchlist setzen.")
        if player is not None and amount is not None:
            repo.open_position(session, player, amount)
            return _back_to_portfolio()
        return _render(
            request,
            "portfolio.html",
            _portfolio_context(session, _settings(request), False, errors),
            status_code=400,
        )


@pages.post("/portfolio/coins")
def portfolio_coins(request: Request, coins: OptionalFormStr = None) -> Response:
    errors: list[str] = []
    amount = _amount("Coinstand", coins, errors)
    with _db(request) as session:
        if amount is not None:
            wallet.set_balance(session, amount)
            return _back_to_portfolio()
        return _render(
            request,
            "portfolio.html",
            _portfolio_context(session, _settings(request), False, errors),
            status_code=400,
        )


@pages.post("/portfolio/{position_id}/listed")
def portfolio_listed(
    request: Request,
    position_id: int,
    price: OptionalFormStr = None,
) -> Response:
    errors: list[str] = []
    amount = _amount("Preis", price, errors)
    with _db(request) as session:
        if amount is not None:
            try:
                repo.mark_listed(session, repo.get_position(session, position_id), amount)
                return _back_to_portfolio()
            except (repo.NotFoundError, repo.InvalidStateError) as exc:
                errors.append(str(exc))
        return _render(
            request,
            "portfolio.html",
            _portfolio_context(session, _settings(request), False, errors),
            status_code=400,
        )


@pages.post("/portfolio/{position_id}/sell")
def portfolio_sell(
    request: Request,
    position_id: int,
    price: OptionalFormStr = None,
) -> Response:
    errors: list[str] = []
    amount = _amount("Verkaufspreis", price, errors)
    with _db(request) as session:
        if amount is not None:
            try:
                repo.sell_position(session, repo.get_position(session, position_id), amount)
                return _back_to_portfolio()
            except (repo.NotFoundError, repo.InvalidStateError) as exc:
                errors.append(str(exc))
        return _render(
            request,
            "portfolio.html",
            _portfolio_context(session, _settings(request), False, errors),
            status_code=400,
        )


@pages.post("/portfolio/{position_id}/delete")
def portfolio_delete(request: Request, position_id: int) -> Response:
    with _db(request) as session:
        try:
            repo.remove_position(session, repo.get_position(session, position_id))
        except repo.NotFoundError as exc:
            return _render(
                request,
                "portfolio.html",
                _portfolio_context(session, _settings(request), False, [str(exc)]),
                status_code=404,
            )
        return _back_to_portfolio()
