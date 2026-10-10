"""Per-beacon health, derived from the columns the pipeline keeps on ``beacons``.

States, most urgent first:

- ``rejected``: replays were rejected since the last accepted report. This is the lockout case: the beacon's counter is at
  or below the high-water mark. Fix with ``beaconctl beacon reset``.
- ``silent``: nothing heard from the beacon (accepted or rejected) for more than ``silent_intervals`` expected intervals,
  or never heard.
- ``ok``: heard recently and accepted.
- ``disabled``: switched off by the operator.

Beacons reported by repeaters but missing from the allowlist are not beacons yet, so they are listed separately by
``Store.unknown_beacons``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import BeaconConfig
from .store import Store

REJECTED = "rejected"
SILENT = "silent"
OK = "ok"
DISABLED = "disabled"

_ORDER = {REJECTED: 0, SILENT: 1, OK: 2, DISABLED: 3}


@dataclass(frozen=True)
class RejectSummary:
    """The observations behind a ``rejected`` state."""

    count: int
    first_at: float
    min_counter: int
    max_counter: int
    repeater_names: list[str]


@dataclass(frozen=True)
class BeaconHealth:
    beacon: sqlite3.Row
    state: str
    last_heard_at: float | None
    batt_mv: int | None
    rejects: RejectSummary | None
    baseline_pending: bool  # reset, waiting for the next report to set the high-water mark


def reject_summary(store: Store, beacon: sqlite3.Row) -> RejectSummary | None:
    """Rejects since the last accepted report of this beacon (within the current epoch)."""
    last_accept = store.conn.execute(
        "SELECT max(id) FROM observations WHERE beacon_prefix = ? AND epoch = ? AND status = 'accepted'",
        (beacon["prefix"], beacon["epoch"]),
    ).fetchone()[0]
    rows = store.conn.execute(
        """SELECT o.counter, o.rx_time, COALESCE(r.name, lower(hex(o.repeater_prefix))) AS repeater
           FROM observations o LEFT JOIN repeaters r ON r.prefix = o.repeater_prefix
           WHERE o.beacon_prefix = ? AND o.epoch = ? AND o.status = 'replay' AND o.reason = 'below_hwm' AND o.id > ?
           ORDER BY o.id""",
        (beacon["prefix"], beacon["epoch"], last_accept or 0),
    ).fetchall()
    if not rows:
        return None
    return RejectSummary(
        count=len(rows),
        first_at=rows[0]["rx_time"],
        min_counter=min(r["counter"] for r in rows),
        max_counter=max(r["counter"] for r in rows),
        repeater_names=sorted({r["repeater"] for r in rows}),
    )


def assess(store: Store, cfg: BeaconConfig, now: float) -> list[BeaconHealth]:
    """Health of every beacon, most urgent first, then by name."""
    out = []
    for b in store.beacons():
        tx = store.latest_transmission(bytes(b["prefix"]))
        silent_after = cfg.interval_s * cfg.silent_intervals
        summary = None
        if not b["enabled"]:
            state = DISABLED
        elif b["rejects_since_accept"] > 0:
            state = REJECTED
            summary = reject_summary(store, b)
        elif b["last_heard_at"] is None or now - b["last_heard_at"] > silent_after:
            state = SILENT
        else:
            state = OK
        out.append(
            BeaconHealth(
                beacon=b,
                state=state,
                last_heard_at=b["last_heard_at"],
                batt_mv=tx["batt_mv"] if tx is not None else None,
                rejects=summary,
                baseline_pending=b["hwm"] is None,
            )
        )
    out.sort(key=lambda h: (_ORDER[h.state], h.beacon["name"] is None, (h.beacon["name"] or "").lower(), bytes(h.beacon["prefix"])))
    return out
