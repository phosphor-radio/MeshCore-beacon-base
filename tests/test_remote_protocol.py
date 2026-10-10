"""Remote administration frames: command builders and response parsers (pure, no I/O)."""

import struct

import pytest

from beacon_base import companion

KEY = bytes(range(32))


def test_add_update_contact_is_a_full_contact_frame_with_no_route():
    frame = companion.add_update_contact(KEY, companion.ADV_TYPE_REPEATER, "ridge", 47.5, -122.25)
    assert len(frame) == 148 and frame[0] == companion.CMD_ADD_UPDATE_CONTACT  # the firmware reads 136 bytes whatever it checks
    assert frame[1:33] == KEY and frame[33] == companion.ADV_TYPE_REPEATER
    assert frame[35] == 0xFF  # path length unknown: messages are flooded
    contact = companion.parse_contact(bytes([companion.RESP_CONTACT]) + frame[1:])
    assert (contact.name, contact.lat, contact.lon) == ("ridge", 47.5, -122.25)
    with pytest.raises(ValueError):
        companion.add_update_contact(KEY[:8], 2, "x")


def test_reset_path_and_device_time_builders():
    assert companion.reset_path(KEY) == bytes([13]) + KEY
    assert companion.get_device_time() == bytes([5])
    assert companion.set_device_time(1_790_000_000) == bytes([6]) + struct.pack("<I", 1_790_000_000)
    with pytest.raises(ValueError):
        companion.reset_path(KEY[:6])


def test_login_frame_and_password_limits():
    assert companion.send_login(KEY) == bytes([26]) + KEY  # empty password is allowed
    assert companion.send_login(KEY, "secret") == bytes([26]) + KEY + b"secret"
    assert companion.send_login(KEY, "x" * 15)  # the longest the firmware keeps
    with pytest.raises(ValueError, match="at most 15"):
        companion.send_login(KEY, "x" * 16)
    with pytest.raises(ValueError, match="at most 15"):
        companion.send_login(KEY, "é" * 8)  # 16 bytes
    with pytest.raises(ValueError, match="NUL"):
        companion.send_login(KEY, "a\0b")


def test_cli_frame_layout_and_length_limit():
    frame = companion.send_cli(KEY, "7f|get name")
    assert frame[:3] == bytes([2, 1, 0])  # send text, CLI_DATA, attempt 0
    assert frame[3:7] == bytes(4)  # timestamp, replaced by the companion's clock
    assert frame[7:13] == KEY[:6] and frame[13:] == b"7f|get name"
    assert companion.send_cli(KEY, "a" * 160)
    with pytest.raises(ValueError):
        companion.send_cli(KEY, "a" * 161)  # the companion would answer 'table full'
    with pytest.raises(ValueError):
        companion.send_cli(KEY, "")


def test_tags():
    assert companion.make_tag(7) == "07" and companion.make_tag(0x1AB) == "ab"
    assert companion.tag_command("7f", "get name") == "7f|get name"
    assert companion.split_tag("7f|> name") == ("7f", "> name")
    assert companion.split_tag("> name") == (None, "> name")
    assert companion.split_tag("ab") == (None, "ab")
    for bad in ("a", "abc", "a|", " a"):
        with pytest.raises(ValueError):
            companion.tag_command(bad, "x")


def test_parse_sent():
    sent = companion.parse_sent(bytes([6, 1]) + KEY[:4] + struct.pack("<I", 4500))
    assert sent == companion.Sent(True, KEY[:4], 4500)
    assert not companion.parse_sent(bytes([6, 0]) + bytes(4) + struct.pack("<I", 1)).flooded
    with pytest.raises(companion.ProtocolError):
        companion.parse_sent(bytes([6, 1, 0]))


def test_parse_device_time():
    assert companion.parse_device_time(bytes([9]) + struct.pack("<I", 1715770351)) == 1715770351
    with pytest.raises(companion.ProtocolError):
        companion.parse_device_time(bytes([9, 1]))


def login_push(admin_flag, perms, code=companion.PUSH_LOGIN_SUCCESS):
    return bytes([code, admin_flag]) + KEY[:6] + struct.pack("<I", 1790000000) + bytes([perms, 11])


def test_login_is_admin_only_when_both_bytes_say_so():
    admin = companion.parse_login(login_push(1, 3))
    assert admin.admin and admin.success and admin.permissions == 3 and admin.server_time == 1790000000 and admin.firmware_level == 11
    assert admin.prefix == KEY[:6]
    assert not companion.parse_login(login_push(0, 0)).admin  # a guest: success, but not admin
    assert not companion.parse_login(login_push(1, 2)).admin  # read-write is not admin
    assert not companion.parse_login(login_push(0, 3)).admin
    assert companion.parse_login(login_push(0, 0)).success


def test_legacy_and_failed_login_pushes_are_never_admin():
    legacy = companion.parse_login(bytes([companion.PUSH_LOGIN_SUCCESS, 0]) + KEY[:6])
    assert legacy.success and not legacy.admin and legacy.permissions is None and legacy.server_time is None
    failed = companion.parse_login(bytes([companion.PUSH_LOGIN_FAIL, 0]) + KEY[:6])
    assert not failed.success and not failed.admin
    with pytest.raises(companion.ProtocolError):
        companion.parse_login(bytes([companion.PUSH_LOGIN_SUCCESS, 1, 1, 2]))
    with pytest.raises(companion.ProtocolError):
        companion.parse_login(bytes([0x83]) + bytes(10))


def test_contact_message_version_3_and_1():
    v3 = companion.build_contact_message(KEY, "7f|> rpt", path_len=2, timestamp=1790000001, snr_x4=-12)
    msg = companion.parse_contact_message(v3)
    assert (msg.prefix, msg.path_len, msg.txt_type, msg.timestamp, msg.text, msg.snr_x4) == (KEY[:6], 2, 1, 1790000001, "7f|> rpt", -12)
    assert msg.is_cli_data
    v1 = companion.build_contact_message(KEY, "hello", snr_x4=None, txt_type=0)
    assert v1[0] == companion.RESP_CONTACT_MSG_RECV
    msg = companion.parse_contact_message(v1)
    assert (msg.text, msg.snr_x4, msg.path_len, msg.is_cli_data) == ("hello", None, 0xFF, False)


def test_contact_message_edge_cases():
    assert companion.parse_contact_message(companion.build_contact_message(KEY, "")).text == ""
    assert companion.parse_contact_message(companion.build_contact_message(KEY, "café")).text == "café"
    broken = companion.build_contact_message(KEY, "x")[:-1] + b"\xff"  # not valid UTF-8: replaced, not raised
    assert "�" in companion.parse_contact_message(broken).text
    for bad in (b"", bytes([companion.RESP_CONTACT_MSG_RECV_V3]) + bytes(10), bytes([companion.RESP_CONTACT_MSG_RECV]) + bytes(5), bytes([99]) + bytes(30)):
        with pytest.raises(companion.ProtocolError):
            companion.parse_contact_message(bad)
