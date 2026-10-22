---
name: Garmin Personal API HTTP adapter
id: spec-b3be46ed
description: "Documented-endpoints-only adapter: Bearer auth with single 401 renewal, retries/backoff/Retry-After, no redirects, size limits, raw bytes for archiving"
dependencies: null
priority: high
complexity: null
status: draft
tags:
- api
- http
scope:
  in: null
  out: null
feature_root_id: null
---
# Garmin Personal API HTTP adapter

## Objective

Implement `src/garmin_archive/api.py`: a small, documented-endpoints-only HTTP adapter for the four activity endpoints in `docs/SPEC.md` §2.2. It handles Bearer authentication via the `TokenManager`, renews once on 401, applies the retry/backoff/Retry-After rules in SPEC §4 and §6.2, enforces redirect and size safety, and returns raw response bytes plus allowlisted metadata for the archive writer. It does not parse or interpret Garmin payloads. That is sync's job.

Depends on "Credentials and token lifecycle" (`TokenManager`, `Secret`, the error classes `AuthFailedError`, `AuthorizationError`, `TransportError`, `ContractMismatchError`).

## Problems to Fix

1. Sync (next spec) must fetch search pages, summaries, workouts and original files. It needs uniform, testable behaviour for auth renewal, retries, rate limiting and errors, and must never need to know token details.
2. The live contract run (`tests/fixtures/contract/contract-report.md`) observed:
   - search pages with `limit=99` returned HTTP **500**, while 49 worked. The threshold depends on the data. Sync handles page-size reduction, so the adapter must let the caller limit attempts for a single request.
   - no rate-limit headers, ETag or Last-Modified
   - ZIP downloads with `Content-Type: application/x-zip-compressed`
3. SPEC §4 forbids forwarding credentials through redirects, unbounded renewal loops and retrying unchanged 400s.

## Changes

### 1. Types

- `ApiResponse` is a frozen dataclass:
  - `method: str` and `path: str`: the path without the query string.
  - `params: dict[str, str]`: the exact query params sent, stringified, with booleans as `true`/`false`.
  - `http_status: int`
  - `content_type: str | None`
  - `body: bytes`: empty for 404.
  - `etag: str | None` and `last_modified: str | None`
  - `elapsed_ms: int`: measured with the injected `monotonic`, covering the final attempt only.
- `RateLimitedError(GarminArchiveError)`:
  - `code="RATE_LIMITED"`, `retryable=True`, exit code 4 (add it to `output.exit_code_for`).
  - `details={"retry_after_s": float | None, "path": str}`.

### 2. `GarminApi`

```python
GarminApi(
    tokens: TokenManager,
    *,
    http: httpx.Client,
    clock: Clock,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,     # jitter source
    base_url: str = "https://personal.connectapi.garmin.com",
    min_interval_s: float = 0.25,         # pacing between requests
    max_attempts: int = 5,                # network errors / 5xx / 429, per request (SPEC §6.2)
    max_retry_after_s: float = 120.0,
    max_body_bytes: int = 256 * 1024 * 1024,
    monotonic: Callable[[], float] = time.monotonic,  # source for elapsed_ms
)
```

Endpoint methods. Activity IDs are digit strings; validate them and raise `ValueError` otherwise.

| Method | Request |
|---|---|
| `search_page(*, start: int, limit: int, start_date: date \| None = None, end_date: date \| None = None, max_attempts: int \| None = None) -> ApiResponse` | `GET /activitylist-service/activities/search/activities`, always with `start`, `limit`, `excludeChildren=false`, `sortBy=startLocal`, `sortOrder=asc`, plus `startDate`/`endDate` (`YYYY-MM-DD`) when given. No other filters ever. `max_attempts` overrides the instance value for this call. |
| `summary(activity_id) -> ApiResponse` | `GET /activity-service/activity/{id}` |
| `workouts(activity_id) -> ApiResponse` | `GET /activity-service/activity/{id}/workouts` |
| `original_file(activity_id) -> ApiResponse` | `GET /download-service/files/activity/{id}` |

### 3. Request algorithm (`_request`)

For each logical request:

1. **Pacing.** Before the **first** attempt of each logical request, sleep so that at least `min_interval_s` passes since the start of the previous logical request. Measure with the injected `monotonic`. Retries, Retry-After waits and the 401 re-send do not get extra pacing sleeps; their own waits are enough.
2. **Auth.** Send `Authorization: Bearer <tokens.get_token().reveal()>` and `Accept: */*`. Send no other custom headers or cookies. Configure `httpx.Client` with `follow_redirects=False`. `build_http_client()` from auth is the production default.
3. **Body read.** Stream the body. If the declared `Content-Length` or the bytes read exceed `max_body_bytes`, abort and raise `ContractMismatchError` (`details={"reason": "body_too_large"}`).
4. **Status handling:**

| Status | Behaviour |
|---|---|
| 2xx | Return `ApiResponse`. |
| 404 | Return `ApiResponse` with `http_status=404` and `body=b""`. The error body is discarded, not stored. Sync decides meaning (SPEC §5.2: a file 404 does not mean deletion). |
| 401 | First 401 for this logical request: `tokens.invalidate()`, then repeat once with a fresh token. This does not consume an attempt. A second 401 raises `AuthFailedError` (`details={"path": ...}`). |
| 403 | Raise `AuthorizationError` (`details={"path": ..., "resource": <"activity_search" \| "summary" \| "workouts" \| "original_file">}`). Do not refresh the token. |
| 429 | Parse `Retry-After` (integer/decimal seconds or HTTP-date relative to `clock.now()`). If it is present and ≤ `max_retry_after_s`, sleep that long and retry. No jitter is applied to a Retry-After wait. A value ≤ 0, or an HTTP-date in the past, means sleep 0. An unparseable header is treated as absent. If it is absent, use backoff (step 5). If it is > `max_retry_after_s`, or attempts are exhausted, raise `RateLimitedError`. |
| 5xx, network errors (`httpx.TransportError`, including timeouts) | Retry with backoff. When attempts are exhausted, raise `TransportError` (`details={"http_status": n \| None, "path": ...}`). |
| 3xx | Never followed. Raise `ContractMismatchError` (`details={"http_status": n, "redirect_host": <host only, or None when Location is missing or has no host>, "path": ...}`). |
| 400 and other 4xx | Raise `ContractMismatchError` (`details={"http_status": n, "path": ...}`) immediately. No retry (SPEC §6.2). |

5. **Backoff.** Before attempt k+1 (k ≥ 1), wait `min(30, 2**(k-1)) * (1 + rng.uniform(0, 0.25))` seconds. That gives 1, 2, 4, 8 before attempts 2–5, before jitter. Use the injected `sleep`. Attempts count every try that ends in 5xx, a network error or 429.
6. **Logging.** Log one record per attempt at DEBUG to `logging.getLogger("garmin_archive.api")`, containing **only** method, path (no query), status, attempt number and elapsed_ms. Never log params, headers, the token or bodies.

The adapter holds no database connection and never writes anything. Sync archives the returned `ApiResponse`.

### 4. Response metadata

Read `Content-Type`, `ETag`, `Last-Modified` and `Retry-After` from the response. All other headers are ignored and never surfaced.

## Files to Modify

| File | Change |
|---|---|
| `src/garmin_archive/api.py` | New |
| `src/garmin_archive/models.py` | `RateLimitedError` |
| `src/garmin_archive/output.py` | Exit 4 for `RateLimitedError` |
| `src/garmin_archive/__init__.py` | Re-export `GarminApi`, `ApiResponse` |
| `tests/test_api.py` | New |

## Acceptance Criteria

Use `httpx.MockTransport`, a fake `TokenManager` (or the real one over the same mock transport), a fixed clock, a recording fake `sleep`, and a seeded `rng`.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check` and `pyright`, with zero errors.
2. Request shape:
   - `search_page(start=98, limit=49)` sends exactly the query `start=98&limit=49&excludeChildren=false&sortBy=startLocal&sortOrder=asc`, with no other params.
   - With dates it adds `startDate`/`endDate` in ISO form.
   - The three per-activity methods hit the documented paths.
   - A non-digit activity ID raises `ValueError` without making a request.
   - The Bearer header carries the token from `TokenManager`.
3. 2xx: the returned `body` is byte-identical to the mock body (test with a real fixture file from `tests/fixtures/contract/summaries/` and an in-memory ZIP). `content_type`, `etag` and `last_modified` are populated when present and `None` otherwise.
4. 404 returns `http_status == 404` with an empty body and no retries.
5. 401 handling:
   - 401 then 200 makes exactly one `invalidate()` call and 2 requests, and succeeds.
   - 401, 401 raises `AuthFailedError` after exactly 2 requests.
   - A 401 after two 503s still gets its single renewal: attempts are counted independently.
6. 403 raises `AuthorizationError` with the correct `resource`, after 1 request and no `invalidate()`.
7. With `min_interval_s=0`, a persistent 500 makes exactly 5 requests, then raises `TransportError` with `details["http_status"] == 500`. The backoff sleeps equal the seeded-jitter values derived from 1, 2, 4 and 8.
8. `search_page(..., max_attempts=1)` with 500 raises after exactly 1 request and no backoff sleep.
9. 429 handling (with `min_interval_s=0`; the recorded sleep list must equal exactly the stated values):
   - `Retry-After: 3` then 200 gives a sleep of 3, then success.
   - `Retry-After: <HTTP-date 10 s ahead of clock>` gives a sleep of 10.
   - `Retry-After: 600` raises `RateLimitedError` (`retry_after_s == 600`) after 1 request.
   - A 429 with no header uses backoff.
   - `Retry-After: garbage` uses backoff. An HTTP-date in the past sleeps 0.
10. A 302 to another host is not followed. No request reaches that host, no `Authorization` header is ever sent to it, and `ContractMismatchError` includes only the host.
11. 400 raises `ContractMismatchError` after exactly 1 request.
12. A body larger than `max_body_bytes` (set small in the test) raises `ContractMismatchError` with reason `body_too_large`.
13. Pacing: with `min_interval_s=0.25` and a fake `monotonic` that does not advance, two consecutive successful calls record exactly one sleep, of 0.25, before the second. A retried call (one 503, then 200) adds a backoff sleep but no extra pacing sleep. `elapsed_ms` reflects the fake `monotonic` delta.
14. Hygiene: across all tests, the fake token never appears in `caplog` records (DEBUG), exception strings or details, or `repr(ApiResponse)`.

## Pending Decisions

- **Resolved:** 404 is returned, not raised, so sync can record `unavailable`/`not_found` coverage without exception-driven flow.
- **Resolved:** Adaptive page-size reduction lives in sync. The adapter only offers the per-call `max_attempts` override.
- **Resolved:** The default pacing is 0.25 s between requests. No rate limits were observed live, but this keeps load polite. It is configurable.
- **Out of scope:** payload parsing, archiving, sync orchestration, cancellation (sync checks cancellation between work units) and persistent token caching.
