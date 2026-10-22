"""Typed adapter around ``garmin-fit-sdk``; the only module that imports it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from garmin_fit_sdk import Decoder, Stream  # pyright: ignore[reportMissingTypeStubs]


@dataclass(frozen=True)
class FitDecodeResult:
    """Decoded FIT messages keyed by message name, plus any decode errors."""

    messages: dict[str, list[dict[str, object]]] = field(default_factory=lambda: {})
    errors: list[str] = field(default_factory=lambda: [])


def decode_fit(data: bytes) -> FitDecodeResult:
    """Decode FIT bytes. Never raises; SDK failures are returned as error strings."""
    try:
        stream: Any = Stream.from_byte_array(bytearray(data))
        decoder: Any = Decoder(stream)
        raw_messages, raw_errors = decoder.read()
    except Exception as exc:  # SDK raises assorted exception types
        return FitDecodeResult(errors=[f"{type(exc).__name__}: {exc}"])
    messages = cast("dict[str, list[dict[str, object]]]", raw_messages)
    errors = [str(e) for e in cast("list[object]", raw_errors)]
    return FitDecodeResult(messages=messages, errors=errors)
