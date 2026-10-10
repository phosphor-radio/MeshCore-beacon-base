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

from helpers import B1, BEACON_KEY, BEACON_PREFIX, REPEATER_A_KEY, REPEATER_B_KEY, obs, rx

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
        store.add_beacon(BEACON_KEY.hex())
        store.add_repeater(REPEATER_A_KEY.hex(), 1, 1, name="ra")
        store.add_repeater(REPEATER_B_KEY.hex(), 1, 1, name="rb")
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
    b = store.beacon(B1)
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
    assert store.beacon(B1)["hwm"] == 100


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
    assert store.beacon(B1)["hwm"] == 5
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
            s.add_beacon(BEACON_KEY.hex())
            s.add_repeater(REPEATER_A_KEY.hex(), 1, 1, name="ra")
        assert run_ingest([100, 101]) == {"accepted": 2}
        assert run_ingest([50]) == {"replay": 1}  # still locked out after a restart
        with Store.open(path) as s:
            s.reset_beacon(B1)
        assert run_ingest([50]) == {"accepted": 1}


# --- clock steps ------------------------------------------------------------------------------------------------------


def test_clock_step_seen_by_ingest_corrects_earlier_observations(tmp_path):
    with Store.open(tmp_path / "t.db") as store:
        store.add_beacon(BEACON_KEY.hex())
        store.add_repeater(REPEATER_A_KEY.hex(), 1, 1, name="ra")
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
        store.add_beacon(BEACON_KEY.hex())
        store.add_repeater(REPEATER_A_KEY.hex(), 1, 1, name="ra")
        handler = PipelineHandler(store, Pipeline(store, boot="boot-x"))
        base = handler._offset
        handler.on_report(rx(REPEATER_A_KEY, obs(1), t=base + 100.0 + 2.0, mono=100.0))  # 2 s of slew
        assert store.conn.execute("SELECT count(*) FROM clock_events").fetchone()[0] == 0


def test_step_already_recorded_by_beaconctl_is_not_recorded_twice(tmp_path):
    with Store.open(tmp_path / "t.db") as store:
        store.add_beacon(BEACON_KEY.hex())
        store.add_repeater(REPEATER_A_KEY.hex(), 1, 1, name="ra")
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


def announce(fake, repeater_key, *entries, **kw):
    fake.enqueue_report(
        wire.encode_names(repeater_key, [wire.NameEntry(p, n.encode() if isinstance(n, str) else n) for p, n in entries]),
        data_type=wire.NAMES_DATA_TYPE,
        **kw,
    )


def names_in(store):
    return {bytes(r["prefix"]): r["name"] for r in store.conn.execute("SELECT prefix, name FROM beacon_names")}


def test_announced_names_are_stored_for_any_prefix_and_the_latest_wins(env):
    fake, store, handler, _ = env
    other = bytes(range(60, 68))
    announce(fake, REPEATER_A_KEY, (BEACON_PREFIX, "Roof"), (other, "beacon-3c3d3e"))
    wait_for(lambda: len(names_in(store)) == 2)
    assert names_in(store) == {BEACON_PREFIX: "Roof", other: "beacon-3c3d3e"}
    announce(fake, REPEATER_B_KEY, (BEACON_PREFIX, "Front gate"))  # a rename, heard by another repeater
    wait_for(lambda: names_in(store)[BEACON_PREFIX] == "Front gate")
    row = store.conn.execute("SELECT * FROM beacon_names WHERE prefix = ?", (BEACON_PREFIX,)).fetchone()
    assert bytes(row["repeater_prefix"]) == REPEATER_B_KEY[:8] and row["updated_at"] >= row["first_seen"]
    assert handler.counts["names_learned"] == 2 and handler.counts["names_changed"] == 1


def test_names_from_a_repeater_that_is_not_trusted_are_kept_so_the_beacon_can_be_recognised(env):
    fake, store, handler, _ = env
    rogue = bytes(range(0x30, 0x50))
    store.set_repeater_enabled("rb", False)
    announce(fake, rogue, (BEACON_PREFIX, "From a stranger"))
    wait_for(lambda: names_in(store) == {BEACON_PREFIX: "From a stranger"})
    announce(fake, REPEATER_B_KEY, (BEACON_PREFIX, "From a disabled one"))
    wait_for(lambda: names_in(store) == {BEACON_PREFIX: "From a disabled one"})


def test_announced_names_are_cleaned(env):
    fake, store, handler, _ = env
    announce(fake, REPEATER_A_KEY, (BEACON_PREFIX, b"\x1b[31mRed\x1b[0m\nroof"), (bytes(range(1, 9)), b"\x07\x07"))
    wait_for(lambda: BEACON_PREFIX in names_in(store))
    assert names_in(store) == {BEACON_PREFIX: "[31mRed [0m roof"}  # the second name was nothing but control characters


def test_a_malformed_name_message_is_kept_for_the_audit_trail_and_changes_nothing(env):
    fake, store, handler, _ = env
    fake.enqueue_report(b"\x09" + bytes(20), data_type=wire.NAMES_DATA_TYPE)
    wait_for(lambda: len(store.bad_reports()) == 1)
    row = store.bad_reports()[0]
    assert row["outcome"] == "bad_names" and "unknown name message version 9" in row["detail"]
    assert names_in(store) == {}


def test_reports_and_names_interleave(env):
    fake, store, handler, _ = env
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, 1, -90, -8, 3900)]))
    announce(fake, REPEATER_A_KEY, (BEACON_PREFIX, "Roof"))
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(BEACON_PREFIX, 2, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 2 and names_in(store) == {BEACON_PREFIX: "Roof"})
    assert store.beacon(B1)["name"] == "Roof"


def test_onboarding_a_beacon_from_the_reports_alone(env, tmp_path, capsys):
    """Let the beacon transmit and announce its name, find its prefix and name in 'status', add it by prefix."""
    from beacon_base.cli import main

    fake, store, handler, _ = env
    cfg = tmp_path / "config.toml"
    cfg.write_text('[database]\npath = "t.db"\n[clock]\nassume_synced = true\n')
    new_prefix = bytes(range(150, 158))
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(new_prefix, 1, -90, -8, 3900)]))
    announce(fake, REPEATER_A_KEY, (new_prefix, "Roof"))
    wait_for(lambda: count(store) == 1 and names_in(store).get(new_prefix) == "Roof")

    assert main(["-c", str(cfg), "status"]) == 0
    out = capsys.readouterr().out
    assert "'Roof'" in out and f"beaconctl beacon add {new_prefix.hex()}" in out

    assert main(["-c", str(cfg), "beacon", "add", new_prefix.hex()]) == 0
    assert "Roof" in capsys.readouterr().out
    fake.enqueue_report(wire.encode_report(REPEATER_A_KEY, [wire.Observation(new_prefix, 2, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 2)
    assert store.beacon(new_prefix.hex())["hwm"] == 2
    assert main(["-c", str(cfg), "status"]) == 0
    out = capsys.readouterr().out
    assert "not on the allowlist" not in out
    row = [l for l in out.splitlines() if new_prefix.hex() in l][0]
    assert row.split()[:3] == ["ok", "Roof", new_prefix.hex()]


# --- repeater adverts -------------------------------------------------------------------------------------------------------


def adverts_in(store):
    return {bytes(r["prefix"]): r for r in store.conn.execute("SELECT * FROM repeater_adverts")}


def test_a_trusted_repeaters_advert_updates_its_position_and_name_last_write_wins(env):
    fake, store, handler, _ = env
    wait_for(lambda: fake.manual_add)
    fake.advert(REPEATER_A_KEY, "North Ridge", lat=40.5, lon=-75.25, counter=1)
    wait_for(lambda: store.repeater(REPEATER_A_KEY[:8].hex())["lat"] == 40.5)
    r = store.repeater(REPEATER_A_KEY[:8].hex())
    assert (r["lat"], r["lon"], r["name"], r["location_source"]) == (40.5, -75.25, "North Ridge", "advert")
    assert bytes(r["pubkey"]) == REPEATER_A_KEY
    fake.advert(REPEATER_A_KEY, "Ridge 2", lat=41.0, lon=-76.0, counter=2)  # corrected at the repeater
    wait_for(lambda: store.repeater(REPEATER_A_KEY[:8].hex())["lat"] == 41.0)
    assert store.repeater(REPEATER_A_KEY[:8].hex())["name"] == "Ridge 2"
    assert handler.counts["repeater_positions"] == 2


def test_an_advert_heard_before_the_repeater_is_trusted_is_used_when_it_is_added(env):
    fake, store, handler, _ = env
    wait_for(lambda: fake.manual_add)
    new_key = bytes(range(0x30, 0x50))
    fake.advert(new_key, "Hilltop", lat=39.0, lon=-74.0, counter=1)
    wait_for(lambda: new_key[:8] in adverts_in(store))
    assert store.repeaters() and all(bytes(r["prefix"]) != new_key[:8] for r in store.repeaters())  # heard, not trusted
    r = store.add_repeater(new_key[:8].hex())
    assert (r["lat"], r["lon"], r["name"], r["location_source"]) == (39.0, -74.0, "Hilltop", "advert")


def test_an_advert_without_a_position_is_kept_for_listing_and_never_erases_a_position(env):
    fake, store, handler, _ = env
    wait_for(lambda: fake.manual_add)
    unplaced = bytes(range(0x30, 0x50))
    fake.advert(unplaced, "Nowhere", lat=0.0, lon=0.0)
    fake.advert(REPEATER_B_KEY, "Named at last", lat=0.0, lon=0.0)  # trusted: its name is taken
    wait_for(lambda: store.repeater(REPEATER_B_KEY[:8].hex())["name"] == "Named at last")
    wait_for(lambda: unplaced[:8] in adverts_in(store))
    row = adverts_in(store)[unplaced[:8]]
    assert (row["name"], row["lat"], row["lon"]) == ("Nowhere", None, None)
    assert [r["prefix"] for r in store.unknown_repeaters(0)] == [unplaced[:8]]  # listed, so it can be added
    assert store.repeater(REPEATER_B_KEY[:8].hex())["lat"] == 1  # the position it had is not erased


def test_auto_add_trusts_repeaters_from_their_adverts_and_reports_and_beacons_from_reports(env):
    fake, store, handler, _ = env
    wait_for(lambda: fake.manual_add)
    store.set_autoadd("repeaters", True)
    store.set_autoadd("beacons", True)
    ridge, quiet = bytes(range(0x30, 0x50)), bytes(range(0x50, 0x70))
    new_beacon = bytes(range(0x90, 0x98))
    fake.advert(quiet, "Quiet", lat=0.0, lon=0.0)  # advert only, no position
    fake.enqueue_report(wire.encode_report(ridge, [wire.Observation(new_beacon, 5, -90, -8, 3900)]))
    wait_for(lambda: len(store.repeaters()) == 4 and count(store) == 1)
    assert {bytes(r["prefix"]) for r in store.repeaters()} >= {ridge[:8], quiet[:8]}
    assert store.beacon(new_beacon.hex())["hwm"] == 5
    assert handler.counts["auto_added"] == 2  # the advert (quiet), and the report (ridge and the beacon together)
    store.set_autoadd("repeaters", False)
    store.set_autoadd("beacons", False)  # locked
    fake.enqueue_report(wire.encode_report(bytes(range(0x70, 0x90)), [wire.Observation(bytes(range(0xA0, 0xA8)), 1, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 2)
    assert len(store.repeaters()) == 4 and len(store.beacons()) == 2


def test_onboarding_a_repeater_from_its_reports_and_its_advert(env, tmp_path, capsys):
    from beacon_base.cli import main

    fake, store, handler, _ = env
    wait_for(lambda: fake.manual_add)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[database]\npath = "t.db"\n[clock]\nassume_synced = true\n')
    new_key = bytes(range(0x30, 0x50))
    fake.enqueue_report(wire.encode_report(new_key, [wire.Observation(BEACON_PREFIX, 1, -90, -8, 3900)]))
    fake.advert(new_key, "Hilltop", lat=39.0, lon=-74.0, counter=1)
    wait_for(lambda: count(store) == 1 and new_key[:8] in adverts_in(store))

    assert main(["-c", str(cfg), "status"]) == 0
    out = capsys.readouterr().out
    assert f"{new_key[:8].hex()}  'Hilltop'  at 39.000000, -74.000000" in out and f"repeater add {new_key[:8].hex()}" in out

    assert main(["-c", str(cfg), "repeater", "add", "--all"]) == 0
    assert "Hilltop" in capsys.readouterr().out
    r = store.repeater(new_key[:8].hex())
    assert (r["lat"], r["lon"]) == (39.0, -74.0)
    assert main(["-c", str(cfg), "status"]) == 0
    assert "not in the repeater table" not in capsys.readouterr().out
    fake.enqueue_report(wire.encode_report(new_key, [wire.Observation(BEACON_PREFIX, 2, -90, -8, 3900)]))
    wait_for(lambda: count(store) == 2)
    assert store.beacon(B1)["hwm"] == 2


def test_repeater_adverts_in_the_contact_list_are_applied_at_connect(tmp_path):
    with FakeCompanion() as fake, Store.open(tmp_path / "t.db") as store:
        fake.contacts[REPEATER_A_KEY] = (2, "Listed", 5, 38.0, -73.0)
        store.add_repeater(REPEATER_A_KEY[:8].hex())
        cfg = Config(companion=CompanionConfig(port=fake.path, command_timeout=2.0), radio=RadioConfig(), channel_key=KEY)
        handler = PipelineHandler(store, Pipeline(store, assume_synced=True))
        session = CompanionSession(cfg, handler)
        stop = threading.Event()
        t = threading.Thread(target=session.run, args=(stop,), daemon=True)
        t.start()
        wait_for(lambda: store.repeater(REPEATER_A_KEY[:8].hex())["lat"] == 38.0)
        stop.set()
        t.join(timeout=5)


def test_the_companion_clock_follows_a_trusted_base_clock(env):
    fake, _, _, session = env  # assume_synced, and the fake companion's clock starts in May 2024
    wait_for(lambda: session.clock_synced)
    assert abs(fake.rtc() - time.time()) <= 2


def test_clock_trusted_follows_the_database(tmp_path):
    with Store.open(tmp_path / "c.db") as store:
        handler = PipelineHandler(store, Pipeline(store, assume_synced=False))
        assert not handler.clock_trusted()
        with store.transaction() as db:
            clock.apply_clock_event(db, handler._pipeline.boot, "set", 0.0, 0.0, time.time())
        assert handler.clock_trusted()
        assert PipelineHandler(store, Pipeline(store, assume_synced=True)).clock_trusted()
