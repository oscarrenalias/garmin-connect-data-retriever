"""Command-line interface. Parses arguments and delegates to the library."""

from typing import Annotated

import typer

from garmin_archive import __version__

app = typer.Typer(help="Archive Garmin Connect activities into a local SQLite database.")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Archive Garmin Connect activities into a local SQLite database."""
