from __future__ import annotations

from typing import Any

from garmin_archive.devtools.discovery.report import (
    FINDING_STATUSES,
    NO_RATE_LIMIT_NOTE,
    SPEC_2_3_KEYS,
    ReportInputs,
    build_report,
    derive_spec_2_3,
    render_markdown,
)

TOP_LEVEL = {
    "report_version",
    "run_id",
    "outcome",
    "token",
    "header_convention",
    "pagination",
    "page_size_cap",
    "ordering",
    "inventory",
    "samples",
    "categories",
    "http",
    "leak_check",
    "spec_2_3",
}


def good_report() -> dict[str, Any]:
    samples = [
        {
            "category": c,
            "summary": {"http_status": 200},
            "original_file": {"http_status": 200},
        }
        for c in ("running", "pool_swim", "cardio", "other")
    ]
    return {
        "token": {"token_field": "accessToken", "status": "confirmed"},
        "header_convention": {
            "scheme": "Bearer",
            "status": "confirmed",
            "probe_http_status": 200,
        },
        "pagination": {"inventory_status": "complete"},
        "ordering": {"stable": True},
        "samples": samples,
        "categories": {
            "manual": {"status": "confirmed", "selected": 1},
            "multisport": {"status": "confirmed", "selected": 1},
        },
        "http": {"rate_limit_headers_seen": ["x-ratelimit-limit"], "status_429_count": 0},
    }


def test_baseline_all_confirmed_except_history() -> None:
    spec = derive_spec_2_3(good_report())
    assert spec == {
        "1_token_and_header": "confirmed",
        "2_pagination": "confirmed",
        "3_history": "unknown",
        "4_detail_and_zip_by_sport": "confirmed",
        "5_manual_multisport_type_keys": "confirmed",
        "6_rate_limits_and_missing_files": "confirmed",
    }


def test_rule1() -> None:
    r = good_report()
    r["token"] = {"status": "confirmed"}
    assert derive_spec_2_3(r)["1_token_and_header"] == "unknown"
    r = good_report()
    r["header_convention"]["probe_http_status"] = 401
    assert derive_spec_2_3(r)["1_token_and_header"] == "unknown"
    r = good_report()
    del r["header_convention"]
    assert derive_spec_2_3(r)["1_token_and_header"] == "unknown"


def test_rule2() -> None:
    r = good_report()
    r["pagination"] = {"inventory_status": "incomplete"}
    assert derive_spec_2_3(r)["2_pagination"] == "unknown"
    r = good_report()
    r["ordering"] = {"stable": None}
    assert derive_spec_2_3(r)["2_pagination"] == "unknown"
    r = good_report()
    r["ordering"] = {"stable": False}
    assert derive_spec_2_3(r)["2_pagination"] == "confirmed"
    del r["ordering"]
    assert derive_spec_2_3(r)["2_pagination"] == "unknown"


def test_rule3_always_unknown() -> None:
    assert derive_spec_2_3(good_report())["3_history"] == "unknown"
    assert derive_spec_2_3({})["3_history"] == "unknown"


def test_rule4() -> None:
    r = good_report()
    r["samples"] = [s for s in r["samples"] if s["category"] != "other"]
    assert derive_spec_2_3(r)["4_detail_and_zip_by_sport"] == "unknown"
    r = good_report()
    del r["samples"][0]["original_file"]
    assert derive_spec_2_3(r)["4_detail_and_zip_by_sport"] == "unknown"
    r = good_report()
    r["samples"][0]["summary"]["http_status"] = 404
    assert derive_spec_2_3(r)["4_detail_and_zip_by_sport"] == "unknown"


def test_rule5() -> None:
    r = good_report()
    r["categories"] = {
        "manual": {"status": "not_observed", "selected": 0},
        "multisport": {"status": "not_observed", "selected": 0},
    }
    assert derive_spec_2_3(r)["5_manual_multisport_type_keys"] == "not_observed"
    r["categories"]["manual"] = {"status": "confirmed", "selected": 2}
    assert derive_spec_2_3(r)["5_manual_multisport_type_keys"] == "unknown"
    assert derive_spec_2_3({})["5_manual_multisport_type_keys"] == "unknown"


def test_rule6() -> None:
    r = good_report()
    r["http"] = {"rate_limit_headers_seen": [], "status_429_count": 2}
    assert derive_spec_2_3(r)["6_rate_limits_and_missing_files"] == "confirmed"
    r["http"] = {"rate_limit_headers_seen": [], "status_429_count": 0}
    assert derive_spec_2_3(r)["6_rate_limits_and_missing_files"] == "not_observed"


def test_build_report_stopped_has_all_keys_and_valid_statuses() -> None:
    report = build_report(ReportInputs("run-1", "stopped"))
    assert set(report) == TOP_LEVEL
    assert report["outcome"] == "stopped"
    assert set(report["spec_2_3"]) == set(SPEC_2_3_KEYS)
    assert set(report["spec_2_3"].values()) <= FINDING_STATUSES
    assert report["token"]["status"] == "unknown"
    assert report["http"]["note"] == NO_RATE_LIMIT_NOTE
    assert report["leak_check"]["failed_fixtures"] == []


def test_build_report_needs_token_field() -> None:
    inputs = ReportInputs(
        "run-2",
        "needs_token_field",
        token={"status": "unknown", "http_status": 200},
    )
    report = build_report(inputs)
    assert set(report) == TOP_LEVEL
    assert report["token"]["http_status"] == 200
    assert set(report["spec_2_3"].values()) <= FINDING_STATUSES
    assert report["spec_2_3"]["1_token_and_header"] == "unknown"


def test_build_report_full_inputs_and_markdown() -> None:
    g = good_report()
    inputs = ReportInputs(
        "run-3",
        "complete",
        token=g["token"],
        header_convention=g["header_convention"],
        pagination=g["pagination"],
        ordering=g["ordering"],
        samples=g["samples"],
        categories=g["categories"],
        http=g["http"],
    )
    report = build_report(inputs)
    assert set(report) == TOP_LEVEL
    assert report["spec_2_3"]["2_pagination"] == "confirmed"
    assert "note" not in report["http"]
    md = render_markdown(report)
    assert md.startswith("# Contract report")
    assert "| 3 | History restrictions | unknown |" in md
    assert md.endswith("\n")


def test_spec_2_3_item_1_confirmed_for_raw_body_token() -> None:
    from garmin_archive.devtools.discovery.report import derive_spec_2_3

    report: dict[str, Any] = {
        "token": {"token_field": None, "token_field_source": "raw_body", "status": "confirmed"},
        "header_convention": {
            "scheme": "Bearer",
            "status": "confirmed",
            "probe_http_status": 200,
        },
    }
    assert derive_spec_2_3(report)["1_token_and_header"] == "confirmed"
