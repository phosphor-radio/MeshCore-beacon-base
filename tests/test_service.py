"""beacon-ingest: companion session wired to the pipeline, against a fake companion."""

import threading
import time

import pytest

from beacon_base import clock, wire
from beacon_base.config import CompanionConfig, Config, RadioConfig
from beacon_base.fake_companion import FakeCompanion
from beacon_base.ingest import CompanionSession
from beacon_base.pipeline import Pipeline
from beacon_base.service import PipelineHandler
from beacon_base.store import Store

from helpers import BEACON_KEY, BEACON_PREFIX, REPEATER_A_KEY, REPEATER_B_KEY, obs, rx

KEY = bytes(range(16))


def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


@pytest.fixture
def env(tmp_path):
    with FakeCompanion() as fake, Store.open(tmp_path / "t.db") as store:
        store.add_beacon("b1", BEACON_KEY.hex())
        store.add_repeater("ra", REPEATER_A_KEY.hex(), 1, 1)
        store.add_repeater("rb", REPEATER_B_KEY.hex(), 1, 1)
        cfg = Config(companion=CompanionConfig(port=fake.path, command_timeout=2.0), radio=RadioConfig(), channel_key=KEY)
        pipeline = Pipeline(store, assume_synced=True)
        handler = PipelineHandler(store, pipeline)
        session = CompanionSession(cfg, handler)
        stop = threading.Event()
        thread = threading.Thread(target=session.run, args=(stop,), daemon=True)
        thread.start()
        yield fake, store, handler, session
        stop.set()
        thread.join(timeout=5)


def count(store, table="observations"):
    return store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_reports_from_the_companion_land_in_the_database(env):
    fake, store, handler, _ = env
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, 10, -90, -8, 3900)]))
    fake.enqueue_report(wire.encode_report(REPEATER_B_KEY, [wire.Observation(BEACON_PREFIX, 10, -95, -12, 3900)]))
    wait_for(lambda: count(store) == 2)
    b = store.beacon("b1")
    assert b["hwm"] == 10
    assert store.conn.execute("SELECT n_repeaters FROM transmissions").fetchone()[0] == 2
    assert handler.counts["accepted"] == 2


def test_replays_and_unknown_beacons_are_recorded_not_applied(env):
    fake, store, handler, _ = env
    stranger = bytes(range(0xF0, 0xF8))
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, 100, -90, -8, 3900)]))
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, 99, -90, -8, 3900), wire.Observation(stranger, 1, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 3)
    rows = [(r["status"], r["reason"]) for r in store.conn.execute("SELECT status, reason FROM observations ORDER BY id")]
    assert rows == [("accepted", ""), ("replay", "below_hwm"), ("unknown_beacon", "")]
    assert store.beacon("b1")["hwm"] == 100


def test_malformed_report_is_kept_in_the_audit_trail(env):
    fake, store, handler, _ = env
    fake.enqueue_report(b"\x07" + bytes(30))
    wait_for(lambda: len(store.bad_reports()) == 1)
    assert count(store) == 0
    assert "unknown report version 7" in store.bad_reports()[0]["detail"]


def test_other_traffic_is_not_stored(env):
    fake, store, handler, session = env
    fake.enqueue_report(bytes(20), data_type=0xFF01)
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, 1, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 1)
    assert count(store, "raw_frames") == 1


def test_backlog_replayed_after_an_outage_keeps_counters_in_order(env):
    fake, store, handler, session = env
    fake.send_push_on_enqueue = False
    fake.disconnect()
    time.sleep(0.3)  # let the session notice the unplug
    for c in range(1, 6):
        fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, c, -90, -8, 3900)]))
    fake.reconnect()
    wait_for(lambda: count(store) == 5)
    assert store.beacon("b1")["hwm"] == 5
    assert store.conn.execute("SELECT count(*) FROM raw_frames WHERE late = 1").fetchone()[0] == 5


def test_hwm_and_reset_survive_ingest_restart(tmp_path):
    path = tmp_path / "t.db"
    with FakeCompanion() as fake:
        cfg = Config(companion=CompanionConfig(port=fake.path, command_timeout=2.0), radio=RadioConfig(), channel_key=KEY)

        def run_ingest(reports):
            with Store.open(path) as store:
                handler = PipelineHandler(store, Pipeline(store, assume_synced=True))
                session = CompanionSession(cfg, handler)
                stop = threading.Event()
                t = threading.Thread(target=session.run, args=(stop,), daemon=True)
                t.start()
                wait_for(lambda: session.stats["reports"] == 0 and fake.channels.get(1))
                for r in reports:
                    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, r, -90, -8, 3900)]))
                wait_for(lambda: handler.counts.total() == len(reports))
                stop.set()
                t.join(timeout=5)
                return dict(handler.counts)

        with Store.open(path) as s:
            s.add_beacon("b1", BEACON_KEY.hex())
            s.add_repeater("ra", REPEATER_A_KEY.hex(), 1, 1)
        assert run_ingest([100, 101]) == {"accepted": 2}
        assert run_ingest([50]) == {"replay": 1}  # still locked out after a restart
        with Store.open(path) as s:
            s.reset_beacon("b1")
        assert run_ingest([50]) == {"accepted": 1}


# --- clock steps ------------------------------------------------------------------------------------------------------


def test_clock_step_seen_by_ingest_corrects_earlier_observations(tmp_path):
    with Store.open(tmp_path / "t.db") as store:
        store.add_beacon("b1", BEACON_KEY.hex())
        store.add_repeater("ra", REPEATER_A_KEY.hex(), 1, 1)
        pipeline = Pipeline(store, boot="boot-x")
        handler = PipelineHandler(store, pipeline)
        base = handler._offset  # the clock offset when ingest started

        # two reports stamped by the stale clock (offset unchanged), then the operator sets the clock forward a year
        handler.on_report(rx(REPEATER_A_KEY, obs(1), t=base + 100.0, mono=100.0))
        handler.on_report(rx(REPEATER_A_KEY, obs(2), t=base + 400.0, mono=400.0))
        assert store.conn.execute("SELECT count(*) FROM observations WHERE time_trusted = 0").fetchone()[0] == 2
        new_offset = base + 365 * 86400
        handler.on_report(rx(REPEATER_A_KEY, obs(3), t=new_offset + 700.0, mono=700.0))

        rows = store.conn.execute("SELECT rx_time, time_trusted FROM observations ORDER BY id").fetchall()
        assert [(r["rx_time"], r["time_trusted"]) for r in rows] == [
            (new_offset + 100.0, 1),
            (new_offset + 400.0, 1),
            (new_offset + 700.0, 1),
        ]
        ev = store.conn.execute("SELECT kind, offset_before, offset_after FROM clock_events").fetchall()
        assert len(ev) == 1 and ev[0]["kind"] == "step"
        assert ev[0]["offset_before"] == pytest.approx(base) and ev[0]["offset_after"] == pytest.approx(new_offset)


def test_small_clock_drift_is_not_a_step(tmp_path):
    with Store.open(tmp_path / "t.db") as store:
        store.add_beacon("b1", BEACON_KEY.hex())
        store.add_repeater("ra", REPEATER_A_KEY.hex(), 1, 1)
        handler = PipelineHandler(store, Pipeline(store, boot="boot-x"))
        base = handler._offset
        handler.on_report(rx(REPEATER_A_KEY, obs(1), t=base + 100.0 + 2.0, mono=100.0))  # 2 s of slew
        assert store.conn.execute("SELECT count(*) FROM clock_events").fetchone()[0] == 0


def test_step_already_recorded_by_beaconctl_is_not_recorded_twice(tmp_path):
    with Store.open(tmp_path / "t.db") as store:
        store.add_beacon("b1", BEACON_KEY.hex())
        store.add_repeater("ra", REPEATER_A_KEY.hex(), 1, 1)
        pipeline = Pipeline(store, boot="boot-x")
        handler = PipelineHandler(store, pipeline)
        base = handler._offset
        new_offset = base + 3600
        with store.transaction() as db:  # what 'beaconctl time set' did
            clock.apply_clock_event(db, "boot-x", "set", base, new_offset, 0)
        handler.on_report(rx(REPEATER_A_KEY, obs(1), t=new_offset + 50.0, mono=50.0))
        kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM clock_events")]
        assert kinds == ["set"]
        assert handler._offset == pytest.approx(new_offset)


def test_onboarding_a_beacon_from_the_reports_alone(env, tmp_path, capsys):
    """Let the beacon transmit, find its prefix in 'status', add it, and its next report is accepted."""
    from beacon_base.cli import main

    fake, store, handler, _ = env
    cfg = tmp_path / "config.toml"
    cfg.write_text('[database]\npath = "t.db"\n[clock]\nassume_synced = true\n')
    new_prefix = bytes(range(150, 158))
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(new_prefix, 1, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 1)

    assert main(["-c", str(cfg), "status"]) == 0
    out = capsys.readouterr().out
    assert f"beaconctl beacon add <name> {new_prefix.hex()}" in out

    assert main(["-c", str(cfg), "beacon", "add", "beacon-007", new_prefix.hex()]) == 0
    capsys.readouterr()
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(new_prefix, 2, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 2)
    assert store.beacon("beacon-007")["hwm"] == 2
    assert main(["-c", str(cfg), "status"]) == 0
    assert "not on the allowlist" not in capsys.readouterr().out
