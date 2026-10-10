"""Logging in to a repeater and running a tagged CLI command through the companion link, against the fake companion and repeater."""

import struct
import time

import pytest

from beacon_base import companion
from beacon_base.fake_companion import NRF52_RTC_START, FakeCompanion
from beacon_base.fake_repeater import FakeRepeater
from beacon_base.link import CommandError, CompanionLink, SerialTransport

RKEY = bytes(range(1, 33))
NOW = 1_790_000_000


@pytest.fixture
def fake():
    with FakeCompanion(rtc_start=NOW) as f:
        f.add_repeater(FakeRepeater(RKEY, name="ridge", now=lambda: NOW + 5))
        yield f


@pytest.fixture
def link(fake):
    link = CompanionLink(SerialTransport(fake.path, 115200), command_timeout=2.0)
    link.request(companion.app_start(), [companion.RESP_SELF_INFO])
    link.request(companion.device_query(), [companion.RESP_DEVICE_INFO])
    yield link
    link.close()


def add_contact(link):
    link.request(companion.add_update_contact(RKEY, companion.ADV_TYPE_REPEATER, "ridge"), [companion.RESP_OK])


def wait_frame(link, predicate, timeout=0.8):
    """The next frame (usually a push) that satisfies predicate, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = link.recv_frame(0.1)
        if frame is not None and predicate(frame):
            return frame
    return None


def login(link, password=""):
    sent = companion.parse_sent(link.request(companion.send_login(RKEY, password), [companion.RESP_SENT]))
    frame = wait_frame(link, lambda f: f[0] in (companion.PUSH_LOGIN_SUCCESS, companion.PUSH_LOGIN_FAIL))
    return sent, companion.parse_login(frame) if frame else None


def run(link, command, tag="7f"):
    """Send a tagged CLI command and return the reply text, or None when nothing came back."""
    sent = companion.parse_sent(link.request(companion.send_cli(RKEY, companion.tag_command(tag, command)), [companion.RESP_SENT]))
    assert sent.tag == bytes(4)  # no ack is expected for a CLI message
    if wait_frame(link, lambda f: f[0] == companion.PUSH_MSG_WAITING) is None:
        return None
    frame = link.request(companion.sync_next_message(), companion.MESSAGE_RESPONSES | {companion.RESP_NO_MORE_MESSAGES})
    msg = companion.parse_contact_message(frame)
    assert msg.prefix == RKEY[:6] and msg.is_cli_data and msg.snr_x4 == 20
    got_tag, text = companion.split_tag(msg.text)
    assert got_tag == tag
    return text


def test_admin_login_then_tagged_command_round_trip(link):
    add_contact(link)
    sent, result = login(link, "password")
    assert sent.flooded and sent.tag == RKEY[:4] and sent.timeout_ms == 2000  # no route known yet: flooded
    assert result.admin and result.prefix == RKEY[:6] and result.server_time == NOW + 5
    assert run(link, "get name") == "> ridge"
    assert run(link, "beacon.window") == "> 60 secs"


def test_the_first_exchange_teaches_the_route_so_later_ones_go_direct(link, fake):
    add_contact(link)
    login(link, "password")
    sent = companion.parse_sent(link.request(companion.send_cli(RKEY, "7f|get name"), [companion.RESP_SENT]))
    assert not sent.flooded
    link.request(companion.reset_path(RKEY), [companion.RESP_OK])
    sent = companion.parse_sent(link.request(companion.send_cli(RKEY, "7e|get name"), [companion.RESP_SENT]))
    assert sent.flooded


def test_empty_password_from_an_unknown_key_is_a_guest_and_gets_no_cli(link):
    add_contact(link)
    _, result = login(link, "")
    assert result.success and not result.admin and result.permissions == 0  # a success push that is not admin
    assert run(link, "get name") is None


def test_empty_password_works_again_for_an_admin_the_repeater_remembers(link, fake):
    add_contact(link)
    assert login(link, "password")[1].admin
    fake.repeaters[RKEY].reboot()
    _, result = login(link, "")
    assert result.admin
    assert run(link, "get name") == "> ridge"


def test_wrong_password_gets_only_a_timeout(link):
    add_contact(link)
    sent, result = login(link, "wrong")
    assert sent.flooded and result is None


def test_a_companion_clock_behind_the_repeaters_record_gets_no_reply_until_it_is_set_forward(link, fake):
    add_contact(link)
    assert login(link, "password")[1].admin
    fake.set_rtc(NRF52_RTC_START)  # the companion rebooted and its clock restarted in 2024
    assert login(link, "password")[1] is None
    assert run(link, "get name") is None  # the CLI is ignored too
    link.request(companion.set_device_time(NOW + 60), [companion.RESP_OK])
    assert login(link, "password")[1].admin
    assert run(link, "get name") == "> ridge"


def test_the_companion_clock_only_moves_forward(link, fake):
    now = companion.parse_device_time(link.request(companion.get_device_time(), [companion.RESP_CURR_TIME]))
    assert NOW <= now <= NOW + 3
    link.request(companion.set_device_time(now + 100), [companion.RESP_OK])
    link.request(companion.set_device_time(fake.rtc()), [companion.RESP_OK])  # equal is accepted
    with pytest.raises(CommandError, match="illegal argument"):
        link.request(companion.set_device_time(now), [companion.RESP_OK])


def test_login_and_command_need_a_contact(link):
    with pytest.raises(CommandError, match="not found"):
        link.request(companion.send_login(RKEY, "password"), [companion.RESP_SENT])
    with pytest.raises(CommandError, match="not found"):
        link.request(companion.send_cli(RKEY, "get name"), [companion.RESP_SENT])


def test_a_short_contact_frame_is_refused_by_the_fake(link):
    short = companion.add_update_contact(RKEY, 2, "ridge")[:100]
    with pytest.raises(CommandError, match="illegal argument"):
        link.request(short, [companion.RESP_OK])


def test_a_full_contact_table_is_reported(link, fake):
    fake.contacts_full = True
    with pytest.raises(CommandError, match="table full"):
        add_contact(link)


def test_text_over_160_bytes_gets_the_misleading_table_full(link):
    add_contact(link)
    raw = bytes([companion.CMD_SEND_TXT_MSG, 1, 0]) + bytes(4) + RKEY[:6] + b"a" * 161  # bypassing the builder's check
    with pytest.raises(CommandError, match="table full"):
        link.request(raw, [companion.RESP_SENT])


def test_unsupported_text_type_is_refused(link):
    add_contact(link)
    raw = bytes([companion.CMD_SEND_TXT_MSG, 2, 0]) + bytes(4) + RKEY[:6] + b"x"  # signed plain text
    with pytest.raises(CommandError, match="unsupported"):
        link.request(raw, [companion.RESP_SENT])


def test_a_lost_reply_and_an_unreachable_repeater_are_timeouts(link, fake):
    add_contact(link)
    assert login(link, "password")[1].admin
    rpt = fake.repeaters[RKEY]
    rpt.drop_replies = True
    assert run(link, "get name") is None
    rpt.drop_replies = False
    rpt.reachable = False
    assert run(link, "get name", tag="7e") is None
    assert login(link, "password")[1] is None


def test_floods_can_be_dropped_while_a_stored_route_still_works(link, fake):
    add_contact(link)
    rpt = fake.repeaters[RKEY]
    assert login(link, "password")[1].admin  # route learned
    rpt.drop_floods = True
    assert run(link, "get name") == "> ridge"  # direct
    link.request(companion.reset_path(RKEY), [companion.RESP_OK])
    assert run(link, "get name", tag="7e") is None  # flooded now, and the mesh drops it


def test_version_1_message_frames_before_protocol_version_3(fake):
    link = CompanionLink(SerialTransport(fake.path, 115200), command_timeout=2.0)
    try:
        link.request(companion.app_start(), [companion.RESP_SELF_INFO])  # no DEVICE_QUERY: app version stays 0
        add_contact(link)
        assert login(link, "password")[1].admin
        link.request(companion.send_cli(RKEY, "7f|get name"), [companion.RESP_SENT])
        assert wait_frame(link, lambda f: f[0] == companion.PUSH_MSG_WAITING)
        frame = link.request(companion.sync_next_message(), companion.MESSAGE_RESPONSES)
        assert frame[0] == companion.RESP_CONTACT_MSG_RECV
        assert companion.parse_contact_message(frame).text == "7f|> ridge"
    finally:
        link.close()


def test_consecutive_commands_get_distinct_timestamps(link, fake):
    add_contact(link)
    login(link, "password")
    stamps = []
    for n in range(3):
        link.request(companion.send_cli(RKEY, f"0{n}|get name"), [companion.RESP_SENT])
        assert wait_frame(link, lambda f: f[0] == companion.PUSH_MSG_WAITING)
        msg = companion.parse_contact_message(link.request(companion.sync_next_message(), companion.MESSAGE_RESPONSES))
        stamps.append(msg.text)
    assert stamps == ["00|> ridge", "01|> ridge", "02|> ridge"]  # none treated as a retry of the one before
    assert fake.repeaters[RKEY].commands == ["get name"] * 3
