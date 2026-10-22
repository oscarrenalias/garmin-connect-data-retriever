"""Command-line interface. Parses arguments and delegates to the library."""

import json
import os
import signal
import sys
import threading
import traceback
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import httpx
import typer

from garmin_archive import __version__
from garmin_archive.api import GarminApiClient, check_auth
from garmin_archive.client import ArchiveClient
from garmin_archive.config import ArchiveConfig
from garmin_archive.credentials import DEFAULT_ENV_FILE, find_api_key, require_api_key
from garmin_archive.models import GarminArchiveError, InvalidQueryError
from garmin_archive.output import (
    UsageError,
    emit,
    error,
    exit_code_for,
    exit_code_for_reprocess,
    exit_code_for_run_status,
    success,
)
from garmin_archive.processing.form_import import FormImportOutcome
from garmin_archive.query import DEFAULT_LIMIT, DEFAULT_TIMEOUT_S

app = typer.Typer(help="Archive Garmin Connect activities into a local SQLite database.")
auth_app = typer.Typer(help="Inspect Garmin API key configuration.")
app.add_typer(auth_app, name="auth")


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
    env_file: Annotated[
        Path,
        typer.Option("--env-file", help="Path to a .env file holding the Garmin API key."),
    ] = DEFAULT_ENV_FILE,
) -> None:
    """Archive Garmin Connect activities into a local SQLite database."""
    prior: dict[str, Any] = ctx.obj or {}
    injected: httpx.Client | None = prior.get("http_client")
    ctx.obj = {"db": db, "env_file": env_file, "http_client": injected}


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


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Report whether an API key is configured and where it comes from."""
    obj: dict[str, Path | None] = ctx.obj or {}
    env_file = obj.get("env_file") or DEFAULT_ENV_FILE
    found = find_api_key(os.environ, env_file)
    source = None
    if found is not None:
        source = found.variable if found.env_file is None else str(found.env_file)
    emit(success({"configured": found is not None, "source": source}), sys.stdout)


@auth_app.command("check")
def auth_check(ctx: typer.Context) -> None:
    """Verify the API key by requesting a token and fetching one activity page."""
    obj: dict[str, Any] = ctx.obj or {}
    source = require_api_key(os.environ, obj.get("env_file") or DEFAULT_ENV_FILE)
    http_client: httpx.Client | None = obj.get("http_client")
    result = check_auth(GarminApiClient(source.key, http_client=http_client))
    emit(
        success({"authenticated": result.token_obtained, "search_status": result.search_status}),
        sys.stdout,
    )


@app.command()
def update(
    ctx: typer.Context,
    full: Annotated[
        bool,
        typer.Option("--full", help="Re-fetch every resource instead of only new or changed ones."),
    ] = False,
) -> int:
    """Discover and archive every activity from Garmin Connect."""
    obj: dict[str, Any] = ctx.obj or {}
    source = require_api_key(os.environ, obj.get("env_file") or DEFAULT_ENV_FILE)
    http_client: httpx.Client | None = obj.get("http_client")
    api = GarminApiClient(source.key, http_client=http_client)
    stop = threading.Event()

    def _on_sigint(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    previous: Any = None
    installed = False
    try:
        previous = signal.signal(signal.SIGINT, _on_sigint)
        installed = True
    except ValueError:  # not in the main thread; fall back to default handling
        pass
    try:
        with ArchiveClient(ArchiveConfig(db_path=obj.get("db"))) as client:
            result = client.update(api, full=full, stop_flag=stop.is_set)
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)
    envelope_status = "ok" if result.status == "complete" else result.status
    emit(success(result.to_dict(), status=envelope_status), sys.stdout)
    return exit_code_for_run_status(result.status)


@app.command()
def reprocess(
    ctx: typer.Context,
    activity_id: Annotated[
        str | None,
        typer.Option("--activity-id", help="Reprocess only this activity."),
    ] = None,
) -> int:
    """Rebuild derived tables from archived payloads (no network, no credentials)."""
    obj: dict[str, Any] = ctx.obj or {}
    with ArchiveClient(ArchiveConfig(db_path=obj.get("db"))) as client:
        counters = client.reprocess(activity_id)
    emit(
        success(
            {
                "processed": counters.processed,
                "partial": counters.partial,
                "failed": counters.failed,
            }
        ),
        sys.stdout,
    )
    return exit_code_for_reprocess(counters.failed)


_FORM_EXIT_CODES = {
    FormImportOutcome.OK: 0,
    FormImportOutcome.PARTIAL: 6,
    FormImportOutcome.NOTHING_VALID: 2,
    FormImportOutcome.DOWNLOAD_FAILED: 4,
}


@app.command("import-form")
def import_form(
    ctx: typer.Context,
    files: Annotated[
        list[Path] | None,
        typer.Argument(help="FORM .fit or .zip files to import."),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Pre-signed FORM export download URL (never echoed)."),
    ] = None,
) -> int:
    """Import FORM goggles FIT files from local files or a download URL (no credentials)."""
    obj: dict[str, Any] = ctx.obj or {}
    if bool(files) == (url is not None):
        raise InvalidQueryError("provide either FILE arguments or --url")
    http_client: httpx.Client | None = obj.get("http_client")
    owned = url is not None and http_client is None
    if owned:
        http_client = httpx.Client()
    try:
        with ArchiveClient(ArchiveConfig(db_path=obj.get("db"))) as client:
            result = client.import_form(paths=files, url=url, http_client=http_client)
    finally:
        if owned and http_client is not None:
            http_client.close()
    if result.outcome is FormImportOutcome.DOWNLOAD_FAILED:
        emit(error("FORM_DOWNLOAD_ERROR", result.error or "Download failed"), sys.stdout)
    else:
        emit(success(result.to_dict(), status=result.outcome.value), sys.stdout)
    return _FORM_EXIT_CODES[result.outcome]


@app.command()
def schema(ctx: typer.Context) -> None:
    """List the queryable tables and views (no network, no credentials)."""
    obj: dict[str, Any] = ctx.obj or {}
    with ArchiveClient(ArchiveConfig(db_path=obj.get("db"))) as client:
        result = client.schema()
    emit(result, sys.stdout)


@app.command()
def describe(
    ctx: typer.Context, name: Annotated[str, typer.Argument(help="Table or view.")]
) -> None:
    """Describe one queryable table or view."""
    obj: dict[str, Any] = ctx.obj or {}
    with ArchiveClient(ArchiveConfig(db_path=obj.get("db"))) as client:
        result = client.describe(name)
    emit(result, sys.stdout)


@app.command()
def sample(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Table or view.")],
    limit: Annotated[int, typer.Option("--limit", help="Number of rows to return.")] = 5,
) -> None:
    """Return the first rows of a queryable table or view."""
    obj: dict[str, Any] = ctx.obj or {}
    with ArchiveClient(ArchiveConfig(db_path=obj.get("db"))) as client:
        result = client.sample(name, limit)
    emit(result, sys.stdout)


@app.command()
def sql(
    ctx: typer.Context,
    query: Annotated[str, typer.Option("--query", help="One read-only SQL statement.")],
    params: Annotated[
        str | None,
        typer.Option("--params", help="JSON object of named bound parameters."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum rows returned.")] = DEFAULT_LIMIT,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Query timeout in seconds.")
    ] = DEFAULT_TIMEOUT_S,
) -> None:
    """Run one read-only SQL statement against the archive."""
    obj: dict[str, Any] = ctx.obj or {}
    parameters: Any = None
    if params is not None:
        try:
            parameters = json.loads(params)
        except ValueError as exc:
            raise InvalidQueryError("--params is not valid JSON") from exc
    with ArchiveClient(ArchiveConfig(db_path=obj.get("db"))) as client:
        result = client.query(query, parameters, limit=limit, timeout_s=timeout)
    emit(result, sys.stdout)


def main(argv: list[str] | None = None, *, http_client: httpx.Client | None = None) -> int:
    """Run the CLI and return the process exit code.

    ``http_client`` lets callers (tests) inject the HTTP client used by network commands.
    """
    try:
        result: Any = app(args=argv, standalone_mode=False, obj={"http_client": http_client})
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
