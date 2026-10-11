"""utilities/advert_probe.py end to end against the fake companion and repeater: one run per verdict it can reach.

The script is run as a subprocess, as an operator would run it, with its own serial link to the fake's pty. The fake companion
has no radio, so a thread plays the air: when the fake repeater is told to advert, it writes a raw radio log frame (0x88) holding
an advert packet, and optionally the push and the updated contact that a real companion would produce.
"""

import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from beacon_base.fake_companion import FakeCompanion
from beacon_base.fake_repeater import PERM_ADMIN, Client, FakeRepeater

SCRIPT = Path(__file__).resolve().parent.parent / "utilities" / "advert_probe.py"
KEY = bytes(range(32))
OLD = (10.0, 20.0)  # the stale position the companion holds
NEW = (47.123456, -122.335167)  # what the repeater was set to


def raw_advert(ts, pos, name="Roof"):
    """A flood advert packet (route 1, type 4) as the companion's raw log shows it: header, path length, key, time, signature, app data."""
    flags = 2 | 0x80 | (0x10 if pos else 0)
    app = bytes([flags]) + (struct.pack("<ii", round(pos[0] * 1e6), round(pos[1] * 1e6)) if pos else b"") + name.encode()
    return bytes([0x11, 0]) + KEY + struct.pack("<I", ts) + bytes(64) + app


def probe(advert_pos, *, send_advert=True, push=False, contact_ts_offset=-100, wait=8):
    now = int(time.time())
    fake = FakeCompanion(rtc_start=now)
    fake.contacts[KEY] = (2, "Roof", now + contact_ts_offset, OLD[0], OLD[1])
    repeater = fake.add_repeater(FakeRepeater(KEY, name="Roof", lat=NEW[0], lon=NEW[1]))
    repeater.acl[fake.public_key] = Client(PERM_ADMIN)
    stop = threading.Event()

    def air():
        seen = 0
        while not stop.is_set():
            if repeater.adverts_sent > seen:
                seen = repeater.adverts_sent
                if send_advert:
                    time.sleep(0.5)
                    fake.write_frame(bytes([0x88, 40, 0xB0]) + raw_advert(now, advert_pos))
                    if push:
                        fake.contacts[KEY] = (2, "Roof", now, *(advert_pos or OLD))
                        fake.write_frame(bytes([0x80]) + KEY)
            time.sleep(0.05)

    thread = threading.Thread(target=air, daemon=True)
    thread.start()
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPT), fake.path, "roof", "--dtr", "on", "--wait", str(wait)],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        stop.set()
        fake.close()
    return out.stdout + out.stderr


def test_advert_without_a_position():
    text = probe(None)
    assert "logged in as admin" in text
    assert "adverts from it heard off the air: 1" in text
    assert "47.123455" in text  # what the repeater says (the float read-back is lossy)
    assert "carries NO position" in text


def test_right_position_but_the_companion_drops_it_as_a_replay():
    text = probe(NEW, contact_ts_offset=1000)  # the companion's record of the repeater is newer than the advert
    assert "DROPPED the advert as a replay" in text


def test_everything_right_on_the_radio_side():
    text = probe(NEW, push=True)
    assert "Everything on the radio side is right" in text


def test_no_advert_heard_is_inconclusive():
    text = probe(NEW, send_advert=False, wait=3)
    assert "INCONCLUSIVE" in text


def test_advert_position_differs_from_what_the_repeater_reports():
    text = probe((11.0, 22.0), push=True)
    assert "transmitting a different position" in text


def test_advert_at_zero_zero_is_unlocated():
    text = probe((0.0, 0.0), push=True)
    assert "advertises the position 0, 0 (unlocated)" in text


def test_the_clock_is_left_alone_with_no_set_clock():
    # the fake companion's clock starts behind the host's by more than a minute
    now = int(time.time())
    fake = FakeCompanion(rtc_start=now - 3600)
    fake.contacts[KEY] = (2, "Roof", now - 100, *OLD)
    repeater = fake.add_repeater(FakeRepeater(KEY, name="Roof", lat=NEW[0], lon=NEW[1]))
    repeater.acl[fake.public_key] = Client(PERM_ADMIN)
    try:
        out = subprocess.run(
            [sys.executable, str(SCRIPT), fake.path, "roof", "--dtr", "on", "--wait", "1", "--no-set-clock"],
            capture_output=True, text=True, timeout=120,
        )
    finally:
        fake.close()
    assert "not changing it" in out.stdout
    assert "moved the companion's clock" not in out.stdout
