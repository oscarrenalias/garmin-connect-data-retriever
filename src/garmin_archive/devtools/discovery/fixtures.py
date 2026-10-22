"""Sanitized fixture writing with leak gating for the discovery tool (spec B6, B7).

Every JSON fixture is sanitized first. Every file written under the fixtures directory
(including manifests, the redaction log, the report and its Markdown rendering) passes the
leak check immediately before writing. On a hit the file is skipped and `{file, json_path}`
is recorded; the run continues. Matched values are never recorded.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garmin_archive.devtools.discovery.report import (
    ReportInputs,
    build_report,
    render_markdown,
)
from garmin_archive.devtools.discovery.samples import SampleResult
from garmin_archive.devtools.sanitize import (
    SanitizeContext,
    check_leaks,
    sanitize,
    sensitive_set,
)

DEFAULT_FIXTURES_DIR = Path("tests/fixtures/contract")
REDACTION_LOG_NAME = "redaction-log.json"
REPORT_JSON_NAME = "contract-report.json"
REPORT_MD_NAME = "contract-report.md"
LEAK_PASSED = "passed"
LEAK_FAILED = "failed"
_MAX_REPORT_PASSES = 4


@dataclass
class _Pending:
    """A file ready to be leak-checked and written. `source` None means run-wide checks."""

    rel: str
    text: str
    source: str | None


@dataclass
class FixtureOutcome:
    """Result of fixture writing. `failed` holds `{file, json_path}` entries only."""

    written: list[str] = field(default_factory=lambda: [])
    failed: list[dict[str, str]] = field(default_factory=lambda: [])
    notes: list[str] = field(default_factory=lambda: [])


def serialize_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _check(pending: _Pending, ctx: SanitizeContext) -> list[dict[str, str]]:
    sensitive = sensitive_set(ctx, pending.source)
    hits = check_leaks(pending.text, sensitive)
    return [{"file": pending.rel, "json_path": hit.json_path} for hit in hits]


def _write(fixtures_dir: Path, pending: _Pending) -> None:
    target = fixtures_dir / pending.rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pending.text, encoding="utf-8", newline="\n")


def _gate(
    fixtures_dir: Path, pending: _Pending, ctx: SanitizeContext, outcome: FixtureOutcome
) -> None:
    hits = _check(pending, ctx)
    if hits:
        outcome.failed.extend(hits)
        return
    _write(fixtures_dir, pending)
    outcome.written.append(pending.rel)


def _redaction_log(ctx: SanitizeContext) -> dict[str, Any]:
    by_source: dict[str, list[dict[str, str]]] = {}
    for entry in ctx.redaction_log:
        by_source.setdefault(entry.source, []).append(
            {"json_path": entry.json_path, "rule": entry.rule}
        )
    return {"fixtures": by_source}


def write_sample_fixtures(
    fixtures_dir: Path, results: Iterable[SampleResult], ctx: SanitizeContext
) -> FixtureOutcome:
    """Sanitize and write summaries, workouts, search items, manifests and the redaction log.

    All documents are sanitized before any file is checked so that the run-wide sensitive set
    is complete when manifests and the redaction log are checked.
    """
    outcome = FixtureOutcome()
    pending: list[_Pending] = []
    manifests: list[_Pending] = []
    for result in results:
        name = result.sample.name
        documents: list[tuple[str, Any]] = []
        if result.summary_json is not None:
            documents.append((f"summaries/{name}.json", result.summary_json))
        else:
            outcome.notes.append(f"summaries/{name}.json skipped: no summary body")
        if result.workouts_json is not None:
            documents.append((f"workouts/{name}.json", result.workouts_json))
        else:
            outcome.notes.append(f"workouts/{name}.json skipped: no workouts body")
        if result.sample.item is not None:
            documents.append((f"search-items/{name}.json", dict(result.sample.item)))
        else:
            outcome.notes.append(f"search-items/{name}.json skipped: not in inventory")
        for rel, document in documents:
            pending.append(_Pending(rel, serialize_json(sanitize(document, ctx, rel)), rel))
        if result.manifest is not None:
            manifests.append(
                _Pending(f"files/{name}.manifest.json", serialize_json(result.manifest), None)
            )
        else:
            outcome.notes.append(f"files/{name}.manifest.json skipped: no original file")

    for item in pending:
        _gate(fixtures_dir, item, ctx, outcome)
    for item in manifests:
        _gate(fixtures_dir, item, ctx, outcome)
    log = _Pending(REDACTION_LOG_NAME, serialize_json(_redaction_log(ctx)), None)
    _gate(fixtures_dir, log, ctx, outcome)
    return outcome


def write_report(
    fixtures_dir: Path,
    inputs: ReportInputs,
    ctx: SanitizeContext,
    outcome: FixtureOutcome,
) -> dict[str, Any]:
    """Write `contract-report.json` and `.md` last, each leak-checked, and return the report.

    The report embeds `leak_check` (including failures of earlier fixtures and of the report
    files themselves). A failure of one report file is folded into the report and the pair is
    re-rendered until stable, so the written files reflect the final leak status.
    """
    base_leak: Mapping[str, Any] = inputs.leak_check or {}
    report: dict[str, Any] = {}
    failed: list[dict[str, str]] = list(outcome.failed)
    json_pending = _Pending(REPORT_JSON_NAME, "", None)
    md_pending = _Pending(REPORT_MD_NAME, "", None)
    json_hits: list[dict[str, str]] = []
    md_hits: list[dict[str, str]] = []
    for _ in range(_MAX_REPORT_PASSES):
        inputs.leak_check = {
            **base_leak,
            "status": LEAK_FAILED if failed else LEAK_PASSED,
            "failed_fixtures": list(failed),
        }
        report = build_report(inputs)
        if outcome.notes:
            report["notes"] = list(outcome.notes)
        json_pending.text = serialize_json(report)
        md_pending.text = render_markdown(report)
        json_hits = _check(json_pending, ctx)
        md_hits = _check(md_pending, ctx)
        merged = list(failed)
        for hit in (*json_hits, *md_hits):
            if hit not in merged:
                merged.append(hit)
        if merged == failed:
            break
        failed = merged
    outcome.failed = list(failed)
    if not json_hits:
        _write(fixtures_dir, json_pending)
        outcome.written.append(json_pending.rel)
    if not md_hits:
        _write(fixtures_dir, md_pending)
        outcome.written.append(md_pending.rel)
    return report


__all__ = [
    "DEFAULT_FIXTURES_DIR",
    "FixtureOutcome",
    "serialize_json",
    "write_report",
    "write_sample_fixtures",
]
