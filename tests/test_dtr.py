"""DTR handling: an nRF52 companion (Adafruit TinyUSB) only sends while the host holds DTR high, an ESP32-S3 can reset if it
is raised. No hardware: the fake companion drops its replies according to the DTR it is told the host opened the port with."""

import threading
import time

import pytest

from beacon_base import link as link_mod
from beacon_base.config import CompanionConfig, Config, ConfigError, RadioConfig, load_config
from beacon_base.fake_companion import FakeCompanion
from beacon_base.ingest import CompanionSession, Handler
from beacon_base.link import ESPRESSIF_VID, SerialTransport, choose_dtr

KEY = bytes(range(16))
NRF52_VID = 0x239A  # Adafruit
IKOKA_VID = 0x1209


# --- choosing DTR ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("vid, expected", [(ESPRESSIF_VID, False), (NRF52_VID, True), (IKOKA_VID, True), (0x0403, True), (None, True)])
def test_auto_keeps_dtr_low_for_espressif_and_raises_it_for_everything_else(monkeypatch, vid, expected):
    monkeypatch.setattr(link_mod, "usb_vendor_id", lambda path: vid)
    dtr, why = choose_dtr("/dev/serial/by-id/x")
    assert dtr is expected and why.startswith("auto")


def test_explicit_modes_ignore_the_vendor(monkeypatch):
    monkeypatch.setattr(link_mod, "usb_vendor_id", lambda path: pytest.fail("must not be looked up"))
    assert choose_dtr("/dev/x", "on")[0] is True
    assert choose_dtr("/dev/x", "off")[0] is False


def test_the_vendor_is_found_through_a_by_id_link(tmp_path, monkeypatch):
    from serial.tools import list_ports

    class Info:
        def __init__(self, device, vid):
            self.device, self.vid = device, vid

    real = tmp_path / "ttyACM7"
    real.write_text("")
    by_id = tmp_path / "usb-Espressif-if00"
    by_id.symlink_to(real)
    monkeypatch.setattr(list_ports, "comports", lambda *a, **k: [Info(str(tmp_path / "ttyACM1"), 1), Info(str(real), ESPRESSIF_VID)])
    assert link_mod.usb_vendor_id(str(by_id)) == ESPRESSIF_VID
    assert link_mod.usb_vendor_id(str(tmp_path / "missing")) is None


def test_a_port_that_is_not_a_usb_device_has_no_vendor():
    with FakeCompanion() as fake:
        assert link_mod.usb_vendor_id(fake.path) is None  # a pty: auto then raises DTR


def test_a_failing_port_listing_is_not_an_error(monkeypatch):
    from serial.tools import list_ports

    def boom(*a, **k):
        raise RuntimeError("no sysfs")

    monkeypatch.setattr(list_ports, "comports", boom)
    assert link_mod.usb_vendor_id("/dev/ttyACM0") is None


# --- config ---------------------------------------------------------------------------------------------------------------


def test_dtr_defaults_to_auto_and_accepts_the_three_modes(tmp_path):
    assert CompanionConfig().dtr == "auto"
    for mode in ("auto", "on", "off"):
        p = tmp_path / f"{mode}.toml"
        p.write_text(f'[companion]\ndtr = "{mode}"\n')
        assert load_config(p).companion.dtr == mode


@pytest.mark.parametrize("bad", ['"yes"', '"AUTO"', '""', "true", "1", '"high"'])
def test_bad_dtr_values_are_rejected(tmp_path, bad):
    p = tmp_path / "c.toml"
    p.write_text(f"[companion]\ndtr = {bad}\n")
    with pytest.raises(ConfigError, match="companion.dtr"):
        load_config(p)


# --- the session ----------------------------------------------------------------------------------------------------------


class Collector(Handler):
    def __init__(self):
        self.connects = 0
        self.disconnects = []
        self._cv = threading.Condition()

    def _bump(self, fn):
        with self._cv:
            fn()
            self._cv.notify_all()

    def on_connected(self, info):
        self._bump(lambda: setattr(self, "connects", self.connects + 1))

    def on_disconnected(self, error):
        self._bump(lambda: self.disconnects.append(error))

    def wait(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self._cv:
            while not predicate():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError("timed out")
                self._cv.wait(left)


class Harness:
    def __init__(self, fake, dtr_mode="auto", vid=None, monkeypatch=None):
        self.fake = fake
        self.opened = []  # the DTR each open asked for
        if monkeypatch is not None:
            monkeypatch.setattr(link_mod, "usb_vendor_id", lambda path: vid)
        cfg = Config(
            companion=CompanionConfig(port=fake.path, dtr=dtr_mode, command_timeout=0.4, learn_repeaters=False),
            radio=RadioConfig(),
            channel_key=KEY,
        )
        self.handler = Collector()
        self.session = CompanionSession(cfg, self.handler, transport_factory=self._open)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.session.run, args=(self.stop,), daemon=True)

    def _open(self, path, dtr):
        self.opened.append(dtr)
        self.fake.set_host_dtr(dtr)  # a pty cannot carry DTR, so tell the fake what the host asked for
        return SerialTransport(path, dtr=dtr)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


@pytest.fixture
def nrf52():
    """An nRF52 companion: it stays silent unless the host holds DTR high."""
    with FakeCompanion() as fake:
        fake.drop_replies_when_dtr = False
        yield fake


@pytest.fixture
def esp32():
    """An ESP32-S3 companion: it answers whatever DTR is (it would reset if DTR were raised, which a pty cannot show)."""
    with FakeCompanion() as fake:
        yield fake


def test_auto_raises_dtr_for_a_non_espressif_companion_and_it_answers_first_time(nrf52, monkeypatch):
    with Harness(nrf52, vid=IKOKA_VID, monkeypatch=monkeypatch) as h:
        h.handler.wait(lambda: h.handler.connects == 1)
    assert h.opened == [True]  # no fallback was needed


def test_auto_keeps_dtr_low_for_espressif(esp32, monkeypatch):
    with Harness(esp32, vid=ESPRESSIF_VID, monkeypatch=monkeypatch) as h:
        h.handler.wait(lambda: h.handler.connects == 1)
    assert h.opened == [False]


def test_auto_falls_back_to_the_other_setting_when_the_first_gets_no_reply(nrf52, monkeypatch, caplog):
    caplog.set_level("INFO", logger="beacon_base.ingest")
    with Harness(nrf52, vid=ESPRESSIF_VID, monkeypatch=monkeypatch) as h:  # guessed Espressif, but it is an nRF52
        h.handler.wait(lambda: h.handler.connects == 1)
        assert h.handler.disconnects == []  # the aborted first attempt never counted as a connection
    assert h.opened == [False, True]
    text = caplog.text
    assert "DTR low" in text and "reopening with DTR high" in text and "keeping it" in text


def test_the_setting_that_worked_is_kept_for_later_reconnects(nrf52, monkeypatch):
    with Harness(nrf52, vid=ESPRESSIF_VID, monkeypatch=monkeypatch) as h:
        h.handler.wait(lambda: h.handler.connects == 1)
        nrf52.disconnect()
        h.handler.wait(lambda: len(h.handler.disconnects) == 1)
        nrf52.reconnect()
        h.handler.wait(lambda: h.handler.connects == 2)
    assert h.opened == [False, True, True]  # the second connection went straight to DTR high


def test_the_fallback_works_the_other_way_too(monkeypatch):
    with FakeCompanion() as fake:
        fake.drop_replies_when_dtr = True  # a board that wants DTR low although it is not Espressif
        with Harness(fake, vid=IKOKA_VID, monkeypatch=monkeypatch) as h:
            h.handler.wait(lambda: h.handler.connects == 1)
    assert h.opened == [True, False]


@pytest.mark.parametrize("mode, silent_when", [("off", False), ("on", True)])
def test_explicit_modes_never_fall_back(mode, silent_when, monkeypatch):
    with FakeCompanion() as fake:
        fake.drop_replies_when_dtr = silent_when  # silent with exactly the setting that was asked for
        with Harness(fake, dtr_mode=mode, vid=NRF52_VID, monkeypatch=monkeypatch) as h:
            h.handler.wait(lambda: len(h.handler.disconnects) == 1)
        assert h.handler.connects == 0
        assert "no reply to command 1" in h.handler.disconnects[0]
    assert set(h.opened) == {mode == "on"} and len(h.opened) == 1


def test_explicit_off_logs_what_to_try(monkeypatch, caplog):
    with FakeCompanion() as fake:
        fake.drop_replies_when_dtr = False
        with Harness(fake, dtr_mode="off", vid=NRF52_VID, monkeypatch=monkeypatch) as h:
            h.handler.wait(lambda: len(h.handler.disconnects) == 1)
    assert "companion.dtr = on" in caplog.text and "docs/operations.md" in caplog.text


def test_when_neither_setting_works_the_attempt_ends_and_the_next_one_starts_over(monkeypatch):
    with FakeCompanion() as fake:
        fake.silent = True
        with Harness(fake, vid=NRF52_VID, monkeypatch=monkeypatch) as h:
            h.handler.wait(lambda: len(h.handler.disconnects) == 1)
        assert h.handler.connects == 0
    assert h.opened[:2] == [True, False]  # tried the guess, then the opposite, then gave up and backed off


def test_silent_companion_after_a_good_connection_does_not_change_the_setting(nrf52, monkeypatch):
    with Harness(nrf52, vid=IKOKA_VID, monkeypatch=monkeypatch) as h:
        h.handler.wait(lambda: h.handler.connects == 1)
        nrf52.disconnect()
        h.handler.wait(lambda: len(h.handler.disconnects) == 1)
        nrf52.silent = True
        nrf52.reconnect()
        h.handler.wait(lambda: len(h.handler.disconnects) == 2)
    assert h.opened[0] is True and h.opened[1] is True  # confirmed earlier, so no flip when it later goes quiet


# --- the command line -----------------------------------------------------------------------------------------------------


def test_listen_takes_dtr_and_works_with_a_companion_that_needs_it(tmp_path, capsys):
    import json

    from beacon_base import wire
    from beacon_base.cli import main

    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    main(["-c", str(cfg), "channel", "generate"])
    capsys.readouterr()
    with FakeCompanion() as fake:
        fake.drop_replies_when_dtr = False
        fake.send_push_on_enqueue = False
        # the CLI opens a real serial port on the pty, so the fake is told about DTR through the transport hook
        real_open = SerialTransport.__init__

        def init(self, path, baud=115200, dtr=False):
            fake.set_host_dtr(dtr)
            real_open(self, path, baud, dtr)

        SerialTransport.__init__ = init
        try:
            fake.enqueue_report(wire.encode_report(bytes(range(32)), [wire.Observation(bytes(range(8)), 1, -90, -8, 3800)]))
            assert main(["-c", str(cfg), "listen", "--port", fake.path, "--dtr", "on", "--count", "1", "--json"]) == 0
        finally:
            SerialTransport.__init__ = real_open
    assert json.loads(capsys.readouterr().out.splitlines()[0])["counter"] == 1


def test_dtr_option_is_checked_by_the_parser(tmp_path):
    from beacon_base.cli import main

    with pytest.raises(SystemExit):
        main(["listen", "--dtr", "high"])
    with pytest.raises(SystemExit):
        main(["ingest", "--dtr", "sometimes"])
