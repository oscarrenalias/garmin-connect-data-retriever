"""Per-activity resource fetching for the discovery tool (spec B4).

For each selected sample this fetches the summary, workouts and original-file resources, records
the per-sample report entry and returns the parsed JSON bodies for fixture writing. Retry
exhaustion is recorded as an `http.failures` entry and never aborts the run. `RunPartial` from
`DiscoveryHttp` propagates to the caller.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from garmin_archive.devtools.discovery.credentials import SecretStr
from garmin_archive.devtools.discovery.fileinfo import analyze_original_file
from garmin_archive.devtools.discovery.http import DiscoveryHttp, HttpResponse, RetriesExhausted
from garmin_archive.devtools.discovery.raw import RawRecorder
from garmin_archive.devtools.discovery.selection import SelectedSample
from garmin_archive.devtools.sanitize import SanitizeContext

SUMMARY_PATH = "/activity-service/activity/{id}"
WORKOUTS_PATH = "/activity-service/activity/{id}/workouts"
FILE_PATH = "/download-service/files/activity/{id}"


@dataclass
class SampleResult:
    """Outcome for one sample. `record` is the report `samples[]` entry."""

    sample: SelectedSample
    record: dict[str, Any]
    summary_json: Any = None
    workouts_json: Any = None
    manifest: dict[str, Any] | None = None


@dataclass
class SamplesResult:
    results: list[SampleResult] = field(default_factory=lambda: [])
    failures: list[dict[str, str]] = field(default_factory=lambda: [])


def _describe(response: HttpResponse) -> dict[str, Any]:
    values = response.headers.values
    return {
        "http_status": response.status,
        "content_type": values.get("content-type"),
        "byte_length": len(response.body),
        "etag_present": "etag" in values,
        "last_modified_present": "last-modified" in values,
        "headers": dict(sorted(values.items())),
    }


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _item_count(parsed: Any) -> int | None:
    if isinstance(parsed, list):
        return len(parsed)  # pyright: ignore[reportUnknownArgumentType]
    return None


def _fetch(
    http: DiscoveryHttp,
    recorder: RawRecorder,
    token: SecretStr,
    sample: SelectedSample,
    kind: str,
    template: str,
    failures: list[dict[str, str]],
) -> HttpResponse | None:
    path = template.format(id=sample.activity_id)
    response = http.get(path, headers={"Authorization": f"Bearer {token.get_secret_value()}"})
    if isinstance(response, RetriesExhausted):
        recorder.record_failure(f"{kind}-{sample.name}", "GET", path, None, response)
        failures.append({"resource": f"{kind}:{sample.name}", "error": response.error})
        return None
    recorder.record(f"{kind}-{sample.name}", "GET", path, None, response)
    return response


def fetch_sample(
    http: DiscoveryHttp,
    recorder: RawRecorder,
    token: SecretStr,
    sample: SelectedSample,
    ctx: SanitizeContext,
    failures: list[dict[str, str]],
) -> SampleResult:
    record: dict[str, Any] = {
        "name": sample.name,
        "category": sample.category,
        "type_key": sample.type_key,
    }
    result = SampleResult(sample=sample, record=record)

    summary = _fetch(http, recorder, token, sample, "summary", SUMMARY_PATH, failures)
    if summary is not None:
        record["summary"] = _describe(summary)
        if 200 <= summary.status < 300:
            result.summary_json = _parse_json(summary.body)

    workouts = _fetch(http, recorder, token, sample, "workouts", WORKOUTS_PATH, failures)
    if workouts is not None:
        entry = _describe(workouts)
        if 200 <= workouts.status < 300:
            result.workouts_json = _parse_json(workouts.body)
            entry["item_count"] = _item_count(result.workouts_json)
        record["workouts"] = entry

    file_response = _fetch(http, recorder, token, sample, "file", FILE_PATH, failures)
    if file_response is not None:
        entry = _describe(file_response)
        if 200 <= file_response.status < 300:
            result.manifest = analyze_original_file(file_response.body, ctx)
            entry["manifest"] = f"files/{sample.name}.manifest.json"
        record["original_file"] = entry
    return result


def fetch_samples(
    http: DiscoveryHttp,
    recorder: RawRecorder,
    token: SecretStr,
    samples: Iterable[SelectedSample],
    ctx: SanitizeContext,
) -> SamplesResult:
    """Fetch all resources for each sample in order. May raise `RunPartial` (429 stop)."""
    out = SamplesResult()
    for sample in samples:
        out.results.append(fetch_sample(http, recorder, token, sample, ctx, out.failures))
    return out


def sample_records(result: SamplesResult) -> list[Mapping[str, Any]]:
    return [r.record for r in result.results]


__all__ = [
    "SampleResult",
    "SamplesResult",
    "fetch_sample",
    "fetch_samples",
    "sample_records",
]
