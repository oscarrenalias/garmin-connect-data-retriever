"""Resolve the Garmin API key from the process environment or a `.env` file."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from garmin_archive.models import MissingApiKeyError

KEY_VARIABLES: tuple[str, ...] = ("GARMIN_API_KEY", "GARMIN_CONNECT_API_KEY")
DEFAULT_ENV_FILE = Path(".env")


@dataclass(frozen=True)
class ApiKeySource:
    """A resolved API key and where it came from. The key is excluded from repr."""

    key: str = field(repr=False)
    variable: str
    env_file: Path | None = None

    @property
    def origin(self) -> str:
        """Human-readable source description; never contains the key."""
        if self.env_file is None:
            return f"environment variable {self.variable}"
        return f"{self.env_file} ({self.variable})"


def _unquote(value: str) -> str:
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end != -1:
            rest = value[end + 1 :].strip()
            if not rest or rest.startswith("#"):
                return value[1:end]
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1].isspace():
            return value[:i].strip()
    return value


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse simple dotenv lines: KEY=value, quotes, `#` comments, blank lines."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        result[name] = _unquote(value)
    return result


def _first(values: Mapping[str, str]) -> tuple[str, str] | None:
    for name in KEY_VARIABLES:
        value = values.get(name)
        if value:
            return name, value
    return None


def find_api_key(
    environ: Mapping[str, str], env_file: Path = DEFAULT_ENV_FILE
) -> ApiKeySource | None:
    """Return the API key and its source, or None. Environment wins over `.env`."""
    found = _first(environ)
    if found is not None:
        return ApiKeySource(key=found[1], variable=found[0])
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    found = _first(parse_dotenv(text))
    if found is not None:
        return ApiKeySource(key=found[1], variable=found[0], env_file=env_file)
    return None


def require_api_key(environ: Mapping[str, str], env_file: Path = DEFAULT_ENV_FILE) -> ApiKeySource:
    """Like `find_api_key` but raises `MissingApiKeyError` when no key is set."""
    source = find_api_key(environ, env_file)
    if source is None:
        raise MissingApiKeyError(
            "No Garmin API key found. Set "
            + " or ".join(KEY_VARIABLES)
            + " in the environment or in a .env file."
        )
    return source
