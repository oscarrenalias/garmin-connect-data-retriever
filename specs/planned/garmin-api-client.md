---
name: Garmin API client
id: spec-e4f45b59
description: null
dependencies: null
priority: high
complexity: null
status: planned
tags: []
scope:
  in: null
  out: null
feature_root_id: B-36752d02
---
# Garmin API client

## Objective

Add a small client for the Garmin Personal API (`src/garmin_archive/api.py`) and two CLI commands, `auth check` and `auth status`. Sync (next spec) uses this client to fetch everything it archives.

## Context

- This is a single-user tool that runs on a Raspberry Pi. Keep it simple and add **no new dependencies**.
- Credential rules are in `docs/SPEC.md` §4 (updated 2026-10-22). The application never stores the key or the token anywhere.
- Live contract facts are in `tests/fixtures/contract/contract-report.md`:
  - **Token:** `POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token` with header `Authorization: ApiKey <key>`. The response is HTTP 200, `text/plain`, and **the body is the bare token**. The token is valid for 24 h.
  - **Data calls:** use `Authorization: Bearer <token>`.
  - **Endpoints (SPEC §2.2):**
    - search `GET /activitylist-service/activities/search/activities`
    - summary `GET /activity-service/activity/{id}`
    - workouts `GET /activity-service/activity/{id}/workouts`
    - original file `GET /download-service/files/activity/{id}`
  - **Page-size errors:** search pages that are too large return HTTP 500. Sync handles this by shrinking the page size.

## Behaviour

1. **API key.** Read it from `GARMIN_API_KEY`, then `GARMIN_CONNECT_API_KEY`, in the process environment. If neither is set, read the same names from a `.env` file. The default is `./.env`, and `--env-file PATH` is a global CLI option. Parse simple `KEY=value` lines, with optional quotes and `#` comments. If no key is found, raise a clear error that names both variables. The key is never a CLI argument.
2. **Token.** Fetch one token lazily on first use and keep it in memory for the life of the client object. Never write it anywhere. There is no expiry tracking. If a data call returns 401, fetch a new token and retry that call once. A second 401 is an auth failure. If the token endpoint itself returns 401 or 403, the key is rejected: raise an auth failure immediately.
3. **Requests.**
   - One method per endpoint. Each returns the HTTP status, content type and **raw body bytes**. The client never parses or re-encodes bodies.
   - Search always sends `excludeChildren=false&sortBy=startLocal&sortOrder=asc`, plus `start` and `limit`, and nothing else.
   - A 404 is returned to the caller, not raised. 403 raises an authorization error. Other 4xx and 3xx raise a contract error.
   - Network errors, 5xx and 429 are retried with exponential backoff, up to 5 attempts. On 429, honour `Retry-After` in seconds, but if it is over 5 minutes, give up and raise a rate-limit error. The search method lets the caller lower the attempt count for a single call.
   - Never follow redirects. Use finite connect and read timeouts.
4. **Errors.** Add typed errors following the existing `GarminArchiveError` pattern in `models.py`, and map them in `output.exit_code_for`:
   - auth failure, missing key, authorization error: exit 3
   - transport error, rate limit, contract error: exit 4

   Error messages and details never contain the key, the token or response bodies.
5. **Testability.** The client accepts an injected `httpx.Client` and `sleep` function.
6. **CLI.**
   - `auth status` reports whether a key is configured and its source, as an env var name or the `.env` path. It makes no network call. It exits 0 even when no key is found. Commands that need the key, such as `auth check`, exit 3.
   - `auth check` gets a token and makes one `limit=1` search request, then reports success.
   - Both print the standard JSON envelope from `output.py`.

## Acceptance Criteria

Tests use `httpx.MockTransport` only. They never touch the network, and they never read the repository's real `.env`.

1. Gates pass: `uv run pytest`, `ruff check`, `ruff format --check`, `pyright`.
2. Key lookup:
   - `GARMIN_API_KEY` wins over `GARMIN_CONNECT_API_KEY`, and the environment wins over `.env`.
   - `--env-file` works.
   - A missing key gives exit 3 with a message naming both variables.
3. A plain-text token body is used as the Bearer token. Two data calls on one client make one token request.
4. A data-call 401 followed by 200 renews exactly once. Two 401s in a row give an auth failure. A 401 from the token endpoint fails without retrying.
5. Search sends exactly the fixed parameters above. The per-activity methods hit the documented paths. Bodies, including a ZIP, are returned byte-identical.
6. A persistent 500 makes exactly 5 attempts, then raises. The search attempt override works. `Retry-After: 2` is honoured, and `Retry-After: 600` raises a rate-limit error.
7. A 302 to another host is not followed, and that host never receives a request.
8. `auth status` and `auth check` print one JSON document and use the documented exit codes.
9. Across all tests, the fake key and token never appear in stdout, stderr, logs or exception text.

## Out of scope

- keyring
- key files written by the app
- `auth configure` / `auth clear`
- token expiry tracking or persistence
- archiving
- sync
