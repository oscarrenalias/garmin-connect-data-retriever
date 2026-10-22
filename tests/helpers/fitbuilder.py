"""Build minimal valid FIT bytes in memory. Never writes files."""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from garmin_fit_sdk import Encoder

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)  # fmt: skip


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def build_fit(record_count: int = 3) -> bytes:
    """A FIT file with one file_id message and `record_count` record messages."""
    body = bytearray()
    # Definition: local 0 -> file_id (global 0): type (enum), manufacturer (uint16).
    body += bytes([0x40, 0x00, 0x00]) + struct.pack("<H", 0) + bytes([2])
    body += bytes([0, 1, 0x00, 1, 2, 0x84])
    body += bytes([0x00, 4]) + struct.pack("<H", 255)  # type=activity, manufacturer=255
    # Definition: local 1 -> record (global 20): timestamp (uint32), heart_rate (uint8).
    body += bytes([0x41, 0x00, 0x00]) + struct.pack("<H", 20) + bytes([2])
    body += bytes([253, 4, 0x86, 3, 1, 0x02])
    for i in range(record_count):
        body += bytes([0x01]) + struct.pack("<I", 1_000_000_000 + i) + bytes([100 + i])
    header = struct.pack("<BBHI4s", 14, 0x20, 2000, len(body), b".FIT")
    header += struct.pack("<H", fit_crc(header))
    payload = header + bytes(body)
    return payload + struct.pack("<H", fit_crc(payload))


# ---- Encoder-based activity FIT builder ---------------------------------------------------

MESG_SESSION = 18
MESG_LAP = 19
MESG_RECORD = 20
MESG_LENGTH = 101
MESG_SET = 225
MESG_FILE_ID = 0

FIT_START = datetime(2026, 6, 25, 6, 42, 28, tzinfo=UTC)


def build_activity_fit(
    *,
    sessions: Iterable[Mapping[str, Any]] = (),
    laps: Iterable[Mapping[str, Any]] = (),
    records: Iterable[Mapping[str, Any]] = (),
    sets: Iterable[Mapping[str, Any]] = (),
    lengths: Iterable[Mapping[str, Any]] = (),
) -> bytes:
    """Encode a FIT file with ``garmin_fit_sdk.Encoder``; each argument is a list of fields."""
    encoder = Encoder()
    encoder.write_mesg(
        {"mesg_num": MESG_FILE_ID, "type": "activity", "manufacturer": "development", "product": 0}
    )
    for number, group in (
        (MESG_RECORD, records),
        (MESG_SET, sets),
        (MESG_LENGTH, lengths),
        (MESG_LAP, laps),
        (MESG_SESSION, sessions),
    ):
        for fields in group:
            encoder.write_mesg({"mesg_num": number, **fields})
    return encoder.close()


def running_fit(
    *, start: datetime = FIT_START, lap_count: int = 3, record_count: int = 100
) -> bytes:
    """One session, ``lap_count`` laps and ``record_count`` GPS records (no power/cadence)."""
    lap_s = record_count // lap_count
    records = [
        {
            "timestamp": start + timedelta(seconds=i),
            "position_lat": 400_000_000 + i * 1000,
            "position_long": -300_000_000 + i * 500,
            "heart_rate": 120 + i % 10,
            "enhanced_speed": 2.5,
            "enhanced_altitude": 20.0 + i * 0.1,
            "distance": i * 2.5,
        }
        for i in range(record_count)
    ]
    laps = [
        {
            "timestamp": start + timedelta(seconds=(k + 1) * lap_s),
            "start_time": start + timedelta(seconds=k * lap_s),
            "total_elapsed_time": float(lap_s),
            "total_timer_time": float(lap_s),
            "total_distance": lap_s * 2.5,
            "avg_heart_rate": 130,
            "max_heart_rate": 140,
            "enhanced_avg_speed": 2.5,
            "lap_trigger": "manual",
            "intensity": "active",
        }
        for k in range(lap_count)
    ]
    session = {
        "timestamp": start + timedelta(seconds=record_count),
        "start_time": start,
        "sport": "running",
        "sub_sport": "generic",
        "total_elapsed_time": float(record_count),
        "total_timer_time": float(record_count),
        "total_distance": record_count * 2.5,
        "avg_heart_rate": 130,
        "max_heart_rate": 140,
    }
    return build_activity_fit(sessions=[session], laps=laps, records=records)


def cardio_fit(*, start: datetime = FIT_START, set_count: int = 10) -> bytes:
    """One session, no laps, ``set_count`` set messages and a few records."""
    sets = [
        {
            "timestamp": start + timedelta(seconds=(i + 1) * 30),
            "start_time": start + timedelta(seconds=i * 30),
            "duration": 30.0,
            "repetitions": 10 + i,
            "set_type": "active" if i % 2 == 0 else "rest",
            "category": ["push_up"],
        }
        for i in range(set_count)
    ]
    records = [{"timestamp": start + timedelta(seconds=i), "heart_rate": 110 + i} for i in range(5)]
    session = {
        "timestamp": start + timedelta(seconds=set_count * 30),
        "start_time": start,
        "sport": "training",
        "total_elapsed_time": set_count * 30.0,
        "total_timer_time": set_count * 30.0,
    }
    return build_activity_fit(sessions=[session], records=records, sets=sets)
