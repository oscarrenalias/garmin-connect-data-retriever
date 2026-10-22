"""Command-line interface (placeholder; replaced by the CLI bead)."""

import typer

app = typer.Typer(help="Archive Garmin Connect activities into a local SQLite database.")


@app.callback()
def main() -> None:
    """Archive Garmin Connect activities into a local SQLite database."""
