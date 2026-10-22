"""Command-line interface. Parses arguments and delegates to the library."""

import sys
import traceback
from pathlib import Path
from typing import Annotated, Any

import typer

from garmin_archive import __version__
from garmin_archive.client import ArchiveClient
from garmin_archive.config import ArchiveConfig
from garmin_archive.models import GarminArchiveError
from garmin_archive.output import UsageError, emit, error, exit_code_for, success

app = typer.Typer(help="Archive Garmin Connect activities into a local SQLite database.")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="Path to the archive database (overrides GARMIN_ARCHIVE_DB)."),
    ] = None,
) -> None:
    """Archive Garmin Connect activities into a local SQLite database."""
    ctx.obj = {"db": db}


@app.command()
def init(ctx: typer.Context) -> None:
    """Create the database and apply known migrations."""
    obj: dict[str, Path | None] = ctx.obj or {}
    db = obj.get("db")
    with ArchiveClient(ArchiveConfig(db_path=db)) as client:
        result = client.initialize()
    emit(
        success(
            {
                "db_path": str(result.db_path),
                "schema_version": result.schema_version,
                "created": result.created,
                "applied_migrations": result.applied_migrations,
            }
        ),
        sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    try:
        result: Any = app(args=argv, standalone_mode=False)
    except UsageError as exc:
        emit(error("INVALID_ARGUMENT", str(exc)), sys.stdout)
        return exit_code_for(exc)
    except GarminArchiveError as exc:
        emit(
            error(exc.code, exc.message, retryable=exc.retryable, details=exc.details or None),
            sys.stdout,
        )
        return exit_code_for(exc)
    except typer.Exit as exc:
        return exc.exit_code
    except typer.Abort:
        return 130
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit(error("INTERNAL_ERROR", f"Unexpected error: {type(exc).__name__}"), sys.stdout)
        return 1
    return result if isinstance(result, int) else 0


def _console_entry() -> None:  # pyright: ignore[reportUnusedFunction]
    sys.exit(main())
