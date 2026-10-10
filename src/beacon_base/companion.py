"""MeshCore companion radio protocol: command builders and response parsers.

Pure functions over bytes, no I/O. Layouts come from ``docs/companion_protocol.md`` and
``examples/companion_radio/MyMesh.cpp`` in the firmware repository. All multi-byte integers are little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# commands (host to device)
CMD_APP_START = 1
CMD_SEND_TXT_MSG = 2
CMD_GET_CONTACTS = 4
CMD_GET_DEVICE_TIME = 5
CMD_SET_DEVICE_TIME = 6
CMD_ADD_UPDATE_CONTACT = 9
CMD_SYNC_NEXT_MESSAGE = 10
CMD_SET_RADIO_PARAMS = 11
CMD_RESET_PATH = 13
CMD_DEVICE_QUERY = 22
CMD_SEND_LOGIN = 26
CMD_GET_CONTACT_BY_KEY = 30
CMD_GET_CHANNEL = 31
CMD_SET_CHANNEL = 32
CMD_SET_OTHER_PARAMS = 38
CMD_SET_PATH_HASH_MODE = 61

# responses (device to host, replies to a command)
RESP_OK = 0
RESP_ERR = 1
RESP_CONTACTS_START = 2
RESP_CONTACT = 3
RESP_END_OF_CONTACTS = 4
RESP_SELF_INFO = 5
RESP_SENT = 6
RESP_CONTACT_MSG_RECV = 7
RESP_CHANNEL_MSG_RECV = 8
RESP_CURR_TIME = 9
RESP_NO_MORE_MESSAGES = 10
RESP_DEVICE_INFO = 13
RESP_CONTACT_MSG_RECV_V3 = 16
RESP_CHANNEL_MSG_RECV_V3 = 17
RESP_CHANNEL_INFO = 18
RESP_CHANNEL_DATA_RECV = 27

# pushes (device to host, unsolicited); every push code has the top bit set
PUSH_ADVERT = 0x80  # an advert from a contact the companion stores: code + 32-byte public key, no position
PUSH_SEND_CONFIRMED = 0x82
PUSH_MSG_WAITING = 0x83
PUSH_LOGIN_SUCCESS = 0x85  # a repeater accepted a login; carries the role it granted
PUSH_LOGIN_FAIL = 0x86  # the companion's own failure push; a repeater never sends a failure reply, so this is not expected
PUSH_LOG_RX_DATA = 0x88
PUSH_NEW_ADVERT = 0x8A  # an advert from a node it does not store: a full contact frame, with position

ADV_TYPE_CHAT = 1
ADV_TYPE_REPEATER = 2
ADV_TYPE_SENSOR = 4
TXT_TYPE_PLAIN = 0  # a person's text message
TXT_TYPE_CLI_DATA = 1  # a CLI command to a repeater, or its reply
PERM_ADMIN = 3  # ACL permission of an admin client; 0 is guest, 1 read-only, 2 read-write
MAX_PASSWORD_LEN = 15  # bytes; the firmware truncates longer passwords silently
MAX_CLI_TEXT_LEN = 160  # bytes; longer text is answered with the misleading ERR_CODE_TABLE_FULL
CONTACT_FRAME_LEN = 148
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


def get_contacts() -> bytes:
    return bytes([CMD_GET_CONTACTS])


def get_contact_by_key(pubkey: bytes) -> bytes:
    if len(pubkey) != PUBKEY_LEN:
        raise ValueError(f"public key must be {PUBKEY_LEN} bytes")
    return bytes([CMD_GET_CONTACT_BY_KEY]) + pubkey


def set_manual_add_contacts(manual: bool) -> bytes:
    """CMD_SET_OTHER_PARAMS with only the first parameter, so the companion's telemetry and location settings are left alone."""
    return bytes([CMD_SET_OTHER_PARAMS, 1 if manual else 0])


def sync_next_message() -> bytes:
    return bytes([CMD_SYNC_NEXT_MESSAGE])


def set_path_hash_mode(mode: int) -> bytes:
    """CMD_SET_PATH_HASH_MODE: the size of the path hashes the companion puts in the packets it floods (mode + 1 bytes)."""
    if not 0 <= mode <= 2:
        raise ValueError("path hash mode must be 0, 1 or 2")
    return bytes([CMD_SET_PATH_HASH_MODE, 0, mode])


def get_device_time() -> bytes:
    return bytes([CMD_GET_DEVICE_TIME])


def set_device_time(epoch_s: int) -> bytes:
    """The companion refuses a time earlier than its current one (ERR_CODE_ILLEGAL_ARG); equal is accepted."""
    return bytes([CMD_SET_DEVICE_TIME]) + struct.pack("<I", epoch_s)


def add_update_contact(public_key: bytes, adv_type: int, name: str, lat: float = 0.0, lon: float = 0.0) -> bytes:
    """Make a node a contact of the companion, with its route unknown so messages to it are flooded. The firmware checks for 36 bytes
    but reads 136, so the whole contact frame is always sent (148 bytes, which this is)."""
    if len(public_key) != PUBKEY_LEN:
        raise ValueError(f"public key must be {PUBKEY_LEN} bytes")
    return build_contact(CMD_ADD_UPDATE_CONTACT, public_key, adv_type, name, 0, lat, lon, 0)


def reset_path(public_key: bytes) -> bytes:
    """Forget a contact's stored route, so the next message to it is flooded (and the reply teaches the new route)."""
    if len(public_key) != PUBKEY_LEN:
        raise ValueError(f"public key must be {PUBKEY_LEN} bytes")
    return bytes([CMD_RESET_PATH]) + public_key


def send_login(public_key: bytes, password: str = "") -> bytes:
    """Log in to a repeater, which must be a contact. An empty password logs in a key the repeater already has in its ACL with the
    role it has there; for any other key it is a guest login (the guest password is empty by default)."""
    if len(public_key) != PUBKEY_LEN:
        raise ValueError(f"public key must be {PUBKEY_LEN} bytes")
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_LEN:
        raise ValueError(f"a repeater password is at most {MAX_PASSWORD_LEN} bytes (longer ones are truncated by the firmware)")
    if b"\0" in raw:
        raise ValueError("password must not contain NUL")
    return bytes([CMD_SEND_LOGIN]) + public_key + raw


def send_cli(public_key: bytes, text: str, attempt: int = 0) -> bytes:
    """Send a CLI command to a repeater (a CLI_DATA message). The companion replaces the timestamp with its own clock. Only the first 6
    bytes of the key are sent, as the contact is looked up by prefix."""
    if len(public_key) != PUBKEY_LEN:
        raise ValueError(f"public key must be {PUBKEY_LEN} bytes")
    raw = text.encode("utf-8")
    if not 1 <= len(raw) <= MAX_CLI_TEXT_LEN:
        raise ValueError(f"a CLI command is 1-{MAX_CLI_TEXT_LEN} bytes")
    return bytes([CMD_SEND_TXT_MSG, TXT_TYPE_CLI_DATA, attempt & 3]) + bytes(4) + public_key[:6] + raw


def make_tag(n: int) -> str:
    """Two characters that a repeater reflects at the start of its reply when the command is sent as ``NN|command``."""
    return f"{n & 0xFF:02x}"


def tag_command(tag: str, command: str) -> str:
    if len(tag) != 2 or "|" in tag or tag.startswith(" "):
        raise ValueError("a command tag is two characters, not '|' and not starting with a space")
    return f"{tag}|{command}"


def split_tag(text: str) -> tuple[str | None, str]:
    """The ``NN`` of a reply that starts with ``NN|`` and the rest; (None, text) otherwise."""
    if len(text) >= 3 and text[2] == "|":
        return text[:2], text[3:]
    return None, text


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
    manual_add_contacts: int = 0  # bit 0: the companion does not add adverts to its contacts by itself


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
        manual_add_contacts=frame[47],
    )


@dataclass(frozen=True)
class DeviceInfo:
    fw_ver: int
    max_channels: int
    build: str
    model: str
    version: str
    path_hash_mode: int | None = None  # 0-2 (hash size 1-3 bytes); None when the firmware is older than v10


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
        path_hash_mode=frame[81] if fw_ver >= 10 and len(frame) >= 82 else None,  # byte 80 is the client-repeat flag (v9+)
    )


@dataclass(frozen=True)
class Sent:
    """RESP_CODE_SENT: the companion transmitted a login or message. It does not mean the repeater received it."""

    flooded: bool  # False when a stored route was used
    tag: bytes  # for a login the first 4 bytes of the repeater's key; zeros for a CLI message (no ack is expected)
    timeout_ms: int  # the companion's estimate of the round trip; it has no timeout of its own


def parse_sent(frame: bytes) -> Sent:
    if len(frame) < 10 or frame[0] != RESP_SENT:
        raise ProtocolError(f"bad sent frame ({len(frame)} bytes)")
    return Sent(bool(frame[1]), bytes(frame[2:6]), struct.unpack_from("<I", frame, 6)[0])


def parse_device_time(frame: bytes) -> int:
    if len(frame) < 5 or frame[0] != RESP_CURR_TIME:
        raise ProtocolError(f"bad device time frame ({len(frame)} bytes)")
    return struct.unpack_from("<I", frame, 1)[0]


@dataclass(frozen=True)
class LoginResult:
    """PUSH_CODE_LOGIN_SUCCESS (or the companion's own PUSH_CODE_LOGIN_FAIL).

    A success is not proof of admin rights: an empty password from a key the repeater does not know logs in as a guest. ``admin`` is
    true only when the repeater says admin (flag 1) and the ACL permission is admin (3)."""

    success: bool
    prefix: bytes  # first 6 bytes of the repeater's key
    admin_flag: int  # byte 1 of a success; 0 for a legacy reply, which cannot be told from a guest
    permissions: int | None  # ACL permission granted: 0 guest, 1 read-only, 2 read-write, 3 admin; None in a legacy reply
    server_time: int | None  # the repeater's clock when it replied
    firmware_level: int | None

    @property
    def admin(self) -> bool:
        return self.success and self.admin_flag == 1 and self.permissions == PERM_ADMIN


def parse_login(frame: bytes) -> LoginResult:
    if len(frame) < 8 or frame[0] not in (PUSH_LOGIN_SUCCESS, PUSH_LOGIN_FAIL):
        raise ProtocolError(f"bad login push ({len(frame)} bytes)")
    prefix = bytes(frame[2:8])
    if frame[0] == PUSH_LOGIN_FAIL:
        return LoginResult(False, prefix, 0, None, None, None)
    if len(frame) < 14:  # legacy repeater reply: 0x85, 0x00, prefix
        return LoginResult(True, prefix, frame[1], None, None, None)
    return LoginResult(True, prefix, frame[1], frame[12], struct.unpack_from("<I", frame, 8)[0], frame[13])


@dataclass(frozen=True)
class ContactMessage:
    """RESP_CODE_CONTACT_MSG_RECV / _V3: a message from a contact, for a repeater the reply to a CLI command."""

    prefix: bytes  # first 6 bytes of the sender's key
    path_len: int  # 0xFF when it arrived by a direct route, else the encoded length of the flooded path
    txt_type: int
    timestamp: int  # the sender's clock
    text: str
    snr_x4: int | None  # SNR at the companion; None in the version 1 frame

    @property
    def is_cli_data(self) -> bool:
        return self.txt_type == TXT_TYPE_CLI_DATA


def parse_contact_message(frame: bytes) -> ContactMessage:
    if frame and frame[0] == RESP_CONTACT_MSG_RECV_V3 and len(frame) >= 16:
        snr, = struct.unpack_from("<b", frame, 1)
        start = 4
    elif frame and frame[0] == RESP_CONTACT_MSG_RECV and len(frame) >= 13:
        snr, start = None, 1
    else:
        raise ProtocolError(f"bad contact message frame ({len(frame)} bytes)")
    ts, = struct.unpack_from("<I", frame, start + 8)
    return ContactMessage(
        prefix=bytes(frame[start : start + 6]),
        path_len=frame[start + 6],
        txt_type=frame[start + 7],
        timestamp=ts,
        text=bytes(frame[start + 12 :]).decode("utf-8", "replace"),
        snr_x4=snr,
    )


def build_contact_message(
    prefix: bytes, text: str, path_len: int = 0xFF, txt_type: int = TXT_TYPE_CLI_DATA, timestamp: int = 0, snr_x4: int | None = 20,
) -> bytes:
    """The inverse of parse_contact_message, for the fake companion: version 3 when an SNR is given, else version 1."""
    body = prefix[:6] + bytes([path_len, txt_type]) + struct.pack("<I", timestamp) + text.encode("utf-8")
    if snr_x4 is None:
        return bytes([RESP_CONTACT_MSG_RECV]) + body
    return struct.pack("<BbBB", RESP_CONTACT_MSG_RECV_V3, snr_x4, 0, 0) + body


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


@dataclass(frozen=True)
class Contact:
    """A node the companion has heard an advert from. A repeater appears here with its key, advertised name and position."""

    public_key: bytes
    adv_type: int
    name: str
    advert_timestamp: int
    lat: float | None  # degrees; None if the frame has no position field. 0.0, 0.0 means the node did not set one
    lon: float | None


def parse_contact(frame: bytes) -> Contact:
    """RESP_CODE_CONTACT and PUSH_CODE_NEW_ADVERT share this layout:
    code, key(32), type, flags, out_path_len, out_path(64), name(32), advert timestamp(4), lat(4), lon(4), lastmod(4);
    lat and lon are int32 degrees x 1e6."""
    if len(frame) < 136 or frame[0] not in (RESP_CONTACT, PUSH_NEW_ADVERT):
        raise ProtocolError(f"bad contact frame ({len(frame)} bytes)")
    lat = lon = None
    if len(frame) >= 144:
        raw_lat, raw_lon = struct.unpack_from("<ii", frame, 136)
        lat, lon = raw_lat / 1e6, raw_lon / 1e6
    return Contact(
        public_key=bytes(frame[1 : 1 + PUBKEY_LEN]),
        adv_type=frame[33],
        name=_cstr(bytes(frame[100:132])),
        advert_timestamp=struct.unpack_from("<I", frame, 132)[0],
        lat=lat,
        lon=lon,
    )


def parse_bare_advert(frame: bytes) -> bytes:
    """PUSH_CODE_ADVERT: the public key of a node the companion already has as a contact."""
    if len(frame) < 1 + PUBKEY_LEN or frame[0] != PUSH_ADVERT:
        raise ProtocolError(f"bad advert push ({len(frame)} bytes)")
    return bytes(frame[1 : 1 + PUBKEY_LEN])


def build_contact(
    code: int, public_key: bytes, adv_type: int, name: str, advert_timestamp: int = 0,
    lat: float = 0.0, lon: float = 0.0, lastmod: int = 0,
) -> bytes:
    """The inverse of parse_contact, for the fake companion."""
    return (
        bytes([code])
        + public_key
        + bytes([adv_type, 0, 0xFF])
        + bytes(64)
        + name.encode("utf-8")[:31].ljust(32, b"\0")
        + struct.pack("<IiiI", advert_timestamp, round(lat * 1e6), round(lon * 1e6), lastmod)
    )
