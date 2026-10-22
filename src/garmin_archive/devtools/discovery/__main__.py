"""Command-line entrypoint: `python -m garmin_archive.devtools.discovery capture`.

Exit codes: 0 complete and leak check passed; 1 unexpected error or leak failure; 2 partial,
stopped or needs_token_field; 3 API key missing. No option accepts the API key; it comes from
the environment or a dotenv file only. Error output never includes exception messages, which
could echo request details.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import httpx

from garmin_archive.devtools.discovery.capture import (
    CaptureDeps,
    CaptureOptions,
    CaptureResult,
    run_capture,
)
from garmin_archive.devtools.discovery.credentials import (
    MissingApiKeyError,
    load_api_key,
)
from garmin_archive.devtools.discovery.fixtures import DEFAULT_FIXTURES_DIR
from garmin_archive.devtools.discovery.inventory import (
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    MIN_PAGE_SIZE,
)
from garmin_archive.devtools.discovery.selection import DEFAULT_CARDIO_TYPES, DEFAULT_PER_CATEGORY

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_MISSING_KEY = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class MainDeps:
    """Injected process-level dependencies; defaults are the real ones."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    cwd: Path = field(default_factory=Path.cwd)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = _utcnow
    transport: httpx.BaseTransport | None = None
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    capture: Callable[[CaptureOptions, CaptureDeps], CaptureResult] = run_capture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m garmin_archive.devtools.discovery",
        description="Garmin Personal API contract discovery.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser(
        "capture",
        help="capture contract fixtures and write the contract report",
        description=(
            "Capture sanitized contract fixtures. The API key is read from GARMIN_API_KEY or "
            "GARMIN_CONNECT_API_KEY (environment or .env file); it is never a CLI option."
        ),
    )
    cap.add_argument("--env-file", type=Path, default=None, help="dotenv file with the API key")
    cap.add_argument("--token-field", default=None, help="token response field holding the token")
    cap.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    cap.add_argument(
        "--min-page-size",
        type=int,
        default=MIN_PAGE_SIZE,
        help="smallest page size adaptive reduction may fall back to",
    )
    cap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    cap.add_argument(
        "--activity-id",
        action="append",
        default=[],
        help="explicit activity id to sample (repeatable)",
    )
    cap.add_argument("--per-category", type=int, default=DEFAULT_PER_CATEGORY)
    cap.add_argument(
        "--cardio-type",
        action="append",
        default=None,
        help="activity type counted as cardio (repeatable; replaces the defaults)",
    )
    cap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    cap.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURES_DIR)
    return parser


def _exit_code(result: CaptureResult) -> int:
    if result.leak_failures:
        return EXIT_ERROR
    return EXIT_OK if result.outcome == "complete" else EXIT_PARTIAL


def main(argv: Sequence[str] | None = None, deps: MainDeps | None = None) -> int:
    deps = deps or MainDeps()
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR

    out, err = deps.stdout, deps.stderr

    def progress(message: str) -> None:
        print(message, file=err, flush=True)

    try:
        api_key = load_api_key(deps.environ, args.env_file, deps.cwd)
    except MissingApiKeyError as exc:
        print(str(exc), file=err)
        return EXIT_MISSING_KEY

    options = CaptureOptions(
        fixtures_dir=args.fixtures_dir,
        token_field=args.token_field,
        explicit_ids=tuple(args.activity_id),
        per_category=args.per_category,
        cardio_types=tuple(args.cardio_type) if args.cardio_type else DEFAULT_CARDIO_TYPES,
        delay=args.delay,
        max_pages=args.max_pages,
        page_size=args.page_size,
        min_page_size=args.min_page_size,
    )
    capture_deps = CaptureDeps(
        api_key=api_key,
        sleep=deps.sleep,
        transport=deps.transport,
        clock=deps.clock,
        progress=progress,
    )
    try:
        result = deps.capture(options, capture_deps)
    except Exception as exc:
        print(f"unexpected error: {type(exc).__name__}", file=err)
        return EXIT_ERROR

    print(f"outcome: {result.outcome}", file=out)
    print(f"report: {result.report_path}", file=out)
    print(f"fixtures written: {result.fixture_count}", file=out)
    if result.action_hint:
        print(f"next: {result.action_hint}", file=out)
    if result.leak_failures:
        print(f"leak check failed for {len(result.leak_failures)} fixture(s)", file=err)
    return _exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
