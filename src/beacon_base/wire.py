"""Beacon report wire format.

Mirrors ``src/helpers/BeaconReport.h`` in the firmware repository, which owns the format. ``tests/fixtures`` holds the
golden vectors generated from the firmware, and ``tests/test_wire.py`` checks this module against them.

A report is the data of one ``GRP_DATA`` packet with ``data_type`` ``REPORT_DATA_TYPE``::

    header (10 bytes):  [version:1][repeater key prefix:8][entry count:1]
    entry  (16 bytes):  [beacon key prefix:8][counter:4 LE][rssi:1 int8 dBm][snr:1 int8, x4][batt mV:2 LE]
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

REPORT_DATA_TYPE = 0xFFBE
REPORT_VERSION = 1

ID_LEN = 8
HEADER_LEN = 1 + ID_LEN + 1
ENTRY_LEN = ID_LEN + 4 + 1 + 1 + 2
MAX_GROUP_DATA_LENGTH = 165
MAX_ENTRIES = (MAX_GROUP_DATA_LENGTH - HEADER_LEN) // ENTRY_LEN

_HEADER = struct.Struct(f"<B{ID_LEN}sB")
_ENTRY = struct.Struct(f"<{ID_LEN}sIbbH")
assert _HEADER.size == HEADER_LEN and _ENTRY.size == ENTRY_LEN


class WireError(ValueError):
    """A report that is malformed or has an unknown version."""


@dataclass(frozen=True)
class Observation:
    beacon_id: bytes  # beacon public key prefix
    counter: int
    rssi: int  # dBm, as measured by the repeater
    snr_x4: int  # dB x 4, as measured by the repeater
    batt_mv: int

    @property
    def snr(self) -> float:
        return self.snr_x4 / 4.0


@dataclass(frozen=True)
class Report:
    repeater_id: bytes  # repeater public key prefix
    observations: tuple[Observation, ...]


def decode_report(data: bytes) -> Report:
    """Decode one report. Unknown versions and malformed lengths raise WireError; trailing bytes are ignored."""
    if len(data) < HEADER_LEN:
        raise WireError(f"report too short ({len(data)} bytes)")
    version, repeater_id, count = _HEADER.unpack_from(data)
    if version != REPORT_VERSION:
        raise WireError(f"unknown report version {version}")
    if count > MAX_ENTRIES:
        raise WireError(f"entry count {count} exceeds the maximum of {MAX_ENTRIES}")
    if len(data) < HEADER_LEN + count * ENTRY_LEN:
        raise WireError(f"report truncated: {count} entries need {HEADER_LEN + count * ENTRY_LEN} bytes, got {len(data)}")
    observations = tuple(
        Observation(*_ENTRY.unpack_from(data, HEADER_LEN + i * ENTRY_LEN)) for i in range(count)
    )
    return Report(repeater_id, observations)


def encode_report(repeater_key: bytes, observations: list[Observation] | tuple[Observation, ...]) -> bytes:
    """Encode a report (used by the simulator and tests). Only the first ID_LEN bytes of the key are sent."""
    if not 1 <= len(observations) <= MAX_ENTRIES:
        raise ValueError(f"a report holds 1 to {MAX_ENTRIES} observations, got {len(observations)}")
    if len(repeater_key) < ID_LEN:
        raise ValueError(f"repeater key must be at least {ID_LEN} bytes")
    parts = [_HEADER.pack(REPORT_VERSION, bytes(repeater_key[:ID_LEN]), len(observations))]
    for o in observations:
        if len(o.beacon_id) != ID_LEN:
            raise ValueError(f"beacon id must be {ID_LEN} bytes")
        parts.append(_ENTRY.pack(o.beacon_id, o.counter, o.rssi, o.snr_x4, o.batt_mv))
    return b"".join(parts)
