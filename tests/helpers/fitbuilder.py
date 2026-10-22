"""Build minimal valid FIT bytes in memory. Never writes files."""

from __future__ import annotations

import struct

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
