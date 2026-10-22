---
name: Credentials and token lifecycle
id: spec-02012456
description: "API key providers (explicit/env/keyring/file), secure key storage, in-memory token manager for the plain-text JWT token, check_auth and auth CLI commands"
dependencies: null
priority: high
complexity: null
status: draft
tags:
- auth
- security
- cli
scope:
  in: null
  out: null
feature_root_id: null
---
# Credentials and token lifecycle

## Objective

Implement `src/garmin_archive/auth.py`, which covers:
- Personal API key lookup
- secure key storage for `auth configure`
- access-token acquisition and caching, following `docs/SPEC.md` §2.1 and §4
- the `auth configure | status | check | clear` CLI commands

The token contract was verified against the live API and recorded in `tests/fixtures/contract/contract-report.md`.

## Problems to Fix

1. Sync and the HTTP adapter (later specs) need a token source that never leaks secrets, refreshes before expiry, and can be invalidated after a 401. No production code does this. The devtools discovery tool has its own throwaway version, which must not be reused.
2. Users need a safe way to provide the key: environment variable, OS keyring, or an owner-only key file. The key must never be passed as a command-line argument (SPEC §4).
3. These contract facts are now confirmed and must be implemented exactly:
   - `POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token` with header `Authorization: ApiKey <key>`.
   - It returns HTTP 200 with `Content-Type: text/plain` and **the bare token as the whole body**. The body is not JSON and has no surrounding whitespace.
   - The token is a 3-segment JWT-like string with `exp` and `iat` claims, and `exp - iat = 86400` s.
   - Data calls accept `Authorization: Bearer <token>`.

## Changes

### 1. Secrets and errors (`models.py`)

- `Secret` is a final class wrapping a `str`. `__repr__` and `__str__` return `Secret('**********')`. `reveal() -> str` returns the value. Instances are not hashable and do not compare equal to plain strings. `Secret` and `reveal()` are the only way secret values move between modules.
- New error classes. Add each to `output.exit_code_for`:

| Class | code | retryable | exit |
|---|---|---|---|
| `CredentialsNotFoundError` | `CREDENTIALS_NOT_FOUND` | no | 3 |
| `InsecureKeyFileError` | `INSECURE_KEY_FILE` | no | 3 |
| `AuthFailedError` | `AUTH_FAILED` | no | 3 |
| `AuthorizationError` | `FORBIDDEN` | no | 3 |
| `TransportError` | `TRANSPORT_ERROR` | yes | 4 |
| `ContractMismatchError` | `CONTRACT_MISMATCH` | no | 4 |

No message or `details` of these errors may contain the key, the token or any response body.

### 2. Credential providers (`auth.py`)

- `CredentialProvider` is a protocol with:
  - `get_api_key() -> Secret | None`
  - `source -> str`: a non-secret label such as `env:GARMIN_API_KEY`, `keyring` or `file:<path>`
- Built-in providers:
  1. `EnvCredentialProvider(environ)`: reads `GARMIN_API_KEY`, then `GARMIN_CONNECT_API_KEY`. Empty values count as absent. The source label names the variable that was used.
  2. `KeyringCredentialProvider(service="garmin-archive", username="personal-api-key")`:
     - Uses the `keyring` package, imported lazily.
     - If `keyring` is not installed, or its backend raises (for example a headless Raspberry Pi with no backend), it returns `None`. It never raises.
     - `keyring` becomes an optional dependency extra: `garmin-archive[keyring]`.
  3. `KeyFileCredentialProvider(path)`:
     - The default path is `Path(platformdirs.user_config_dir("garmin-archive", appauthor=False)) / "api-key"`, overridable with `GARMIN_API_KEY_FILE`.
     - The file holds the key on its first line. Trailing whitespace and newlines are stripped. An empty file counts as absent.
     - On POSIX, raise `InsecureKeyFileError` if the mode grants any group or other permission (`mode & 0o077 != 0`). `details={"path": str, "mode": "0o644"}`.
- `ChainCredentialProvider(providers)` returns the first non-`None` key. Its `source` is the source of the provider that answered, or `none`.
- `default_credential_provider(environ, *, explicit: CredentialProvider | None = None)` builds the chain in this order: explicit provider if given → env → keyring → key file.
- `.env` files are **not** read by the production library (SPEC §4 does not list them). Only the devtools discovery tool reads `.env`.

### 3. Storing a key (`auth.py`)

- `store_api_key(key: Secret, *, target: Literal["keyring", "file"], path: Path | None = None) -> str` returns the source label.
- `keyring` target: `keyring.set_password(...)`. If keyring is unavailable, raise `CredentialsNotFoundError` with `details={"reason": "keyring_unavailable"}`.
- `file` target: write atomically:
  1. Create a temp file in the same directory with `0o600`.
  2. Write the key and a newline, then `fsync`.
  3. `os.replace` it onto the target.
  4. Create parent directories with `0o700` on POSIX. On Windows, skip mode handling.
- `remove_stored_key(...)` is **out of scope** (SPEC §4: key revocation happens in Garmin Connect).

### 4. Token manager (`auth.py`)

`TokenManager(provider: CredentialProvider, *, http: httpx.Client, clock: Clock, sleep: Callable[[float], None] = time.sleep, refresh_margin_s: int = 60, fallback_lifetime_s: int = 3600)`

- `http` must be configured with `follow_redirects=False` and finite timeouts. `build_http_client()` returns the production default: connect 10 s, read 60 s.
- `get_token() -> Secret`:
  1. Read the key from the provider. If it is missing, raise `CredentialsNotFoundError` naming the supported sources.
  2. Compute the credential identity in memory as `sha256(key)`. It is never persisted or logged.
  3. If there is a cached token for the same identity and `now < expires_at - refresh_margin_s`, return it.
  4. Otherwise, `POST` the token URL with `Authorization: ApiKey <key>` and no body.
- Token response handling:
  - **200:** strip the body. It must be non-empty and match `^[A-Za-z0-9_.-]+$`; otherwise raise `ContractMismatchError` (`details={"reason": "unexpected_token_format", "content_type": ...}`). The token is the stripped body.
  - **Expiry:** if the token has exactly 3 dot-separated segments and the middle segment base64url-decodes to a JSON object with an integer `exp`, then `expires_at = exp` (UTC). Do not verify the signature, and do not record any claim. Otherwise, or on any decode error, `expires_at = now + fallback_lifetime_s`. The token is not assumed to be a JWT.
  - If `exp` is already in the past, minus the margin, treat the token as expiring immediately: use it once, and refresh on the next call.
  - **401 or 403** from the token endpoint: raise `AuthFailedError` (`details={"http_status": n}`). The key is invalid, expired or revoked. **Do not retry.**
  - **Network error or 5xx:** retry up to 3 times (4 attempts in total), sleeping 1, 2 and 4 seconds through the injected `sleep`. Then raise `TransportError` (`details={"http_status": n | None}`).
  - **Any other status:** raise `ContractMismatchError` (`details={"http_status": n}`).
  - **3xx:** never followed. Raise `ContractMismatchError` (`details={"http_status": n, "redirect_host": <host only>}`).
- `invalidate() -> None` drops the cached token. The HTTP adapter (next spec) calls it after a data-call 401 and then calls `get_token()` once more.
- Key rotation: if the provider returns a key with a different identity, the cached token is discarded and a new one is requested. Keys are re-read on every `get_token()` call, which is cheap for env and file providers.
- `token_info() -> TokenInfo | None` returns `TokenInfo(expires_at_utc: str, lifetime_s: int | None, expiry_source: Literal["jwt_exp", "fallback"], credential_source: str)` for the cached token, without the token.
- The token cache is **in-memory only**. Optional persistent caching (SPEC §4) is out of scope.

### 5. Library API (`client.py`)

- `ArchiveClient.__init__` gains these keyword-only parameters:
  - `credential_provider: CredentialProvider | None = None`, used as the explicit first provider in the chain
  - `http: httpx.Client | None = None`
  - `sleep: Callable[[float], None] | None = None`
- `check_auth() -> AuthCheckResult(credential_source: str, token_expires_at_utc: str, token_lifetime_s: int | None, expiry_source: str, read_check_http_status: int)`. It:
  1. Acquires a token.
  2. Performs one minimal read: `GET /activitylist-service/activities/search/activities?start=0&limit=1` with `Authorization: Bearer <token>`. It does not parse or store the body.
  3. On a 401, calls `invalidate()`, gets a new token and retries once. A second 401 raises `AuthFailedError`.
  4. On a 403, raises `AuthorizationError` (`details={"resource": "activity_search"}`).

  `check_auth` does not touch the database.
- `auth_status() -> AuthStatus(configured: bool, credential_source: str, key_file_path: str, key_file_insecure: bool, keyring_available: bool)`. It is local only: no network, no token. If the key file has insecure permissions, it sets `key_file_insecure=true` instead of raising.

### 6. CLI (`cli.py`, via `output.py`)

Add an `auth` Typer sub-app. Each command emits exactly one JSON document on stdout using the existing envelope and exit codes, and contains no auth logic itself.

- `auth status` → `success(asdict(AuthStatus))`.
- `auth check` → `success(asdict(AuthCheckResult))`, or an error envelope with exit 3 or 4.
- `auth configure [--store keyring|file] [--key-file PATH] [--from-file PATH | --stdin]`:
  - The default `--store` is `keyring` if a keyring backend is available, otherwise `file`.
  - Without `--from-file` or `--stdin`, it prompts with hidden input (`typer.prompt(..., hide_input=True)`). If stdin is not a TTY and no flag is given, it fails with `INVALID_ARGUMENT`.
  - With `--from-file PATH`, it reads the first line of that file.
  - The key is never echoed.
  - Output: `success({"stored": true, "credential_source": <label>})`.
  - No option takes the key value itself.
- `auth clear` → `success({"cleared": false, "reason": "no_persistent_token_cache"})`, exit 0. Tokens are only ever held in memory. This documents SPEC §4's command until persistent caching exists.

### 7. Clarifications (normative; these override anything above that is less specific)

1. **CLI test seam.** Signature: `main(argv: Sequence[str] | None = None, *, client_factory: Callable[[ArchiveConfig], ArchiveClient] | None = None) -> int`. Commands build their client through the factory, which defaults to `ArchiveClient`. CLI tests pass a factory that returns a client built with a `MockTransport` `httpx.Client` and a fake credential provider. No global monkeypatching.
2. **Keyring seam.**
   - `KeyringCredentialProvider` and `store_api_key` accept `keyring_module: ModuleType | None = None`. When it is `None`, import `keyring` lazily, and treat an `ImportError` as unavailable. Tests pass a fake module object.
   - "Keyring available" means: the module imports, and `keyring.get_keyring()` is not an instance of `keyring.backends.fail.Keyring` and its class name is not `NullKeyring`. Any exception while checking counts as unavailable.
   - `auth status` may call `get_password` to determine `configured`. On macOS this may show a Keychain prompt for items created by another app. This is accepted and documented in the help text.
   - A `keyring.errors.KeyringError` (or any exception) from `set_password` raises `CredentialsNotFoundError` with `details={"reason": "keyring_write_failed"}`. The exception text is never included.
3. **Redirects are enforced, not assumed.** Every token POST and read GET passes `follow_redirects=False` explicitly per request, so an injected client configured to follow redirects still never follows one.
4. **`check_auth` read errors:**
   - Reads use the base URL constant `GARMIN_API_BASE = "https://personal.connectapi.garmin.com"`, defined in `auth.py` and reused by the adapter spec.
   - A 5xx or a network error raises `TransportError` (`details={"http_status": n | None}`) **without retry** here. 429 raises `TransportError` with `details={"http_status": 429}`. The adapter spec adds full retry and rate-limit handling for data calls.
   - A 3xx or any other unexpected status raises `ContractMismatchError` (`details={"http_status": n}`).
5. **Formats:**
   - `TokenInfo.expires_at_utc = utc_rfc3339(datetime.fromtimestamp(exp, UTC))`, or `utc_rfc3339(now + fallback)`.
   - `lifetime_s = exp - iat` when both are integers, otherwise `None` (always `None` for `fallback`).
   - `AuthStatus`: with no key found, `configured=False` and `credential_source="none"`. With an insecure key file and no other source, `configured=False`, `credential_source="none"` and `key_file_insecure=True`.
6. **Lifecycle.** `ArchiveClient` creates `build_http_client()` lazily on the first network call when `http` is `None`, and closes it in `close()`. An injected `http` client is never closed by the library. `TokenManager` is created lazily on the first network call and lives for the client's lifetime, so tokens are reused across calls on one client.
7. **`auth configure` input:**
   - Non-TTY stdin with no source flag makes the CLI raise a Typer/Click `UsageError`, which gives `INVALID_ARGUMENT` and exit 2.
   - `--stdin` reads the first line and strips trailing whitespace. `--from-file` does the same.
   - An empty key gives `INVALID_ARGUMENT`.
   - `--from-file` and `--stdin` together give `INVALID_ARGUMENT`.
8. **Key-file write order:**
   1. Create missing parent directories (`0o700` on POSIX). Existing directories are not modified.
   2. Write a temp file in the target directory with `0o600`.
   3. Write, then `fsync`.
   4. `os.replace` onto the target.
   5. On failure, remove the temp file.
9. **Token format.** After stripping *leading and trailing* whitespace, the token must match `^[A-Za-z0-9_.-]+$`. Internal whitespace, or a JSON object, is a `ContractMismatchError`.
10. **Re-exports** from `garmin_archive/__init__.py`:
    - classes: `Secret`, `CredentialProvider`, `EnvCredentialProvider`, `KeyringCredentialProvider`, `KeyFileCredentialProvider`, `ChainCredentialProvider`, `TokenManager`, `TokenInfo`, `AuthCheckResult`, `AuthStatus`
    - functions: `store_api_key`, `build_http_client`
    - all new error classes

## Files to Modify

| File | Change |
|---|---|
| `pyproject.toml`, `uv.lock` | Optional extra `keyring = ["keyring"]` |
| `src/garmin_archive/models.py` | `Secret` and the new errors |
| `src/garmin_archive/output.py` | Exit-code mapping for the new errors |
| `src/garmin_archive/auth.py` | New |
| `src/garmin_archive/client.py` | New constructor parameters, `check_auth`, `auth_status` |
| `src/garmin_archive/__init__.py` | Re-exports |
| `src/garmin_archive/cli.py` | `auth` sub-app |
| `tests/test_auth.py`, `tests/test_cli_auth.py` | New |

Do not modify `src/garmin_archive/devtools/**`, `tests/fixtures/**` or the storage modules.

## Acceptance Criteria

All HTTP tests use `httpx.MockTransport`. Keyring is replaced by a fake module passed through the `keyring_module` seam. CLI tests use `client_factory`. The clock and sleep are fixed fakes. The conftest isolation already removes key environment variables and runs each test in `tmp_path`.

1. The gates pass: `uv run pytest`, `ruff check`, `ruff format --check` and `pyright`, with zero errors.
2. Provider chain:
   - Explicit beats env, env beats keyring, keyring beats file.
   - `GARMIN_API_KEY` beats `GARMIN_CONNECT_API_KEY`.
   - A missing `keyring` module or a raising backend falls through to the file.
   - No key anywhere raises `CredentialsNotFoundError`, exit 3 from the CLI.
3. Key file:
   - Mode `0o600` is read.
   - Mode `0o644` raises `InsecureKeyFileError` on POSIX (skipped on Windows), and `auth status` reports `key_file_insecure: true` with exit 0.
   - `store_api_key(target="file")` creates mode `0o600` and leaves no temp file behind.
4. Token acquisition:
   - A text/plain JWT-like body whose `exp = iat + 86400` gives `token_info().expires_at_utc` equal to `exp` and `expiry_source == "jwt_exp"`.
   - An opaque token (one segment) uses `now + 3600` and `"fallback"`.
   - A body with spaces or a JSON object raises `ContractMismatchError`.
5. Caching and refresh:
   - Two `get_token()` calls inside the validity window make exactly one POST.
   - At `expires_at - 59 s` a new POST happens. At `expires_at - 61 s` it does not.
   - After `invalidate()`, the next call POSTs.
6. Rotation: the provider returns key A, then key B. The second `get_token()` POSTs with key B, and the cached token for A is discarded.
7. Token endpoint errors:
   - 401 and 403 raise `AuthFailedError` with exactly one POST and no sleeps.
   - A persistent 503 makes exactly 4 POSTs with sleeps [1, 2, 4], then raises `TransportError`.
   - A 302 is not followed: no request reaches another host, and `ContractMismatchError` is raised.
8. `check_auth`:
   - 200 returns the result.
   - 401 then 200 on the read triggers exactly one token refresh and succeeds.
   - 401 twice raises `AuthFailedError`, with exactly 2 token POSTs and 2 reads.
   - 403 raises `AuthorizationError` with no token refresh.
9. CLI:
   - `auth status`, `auth check` (with an injected fake transport), `auth configure --store file --from-file <path>` and `auth clear` each emit one JSON document with the documented shape and exit code.
   - `auth configure` with non-TTY stdin and no source flag gives exit 2 and `INVALID_ARGUMENT`.
   - Typer's help for `auth configure` lists no option that accepts the key value.
10. Secret hygiene:
    - In every test above, the fake key and fake token strings never appear in captured stdout or stderr, in log records (`caplog` at DEBUG), in any exception's `str()`, `message` or `details`, or in `repr()` of any public result object.
    - `repr(Secret("x"))` does not contain `x`.

11. Clarification coverage:
    - An injected `httpx.Client(follow_redirects=True)` still does not follow a 302, on both the token endpoint and the read.
    - A `check_auth` read 503 raises `TransportError` after exactly 1 read. A read 429 raises `TransportError` with status 429. A read 418 raises `ContractMismatchError`.
    - `keyring_available` is false for a fake fail backend and true for a fake working backend.
    - A fake keyring whose `set_password` raises maps to `keyring_write_failed`, without the exception text.
    - `ArchiveClient.close()` closes the client it created, and does not close an injected one.
    - Two `check_auth()` calls on one client inside the validity window make one token POST.
    - `expires_at_utc` equals `utc_rfc3339` of `exp`, and `lifetime_s == 86400`.
    - `--from-file` together with `--stdin` gives exit 2.

## Pending Decisions

- **Resolved:** `GARMIN_CONNECT_API_KEY` is accepted as a fallback env var because the developer's environment uses it. `GARMIN_API_KEY` stays the documented name.
- **Resolved:** The production library does not read `.env` files.
- **Resolved:** Tokens are cached in memory only, so `auth clear` is a documented no-op. Persistent token caching is deferred.
- **Resolved:** The 401-renew-once rule for data calls is implemented in `check_auth` here and in the HTTP adapter (next spec), on top of `TokenManager.invalidate()`.
- **Resolved:** `ContractMismatchError` exits with 4. SPEC §9 groups it with transport failures, because the upstream contract deviated.
- **Resolved:** A 403 from the *token* endpoint is `AUTH_FAILED`, because the key was rejected. A 403 on a *data* call is `FORBIDDEN` (SPEC §4).
- **Resolved:** This spec is larger than 3–5 beads. That is accepted because its parts are tightly coupled.
- **Out of scope:** the general HTTP adapter (activity endpoints, pagination, 429 handling), sync and storage changes.
