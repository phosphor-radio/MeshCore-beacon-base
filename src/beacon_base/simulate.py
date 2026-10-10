"""Synthetic beacon traffic for the fake companion: a handful of beacons heard by a handful of repeaters."""

from __future__ import annotations

import random

from . import wire
from .fake_companion import FakeCompanion


class Simulator:
    def __init__(self, fake: FakeCompanion, channel_index: int = 1, beacons: int = 3, repeaters: int = 3, seed: int = 1):
        self._fake = fake
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
        for b, beacon_id in enumerate(self.beacon_ids):
            self._counters[b] += 1
            self._batt[b] = max(3300, self._batt[b] - self._rng.randrange(0, 2))
            for r in range(len(self.repeater_keys)):
                if self._rng.random() < 0.7:
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
