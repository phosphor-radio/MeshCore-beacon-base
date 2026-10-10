import pytest

from beacon_base import health
from beacon_base.config import BeaconConfig
from beacon_base.pipeline import Pipeline
from beacon_base.store import Store

from helpers import BEACON2_KEY, BEACON2_PREFIX, BEACON_KEY, REPEATER_A_KEY, REPEATER_B_KEY, obs, rx

CFG = BeaconConfig(interval_s=300, silent_intervals=3)
NOW = 100_000.0


@pytest.fixture
def env(tmp_path):
    store = Store.open(tmp_path / "t.db")
    for name, key in (("b1", BEACON_KEY), ("b2", BEACON2_KEY)):
        store.add_beacon(key.hex())
        with store.transaction():
            store.record_name(key[:8], name, REPEATER_A_KEY[:8])  # the names repeaters announced
    store.add_repeater(REPEATER_A_KEY.hex(), 1, 1, name="ra")
    store.add_repeater(REPEATER_B_KEY.hex(), 1, 1, name="rb")
    yield store, Pipeline(store, boot="b")
    store.close()


def states(store, now=NOW):
    return {h.beacon["name"]: h.state for h in health.assess(store, CFG, now)}


def test_never_heard_beacon_is_silent(env):
    store, _ = env
    assert states(store) == {"b1": "silent", "b2": "silent"}


def test_recently_accepted_beacon_is_ok_and_goes_silent_after_the_configured_intervals(env):
    store, p = env
    p.process(rx(REPEATER_A_KEY, obs(1), t=NOW - 100))
    assert states(store)["b1"] == "ok"
    assert states(store, NOW - 100 + 900)["b1"] == "ok"  # exactly 3 intervals
    assert states(store, NOW - 100 + 901)["b1"] == "silent"


def test_replay_rejections_make_the_beacon_rejected_with_a_summary(env):
    store, p = env
    p.process(rx(REPEATER_A_KEY, obs(1000), t=NOW - 600))
    p.process(rx(REPEATER_A_KEY, obs(20), t=NOW - 30))
    p.process(rx(REPEATER_B_KEY, obs(22), t=NOW - 20))
    p.process(rx(REPEATER_B_KEY, obs(21), t=NOW - 10))
    result = {h.beacon["name"]: h for h in health.assess(store, CFG, NOW)}
    h = result["b1"]
    assert h.state == "rejected"
    s = h.rejects
    assert (s.count, s.min_counter, s.max_counter, s.first_at) == (3, 20, 22, NOW - 30)
    assert s.repeater_names == ["ra", "rb"]


def test_rejected_beats_silent_and_is_listed_first(env):
    store, p = env
    p.process(rx(REPEATER_A_KEY, obs(1000), obs(5, beacon=BEACON2_PREFIX), t=NOW - 5000))  # both long silent
    p.process(rx(REPEATER_A_KEY, obs(3), t=NOW - 4000))  # b1 gets a replay
    order = [(h.beacon["name"], h.state) for h in health.assess(store, CFG, NOW)]
    assert order == [("b1", "rejected"), ("b2", "silent")]


def test_late_reports_do_not_make_a_beacon_rejected(env):
    store, p = env
    p.process(rx(REPEATER_A_KEY, obs(10), t=NOW - 40))
    p.process(rx(REPEATER_A_KEY, obs(11), t=NOW - 20))
    p.process(rx(REPEATER_B_KEY, obs(10), t=NOW - 10))  # late
    assert states(store)["b1"] == "ok"


def test_reset_clears_the_rejected_state(env):
    store, p = env
    p.process(rx(REPEATER_A_KEY, obs(1000), t=NOW - 60))
    p.process(rx(REPEATER_A_KEY, obs(3), t=NOW - 30))
    assert states(store)["b1"] == "rejected"
    store.reset_beacon(BEACON_KEY[:8].hex())
    h = {h.beacon["name"]: h for h in health.assess(store, CFG, NOW)}["b1"]
    assert h.state == "ok" and h.baseline_pending and h.rejects is None
    p.process(rx(REPEATER_A_KEY, obs(4), t=NOW - 10))
    h = {h.beacon["name"]: h for h in health.assess(store, CFG, NOW)}["b1"]
    assert h.state == "ok" and not h.baseline_pending


def test_rejects_before_a_reset_do_not_reappear_in_the_summary(env):
    store, p = env
    p.process(rx(REPEATER_A_KEY, obs(1000), t=NOW - 90))
    p.process(rx(REPEATER_A_KEY, obs(3), t=NOW - 80))
    store.reset_beacon(BEACON_KEY[:8].hex())
    p.process(rx(REPEATER_A_KEY, obs(50), t=NOW - 70))  # new baseline
    p.process(rx(REPEATER_A_KEY, obs(40), t=NOW - 60))  # a fresh lockout
    h = {h.beacon["name"]: h for h in health.assess(store, CFG, NOW)}["b1"]
    assert h.state == "rejected" and h.rejects.count == 1 and h.rejects.min_counter == 40


def test_disabled_beacon(env):
    store, _ = env
    store.set_beacon_enabled(BEACON_KEY[:8].hex(), False)
    assert states(store) == {"b2": "silent", "b1": "disabled"}


def test_battery_comes_from_the_latest_transmission(env):
    store, p = env
    p.process(rx(REPEATER_A_KEY, obs(1, batt=3900), t=NOW - 600))
    p.process(rx(REPEATER_A_KEY, obs(2, batt=3850), t=NOW - 300))
    h = {h.beacon["name"]: h for h in health.assess(store, CFG, NOW)}["b1"]
    assert h.batt_mv == 3850
