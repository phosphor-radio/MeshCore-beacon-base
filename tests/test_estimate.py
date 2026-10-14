"""The placeholder estimator (estimate.py) and where the pipeline puts its output."""

import math

import pytest

from beacon_base import estimate
from beacon_base.config import EstimateConfig
from beacon_base.estimate import Estimator, Hearing
from beacon_base.pipeline import Pipeline
from beacon_base.store import Store

from helpers import (
    BEACON2_KEY,
    BEACON_KEY,
    BEACON_PREFIX,
    REPEATER_A_KEY,
    REPEATER_B_KEY,
    REPEATER_C_KEY,
    obs,
    rx,
)

A, B, C = REPEATER_A_KEY, REPEATER_B_KEY, REPEATER_C_KEY
M_PER_DEG_LAT = 111_195.0


def metres(lat1, lon1, lat2, lon2):
    dy = math.radians(lat2 - lat1) * 6_371_000.0
    dx = math.radians(lon2 - lon1) * math.cos(math.radians(lat1)) * 6_371_000.0
    return math.hypot(dx, dy)


# --- the estimator ---------------------------------------------------------------------------------------------------------


def test_nobody_heard_it_means_no_position():
    assert Estimator().estimate([]) is None


def test_one_repeater_is_a_range_circle_around_it():
    est = Estimator()
    p = est.estimate([Hearing(40.0, -75.0, -90)])
    assert (p.lat, p.lon, p.method, p.n_repeaters) == (40.0, -75.0, estimate.METHOD_RANGE, 1)
    assert p.radius_m == pytest.approx(est.range_m(-90))


def test_a_weaker_signal_is_a_wider_circle_within_the_limits():
    est = Estimator(EstimateConfig(min_radius_m=30, max_radius_m=3000))
    radii = [est.range_m(rssi) for rssi in (-30, -70, -90, -110, -140)]
    assert radii == sorted(radii)
    assert radii[0] == 30 and radii[-1] == 3000  # the loudest and the faintest are held at the limits
    assert 30 < est.range_m(-90) < 3000


def test_range_model_matches_free_space_when_the_exponent_is_two():
    est = Estimator(EstimateConfig(tx_power_dbm=20.0, path_loss_exponent=2.0, min_radius_m=1, max_radius_m=1e9), freq_khz=905775)
    # free space loss at 1 km and 905.775 MHz is about 91.6 dB, so 20 dBm arrives at about -71.6 dBm
    assert est.rssi_at(1000) == pytest.approx(-71.6, abs=0.2)
    assert est.range_m(est.rssi_at(1000)) == pytest.approx(1000, rel=0.01)


def test_two_repeaters_give_a_weighted_centroid_nearer_the_louder_one():
    est = Estimator()
    a, b = (40.0, -75.0), (40.0, -74.99)  # about 850 m apart
    p = est.estimate([Hearing(*a, -70), Hearing(*b, -90)])
    assert p.method == estimate.METHOD_CENTROID and p.n_repeaters == 2
    assert metres(*a, p.lat, p.lon) < metres(*b, p.lat, p.lon)
    assert metres(*a, p.lat, p.lon) < 20  # 20 dB louder: it carries 99 percent of the weight
    even = est.estimate([Hearing(*a, -80), Hearing(*b, -80)])
    assert even.lat == pytest.approx(40.0) and even.lon == pytest.approx(-74.995, abs=1e-6)  # equal signals: the midpoint


def test_the_centroid_radius_is_the_spread_or_half_the_best_range_and_stays_within_the_limits():
    est = Estimator()
    far = est.estimate([Hearing(40.0, -75.0, -80), Hearing(40.0, -74.9, -80)])  # 8.5 km apart, equally loud
    assert far.radius_m == pytest.approx(metres(40.0, -75.0, 40.0, -74.9) / 2, rel=0.01)  # each is half the gap from the midpoint
    capped = Estimator(EstimateConfig(max_radius_m=3000)).estimate([Hearing(40.0, -75.0, -80), Hearing(40.0, -74.9, -80)])
    assert capped.radius_m == 3000
    near = est.estimate([Hearing(40.0, -75.0, -60), Hearing(40.0001, -75.0, -61)])
    assert near.radius_m >= est.cfg.min_radius_m
    assert near.radius_m == pytest.approx(max(est.range_m(-60) / 2, est.cfg.min_radius_m), rel=0.2)


def test_the_centroid_works_across_the_antimeridian_and_the_poles_do_not_divide_by_zero():
    est = Estimator()
    p = est.estimate([Hearing(10.0, 179.999, -80), Hearing(10.0, -179.999, -80)])
    assert abs(p.lon) > 179.99  # near the date line, not near zero
    q = est.estimate([Hearing(90.0, 0.0, -80), Hearing(90.0, 90.0, -80)])
    assert q.lat == pytest.approx(90.0)


# --- positions in the pipeline ---------------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    store = Store.open(tmp_path / "t.db")
    store.add_beacon(BEACON_KEY.hex())
    store.add_beacon(BEACON2_KEY.hex())
    store.add_repeater(A.hex(), 40.0, -75.0, name="ra")
    store.add_repeater(B.hex(), 40.0, -74.99, name="rb")
    store.add_repeater(C.hex(), name="rc")  # not located
    yield store, Pipeline(store, boot="boot-1")
    store.close()


def positions(store):
    return store.conn.execute("SELECT p.*, t.counter FROM positions p JOIN transmissions t ON t.id = p.transmission_id ORDER BY p.transmission_id").fetchall()


def test_an_accepted_report_gets_a_range_position(env):
    store, p = env
    p.process(rx(A, obs(10, rssi=-85)))
    (row,) = positions(store)
    assert (row["counter"], row["method"], row["n_repeaters"], bytes(row["beacon_prefix"])) == (10, "range", 1, BEACON_PREFIX)
    assert (row["lat"], row["lon"]) == (40.0, -75.0)


def test_a_second_repeater_moves_the_same_transmission_to_a_centroid(env):
    store, p = env
    p.process(rx(A, obs(10, rssi=-70)))
    p.process(rx(B, obs(10, rssi=-70), t=1001.0))
    (row,) = positions(store)  # one row per transmission, replaced
    assert (row["method"], row["n_repeaters"]) == ("centroid", 2)
    assert row["lon"] == pytest.approx(-74.995, abs=1e-6)


def test_each_transmission_has_its_own_position(env):
    store, p = env
    p.process(rx(A, obs(10, rssi=-70)))
    p.process(rx(B, obs(11, rssi=-70), t=1300.0))
    assert [(r["counter"], r["lon"]) for r in positions(store)] == [(10, -75.0), (11, -74.99)]


def test_a_duplicate_a_replay_and_an_unknown_beacon_add_nothing(env):
    store, p = env
    p.process(rx(A, obs(10)))
    p.process(rx(A, obs(10)))  # duplicate
    p.process(rx(A, obs(5)))  # replay
    unknown = bytes(range(200, 208))
    p.process(rx(A, obs(1, beacon=unknown)))
    assert [r["counter"] for r in positions(store)] == [10]


def test_unlocated_and_disabled_repeaters_are_not_used(env):
    store, p = env
    p.process(rx(C, obs(10, rssi=-60)))  # heard only by an unlocated repeater: nothing to place it by
    assert positions(store) == []
    p.process(rx(A, obs(10, rssi=-90), t=1001.0))
    p.process(rx(B, obs(10, rssi=-60), t=1002.0))
    store.set_repeater_enabled("rb", False)
    p.process(rx(A, obs(11, rssi=-90), t=1300.0))
    p.process(rx(B, obs(11, rssi=-60), t=1301.0))  # disabled: stored as disabled and not an input
    rows = positions(store)
    assert [(r["counter"], r["method"], r["n_repeaters"]) for r in rows] == [(10, "centroid", 2), (11, "range", 1)]


def test_a_reset_gives_the_new_epoch_its_own_positions(env):
    store, p = env
    p.process(rx(A, obs(10)))
    store.reset_beacon(BEACON_PREFIX.hex())
    p.process(rx(B, obs(10), t=1100.0))  # the counter restarted: a new transmission, not the old one
    assert [(r["counter"], r["lon"]) for r in positions(store)] == [(10, -75.0), (10, -74.99)]


def test_rebuild_uses_the_repeaters_as_they_are_now(env):
    store, p = env
    p.process(rx(C, obs(10, rssi=-70)))  # nothing located heard it, so no position
    p.process(rx(A, obs(11, rssi=-70), t=1300.0))
    assert [r["counter"] for r in positions(store)] == [11]
    store.locate_repeater("rc", 41.0, -75.0)
    store.locate_repeater("ra", 40.5, -75.0)
    assert p.rebuild_positions() == 2
    assert [(r["counter"], r["lat"]) for r in positions(store)] == [(10, 41.0), (11, 40.5)]
    assert p.rebuild_positions(since=1200.0) == 1  # only what was last seen since then


def test_the_positions_table_keeps_to_its_transmissions(env):
    store, p = env
    p.process(rx(A, obs(10)))
    assert store.conn.execute("PRAGMA foreign_key_check").fetchall() == []
