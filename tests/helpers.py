"""Builders shared by the pipeline and CLI tests."""

from beacon_base import wire
from beacon_base.ingest import ReceivedReport

BEACON_KEY = bytes(range(1, 33))  # prefix 0102030405060708
BEACON_PREFIX = BEACON_KEY[:8]
BEACON2_KEY = bytes(range(101, 133))
BEACON2_PREFIX = BEACON2_KEY[:8]
REPEATER_A_KEY = bytes(range(0xA0, 0xC0))
REPEATER_B_KEY = bytes(range(0xB0, 0xD0))
REPEATER_C_KEY = bytes(range(0xC0, 0xE0))


def obs(counter, beacon=BEACON_PREFIX, rssi=-90, snr_x4=-8, batt=3800):
    return wire.Observation(beacon, counter, rssi, snr_x4, batt)


def rx(repeater_key, *observations, t=1000.0, mono=100.0, late=False, snr_x4=20):
    payload = wire.encode_report(repeater_key, list(observations))
    return ReceivedReport(wire.decode_report(payload), snr_x4, 1, t, mono, late, payload)

B1 = BEACON_PREFIX.hex()    # how the CLI and store address beacons: by key prefix
B2 = BEACON2_PREFIX.hex()
