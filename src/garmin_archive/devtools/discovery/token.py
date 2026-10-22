"""Token acquisition and Bearer-header probe for the discovery tool (spec B2).

Only the *shape* of the token response is recorded. The token body and the token
itself are never written anywhere; the token lives in memory in a `SecretStr`.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from garmin_archive.devtools.discovery.credentials import SecretStr
from garmin_archive.devtools.discovery.http import DiscoveryHttp, HttpResponse, RetriesExhausted

TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SEARCH_PATH = "/activitylist-service/activities/search/activities"

Outcome = Literal["stopped", "needs_token_field"]


@dataclass
class TokenStepResult:
    """Outcome of the token step. `outcome` is None when the run may continue."""

    token: SecretStr | None
    outcome: Outcome | None
    token_report: dict[str, Any]
    header_convention: dict[str, Any]
    note: str | None = None


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "object"


def _jwt_like(value: str) -> bool:
    return len(value.split(".")) == 3


_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+")


def raw_body_token(body: bytes) -> str | None:
    """Return the stripped body when it looks like a bare token, else None."""
    try:
        text = body.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return text if _RAW_TOKEN_RE.fullmatch(text) else None


def _decode_segment(segment: str) -> dict[str, Any] | None:
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        parsed: object = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed  # pyright: ignore[reportUnknownVariableType]


def describe_jwt(token: str) -> dict[str, Any]:
    """Record JWT structure only: key and claim names, alg and lifetime. No values."""
    header_part, payload_part, _ = token.split(".")
    header = _decode_segment(header_part)
    payload = _decode_segment(payload_part)
    if header is None or payload is None:
        return {"decode_error": True}
    info: dict[str, Any] = {
        "header_keys": sorted(header),
        "claim_names": sorted(payload),
    }
    alg = header.get("alg")
    if isinstance(alg, str):
        info["alg"] = alg
    exp, iat = payload.get("exp"), payload.get("iat")
    if (
        isinstance(exp, int)
        and isinstance(iat, int)
        and not isinstance(exp, bool)
        and not isinstance(iat, bool)
    ):
        info["lifetime_s"] = exp - iat
    return info


def describe_token_body(
    body: bytes,
) -> tuple[dict[str, dict[str, Any]], dict[str, int | float], dict[str, str], int | None]:
    """Return (fields, expiry_fields, string_values, body_length).

    `string_values` carries string values for token-field selection only; it must
    never be recorded. `body_length` is set only when the body is not a JSON object.
    """
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return {}, {}, {}, len(body)
    if not isinstance(parsed, dict):
        return {}, {}, {}, len(body)
    obj: dict[str, Any] = parsed  # pyright: ignore[reportUnknownVariableType]
    fields: dict[str, dict[str, Any]] = {}
    expiry: dict[str, int | float] = {}
    strings: dict[str, str] = {}
    for key, value in obj.items():
        entry: dict[str, Any] = {"type": _json_type(value)}
        if isinstance(value, str):
            entry["length"] = len(value)
            entry["jwt_like"] = _jwt_like(value)
            strings[key] = value
        fields[key] = entry
        lowered = key.lower()
        if (
            ("expir" in lowered or "ttl" in lowered)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            expiry[key] = value
    return fields, expiry, strings, None


def auto_token_field(string_keys: list[str]) -> str | None:
    """Select the only string key containing `token` but not `refresh`/`type`."""
    candidates = [
        k
        for k in string_keys
        if "token" in k.lower() and "refresh" not in k.lower() and "type" not in k.lower()
    ]
    return candidates[0] if len(candidates) == 1 else None


def acquire_token(
    http: DiscoveryHttp,
    api_key: SecretStr,
    token_field: str | None = None,
) -> TokenStepResult:
    """POST for a token, pick the token field, then probe search with a Bearer header.

    May raise `RunPartial` (429 handling in `DiscoveryHttp`); the caller maps it.
    """
    report: dict[str, Any] = {}
    header: dict[str, Any] = {}
    response = http.request(
        "POST",
        TOKEN_PATH,
        headers={"Authorization": f"ApiKey {api_key.get_secret_value()}"},
    )
    if isinstance(response, RetriesExhausted):
        report.update(status="failed", error=response.error)
        return TokenStepResult(None, "stopped", report, header, "token request retries exhausted")

    content_type = response.headers.values.get("content-type")
    report["http_status"] = response.status
    report["content_type"] = content_type
    if not 200 <= response.status < 300:
        report["status"] = "failed"
        return TokenStepResult(None, "stopped", report, header, "token request not 2xx")

    fields, expiry, strings, body_length = describe_token_body(response.body)
    report["header_names"] = sorted(
        [*response.headers.values.keys(), *response.headers.other_names]
    )
    if body_length is not None:
        report["body_length"] = body_length
    else:
        report["fields"] = fields
        report["expiry_fields"] = expiry

    raw_token = raw_body_token(response.body) if body_length is not None else None
    if raw_token is not None:
        jwt_like = _jwt_like(raw_token)
        report["token_field"] = None
        report["token_field_source"] = "raw_body"
        report["jwt_like"] = jwt_like
        if jwt_like:
            report["jwt"] = describe_jwt(raw_token)
        report["status"] = "confirmed"
        token = SecretStr(raw_token)
    else:
        if token_field is not None:
            chosen, source = token_field, "explicit"
        else:
            chosen, source = auto_token_field(list(strings)), "auto"
        if chosen is None or chosen not in strings:
            report["status"] = "unknown"
            return TokenStepResult(
                None, "needs_token_field", report, header, "rerun with --token-field"
            )
        report["token_field"] = chosen
        report["token_field_source"] = source
        report["status"] = "confirmed"
        token = SecretStr(strings[chosen])
    del strings

    header.update(scheme="Bearer")
    probe = http.get(
        SEARCH_PATH,
        params={"start": "0", "limit": "1"},
        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
    )
    if isinstance(probe, RetriesExhausted):
        header.update(status="failed", error=probe.error)
        return TokenStepResult(token, "stopped", report, header, "probe retries exhausted")
    header["probe_http_status"] = probe.status
    if probe.status in (401, 403):
        header["status"] = "failed"
        return TokenStepResult(token, "stopped", report, header, "Bearer probe rejected")
    header["status"] = "confirmed" if _ok(probe) else "unknown"
    return TokenStepResult(token, None, report, header)


def _ok(response: HttpResponse) -> bool:
    return 200 <= response.status < 300


__all__ = ["TokenStepResult", "acquire_token", "auto_token_field", "describe_token_body"]
