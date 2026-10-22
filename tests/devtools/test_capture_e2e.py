"""End-to-end capture against a fake Garmin API (spec AC5, AC6, AC12)."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from typing import Any

import httpx
import pytest
from fake_api import (
    ACTIVITY_IDS,
    API_KEY,
    PAGE_SIZE,
    PROFILE_ID,
    TOKEN,
    FakeGarminApi,
)

from garmin_archive.devtools.discovery.__main__ import EXIT_OK, MainDeps, main
from garmin_archive.devtools.discovery.capture import (
    CaptureDeps,
    CaptureOptions,
    CaptureResult,
    run_capture,
)
from garmin_archive.devtools.discovery.credentials import SecretStr

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
EXPECTED_SAMPLES = [
    "multisport-1",
    "multisport-1-child-1",
    "multisport-1-child-2",
    "manual-1",
    "pool_swim-1",
    "running-1",
    "other-1",
]


class Runner:
    """Holds the injected fakes and the directories for one run."""

    def __init__(self, tmp_path: Path, api: FakeGarminApi | None = None) -> None:
        self.api = api or FakeGarminApi()
        self.sleeps: list[float] = []
        self.fixtures = tmp_path / "fixtures"
        self.raw = tmp_path / "raw"
        self.progress: list[str] = []

    def capture(self, **overrides: Any) -> CaptureResult:
        options = CaptureOptions(
            fixtures_dir=self.fixtures,
            raw_dir=self.raw,
            delay=0,
            page_size=PAGE_SIZE,
            **overrides,
        )
        deps = CaptureDeps(
            api_key=SecretStr(API_KEY),
            sleep=self.sleeps.append,
            transport=self.api.transport(),
            clock=lambda: FIXED_NOW,
            progress=self.progress.append,
            rng=Random(7),
        )
        return run_capture(options, deps)

    def report(self) -> dict[str, Any]:
        return json.loads((self.fixtures / "contract-report.json").read_text(encoding="utf-8"))


def _all_files(*roots: Path) -> list[Path]:
    return [p for root in roots for p in sorted(root.rglob("*")) if p.is_file()]


def _assert_absent(needles: list[bytes], files: list[Path], extra: dict[str, str]) -> None:
    for path in files:
        data = path.read_bytes()
        for needle in needles:
            assert needle not in data, f"{needle!r} found in {path}"
    for label, text in extra.items():
        for needle in needles:
            assert needle.decode() not in text, f"{needle!r} found in {label}"


def _check_run(runner: Runner, stdout: str = "", stderr: str = "") -> None:
    report = runner.report()
    assert report["outcome"] == "complete"
    assert report["token"]["token_field"] == "accessToken"
    assert report["token"]["token_field_source"] == "auto"
    assert report["token"]["expiry_fields"]["expiresIn"] == 3600
    assert report["page_size_cap"]["probes"][0]["returned"] == 100
    assert report["page_size_cap"]["probes"][1]["returned"] == 100
    assert report["pagination"]["duplicate_id_count"] == 1
    assert report["pagination"]["inventory_status"] == "complete"
    assert len(report["pagination"]["pages"]) == 4
    assert report["pagination"]["pages"][-1]["item_count"] == 0
    assert report["http"]["status_429_count"] == 1
    assert report["ordering"]["stable"] is True
    assert report["leak_check"]["status"] == "passed"
    assert report["leak_check"]["failed_fixtures"] == []

    samples = {s["name"]: s for s in report["samples"]}
    assert sorted(samples) == sorted(EXPECTED_SAMPLES)
    assert samples["manual-1"]["original_file"]["http_status"] == 404
    assert "manifest" not in samples["manual-1"]["original_file"]
    assert samples["running-1"]["original_file"]["http_status"] == 200
    assert samples["running-1"]["original_file"]["manifest"] == "files/running-1.manifest.json"
    assert samples["pool_swim-1"]["workouts"]["item_count"] == 2

    # B6 fixture files exist for each sample (manual has no original file, so no manifest).
    for name in EXPECTED_SAMPLES:
        for folder in ("summaries", "workouts", "search-items"):
            assert (runner.fixtures / folder / f"{name}.json").is_file(), (folder, name)
        manifest = runner.fixtures / "files" / f"{name}.manifest.json"
        assert manifest.is_file() == (name != "manual-1"), name
    for name in ("redaction-log.json", "contract-report.json", "contract-report.md"):
        assert (runner.fixtures / name).is_file()

    # AC6: no fake secret in any written file, progress, stdout or stderr.
    secrets = [API_KEY.encode(), TOKEN.encode()]
    files = _all_files(runner.raw, runner.fixtures)
    assert any(p.parent.name == "raw" for p in files)
    _assert_absent(
        secrets,
        files,
        {"stdout": stdout, "stderr": stderr, "progress": "\n".join(runner.progress)},
    )
    # AC6: no raw activity or profile ID anywhere under the fixtures directory.
    ids = [str(i).encode() for i in (*ACTIVITY_IDS, PROFILE_ID)]
    _assert_absent(ids, _all_files(runner.fixtures), {})
    # The raw directory keeps bodies as received, so the IDs are expected there.
    assert any(str(ACTIVITY_IDS[0]).encode() in p.read_bytes() for p in _all_files(runner.raw))


def test_run_capture_end_to_end(tmp_path: Path) -> None:
    runner = Runner(tmp_path)
    result = runner.capture()

    assert result.outcome == "complete"
    assert result.leak_failures == []
    assert result.action_hint is None
    assert result.run_id == "20260102T030405Z"
    assert result.fixture_count > 0
    assert result.report_path == runner.fixtures / "contract-report.json"
    _check_run(runner)
    # The one 429 had Retry-After: 2, so the injected sleep was called with 2 (delay is 0).
    assert runner.sleeps == [2.0]
    assert runner.report()["http"]["retry_after_seen"] is True
    assert "x-ratelimit-limit" in runner.report()["http"]["rate_limit_headers_seen"]


def test_multisport_children_and_pool_swim_selected(tmp_path: Path) -> None:
    runner = Runner(tmp_path)
    runner.capture()
    samples = {s["name"]: s for s in runner.report()["samples"]}
    assert samples["multisport-1-child-1"]["category"] == "multisport"
    assert samples["multisport-1-child-2"]["category"] == "multisport"
    assert samples["pool_swim-1"]["type_key"] == "lap_swimming"
    assert (runner.fixtures / "summaries" / "multisport-1-child-2.json").is_file()
    assert not (runner.fixtures / "summaries" / "multisport-1-child-3.json").exists()


def test_every_request_is_authorized_and_nothing_else_is_hit(tmp_path: Path) -> None:
    runner = Runner(tmp_path)
    runner.capture()
    api = runner.api
    assert {r.url.host for r in api.requests} == {"personal.connectapi.garmin.com"}
    assert api.requests[0].method == "POST"
    assert all(r.method == "GET" for r in api.requests[1:])


def test_ordering_probe_reports_unstable_when_first_page_reordered(tmp_path: Path) -> None:
    runner = Runner(tmp_path, FakeGarminApi(reorder_first_page=True))
    result = runner.capture()
    assert result.outcome == "complete"
    assert runner.report()["ordering"]["stable"] is False


def test_main_entrypoint_end_to_end(tmp_path: Path) -> None:
    runner = Runner(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    deps = MainDeps(
        environ={"GARMIN_API_KEY": API_KEY},
        cwd=tmp_path,
        sleep=runner.sleeps.append,
        clock=lambda: FIXED_NOW,
        transport=runner.api.transport(),
        stdout=stdout,
        stderr=stderr,
    )
    code = main(
        [
            "capture",
            "--fixtures-dir",
            str(runner.fixtures),
            "--page-size",
            str(PAGE_SIZE),
            "--delay",
            "0",
        ],
        deps,
    )
    assert code == EXIT_OK
    assert "outcome: complete" in stdout.getvalue()
    assert "fixtures written:" in stdout.getvalue()
    assert "fetching sample" in stderr.getvalue()
    assert runner.sleeps == [2.0]
    # No raw dir is passed on the command line: it defaults to .local/discovery under the cwd.
    runner.raw = tmp_path / ".local" / "discovery"
    assert (runner.raw / "20260102T030405Z" / "raw").is_dir()
    _check_run(runner, stdout.getvalue(), stderr.getvalue())


def test_main_reads_key_from_env_file_only_and_never_prints_it(tmp_path: Path) -> None:
    runner = Runner(tmp_path)
    env_file = tmp_path / "custom.env"
    env_file.write_text(f'# comment\nGARMIN_API_KEY="{API_KEY}"\n', encoding="utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    deps = MainDeps(
        environ={},
        cwd=tmp_path,
        sleep=runner.sleeps.append,
        clock=lambda: FIXED_NOW,
        transport=runner.api.transport(),
        stdout=stdout,
        stderr=stderr,
    )
    code = main(
        ["capture", "--env-file", str(env_file), "--fixtures-dir", str(runner.fixtures),
         "--page-size", str(PAGE_SIZE), "--delay", "0"],
        deps,
    )  # fmt: skip
    assert code == EXIT_OK
    for text in (stdout.getvalue(), stderr.getvalue()):
        assert API_KEY not in text
        assert TOKEN not in text
    files = _all_files(tmp_path / ".local", runner.fixtures)
    _assert_absent([API_KEY.encode(), TOKEN.encode()], files, {})


def test_persistent_server_error_never_leaks_into_outputs(tmp_path: Path) -> None:
    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, headers={"X-Echo": request.headers.get("authorization", "")})

    runner = Runner(tmp_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    deps = MainDeps(
        environ={"GARMIN_API_KEY": API_KEY},
        cwd=tmp_path,
        sleep=runner.sleeps.append,
        clock=lambda: FIXED_NOW,
        transport=httpx.MockTransport(always_500),
        stdout=stdout,
        stderr=stderr,
    )
    code = main(["capture", "--fixtures-dir", str(runner.fixtures), "--delay", "0"], deps)
    assert code == 2
    assert "outcome: stopped" in stdout.getvalue()
    assert runner.sleeps == [1.0, 2.0, 4.0]
    files = _all_files(tmp_path / ".local", runner.fixtures)
    _assert_absent(
        [API_KEY.encode()], files, {"stdout": stdout.getvalue(), "stderr": stderr.getvalue()}
    )


def test_python_dash_m_entrypoint_without_key_exits_3_offline(tmp_path: Path) -> None:
    """The real `python -m` entrypoint; fails before any HTTP client is created."""
    env = {k: v for k, v in __import__("os").environ.items() if "GARMIN" not in k}
    proc = subprocess.run(
        [sys.executable, "-m", "garmin_archive.devtools.discovery", "capture"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 3
    assert "GARMIN_API_KEY" in proc.stderr
    assert "GARMIN_CONNECT_API_KEY" in proc.stderr
    assert not (tmp_path / "tests").exists()
    assert re.search(r"outcome", proc.stdout) is None


def test_isolation_removes_key_variables_and_runs_in_tmp(tmp_path: Path) -> None:
    import os

    assert "GARMIN_API_KEY" not in os.environ
    assert "GARMIN_CONNECT_API_KEY" not in os.environ
    assert Path.cwd().resolve() == tmp_path.resolve()


@pytest.mark.parametrize("name", ["summaries", "workouts", "search-items"])
def test_fixture_documents_are_valid_json(tmp_path: Path, name: str) -> None:
    runner = Runner(tmp_path)
    runner.capture()
    for path in (runner.fixtures / name).glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


def _run_main(
    runner: Runner, tmp_path: Path, transport: httpx.BaseTransport, *extra: str
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    deps = MainDeps(
        environ={"GARMIN_API_KEY": API_KEY},
        cwd=tmp_path,
        sleep=runner.sleeps.append,
        clock=lambda: FIXED_NOW,
        transport=transport,
        stdout=stdout,
        stderr=stderr,
    )
    argv = ["capture", "--fixtures-dir", str(runner.fixtures), "--delay", "0", *extra]
    return main(argv, deps), stdout.getvalue(), stderr.getvalue()


def test_ambiguous_token_field_exits_2_and_writes_report(tmp_path: Path) -> None:
    """AC7: two plausible token fields and no --token-field give needs_token_field, exit 2."""

    def ambiguous(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"accessToken": "tok-one-xxxx", "idToken": "tok-two-xxxx"})

    runner = Runner(tmp_path)
    code, stdout, stderr = _run_main(runner, tmp_path, httpx.MockTransport(ambiguous))
    assert code == 2
    assert "outcome: needs_token_field" in stdout
    assert "--token-field" in stdout
    report = runner.report()
    assert report["outcome"] == "needs_token_field"
    assert report["samples"] == []
    _assert_absent([b"tok-one-xxxx", b"tok-two-xxxx"], _all_files(runner.fixtures), {
        "stdout": stdout,
        "stderr": stderr,
    })  # fmt: skip


def test_max_pages_reached_is_partial_exit_2_and_sampling_still_runs(tmp_path: Path) -> None:
    """AC8: stopping inventory at --max-pages is partial/exit 2; samples are still captured."""
    runner = Runner(tmp_path)
    code, stdout, _ = _run_main(
        runner, tmp_path, runner.api.transport(), "--page-size", str(PAGE_SIZE), "--max-pages", "1"
    )
    assert code == 2
    assert "outcome: partial" in stdout
    report = runner.report()
    assert report["outcome"] == "partial"
    assert report["pagination"]["inventory_status"] != "complete"
    assert len(report["pagination"]["pages"]) == 1
    assert report["samples"], "sampling must still run on the partial inventory"
    assert any((runner.fixtures / "summaries").glob("*.json"))
    # Only activities from page 1 can have been sampled.
    summary_paths = [p for p in runner.api.paths() if p.startswith("/activity-service/activity/")]
    fetched = {int(p.split("/")[3]) for p in summary_paths}
    assert fetched <= {ACTIVITY_IDS[0], ACTIVITY_IDS[1], ACTIVITY_IDS[2]}


def test_retry_after_300_gives_partial_run_exit_2(tmp_path: Path) -> None:
    """AC9: a 429 with Retry-After 300 stops the run as partial without sleeping."""
    api = FakeGarminApi()

    def throttled(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/workouts"):
            return httpx.Response(429, headers={"Retry-After": "300"})
        return api(request)

    runner = Runner(tmp_path, api)
    code, stdout, _ = _run_main(
        runner, tmp_path, httpx.MockTransport(throttled), "--page-size", str(PAGE_SIZE)
    )
    assert code == 2
    assert "outcome: partial" in stdout
    assert runner.sleeps == []
    report = runner.report()
    assert report["outcome"] == "partial"
    assert report["http"]["status_429_count"] == 1
    assert report["http"]["retry_after_seen"] is True


def test_leak_gate_skips_file_records_failure_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC11: a planted original value skips the file, fills failed_fixtures, exits 1."""
    from garmin_archive.devtools.discovery import capture as capture_module

    real_fetch = capture_module.fetch_sample
    planted = "Secret Name 5100000001"  # an original that sanitize redacts in the summary

    def leaky_fetch(*args: Any, **kwargs: Any) -> Any:
        result = real_fetch(*args, **kwargs)
        if result.sample.name == "running-1" and result.manifest is not None:
            result.manifest = {**result.manifest, "planted": planted}
        return result

    monkeypatch.setattr(capture_module, "fetch_sample", leaky_fetch)
    runner = Runner(tmp_path)
    code, stdout, stderr = _run_main(
        runner, tmp_path, runner.api.transport(), "--page-size", str(PAGE_SIZE)
    )
    assert code == 1
    assert "leak check failed for 1 fixture(s)" in stderr
    assert not (runner.fixtures / "files" / "running-1.manifest.json").exists()
    # Other fixtures are still written; the run continues.
    assert (runner.fixtures / "summaries" / "running-1.json").is_file()
    assert (runner.fixtures / "files" / "pool_swim-1.manifest.json").is_file()
    report = runner.report()
    assert report["leak_check"]["status"] == "failed"
    failed = report["leak_check"]["failed_fixtures"]
    assert [f["file"] for f in failed] == ["files/running-1.manifest.json"]
    assert failed[0]["json_path"] == "$.planted"
    # The matched value is never recorded anywhere.
    _assert_absent([planted.encode()], _all_files(runner.fixtures), {"stdout": stdout})
