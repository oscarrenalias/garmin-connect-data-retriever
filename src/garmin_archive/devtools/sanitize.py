"""Sanitization of parsed Garmin JSON for committable fixtures (spec B7, rules 1-7).

``sanitize`` is pure over parsed JSON: it returns a new value and records bookkeeping
(replaced originals, pass-through values, redaction log) on the supplied context. The
pseudonym map lives only in memory and is never persisted.
"""

from __future__ import annotations

import json
import random
import re
import string
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast

CATALOG_KEYS = frozenset(
    {
        "activityType",
        "activityTypeDTO",
        "eventType",
        "eventTypeDTO",
        "timeZoneUnitDTO",
        "deviceDisplayUnitsDTO",
        "unitOfPoolLength",
        "fileFormat",
    }
)
# Catalog enum keys that match the free-text name regex but hold Garmin catalog values
# (for example exerciseName "WALK"); kept verbatim and counted as pass-through values.
CATALOG_NAME_KEYS = frozenset({"exerciseName"})
EXPLICIT_ID_KEYS = frozenset(
    {
        "activityId",
        "parentId",
        "childIds",
        "userProfileId",
        "ownerId",
        "deviceId",
        "unitId",
        "serialNumber",
        "activityUUID",
        "uuid",
    }
)
# Trailing optional "s" so plural arrays such as activityLikeDisplayNames are covered.
_NAME_RE = re.compile(
    r"(name|fullname|displayname|description|comment|email|locationname)s?$", re.IGNORECASE
)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DIGIT_RUN_RE = re.compile(r"\d{6,}")
_ALNUM = string.ascii_letters + string.digits
_MIN_ID_DIGITS = 6

REDACTED = "REDACTED"
REDACTED_URL = "https://redacted.invalid/"

RULE_IDENTIFIER = "identifier"
RULE_FREE_TEXT = "free_text"
RULE_URL = "url"
RULE_COORDINATE = "coordinate"


@dataclass(frozen=True)
class RedactionEntry:
    """One redaction: where and which rule. Never holds a value."""

    source: str
    json_path: str
    rule: str


@dataclass
class SanitizeContext:
    """Per-run state: pseudonym map, collected originals and redaction log."""

    rng: random.Random = field(default_factory=random.Random)
    pseudonyms: dict[str, str] = field(default_factory=lambda: {})
    originals: set[str] = field(default_factory=lambda: set[str]())
    # Identifier and URL originals: never exempted by the pass-through allowance.
    strict_originals: set[str] = field(default_factory=lambda: set[str]())
    pass_through: dict[str, set[str]] = field(default_factory=lambda: {})
    redaction_log: list[RedactionEntry] = field(default_factory=lambda: [])
    _used: set[str] = field(default_factory=lambda: set[str](), repr=False)

    def pseudonym_for(self, original: str) -> str:
        """Return the consistent pseudonym for an identifier's decimal/string form."""
        existing = self.pseudonyms.get(original)
        if existing is not None:
            return existing
        self.originals.add(original)
        self.strict_originals.add(original)
        while True:
            candidate = self._generate(original)
            if candidate != original and candidate not in self._used:
                break
        self._used.add(candidate)
        self.pseudonyms[original] = candidate
        return candidate

    def _generate(self, original: str) -> str:
        if _UUID_RE.match(original):
            return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        digits = original.lstrip("-")
        if digits.isascii() and digits.isdigit():
            n = len(digits)
            first = self.rng.choice("123456789")
            rest = "".join(self.rng.choice(string.digits) for _ in range(n - 1))
            return ("-" if original.startswith("-") else "") + first + rest
        return "".join(self.rng.choice(_ALNUM) for _ in range(len(original)))

    def note_pass_through(self, source: str, value: object) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (str, int, float)):
            self.pass_through.setdefault(source, set()).add(str(value))

    def log(self, source: str, path: str, rule: str) -> None:
        self.redaction_log.append(RedactionEntry(source, path, rule))


def pseudonymize_digit_runs(text: str, ctx: SanitizeContext) -> str:
    """Replace each run of 6+ digits with its pseudonym (e.g. ZIP member names)."""
    return _DIGIT_RUN_RE.sub(lambda m: ctx.pseudonym_for(m.group(0)), text)


def sanitize(value: Any, ctx: SanitizeContext, source: str = "") -> Any:
    """Return a sanitized copy of parsed JSON ``value``."""
    return _walk(value, ctx, source, "$", None, False, False)


def _is_id_value(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return len(str(abs(value))) >= _MIN_ID_DIGITS
    if isinstance(value, str):
        if _UUID_RE.match(value):
            return True
        return value.isascii() and value.isdigit() and len(value) >= _MIN_ID_DIGITS
    return False


def _walk(
    value: Any,
    ctx: SanitizeContext,
    source: str,
    path: str,
    key: str | None,
    id_ctx: bool,
    name_ctx: bool,
) -> Any:
    """Sanitize ``value``; ``id_ctx``/``name_ctx`` carry the parent key's rule into arrays."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, v in cast("dict[Any, Any]", value).items():
            k = str(raw_key)
            child = f"{path}.{k}"
            if k in CATALOG_KEYS or (k in CATALOG_NAME_KEYS and isinstance(v, str)):
                _note_all(v, ctx, source)
                out[k] = v
                continue
            is_id = k in EXPLICIT_ID_KEYS
            generic_id = k.endswith(("Id", "ID", "UUID", "Pk", "PK"))
            lowered = k.lower()
            is_coord_key = "latitude" in lowered or "longitude" in lowered
            if is_coord_key and isinstance(v, (int, float)) and not isinstance(v, bool):
                ctx.log(source, child, RULE_COORDINATE)
                out[k] = None
                continue
            out[k] = _walk(
                v,
                ctx,
                source,
                child,
                k,
                is_id or generic_id,
                bool(_NAME_RE.search(k)),
            )
        return out
    if isinstance(value, list):
        return [
            _walk(item, ctx, source, f"{path}[{i}]", key, id_ctx, name_ctx)
            for i, item in enumerate(cast("list[Any]", value))
        ]
    return _scalar(value, ctx, source, path, key, id_ctx, name_ctx)


def _scalar(
    value: Any,
    ctx: SanitizeContext,
    source: str,
    path: str,
    key: str | None,
    id_ctx: bool,
    name_ctx: bool,
) -> Any:
    explicit = key in EXPLICIT_ID_KEYS
    is_scalar_id = id_ctx and not isinstance(value, bool) and isinstance(value, (int, str))
    if is_scalar_id and (explicit or _is_id_value(value)):
        ctx.log(source, path, RULE_IDENTIFIER)
        pseudo = ctx.pseudonym_for(str(value))
        return int(pseudo) if isinstance(value, int) else pseudo
    if isinstance(value, str):
        if name_ctx:
            ctx.log(source, path, RULE_FREE_TEXT)
            ctx.originals.add(value)
            return REDACTED
        if value.startswith(("http://", "https://")):
            ctx.log(source, path, RULE_URL)
            ctx.originals.add(value)
            ctx.strict_originals.add(value)
            return REDACTED_URL
    ctx.note_pass_through(source, value)
    return value


def _note_all(value: Any, ctx: SanitizeContext, source: str) -> None:
    """Record every leaf of a verbatim catalog object as pass-through."""
    if isinstance(value, dict):
        for v in cast("dict[Any, Any]", value).values():
            _note_all(v, ctx, source)
    elif isinstance(value, list):
        for v in cast("list[Any]", value):
            _note_all(v, ctx, source)
    else:
        ctx.note_pass_through(source, value)


_MIN_SENSITIVE_STRING = 4
_MIN_SENSITIVE_DIGITS = 5


@dataclass(frozen=True)
class LeakHit:
    """One leak-check hit: where, never which value."""

    json_path: str


def _is_numeric_original(value: str) -> bool:
    digits = value.lstrip("-")
    return bool(digits) and digits.isascii() and digits.isdigit()


def sensitive_set(ctx: SanitizeContext, source: str | None = None) -> frozenset[str]:
    """Return the originals that must not appear in written output (spec B7 rule 7).

    With ``source``, exclude free-text originals that pass through unchanged in that source
    document. Without it (report, manifests, redaction log), exclude them if they pass
    through in any document. The exclusion only prevents free-text false positives (a name
    equal to a typeKey); identifier and URL originals are always sensitive.
    """
    if source is None:
        allowed: set[str] = set()
        for values in ctx.pass_through.values():
            allowed |= values
    else:
        allowed = ctx.pass_through.get(source, set())
    out: set[str] = set()
    for original in ctx.originals:
        if original in allowed and original not in ctx.strict_originals:
            continue
        if _is_numeric_original(original):
            if len(original.lstrip("-")) >= _MIN_SENSITIVE_DIGITS:
                out.add(original)
        elif len(original) >= _MIN_SENSITIVE_STRING:
            out.add(original)
    return frozenset(out)


def _matches(text: str, value: str) -> bool:
    if _is_numeric_original(value):
        return re.search(rf"(?<!\d){re.escape(value)}(?!\d)", text) is not None
    return value in text


def _leaks(text: str, sensitive: Iterable[str]) -> bool:
    return any(_matches(text, v) for v in sensitive)


def check_leaks(serialized: str, sensitive: Iterable[str]) -> list[LeakHit]:
    """Search ``serialized`` for sensitive values; report json_paths only, never values.

    Matching is case-sensitive substring search; numeric originals match only when not
    adjacent to another digit. If the text is JSON the hits are located by path,
    otherwise a single ``$`` hit is returned.
    """
    values = list(sensitive)
    if not _leaks(serialized, values):
        return []
    try:
        parsed = json.loads(serialized)
    except ValueError:
        return [LeakHit("$")]
    paths = locate_leaks(parsed, values)
    return paths or [LeakHit("$")]


def locate_leaks(parsed: Any, sensitive: Iterable[str]) -> list[LeakHit]:
    """Return the json_path of every key or scalar in ``parsed`` containing a sensitive value."""
    values = list(sensitive)
    hits: list[LeakHit] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for raw_key, v in cast("dict[Any, Any]", node).items():
                k = str(raw_key)
                child = f"{path}.{k}"
                if _leaks(k, values):
                    hits.append(LeakHit(child))
                visit(v, child)
        elif isinstance(node, list):
            for i, v in enumerate(cast("list[Any]", node)):
                visit(v, f"{path}[{i}]")
        elif node is not None and not isinstance(node, bool):
            text = node if isinstance(node, str) else json.dumps(node)
            if _leaks(text, values):
                hits.append(LeakHit(path))

    visit(parsed, "$")
    return hits
