"""Load the Garmin API key for discovery tooling from the environment or a dotenv file."""

from collections.abc import Mapping
from pathlib import Path

KEY_VARIABLES: tuple[str, ...] = ("GARMIN_API_KEY", "GARMIN_CONNECT_API_KEY")


class MissingApiKeyError(Exception):
    """Raised when no API key is found. The message never contains key values."""

    def __init__(self) -> None:
        super().__init__(
            "No Garmin API key found. Set "
            + " or ".join(KEY_VARIABLES)
            + " in the environment or in a .env file."
        )


class SecretStr:
    """Holds a secret; repr/str never reveal it. Use `get_secret_value()` to read."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretStr('**********')"

    def __str__(self) -> str:
        return "**********"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretStr) and self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    # Unquoted values: strip trailing inline comment.
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1].isspace():
            return value[:i].strip()
    return value


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse minimal dotenv syntax: KEY=value, quotes, `#` comments, blank lines."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        result[key] = _unquote(value)
    return result


def _first(values: Mapping[str, str]) -> str | None:
    for name in KEY_VARIABLES:
        value = values.get(name)
        if value:
            return value
    return None


def load_api_key(
    environ: Mapping[str, str],
    env_file: Path | None,
    cwd: Path,
) -> SecretStr:
    """Return the API key: environment first, then the dotenv file.

    The dotenv file is `env_file` if given, else `cwd/.env` if it exists.
    """
    found = _first(environ)
    if found is not None:
        return SecretStr(found)

    path = env_file if env_file is not None else cwd / ".env"
    if env_file is not None or path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        found = _first(parse_dotenv(text))
        if found is not None:
            return SecretStr(found)

    raise MissingApiKeyError
