"""MeshCore companion radio protocol: command builders and response parsers.

Pure functions over bytes, no I/O. Layouts come from ``docs/companion_protocol.md`` and
``examples/companion_radio/MyMesh.cpp`` in the firmware repository. All multi-byte integers are little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# commands (host to device)
CMD_APP_START = 1
CMD_SYNC_NEXT_MESSAGE = 10
CMD_SET_RADIO_PARAMS = 11
CMD_DEVICE_QUERY = 22
CMD_GET_CHANNEL = 31
CMD_SET_CHANNEL = 32

# responses (device to host, replies to a command)
RESP_OK = 0
RESP_ERR = 1
RESP_SELF_INFO = 5
RESP_CONTACT_MSG_RECV = 7
RESP_CHANNEL_MSG_RECV = 8
RESP_NO_MORE_MESSAGES = 10
RESP_DEVICE_INFO = 13
RESP_CONTACT_MSG_RECV_V3 = 16
RESP_CHANNEL_MSG_RECV_V3 = 17
RESP_CHANNEL_INFO = 18
RESP_CHANNEL_DATA_RECV = 27

# pushes (device to host, unsolicited); every push code has the top bit set
PUSH_ADVERT = 0x80
PUSH_SEND_CONFIRMED = 0x82
PUSH_MSG_WAITING = 0x83
PUSH_LOG_RX_DATA = 0x88
PUSH_NEW_ADVERT = 0x8A

APP_NAME = b"beacon-base"
PROTOCOL_VERSION = 3  # DEVICE_QUERY app target version
CHANNEL_NAME_LEN = 32
CHANNEL_SECRET_LEN = 16
PUBKEY_LEN = 32

ERROR_NAMES = {
    1: "unsupported command",
    2: "not found",
    3: "table full",
    4: "bad state",
    5: "file io error",
    6: "illegal argument",
}

# replies to CMD_SYNC_NEXT_MESSAGE that carry a message, plus the "queue empty" reply
MESSAGE_RESPONSES = frozenset(
    {
        RESP_CONTACT_MSG_RECV,
        RESP_CHANNEL_MSG_RECV,
        RESP_CONTACT_MSG_RECV_V3,
        RESP_CHANNEL_MSG_RECV_V3,
        RESP_CHANNEL_DATA_RECV,
    }
)


class ProtocolError(Exception):
    """A frame from the companion that does not parse."""


def is_push(code: int) -> bool:
    return code >= 0x80


def plausible_code(code: int) -> bool:
    """True for a first payload byte the companion could send. Used to resynchronise after garbage."""
    return code <= 0x1F or 0x80 <= code <= 0x9F


# --- commands ------------------------------------------------------------------------------------------------------


def app_start() -> bytes:
    return bytes([CMD_APP_START]) + bytes(7) + APP_NAME


def device_query() -> bytes:
    return bytes([CMD_DEVICE_QUERY, PROTOCOL_VERSION])


def get_channel(index: int) -> bytes:
    return bytes([CMD_GET_CHANNEL, index])


def set_channel(index: int, name: str, secret: bytes) -> bytes:
    raw_name = name.encode("utf-8")
    if len(raw_name) >= CHANNEL_NAME_LEN:
        raise ValueError(f"channel name must be at most {CHANNEL_NAME_LEN - 1} bytes")
    if len(secret) != CHANNEL_SECRET_LEN:
        raise ValueError(f"channel secret must be {CHANNEL_SECRET_LEN} bytes")
    return bytes([CMD_SET_CHANNEL, index]) + raw_name.ljust(CHANNEL_NAME_LEN, b"\0") + secret


def sync_next_message() -> bytes:
    return bytes([CMD_SYNC_NEXT_MESSAGE])


def set_radio_params(freq_khz: int, bw_hz: int, sf: int, cr: int) -> bytes:
    return struct.pack("<BIIBB", CMD_SET_RADIO_PARAMS, freq_khz, bw_hz, sf, cr)


# --- responses -----------------------------------------------------------------------------------------------------


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def error_code(frame: bytes) -> int | None:
    return frame[1] if len(frame) > 1 else None


def describe_error(code: int | None) -> str:
    if code is None:
        return "error"
    return f"error {code} ({ERROR_NAMES.get(code, 'unknown')})"


@dataclass(frozen=True)
class SelfInfo:
    public_key: bytes
    tx_power_dbm: int
    freq_khz: int
    bw_hz: int
    sf: int
    cr: int
    name: str


def parse_self_info(frame: bytes) -> SelfInfo:
    if len(frame) < 58 or frame[0] != RESP_SELF_INFO:
        raise ProtocolError(f"bad self info frame ({len(frame)} bytes)")
    freq, bw = struct.unpack_from("<II", frame, 48)
    return SelfInfo(
        public_key=bytes(frame[4 : 4 + PUBKEY_LEN]),
        tx_power_dbm=frame[2],
        freq_khz=freq,
        bw_hz=bw,
        sf=frame[56],
        cr=frame[57],
        name=_cstr(bytes(frame[58:])),
    )


@dataclass(frozen=True)
class DeviceInfo:
    fw_ver: int
    max_channels: int
    build: str
    model: str
    version: str


def parse_device_info(frame: bytes) -> DeviceInfo:
    if len(frame) < 2 or frame[0] != RESP_DEVICE_INFO:
        raise ProtocolError("bad device info frame")
    fw_ver = frame[1]
    if fw_ver < 3 or len(frame) < 80:
        raise ProtocolError(f"companion firmware too old or truncated device info (version {fw_ver}, {len(frame)} bytes)")
    return DeviceInfo(
        fw_ver=fw_ver,
        max_channels=frame[3],
        build=_cstr(bytes(frame[8:20])),
        model=_cstr(bytes(frame[20:60])),
        version=_cstr(bytes(frame[60:80])),
    )


@dataclass(frozen=True)
class ChannelInfo:
    index: int
    name: str
    secret: bytes


def parse_channel_info(frame: bytes) -> ChannelInfo:
    if len(frame) < 2 + CHANNEL_NAME_LEN + CHANNEL_SECRET_LEN or frame[0] != RESP_CHANNEL_INFO:
        raise ProtocolError(f"bad channel info frame ({len(frame)} bytes)")
    return ChannelInfo(
        index=frame[1],
        name=_cstr(bytes(frame[2 : 2 + CHANNEL_NAME_LEN])),
        secret=bytes(frame[2 + CHANNEL_NAME_LEN : 2 + CHANNEL_NAME_LEN + CHANNEL_SECRET_LEN]),
    )


@dataclass(frozen=True)
class ChannelData:
    """RESP_CODE_CHANNEL_DATA_RECV: a GRP_DATA packet received on a channel."""

    snr_x4: int  # SNR at the companion (not at the repeater), dB x 4
    channel_index: int
    path_len: int  # encoded path length when flooded, 0xFF when it arrived by a direct route
    data_type: int
    payload: bytes


def parse_channel_data(frame: bytes) -> ChannelData:
    if len(frame) < 9 or frame[0] != RESP_CHANNEL_DATA_RECV:
        raise ProtocolError(f"bad channel data frame ({len(frame)} bytes)")
    (snr_x4,) = struct.unpack_from("<b", frame, 1)
    (data_type,) = struct.unpack_from("<H", frame, 6)
    data_len = frame[8]
    if len(frame) < 9 + data_len:
        raise ProtocolError(f"channel data truncated: length byte says {data_len}, frame has {len(frame) - 9}")
    return ChannelData(
        snr_x4=snr_x4,
        channel_index=frame[4],
        path_len=frame[5],
        data_type=data_type,
        payload=bytes(frame[9 : 9 + data_len]),
    )


# --- frames used by the fake companion (the inverse of the parsers above) -----------------------------------------


def build_channel_data(snr_x4: int, channel_index: int, path_len: int, data_type: int, payload: bytes) -> bytes:
    return (
        struct.pack("<BbBBBBHB", RESP_CHANNEL_DATA_RECV, snr_x4, 0, 0, channel_index, path_len, data_type, len(payload))
        + payload
    )
