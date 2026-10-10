import struct

import pytest

from beacon_base import companion
from beacon_base.companion import ProtocolError


def test_command_builders():
    assert companion.app_start() == bytes([1]) + bytes(7) + b"beacon-base"
    assert companion.device_query() == bytes([0x16, 0x03])
    assert companion.get_channel(1) == bytes([0x1F, 1])
    assert companion.sync_next_message() == bytes([0x0A])
    # 905775 kHz, 62500 Hz, SF8, CR 6
    assert companion.set_radio_params(905775, 62500, 8, 6) == bytes.fromhex("0b2fd20d0024f400000806")


def test_set_channel_layout():
    frame = companion.set_channel(2, "beacon-reports", bytes(range(16)))
    assert len(frame) == 50
    assert frame[:2] == bytes([0x20, 2])
    assert frame[2:34] == b"beacon-reports".ljust(32, b"\0")
    assert frame[34:] == bytes(range(16))


def test_set_channel_validates():
    with pytest.raises(ValueError):
        companion.set_channel(1, "x" * 32, bytes(16))  # needs room for the terminating NUL
    with pytest.raises(ValueError):
        companion.set_channel(1, "ok", bytes(32))  # only 128-bit secrets are supported


def test_parse_channel_data():
    payload = bytes.fromhex("0102030405")
    frame = bytes([0x1B, 0xEC, 0, 0, 1, 3, 0xBE, 0xFF, 5]) + payload
    data = companion.parse_channel_data(frame)
    assert data.snr_x4 == -20  # 0xEC as int8
    assert data.channel_index == 1
    assert data.path_len == 3
    assert data.data_type == 0xFFBE
    assert data.payload == payload


def test_parse_channel_data_direct_and_trailing_bytes():
    frame = bytes([0x1B, 8, 0, 0, 0, 0xFF, 0x01, 0x01, 1, 0xAA, 0xBB])
    data = companion.parse_channel_data(frame)
    assert data.path_len == 0xFF
    assert data.payload == b"\xaa"  # bytes beyond the length byte are ignored


def test_parse_channel_data_rejects_malformed():
    for frame in (b"", bytes([0x1B]), bytes([0x1B] + [0] * 7), bytes([0x1B, 0, 0, 0, 1, 1, 1, 1, 5, 1, 2]), bytes([0x08] + [0] * 20)):
        with pytest.raises(ProtocolError):
            companion.parse_channel_data(frame)


def test_build_channel_data_round_trip():
    frame = companion.build_channel_data(-7, 3, 0xFF, 0xFFBE, b"hello")
    data = companion.parse_channel_data(frame)
    assert (data.snr_x4, data.channel_index, data.path_len, data.data_type, data.payload) == (-7, 3, 0xFF, 0xFFBE, b"hello")


def test_parse_self_info():
    key = bytes(range(32))
    frame = bytes([5, 1, 22, 22]) + key + struct.pack("<iiBBBB", 0, 0, 0, 0, 0, 0)
    frame += struct.pack("<IIBB", 905775, 62500, 8, 6) + b"base\0"
    info = companion.parse_self_info(frame)
    assert info.public_key == key
    assert (info.freq_khz, info.bw_hz, info.sf, info.cr) == (905775, 62500, 8, 6)
    assert info.name == "base"
    with pytest.raises(ProtocolError):
        companion.parse_self_info(frame[:57])


def test_parse_device_info():
    frame = bytes([13, 10, 50, 40]) + bytes(4) + b"08 Oct".ljust(12, b"\0") + b"Model".ljust(40, b"\0") + b"v1.2".ljust(20, b"\0")
    info = companion.parse_device_info(frame)
    assert (info.fw_ver, info.max_channels, info.model, info.version, info.build) == (10, 40, "Model", "v1.2", "08 Oct")
    with pytest.raises(ProtocolError):
        companion.parse_device_info(frame[:50])
    with pytest.raises(ProtocolError):
        companion.parse_device_info(bytes([13, 2, 0, 0]))


def test_parse_channel_info():
    frame = bytes([18, 4]) + b"name".ljust(32, b"\0") + bytes(range(16))
    info = companion.parse_channel_info(frame)
    assert (info.index, info.name, info.secret) == (4, "name", bytes(range(16)))
    with pytest.raises(ProtocolError):
        companion.parse_channel_info(frame[:-1])


def test_error_description():
    assert "not found" in companion.describe_error(2)
    assert "unknown" in companion.describe_error(99)
    assert companion.error_code(bytes([1])) is None
    assert companion.error_code(bytes([1, 6])) == 6


def test_push_and_plausible_codes():
    assert companion.is_push(0x83) and not companion.is_push(0x1B)
    assert companion.plausible_code(0x1B) and companion.plausible_code(0x83)
    assert not companion.plausible_code(0x20) and not companion.plausible_code(0xFF) and not companion.plausible_code(0x7F)


def test_contact_parsing_and_round_trip():
    key = bytes(range(32))
    frame = companion.build_contact(companion.RESP_CONTACT, key, companion.ADV_TYPE_REPEATER, "North Ridge", 4242, 40.123456, -75.654321, 9)
    assert len(frame) == companion.CONTACT_FRAME_LEN
    c = companion.parse_contact(frame)
    assert (c.public_key, c.adv_type, c.name, c.advert_timestamp) == (key, 2, "North Ridge", 4242)
    assert (c.lat, c.lon) == (40.123456, -75.654321)
    pushed = companion.build_contact(companion.PUSH_NEW_ADVERT, key, 2, "x", 1, -33.8, 151.2)
    assert (companion.parse_contact(pushed).lat, companion.parse_contact(pushed).lon) == (-33.8, 151.2)


def test_contact_layout_matches_the_firmware():
    # hand-assembled from writeContactRespFrame: code, key, type, flags, path_len, path[64], name[32], ts, lat, lon, lastmod
    key = bytes(range(1, 33))
    frame = (bytes([3]) + key + bytes([2, 0, 255]) + bytes(64) + b"tag".ljust(32, b"\0") + (7).to_bytes(4, "little")
             + (40_000_000).to_bytes(4, "little", signed=True) + (-75_500_000).to_bytes(4, "little", signed=True) + bytes(4))
    c = companion.parse_contact(frame)
    assert (c.name, c.advert_timestamp, c.adv_type, c.lat, c.lon) == ("tag", 7, 2, 40.0, -75.5)


def test_a_contact_frame_without_the_position_fields_has_none():
    frame = companion.build_contact(companion.RESP_CONTACT, bytes(32), 2, "x")[:140]
    c = companion.parse_contact(frame)
    assert c.lat is None and c.lon is None


def test_parse_contact_rejects_bad_frames():
    for frame in (b"", bytes([3]) + bytes(40), bytes([companion.RESP_OK]) + bytes(147)):
        with pytest.raises(ProtocolError):
            companion.parse_contact(frame)


def test_bare_advert_and_contact_commands():
    key = bytes(range(32))
    assert companion.parse_bare_advert(bytes([0x80]) + key) == key
    with pytest.raises(ProtocolError):
        companion.parse_bare_advert(bytes([0x80]) + key[:5])
    assert companion.get_contacts() == bytes([4])
    assert companion.get_contact_by_key(key) == bytes([30]) + key
    with pytest.raises(ValueError):
        companion.get_contact_by_key(key[:8])


def test_set_manual_add_contacts_leaves_the_other_settings_alone():
    # CMD_SET_OTHER_PARAMS has optional telemetry/location bytes after the first; sending only the first keeps them
    assert companion.set_manual_add_contacts(True) == bytes([38, 1])
    assert companion.set_manual_add_contacts(False) == bytes([38, 0])


def test_self_info_reports_manual_add_mode():
    frame = bytes([5, 1, 22, 22]) + bytes(32) + bytes(4 + 4 + 3) + bytes([1]) + struct.pack("<IIBB", 905775, 62500, 8, 6) + b"x"
    assert companion.parse_self_info(frame).manual_add_contacts == 1
