"""Contract report model for the discovery tool (spec B6). Pure; no I/O.

`build_report` accepts whatever the run produced so far, so an early stop still
yields a schema-valid report. Missing sections get a `unknown` finding. Raw
activity IDs and secrets never belong in the inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

REPORT_VERSION = 1
STATUS_CONFIRMED = "confirmed"
STATUS_NOT_OBSERVED = "not_observed"
STATUS_UNKNOWN = "unknown"
FINDING_STATUSES = frozenset({STATUS_CONFIRMED, STATUS_NOT_OBSERVED, STATUS_UNKNOWN})
OUTCOMES = frozenset({"complete", "partial", "stopped", "needs_token_field"})

SPEC_2_3_KEYS: tuple[str, ...] = (
    "1_token_and_header",
    "2_pagination",
    "3_history",
    "4_detail_and_zip_by_sport",
    "5_manual_multisport_type_keys",
    "6_rate_limits_and_missing_files",
)
SPEC_4_CATEGORIES: tuple[str, ...] = ("running", "pool_swim", "cardio", "other")
NO_RATE_LIMIT_NOTE = "absence of rate-limit headers does not prove there are no limits"
HISTORY_NOTE = "historical restrictions cannot be proven by this tool"

Section = dict[str, Any]


@dataclass
class ReportInputs:
    """Everything a run has gathered. Any section may be None after an early stop."""

    run_id: str
    outcome: str
    token: Mapping[str, Any] | None = None
    header_convention: Mapping[str, Any] | None = None
    pagination: Mapping[str, Any] | None = None
    page_size_cap: Mapping[str, Any] | None = None
    ordering: Mapping[str, Any] | None = None
    inventory: Mapping[str, Any] | None = None
    samples: Sequence[Mapping[str, Any]] = ()
    categories: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: {})
    http: Mapping[str, Any] | None = None
    leak_check: Mapping[str, Any] | None = None


def _section(value: Mapping[str, Any] | None, default: Section) -> Section:
    result: Section = dict(value) if value is not None else dict(default)
    result.setdefault("status", STATUS_UNKNOWN)
    return result


def _is_2xx(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 200 <= value < 300


def _sample_ok(sample: Mapping[str, Any], key: str) -> bool:
    part: object = sample.get(key)
    if not isinstance(part, Mapping):
        return False
    typed: Mapping[str, Any] = part  # pyright: ignore[reportUnknownVariableType]
    return _is_2xx(typed.get("http_status"))


def _category_sampled(categories: Mapping[str, Mapping[str, Any]], name: str) -> bool:
    entry = categories.get(name)
    return (
        entry is not None
        and entry.get("status") == STATUS_CONFIRMED
        and int(entry.get("selected", 0) or 0) > 0
    )


def _category_absent(categories: Mapping[str, Mapping[str, Any]], name: str) -> bool:
    entry = categories.get(name)
    return entry is not None and entry.get("status") == STATUS_NOT_OBSERVED


def derive_spec_2_3(report: Mapping[str, Any]) -> dict[str, str]:
    """Derive the SPEC §2.3 statuses from a built report (spec B6, rules 1-6)."""
    token: Mapping[str, Any] = report.get("token") or {}
    header: Mapping[str, Any] = report.get("header_convention") or {}
    pagination: Mapping[str, Any] = report.get("pagination") or {}
    ordering: Mapping[str, Any] = report.get("ordering") or {}
    samples: Sequence[Mapping[str, Any]] = report.get("samples") or ()
    categories: Mapping[str, Mapping[str, Any]] = report.get("categories") or {}
    http: Mapping[str, Any] = report.get("http") or {}

    rule1 = (
        bool(token.get("token_field"))
        and token.get("status") == STATUS_CONFIRMED
        and header.get("scheme") == "Bearer"
        and header.get("status") == STATUS_CONFIRMED
        and _is_2xx(header.get("probe_http_status"))
    )
    rule2 = pagination.get("inventory_status") == "complete" and isinstance(
        ordering.get("stable"), bool
    )
    rule4 = all(
        any(
            s.get("category") == cat and _sample_ok(s, "summary") and _sample_ok(s, "original_file")
            for s in samples
        )
        for cat in SPEC_4_CATEGORIES
    )
    if _category_sampled(categories, "manual") and _category_sampled(categories, "multisport"):
        rule5 = STATUS_CONFIRMED
    elif _category_absent(categories, "manual") and _category_absent(categories, "multisport"):
        rule5 = STATUS_NOT_OBSERVED
    else:
        rule5 = STATUS_UNKNOWN
    rule6 = (
        bool(http.get("rate_limit_headers_seen")) or int(http.get("status_429_count", 0) or 0) > 0
    )

    def flag(ok: bool) -> str:
        return STATUS_CONFIRMED if ok else STATUS_UNKNOWN

    return {
        "1_token_and_header": flag(rule1),
        "2_pagination": flag(rule2),
        "3_history": STATUS_UNKNOWN,
        "4_detail_and_zip_by_sport": flag(rule4),
        "5_manual_multisport_type_keys": rule5,
        "6_rate_limits_and_missing_files": STATUS_CONFIRMED if rule6 else STATUS_NOT_OBSERVED,
    }


def build_report(inputs: ReportInputs) -> dict[str, Any]:
    """Assemble the `contract-report.json` structure (report_version 1)."""
    http = _section(
        inputs.http,
        {
            "rate_limit_headers_seen": [],
            "retry_after_seen": False,
            "status_429_count": 0,
            "redirects": [],
            "other_header_names": [],
            "failures": [],
        },
    )
    leak_check = _section(inputs.leak_check, {"failed_fixtures": []})
    leak_check.setdefault("failed_fixtures", [])
    pagination = _section(inputs.pagination, {"inventory_status": "incomplete", "pages": []})
    ordering = _section(inputs.ordering, {})
    inventory = _section(inputs.inventory, {})

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": inputs.run_id,
        "outcome": inputs.outcome,
        "token": _section(inputs.token, {}),
        "header_convention": _section(inputs.header_convention, {}),
        "pagination": pagination,
        "page_size_cap": _section(inputs.page_size_cap, {"probes": []}),
        "ordering": ordering,
        "inventory": inventory,
        "samples": [dict(s) for s in inputs.samples],
        "categories": {k: dict(v) for k, v in inputs.categories.items()},
        "http": http,
        "leak_check": leak_check,
    }
    spec = derive_spec_2_3(report)
    if spec["6_rate_limits_and_missing_files"] == STATUS_NOT_OBSERVED:
        http.setdefault("note", NO_RATE_LIMIT_NOTE)
    report["spec_2_3"] = spec
    return report


__all__ = [
    "FINDING_STATUSES",
    "OUTCOMES",
    "REPORT_VERSION",
    "SPEC_2_3_KEYS",
    "ReportInputs",
    "build_report",
    "derive_spec_2_3",
]
