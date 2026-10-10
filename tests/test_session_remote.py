"""The session's part of remote administration: moving the companion's clock forward, and handing login pushes and contact messages on."""

import dataclasses
import logging
import struct
import threading
import time

import pytest

from beacon_base import companion, ingest
from beacon_base.fake_companion import FakeCompanion
from beacon_base.ingest import CompanionSession
from test_session import Collector, make_config

RKEY = bytes(range(1, 33))


def until(predicate, timeout=8.0):
    """Poll: the session sets plain attributes, which do not notify the collector's condition."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the session")
        time.sleep(0.02)


class RemoteCollector(Collector):
    def __init__(self, trusted=True):
        super().__init__()
        self.trusted = trusted
        self.logins = []
        self.messages = []

    def clock_trusted(self):
        return self.trusted

    def on_login(self, result):
        self._note(lambda: self.logins.append(result))

    def on_contact_message(self, message):
        self._note(lambda: self.messages.append(message))


class Running:
    def __init__(self, config, handler):
        self.handler = handler
        self.session = CompanionSession(config, handler)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=lambda: self.session.run(self.stop), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "session did not stop"

    def synced_clock(self):
        return self.session.clock_synced


@pytest.fixture
def fake():
    with FakeCompanion() as f:  # the clock starts at the nRF52 default in May 2024
        yield f


def set_time_commands(fake):
    return [c for c in fake.commands if c[0] == companion.CMD_SET_DEVICE_TIME]


def test_an_untrusted_base_clock_is_never_pushed_onto_the_companion(fake):
    with Running(make_config(fake), RemoteCollector(trusted=False)) as run:
        run.handler.wait(lambda: run.handler.synced == 1)
        time.sleep(0.3)
    assert not set_time_commands(fake) and not run.session.clock_synced
    assert fake.rtc() < 1_720_000_000  # still May 2024


def test_a_trusted_base_clock_moves_a_behind_companion_forward(fake):
    with Running(make_config(fake), RemoteCollector()) as run:
        until(lambda: run.session.clock_synced)
    assert len(set_time_commands(fake)) == 1
    assert abs(fake.rtc() - time.time()) <= 2
    assert run.session.companion_clock_offset == 0


def test_a_companion_that_is_close_is_left_alone(fake):
    fake.set_rtc(int(time.time()) - 1)
    with Running(make_config(fake), RemoteCollector()) as run:
        until(lambda: run.session.clock_synced)
    assert not set_time_commands(fake)
    assert run.session.companion_clock_offset in (-1, 0, -2)


def test_a_companion_that_is_ahead_is_reported_not_set(fake, caplog):
    ahead = int(time.time()) + 500
    fake.set_rtc(ahead)
    with caplog.at_level(logging.WARNING, logger="beacon_base.ingest"):
        with Running(make_config(fake), RemoteCollector()) as run:
            until(lambda: run.session.clock_synced)
    assert not set_time_commands(fake) and fake.rtc() >= ahead  # the command would be refused; it is not even tried
    assert 495 <= run.session.companion_clock_offset <= 505
    assert any("ahead of the base" in r.message and "reboot the companion" in r.message for r in caplog.records)


def test_the_clock_is_synced_once_the_base_clock_becomes_trusted(fake, monkeypatch):
    monkeypatch.setattr(ingest, "CLOCK_RECHECK_S", 0.2)
    handler = RemoteCollector(trusted=False)
    with Running(make_config(fake), handler) as run:
        handler.wait(lambda: handler.synced == 1)
        time.sleep(0.5)
        assert not run.session.clock_synced and not set_time_commands(fake)
        handler.trusted = True  # beaconctl time set
        until(lambda: run.session.clock_synced)
    assert len(set_time_commands(fake)) == 1


def test_sync_clock_can_be_switched_off(fake):
    cfg = make_config(fake)
    cfg = dataclasses.replace(cfg, companion=dataclasses.replace(cfg.companion, sync_clock=False))
    with Running(cfg, RemoteCollector()) as run:
        run.handler.wait(lambda: run.handler.synced == 1)
        time.sleep(0.3)
    assert not set_time_commands(fake) and not run.session.clock_synced


def test_a_rebooted_companion_gets_its_clock_again(fake):
    handler = RemoteCollector()
    with Running(make_config(fake), handler) as run:
        until(lambda: run.session.clock_synced)
        fake.reconnect(reboot=True)  # the clock is back in 2024
        until(lambda: handler.connects == 2 and run.session.clock_synced and abs(fake.rtc() - time.time()) <= 2)
    assert len(set_time_commands(fake)) == 2


def test_a_clock_that_survives_a_reboot_is_not_set_again(fake):
    fake.rtc_persistent = True
    handler = RemoteCollector()
    with Running(make_config(fake), handler) as run:
        until(lambda: run.session.clock_synced)
        fake.reconnect(reboot=True)
        until(lambda: handler.connects == 2 and run.session.clock_synced)
    assert len(set_time_commands(fake)) == 1


def test_login_pushes_and_cli_replies_reach_the_handler(fake):
    handler = RemoteCollector()
    with Running(make_config(fake), handler) as run:
        handler.wait(lambda: handler.synced == 1)
        fake.write_frame(
            bytes([companion.PUSH_LOGIN_SUCCESS, 1]) + RKEY[:6] + struct.pack("<I", 1_790_000_000) + bytes([3, 11])
        )
        fake.enqueue_frame(companion.build_contact_message(RKEY, "7f|> ridge", timestamp=5))
        handler.wait(lambda: handler.logins and handler.messages)
    assert handler.logins[0].admin and handler.logins[0].prefix == RKEY[:6]
    assert (handler.messages[0].text, handler.messages[0].prefix) == ("7f|> ridge", RKEY[:6])
    assert not handler.drops  # not counted as unrelated traffic
    assert run.session.stats["contact_messages"] == 1 and run.session.stats["logins"] == 1


def test_a_malformed_login_push_or_contact_message_is_counted_not_fatal(fake):
    handler = RemoteCollector()
    with Running(make_config(fake), handler) as run:
        handler.wait(lambda: handler.synced == 1)
        fake.write_frame(bytes([companion.PUSH_LOGIN_SUCCESS, 1, 2]))
        fake.enqueue_frame(bytes([companion.RESP_CONTACT_MSG_RECV_V3]) + bytes(4))
        fake.enqueue_report(b"")  # something after, to know the queue was read
        handler.wait(lambda: handler.drops)
    assert run.session.stats["bad_login"] == 1
    assert [d[0] for d in handler.drops][:1] == ["bad_frame"]
    assert not handler.logins and not handler.messages
