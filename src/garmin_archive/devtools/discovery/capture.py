"""Capture orchestration for the discovery tool (spec B1-B6).

`run_capture` wires the token step, inventory, sample selection, per-activity fetching, report
building and fixture writing. Every run that gets past credential loading writes
`contract-report.json` and `.md`, including runs that stop early. The module never prints or
exits; progress goes through the injected callback and the caller decides the exit code.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from garmin_archive.devtools.discovery.credentials import SecretStr
from garmin_archive.devtools.discovery.fixtures import (
    DEFAULT_FIXTURES_DIR,
    REPORT_JSON_NAME,
    REPORT_MD_NAME,
    FixtureOutcome,
    write_report,
    write_sample_fixtures,
)
from garmin_archive.devtools.discovery.http import DiscoveryHttp, RunPartial
from garmin_archive.devtools.discovery.inventory import (
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    MIN_PAGE_SIZE,
    InventoryResult,
    run_inventory,
)
from garmin_archive.devtools.discovery.raw import RawRecorder
from garmin_archive.devtools.discovery.report import (
    STATUS_CONFIRMED,
    STATUS_NOT_OBSERVED,
    STATUS_UNKNOWN,
    ReportInputs,
)
from garmin_archive.devtools.discovery.samples import SampleResult, fetch_sample
from garmin_archive.devtools.discovery.selection import (
    DEFAULT_CARDIO_TYPES,
    DEFAULT_PER_CATEGORY,
    SelectionResult,
    select_samples,
)
from garmin_archive.devtools.discovery.token import TokenStepResult, acquire_token
from garmin_archive.devtools.sanitize import SanitizeContext

ProgressReporter = Callable[[str], None]

TOKEN_FIELD_HINT = "rerun with --token-field"
PARTIAL_HINT = "run ended early; wait and rerun, or raise --max-pages"
STOPPED_HINT = "run stopped; see the report for the failing step"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _no_progress(_message: str) -> None:
    return None


@dataclass
class CaptureOptions:
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR
    raw_dir: Path | None = None
    token_field: str | None = None
    explicit_ids: Sequence[str] = ()
    per_category: int = DEFAULT_PER_CATEGORY
    cardio_types: Iterable[str] = DEFAULT_CARDIO_TYPES
    delay: float = 1.0
    max_pages: int = DEFAULT_MAX_PAGES
    page_size: int = DEFAULT_PAGE_SIZE
    min_page_size: int = MIN_PAGE_SIZE


@dataclass
class CaptureDeps:
    """Injected dependencies. `sleep` has no default so tests never sleep for real."""

    api_key: SecretStr
    sleep: Callable[[float], None]
    transport: httpx.BaseTransport | None = None
    clock: Callable[[], datetime] = _utcnow
    progress: ProgressReporter = _no_progress
    rng: random.Random = field(default_factory=random.Random)


@dataclass
class CaptureResult:
    outcome: str
    run_id: str
    report_path: Path
    report_md_path: Path
    fixture_count: int
    leak_failures: list[dict[str, str]]
    action_hint: str | None = None


def _http_section(http: DiscoveryHttp, failures: list[dict[str, str]]) -> dict[str, Any]:
    stats = http.stats
    seen = bool(stats.rate_limit_headers or stats.count_429 or stats.retry_after_seen)
    return {
        "status": STATUS_CONFIRMED if seen else STATUS_NOT_OBSERVED,
        "rate_limit_headers_seen": sorted(stats.rate_limit_headers),
        "retry_after_seen": bool(stats.retry_after_seen),
        "status_429_count": stats.count_429,
        "redirects": [{"host": host} for host in stats.redirects],
        "other_header_names": sorted(stats.other_header_names),
        "failures": list(failures),
    }


def _inventory_sections(inv: InventoryResult) -> dict[str, dict[str, Any]]:
    pagination = {
        **inv.pagination,
        "status": STATUS_CONFIRMED if inv.inventory_status == "complete" else STATUS_UNKNOWN,
    }
    if inv.note:
        pagination["note"] = inv.note
    probes: list[dict[str, Any]] = []
    for p in inv.cap_probes:
        probe: dict[str, Any] = {"requested": p["limit"], "returned": p.get("item_count")}
        for key in ("http_status", "error", "note"):
            if key in p:
                probe[key] = p[key]
        probes.append(probe)
    cap: dict[str, Any] = {
        "status": STATUS_CONFIRMED if probes else STATUS_UNKNOWN,
        "probes": probes,
    }
    ordering: dict[str, Any] = dict(inv.ordering_probe or {})
    ordering["status"] = (
        STATUS_CONFIRMED if isinstance(ordering.get("stable"), bool) else STATUS_UNKNOWN
    )
    inventory: dict[str, Any] = {
        **inv.inventory,
        "status": STATUS_CONFIRMED if inv.items else STATUS_UNKNOWN,
    }
    return {"pagination": pagination, "cap": cap, "ordering": ordering, "inventory": inventory}


def _categories(selection: SelectionResult) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, result in selection.categories.items():
        entry = {k: v for k, v in asdict(result).items() if v is not None}
        if name == "other" and selection.other_type_key is not None:
            entry["type_key"] = selection.other_type_key
        out[name] = entry
    return out


def _hint(outcome: str) -> str | None:
    return {
        "needs_token_field": TOKEN_FIELD_HINT,
        "partial": PARTIAL_HINT,
        "stopped": STOPPED_HINT,
    }.get(outcome)


def run_capture(options: CaptureOptions, deps: CaptureDeps) -> CaptureResult:
    """Run the discovery capture and write the report. May raise only on unexpected errors."""
    progress = deps.progress
    recorder = RawRecorder(options.raw_dir, clock=deps.clock)
    http = DiscoveryHttp(
        delay=options.delay, sleep=deps.sleep, clock=deps.clock, transport=deps.transport
    )
    ctx = SanitizeContext(rng=deps.rng)
    fixtures_dir = options.fixtures_dir
    fixture_outcome = FixtureOutcome()
    outcome = "complete"
    token_step: TokenStepResult | None = None
    inv: InventoryResult | None = None
    selection: SelectionResult | None = None
    results: list[SampleResult] = []
    failures: list[dict[str, str]] = []
    try:
        try:
            progress("requesting token")
            token_step = acquire_token(http, deps.api_key, options.token_field)
            if token_step.outcome is not None or token_step.token is None:
                outcome = token_step.outcome or "stopped"
                if token_step.note:
                    progress(token_step.note)
            else:
                token = token_step.token
                progress("fetching activity inventory")
                inv = run_inventory(
                    http,
                    recorder,
                    token,
                    page_size=options.page_size,
                    max_pages=options.max_pages,
                    min_page_size=options.min_page_size,
                )
                if inv.partial:
                    outcome = "partial"
                    progress(f"inventory incomplete: {inv.note or 'unknown reason'}")
                selection = select_samples(
                    inv.items, options.explicit_ids, options.per_category, options.cardio_types
                )
                total = len(selection.samples)
                for n, sample in enumerate(selection.samples, start=1):
                    progress(f"fetching sample {n}/{total}: {sample.name}")
                    results.append(fetch_sample(http, recorder, token, sample, ctx, failures))
        except RunPartial as stop:
            outcome = "partial"
            progress(f"stopping: {stop.reason}")

        progress("writing fixtures and report")
        if results:
            fixture_outcome = write_sample_fixtures(fixtures_dir, results, ctx)
        sections = _inventory_sections(inv) if inv is not None else {}
        inputs = ReportInputs(
            run_id=recorder.run_id,
            outcome=outcome,
            token=token_step.token_report if token_step else None,
            header_convention=token_step.header_convention if token_step else None,
            pagination=sections.get("pagination"),
            page_size_cap=sections.get("cap"),
            ordering=sections.get("ordering"),
            inventory=sections.get("inventory"),
            samples=[r.record for r in results],
            categories=_categories(selection) if selection is not None else {},
            http=_http_section(http, failures),
        )
        write_report(fixtures_dir, inputs, ctx, fixture_outcome)
    finally:
        http.close()

    return CaptureResult(
        outcome=outcome,
        run_id=recorder.run_id,
        report_path=fixtures_dir / REPORT_JSON_NAME,
        report_md_path=fixtures_dir / REPORT_MD_NAME,
        fixture_count=len(fixture_outcome.written),
        leak_failures=list(fixture_outcome.failed),
        action_hint=_hint(outcome),
    )


__all__ = [
    "CaptureDeps",
    "CaptureOptions",
    "CaptureResult",
    "ProgressReporter",
    "run_capture",
]
