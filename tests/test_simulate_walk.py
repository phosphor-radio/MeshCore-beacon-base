"""The walk simulation: a beacon carried round a course past located repeaters, and the pipeline following it."""

import math

import pytest

from beacon_base import wire
from beacon_base.ingest import ReceivedReport
from beacon_base.pipeline import Pipeline
from beacon_base.simulate import REPEATER_SPACING_M, Simulator
from beacon_base.store import Store


class Sink:
    """What the Simulator needs of a FakeCompanion: somewhere to put reports."""

    def __init__(self):
        self.payloads = []

    def enqueue_report(self, payload, channel_index=1, data_type=None):
        self.payloads.append(payload)

    def advert(self, *args, **kwargs):
        pass


def make(**kw):
    sink = Sink()
    return Simulator(sink, beacons=2, repeaters=3, seed=1, walk=True, **kw), sink


def test_without_walk_the_simulator_is_as_it_was():
    sim = Simulator(Sink(), beacons=2, repeaters=3, seed=1)
    assert [sim.repeater_position(i) is None for i in range(3)] == [False, False, True]  # the last repeater is unlocated


def test_on_the_course_every_repeater_is_located_and_they_are_apart():
    sim, _ = make()
    positions = [sim.repeater_position(i) for i in range(3)]
    assert all(p is not None for p in positions)
    assert len(set(positions)) == 3
    assert math.dist(sim._repeater_xy(0), sim._repeater_xy(1)) >= REPEATER_SPACING_M


def test_the_walker_goes_round_a_closed_loop_past_each_repeater():
    sim, _ = make()
    total = sum(math.dist(a, b) for a, b in zip(sim._course(), sim._course()[1:] + sim._course()[:1]))
    assert sim.walker_xy(0) == pytest.approx(sim.walker_xy(total))  # back where it started
    path = [sim.walker_xy(d) for d in range(0, int(total), 10)]
    for r in range(3):
        assert min(math.dist(p, sim._repeater_xy(r)) for p in path) < 250  # passes close to every repeater


def test_each_tick_moves_the_walker_and_closer_repeaters_hear_it_louder():
    sim, sink = make(walk_step_m=100.0)
    rssi_by_distance = []
    for _ in range(30):
        sink.payloads.clear()
        sim.tick()
        xy = sim.walker_xy()
        for payload in sink.payloads:
            report = wire.decode_report(payload)
            r = sim.repeater_keys.index(next(k for k in sim.repeater_keys if k[:8] == report.repeater_id))
            for o in report.observations:
                if o.beacon_id == sim.beacon_ids[0]:
                    rssi_by_distance.append((math.dist(xy, sim._repeater_xy(r)), o.rssi))
    assert len(rssi_by_distance) > 30
    near = [rssi for d, rssi in rssi_by_distance if d < 400]
    far = [rssi for d, rssi in rssi_by_distance if d > 900]
    assert near and far and sum(near) / len(near) > sum(far) / len(far) + 10
    assert sim.walker_xy() != sim.walker_xy(0)


def test_the_pipeline_places_the_walker_near_where_it_was_and_the_track_moves(tmp_path):
    sim, sink = make()
    with Store.open(tmp_path / "t.db") as store:
        for b in sim.beacon_ids:
            store.add_beacon(b.hex())
        for i, key in enumerate(sim.repeater_keys):
            store.add_repeater(key.hex(), *sim.repeater_position(i))
        pipeline = Pipeline(store, assume_synced=True)
        t, errors, track = 1000.0, [], []
        for _ in range(60):
            sink.payloads.clear()
            sim.tick()
            for payload in sink.payloads:
                t += 1
                pipeline.process(ReceivedReport(wire.decode_report(payload), 0, 1, t, t, False, payload))
            row = store.conn.execute("SELECT * FROM positions WHERE beacon_prefix = ? ORDER BY transmission_id DESC LIMIT 1", (sim.beacon_ids[0],)).fetchone()
            if row is not None:
                est = ((row["lon"] + 75.0) * 111_320 * math.cos(math.radians(40.0)), (row["lat"] - 40.0) * 111_320)
                errors.append(math.dist(est, sim.walker_xy()))
                track.append(est)
        assert len(track) >= 55
        assert sorted(errors)[len(errors) // 2] < 2 * REPEATER_SPACING_M  # a crude estimator: the median is within a kilometre
        assert max(math.dist(a, b) for a in track for b in track) > 300  # and the track really does travel
