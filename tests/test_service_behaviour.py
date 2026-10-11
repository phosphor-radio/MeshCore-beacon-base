"""B3.1: the in-process watchdog, beaconctl health and the container-friendly clock message."""

import os
import threading
import time

import pytest

from beacon_base import admin, heartbeat, service, watchdog
from beacon_base.cli import main
from beacon_base.config import ConfigError, CompanionConfig, Config, DatabaseConfig, RadioConfig, ServiceConfig, load_config
from beacon_base.fake_companion import FakeCompanion
from beacon_base.heartbeat import HeartbeatWriter
from beacon_base.store import Store, StoreError
from beacon_base.watchdog import Watchdog

KEY = bytes(range(16))


# --- the watchdog ---------------------------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_the_watchdog_fires_when_nothing_feeds_it():
    clock, fired = FakeClock(), threading.Event()
    wd = Watchdog(10.0, on_stall=fired.set, check_every=0.01, clock=clock)
    wd.start()
    try:
        clock.t += 5
        time.sleep(0.1)
        assert not fired.is_set()
        clock.t += 6  # 11 s since the last feed
        assert fired.wait(2)
        assert wd.fired
    finally:
        wd.stop()


def test_feeding_keeps_it_quiet_and_says_what_the_loop_was_doing(caplog):
    clock, fired = FakeClock(), threading.Event()
    wd = Watchdog(10.0, on_stall=fired.set, check_every=0.01, clock=clock)
    wd.start()
    try:
        for _ in range(5):
            clock.t += 8
            wd.feed("session loop")
            time.sleep(0.05)
        assert not fired.is_set()
        clock.t += 11
        assert fired.wait(2)
    finally:
        wd.stop()
    assert "no progress for 11 s" in caplog.text and "last seen: session loop" in caplog.text and "restarts beacon-ingest" in caplog.text


def test_a_stopped_watchdog_does_not_fire():
    clock, fired = FakeClock(), threading.Event()
    wd = Watchdog(1.0, on_stall=fired.set, check_every=0.01, clock=clock)
    wd.start()
    wd.stop()
    clock.t += 100
    time.sleep(0.1)
    assert not fired.is_set()


def test_the_watchdog_needs_a_positive_timeout_and_exits_with_a_recognisable_status(monkeypatch):
    with pytest.raises(ValueError):
        Watchdog(0)
    codes = []
    monkeypatch.setattr(os, "_exit", lambda code: codes.append(code))
    monkeypatch.setattr("logging.shutdown", lambda: None)
    watchdog._exit_process()
    assert codes == [70]


def test_the_session_loop_feeds_the_watchdog_and_a_stalled_one_would_be_caught(tmp_path):
    """Run the real service with a fake companion and a watchdog: it is fed by the loop's ticks. Then stop feeding it."""
    with FakeCompanion() as fake:
        cfg = Config(
            companion=CompanionConfig(port=fake.path, command_timeout=2.0), radio=RadioConfig(), channel_key=KEY,
            database=DatabaseConfig(path=str(tmp_path / "svc.db")), service=ServiceConfig(watchdog_s=30.0),
        )
        stalls = []
        original = Watchdog.__init__

        def spy(self, timeout, on_stall=None, **kw):
            original(self, timeout, on_stall=lambda: stalls.append(1), check_every=0.05, **kw)

        mp = pytest.MonkeyPatch()
        mp.setattr(Watchdog, "__init__", spy)
        stop = threading.Event()
        errors = []

        def run():
            try:
                service.run(cfg, stop)
            except Exception as e:
                errors.append(e)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            with Store.open(tmp_path / "svc.db") as reader:
                wait_until(lambda: (r := reader.status_row()) is not None and r["connected"] == 1)
            time.sleep(0.6)  # many ticks: a 30 s watchdog stays quiet
            assert not stalls
        finally:
            stop.set()
            thread.join(timeout=5)
            mp.undo()
        assert not errors and not stalls


def test_watchdog_seconds_comes_from_the_config_or_the_environment(monkeypatch):
    monkeypatch.delenv(service.WATCHDOG_ENV, raising=False)
    assert service.watchdog_seconds(Config()) == 0.0
    assert service.watchdog_seconds(Config(service=ServiceConfig(watchdog_s=45))) == 45.0
    monkeypatch.setenv(service.WATCHDOG_ENV, "120")
    assert service.watchdog_seconds(Config(service=ServiceConfig(watchdog_s=45))) == 120.0  # the image's setting wins
    monkeypatch.setenv(service.WATCHDOG_ENV, "0")
    assert service.watchdog_seconds(Config(service=ServiceConfig(watchdog_s=45))) == 0.0  # and can turn it off
    monkeypatch.setenv(service.WATCHDOG_ENV, "")
    assert service.watchdog_seconds(Config(service=ServiceConfig(watchdog_s=45))) == 45.0
    for bad in ("soon", "-5"):
        monkeypatch.setenv(service.WATCHDOG_ENV, bad)
        with pytest.raises(ConfigError):
            service.watchdog_seconds(Config())


def test_the_config_reads_and_checks_the_watchdog(tmp_path):
    def cfg(text):
        p = tmp_path / "c.toml"
        p.write_text(text)
        return load_config(p)

    assert cfg("").service.watchdog_s == 0
    assert cfg("[service]\nwatchdog_s = 90\n").service.watchdog_s == 90
    for bad in ("-1", "true", '"x"'):
        with pytest.raises(ConfigError, match="watchdog_s"):
            cfg(f"[service]\nwatchdog_s = {bad}\n")
    with pytest.raises(ConfigError, match="unknown"):
        cfg("[service]\nnonsense = 1\n")


# --- beaconctl health -----------------------------------------------------------------------------------------------------


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[clock]\nassume_synced = true\n")
    return str(p)


def health(cfg_file, capsys, *extra):
    code = main(["-c", cfg_file, "health", *extra])
    return code, capsys.readouterr().out


def test_health_without_a_database_creates_none(cfg_file, tmp_path, capsys):
    code, out = health(cfg_file, capsys)
    assert code == 1 and "no database" in out and not (tmp_path / "beacon.db").exists()


def test_health_for_each_state_of_ingest(cfg_file, tmp_path, capsys):
    with Store.open(tmp_path / "beacon.db") as s:
        code, out = health(cfg_file, capsys)
        assert code == 1 and "has never run" in out
        w = HeartbeatWriter(s)
        w.write()  # running, no companion
        code, out = health(cfg_file, capsys)
        assert code == 0 and "ok: beacon-ingest is running; the companion is not connected" in out
        code, out = health(cfg_file, capsys, "--companion")
        assert code == 1 and "companion is not connected" in out
        w.set(connected=1, last_report_at=time.time() - 30)
        w.write()
        code, out = health(cfg_file, capsys, "--companion")
        assert code == 0 and "companion connected" in out and "last report 30s ago" in out
        w.stop()
        code, out = health(cfg_file, capsys)
        assert code == 1 and "was stopped" in out
        HeartbeatWriter(s, boot="an-older-boot").write()
        code, out = health(cfg_file, capsys)
        assert code == 1 and "is not running" in out and "last heard from" in out


# --- the clock in a container ---------------------------------------------------------------------------------------------


def test_time_set_says_what_to_do_when_there_is_no_timedatectl(cfg_file, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None)
    code = main(["-c", cfg_file, "time", "set", "2026-10-10 12:00:00"])
    err = capsys.readouterr().err
    assert code == 2 and "no timedatectl" in err and "set the Pi's clock on the Pi" in err and "beaconctl time confirm" in err


def test_a_failing_timedatectl_gets_the_same_hint_but_a_read_does_not(monkeypatch):
    class Result:
        returncode, stdout, stderr = 1, "", "Failed to connect to bus: No such file or directory"

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/timedatectl")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    with pytest.raises(StoreError, match="Failed to connect to bus.*set the Pi's clock on the Pi"):
        admin.run_timedatectl("set-time", "2026-10-10 12:00:00")
    with pytest.raises(StoreError) as e:
        admin.run_timedatectl("show", "-p", "NTPSynchronized")
    assert "Pi's clock" not in str(e.value)  # reading the NTP state does not need the hint


# --- beacon-ingest --check ------------------------------------------------------------------------------------------------


def test_check_reports_what_is_missing_and_what_is_ready(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    assert service.main(["-c", str(cfg), "--check"]) == 2
    assert "no companion serial port" in capsys.readouterr().err
    cfg.write_text('[companion]\nport = "/dev/ttyACM0"\n')
    assert service.main(["-c", str(cfg), "--check"]) == 2
    assert "no report channel key" in capsys.readouterr().err
    (tmp_path / "secrets.toml").write_text('[channel]\nkey = "000102030405060708090a0b0c0d0e0f"\n')
    assert service.main(["-c", str(cfg), "--check"]) == 0
    assert "configuration ok: companion /dev/ttyACM0" in capsys.readouterr().out
    assert not (tmp_path / "beacon.db").exists()  # checking touches nothing
    assert service.main(["-c", str(cfg), "--check", "--port", "/dev/other"]) == 0
    assert "companion /dev/other" in capsys.readouterr().out
