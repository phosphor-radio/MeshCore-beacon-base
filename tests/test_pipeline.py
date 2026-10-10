"""Replay and dedupe pipeline: table driven, no hardware."""

import pytest

from beacon_base import clock
from beacon_base import wire
from beacon_base.pipeline import Pipeline
from beacon_base.store import Store

from helpers import (
    B1,
    B2,
    BEACON2_KEY,
    BEACON2_PREFIX,
    BEACON_KEY,
    BEACON_PREFIX,
    REPEATER_A_KEY,
    REPEATER_B_KEY,
    REPEATER_C_KEY,
    obs,
    rx,
)

A, B, C = REPEATER_A_KEY, REPEATER_B_KEY, REPEATER_C_KEY


@pytest.fixture
def env(tmp_path):
    store = Store.open(tmp_path / "t.db")
    store.add_beacon(BEACON_KEY.hex())
    store.add_beacon(BEACON2_KEY.hex())
    for name, key in (("ra", A), ("rb", B), ("rc", C)):
        store.add_repeater(key.hex(), 40.0, -75.0, name=name)
    pipeline = Pipeline(store, boot="boot-1")
    yield store, pipeline
    store.close()


def statuses(verdicts):
    return [(v.status, v.reason) for v in verdicts]


def beacon(store, ref=B1):
    return store.beacon(ref)


def test_first_report_sets_the_baseline(env):
    store, p = env
    assert beacon(store)["hwm"] is None
    v = p.process(rx(A, obs(500)))
    assert statuses(v) == [("accepted", "")]
    assert beacon(store)["hwm"] == 500
    assert v[0].beacon_name is None and v[0].repeater_name == "ra"  # no name announced for the beacon yet


def test_verdicts_carry_the_announced_beacon_name(env):
    store, p = env
    with store.transaction():
        store.record_name(BEACON_PREFIX, "Roof", A[:8])
    (v,) = p.process(rx(A, obs(1)))
    assert v.beacon_name == "Roof"


def test_increasing_counters_advance_the_mark_and_finalise_the_previous_transmission(env):
    store, p = env
    p.process(rx(A, obs(1), t=1000))
    p.process(rx(A, obs(2), t=1300))
    p.process(rx(A, obs(7), t=1600))  # skipped numbers are fine, only increase matters
    assert beacon(store)["hwm"] == 7
    txs = store.conn.execute("SELECT counter, final FROM transmissions ORDER BY counter").fetchall()
    assert [(t["counter"], t["final"]) for t in txs] == [(1, 1), (2, 1), (7, 0)]


def test_lower_counter_is_rejected_as_replay_and_counts_towards_lockout(env):
    store, p = env
    p.process(rx(A, obs(100)))
    v = p.process(rx(A, obs(40), t=1100))
    assert statuses(v) == [("replay", "below_hwm")]
    b = beacon(store)
    assert b["hwm"] == 100 and b["rejects_since_accept"] == 1
    assert b["last_reject_counter"] == 40 and b["last_reject_at"] == 1100


def test_equal_counter_from_another_repeater_joins_the_group(env):
    store, p = env
    p.process(rx(A, obs(5), t=1000))
    v = p.process(rx(B, obs(5, rssi=-80), t=1004))
    p.process(rx(C, obs(5), t=1009))
    assert statuses(v) == [("accepted", "")]
    tx = store.conn.execute("SELECT * FROM transmissions").fetchone()
    assert tx["n_repeaters"] == 3 and tx["first_seen"] == 1000 and tx["last_seen"] == 1009
    assert store.conn.execute("SELECT count(*) FROM transmissions").fetchone()[0] == 1
    assert beacon(store)["hwm"] == 5


def test_same_repeater_twice_is_a_duplicate(env):
    store, p = env
    p.process(rx(A, obs(5)))
    v = p.process(rx(A, obs(5), t=1002))
    assert statuses(v) == [("duplicate", "")]
    assert store.conn.execute("SELECT n_repeaters FROM transmissions").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM observations WHERE status = 'accepted'").fetchone()[0] == 1


def test_duplicate_within_one_report(env):
    store, p = env
    v = p.process(rx(A, obs(5), obs(5)))
    assert statuses(v) == [("accepted", ""), ("duplicate", "")]


def test_unknown_beacon_changes_nothing_else(env):
    store, p = env
    stranger = bytes(range(200, 208))
    v = p.process(rx(A, obs(9, beacon=stranger)))
    assert statuses(v) == [("unknown_beacon", "")]
    assert store.conn.execute("SELECT count(*) FROM transmissions").fetchone()[0] == 0
    assert all(b["hwm"] is None for b in store.beacons())
    rows = store.unknown_beacons(0)
    assert len(rows) == 1 and bytes(rows[0]["beacon_prefix"]) == stranger and rows[0]["n"] == 1


def test_unknown_repeater_cannot_move_the_high_water_mark(env):
    store, p = env
    p.process(rx(A, obs(100)))
    rogue = bytes(range(0x10, 0x30))
    v = p.process(rx(rogue, obs(2_000_000_000)))
    assert statuses(v) == [("unknown_repeater", "")]
    b = beacon(store)
    assert b["hwm"] == 100 and b["rejects_since_accept"] == 0
    # the genuine beacon is not locked out
    assert statuses(p.process(rx(A, obs(101), t=1300))) == [("accepted", "")]
    assert len(store.unknown_repeaters(0)) == 1


def test_unknown_repeater_cannot_set_the_baseline(env):
    store, p = env
    p.process(rx(bytes(range(0x10, 0x30)), obs(2_000_000_000)))
    assert beacon(store)["hwm"] is None


def test_disabled_beacon_and_repeater_are_not_processed(env):
    store, p = env
    store.set_beacon_enabled(B2, False)
    assert statuses(p.process(rx(A, obs(1, beacon=BEACON2_PREFIX)))) == [("disabled", "beacon")]
    assert store.beacon(B2)["hwm"] is None
    store.set_repeater_enabled("rb", False)
    assert statuses(p.process(rx(B, obs(1)))) == [("disabled", "repeater")]
    assert beacon(store)["hwm"] is None
    assert store.unknown_beacons(0) == [] and store.unknown_repeaters(0) == []  # not action items


def test_reset_makes_the_next_report_the_baseline(env):
    store, p = env
    p.process(rx(A, obs(1_000_000)))  # e.g. a forged report that locks the beacon out
    assert statuses(p.process(rx(A, obs(301), t=1100))) == [("replay", "below_hwm")]
    info = store.reset_beacon(B1)
    assert (info.old_hwm, info.last_reject_counter, info.rejects_since_accept) == (1_000_000, 301, 1)
    b = beacon(store)
    assert b["hwm"] is None and b["rejects_since_accept"] == 0
    assert statuses(p.process(rx(A, obs(302), t=1200))) == [("accepted", "")]
    assert beacon(store)["hwm"] == 302


def test_counters_that_restart_after_a_reset_are_not_deduped_against_old_ones(env):
    store, p = env
    p.process(rx(A, obs(5)))
    store.reset_beacon(B1)  # e.g. the beacon's flash was erased and its counter restarted
    assert statuses(p.process(rx(A, obs(5), t=1300))) == [("accepted", "")]
    assert store.conn.execute("SELECT count(*) FROM transmissions").fetchone()[0] == 2
    assert statuses(p.process(rx(A, obs(5), t=1301))) == [("duplicate", "")]


def test_late_report_after_a_newer_transmission_is_rejected_but_is_not_a_lockout(env):
    store, p = env
    p.process(rx(A, obs(10), t=1000))
    p.process(rx(A, obs(11), t=1300))  # repeater A flushed on time
    v = p.process(rx(B, obs(10), t=1310))  # repeater B's window ran long
    assert statuses(v) == [("replay", "late")]
    b = beacon(store)
    assert b["hwm"] == 11
    assert b["rejects_since_accept"] == 0 and b["last_reject_counter"] is None
    assert b["last_heard_at"] == 1310


def test_report_order_across_repeaters_inside_the_window_is_accepted(env):
    store, p = env
    for key, t in ((A, 1000), (B, 1015), (C, 1020)):
        p.process(rx(key, obs(10), t=t))
    for key, t in ((C, 1300), (A, 1310), (B, 1320)):
        p.process(rx(key, obs(11), t=t))
    assert store.conn.execute("SELECT count(*) FROM observations WHERE status = 'accepted'").fetchone()[0] == 6
    assert store.conn.execute("SELECT count(*) FROM observations WHERE status = 'replay'").fetchone()[0] == 0


def test_entries_in_one_report_are_judged_independently(env):
    store, p = env
    p.process(rx(A, obs(50), obs(7, beacon=BEACON2_PREFIX)))
    v = p.process(rx(A, obs(49), obs(8, beacon=BEACON2_PREFIX), obs(1, beacon=bytes(8)), t=1300))
    assert statuses(v) == [("replay", "below_hwm"), ("accepted", ""), ("unknown_beacon", "")]
    assert beacon(store, B1)["hwm"] == 50 and beacon(store, B2)["hwm"] == 8


def test_accept_clears_the_rejected_state(env):
    store, p = env
    p.process(rx(A, obs(100)))
    p.process(rx(A, obs(3), t=1100))
    p.process(rx(B, obs(4), t=1101))
    assert beacon(store)["rejects_since_accept"] == 2
    p.process(rx(A, obs(101), t=1300))
    assert beacon(store)["rejects_since_accept"] == 0


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "t.db"
    with Store.open(path) as s:
        s.add_beacon(BEACON_KEY.hex())
        s.add_repeater(A.hex(), 1, 1, name="ra")
        Pipeline(s, boot="boot-1").process(rx(A, obs(100)))
    with Store.open(path) as s:  # a new process
        p = Pipeline(s, boot="boot-1")
        assert s.beacon(B1)["hwm"] == 100
        assert statuses(p.process(rx(A, obs(99), t=1100))) == [("replay", "below_hwm")]
        assert statuses(p.process(rx(A, obs(100), t=1101))) == [("duplicate", "")]
        assert statuses(p.process(rx(A, obs(101), t=1300))) == [("accepted", "")]


def test_a_failure_part_way_leaves_the_database_consistent(env, monkeypatch):
    store, p = env
    p.process(rx(A, obs(10)))
    before = {t: store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("raw_frames", "observations", "transmissions")}
    calls = {"n": 0}
    real = p._observe

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("crash between steps")
        return real(*args, **kwargs)

    monkeypatch.setattr(p, "_observe", flaky)
    with pytest.raises(RuntimeError):
        p.process(rx(A, obs(11), obs(12, beacon=BEACON2_PREFIX), t=1300))
    after = {t: store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before
    assert beacon(store)["hwm"] == 10 and beacon(store, B2)["hwm"] is None
    monkeypatch.undo()
    # and the same report goes through cleanly afterwards
    assert statuses(p.process(rx(A, obs(11), obs(12, beacon=BEACON2_PREFIX), t=1300))) == [("accepted", ""), ("accepted", "")]


def test_raw_frames_are_kept_for_every_report_and_malformed_ones_are_marked(env):
    store, p = env
    r = rx(A, obs(1), obs(2, beacon=bytes(8)), late=True, snr_x4=-6)
    p.process(r)
    row = store.conn.execute("SELECT * FROM raw_frames").fetchone()
    assert bytes(row["payload"]) == r.payload and row["outcome"] == "ok" and row["late"] == 1 and row["companion_snr_x4"] == -6
    assert row["boot_id"] == "boot-1"

    class Bad:
        payload = b"\x02garbage"
        companion_snr_x4 = 4
        path_len = 255
        rx_wall = 2000.0
        rx_mono = 200.0
        late = False

    p.record_bad_report(Bad, "unknown report version 2")
    bad = store.bad_reports()
    assert len(bad) == 1 and bad[0]["detail"] == "unknown report version 2" and bytes(bad[0]["payload"]) == b"\x02garbage"
    assert store.conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 2  # nothing derived from the bad one


def test_observation_stores_the_measurements_and_monotonic_time(env):
    store, p = env
    p.process(rx(A, obs(5, rssi=-101, snr_x4=-13, batt=3777), t=5000.5, mono=321.0))
    o = store.conn.execute("SELECT * FROM observations").fetchone()
    assert (o["rssi"], o["snr_x4"], o["batt_mv"], o["rx_time"], o["rx_mono"], o["boot_id"]) == (-101, -13, 3777, 5000.5, 321.0, "boot-1")
    assert bytes(o["repeater_prefix"]) == A[:8] and bytes(o["beacon_prefix"]) == BEACON_PREFIX


# --- clock ------------------------------------------------------------------------------------------------------------


def test_times_are_untrusted_until_the_clock_is_confirmed_and_then_corrected(env):
    store, p = env
    # the Pi booted with a stale clock: wall time is 2025, monotonic 100 s
    p.process(rx(A, obs(1), t=1_700_000_100.0, mono=100.0))
    p.process(rx(A, obs(2), t=1_700_000_400.0, mono=400.0))
    assert [r["time_trusted"] for r in store.conn.execute("SELECT time_trusted FROM observations")] == [0, 0]

    # the operator sets the clock; the true offset is now 1_800_000_000
    with store.transaction() as db:
        n = clock.apply_clock_event(db, "boot-1", "set", 1_700_000_000.0, 1_800_000_000.0, 1_800_000_450.0)
    assert n == 2
    rows = store.conn.execute("SELECT rx_time, time_trusted FROM observations ORDER BY id").fetchall()
    assert [(r["rx_time"], r["time_trusted"]) for r in rows] == [(1_800_000_100.0, 1), (1_800_000_400.0, 1)]
    assert store.conn.execute("SELECT rx_time FROM raw_frames ORDER BY id").fetchall()[0][0] == 1_800_000_100.0
    # denormalised times follow
    b = beacon(store)
    assert b["last_heard_at"] == b["last_accept_at"] == 1_800_000_400.0 and b["hwm_at"] == 1_800_000_400.0
    tx = store.conn.execute("SELECT first_seen, last_seen FROM transmissions ORDER BY counter").fetchall()
    assert [(t[0], t[1]) for t in tx] == [(1_800_000_100.0, 1_800_000_100.0), (1_800_000_400.0, 1_800_000_400.0)]

    # later observations in this boot are trusted
    p.process(rx(A, obs(3), t=1_800_000_700.0, mono=700.0))
    assert store.conn.execute("SELECT time_trusted FROM observations ORDER BY id DESC LIMIT 1").fetchone()[0] == 1


def test_clock_event_only_touches_the_current_boot(env):
    store, p = env
    old = Pipeline(store, boot="boot-0")
    old.process(rx(A, obs(1), t=1000.0, mono=50.0))
    p.process(rx(A, obs(2), t=1_700_000_100.0, mono=100.0))
    with store.transaction() as db:
        clock.apply_clock_event(db, "boot-1", "set", 0, 1_800_000_000.0, 0)
    times = {r["boot_id"]: r["rx_time"] for r in store.conn.execute("SELECT boot_id, rx_time FROM observations")}
    assert times == {"boot-0": 1000.0, "boot-1": 1_800_000_100.0}
    # boot-0 stays provisional: it was never confirmed
    assert not clock.is_trusted(store.conn, "boot-0", False)
    assert clock.is_trusted(store.conn, "boot-1", False)


def test_assume_synced_trusts_the_clock(env):
    store, _ = env
    p = Pipeline(store, assume_synced=True, boot="boot-9")
    p.process(rx(A, obs(1)))
    assert store.conn.execute("SELECT time_trusted FROM observations").fetchone()[0] == 1


# --- announced names ----------------------------------------------------------------------------------------------------


def names_rx(repeater_key, *entries, t=1000.0):
    from beacon_base.ingest import ReceivedNames

    payload = wire.encode_names(repeater_key, [wire.NameEntry(p, n if isinstance(n, bytes) else n.encode()) for p, n in entries])
    return ReceivedNames(wire.decode_names(payload), 20, 1, t, 100.0, False, payload)


def test_names_from_a_known_repeater_are_stored_cleaned_and_reported_as_changes(env):
    store, p = env
    other = bytes(range(70, 78))
    changes = p.process_names(names_rx(A, (BEACON_PREFIX, "Roof"), (other, b"\x1b[1mBold\x1b[0m"), (bytes(8), b"\x07")))
    assert [(c.beacon_prefix, c.old, c.new, c.changed) for c in changes] == [(BEACON_PREFIX, None, "Roof", True), (other, None, "[1mBold [0m", True)]
    changes = p.process_names(names_rx(B, (BEACON_PREFIX, "Roof"), t=2000.0))
    assert [(c.old, c.new, c.changed) for c in changes] == [("Roof", "Roof", False)]
    changes = p.process_names(names_rx(B, (BEACON_PREFIX, "Front gate"), t=3000.0))
    assert [(c.old, c.new, c.changed) for c in changes] == [("Roof", "Front gate", True)]
    assert store.beacon(B1)["name"] == "Front gate"
    row = store.conn.execute("SELECT * FROM beacon_names WHERE prefix = ?", (BEACON_PREFIX,)).fetchone()
    assert (row["first_seen"], row["updated_at"], bytes(row["repeater_prefix"])) == (1000.0, 3000.0, B[:8])


def test_names_from_unknown_or_disabled_repeaters_are_ignored(env):
    store, p = env
    assert p.process_names(names_rx(bytes(range(0x30, 0x50)), (BEACON_PREFIX, "Evil"))) == []
    store.set_repeater_enabled("rb", False)
    assert p.process_names(names_rx(B, (BEACON_PREFIX, "Also evil"))) == []
    assert store.conn.execute("SELECT count(*) FROM beacon_names").fetchone()[0] == 0


def test_a_name_never_changes_a_beacons_counters(env):
    store, p = env
    p.process(rx(A, obs(100)))
    p.process_names(names_rx(A, (BEACON_PREFIX, "Roof")))
    b = beacon(store)
    assert (b["hwm"], b["rejects_since_accept"], b["epoch"], b["enabled"]) == (100, 0, 0, 1)
