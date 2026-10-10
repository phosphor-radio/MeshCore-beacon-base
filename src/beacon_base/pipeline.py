"""Replay and dedupe pipeline: the authoritative replay protection (project plan, "Replay checks at the base").

``Pipeline.process`` handles one received report in one database transaction, so a crash leaves either the whole report
applied or none of it, and re-running it on an unchanged database gives the same result. For each entry:

1. **Repeater known.** An unknown repeater prefix is stored as ``unknown_repeater`` and nothing else changes, so a repeater
   that is not trusted can neither move a high-water mark nor get a beacon trusted. (It is still listed, with the beacons
   it reported, so the operator can add them.) With auto-add for repeaters on, it is trusted here and processing goes on.
2. **Allowlist.** An unknown beacon prefix, from a trusted repeater, is stored as ``unknown_beacon`` and nothing else
   changes; with auto-add for beacons on it is trusted here and processing goes on (this report becomes its baseline).
3. **High-water mark.** No mark yet: accept and take the counter as the baseline. A lower counter is rejected as
   ``replay``. An equal counter is the current transmission and joins its group. A higher counter is a new transmission:
   accept and advance the mark.
4. **Dedupe.** The same ``(repeater, beacon, counter)`` twice is stored as ``duplicate``.
5. **Group.** Accepted observations of one ``(beacon, counter)`` form a transmission.

Reset (``Store.reset_beacon``) clears the mark and bumps the beacon's epoch. Dedupe and grouping are scoped to the epoch,
so counters that legitimately restart after a reset are not mistaken for old ones.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from . import clock, names, store, wire
from .ingest import ReceivedNames, ReceivedReport
from .store import (
    ACCEPTED, AUTOADD_BEACONS, AUTOADD_REPEATERS, DISABLED, DUPLICATE, REPLAY, UNKNOWN_BEACON, UNKNOWN_REPEATER, Store,
)

log = logging.getLogger(__name__)


class RawFrameLike(Protocol):
    payload: bytes
    companion_snr_x4: int
    path_len: int
    rx_wall: float
    rx_mono: float
    late: bool


@dataclass(frozen=True)
class NameChange:
    """A stored beacon name: old is None the first time the name is learned."""

    beacon_prefix: bytes
    old: str | None
    new: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


@dataclass(frozen=True)
class Verdict:
    """What happened to one observation."""

    beacon_prefix: bytes
    repeater_prefix: bytes
    counter: int
    status: str
    reason: str = ""
    beacon_name: str | None = None
    repeater_name: str | None = None
    transmission_id: int | None = None
    auto_added: str = ""  # "repeater", "beacon" or "repeater+beacon": trusted by auto-add while processing this entry


class Pipeline:
    def __init__(self, store: Store, assume_synced: bool = False, boot: str | None = None):
        self._store = store
        self._assume_synced = assume_synced
        self._boot = boot or clock.boot_id()

    @property
    def boot(self) -> str:
        return self._boot

    def process(self, rx: ReceivedReport) -> list[Verdict]:
        """Store the raw frame and judge every observation in it, atomically."""
        with self._store.transaction() as db:
            trusted = clock.is_trusted(db, self._boot, self._assume_synced)
            raw_id = self._insert_raw(db, rx, "ok", "")
            auto = (self._store._flag(db, AUTOADD_REPEATERS), self._store._flag(db, AUTOADD_BEACONS))
            return [self._observe(db, rx, raw_id, trusted, rx.report.repeater_id, o, auto) for o in rx.report.observations]

    def record_bad_report(self, rx: RawFrameLike, detail: str, outcome: str = "bad_report") -> None:
        """Keep the audit trail for a frame that is on the report channel but does not decode."""
        with self._store.transaction() as db:
            self._insert_raw(db, rx, outcome, detail)

    def process_names(self, rx: ReceivedNames) -> list[NameChange]:
        """Store the names a repeater announced, from any repeater (trusted or not) so an unlisted beacon can be recognised before
        it is added. A name that cleans down to nothing is skipped and the latest announcement wins. Names are display only and
        announcements are encrypted with the channel key, so only a holder of it can send one."""
        changes = []
        with self._store.transaction():
            for entry in rx.announcement.entries:
                name = names.sanitize_name(entry.name)
                if name is None:
                    continue
                old = self._store.record_name(entry.beacon_id, name, rx.announcement.repeater_id, rx.rx_wall)
                changes.append(NameChange(entry.beacon_id, old, name))
        return changes

    # --- internals --------------------------------------------------------------------------------------------------

    def _insert_raw(self, db: sqlite3.Connection, rx: RawFrameLike, outcome: str, detail: str) -> int:
        cur = db.execute(
            """INSERT INTO raw_frames (rx_time, rx_mono, boot_id, companion_snr_x4, path_len, late, outcome, detail, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rx.rx_wall, rx.rx_mono, self._boot, rx.companion_snr_x4, rx.path_len, int(rx.late), outcome, detail, rx.payload),
        )
        return cur.lastrowid

    def _observe(
        self,
        db: sqlite3.Connection,
        rx: RawFrameLike,
        raw_id: int,
        trusted: bool,
        repeater_prefix: bytes,
        o: wire.Observation,
        auto: tuple[bool, bool] = (False, False),
    ) -> Verdict:
        now = rx.rx_wall
        auto_repeaters, auto_beacons = auto
        auto_added = []
        if auto_repeaters and db.execute("SELECT 1 FROM repeaters WHERE prefix = ?", (repeater_prefix,)).fetchone() is None:
            self._store._insert_repeater(db, repeater_prefix, None, None, None, None, None, now)
            auto_added.append("repeater")
        beacon = db.execute(Store._BEACON_SELECT + " WHERE b.prefix = ?", (o.beacon_id,)).fetchone()
        repeater = db.execute("SELECT * FROM repeaters WHERE prefix = ?", (repeater_prefix,)).fetchone()
        epoch = beacon["epoch"] if beacon is not None else 0

        def record(status: str, reason: str = "", transmission_id: int | None = None) -> Verdict:
            db.execute(
                """INSERT INTO observations (raw_id, rx_time, rx_mono, boot_id, time_trusted, repeater_prefix, beacon_prefix,
                                             epoch, counter, rssi, snr_x4, batt_mv, status, reason, transmission_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (raw_id, now, rx.rx_mono, self._boot, int(trusted), repeater_prefix, o.beacon_id, epoch, o.counter, o.rssi,
                 o.snr_x4, o.batt_mv, status, reason, transmission_id),
            )
            return Verdict(
                o.beacon_id,
                repeater_prefix,
                o.counter,
                status,
                reason,
                beacon["name"] if beacon is not None else None,
                repeater["name"] if repeater is not None else None,
                transmission_id,
                "+".join(auto_added),
            )

        # Repeaters are checked first: they are the infrastructure, set up before the beacons, and a stranger must not be able
        # to get its beacons listed or move a high-water mark.
        if repeater is None:
            return record(UNKNOWN_REPEATER)  # the high-water mark is deliberately not touched
        if not repeater["enabled"]:
            return record(DISABLED, "repeater")
        if beacon is None and auto_beacons:  # only a trusted, enabled repeater can get a beacon trusted
            db.execute("INSERT INTO beacons (prefix, created_at) VALUES (?, ?)", (o.beacon_id, now))
            beacon = db.execute(Store._BEACON_SELECT + " WHERE b.prefix = ?", (o.beacon_id,)).fetchone()
            auto_added.append("beacon")
        if beacon is None:
            return record(UNKNOWN_BEACON)
        if not beacon["enabled"]:
            return record(DISABLED, "beacon")

        hwm = beacon["hwm"]
        if hwm is not None and o.counter < hwm:
            seen = db.execute(
                "SELECT 1 FROM transmissions WHERE beacon_prefix = ? AND epoch = ? AND counter = ?",
                (o.beacon_id, epoch, o.counter),
            ).fetchone()
            if seen is not None:
                # A real transmission reported after a newer one was accepted. Not a lockout, so it does not count
                # towards the beacon's rejected state.
                db.execute("UPDATE beacons SET last_heard_at = ? WHERE prefix = ?", (now, o.beacon_id))
                return record(REPLAY, store.REASON_LATE)
            db.execute(
                """UPDATE beacons SET last_heard_at = ?, last_reject_at = ?, last_reject_counter = ?,
                          rejects_since_accept = rejects_since_accept + 1 WHERE prefix = ?""",
                (now, now, o.counter, o.beacon_id),
            )
            return record(REPLAY, store.REASON_BELOW_HWM)

        if hwm is None or o.counter > hwm:
            if hwm is not None:
                db.execute(
                    "UPDATE transmissions SET final = 1 WHERE beacon_prefix = ? AND epoch = ? AND counter = ?",
                    (o.beacon_id, epoch, hwm),
                )
            tx_id = db.execute(
                """INSERT INTO transmissions (beacon_prefix, epoch, counter, first_seen, last_seen, n_repeaters, batt_mv)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (o.beacon_id, epoch, o.counter, now, now, o.batt_mv),
            ).lastrowid
            db.execute(
                """UPDATE beacons SET hwm = ?, hwm_at = ?, last_heard_at = ?, last_accept_at = ?, rejects_since_accept = 0
                   WHERE prefix = ?""",
                (o.counter, now, now, now, o.beacon_id),
            )
            return record(ACCEPTED, transmission_id=tx_id)

        # counter == hwm: another report of the current transmission
        tx = db.execute(
            "SELECT id FROM transmissions WHERE beacon_prefix = ? AND epoch = ? AND counter = ?", (o.beacon_id, epoch, o.counter)
        ).fetchone()
        if tx is None:  # cannot happen unless the database was edited by hand; repair rather than lose the observation
            log.warning("no transmission for %s counter %d at the high-water mark, creating it", o.beacon_id.hex(), o.counter)
            tx_id = db.execute(
                """INSERT INTO transmissions (beacon_prefix, epoch, counter, first_seen, last_seen, n_repeaters, batt_mv)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (o.beacon_id, epoch, o.counter, now, now, o.batt_mv),
            ).lastrowid
        else:
            tx_id = tx["id"]
        db.execute("UPDATE beacons SET last_heard_at = ? WHERE prefix = ?", (now, o.beacon_id))
        dup = db.execute(
            """SELECT 1 FROM observations WHERE repeater_prefix = ? AND beacon_prefix = ? AND epoch = ? AND counter = ?
               AND status = 'accepted'""",
            (repeater_prefix, o.beacon_id, epoch, o.counter),
        ).fetchone()
        if dup is not None:
            return record(DUPLICATE, transmission_id=tx_id)
        verdict = record(ACCEPTED, transmission_id=tx_id)
        db.execute(
            """UPDATE transmissions SET last_seen = max(last_seen, ?), batt_mv = ?,
                      n_repeaters = (SELECT count(DISTINCT repeater_prefix) FROM observations
                                     WHERE transmission_id = ? AND status = 'accepted')
               WHERE id = ?""",
            (now, o.batt_mv, tx_id, tx_id),
        )
        db.execute("UPDATE beacons SET last_accept_at = ?, rejects_since_accept = 0 WHERE prefix = ?", (now, o.beacon_id))
        return verdict
