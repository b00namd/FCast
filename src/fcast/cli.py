"""Command line interface."""

from typing import Annotated

import typer

from fcast import __version__

app = typer.Typer(
    name="fcast", help="FCast - EA FC Ultimate Team market analysis.", no_args_is_help=True
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fcast {__version__}")
        raise typer.Exit()


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
    from fcast.config import get_settings

    settings = get_settings()
    for key, value in settings.model_dump().items():
        typer.echo(f"{key} = {value}")
