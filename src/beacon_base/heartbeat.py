"""The ingest heartbeat: one row that ``beacon-ingest`` keeps current so that ``beaconctl`` (and later the web UI) can tell
"ingest is down" from "nothing is happening".

Liveness is judged on the monotonic clock, which every process of a boot shares, so setting the Pi's wall clock (which happens
by hand after boot) cannot make a dead service look alive or the reverse. A row from an earlier boot is never alive.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any

from . import clock
from .store import Store

log = logging.getLogger(__name__)

INTERVAL = 15.0  # seconds between writes when nothing changes (one small row, kind to the SD card)
DOWN_AFTER = 3 * INTERVAL  # ingest is down when the row is older than this

UP = "up"
DOWN = "down"  # the row says running but has not been updated: crashed, killed or hung
STOPPED = "stopped"  # a clean shutdown
NEVER = "never"  # no row: ingest has never run against this database

# fields whose change is written at once; the others (counters and "last seen" times) only go out with the periodic write
EVENT_FIELDS = (
    "connected", "port", "connected_since", "companion_name", "companion_model", "companion_firmware", "companion_key_prefix",
    "clock_trusted", "companion_clock_offset_s", "remote_state",
)
PERIODIC_FIELDS = ("stats", "last_frame_at", "last_report_at")


def state(row: sqlite3.Row | None, mono_now: float | None = None, boot: str | None = None) -> str:
    """UP, DOWN, STOPPED or NEVER for a status row."""
    if row is None:
        return NEVER
    if row["state"] == "stopped":
        return STOPPED
    if row["boot_id"] != (boot or clock.boot_id()):
        return DOWN
    age = (time.monotonic() if mono_now is None else mono_now) - row["updated_mono"]
    return UP if age <= DOWN_AFTER else DOWN


class HeartbeatWriter:
    """Keeps the status row. ``set`` records what is true now; ``tick`` writes when something important changed or the interval
    has passed. A write that fails (the database is busy or locked) is logged and retried on the next tick; it never raises."""

    def __init__(self, store: Store, interval: float = INTERVAL, boot: str | None = None):
        self._store = store
        self._interval = interval
        self._boot = boot or clock.boot_id()
        self._pid = os.getpid()
        self._started = time.time()
        self._values: dict[str, Any] = {"state": "running", "stats": "{}", "remote_state": "idle", "connected": 0, "clock_trusted": 0}
        self._written: dict[str, Any] = {}
        self._last_write: float | None = None
        self._dirty = True

    def set(self, **fields: Any) -> None:
        for name, value in fields.items():
            if name == "stats" and not isinstance(value, str):
                value = json.dumps(value, sort_keys=True)
            if self._values.get(name) != value:
                self._values[name] = value
                if name in EVENT_FIELDS:
                    self._dirty = True

    def value(self, name: str) -> Any:
        return self._values.get(name)

    def tick(self, now: float | None = None) -> bool:
        """Write if due. Returns True when a write happened."""
        mono = time.monotonic() if now is None else now
        if not self._dirty and self._last_write is not None and mono - self._last_write < self._interval:
            return False
        return self.write(mono)

    def write(self, mono: float | None = None) -> bool:
        mono = time.monotonic() if mono is None else mono
        row = dict(self._values)
        row.update(pid=self._pid, boot_id=self._boot, started_at=self._started, updated_at=time.time(), updated_mono=mono)
        try:
            self._store.write_status(row)
        except sqlite3.Error as e:
            log.warning("could not write the ingest heartbeat: %s", e)
            return False
        self._last_write = mono
        self._dirty = False
        return True

    def stop(self) -> None:
        """A clean shutdown: say so, so it is not mistaken for a crash."""
        self.set(state="stopped", connected=0, remote_state="idle")
        self.write()
