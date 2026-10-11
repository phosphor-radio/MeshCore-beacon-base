"""What beacon-web serves: the store turned into JSON-able dictionaries. No HTTP in here, so it is testable on its own.

Every view carries ``now``, the base's clock: the page computes ages from it, not from the browser's clock (a phone's), and
``clock.trusted`` says whether the base's own clock has been set. Prefixes are lowercase hex strings. Names are the untrusted display
text the repeaters announced (already cleaned by ``names``); ``label`` is the one-line form used in messages. Positions are
estimates (see ``estimate.py``) and carry the method that produced them.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import admin, health, heartbeat, names
from .config import Config
from .store import Store, StoreError

PREFIX_RE = re.compile(r"^[0-9a-f]{16}$")
UNKNOWN_HOURS = 24.0  # how far back the map looks for beacons and repeaters that are not trusted yet


class NotFound(StoreError):
    """No such beacon or transmission."""


def _hex(b: bytes | None) -> str | None:
    return None if b is None else bytes(b).hex()


def parse_prefix(text: str) -> bytes:
    """A full 16-digit beacon prefix, as the API gives it out. Short references are for people at a command line, not for a URL."""
    text = text.strip().lower()
    if not PREFIX_RE.match(text):
        raise NotFound(f"{text[:20]!r} is not a beacon prefix (16 hex digits)")
    return bytes.fromhex(text)


def _position(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "lat": row["lat"], "lon": row["lon"], "radius_m": row["radius_m"], "method": row["method"], "n_repeaters": row["n_repeaters"],
        "counter": row["counter"], "at": row["last_seen"], "transmission": row["transmission_id"],
    }


# --- health --------------------------------------------------------------------------------------------------------------


def health_view(store: Store, cfg: Config, now: float) -> dict[str, Any]:
    """Is ingest running, is the companion connected, is the base's clock set: everything a banner is made from."""
    row = store.status_row()
    state = heartbeat.state(row)
    ingest: dict[str, Any] = {"state": state, "updated_at": None, "connected": False}
    problems: list[dict[str, str]] = []
    if row is not None:
        ingest.update(
            updated_at=row["updated_at"], started_at=row["started_at"], pid=row["pid"], connected=bool(row["connected"]) and state == heartbeat.UP,
            port=row["port"], connected_since=row["connected_since"], last_frame_at=row["last_frame_at"], last_report_at=row["last_report_at"],
            remote_state=row["remote_state"], companion_clock_offset_s=row["companion_clock_offset_s"],
            companion={
                "name": row["companion_name"], "model": row["companion_model"], "firmware": row["companion_firmware"],
                "key_prefix": _hex(row["companion_key_prefix"]),
            },
        )
    if state == heartbeat.NEVER:
        problems.append({"code": "ingest_never", "level": "error", "text": "beacon-ingest has never run against this database; start it"})
    elif state == heartbeat.STOPPED:
        problems.append({"code": "ingest_stopped", "level": "error", "text": "beacon-ingest was stopped; reports are not being received"})
    elif state == heartbeat.DOWN:
        problems.append({"code": "ingest_down", "level": "error", "text": "beacon-ingest is not running; reports are not being received"})
    elif not row["connected"]:
        problems.append({"code": "companion_disconnected", "level": "error", "text": "the companion is not connected; reports are not being received"})
    trusted, clock_text = admin.clock_state(store, cfg)
    if not trusted:
        problems.append({"code": "clock_not_set", "level": "warning", "text": "the base's clock is not set; the times shown are provisional until it is (beaconctl time set)"})
    offset = ingest.get("companion_clock_offset_s")
    if state == heartbeat.UP and offset is not None and abs(offset) > 60:
        problems.append({"code": "companion_clock", "level": "warning", "text": f"the companion's clock is {abs(offset)} s {'ahead of' if offset > 0 else 'behind'} the base's"})
    auto = {k: store.autoadd(k) for k in ("beacons", "repeaters")}
    if any(auto.values()):
        on = " and ".join(k for k, v in auto.items() if v)
        problems.append({"code": "autoadd", "level": "warning", "text": f"auto-add is on for {on}: anything new on the channel is trusted as it is seen"})
    return {
        "now": now,
        "ok": not any(p["level"] == "error" for p in problems),
        "ingest": ingest,
        "clock": {"trusted": trusted, "text": clock_text},
        "autoadd": auto,
        "problems": problems,
    }


# --- beacons and repeaters ------------------------------------------------------------------------------------------------


def _latest_positions(store: Store) -> dict[bytes, sqlite3.Row]:
    """Each beacon's most recent position, with the counter and time of its transmission (one indexed lookup per beacon)."""
    out = {}
    for b in store.conn.execute("SELECT prefix FROM beacons"):
        row = store.conn.execute(
            """SELECT p.*, t.counter, t.last_seen FROM positions p JOIN transmissions t ON t.id = p.transmission_id
               WHERE p.beacon_prefix = ? ORDER BY p.transmission_id DESC LIMIT 1""",
            (b["prefix"],),
        ).fetchone()
        if row is not None:
            out[bytes(b["prefix"])] = row
    return out


def _beacon_dict(h: health.BeaconHealth, position: sqlite3.Row | None) -> dict[str, Any]:
    b = h.beacon
    rejects = None
    if h.rejects is not None:
        r = h.rejects
        rejects = {"count": r.count, "since": r.first_at, "min_counter": r.min_counter, "max_counter": r.max_counter, "via": r.repeater_names}
    return {
        "prefix": _hex(b["prefix"]), "name": b["name"], "label": names.label(b["name"], b["prefix"]), "notes": b["notes"],
        "enabled": bool(b["enabled"]), "state": h.state, "hwm": b["hwm"], "hwm_at": b["hwm_at"], "baseline_pending": h.baseline_pending,
        "last_heard_at": h.last_heard_at, "last_accept_at": b["last_accept_at"], "batt_mv": h.batt_mv, "rejects": rejects,
        "last_reject_counter": b["last_reject_counter"], "position": _position(position),
    }


def beacons_view(store: Store, cfg: Config, now: float) -> list[dict[str, Any]]:
    """Every allowlisted beacon, rejected and silent first (the order 'beaconctl status' uses)."""
    positions = _latest_positions(store)
    return [_beacon_dict(h, positions.get(bytes(h.beacon["prefix"]))) for h in health.assess(store, cfg.beacon, now)]


def repeaters_view(store: Store, now: float) -> list[dict[str, Any]]:
    out = []
    for r in store.repeaters():
        located = Store.is_located(r["lat"], r["lon"])
        last = store.conn.execute(
            "SELECT rx_time FROM observations WHERE id = (SELECT max(id) FROM observations WHERE repeater_prefix = ?)", (r["prefix"],)
        ).fetchone()
        advert = store.repeater_advert(bytes(r["prefix"]))
        out.append(
            {
                "prefix": _hex(r["prefix"]), "name": r["name"], "label": names.label(r["name"], r["prefix"]), "enabled": bool(r["enabled"]),
                "located": located, "lat": r["lat"] if located else None, "lon": r["lon"] if located else None,
                "location_source": r["location_source"] if located else None, "window_s": r["window_s"],
                "last_report_at": last["rx_time"] if last else None, "last_advert_at": advert["last_heard"] if advert else None,
            }
        )
    return out


def state_view(store: Store, cfg: Config, now: float, operator: bool) -> dict[str, Any]:
    """The whole map in one response: what the page polls."""
    since = now - UNKNOWN_HOURS * 3600
    return {
        "now": now,
        "health": health_view(store, cfg, now),
        "repeaters": repeaters_view(store, now),
        "beacons": beacons_view(store, cfg, now),
        "unknown": {"beacons": len(store.unknown_beacons(since)), "repeaters": len(store.unknown_repeaters(since))},
        "expected_interval_s": cfg.beacon.interval_s,
        "operator": operator,
    }


# --- history -------------------------------------------------------------------------------------------------------------


def _transmission_dict(row: sqlite3.Row) -> dict[str, Any]:
    pos = None
    if row["lat"] is not None:
        pos = {"lat": row["lat"], "lon": row["lon"], "radius_m": row["radius_m"], "method": row["method"], "n_repeaters": row["pos_n"]}
    return {
        "id": row["id"], "counter": row["counter"], "epoch": row["epoch"], "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        "n_repeaters": row["n_repeaters"], "batt_mv": row["batt_mv"], "final": bool(row["final"]), "position": pos,
    }


_TX_SELECT = """SELECT t.*, p.lat, p.lon, p.radius_m, p.method, p.n_repeaters AS pos_n
                FROM transmissions t LEFT JOIN positions p ON p.transmission_id = t.id"""


def beacon_view(store: Store, cfg: Config, prefix: str, now: float, limit: int = 50) -> dict[str, Any]:
    """One beacon: its state and its latest transmissions, newest first, each with the position that was estimated."""
    raw = parse_prefix(prefix)
    try:
        store.beacon_by_prefix(raw)
    except StoreError:
        raise NotFound(f"no beacon with the prefix {raw.hex()}") from None
    positions = _latest_positions(store)
    h = next(x for x in health.assess(store, cfg.beacon, now) if bytes(x.beacon["prefix"]) == raw)
    rows = store.conn.execute(_TX_SELECT + " WHERE t.beacon_prefix = ? ORDER BY t.id DESC LIMIT ?", (raw, limit)).fetchall()
    return {"now": now, "beacon": _beacon_dict(h, positions.get(raw)), "transmissions": [_transmission_dict(r) for r in rows]}


def track_view(store: Store, prefix: str, now: float, hours: float = 6.0, limit: int = 200) -> dict[str, Any]:
    """The estimated positions of a beacon, oldest first, for drawing the path it walked."""
    raw = parse_prefix(prefix)
    rows = store.conn.execute(
        """SELECT p.*, t.counter, t.first_seen, t.last_seen FROM positions p JOIN transmissions t ON t.id = p.transmission_id
           WHERE p.beacon_prefix = ? AND t.last_seen >= ? ORDER BY p.transmission_id DESC LIMIT ?""",
        (raw, now - hours * 3600, limit),
    ).fetchall()
    points = [
        {
            "transmission": r["transmission_id"], "counter": r["counter"], "at": r["last_seen"], "lat": r["lat"], "lon": r["lon"],
            "radius_m": r["radius_m"], "method": r["method"], "n_repeaters": r["n_repeaters"],
        }
        for r in reversed(rows)
    ]
    return {"now": now, "beacon": raw.hex(), "points": points}


def transmission_view(store: Store, transmission_id: int, now: float) -> dict[str, Any]:
    """One transmission: which repeaters heard it and how well, and where it was put."""
    t = store.conn.execute(_TX_SELECT + " WHERE t.id = ?", (transmission_id,)).fetchone()
    if t is None:
        raise NotFound(f"no transmission {transmission_id}")
    beacon = store.conn.execute(Store._BEACON_SELECT + " WHERE b.prefix = ?", (t["beacon_prefix"],)).fetchone()
    rows = store.conn.execute(
        """SELECT o.*, r.name AS repeater_name, r.lat AS rlat, r.lon AS rlon FROM observations o
           LEFT JOIN repeaters r ON r.prefix = o.repeater_prefix WHERE o.transmission_id = ? ORDER BY o.id""",
        (transmission_id,),
    ).fetchall()
    observations = []
    for o in rows:
        located = Store.is_located(o["rlat"], o["rlon"])
        observations.append(
            {
                "repeater": _hex(o["repeater_prefix"]), "repeater_label": names.label(o["repeater_name"], o["repeater_prefix"]),
                "lat": o["rlat"] if located else None, "lon": o["rlon"] if located else None, "rssi": o["rssi"], "snr": o["snr_x4"] / 4,
                "batt_mv": o["batt_mv"], "status": o["status"], "reason": o["reason"], "at": o["rx_time"], "time_trusted": bool(o["time_trusted"]),
            }
        )
    return {
        "now": now,
        "transmission": _transmission_dict(t),
        "beacon": {"prefix": _hex(t["beacon_prefix"]), "label": names.label(beacon["name"] if beacon else None, t["beacon_prefix"])},
        "observations": observations,
    }


def rejects_view(store: Store, now: float, beacon: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Recent observations that were not accepted, newest first, as 'beaconctl rejects' lists them."""
    if beacon is not None:
        beacon = parse_prefix(beacon).hex()
    rows = store.rejects(beacon, limit)
    return {
        "now": now,
        "rejects": [
            {
                "at": r["rx_time"], "time_trusted": bool(r["time_trusted"]), "status": r["status"], "reason": r["reason"], "counter": r["counter"],
                "hwm": r["beacon_hwm"], "beacon": _hex(r["beacon_prefix"]), "beacon_label": names.label(r["beacon_name"], r["beacon_prefix"]),
                "repeater": _hex(r["repeater_prefix"]), "repeater_label": names.label(r["repeater_name"], r["repeater_prefix"]),
            }
            for r in rows
        ],
    }


# --- operator action ----------------------------------------------------------------------------------------------------


def reset_beacon(store: Store, prefix: str) -> dict[str, Any]:
    """The Reset button: clear the high-water mark so the next report becomes the baseline. What it cleared is returned."""
    raw = parse_prefix(prefix)
    try:
        info = store.reset_beacon(raw.hex())
    except StoreError:
        raise NotFound(f"no beacon with the prefix {raw.hex()}") from None
    return {
        "beacon": raw.hex(), "label": names.label(info.name, info.prefix), "old_hwm": info.old_hwm,
        "last_reject_counter": info.last_reject_counter, "rejects_cleared": info.rejects_since_accept,
    }
