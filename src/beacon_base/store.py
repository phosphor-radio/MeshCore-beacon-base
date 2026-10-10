"""SQLite store: schema, migrations and the operator-facing queries.

One SQLite file (WAL mode) is the only interface between ``beacon-ingest``, ``beaconctl`` and, later, ``beacon-web``.
Migrations are numbered and applied on open, tracked in ``PRAGMA user_version``, so a deployed database upgrades in
place. Never edit a released migration, append a new one.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import wire

PUBKEY_LEN = 32

# Statuses of a stored observation.
ACCEPTED = "accepted"
DUPLICATE = "duplicate"  # same (repeater, beacon, counter) as an accepted one
REPLAY = "replay"  # counter below the high-water mark (reason: below_hwm or late)
UNKNOWN_BEACON = "unknown_beacon"
UNKNOWN_REPEATER = "unknown_repeater"
DISABLED = "disabled"  # beacon or repeater is on the list but switched off

REASON_BELOW_HWM = "below_hwm"  # a counter at or below the high-water mark that was never seen: the lockout case
REASON_LATE = "late"  # a transmission that was already seen, reported again after a newer one was accepted

MIGRATIONS: list[tuple[str, ...]] = [
    # 1: allowlist, repeaters, audit trail, observations, transmissions, clock steps
    (
        """CREATE TABLE beacons (
            prefix BLOB PRIMARY KEY CHECK (length(prefix) = 8),  -- the 8-byte key prefix reports identify a beacon by
            name TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            hwm INTEGER,                      -- highest accepted counter; NULL: the next report becomes the baseline
            hwm_at REAL,
            epoch INTEGER NOT NULL DEFAULT 0, -- bumped by a reset, so counters that restart are not deduped against old ones
            notes TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            last_heard_at REAL,
            last_accept_at REAL,
            last_reject_at REAL,
            last_reject_counter INTEGER,
            rejects_since_accept INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE repeaters (
            prefix BLOB PRIMARY KEY CHECK (length(prefix) = 8),
            pubkey BLOB UNIQUE CHECK (pubkey IS NULL OR length(pubkey) = 32),
            name TEXT NOT NULL UNIQUE,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            window_s REAL                     -- the repeater's beacon.window, if known, for 'beaconctl check'
        )""",
        """CREATE TABLE raw_frames (
            id INTEGER PRIMARY KEY,
            rx_time REAL NOT NULL,
            rx_mono REAL NOT NULL,
            boot_id TEXT NOT NULL,
            companion_snr_x4 INTEGER NOT NULL,
            path_len INTEGER NOT NULL,
            late INTEGER NOT NULL,
            outcome TEXT NOT NULL,            -- ok, bad_report
            detail TEXT NOT NULL DEFAULT '',
            payload BLOB NOT NULL
        )""",
        "CREATE INDEX raw_frames_time ON raw_frames (rx_time)",
        """CREATE TABLE transmissions (
            id INTEGER PRIMARY KEY,
            beacon_prefix BLOB NOT NULL,
            epoch INTEGER NOT NULL,
            counter INTEGER NOT NULL,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            n_repeaters INTEGER NOT NULL,
            batt_mv INTEGER NOT NULL,
            final INTEGER NOT NULL DEFAULT 0, -- a newer transmission has been accepted, so no more reports are taken
            UNIQUE (beacon_prefix, epoch, counter)
        )""",
        """CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            raw_id INTEGER NOT NULL REFERENCES raw_frames (id),
            rx_time REAL NOT NULL,
            rx_mono REAL NOT NULL,
            boot_id TEXT NOT NULL,
            time_trusted INTEGER NOT NULL,
            repeater_prefix BLOB NOT NULL,
            beacon_prefix BLOB NOT NULL,
            epoch INTEGER NOT NULL,
            counter INTEGER NOT NULL,
            rssi INTEGER NOT NULL,
            snr_x4 INTEGER NOT NULL,
            batt_mv INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            transmission_id INTEGER REFERENCES transmissions (id)
        )""",
        """CREATE UNIQUE INDEX observations_accepted
            ON observations (repeater_prefix, beacon_prefix, epoch, counter) WHERE status = 'accepted'""",
        "CREATE INDEX observations_beacon ON observations (beacon_prefix, id)",
        "CREATE INDEX observations_status ON observations (status, id)",
        "CREATE INDEX observations_time ON observations (rx_time)",
        "CREATE INDEX observations_boot ON observations (boot_id)",
        """CREATE TABLE clock_events (
            id INTEGER PRIMARY KEY,
            at_wall REAL NOT NULL,            -- the new wall clock when it was observed
            boot_id TEXT NOT NULL,
            kind TEXT NOT NULL,               -- set (beaconctl time set), step (seen by ingest)
            offset_before REAL NOT NULL,      -- wall minus monotonic clock
            offset_after REAL NOT NULL
        )""",
        "CREATE INDEX clock_events_boot ON clock_events (boot_id)",
    ),
]


class StoreError(Exception):
    """An operator error: unknown name, duplicate, collision, bad value."""


@dataclass(frozen=True)
class ResetInfo:
    name: str
    old_hwm: int | None
    last_reject_counter: int | None
    rejects_since_accept: int


def _hex(b: bytes) -> str:
    return b.hex()


def parse_hex_key(text: str, what: str, lengths: tuple[int, ...]) -> bytes:
    try:
        raw = bytes.fromhex(text.strip())
    except ValueError:
        raise StoreError(f"{what} must be hexadecimal") from None
    if len(raw) not in lengths:
        want = " or ".join(f"{n} bytes ({2 * n} hex characters)" for n in lengths)
        raise StoreError(f"{what} must be {want}, got {len(raw)} bytes")
    return raw


class Store:
    def __init__(self, conn: sqlite3.Connection, path: str):
        self.conn = conn
        self.path = path

    @classmethod
    def open(cls, path: str | Path) -> "Store":
        path = str(path)
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: transactions are explicit (see transaction()). check_same_thread is off because a
        # Store may be created in one thread and used in another, never concurrently.
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        store = cls(conn, path)
        store._migrate()
        return store

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- migrations ------------------------------------------------------------------------------------------------

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        if self.schema_version() > len(MIGRATIONS):
            raise StoreError(
                f"{self.path} has schema version {self.schema_version()}, newer than this software "
                f"({len(MIGRATIONS)}); upgrade beacon-base"
            )
        while self.schema_version() < len(MIGRATIONS):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                version = self.schema_version()  # re-read: another process may have migrated while we waited
                if version < len(MIGRATIONS):
                    for statement in MIGRATIONS[version]:
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version = {version + 1}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction: everything inside commits together or not at all."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # --- beacons -------------------------------------------------------------------------------------------------------

    def add_beacon(self, name: str, key_hex: str, notes: str = "", now: float | None = None) -> sqlite3.Row:
        """Allowlist a beacon by the 8-byte public key prefix its reports carry (16 hex characters). A full 64-character
        key is accepted for convenience, for example pasted from the beacon's serial 'pubkey' command; only its prefix is
        kept, since that is all the base ever sees."""
        raw = parse_hex_key(key_hex, "beacon key prefix", (wire.ID_LEN, PUBKEY_LEN))
        name = self._check_name(name)
        prefix = raw[: wire.ID_LEN]
        with self.transaction() as db:
            by_prefix = db.execute("SELECT name FROM beacons WHERE prefix = ?", (prefix,)).fetchone()
            if by_prefix is not None:
                raise StoreError(f"beacon {by_prefix['name']!r} already has the prefix {_hex(prefix)}")
            if db.execute("SELECT 1 FROM beacons WHERE name = ?", (name,)).fetchone() is not None:
                raise StoreError(f"a beacon named {name!r} already exists")
            db.execute(
                "INSERT INTO beacons (prefix, name, notes, created_at) VALUES (?, ?, ?, ?)",
                (prefix, name, notes, time.time() if now is None else now),
            )
        return self.beacon(name)

    @staticmethod
    def _check_name(name: str) -> str:
        name = name.strip()
        if not name or any(c.isspace() for c in name):
            raise StoreError("names must be non-empty and contain no whitespace")
        return name

    def beacon(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM beacons WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise StoreError(f"no beacon named {name!r}")
        return row

    def beacons(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM beacons ORDER BY name").fetchall()

    def remove_beacon(self, name: str) -> None:
        with self.transaction() as db:
            self.beacon(name)
            db.execute("DELETE FROM beacons WHERE name = ?", (name,))

    def set_beacon_enabled(self, name: str, enabled: bool) -> None:
        with self.transaction() as db:
            self.beacon(name)
            db.execute("UPDATE beacons SET enabled = ? WHERE name = ?", (int(enabled), name))

    def reset_beacon(self, name: str) -> ResetInfo:
        """Clear the high-water mark so the next report becomes the new baseline, and clear the rejected state."""
        with self.transaction() as db:
            b = self.beacon(name)
            db.execute(
                "UPDATE beacons SET hwm = NULL, hwm_at = NULL, epoch = epoch + 1, rejects_since_accept = 0 WHERE name = ?",
                (name,),
            )
        return ResetInfo(name, b["hwm"], b["last_reject_counter"], b["rejects_since_accept"])

    # --- repeaters -------------------------------------------------------------------------------------------------

    def add_repeater(
        self, name: str, key_hex: str, lat: float, lon: float, window_s: float | None = None
    ) -> sqlite3.Row:
        raw = parse_hex_key(key_hex, "repeater public key or prefix", (wire.ID_LEN, PUBKEY_LEN))
        name = self._check_name(name)
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise StoreError("latitude must be within +/-90 and longitude within +/-180")
        if window_s is not None and window_s <= 0:
            raise StoreError("window must be positive")
        prefix = raw[: wire.ID_LEN]
        pubkey = raw if len(raw) == PUBKEY_LEN else None
        with self.transaction() as db:
            clash = db.execute("SELECT name, prefix FROM repeaters WHERE prefix = ? OR name = ?", (prefix, name)).fetchone()
            if clash is not None:
                if bytes(clash["prefix"]) == prefix:
                    raise StoreError(f"repeater {clash['name']!r} already has the prefix {_hex(prefix)}")
                raise StoreError(f"a repeater named {name!r} already exists")
            db.execute(
                "INSERT INTO repeaters (prefix, pubkey, name, lat, lon, window_s) VALUES (?, ?, ?, ?, ?, ?)",
                (prefix, pubkey, name, lat, lon, window_s),
            )
        return self.repeater(name)

    def repeater(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM repeaters WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise StoreError(f"no repeater named {name!r}")
        return row

    def repeaters(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM repeaters ORDER BY name").fetchall()

    def remove_repeater(self, name: str) -> None:
        with self.transaction() as db:
            self.repeater(name)
            db.execute("DELETE FROM repeaters WHERE name = ?", (name,))

    def set_repeater_enabled(self, name: str, enabled: bool) -> None:
        with self.transaction() as db:
            self.repeater(name)
            db.execute("UPDATE repeaters SET enabled = ? WHERE name = ?", (int(enabled), name))

    def set_repeater_window(self, name: str, window_s: float | None) -> None:
        if window_s is not None and window_s <= 0:
            raise StoreError("window must be positive")
        with self.transaction() as db:
            self.repeater(name)
            db.execute("UPDATE repeaters SET window_s = ? WHERE name = ?", (window_s, name))

    def names(self) -> tuple[dict[bytes, str], dict[bytes, str]]:
        """(beacon prefix -> name, repeater prefix -> name), for display."""
        beacons = {bytes(r["prefix"]): r["name"] for r in self.conn.execute("SELECT prefix, name FROM beacons")}
        repeaters = {bytes(r["prefix"]): r["name"] for r in self.conn.execute("SELECT prefix, name FROM repeaters")}
        return beacons, repeaters

    # --- observations --------------------------------------------------------------------------------------------------

    def rejects(self, beacon: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        """Recent observations that were not accepted (newest first), with the beacon's current high-water mark."""
        sql = """SELECT o.*, b.name AS beacon_name, b.hwm AS beacon_hwm, r.name AS repeater_name
                 FROM observations o
                 LEFT JOIN beacons b ON b.prefix = o.beacon_prefix
                 LEFT JOIN repeaters r ON r.prefix = o.repeater_prefix
                 WHERE o.status NOT IN ('accepted', 'duplicate')"""
        args: list = []
        if beacon is not None:
            prefix = bytes(self.beacon(beacon)["prefix"])
            sql += " AND o.beacon_prefix = ?"
            args.append(prefix)
        sql += " ORDER BY o.id DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def bad_reports(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM raw_frames WHERE outcome != 'ok' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def unknown_beacons(self, since: float) -> list[sqlite3.Row]:
        """Beacon prefixes that were reported but are not on the allowlist, one row per prefix."""
        return self.conn.execute(
            """SELECT beacon_prefix, count(*) AS n, max(rx_time) AS last_seen, max(counter) AS last_counter,
                      count(DISTINCT repeater_prefix) AS n_repeaters
               FROM observations WHERE status = ? AND rx_time >= ? AND beacon_prefix NOT IN (SELECT prefix FROM beacons)
               GROUP BY beacon_prefix ORDER BY last_seen DESC""",
            (UNKNOWN_BEACON, since),
        ).fetchall()

    def unknown_repeaters(self, since: float) -> list[sqlite3.Row]:
        """Repeater prefixes reporting known beacons but not in the repeater table."""
        return self.conn.execute(
            """SELECT repeater_prefix, count(*) AS n, max(rx_time) AS last_seen
               FROM observations WHERE status = ? AND rx_time >= ? AND repeater_prefix NOT IN (SELECT prefix FROM repeaters)
               GROUP BY repeater_prefix ORDER BY last_seen DESC""",
            (UNKNOWN_REPEATER, since),
        ).fetchall()

    def latest_transmission(self, prefix: bytes) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM transmissions WHERE beacon_prefix = ? ORDER BY last_seen DESC, id DESC LIMIT 1", (prefix,)
        ).fetchone()
