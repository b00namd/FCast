"""Command line interface."""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.orm import Session

from fcast import __version__
from fcast.config import get_settings

app = typer.Typer(
    name="fcast", help="FCast - EA FC Ultimate Team market analysis.", no_args_is_help=True
)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
watch_app = typer.Typer(help="Manage the watchlist.", no_args_is_help=True)
prices_app = typer.Typer(help="Price data.", no_args_is_help=True)
sources_app = typer.Typer(help="Price source health.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(watch_app, name="watch")
app.add_typer(prices_app, name="prices")
app.add_typer(sources_app, name="sources")

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


@watch_app.command("add")
def watch_add(
    ea_id: Annotated[int, typer.Argument(help="EA player/card id.", min=1)],
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
    """Add a player to the watchlist or update the existing entry."""
    from fcast.db import repositories as repo
    from fcast.sources import futbin as futbin_source

    futbin_ref = None
    if futbin is not None:
        try:
            futbin_ref = futbin_source.normalize_ref(futbin)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--futbin") from exc

    if buy is not None and sell is not None and sell * 0.95 <= buy:
        typer.secho(
            "Warning: target sell after 5% EA tax does not exceed target buy.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    with _db_session() as session:
        player = repo.upsert_player(session, ea_id, repo.PlayerDetails(name=name))
        repo.set_watch(session, player, target_buy=buy, target_sell=sell, note=note)
        if futbin_ref is not None:
            repo.set_source_ref(session, player, futbin_source.SOURCE_NAME, futbin_ref)
        typer.echo(
            f"Watching {player.display_name}: buy {format_coins(buy)}, sell {format_coins(sell)}"
        )


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
