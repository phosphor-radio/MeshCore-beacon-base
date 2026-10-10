"""Time handling.

Beacons have no clock and reports carry no time, so the base stamps observations with the Pi's clock. The Pi has no
internet in the field and its clock is set by hand after boot, so until then it is wrong. Every observation therefore
also stores ``rx_mono`` (monotonic seconds) and the ``boot_id``. Once the clock is set, the wall time of every earlier
observation from the same boot is recomputed as ``rx_mono + offset`` and ``time_trusted`` becomes true.

``offset`` is wall clock minus monotonic clock. It is constant while the clock runs undisturbed, so a change of more than
STEP_THRESHOLD seconds means the clock was set.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from functools import lru_cache

STEP_THRESHOLD = 5.0  # seconds; smaller changes are slewing, not a clock being set


@lru_cache(maxsize=1)
def boot_id() -> str:
    """Identifies this boot of the machine. Monotonic timestamps are only comparable within one boot."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return uuid.uuid4().hex  # not Linux: one id per process, which is the safe direction


def offset() -> float:
    """Wall clock minus monotonic clock, in seconds."""
    return time.time() - time.monotonic()


def is_trusted(db: sqlite3.Connection, boot: str, assume_synced: bool) -> bool:
    """True once the clock is known to be right for this boot: it was set or confirmed, or the config says it is synced."""
    if assume_synced:
        return True
    return db.execute("SELECT 1 FROM clock_events WHERE boot_id = ? LIMIT 1", (boot,)).fetchone() is not None


def last_event(db: sqlite3.Connection, boot: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM clock_events WHERE boot_id = ? ORDER BY id DESC LIMIT 1", (boot,)).fetchone()


def apply_clock_event(
    db: sqlite3.Connection, boot: str, kind: str, offset_before: float, offset_after: float, now_wall: float
) -> int:
    """Record that the clock is right from now on and fix the times of this boot's untrusted observations.

    Call inside a transaction. Returns the number of observations rewritten.
    """
    db.execute(
        "INSERT INTO clock_events (at_wall, boot_id, kind, offset_before, offset_after) VALUES (?, ?, ?, ?, ?)",
        (now_wall, boot, kind, offset_before, offset_after),
    )
    db.execute("UPDATE raw_frames SET rx_time = rx_mono + ? WHERE boot_id = ?", (offset_after, boot))
    cur = db.execute(
        "UPDATE observations SET rx_time = rx_mono + ?, time_trusted = 1 WHERE boot_id = ? AND time_trusted = 0",
        (offset_after, boot),
    )
    rewritten = cur.rowcount
    if rewritten:
        _refresh_derived_times(db, boot)
    return rewritten


def _refresh_derived_times(db: sqlite3.Connection, boot: str) -> None:
    """Recompute the times denormalised onto transmissions and beacons from the (now correct) observations."""
    db.execute(
        """UPDATE transmissions SET
             first_seen = COALESCE((SELECT min(rx_time) FROM observations o
                                    WHERE o.transmission_id = transmissions.id AND o.status = 'accepted'), first_seen),
             last_seen = COALESCE((SELECT max(rx_time) FROM observations o
                                   WHERE o.transmission_id = transmissions.id AND o.status = 'accepted'), last_seen)
           WHERE id IN (SELECT transmission_id FROM observations WHERE boot_id = ? AND transmission_id IS NOT NULL)""",
        (boot,),
    )
    db.execute(
        """UPDATE beacons SET
             last_heard_at = COALESCE((SELECT max(rx_time) FROM observations o WHERE o.beacon_prefix = beacons.prefix
                                       AND o.status IN ('accepted', 'duplicate', 'replay')), last_heard_at),
             last_accept_at = COALESCE((SELECT max(rx_time) FROM observations o WHERE o.beacon_prefix = beacons.prefix
                                        AND o.status = 'accepted'), last_accept_at),
             last_reject_at = COALESCE((SELECT max(rx_time) FROM observations o WHERE o.beacon_prefix = beacons.prefix
                                        AND o.status = 'replay' AND o.reason = 'below_hwm'), last_reject_at),
             hwm_at = COALESCE((SELECT first_seen FROM transmissions t WHERE t.beacon_prefix = beacons.prefix
                                AND t.epoch = beacons.epoch AND t.counter = beacons.hwm), hwm_at)
           WHERE prefix IN (SELECT beacon_prefix FROM observations WHERE boot_id = ?)""",
        (boot,),
    )
