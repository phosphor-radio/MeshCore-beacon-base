"""The data beacon-web serves (views.py), without HTTP."""

import dataclasses

import pytest

from beacon_base import heartbeat, views
from beacon_base.config import load_config
from beacon_base.pipeline import Pipeline
from beacon_base.store import Store

from helpers import BEACON2_KEY, BEACON2_PREFIX, BEACON_KEY, BEACON_PREFIX, REPEATER_A_KEY, REPEATER_B_KEY, REPEATER_C_KEY, obs, rx

A, B, C = REPEATER_A_KEY, REPEATER_B_KEY, REPEATER_C_KEY
NOW = 1100.0


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.toml").write_text('[database]\npath = "t.db"\n[clock]\nassume_synced = true\n')
    return load_config(tmp_path / "config.toml")


@pytest.fixture
def env(cfg):
    store = Store.open(cfg.db_path)
    store.add_beacon(BEACON_KEY.hex())
    store.add_beacon(BEACON2_KEY.hex())
    store.add_repeater(A.hex(), 40.0, -75.0, name="north")
    store.add_repeater(B.hex(), 40.0, -74.99, name="south")
    store.add_repeater(C.hex())  # never located
    with store.transaction():
        store.record_name(BEACON_PREFIX, "Roof", A[:8])
    yield store, Pipeline(store, assume_synced=True, boot="boot-1")
    store.close()


def run_ingest(store, state="up"):
    """The heartbeat of a running ingest with a companion connected."""
    w = heartbeat.HeartbeatWriter(store)
    w.set(connected=1, port="/dev/ttyX", connected_since=1000.0, companion_name="base", companion_model="Wio", companion_firmware="v10", companion_key_prefix=b"\x01" * 6, clock_trusted=1)
    w.write()
    return w


def change_status(store, **changes):
    row = {k: v for k, v in dict(store.status_row()).items() if k != "id"}
    store.write_status({**row, **changes})


# --- health --------------------------------------------------------------------------------------------------------------


def test_health_before_ingest_ever_ran(env, cfg):
    store, _ = env
    h = views.health_view(store, cfg, NOW)
    assert h["ingest"]["state"] == "never" and h["ok"] is False
    assert [p["code"] for p in h["problems"]] == ["ingest_never"]


def test_health_with_ingest_running_is_clean(env, cfg):
    store, _ = env
    run_ingest(store)
    h = views.health_view(store, cfg, NOW)
    assert h["ok"] and h["problems"] == []
    assert h["ingest"]["state"] == "up" and h["ingest"]["connected"] is True
    assert h["ingest"]["companion"] == {"name": "base", "model": "Wio", "firmware": "v10", "key_prefix": "010101010101"}
    assert h["clock"]["trusted"] is True and h["now"] == NOW


def test_health_names_each_way_ingest_can_be_unwell(env, cfg):
    store, _ = env
    w = run_ingest(store)
    w.set(connected=0)
    w.write()
    assert [p["code"] for p in views.health_view(store, cfg, NOW)["problems"]] == ["companion_disconnected"]
    w.stop()
    assert [p["code"] for p in views.health_view(store, cfg, NOW)["problems"]] == ["ingest_stopped"]
    change_status(store, state="running", updated_mono=0.0)  # last heard of at boot: long ago
    assert [p["code"] for p in views.health_view(store, cfg, NOW)["problems"]] == ["ingest_down"]


def test_health_warns_about_the_clock_the_companion_and_auto_add(env, cfg):
    store, _ = env
    run_ingest(store)
    change_status(store, companion_clock_offset_s=-500)
    store.set_autoadd("beacons", True)
    unsynced = dataclasses.replace(cfg, clock=dataclasses.replace(cfg.clock, assume_synced=False))
    h = views.health_view(store, unsynced, NOW)
    assert {p["code"] for p in h["problems"]} == {"clock_not_set", "companion_clock", "autoadd"}
    assert h["ok"] is True  # warnings do not make it not ok
    assert h["clock"]["trusted"] is False


# --- the map -------------------------------------------------------------------------------------------------------------


def test_state_lists_repeaters_beacons_and_what_is_unknown(env, cfg):
    store, p = env
    run_ingest(store)
    p.process(rx(A, obs(10, rssi=-70), t=1000.0))
    p.process(rx(B, obs(10, rssi=-70), t=1001.0))
    stranger = bytes(range(200, 208))
    p.process(rx(A, obs(3, beacon=stranger), t=1002.0))
    s = views.state_view(store, cfg, NOW, operator=True)
    assert s["operator"] is True and s["now"] == NOW and s["expected_interval_s"] == 300
    north, south, third = s["repeaters"]  # by name, unnamed last
    assert (north["label"], north["located"], north["lat"], north["lon"], north["last_report_at"]) == ("north (a0a1a2)", True, 40.0, -75.0, 1002.0)
    assert (south["last_report_at"], third["located"], third["lat"], third["last_report_at"]) == (1001.0, False, None, None)
    other, roof = s["beacons"]  # silent before ok
    assert (roof["name"], roof["state"], roof["hwm"], roof["prefix"]) == ("Roof", "ok", 10, BEACON_PREFIX.hex())
    assert roof["position"]["method"] == "centroid" and roof["position"]["n_repeaters"] == 2 and roof["position"]["counter"] == 10
    assert (other["state"], other["position"], other["last_heard_at"]) == ("silent", None, None)
    assert s["unknown"] == {"beacons": 1, "repeaters": 0}


def test_a_lockout_is_a_rejected_beacon_with_what_the_banner_needs(env, cfg):
    store, p = env
    p.process(rx(A, obs(100), t=1000.0))
    p.process(rx(A, obs(40), t=1010.0))
    p.process(rx(B, obs(41), t=1011.0))
    (rejected, *_), = [views.beacons_view(store, cfg, NOW)]
    assert rejected["state"] == "rejected"
    assert rejected["rejects"] == {"count": 2, "since": 1010.0, "min_counter": 40, "max_counter": 41, "via": ["north", "south"]}
    assert (rejected["hwm"], rejected["last_reject_counter"]) == (100, 41)


def test_reset_clears_the_mark_and_says_what_it_cleared(env, cfg):
    store, p = env
    p.process(rx(A, obs(100), t=1000.0))
    p.process(rx(A, obs(40), t=1010.0))
    result = views.reset_beacon(store, BEACON_PREFIX.hex())
    assert result == {"beacon": BEACON_PREFIX.hex(), "label": "Roof (010203)", "old_hwm": 100, "last_reject_counter": 40, "rejects_cleared": 1}
    b = views.beacons_view(store, cfg, NOW)[0]
    assert (b["hwm"], b["baseline_pending"], b["rejects"]) == (None, True, None)
    with pytest.raises(views.NotFound):
        views.reset_beacon(store, "ffffffffffffffff")


@pytest.mark.parametrize("bad", ["", "abc", "0102030405060708ff", "zzzzzzzzzzzzzzzz", "010203"])
def test_only_a_full_prefix_names_a_beacon(env, bad):
    store, _ = env
    with pytest.raises(views.NotFound):
        views.parse_prefix(bad)


# --- history -------------------------------------------------------------------------------------------------------------


@pytest.fixture
def walked(env):
    store, p = env
    for i, (rep, t) in enumerate(((A, 1000.0), (B, 1300.0), (A, 1600.0))):
        p.process(rx(rep, obs(10 + i, rssi=-70 - i), t=t))
    return store, p


def test_beacon_detail_has_its_transmissions_newest_first(walked, cfg):
    store, _ = walked
    d = views.beacon_view(store, cfg, BEACON_PREFIX.hex(), 1700.0)
    assert d["beacon"]["label"] == "Roof (010203)"
    assert [t["counter"] for t in d["transmissions"]] == [12, 11, 10]
    newest = d["transmissions"][0]
    assert (newest["n_repeaters"], newest["batt_mv"], newest["position"]["method"]) == (1, 3800, "range")
    assert len(views.beacon_view(store, cfg, BEACON_PREFIX.hex(), 1700.0, limit=2)["transmissions"]) == 2
    with pytest.raises(views.NotFound):
        views.beacon_view(store, cfg, "ffffffffffffffff", 1700.0)


def test_a_transmission_nobody_located_heard_has_no_position(env, cfg):
    store, p = env
    p.process(rx(C, obs(1), t=1000.0))
    d = views.beacon_view(store, cfg, BEACON_PREFIX.hex(), 1100.0)
    assert d["transmissions"][0]["position"] is None and d["beacon"]["position"] is None


def test_track_is_oldest_first_and_limited_by_age_and_count(walked):
    store, _ = walked
    t = views.track_view(store, BEACON_PREFIX.hex(), 1700.0)
    assert [(pt["counter"], pt["lon"]) for pt in t["points"]] == [(10, -75.0), (11, -74.99), (12, -75.0)]
    assert [pt["counter"] for pt in views.track_view(store, BEACON_PREFIX.hex(), 1700.0, hours=0.15)["points"]] == [11, 12]  # 540 s back
    assert [pt["counter"] for pt in views.track_view(store, BEACON_PREFIX.hex(), 1700.0, limit=1)["points"]] == [12]  # the newest kept
    assert views.track_view(store, BEACON2_PREFIX.hex(), 1700.0)["points"] == []


def test_transmission_detail_lists_who_heard_it(env):
    store, p = env
    p.process(rx(A, obs(10, rssi=-70, snr_x4=-8), t=1000.0))
    p.process(rx(B, obs(10, rssi=-90), t=1001.0))
    p.process(rx(B, obs(10, rssi=-90), t=1002.0))  # a duplicate is part of the transmission too
    tx_id = store.latest_transmission(BEACON_PREFIX)["id"]
    d = views.transmission_view(store, tx_id, 1100.0)
    assert d["beacon"] == {"prefix": BEACON_PREFIX.hex(), "label": "Roof (010203)"}
    assert [(o["repeater_label"], o["rssi"], o["snr"], o["status"]) for o in d["observations"]] == [
        ("north (a0a1a2)", -70, -2.0, "accepted"), ("south (b0b1b2)", -90, -2.0, "accepted"), ("south (b0b1b2)", -90, -2.0, "duplicate"),
    ]
    assert d["observations"][0]["lat"] == 40.0 and d["transmission"]["position"]["method"] == "centroid"
    with pytest.raises(views.NotFound):
        views.transmission_view(store, 9999, 1100.0)


def test_rejects_view_lists_what_was_not_accepted(env):
    store, p = env
    p.process(rx(A, obs(100), t=1000.0))
    p.process(rx(A, obs(40), t=1010.0))
    p.process(rx(C, obs(101, beacon=BEACON2_PREFIX), t=1020.0))
    p.process(rx(A, obs(3, beacon=bytes(range(200, 208))), t=1030.0))
    r = views.rejects_view(store, 1100.0)["rejects"]
    assert [(x["status"], x["reason"], x["counter"]) for x in r] == [("unknown_beacon", "", 3), ("replay", "below_hwm", 40)]
    assert views.rejects_view(store, 1100.0, beacon=BEACON_PREFIX.hex())["rejects"][0]["hwm"] == 100
    assert views.rejects_view(store, 1100.0, limit=1)["rejects"][0]["counter"] == 3
