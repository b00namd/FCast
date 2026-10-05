"""Command line interface."""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from fcast.db.models import Player
    from fcast.portfolio.entries import Booking, Entry, Result

from fcast import __version__
from fcast.config import get_settings

app = typer.Typer(
    name="fcast", help="FCast - EA FC Ultimate Team market analysis.", no_args_is_help=True
)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
watch_app = typer.Typer(help="Manage the watchlist.", no_args_is_help=True)
prices_app = typer.Typer(help="Price data.", no_args_is_help=True)
sources_app = typer.Typer(help="Price source health.", no_args_is_help=True)
portfolio_app = typer.Typer(help="Purchases, sales and profit.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(watch_app, name="watch")
app.add_typer(prices_app, name="prices")
app.add_typer(sources_app, name="sources")
app.add_typer(portfolio_app, name="portfolio")

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fcast {__version__}")
        raise typer.Exit()


def format_coins(value: int | None) -> str:
    return "-" if value is None else f"{value:,}".replace(",", ".")


@contextmanager
def _db_session() -> Iterator[Session]:
    """Open a transactional session on the configured database, migrating it first."""
    from fcast.db import migrate
    from fcast.db.session import create_db_engine, create_session_factory, session_scope

    url = get_settings().db_url
    migrate.upgrade(url)
    engine = create_db_engine(url)
    try:
        with session_scope(create_session_factory(engine)) as session:
            yield session
    finally:
        engine.dispose()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Keep third-party noise down unless explicitly asked for.
    for noisy in ("httpx", "httpcore", "apscheduler", "alembic"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """FCast - EA FC Ultimate Team market analysis."""


@app.command()
def config() -> None:
    """Show the active configuration (secrets masked)."""
    settings = get_settings()
    for key, value in settings.model_dump().items():
        typer.echo(f"{key} = {value}")


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
) -> None:
    """Apply database migrations."""
    from fcast.db import migrate

    settings = get_settings()
    migrate.upgrade(settings.db_url, revision)
    typer.echo(f"Database at {settings.db_path} is up to date ({revision}).")


@db_app.command("backup")
def db_backup() -> None:
    """Back up the database now (also runs daily at FCAST_BACKUP_TIME)."""
    from fcast.db.backup import backup_database, list_backups
    from fcast.db.base import utcnow

    settings = get_settings()
    try:
        target = backup_database(
            settings.db_path, settings.backup_path, utcnow(), settings.backup_keep
        )
    except (OSError, FileNotFoundError) as exc:
        typer.secho(f"Error: backup failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    count = len(list_backups(settings.backup_path))
    typer.echo(f"Backup written to {target} ({count} backups kept).")


@watch_app.command("add")
def watch_add(
    ea_id: Annotated[
        int | None,
        typer.Argument(help="EA card id; optional if --futbin is given.", min=1),
    ] = None,
    buy: Annotated[int | None, typer.Option("--buy", help="Target buy price.", min=1)] = None,
    sell: Annotated[int | None, typer.Option("--sell", help="Target sell price.", min=1)] = None,
    note: Annotated[str | None, typer.Option("--note", help="Free-text note.")] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Player name, if not yet known.")
    ] = None,
    futbin: Annotated[
        str | None,
        typer.Option(
            "--futbin", help="FUTBIN page of this card, e.g. .../27/player/21487/maradona"
        ),
    ] = None,
) -> None:
    """Add a card to the watchlist (by EA id and/or FUTBIN link) or update it."""
    from fcast.collector.job import apply_player_info
    from fcast.db import repositories as repo
    from fcast.sources import futbin as futbin_source
    from fcast.sources.base import PlayerInfo, SourceError
    from fcast.sources.registry import make_http_client

    settings = get_settings()
    futbin_ref = None
    if futbin is not None:
        try:
            futbin_ref = futbin_source.normalize_ref(futbin)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--futbin") from exc
    if ea_id is None and futbin_ref is None:
        raise typer.BadParameter("give an EA id or --futbin", param_hint="EA_ID")

    info: PlayerInfo | None = None
    if futbin_ref is not None:
        source = futbin_source.FutbinSource(
            make_http_client(settings), lambda _: None, settings.platform
        )

        async def _resolve() -> PlayerInfo:
            try:
                return await source.resolve_card(futbin_ref)
            finally:
                await source.aclose()

        try:
            info = asyncio.run(_resolve())
        except SourceError as exc:
            if ea_id is None:
                typer.secho(f"Error: FUTBIN lookup failed: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1) from exc
            typer.secho(f"Warning: FUTBIN lookup failed: {exc}", fg=typer.colors.YELLOW, err=True)
        if info is not None and ea_id is not None and info.ea_id != ea_id:
            typer.secho(
                f"Error: the FUTBIN link belongs to card {info.ea_id}, not {ea_id}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
    card_id = info.ea_id if info is not None else ea_id
    if card_id is None:  # pragma: no cover - excluded by the checks above
        raise typer.Exit(1)

    if buy is not None and sell is not None and sell * 0.95 <= buy:
        typer.secho(
            "Warning: target sell after 5% EA tax does not exceed target buy.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    with _db_session() as session:
        player = repo.upsert_player(session, card_id, repo.PlayerDetails(name=name))
        if info is not None:
            apply_player_info(session, info)
        repo.set_watch(session, player, target_buy=buy, target_sell=sell, note=note)
        if futbin_ref is not None:
            repo.set_source_ref(session, player, futbin_source.SOURCE_NAME, futbin_ref)
        typer.echo(
            f"Watching {player.display_name} [{card_id}]: "
            f"buy {format_coins(buy)}, sell {format_coins(sell)}"
        )


def format_interval(minutes: int | None) -> str:
    if minutes is None:
        return "every run"
    return f"{minutes // 60} h" if minutes % 60 == 0 else f"{minutes} min"


def _watched_player(session: Session, ea_id: int) -> "Player":
    from fcast.db import repositories as repo

    player = repo.get_player_by_ea_id(session, ea_id)
    if player is None or repo.get_watch(session, player) is None:
        typer.secho(f"Error: {ea_id} is not on the watchlist.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return player


@watch_app.command("interval")
def watch_interval(
    ea_id: Annotated[int, typer.Argument(help="EA card id.", min=1)],
    minutes: Annotated[
        int, typer.Argument(help="Collect every N minutes; 0 = every collector run.", min=0)
    ],
) -> None:
    """Collect a card less often (e.g. 120 for every 2 h) to spare the price sources."""
    from fcast.db import repositories as repo

    with _db_session() as session:
        player = _watched_player(session, ea_id)
        repo.set_watch_interval(session, player, minutes or None)
        typer.echo(f"{player.display_name}: {format_interval(minutes or None)}")


@watch_app.command("remove")
def watch_remove(ea_id: Annotated[int, typer.Argument(help="EA card id.", min=1)]) -> None:
    """Remove a card from the watchlist (its price history is kept)."""
    from fcast.db import repositories as repo

    with _db_session() as session:
        player = _watched_player(session, ea_id)
        repo.remove_watch(session, player)
        typer.echo(f"Removed {player.display_name} from the watchlist.")


@watch_app.command("list")
def watch_list(
    show_all: Annotated[
        bool, typer.Option("--all", "-a", help="Include inactive entries.")
    ] = False,
) -> None:
    """Show the watchlist."""
    from fcast.db import repositories as repo

    with _db_session() as session:
        entries = repo.list_watchlist(session, active_only=not show_all)
        if not entries:
            typer.echo("Watchlist is empty.")
            return
        table = Table(title="Watchlist")
        table.add_column("EA-ID", justify="right")
        table.add_column("Player")
        table.add_column("Target buy", justify="right")
        table.add_column("Target sell", justify="right")
        if show_all:
            table.add_column("Active")
        table.add_column("Interval")
        table.add_column("Sources")
        table.add_column("Note")
        for entry in entries:
            row = [
                str(entry.player.ea_id),
                entry.player.display_name,
                format_coins(entry.target_buy),
                format_coins(entry.target_sell),
            ]
            if show_all:
                row.append("yes" if entry.active else "no")
            row.append(format_interval(entry.interval_min))
            row.append(", ".join(sorted(repo.list_source_refs(session, entry.player))) or "-")
            row.append(entry.note or "")
            table.add_row(*row)
        console.print(table)


@app.command()
def collect(
    once: Annotated[bool, typer.Option("--once", help="Run a single collection and exit.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Collect prices for all active watchlist players (every FCAST_COLLECT_INTERVAL_MIN)."""
    from fcast.collector.service import Collector, run_forever

    _setup_logging(verbose)
    settings = get_settings()
    if not once:
        asyncio.run(run_forever(settings))
        return

    async def _run() -> None:
        collector = Collector(settings)
        try:
            result = await collector.run_once()
        finally:
            await collector.aclose()
        typer.echo(
            f"{result.players} players: {result.stored} new snapshots, "
            f"{result.unchanged} unchanged, {len(result.extinct)} extinct, "
            f"{len(result.untradeable)} untradeable, "
            f"{len(result.missing)} without price"
        )
        for source, reason in result.paused.items():
            typer.secho(
                f"{source} refused requests and is paused for "
                f"{settings.source_pause_h} h: {reason}",
                fg=typer.colors.YELLOW,
                err=True,
            )
        if result.skipped:
            typer.echo(f"Skipped paused sources: {', '.join(result.skipped)}", err=True)
        for source, messages in result.errors.items():
            typer.secho(f"{source}: {len(messages)} error(s)", fg=typer.colors.RED, err=True)
            for message in messages[:5]:
                typer.echo(f"  {message}", err=True)

    asyncio.run(_run())


@prices_app.command("import")
def prices_import(
    csv_file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="CSV file.")
    ],
) -> None:
    """Import price history from a CSV file (same format as FCAST_MANUAL_CSV)."""
    from fcast.collector.job import import_rows
    from fcast.sources.base import SourceError
    from fcast.sources.manual import parse_csv

    settings = get_settings()
    try:
        rows = parse_csv(csv_file, settings.platform, settings.tz)
    except SourceError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    with _db_session() as session:
        stored, unchanged = import_rows(session, [(row.player, row.quote) for row in rows])
    typer.echo(f"Imported {stored} snapshots ({unchanged} already present).")


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(get_settings().tz).strftime("%d.%m. %H:%M")


@sources_app.command("status")
def sources_status() -> None:
    """Show configured sources, last success/error and active pauses."""
    from fcast.db import repositories as repo
    from fcast.db.base import utcnow

    settings = get_settings()
    configured = settings.web_sources + (["manual"] if settings.manual_csv else [])
    with _db_session() as session:
        statuses = {status.source: status for status in repo.list_source_statuses(session)}
        table = Table(title=f"Sources ({settings.source_strategy})")
        for column in ("Source", "Enabled", "Last success", "Last error", "Paused until"):
            table.add_column(column)
        now = utcnow()
        for name in dict.fromkeys([*configured, *statuses]):
            status = statuses.get(name)
            paused = (
                status is not None and status.paused_until is not None and status.paused_until > now
            )
            table.add_row(
                name,
                "yes" if name in configured else "no",
                _format_time(status.last_success_at if status else None),
                (status.last_error or "-")[:60] if status else "-",
                f"{_format_time(status.paused_until)} ({status.pause_reason})"[:80]
                if paused and status
                else "-",
            )
        console.print(table)


@sources_app.command("resume")
def sources_resume(name: Annotated[str, typer.Argument(help="Source name, e.g. futbin.")]) -> None:
    """Lift a pause before it expires (only if you are sure the block is over)."""
    from fcast.db import repositories as repo

    with _db_session() as session:
        repo.resume_source(session, name.lower())
    typer.echo(f"{name} resumed.")


@app.command()
def serve(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Run the web dashboard together with the scheduled collector."""
    import uvicorn

    from fcast.web.app import create_app

    _setup_logging(verbose)
    settings = get_settings()
    try:
        web_app = create_app(settings)
    except RuntimeError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    uvicorn.run(
        web_app,
        host=settings.web_host,
        port=settings.web_port,
        log_config=None,  # keep our logging setup
        access_log=verbose,
        proxy_headers=False,
    )


@app.command()
def analyze(ea_id: Annotated[int, typer.Argument(help="EA card id.", min=1)]) -> None:
    """Key figures, supply picture and signals for one card."""
    from fcast.analysis.service import analyze_player
    from fcast.db import repositories as repo
    from fcast.db.base import utcnow

    settings = get_settings()
    with _db_session() as session:
        player = repo.get_player_by_ea_id(session, ea_id)
        if player is None:
            typer.secho(f"Unknown card {ea_id}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        result = analyze_player(session, player, settings, utcnow())
        stats = result.stats

        def pct(value: float | None) -> str:
            return "-" if value is None else f"{value:+.1f} %"

        table = Table(title=f"{player.display_name} ({settings.platform.value})")
        table.add_column("Kennzahl")
        table.add_column("Wert", justify="right")
        rows = [
            ("Aktuell", format_coins(stats.current)),
            ("Ø 24 h", format_coins(round(stats.day.mean)) if stats.day else "-"),
            ("Ø 7 Tage", format_coins(round(stats.week.mean)) if stats.week else "-"),
            (
                "Min / Max 7 Tage",
                f"{format_coins(stats.week.low)} / {format_coins(stats.week.high)}"
                if stats.week
                else "-",
            ),
            ("Änderung 24 h", pct(stats.change_24h_pct)),
            ("Abweichung zu Ø 7 Tage", pct(stats.deviation_pct)),
            ("Volatilität", pct(stats.volatility_pct).lstrip("+")),
            (
                "Preisänderungen / Tag",
                "-" if stats.changes_per_day is None else f"{stats.changes_per_day:.1f}",
            ),
            (
                "ÜV-Score",
                "-" if result.overprice is None else f"{result.overprice.score:.0f} / 100",
            ),
        ]
        if result.market is not None:
            listings = ", ".join(format_coins(p) for p in result.market.listings) or "extinct"
            rows.append(("Angebote", listings))
            rows.append(
                (
                    "EA-Spanne",
                    f"{format_coins(result.market.range_min)} - "
                    f"{format_coins(result.market.range_max)}",
                )
            )
        for label, value in rows:
            table.add_row(label, value)
        console.print(table)
        for signal in result.signals:
            console.print(
                f"[bold]{signal.label}[/bold]: Empfehlung {format_coins(signal.recommended)}, "
                f"Profit {format_coins(signal.expected_profit)} - {'; '.join(signal.reasons)}"
            )


@app.command()
def signals(
    rule: Annotated[
        str | None,
        typer.Option("--rule", help="BUY_DIP, SELL_TARGET or OVERPRICE_CHANCE."),
    ] = None,
) -> None:
    """Current signals for all active watchlist cards."""
    from fcast.analysis.service import current_signals
    from fcast.analysis.signals import Rule
    from fcast.db.base import utcnow

    selected = None
    if rule is not None:
        try:
            selected = Rule(rule.upper())
        except ValueError as exc:
            raise typer.BadParameter(f"unknown rule {rule}", param_hint="--rule") from exc
    with _db_session() as session:
        found = current_signals(session, get_settings(), utcnow(), selected)
    if not found:
        typer.echo("Keine Signale.")
        return
    table = Table(title="Signale")
    for column in ("Signal", "Karte", "Preis", "Ø 7 Tage", "Empfehlung", "Profit", "Score"):
        table.add_column(column, justify="right" if column not in ("Signal", "Karte") else "left")
    for s in found:
        table.add_row(
            s.label,
            s.name,
            format_coins(s.price),
            format_coins(s.reference),
            format_coins(s.recommended),
            format_coins(s.expected_profit),
            "-" if s.score is None else f"{s.score:.0f}",
        )
    console.print(table)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.1f} %".replace(".", ",")


def _day(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=option) from exc


@app.command()
def backtest(
    rule: Annotated[
        str, typer.Option("--rule", help="BUY_DIP, OVERPRICE_CHANCE, TREND_START or PROMO_PREBUY.")
    ] = "BUY_DIP",
    first: Annotated[
        str | None, typer.Option("--from", help="First day (YYYY-MM-DD); default 30 days ago.")
    ] = None,
    last: Annotated[
        str | None, typer.Option("--to", help="Last day (YYYY-MM-DD); default today.")
    ] = None,
    sweep: Annotated[
        str | None,
        typer.Option("--sweep", help="Parameter sweep, e.g. dip_pct=5,10,15,20."),
    ] = None,
    hold: Annotated[
        float | None, typer.Option("--hold", help="Max holding time in hours (BUY_DIP, ÜV).", min=1)
    ] = None,
    card: Annotated[
        list[int] | None, typer.Option("--card", help="Only this EA id (repeatable).")
    ] = None,
    curves: Annotated[
        bool, typer.Option("--curves", help="Also show price reactions to promos and TOTW.")
    ] = False,
    trades: Annotated[int, typer.Option("--trades", help="How many trades to list.")] = 15,
) -> None:
    """Replay a rule on the stored prices: trades, hit rate, profit, drawdown, capital."""
    from dataclasses import replace

    from fcast.analysis.signals import Rule
    from fcast.backtest import engine
    from fcast.backtest import service as bt
    from fcast.db.base import utcnow

    settings = get_settings()
    try:
        selected = Rule(rule.upper())
    except ValueError as exc:
        raise typer.BadParameter(f"unknown rule {rule}", param_hint="--rule") from exc
    if selected not in engine.SUPPORTED:
        raise typer.BadParameter(
            f"{selected} cannot be backtested (supported: {', '.join(engine.SUPPORTED)})",
            param_hint="--rule",
        )
    try:
        start, end = bt.window(_day(first, "--from"), _day(last, "--to"), settings.tz, utcnow())
        sweep_name, values = bt.parse_sweep(selected, sweep) if sweep else (None, [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    params = engine.Params.from_settings(settings)
    if hold is not None:
        params = replace(params, max_hold_h=hold)

    with _db_session() as session:
        report = bt.run_backtest(
            session, settings, selected, start, end, params, sweep_name, values, card
        )
        event_curves = (
            bt.promo_curves(session, settings, start, end)
            + bt.totw_curves(session, settings, start, end)
            if curves
            else []
        )

    m = report.metrics
    tz = settings.tz
    console.print(
        f"[bold]Backtest {selected}[/bold] {start.astimezone(tz):%d.%m.%Y} - "
        f"{end.astimezone(tz):%d.%m.%Y %H:%M} · {report.cards} Karten, "
        f"{report.snapshots} Preise"
        + (f", {report.promos} Promos" if selected is Rule.PROMO_PREBUY else "")
        + f" · Fingerprint {report.fingerprint}"
    )
    summary = Table(show_header=False)
    summary.add_column()
    summary.add_column(justify="right")
    for label, value in (
        ("Trades (abgeschlossen)", str(m.trades)),
        ("offen", f"{m.open} ({format_coins(m.unrealized)} unrealisiert)"),
        ("Trefferquote", "-" if m.hit_rate is None else f"{m.hit_rate:.0f} %"),
        ("Profit gesamt", format_coins(m.total_profit)),
        ("Ø Profit pro Trade", format_coins(None if m.avg_profit is None else round(m.avg_profit))),
        ("Max-Drawdown", format_coins(m.max_drawdown)),
        ("Kapitalbindung (Spitze)", format_coins(m.peak_capital)),
        ("Ø Haltedauer", "-" if m.avg_hold_h is None else f"{m.avg_hold_h:.1f} h"),
    ):
        summary.add_row(label, value)
    console.print(summary)

    if report.trades:
        table = Table(title="Trades")
        for column in ("Karte", "Kauf", "Preis", "Verkauf", "Preis", "Profit", "Rendite", "Grund"):
            table.add_column(column)
        for t in report.trades[:trades]:
            table.add_row(
                t.name,
                f"{t.bought_at.astimezone(tz):%d.%m. %H:%M}",
                format_coins(t.buy),
                f"{t.sold_at.astimezone(tz):%d.%m. %H:%M}",
                format_coins(t.sell),
                format_coins(t.profit),
                _pct(t.return_pct),
                f"{t.exit}" + (f" · {t.note}" if t.note else ""),
            )
        console.print(table)

    if report.sweep:
        table = Table(title=f"Sweep {report.sweep_name}")
        for column in ("Wert", "Trades", "Treffer", "Profit", "Ø Profit", "Drawdown", "Kapital"):
            table.add_column(column, justify="right")
        for row in report.sweep:
            r = row.metrics
            table.add_row(
                f"{row.value:g}",
                str(r.trades),
                "-" if r.hit_rate is None else f"{r.hit_rate:.0f} %",
                format_coins(r.total_profit),
                format_coins(None if r.avg_profit is None else round(r.avg_profit)),
                format_coins(r.max_drawdown),
                format_coins(r.peak_capital),
            )
        console.print(table)

    if curves:
        if not event_curves:
            typer.echo("Keine Promo- oder TOTW-Verläufe im Zeitraum.")
        for ec in event_curves:
            avg = ec.average()
            line = "  ".join(
                f"T{offset:+d}h {_pct(value)} ({n})" for offset, (value, n) in avg.items()
            )
            console.print(
                f"[bold]{ec.label}[/bold] ({ec.group}, {ec.at.astimezone(tz):%d.%m. %H:%M}, "
                f"{len(ec.cards)} Karten): {line or 'keine Preise'}"
            )


@app.command()
def lage(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Market overview in one go: cards, mood, promos, TOTW, alerts, sources, backtest."""
    import json
    from dataclasses import asdict

    from fcast.analysis.overview import build_overview
    from fcast.db.base import utcnow

    settings = get_settings()
    with _db_session() as session:
        ov = build_overview(session, settings, utcnow())
    if as_json:
        typer.echo(json.dumps(asdict(ov), default=str, ensure_ascii=False, indent=1))
        return

    tz = settings.tz
    mood = ov.mood
    console.print(
        f"[bold]Marktlage {ov.generated_at.astimezone(tz):%d.%m.%Y %H:%M}[/bold] "
        f"({ov.platform}) · {mood.cards} Karten · Ø 24 h {_pct(mood.avg_change_24h_pct)} "
        f"· {mood.rising} steigend, {mood.falling} fallend"
    )
    for c in ov.cards:
        parts = [
            f"[bold]{c.name}[/bold] {c.card_type or ''}".strip(),
            format_coins(c.price)
            + (f" (vor {c.price_age_h:.1f} h)".replace(".", ",") if c.price_age_h else ""),
            f"24 h {_pct(c.change_24h_pct)}",
            f"Ø7T {_pct(c.deviation_pct)}",
            f"7T {format_coins(c.week_low)}-{format_coins(c.week_high)} "
            f"({c.points_7d} P., {c.days_of_data} Tg.)",
        ]
        if c.extinct:
            parts.append("[red]extinct[/red]")
        elif c.listings:
            parts.append(
                f"{len(c.listings)} Angebote, Lücke {_pct(c.gap_pct)}, Luft {_pct(c.headroom_pct)}"
            )
            if c.thin_hours:
                parts.append(f"dünn seit {c.thin_hours:.0f} h")
        if c.play_value is not None:
            parts.append(f"Spielwert {c.play_value:.0f} ({c.play_group})")
        if c.uev_score is not None:
            parts.append(f"ÜV {c.uev_score:.0f}")
        if c.holo_spread_pct is not None:
            parts.append(f"Holo {format_coins(c.holo_price)} ({_pct(c.holo_spread_pct)})")
        if c.cheapest_hour is not None:
            parts.append(f"günstig ~{c.cheapest_hour} Uhr")
        if c.cheapest_weekday is not None:
            parts.append(f"günstig {c.cheapest_weekday}")
        if c.signals:
            parts.append("[yellow]" + ", ".join(c.signals) + "[/yellow]")
        if c.note:
            parts.append(f"[dim]{c.note}[/dim]")
        console.print(" · ".join(parts))
    for title, lines in (
        (
            "Promos",
            [
                f"{p.name} ab {p.starts_at.astimezone(tz):%a %d.%m. %H:%M} "
                f"(Konfidenz {p.confidence:.0%}): "
                + (", ".join(p.candidates) or "keine Kandidaten")
                for p in ov.promos
            ],
        ),
        (
            f"TOTW {ov.totw_week} (Release {ov.totw_release.astimezone(tz):%a %d.%m. %H:%M})"
            if ov.totw_release
            else "TOTW",
            [
                f"{t.rank}. {t.name} ({t.team}) {t.reasons}"
                + (f" · {t.card} {format_coins(t.price)}" if t.card else "")
                for t in ov.totw
            ],
        ),
        ("Radar", ov.radar),
        ("Futter-Index", ov.fodder),
        ("Alerts 24 h", ov.alerts),
        ("Quellen", ov.sources),
        ("Backtest", [ov.backtest] if ov.backtest else []),
    ):
        console.print(f"[bold]{title}[/bold]")
        for line in lines or ["-"]:
            console.print(f"  {line}", highlight=False)


@app.command()
def cards(
    top: Annotated[int, typer.Option("--top", help="How many cards to list.")] = 25,
    group: Annotated[
        str | None, typer.Option("--group", help="Only one position group, e.g. Sturm.")
    ] = None,
) -> None:
    """Play value of all known cards, the usual price for it and the check against usage."""
    from sqlalchemy import select

    from fcast.analysis.cards import (
        agreement,
        card_values,
        expected_price,
        price_fit,
        stat_drivers,
    )
    from fcast.db.base import utcnow
    from fcast.db.models import Player

    settings = get_settings()
    with _db_session() as session:
        values = card_values(session, settings, utcnow())
        players = session.scalars(select(Player).where(Player.id.in_(values))).all()
        names = {p.id: p.display_name for p in players}
        stats = {p.id: p.attributes.stats for p in players if p.attributes}
    fit = price_fit(values)
    check = agreement(values)
    rows = sorted(
        (v for v in values.values() if group is None or v.play.group == group),
        key=lambda v: -v.meta,
    )[:top]
    table = Table(title=f"Spielwert ({len(values)} Karten mit Werten)")
    for column in ("Karte", "Gruppe", "Spielwert", "Meta", "Preis", "Üblich", "Spiele"):
        table.add_column(column, justify="left" if column in ("Karte", "Gruppe") else "right")
    for v in rows:
        usual = expected_price(fit, v)
        table.add_row(
            names.get(v.player_id, str(v.player_id)),
            v.play.group,
            f"{v.play.score:.0f}",
            f"{v.meta:.0f}",
            format_coins(v.price),
            format_coins(usual),
            format_coins(v.games),
        )
    console.print(table)
    if check.correlation is None:
        typer.echo(f"Zu wenige Karten mit Spielzahl für den Abgleich ({check.cards}).")
    else:
        corr = f"{check.correlation:+.2f}".replace(".", ",")
        typer.echo(
            f"Spielwert ↔ {check.basis}: Rangkorrelation {corr} bei {check.cards} Karten "
            "(+1 = passt perfekt, 0 = kein Zusammenhang)"
        )
    if fit is None:
        typer.echo("Noch zu wenige bepreiste Goldkarten für die übliche Preiskurve.")
    drivers = stat_drivers(values, stats)
    if drivers:

        def fmt(items: list[tuple[str, float, int]]) -> str:
            return ", ".join(f"{name} {corr:+.2f}".replace(".", ",") for name, corr, _ in items)

        typer.echo(
            f"Werte, die mit der Nutzung zusammenhängen ({drivers[0][2]} Goldkarten, "
            "Preis herausgerechnet):"
        )
        typer.echo(f"  am stärksten: {fmt(drivers[:6])}")
        typer.echo(f"  am schwächsten: {fmt(drivers[-3:])}")


alert_app = typer.Typer(help="Push alerts.", no_args_is_help=True)
app.add_typer(alert_app, name="alert")


@alert_app.command("test")
def alert_test() -> None:
    """Send a test notification through the configured channel."""
    from fcast.alerts.engine import build_notifier
    from fcast.alerts.notifier import Notification, NotifierError

    notifier = build_notifier(get_settings())

    async def _send() -> None:
        try:
            await notifier.send(
                Notification(
                    title="FCast Test",
                    message="Test aus der CLI - Alerts kommen an.",
                    tags=("white_check_mark",),
                )
            )
        finally:
            await notifier.aclose()

    try:
        asyncio.run(_send())
    except NotifierError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Test notification sent via {notifier.name}.")


@alert_app.command("check")
def alert_check() -> None:
    """Show which alerts would be sent right now (dry run, nothing is sent)."""
    from fcast.alerts.engine import AlertEngine, build_notifier
    from fcast.db.base import utcnow

    settings = get_settings()
    engine = AlertEngine(build_notifier(settings), settings)
    with _db_session() as session:
        report = engine.evaluate(session, utcnow())
    if not report.decisions:
        typer.echo("Keine Alert-Kandidaten.")
        return
    table = Table(title="Alert-Kandidaten")
    for column in ("Entscheidung", "Regel", "Titel"):
        table.add_column(column)
    for candidate, outcome in report.decisions:
        table.add_row(outcome.value, candidate.rule, candidate.notification.title)
    console.print(table)


promo_app = typer.Typer(help="Leak & promo radar.", no_args_is_help=True)
app.add_typer(promo_app, name="promo")


@promo_app.command("add")
def promo_add(
    name: Annotated[str, typer.Argument(help="Promo name, e.g. 'Future Stars'.")],
    start: Annotated[str, typer.Option("--start", help="Start day YYYY-MM-DD (release 19:00).")],
    end: Annotated[str | None, typer.Option("--end", help="End day YYYY-MM-DD.")] = None,
    confidence: Annotated[float, typer.Option("--confidence", min=0, max=1)] = 0.7,
    player: Annotated[list[str] | None, typer.Option("--player", help="Full player name.")] = None,
    club: Annotated[list[str] | None, typer.Option("--club")] = None,
    league: Annotated[list[str] | None, typer.Option("--league")] = None,
    nation: Annotated[list[str] | None, typer.Option("--nation")] = None,
    source: Annotated[str | None, typer.Option("--source", help="Link to the leak.")] = None,
) -> None:
    """Record a promo leak with the players, clubs, leagues and nations it concerns."""
    from datetime import UTC, date, datetime, time

    from fcast.db.models import LinkType
    from fcast.promos import service as promo_service
    from fcast.sources.futbin_locator import slugify

    settings = get_settings()

    def day(value: str) -> datetime:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise typer.BadParameter(f"expected YYYY-MM-DD, got {value}") from exc
        return datetime.combine(parsed, time(19, 0), tzinfo=settings.tz).astimezone(UTC)

    links = [(LinkType.PLAYER, slugify(p)) for p in player or []]
    links += [(LinkType.CLUB, c) for c in club or []]
    links += [(LinkType.LEAGUE, lg) for lg in league or []]
    links += [(LinkType.NATION, n) for n in nation or []]
    if not links:
        raise typer.BadParameter("give at least one --player, --club, --league or --nation")
    with _db_session() as session:
        promo = promo_service.create_promo(
            session, name, day(start), day(end) if end else None, confidence, source, None, links
        )
        typer.echo(f"Promo {promo.id} '{promo.name}' saved with {len(links)} link(s).")


@promo_app.command("list")
def promo_list() -> None:
    """Upcoming and running promos."""
    from fcast.db import repositories as repo
    from fcast.db.base import utcnow

    now = utcnow()
    with _db_session() as session:
        promos = [p for p in repo.list_promos(session) if p.ends_at is None or p.ends_at >= now]
        if not promos:
            typer.echo("Keine Promos.")
            return
        table = Table(title="Promos")
        for column in ("ID", "Name", "Start", "Ende", "Konfidenz", "Bezug"):
            table.add_column(column)
        tz = get_settings().tz
        for p in promos:
            table.add_row(
                str(p.id),
                p.name,
                p.starts_at.astimezone(tz).strftime("%d.%m. %H:%M"),
                p.ends_at.astimezone(tz).strftime("%d.%m.") if p.ends_at else "-",
                f"{p.confidence:.0%}",
                ", ".join(f"{link.link_type.value}:{link.link_value}" for link in p.links),
            )
        console.print(table)


class _PreviewOnlyError(Exception):
    """Leaves `_db_session` without committing (preview of a booking)."""

    def __init__(self, booking: "Booking") -> None:
        super().__init__("preview")
        self.booking = booking


def _book(entries: "list[Entry]", coins: int | None = None, write: bool = True) -> "Booking":
    """Book entries (looking up unknown cards on FUTBIN); commit only if `write` and all ok."""
    from fcast.db.base import utcnow
    from fcast.portfolio import entries as booking_service
    from fcast.sources import futbin as futbin_source
    from fcast.sources.registry import make_http_client

    settings = get_settings()
    source = None
    if futbin_source.SOURCE_NAME in settings.web_sources:
        source = futbin_source.FutbinSource(
            make_http_client(settings), lambda _: None, settings.platform
        )

    async def run(session: Session) -> "Booking":
        try:
            return await booking_service.book(session, entries, utcnow(), coins, source)
        finally:
            if source is not None:
                await source.aclose()

    try:
        with _db_session() as session:
            booking = asyncio.run(run(session))
            if not (write and booking.ok):
                raise _PreviewOnlyError(booking)
            return booking
    except _PreviewOnlyError as rollback:
        return rollback.booking


def _print_booking(booking: "Booking", written: bool) -> None:
    from fcast.portfolio.entries import Outcome

    title = "Booked" if written else "Preview - nothing written yet"
    table = Table(title=title)
    for column in ("Action", "Card", "Price", "#", "Result", "Coins"):
        table.add_column(column, justify="right" if column in ("Price", "#", "Coins") else "left")
    styles = {Outcome.DONE: "green", Outcome.SKIPPED: "yellow", Outcome.ERROR: "red"}
    for result in booking.results:
        note = result.note + (
            " (new card from FUTBIN, now on the watchlist)" if result.new_card else ""
        )
        table.add_row(
            result.entry.action.value,
            result.card or result.entry.card or f"#{result.entry.position_id}",
            format_coins(result.entry.price),
            str(result.position_id or "-"),
            f"[{styles[result.outcome]}]{result.outcome.value}[/]: {note}",
            format_coins(result.coins) if result.coins else "",
        )
    console.print(table)
    if booking.balance_after is not None:
        typer.echo(
            f"Coin balance: {format_coins(booking.balance_before)} -> "
            f"{format_coins(booking.balance_after)}"
        )


def _position_or_card(ref: str) -> tuple[str | None, int | None]:
    """'17' or '#17' is a position id, anything else a card ("Musiala 87", "ea:231747")."""
    value = ref.strip()
    if value.lstrip("#").isdigit():
        return None, int(value.lstrip("#"))
    if value.lower().startswith("ea:"):
        return value[3:].strip(), None
    return value, None


def _single(entry: "Entry") -> "Result":
    from fcast.portfolio.entries import Outcome

    booking = _book([entry])
    result = booking.results[0]
    if result.outcome is Outcome.ERROR:
        typer.secho(f"Error: {result.note}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    _print_booking(booking, written=True)
    return result


CARD_HELP = 'Card as on the screenshot ("Musiala 87", "Olise 91 TOTW") or EA id.'
AGAIN_HELP = "Book even if it looks like a duplicate."


@portfolio_app.command("buy")
def portfolio_buy(
    card: Annotated[str, typer.Argument(help=CARD_HELP)],
    price: Annotated[int, typer.Argument(help="Price paid, in coins.", min=1)],
    again: Annotated[bool, typer.Option("--again", help=AGAIN_HELP)] = False,
) -> None:
    """Record a purchase."""
    from fcast.analysis.pricing import break_even_sell_price
    from fcast.portfolio.entries import Action, Entry

    _single(Entry(Action.BUY, price, card=card, again=again))
    typer.echo(
        f"Sell at {format_coins(break_even_sell_price(price))} or more to break even "
        "after the 5 % tax."
    )


@portfolio_app.command("listed")
def portfolio_listed(
    ref: Annotated[str, typer.Argument(help="Position id (17 or #17) or card.")],
    price: Annotated[int, typer.Argument(help="Asking price, in coins.", min=1)],
    again: Annotated[
        bool, typer.Option("--again", help="Relisted at the same price (e.g. after expiry).")
    ] = False,
) -> None:
    """Note a (re)listing; every listing is kept in the history."""
    from fcast.portfolio.entries import Action, Entry

    card, position_id = _position_or_card(ref)
    _single(Entry(Action.LISTED, price, card=card, position_id=position_id, again=again))


@portfolio_app.command("sell")
def portfolio_sell(
    ref: Annotated[str, typer.Argument(help="Position id (17 or #17) or card.")],
    price: Annotated[int, typer.Argument(help="Price the card sold for, in coins.", min=1)],
    again: Annotated[bool, typer.Option("--again", help=AGAIN_HELP)] = False,
) -> None:
    """Record a sale; without an open position one with unknown buy price is created."""
    from fcast.analysis.pricing import profit as net_profit
    from fcast.db import repositories as repo
    from fcast.portfolio.entries import Action, Entry

    card, position_id = _position_or_card(ref)
    result = _single(Entry(Action.SOLD, price, card=card, position_id=position_id, again=again))
    with _db_session() as session:
        position = repo.get_position(session, result.position_id or 0)
        if position.buy_price is None:
            typer.echo("Buy price unknown - the sale counts for the coin balance only.")
        else:
            gain = net_profit(position.buy_price, price)
            verdict = "profit" if gain >= 0 else "loss"
            typer.echo(
                f"{format_coins(position.buy_price)} -> {format_coins(price)} "
                f"= {format_coins(gain)} {verdict} after tax."
            )


@portfolio_app.command("apply")
def portfolio_apply(
    file: Annotated[str, typer.Argument(help="JSON file with the entries, '-' for stdin.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Write; default is a preview.")] = False,
) -> None:
    """Book several entries from a screenshot at once (preview first).

    JSON: {"coins": 250000, "entries": [{"action": "sold", "card": "Wirtz 86",
    "price": 30000}, {"action": "listed", "position": 12, "price": "45k", "again": true}]}
    """
    import json
    import sys

    from fcast.portfolio.entries import parse_entries

    text = sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8")
    try:
        entries, coins = parse_entries(json.loads(text))
    except (ValueError, json.JSONDecodeError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    booking = _book(entries, coins, write=yes)
    written = yes and booking.ok
    _print_booking(booking, written=written)
    if not booking.ok:
        typer.secho("Nothing written: fix the errors above.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if not yes:
        typer.echo("Run again with --yes to write these entries.")


@portfolio_app.command("rm")
def portfolio_rm(
    position_id: Annotated[int, typer.Argument(help="Position id.", min=1)],
) -> None:
    """Delete a position that was entered by mistake."""
    from fcast.db import repositories as repo

    with _db_session() as session:
        try:
            position = repo.get_position(session, position_id)
        except repo.NotFoundError as exc:
            typer.secho(f"Error: {exc}.", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        name = position.player.display_name
        repo.remove_position(session, position)
        typer.echo(f"Removed position #{position_id} ({name}).")


@portfolio_app.command("coins")
def portfolio_coins(
    amount: Annotated[
        int | None, typer.Argument(help="Current coin balance from the game.", min=0)
    ] = None,
) -> None:
    """Show the coin balance, or enter the current one from the game."""
    from fcast.portfolio import coins as wallet

    with _db_session() as session:
        if amount is not None:
            wallet.set_balance(session, amount)
        balance = wallet.balance(session)
        if balance is None:
            typer.echo("No coin balance yet - enter it with `fcast portfolio coins <amount>`.")
            return
        typer.echo(
            f"Coin balance: {format_coins(balance.coins)} "
            f"(entered {format_coins(balance.entered)}, spent {format_coins(balance.spent)}, "
            f"received {format_coins(balance.received)} "
            "after tax since then). Buy signals above it are not pushed."
        )


@portfolio_app.command("list")
def portfolio_list(
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Include sold positions.")] = False,
) -> None:
    """Open positions, listings, realised profit and the capital currently tied up."""
    from fcast.portfolio import coins as wallet
    from fcast.portfolio import summary as portfolio_summary

    with _db_session() as session:
        summary = portfolio_summary.summarize(
            session, get_settings(), None if show_all else portfolio_summary.OPEN_POSITIONS
        )
        if not summary.rows:
            typer.echo("No positions yet - record one with `fcast portfolio buy`.")
            return

        table = Table(title="Portfolio")
        for column in ("#", "Card", "Buy", "Status", "Sell", "Listed", "Market", "Break-even"):
            table.add_column(column, justify="left" if column in ("Card", "Status") else "right")
        table.add_column("Profit", justify="right")
        for row in summary.rows:
            position = row.position
            listed = f"{row.listings}x" if row.listings else "-"
            if row.market_below_listing:
                listed += " (market lower!)"
            table.add_row(
                str(position.id),
                position.player.display_name,
                format_coins(position.buy_price) if not row.unknown_buy else "unknown",
                position.status.value,
                format_coins(position.sell_price),
                listed,
                format_coins(row.market),
                format_coins(row.break_even),
                format_coins(row.profit if row.profit is not None else row.at_market),
            )
        console.print(table)
        balance = wallet.balance(session)
        typer.echo(
            f"Realised profit: {format_coins(summary.realised)} - capital tied up: "
            f"{format_coins(summary.tied_up)}"
            + (f" - coin balance: {format_coins(balance.coins)}" if balance else "")
        )


@app.command()
def uev(
    max_price: Annotated[
        int, typer.Option("--max-price", help="Only cards up to this market price.", min=1)
    ] = 60_000,
    premium: Annotated[
        int, typer.Option("--premium", help="Extra coins a styled copy may cost.", min=0)
    ] = 500,
    limit: Annotated[int, typer.Option("--limit", help="How many cards per rating.", min=1)] = 5,
    rating: Annotated[
        int | None, typer.Option("--rating", help="Only this card rating, e.g. 86.", min=1)
    ] = None,
    flat: Annotated[
        bool, typer.Option("--flat", help="One list by popularity instead of per rating.")
    ] = False,
) -> None:
    """Shopping list for ÜV: the most played cards that are still cheap to buy."""
    from sqlalchemy import select

    from fcast.analysis.cards import card_values
    from fcast.analysis.uev import by_rating, uev_candidates
    from fcast.db.base import utcnow
    from fcast.db.models import Player

    settings = get_settings()
    with _db_session() as session:
        values = card_values(session, settings, utcnow())
        candidates = uev_candidates(
            values,
            max_price=max_price,
            premium=premium,
            limit=limit if flat else None,
            rating=rating,
        )
        if not candidates:
            typer.echo("No cards with both a price and usage data yet.")
            return
        groups = [(None, candidates)] if flat else [(r, g) for r, g in by_rating(candidates, limit)]
        names = {
            player.id: player.display_name
            for player in session.scalars(
                select(Player).where(Player.id.in_([c.player_id for c in candidates]))
            )
        }

        title = f"ÜV candidates (up to {format_coins(max_price)}, +{premium} premium)"
        table = Table(title=title)
        table.add_column("Rating", justify="right")
        table.add_column("Games", justify="right")
        table.add_column("Card")
        table.add_column("Market", justify="right")
        table.add_column("Max buy", justify="right")
        table.add_column("Break-even", justify="right")
        for group_rating, group in groups:
            if group_rating is not None and group is not groups[0][1]:
                table.add_section()
            for candidate in group:
                table.add_row(
                    str(candidate.rating) if candidate.rating is not None else "-",
                    f"{candidate.games:,}".replace(",", "."),
                    names.get(candidate.player_id, str(candidate.player_id)),
                    format_coins(candidate.price),
                    format_coins(candidate.max_buy),
                    format_coins(candidate.break_even),
                )
        console.print(table)
        typer.echo(
            "Market price is the cheapest offer, usually without a chemistry style. "
            "Buy a styled copy up to 'Max buy' and list it above 'Break-even'."
        )
