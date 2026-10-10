"""CompanionSession against a fake companion on a pty."""

import dataclasses
import threading
import time

import pytest

from beacon_base import companion, wire
from beacon_base.config import CompanionConfig, Config, ConfigError, RadioConfig
from beacon_base.fake_companion import FakeCompanion
from beacon_base.ingest import CompanionSession, Handler

KEY = bytes(range(16))
REPEATER = bytes(range(32))


def obs(beacon: int, counter: int) -> wire.Observation:
    return wire.Observation(bytes([beacon] * 8), counter, -90, -12, 3800)


def report(*observations: wire.Observation) -> bytes:
    return wire.encode_report(REPEATER, list(observations))


class Collector(Handler):
    def __init__(self):
        self.reports = []
        self.drops = []
        self.raw_drops = []
        self.connects = 0
        self.synced = 0
        self.disconnects = []
        self._cv = threading.Condition()

    def _note(self, fn):
        with self._cv:
            fn()
            self._cv.notify_all()

    def on_connected(self, info):
        self._note(lambda: setattr(self, "connects", self.connects + 1))

    def on_synced(self):
        self._note(lambda: setattr(self, "synced", self.synced + 1))

    def on_report(self, rx):
        self._note(lambda: self.reports.append(rx))

    def on_drop(self, reason, detail, raw=None):
        self._note(lambda: self.drops.append((reason, detail)))
        if raw is not None:
            self._note(lambda: self.raw_drops.append(raw))

    def on_disconnected(self, error):
        self._note(lambda: self.disconnects.append(error))

    def wait(self, predicate, timeout=8.0):
        deadline = time.monotonic() + timeout
        with self._cv:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError("timed out waiting for the session")
                self._cv.wait(left)


@pytest.fixture
def fake():
    with FakeCompanion() as f:
        yield f


def make_config(fake, **companion_overrides) -> Config:
    cc = CompanionConfig(port=fake.path, poll_interval=30.0, command_timeout=2.0, **companion_overrides)
    return Config(companion=cc, radio=RadioConfig(), channel_key=KEY)


class Running:
    def __init__(self, config):
        self.handler = Collector()
        self.session = CompanionSession(config, self.handler)
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self.session.run(self.stop)
        except Exception as e:  # surfaced by the test through .error
            self.error = e

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive(), "session did not stop"


def test_requires_key_and_port(fake):
    cfg = make_config(fake)
    with pytest.raises(ConfigError, match="channel key"):
        CompanionSession(dataclasses.replace(cfg, channel_key=None), Handler())
    with pytest.raises(ConfigError, match="port"):
        CompanionSession(dataclasses.replace(cfg, companion=dataclasses.replace(cfg.companion, port=None)), Handler())


def test_provisions_channel_and_decodes_a_live_report(fake):
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: run.handler.synced == 1)
        assert fake.channels[1] == ("beacon-reports", KEY)
        fake.enqueue_report(report(obs(0xA1, 7), obs(0xB2, 9)), snr_x4=22, path_len=2)
        run.handler.wait(lambda: len(run.handler.reports) == 1)
    rx = run.handler.reports[0]
    assert rx.report.repeater_id == REPEATER[:8]
    assert [(o.beacon_id[0], o.counter) for o in rx.report.observations] == [(0xA1, 7), (0xB2, 9)]
    assert rx.companion_snr_x4 == 22 and rx.path_len == 2
    assert not rx.late
    assert rx.rx_wall > 1.6e9 and rx.rx_mono > 0
    assert rx.payload == report(obs(0xA1, 7), obs(0xB2, 9))


def test_leaves_matching_channel_alone(fake):
    fake.channels[1] = ("beacon-reports", KEY)
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: run.handler.connects == 1)
    assert not [c for c in fake.commands if c[0] == companion.CMD_SET_CHANNEL]


def test_replaces_stale_channel_key(fake):
    fake.channels[1] = ("beacon-reports", bytes(16))
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: run.handler.connects == 1)
    assert fake.channels[1] == ("beacon-reports", KEY)


def test_queued_reports_are_drained_on_connect_and_marked_late(fake):
    fake.send_push_on_enqueue = False
    for n in range(3):
        fake.enqueue_report(report(obs(0xA1, n)))
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: len(run.handler.reports) == 3)
        assert [r.report.observations[0].counter for r in run.handler.reports] == [0, 1, 2]
        run.handler.wait(lambda: run.handler.synced == 1)
        assert all(r.late for r in run.handler.reports)
        fake.send_push_on_enqueue = True
        fake.enqueue_report(report(obs(0xA1, 3)))
        run.handler.wait(lambda: len(run.handler.reports) == 4)
    assert not run.handler.reports[3].late


def test_poll_picks_up_a_missed_push(fake):
    fake.send_push_on_enqueue = False
    cfg = make_config(fake)
    cfg = dataclasses.replace(cfg, companion=dataclasses.replace(cfg.companion, poll_interval=0.5))
    with Running(cfg) as run:
        run.handler.wait(lambda: run.handler.synced == 1)
        fake.enqueue_report(report(obs(0xA1, 1)))
        run.handler.wait(lambda: len(run.handler.reports) == 1)


def test_unrelated_traffic_is_counted_and_dropped(fake):
    fake.send_push_on_enqueue = False
    fake.enqueue_report(report(obs(0xA1, 1)), channel_index=2)  # another channel
    fake.enqueue_report(report(obs(0xA1, 1)), data_type=0xFF01)  # another application
    fake.enqueue_frame(bytes([companion.RESP_CHANNEL_MSG_RECV_V3]) + bytes(20))  # a text message
    fake.enqueue_report(b"\x02" + bytes(20))  # unknown report version
    fake.enqueue_report(report(obs(0xA1, 1))[:-3])  # truncated report
    fake.enqueue_frame(bytes([companion.RESP_CHANNEL_DATA_RECV, 0, 0, 0, 1, 0, 0xBE, 0xFF, 50]))  # claims 50 data bytes
    fake.enqueue_report(report(obs(0xA1, 2)))  # the one good report
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: len(run.handler.reports) == 1 and len(run.handler.drops) == 6)
    assert sorted(r for r, _ in run.handler.drops) == sorted(
        ["other_channel", "other_data_type", "other_message", "bad_report", "bad_report", "bad_frame"]
    )
    assert run.session.stats["reports"] == 1
    assert run.session.stats["dropped_bad_report"] == 2
    assert len(run.handler.raw_drops) == 2  # malformed reports come with their raw frame for the audit trail
    assert run.handler.raw_drops[0].payload.startswith(b"\x02")


def test_garbage_between_frames_is_survived(fake):
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: run.handler.synced == 1)
        fake.send_push_on_enqueue = False
        fake.enqueue_report(report(obs(0xA1, 1)))
        fake.write_raw(b"E (123) debug text > with a marker\r\n\xff\x00>")
        fake.write_frame(bytes([companion.PUSH_MSG_WAITING]))
        run.handler.wait(lambda: len(run.handler.reports) == 1)
        assert run.error is None


def test_reconnects_after_unplug_and_drains_the_queue_as_late(fake):
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: run.handler.connects == 1)
        fake.send_push_on_enqueue = False
        fake.disconnect()
        run.handler.wait(lambda: len(run.handler.disconnects) == 1)
        fake.enqueue_report(report(obs(0xA1, 5)))  # arrives over the air while the host is away
        fake.reconnect()
        run.handler.wait(lambda: run.handler.connects == 2 and len(run.handler.reports) == 1)
        assert run.handler.reports[0].late
        assert run.error is None


def test_reprovisions_after_companion_loses_its_channel(fake):
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: run.handler.connects == 1)
        fake.disconnect()
        run.handler.wait(lambda: len(run.handler.disconnects) == 1)
        del fake.channels[1]
        fake.reconnect(reboot=True)
        run.handler.wait(lambda: run.handler.connects == 2)
        assert fake.channels[1] == ("beacon-reports", KEY)


def test_missing_port_is_retried(tmp_path):
    cfg = Config(companion=CompanionConfig(port=str(tmp_path / "absent")), radio=RadioConfig(), channel_key=KEY)
    run = Running(cfg)
    with run:
        time.sleep(0.5)  # at least one failed attempt, then stop() must end the backoff wait promptly
    assert run.error is None


def test_channel_index_beyond_companion_slots_is_fatal(fake):
    fake.max_channels = 1
    run = Running(make_config(fake))
    run.thread.start()
    run.thread.join(timeout=5)
    assert isinstance(run.error, ConfigError)
    assert "out of range" in str(run.error)


def test_radio_mismatch_is_left_alone_by_default(fake):
    fake.radio = (869525, 250000, 11, 5)
    with Running(make_config(fake)) as run:
        run.handler.wait(lambda: run.handler.connects == 1)
    assert fake.radio == (869525, 250000, 11, 5)
    assert not [c for c in fake.commands if c[0] == companion.CMD_SET_RADIO_PARAMS]


def test_manage_radio_applies_settings(fake):
    fake.radio = (869525, 250000, 11, 5)
    with Running(make_config(fake, manage_radio=True)) as run:
        run.handler.wait(lambda: run.handler.connects == 1)
    assert fake.radio == (905775, 62500, 8, 6)


def test_radio_frequency_off_by_one_khz_counts_as_matching(fake):
    fake.radio = (905774, 62500, 8, 6)  # the companion's float frequency can truncate
    with Running(make_config(fake, manage_radio=True)) as run:
        run.handler.wait(lambda: run.handler.connects == 1)
    assert not [c for c in fake.commands if c[0] == companion.CMD_SET_RADIO_PARAMS]
