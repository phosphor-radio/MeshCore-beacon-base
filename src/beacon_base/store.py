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

from . import names as names_mod
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
        """CREATE TABLE beacon_names (
            prefix BLOB PRIMARY KEY CHECK (length(prefix) = 8),  -- announced by repeaters, so it exists before the beacon is added
            name TEXT NOT NULL,
            first_seen REAL NOT NULL,
            updated_at REAL NOT NULL,
            repeater_prefix BLOB NOT NULL     -- who announced it last
        )""",
        """CREATE TABLE repeaters (
            prefix BLOB PRIMARY KEY CHECK (length(prefix) = 8),
            pubkey BLOB UNIQUE CHECK (pubkey IS NULL OR length(pubkey) = 32),
            name TEXT,                        -- operator-assigned, optional, not unique
            lat REAL NOT NULL DEFAULT 0,      -- 0, 0 means unlocated: the repeater is excluded from positioning
            lon REAL NOT NULL DEFAULT 0,
            location_source TEXT NOT NULL DEFAULT 'none',   -- none, advert or manual: whoever wrote it last
            location_updated_at REAL,
            enabled INTEGER NOT NULL DEFAULT 1,
            window_s REAL                     -- the repeater's beacon.window, if known, for 'beaconctl check'
        )""",
        """CREATE TABLE repeater_adverts (
            prefix BLOB PRIMARY KEY CHECK (length(prefix) = 8),  -- heard before or after the repeater is trusted
            pubkey BLOB NOT NULL CHECK (length(pubkey) = 32),
            name TEXT,
            lat REAL,                         -- NULL until an advert carries a valid position (0, 0 and out of range are not)
            lon REAL,
            advert_timestamp INTEGER,
            first_seen REAL NOT NULL,
            last_heard REAL                   -- NULL: only known from the companion's contact list, never heard live
        )""",
        """CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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


@dataclass
class AdvertEffect:
    """What taking a repeater advert changed. position and name are set when they are new or different."""

    prefix: bytes
    trusted: bool  # the repeater is in the table
    position: tuple[float, float] | None = None
    name: str | None = None
    old_name: str | None = None
    old_position: tuple[float, float] | None = None
    added: bool = False  # the repeater was not trusted and auto-add trusted it


@dataclass(frozen=True)
class ResetInfo:
    prefix: bytes
    name: str | None
    old_hwm: int | None
    last_reject_counter: int | None
    rejects_since_accept: int


AUTOADD_BEACONS = "autoadd.beacons"
AUTOADD_REPEATERS = "autoadd.repeaters"


def _autoadd_key(kind: str) -> str:
    if kind not in ("beacons", "repeaters"):
        raise StoreError(f"auto-add applies to 'beacons' or 'repeaters', not {kind!r}")
    return AUTOADD_BEACONS if kind == "beacons" else AUTOADD_REPEATERS


def _hex(b: bytes) -> str:
    return b.hex()


def _ref_digits(ref: str, what: str) -> str:
    """Normalise a command-line key reference to lowercase hex digits: at least six, and at most the 16 of a prefix (a longer
    string, such as a full key, is cut to its first 16)."""
    digits = ref.strip().lower()
    if not digits or any(c not in "0123456789abcdef" for c in digits):
        raise StoreError(f"{ref!r} is not a hex key prefix; give the start of the {what}'s key prefix, as 'beaconctl status' shows")
    if len(digits) < names_mod.MIN_REF_DIGITS:
        raise StoreError(f"give at least {names_mod.MIN_REF_DIGITS} hex digits of the {what}'s key prefix, got {len(digits)}")
    return digits[: 2 * wire.ID_LEN]


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
    #
    # A beacon is its 8-byte key prefix. Its name is whatever repeaters last announced for that prefix (beacon_names), shown
    # for display only; names are not unique and never used to find a beacon.

    _BEACON_SELECT = "SELECT b.*, n.name AS name FROM beacons b LEFT JOIN beacon_names n ON n.prefix = b.prefix"

    def add_beacon(self, key_hex: str, notes: str = "", now: float | None = None) -> sqlite3.Row:
        """Allowlist a beacon by the 8-byte public key prefix its reports carry (16 hex characters). A full 64-character
        key is accepted for convenience, for example pasted from the beacon's serial 'pubkey' command; only its prefix is
        kept, since that is all the base ever sees."""
        raw = parse_hex_key(key_hex, "beacon key prefix", (wire.ID_LEN, PUBKEY_LEN))
        prefix = raw[: wire.ID_LEN]
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM beacons WHERE prefix = ?", (prefix,)).fetchone() is not None:
                raise StoreError(f"beacon {_hex(prefix)} is already on the allowlist")
            db.execute(
                "INSERT INTO beacons (prefix, notes, created_at) VALUES (?, ?, ?)",
                (prefix, notes, time.time() if now is None else now),
            )
        return self.beacon_by_prefix(prefix)

    def beacon_by_prefix(self, prefix: bytes) -> sqlite3.Row:
        row = self.conn.execute(self._BEACON_SELECT + " WHERE b.prefix = ?", (bytes(prefix),)).fetchone()
        if row is None:
            raise StoreError(f"no beacon with the prefix {_hex(bytes(prefix))}")
        return row

    def beacons(self) -> list[sqlite3.Row]:
        """The allowlist, by announced name (beacons without one last), then prefix."""
        return self.conn.execute(self._BEACON_SELECT + " ORDER BY n.name IS NULL, lower(n.name), b.prefix").fetchall()

    def beacon(self, ref: str) -> sqlite3.Row:
        """The allowlisted beacon a command-line reference names: its key prefix, or the start of it (at least six hex
        digits) when that is unambiguous. A longer string, such as a full key, is read as its first 16 digits."""
        digits = _ref_digits(ref, "beacon")
        found = [b for b in self.beacons() if bytes(b["prefix"]).hex().startswith(digits)]
        if not found:
            raise StoreError(f"no beacon on the allowlist matches {ref!r}")
        if len(found) > 1:
            options = ", ".join(names_mod.label(b["name"], b["prefix"]) for b in found)
            raise StoreError(f"{ref!r} matches {len(found)} beacons: {options}; give more of the prefix")
        return found[0]

    def remove_beacon(self, ref: str) -> sqlite3.Row:
        with self.transaction() as db:
            b = self.beacon(ref)
            db.execute("DELETE FROM beacons WHERE prefix = ?", (b["prefix"],))
        return b

    def remove_all_beacons(self) -> list[sqlite3.Row]:
        """Empty the allowlist (observation history is kept). Returns the beacons removed."""
        with self.transaction() as db:
            gone = self.beacons()
            db.execute("DELETE FROM beacons")
        return gone

    def set_beacon_enabled(self, ref: str, enabled: bool) -> sqlite3.Row:
        with self.transaction() as db:
            b = self.beacon(ref)
            db.execute("UPDATE beacons SET enabled = ? WHERE prefix = ?", (int(enabled), b["prefix"]))
        return b

    def set_all_beacons_enabled(self, enabled: bool) -> list[sqlite3.Row]:
        with self.transaction() as db:
            changed = self.beacons()
            db.execute("UPDATE beacons SET enabled = ?", (int(enabled),))
        return changed

    @staticmethod
    def _reset(db: sqlite3.Connection, b: sqlite3.Row) -> ResetInfo:
        db.execute(
            "UPDATE beacons SET hwm = NULL, hwm_at = NULL, epoch = epoch + 1, rejects_since_accept = 0 WHERE prefix = ?",
            (b["prefix"],),
        )
        return ResetInfo(bytes(b["prefix"]), b["name"], b["hwm"], b["last_reject_counter"], b["rejects_since_accept"])

    def reset_beacon(self, ref: str) -> ResetInfo:
        """Clear the high-water mark so the next report becomes the new baseline, and clear the rejected state."""
        with self.transaction() as db:
            return self._reset(db, self.beacon(ref))

    def reset_all_beacons(self) -> list[ResetInfo]:
        with self.transaction() as db:
            return [self._reset(db, b) for b in self.beacons()]

    def add_heard_beacons(self, since: float, now: float | None = None) -> list[sqlite3.Row]:
        """Add every beacon prefix that has been reported since `since` (by any repeater, trusted or not) but is not on the
        allowlist, in one transaction."""
        stamp = time.time() if now is None else now
        with self.transaction() as db:
            heard = db.execute(
                """SELECT DISTINCT beacon_prefix FROM observations
                   WHERE status IN (?, ?) AND rx_time >= ? AND beacon_prefix NOT IN (SELECT prefix FROM beacons)
                   ORDER BY beacon_prefix""",
                (UNKNOWN_BEACON, UNKNOWN_REPEATER, since),
            ).fetchall()
            for row in heard:
                db.execute("INSERT INTO beacons (prefix, created_at) VALUES (?, ?)", (bytes(row["beacon_prefix"]), stamp))
            return [self.beacon_by_prefix(bytes(r["beacon_prefix"])) for r in heard]

    # --- announced names ---------------------------------------------------------------------------------------------

    def record_name(self, prefix: bytes, name: str, repeater_prefix: bytes, now: float | None = None) -> str | None:
        """Store the name a repeater announced for a beacon prefix; the latest announcement wins. Returns the previous name
        (None if there was none). Call inside a transaction."""
        stamp = time.time() if now is None else now
        old = self.conn.execute("SELECT name FROM beacon_names WHERE prefix = ?", (bytes(prefix),)).fetchone()
        self.conn.execute(
            """INSERT INTO beacon_names (prefix, name, first_seen, updated_at, repeater_prefix) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (prefix) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at,
                                                  repeater_prefix = excluded.repeater_prefix""",
            (bytes(prefix), name, stamp, stamp, bytes(repeater_prefix)),
        )
        return old["name"] if old is not None else None

    def beacon_name(self, prefix: bytes) -> str | None:
        row = self.conn.execute("SELECT name FROM beacon_names WHERE prefix = ?", (bytes(prefix),)).fetchone()
        return row["name"] if row is not None else None

    # --- repeaters -------------------------------------------------------------------------------------------------

    def add_repeater(
        self,
        key_hex: str,
        lat: float | None = None,
        lon: float | None = None,
        name: str | None = None,
        window_s: float | None = None,
        now: float | None = None,
    ) -> sqlite3.Row:
        """Trust a repeater. Without a position it takes the one from the repeater's advert if one was heard, else it is
        added unlocated at 0, 0. Without a name it takes the advertised one."""
        raw = parse_hex_key(key_hex, "repeater public key or prefix", (wire.ID_LEN, PUBKEY_LEN))
        if (lat is None) != (lon is None):
            raise StoreError("give both latitude and longitude, or neither")
        if lat is not None and (not -90 <= lat <= 90 or not -180 <= lon <= 180):
            raise StoreError("latitude must be within +/-90 and longitude within +/-180")
        if window_s is not None and window_s <= 0:
            raise StoreError("window must be positive")
        prefix = raw[: wire.ID_LEN]
        with self.transaction() as db:
            self._insert_repeater(db, prefix, raw if len(raw) == PUBKEY_LEN else None, lat, lon, name, window_s, now)
        return self.repeater(prefix.hex())

    def _insert_repeater(self, db, prefix, pubkey, lat, lon, name, window_s, now) -> None:
        stamp = time.time() if now is None else now
        if db.execute("SELECT 1 FROM repeaters WHERE prefix = ?", (prefix,)).fetchone() is not None:
            existing = db.execute("SELECT name FROM repeaters WHERE prefix = ?", (prefix,)).fetchone()
            raise StoreError(f"repeater {names_mod.label(existing['name'], prefix)} is already in the repeater table")
        heard = db.execute("SELECT * FROM repeater_adverts WHERE prefix = ?", (prefix,)).fetchone()
        source = "none"
        if lat is not None:
            source = "manual" if (lat, lon) != (0.0, 0.0) else "none"
        elif heard is not None and self.is_located(heard["lat"], heard["lon"]):
            lat, lon, source = heard["lat"], heard["lon"], "advert"
        else:
            lat = lon = 0.0
        if name is not None:
            name = names_mod.clean_name(name, 64)
        elif heard is not None:
            name = names_mod.clean_name(heard["name"] or "", names_mod.MAX_NAME_BYTES)
        if pubkey is None and heard is not None:
            pubkey = bytes(heard["pubkey"])
        db.execute(
            """INSERT INTO repeaters (prefix, pubkey, name, lat, lon, location_source, location_updated_at, window_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (prefix, pubkey, name, lat, lon, source, stamp if source != "none" else None, window_s),
        )

    def add_heard_repeaters(self, since: float, now: float | None = None) -> list[sqlite3.Row]:
        """Trust every repeater that has sent reports or been heard advertising since `since` but is not in the table, each
        with the position and name from its advert when one was heard (else unlocated at 0, 0), in one transaction."""
        with self.transaction() as db:
            prefixes = sorted(bytes(r["prefix"]) for r in self._unknown_repeater_rows(db, since))
            for prefix in prefixes:
                self._insert_repeater(db, prefix, None, None, None, None, None, now)
            return [self.repeater(p.hex()) for p in prefixes]

    def locate_repeater(self, ref: str, lat: float, lon: float, now: float | None = None) -> sqlite3.Row:
        """Set a repeater's position by hand (testing, before it has advertised, or when it cannot be set on the repeater).
        The latest write wins, so the next advert with a position replaces it."""
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise StoreError("latitude must be within +/-90 and longitude within +/-180")
        with self.transaction() as db:
            r = self.repeater(ref)
            db.execute(
                "UPDATE repeaters SET lat = ?, lon = ?, location_source = ?, location_updated_at = ? WHERE prefix = ?",
                (lat, lon, "manual" if (lat, lon) != (0.0, 0.0) else "none", time.time() if now is None else now, r["prefix"]),
            )
        return self.repeater(bytes(r["prefix"]).hex())

    @staticmethod
    def is_located(lat: float | None, lon: float | None) -> bool:
        """A valid position that is not the 0, 0 an unset one advertises."""
        return (
            lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0)
        )

    def located_repeaters(self) -> list[sqlite3.Row]:
        """Enabled repeaters with a position: the ones location estimation may use. Unlocated ones (0, 0) are left out."""
        return [r for r in self.repeaters() if r["enabled"] and self.is_located(r["lat"], r["lon"])]

    def record_repeater_advert(
        self,
        pubkey: bytes,
        name: str | None,
        lat: float | None,
        lon: float | None,
        advert_timestamp: int | None,
        heard_at: float | None,
        now: float | None = None,
        auto_add: bool | None = None,
    ) -> AdvertEffect:
        """Take a repeater's advert. Every repeater advert is kept, trusted or not, so it can be listed and added; the
        position is only stored when valid (not 0, 0, which an unlocated repeater advertises). For a trusted repeater the
        position and name also replace what the table has, the latest write wins, and an advert without a position still
        updates the name but never erases a position. With auto-add for repeaters on (read from the settings unless given),
        an untrusted repeater is trusted by its advert."""
        if len(pubkey) != PUBKEY_LEN:
            raise StoreError("advert public key must be 32 bytes")
        prefix = pubkey[: wire.ID_LEN]
        stamp = time.time() if now is None else now
        located = self.is_located(lat, lon)
        clean = names_mod.clean_name(name, names_mod.MAX_NAME_BYTES) if name else None
        with self.transaction() as db:
            db.execute(
                """INSERT INTO repeater_adverts (prefix, pubkey, name, lat, lon, advert_timestamp, first_seen, last_heard)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (prefix) DO UPDATE SET pubkey = excluded.pubkey, name = COALESCE(excluded.name, name),
                     lat = CASE WHEN excluded.lat IS NOT NULL THEN excluded.lat ELSE lat END,
                     lon = CASE WHEN excluded.lat IS NOT NULL THEN excluded.lon ELSE lon END,
                     advert_timestamp = COALESCE(excluded.advert_timestamp, advert_timestamp),
                     last_heard = COALESCE(excluded.last_heard, last_heard)""",
                (prefix, pubkey, clean, lat if located else None, lon if located else None, advert_timestamp, stamp, heard_at),
            )
            row = db.execute("SELECT * FROM repeaters WHERE prefix = ?", (prefix,)).fetchone()
            effect = AdvertEffect(prefix, trusted=row is not None)
            if row is None:
                if auto_add if auto_add is not None else self._flag(db, AUTOADD_REPEATERS):
                    self._insert_repeater(db, prefix, pubkey, None, None, None, None, stamp)
                    effect.trusted = effect.added = True
                effect.position = (lat, lon) if located else None
                effect.name = clean
                return effect
            effect.old_name, effect.old_position = row["name"], (row["lat"], row["lon"])
            updates, args = ["pubkey = ?"], [pubkey]
            if clean is not None and clean != row["name"]:
                updates.append("name = ?")
                args.append(clean)
                effect.name = clean
            if located and (lat, lon) != (row["lat"], row["lon"]):
                updates += ["lat = ?", "lon = ?", "location_source = 'advert'", "location_updated_at = ?"]
                args += [lat, lon, stamp]
                effect.position = (lat, lon)
            elif located and row["location_source"] != "advert":
                updates += ["location_source = 'advert'", "location_updated_at = ?"]  # same place, now owned by the advert
                args.append(stamp)
            db.execute(f"UPDATE repeaters SET {', '.join(updates)} WHERE prefix = ?", (*args, prefix))
        return effect

    def repeater_advert(self, prefix: bytes) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM repeater_adverts WHERE prefix = ?", (bytes(prefix),)).fetchone()

    def repeater(self, ref: str) -> sqlite3.Row:
        """The repeater a command-line reference names: the start of its key prefix (at least six hex digits) or its name,
        whichever matches exactly one repeater."""
        ref = ref.strip()
        digits = ref.lower()
        by_prefix = []
        if len(digits) >= names_mod.MIN_REF_DIGITS and all(c in "0123456789abcdef" for c in digits):
            digits = digits[: 2 * wire.ID_LEN]
            by_prefix = [r for r in self.repeaters() if bytes(r["prefix"]).hex().startswith(digits)]
        by_name = [r for r in self.repeaters() if r["name"] and r["name"].lower() == ref.lower()]
        found = {bytes(r["prefix"]): r for r in by_prefix + by_name}
        if not found:
            raise StoreError(f"no repeater matches {ref!r}")
        if len(found) > 1:
            options = ", ".join(names_mod.label(r["name"], r["prefix"]) for r in found.values())
            raise StoreError(f"{ref!r} matches {len(found)} repeaters: {options}; use the key prefix")
        return next(iter(found.values()))

    def repeaters(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM repeaters ORDER BY name IS NULL, lower(name), prefix").fetchall()

    def remove_repeater(self, ref: str) -> sqlite3.Row:
        with self.transaction() as db:
            r = self.repeater(ref)
            db.execute("DELETE FROM repeaters WHERE prefix = ?", (r["prefix"],))
        return r

    def set_repeater_enabled(self, ref: str, enabled: bool) -> sqlite3.Row:
        with self.transaction() as db:
            r = self.repeater(ref)
            db.execute("UPDATE repeaters SET enabled = ? WHERE prefix = ?", (int(enabled), r["prefix"]))
        return r

    def set_repeater_window(self, ref: str, window_s: float | None) -> sqlite3.Row:
        if window_s is not None and window_s <= 0:
            raise StoreError("window must be positive")
        with self.transaction() as db:
            r = self.repeater(ref)
            db.execute("UPDATE repeaters SET window_s = ? WHERE prefix = ?", (window_s, r["prefix"]))
        return r

    def names(self) -> tuple[dict[bytes, str], dict[bytes, str]]:
        """(beacon prefix -> announced name, repeater prefix -> operator name), for display. Entries without a name are
        left out."""
        beacons = {bytes(r["prefix"]): r["name"] for r in self.conn.execute("SELECT prefix, name FROM beacon_names")}
        repeaters = {
            bytes(r["prefix"]): r["name"] for r in self.conn.execute("SELECT prefix, name FROM repeaters WHERE name IS NOT NULL")
        }
        return beacons, repeaters

    # --- settings ---------------------------------------------------------------------------------------------------

    def _flag(self, db: sqlite3.Connection, key: str) -> bool:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row is not None and row["value"] == "1"

    def autoadd(self, kind: str) -> bool:
        """Whether unknown `beacons` or `repeaters` are trusted automatically as they are seen. Off by default."""
        return self._flag(self.conn, _autoadd_key(kind))

    def set_autoadd(self, kind: str, enabled: bool) -> None:
        key = _autoadd_key(kind)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, "1" if enabled else "0"),
            )

    # --- observations --------------------------------------------------------------------------------------------------

    def rejects(self, beacon: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        """Recent observations that were not accepted (newest first), with the beacon's current high-water mark."""
        sql = """SELECT o.*, n.name AS beacon_name, b.hwm AS beacon_hwm, r.name AS repeater_name
                 FROM observations o
                 LEFT JOIN beacons b ON b.prefix = o.beacon_prefix
                 LEFT JOIN beacon_names n ON n.prefix = o.beacon_prefix
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
        """Beacon prefixes that were reported but are not on the allowlist, one row per prefix. Reports from repeaters that
        are not trusted yet count too, so a beacon can be added as soon as it is seen; `n_trusted` is how many of the
        reports came from trusted repeaters."""
        return self.conn.execute(
            """SELECT o.beacon_prefix, count(*) AS n, max(o.rx_time) AS last_seen, max(o.counter) AS last_counter,
                      count(DISTINCT o.repeater_prefix) AS n_repeaters, sum(o.status = ?) AS n_trusted, bn.name AS name
               FROM observations o LEFT JOIN beacon_names bn ON bn.prefix = o.beacon_prefix
               WHERE o.status IN (?, ?) AND o.rx_time >= ? AND o.beacon_prefix NOT IN (SELECT prefix FROM beacons)
               GROUP BY o.beacon_prefix ORDER BY last_seen DESC""",
            (UNKNOWN_BEACON, UNKNOWN_BEACON, UNKNOWN_REPEATER, since),
        ).fetchall()

    def _unknown_repeater_rows(self, db: sqlite3.Connection, since: float) -> list[dict]:
        reports = {
            bytes(r["repeater_prefix"]): r
            for r in db.execute(
                """SELECT repeater_prefix, count(*) AS n, max(rx_time) AS last_seen FROM observations
                   WHERE status = ? AND rx_time >= ? AND repeater_prefix NOT IN (SELECT prefix FROM repeaters)
                   GROUP BY repeater_prefix""",
                (UNKNOWN_REPEATER, since),
            )
        }
        adverts = {
            bytes(r["prefix"]): r
            for r in db.execute(
                """SELECT * FROM repeater_adverts WHERE COALESCE(last_heard, first_seen) >= ?
                   AND prefix NOT IN (SELECT prefix FROM repeaters)""",
                (since,),
            )
        }
        rows = []
        for prefix in reports.keys() | adverts.keys():
            r, a = reports.get(prefix), adverts.get(prefix)
            seen = [t for t in (r["last_seen"] if r else None, (a["last_heard"] or a["first_seen"]) if a else None) if t]
            rows.append(
                {
                    "prefix": prefix,
                    "n": r["n"] if r else 0,  # reports sent on our channel
                    "last_seen": max(seen),
                    "name": a["name"] if a else None,
                    "lat": a["lat"] if a else None,
                    "lon": a["lon"] if a else None,
                    "advertised": a is not None,
                }
            )
        rows.sort(key=lambda x: -x["last_seen"])
        return rows

    def unknown_repeaters(self, since: float) -> list[dict]:
        """Repeaters that have sent reports on the channel or been heard advertising but are not in the repeater table, newest
        first. `n` counts their reports (0 for one only heard advertising); name and position are from its advert."""
        return self._unknown_repeater_rows(self.conn, since)

    def latest_transmission(self, prefix: bytes) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM transmissions WHERE beacon_prefix = ? ORDER BY last_seen DESC, id DESC LIMIT 1", (prefix,)
        ).fetchone()
