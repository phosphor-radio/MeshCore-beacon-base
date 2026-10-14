"""Placeholder location estimator (plan: docs/plan/web-map.md, decision 2).

Given what the located repeaters heard of one transmission, say roughly where the beacon was:

- one repeater: a *range circle* around it, the radius from the RSSI through a log-distance path loss model;
- two or more: a *weighted centroid* of the repeaters (weights are the received power in linear units, so the closest repeater
  counts most), with a radius that is the larger of the spread of the repeaters about the centroid and half the range of the
  strongest one.

This is deliberately crude. Real estimation, tuned against walk tests, is milestone 6; the ``method`` field says which of the two
produced a position so a map can draw them differently. Pure functions, no I/O: the pipeline gathers the hearings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import EstimateConfig

METHOD_RANGE = "range"
METHOD_CENTROID = "centroid"
EARTH_RADIUS_M = 6_371_000.0
SPEED_OF_LIGHT = 299_792_458.0


@dataclass(frozen=True)
class Hearing:
    """One repeater's report of a transmission: where the repeater is and how strongly it heard the beacon."""

    lat: float
    lon: float
    rssi: int


@dataclass(frozen=True)
class Position:
    lat: float
    lon: float
    radius_m: float
    method: str
    n_repeaters: int


def free_space_loss_db(distance_m: float, freq_hz: float) -> float:
    return 20 * math.log10(4 * math.pi * distance_m * freq_hz / SPEED_OF_LIGHT)


class Estimator:
    def __init__(self, cfg: EstimateConfig | None = None, freq_khz: int = 905775):
        self.cfg = cfg or EstimateConfig()
        # received power at one metre: the effective transmit power less the free space loss over that metre
        self._rssi_at_1m = self.cfg.tx_power_dbm - free_space_loss_db(1.0, freq_khz * 1000.0)

    def range_m(self, rssi: float) -> float:
        """How far away a beacon heard at this RSSI probably is, kept between the configured minimum and maximum radius."""
        metres = 10 ** ((self._rssi_at_1m - rssi) / (10 * self.cfg.path_loss_exponent))
        return min(max(metres, self.cfg.min_radius_m), self.cfg.max_radius_m)

    def rssi_at(self, distance_m: float) -> float:
        """The inverse of range_m, without the clamping: what the model predicts at a distance (used by the simulator)."""
        return self._rssi_at_1m - 10 * self.cfg.path_loss_exponent * math.log10(max(distance_m, 1.0))

    def estimate(self, hearings: list[Hearing]) -> Position | None:
        """None when no located repeater heard it."""
        if not hearings:
            return None
        strongest = max(hearings, key=lambda h: h.rssi)
        if len(hearings) == 1:
            return Position(strongest.lat, strongest.lon, self.range_m(strongest.rssi), METHOD_RANGE, 1)
        # work in metres east and north of the strongest repeater: right at the size of this event, and no trouble at 180 degrees
        lat0, lon0 = strongest.lat, strongest.lon
        cos0 = math.cos(math.radians(lat0))
        points = []
        for h in hearings:
            dlon = (h.lon - lon0 + 180.0) % 360.0 - 180.0
            points.append((math.radians(dlon) * cos0 * EARTH_RADIUS_M, math.radians(h.lat - lat0) * EARTH_RADIUS_M, 10 ** (h.rssi / 10)))
        total = sum(w for _, _, w in points)
        x = sum(px * w for px, _, w in points) / total
        y = sum(py * w for _, py, w in points) / total
        spread = math.sqrt(sum(w * ((px - x) ** 2 + (py - y) ** 2) for px, py, w in points) / total)
        radius = min(max(spread, self.range_m(strongest.rssi) / 2, self.cfg.min_radius_m), self.cfg.max_radius_m)
        lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
        lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * cos0)) if cos0 > 1e-9 else lon0
        lon = (lon + 180.0) % 360.0 - 180.0
        return Position(lat, lon, radius, METHOD_CENTROID, len(hearings))
