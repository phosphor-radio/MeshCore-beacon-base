"""Synthetic beacon traffic for the fake companion: a handful of beacons heard by a handful of repeaters."""

from __future__ import annotations

import math
import random

from . import wire
from .estimate import Estimator
from .fake_companion import FakeCompanion

M_PER_DEG_LAT = 111_320.0
SENSITIVITY_DBM = -120  # a signal weaker than this is not heard
ORIGIN = (40.0, -75.0)  # where the walk course starts
REPEATER_SPACING_M = 500.0


class Simulator:
    def __init__(
        self, fake: FakeCompanion, channel_index: int = 1, beacons: int = 3, repeaters: int = 3, seed: int = 1,
        walk: bool = False, walk_step_m: float = 80.0, estimator: Estimator | None = None,
    ):
        """With `walk`, the first beacon is carried round a course past the repeaters (every one located, in a zigzag 500 m apart):
        each tick it moves `walk_step_m` metres and each repeater hears it with the RSSI the estimator's own model predicts for the
        distance, plus a few dB of noise. The other beacons keep reporting at random."""
        self._fake = fake
        self._walk = walk and beacons > 0
        self._walk_step_m = walk_step_m
        self._walk_at = 0.0  # metres along the course
        self._estimator = estimator or Estimator()
        self._channel_index = channel_index
        self._rng = random.Random(seed)
        self.beacon_ids = [self._rng.randbytes(wire.ID_LEN) for _ in range(beacons)]
        self.repeater_keys = [self._rng.randbytes(32) for _ in range(repeaters)]
        self._counters = [self._rng.randrange(1, 5000) for _ in self.beacon_ids]
        self._batt = [self._rng.randrange(3500, 4150) for _ in self.beacon_ids]

    def beacon_name(self, index: int) -> str:
        """Alternate the two kinds of name a real beacon has: the default derived from its key, and an explicit one."""
        if index % 2 == 0:
            return f"beacon-{self.beacon_ids[index].hex()[:6]}"
        return f"sim-beacon-{index + 1}"

    def repeater_name(self, index: int) -> str:
        return f"sim-repeater-{index + 1}"

    def repeater_position(self, index: int) -> tuple[float, float] | None:
        """Where repeater `index` has been told it is. The last one (when there are two or more) was never located, except on
        the walk course, where all of them are."""
        if self._walk:
            return self._latlon(*self._repeater_xy(index))
        if index == len(self.repeater_keys) - 1 and index > 0:
            return None
        return (40.0 + 0.01 * (index + 1), -75.0 + 0.01 * (index + 1))

    # --- the walk course: metres east and north of ORIGIN ------------------------------------------------------------

    @staticmethod
    def _latlon(x: float, y: float) -> tuple[float, float]:
        return (ORIGIN[0] + y / M_PER_DEG_LAT, ORIGIN[1] + x / (M_PER_DEG_LAT * math.cos(math.radians(ORIGIN[0]))))

    @staticmethod
    def _repeater_xy(index: int) -> tuple[float, float]:
        return (REPEATER_SPACING_M * index, 200.0 if index % 2 else 0.0)

    def _course(self) -> list[tuple[float, float]]:
        """The corners of the loop that is walked: round all the repeaters, 400 m beyond the first and last."""
        end = REPEATER_SPACING_M * max(len(self.repeater_keys) - 1, 0)
        return [(-400.0, -150.0), (end + 400.0, -150.0), (end + 400.0, 350.0), (-400.0, 350.0)]

    def walker_xy(self, along: float | None = None) -> tuple[float, float]:
        """Where the walker is, `along` metres round the loop (default: now)."""
        corners = self._course()
        legs = list(zip(corners, corners[1:] + corners[:1]))
        total = sum(math.dist(a, b) for a, b in legs)
        left = (self._walk_at if along is None else along) % total
        for a, b in legs:
            length = math.dist(a, b)
            if left <= length:
                f = left / length
                return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            left -= length
        return corners[0]

    def walker_position(self) -> tuple[float, float]:
        return self._latlon(*self.walker_xy())

    def advertise_repeaters(self) -> int:
        """Every repeater's flood advert is heard by the companion: name, and position when it has one (an unlocated
        repeater advertises 0, 0). Returns the number of adverts sent."""
        for i, key in enumerate(self.repeater_keys):
            lat, lon = self.repeater_position(i) or (0.0, 0.0)
            self._fake.advert(key, self.repeater_name(i), lat=lat, lon=lon, counter=1000 + i)
        return len(self.repeater_keys)

    def announce_names(self) -> int:
        """Every repeater announces the names of all the beacons, as repeaters do on first sight and on their refresh.
        Returns the number of messages queued."""
        queued = 0
        for key in self.repeater_keys:
            entries = [wire.NameEntry(bid, self.beacon_name(i).encode()) for i, bid in enumerate(self.beacon_ids)]
            while entries:
                chunk: list[wire.NameEntry] = []
                size = wire.NAMES_HEADER_LEN
                while entries and size + wire.NAMES_ENTRY_OVERHEAD + len(entries[0].name) <= wire.MAX_GROUP_DATA_LENGTH:
                    size += wire.NAMES_ENTRY_OVERHEAD + len(entries[0].name)
                    chunk.append(entries.pop(0))
                self._fake.enqueue_report(
                    wire.encode_names(key, chunk), channel_index=self._channel_index, data_type=wire.NAMES_DATA_TYPE
                )
                queued += 1
        return queued

    def tick(self) -> int:
        """Every beacon sends once; each repeater reports the beacons it heard. Returns the number of reports queued."""
        heard: dict[int, list[wire.Observation]] = {i: [] for i in range(len(self.repeater_keys))}
        if self._walk:
            self._walk_at += self._walk_step_m
        for b, beacon_id in enumerate(self.beacon_ids):
            self._counters[b] += 1
            self._batt[b] = max(3300, self._batt[b] - self._rng.randrange(0, 2))
            for r in range(len(self.repeater_keys)):
                if self._walk and b == 0:
                    rssi = self._estimator.rssi_at(max(math.dist(self.walker_xy(), self._repeater_xy(r)), 1.0)) + self._rng.gauss(0, 3)
                    if rssi >= SENSITIVITY_DBM:
                        heard[r].append(
                            wire.Observation(beacon_id, self._counters[b], round(rssi), self._rng.randrange(-40, 40), self._batt[b])
                        )
                elif self._rng.random() < 0.7:
                    heard[r].append(
                        wire.Observation(
                            beacon_id=beacon_id,
                            counter=self._counters[b],
                            rssi=self._rng.randrange(-120, -60),
                            snr_x4=self._rng.randrange(-40, 40),
                            batt_mv=self._batt[b],
                        )
                    )
        queued = 0
        for r, observations in heard.items():
            if observations:
                payload = wire.encode_report(self.repeater_keys[r], observations)
                self._fake.enqueue_report(payload, channel_index=self._channel_index)
                queued += 1
        return queued
