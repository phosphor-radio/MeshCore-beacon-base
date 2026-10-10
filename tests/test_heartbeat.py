"""The ingest heartbeat: the status row, how liveness is judged, what status and check say, and the service writing it."""

import json
import threading
import time

import pytest

from beacon_base import heartbeat, service, wire
from beacon_base.cli import main
from beacon_base.config import ClockConfig, CompanionConfig, Config, DatabaseConfig, RadioConfig
from beacon_base.fake_companion import FakeCompanion
from beacon_base.heartbeat import HeartbeatWriter
from beacon_base.store import Store

from helpers import B1, REPEATER_A_KEY

KEY = bytes(range(16))


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "beacon.db") as s:
        yield s


def until(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


def row(store):
    return dict(store.status_row())


# --- liveness -------------------------------------------------------------------------------------------------------------


def test_no_row_means_ingest_never_ran(store):
    assert heartbeat.state(store.status_row()) == heartbeat.NEVER


def test_a_fresh_row_is_up_and_goes_down_when_it_is_not_refreshed(store):
    w = HeartbeatWriter(store)
    w.write(mono=1000.0)
    r = store.status_row()
    assert heartbeat.state(r, mono_now=1000.0 + heartbeat.DOWN_AFTER - 1) == heartbeat.UP
    assert heartbeat.state(r, mono_now=1000.0 + heartbeat.DOWN_AFTER + 1) == heartbeat.DOWN


def test_a_row_from_an_earlier_boot_is_down_whatever_its_age(store):
    HeartbeatWriter(store, boot="an-older-boot").write()
    assert heartbeat.state(store.status_row()) == heartbeat.DOWN


def test_a_clean_stop_is_told_apart_from_a_crash(store):
    w = HeartbeatWriter(store)
    w.set(connected=1)
    w.write()
    w.stop()
    r = store.status_row()
    assert heartbeat.state(r) == heartbeat.STOPPED and r["connected"] == 0 and r["remote_state"] == "idle"


def test_setting_the_wall_clock_cannot_fake_liveness(store, monkeypatch):
    w = HeartbeatWriter(store)
    w.write()
    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 4_000_000_000)  # the Pi's clock is set to a far-off date
    assert heartbeat.state(store.status_row()) == heartbeat.UP


# --- the writer -----------------------------------------------------------------------------------------------------------


def test_events_are_written_at_once_and_counters_wait_for_the_interval(store):
    w = HeartbeatWriter(store, interval=15.0)
    assert w.tick(100.0)  # the first tick writes
    assert not w.tick(101.0)  # nothing changed
    w.set(stats={"reports": 5}, last_report_at=50.0, last_frame_at=51.0)  # counters only
    assert not w.tick(102.0)
    w.set(connected=1, port="/dev/x")  # an event
    assert w.tick(103.0)
    assert row(store)["port"] == "/dev/x" and json.loads(row(store)["stats"]) == {"reports": 5}
    w.set(stats={"reports": 6})
    assert not w.tick(110.0) and w.tick(118.5)  # the periodic write carries the counters
    assert json.loads(row(store)["stats"]) == {"reports": 6}


def test_setting_the_same_value_again_is_not_a_change(store):
    w = HeartbeatWriter(store)
    w.set(connected=1)
    assert w.tick(1.0)
    w.set(connected=1)
    assert not w.tick(2.0)


def test_a_failed_write_is_not_fatal_and_is_retried(store, caplog):
    w = HeartbeatWriter(store)
    store.conn.close()
    assert not w.write()  # the database is gone: logged, not raised
    assert "could not write the ingest heartbeat" in caplog.text


def test_there_is_only_ever_one_row(store):
    w = HeartbeatWriter(store)
    for n in range(3):
        w.set(remote_state=f"job {n}")
        w.write()
    assert store.conn.execute("SELECT count(*) FROM service_status").fetchone()[0] == 1
    with pytest.raises(Exception):
        store.conn.execute("INSERT INTO service_status (id, pid, boot_id, started_at, updated_at, updated_mono, state) VALUES (2, 1, 'b', 0, 0, 0, 'running')")


def test_unknown_columns_are_refused(store):
    with pytest.raises(ValueError, match="unknown"):
        store.write_status({"pid": 1, "nonsense": 2})


# --- beaconctl ------------------------------------------------------------------------------------------------------------


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[clock]\nassume_synced = true\n")
    return str(p)


def ctl(cfg_file, capsys, *argv):
    code = main(["-c", cfg_file, *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def seed(cfg_file, capsys):
    ctl(cfg_file, capsys, "beacon", "add", B1)
    ctl(cfg_file, capsys, "repeater", "add", REPEATER_A_KEY.hex(), "40.1", "-75.2", "--name", "north", "--window", "20")


def test_status_says_ingest_has_never_run(cfg_file, capsys):
    seed(cfg_file, capsys)
    _, out, _ = ctl(cfg_file, capsys, "status")
    assert out.startswith("! ingest: has never run")


def test_status_describes_a_running_ingest_and_its_companion(cfg_file, tmp_path, capsys):
    seed(cfg_file, capsys)
    with Store.open(tmp_path / "beacon.db") as s:
        w = HeartbeatWriter(s)
        w.set(connected=1, port="/dev/serial/by-id/x", connected_since=time.time() - 3600, companion_name="base", companion_model="Xiao",
              companion_firmware="v1.2", companion_key_prefix=bytes.fromhex("808182838485"), last_frame_at=time.time() - 2,
              last_report_at=time.time() - 40, companion_clock_offset_s=0)
        w.write()
    _, out, _ = ctl(cfg_file, capsys, "status")
    lines = out.splitlines()
    assert lines[0].startswith("ingest: running (pid ")
    assert "base (Xiao v1.2) on /dev/serial/by-id/x, key 808182838485, connected 60m ago" in lines[1]
    assert "last frame 2s ago, last report 40s ago" in lines[1]
    assert not any(l.startswith("!") for l in lines[:3])


def test_status_flags_a_disconnected_companion_a_dead_ingest_and_a_stopped_one(cfg_file, tmp_path, capsys):
    seed(cfg_file, capsys)
    with Store.open(tmp_path / "beacon.db") as s:
        w = HeartbeatWriter(s)
        w.write()  # running, not connected
        _, out, _ = ctl(cfg_file, capsys, "status")
        assert "! companion: not connected" in out
        HeartbeatWriter(s, boot="another-boot").write()
        _, out, _ = ctl(cfg_file, capsys, "status")
        assert "! ingest: NOT RUNNING, last heard from" in out
        w.stop()
        _, out, _ = ctl(cfg_file, capsys, "status")
        assert "! ingest: stopped cleanly" in out


def test_status_warns_about_a_companion_clock_that_is_ahead(cfg_file, tmp_path, capsys):
    seed(cfg_file, capsys)
    with Store.open(tmp_path / "beacon.db") as s:
        w = HeartbeatWriter(s)
        w.set(connected=1, companion_name="b", companion_clock_offset_s=500)
        w.write()
    _, out, _ = ctl(cfg_file, capsys, "status")
    assert "! companion clock: 500 s ahead of the base's; it cannot be set back, reboot the companion" in out


def test_check_fails_when_ingest_is_down_or_the_companion_is_not_connected(cfg_file, tmp_path, capsys):
    seed(cfg_file, capsys)
    _, out, _ = ctl(cfg_file, capsys, "check")
    assert "beacon-ingest is not running" not in out  # never run: not a finding (a fresh setup is checked before the first start)
    with Store.open(tmp_path / "beacon.db") as s:
        w = HeartbeatWriter(s)
        w.write()
        _, out, _ = ctl(cfg_file, capsys, "check")
        assert "running but the companion is not connected" in out
        w.set(connected=1)
        w.write()
        _, out, _ = ctl(cfg_file, capsys, "check")
        assert "companion is not connected" not in out and "not running" not in out
        w.stop()
        code, out, _ = ctl(cfg_file, capsys, "check")
        assert code == 1 and "beacon-ingest is not running (stopped)" in out


# --- the service ----------------------------------------------------------------------------------------------------------


def test_the_service_keeps_the_heartbeat_and_says_so_when_it_stops(tmp_path):
    with FakeCompanion() as fake:
        cfg = Config(
            companion=CompanionConfig(port=fake.path, command_timeout=2.0), radio=RadioConfig(), channel_key=KEY,
            database=DatabaseConfig(path=str(tmp_path / "svc.db")),
        )
        stop = threading.Event()
        errors = []
        thread = threading.Thread(target=lambda: _run(cfg, stop, errors), daemon=True)
        thread.start()
        with Store.open(tmp_path / "svc.db") as reader:
            until(lambda: (r := reader.status_row()) is not None and r["connected"] == 1)
            r = dict(reader.status_row())
            assert r["state"] == "running" and r["companion_name"] == "fake-companion" and r["companion_model"] == "Fake Companion"
            assert r["companion_key_prefix"] == fake.public_key[:6] and r["port"] == fake.path
            assert heartbeat.state(reader.status_row()) == heartbeat.UP
            fake.disconnect()
            until(lambda: reader.status_row()["connected"] == 0)
            assert reader.status_row()["connected_since"] is None
            stop.set()
            thread.join(timeout=5)
            assert not errors and not thread.is_alive()
            assert heartbeat.state(reader.status_row()) == heartbeat.STOPPED


def _run(cfg, stop, errors):
    try:
        service.run(cfg, stop)
    except Exception as e:  # reported by the test
        errors.append(e)


def test_the_periodic_write_carries_counters_and_the_last_report(tmp_path, monkeypatch):
    original = service.PipelineHandler.__init__

    def quick(self, store, pipeline, port=None, heartbeat_interval=1.0):  # a one second heartbeat, not the 15 s default
        original(self, store, pipeline, port, heartbeat_interval)

    monkeypatch.setattr(service.PipelineHandler, "__init__", quick)
    repeater = bytes(range(32))
    with FakeCompanion() as fake:
        cfg = Config(
            companion=CompanionConfig(port=fake.path, command_timeout=2.0), radio=RadioConfig(), channel_key=KEY,
            database=DatabaseConfig(path=str(tmp_path / "svc.db")), clock=ClockConfig(assume_synced=True),
        )
        with Store.open(tmp_path / "svc.db") as seed_store:
            seed_store.add_repeater(repeater.hex(), 1, 1, name="r")
            seed_store.add_beacon("a1" * 8)
        stop = threading.Event()
        errors = []
        thread = threading.Thread(target=lambda: _run(cfg, stop, errors), daemon=True)
        thread.start()
        try:
            with Store.open(tmp_path / "svc.db") as reader:
                until(lambda: (r := reader.status_row()) is not None and r["connected"] == 1)
                fake.enqueue_report(wire.encode_report(repeater[:8], [wire.Observation(bytes.fromhex("a1" * 8), 5, -90, -12, 3800)]))
                until(lambda: reader.status_row()["last_report_at"] is not None, timeout=10)
                r = dict(reader.status_row())
                assert json.loads(r["stats"])["reports"] == 1 and r["last_frame_at"] is not None and r["clock_trusted"] == 1
        finally:
            stop.set()
            thread.join(timeout=5)
        assert not errors
