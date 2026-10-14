"""beaconctl commands that work on the database: allowlist, repeaters, status, rejects, time, check."""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import time

from . import clock, health, heartbeat, names, remote
from .config import Config
from .store import Store, StoreError


# --- formatting --------------------------------------------------------------------------------------------------------


def fmt_time(ts: float | None) -> str:
    if ts is None:
        return "never"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_clock(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_advert_clock(ts: int | None) -> str:
    """The time in a repeater's advert (its own clock, UTC), or '-' when it has none."""
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def fmt_age(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    secs = max(now - ts, 0)
    if secs < 90:
        return f"{secs:.0f}s ago"
    if secs < 90 * 60:
        return f"{secs / 60:.0f}m ago"
    if secs < 36 * 3600:
        return f"{secs / 3600:.0f}h ago"
    return f"{secs / 86400:.0f}d ago"


def fmt_duration(secs: float) -> str:
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if d or h else "") + f"{m}m"


def fmt_batt(mv: int | None) -> str:
    return "-" if not mv else f"{mv / 1000:.2f}V"


def table(rows: list[list[str]], header: list[str]) -> str:
    """Left-aligned columns; the last column is not padded."""
    rows = [header] + rows
    widths = [max(len(r[i]) for r in rows) for i in range(len(header) - 1)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r[:-1], widths)) + "  " + r[-1] for r in rows]
    return "\n".join(line.rstrip() for line in lines)


def _open(cfg: Config) -> Store:
    return Store.open(cfg.db_path)


def clock_state(store: Store, cfg: Config) -> tuple[bool, str]:
    """(trusted, one-line description) of the clock for the current boot."""
    boot = clock.boot_id()
    if cfg.clock.assume_synced:
        return True, "assumed correct (clock.assume_synced)"
    event = clock.last_event(store.conn, boot)
    if event is None:
        return False, "NOT SET: observation times are provisional until 'beaconctl time set'"
    how = {"set": "set with beaconctl", "step": "stepped, seen by ingest"}.get(event["kind"], event["kind"])
    return True, f"{how} at {fmt_time(event['at_wall'])}"


def ingest_lines(store: Store, now: float) -> list[str]:
    """The first lines of 'status': is ingest running, is the companion connected. Problems start with '!'."""
    row = store.status_row()
    state = heartbeat.state(row)
    if state == heartbeat.NEVER:
        return ["! ingest: has never run against this database; start beacon-ingest"]
    since = fmt_age(row["updated_at"], now)
    if state == heartbeat.STOPPED:
        return [f"! ingest: stopped cleanly {since}; start beacon-ingest"]
    if state == heartbeat.DOWN:
        return [f"! ingest: NOT RUNNING, last heard from {since} (crashed, killed, or the machine rebooted)"]
    lines = [f"ingest: running (pid {row['pid']}, started {fmt_age(row['started_at'], now)})"]
    if not row["connected"]:
        lines.append("! companion: not connected (reports are not being received)")
        return lines
    model = " ".join(p for p in (row["companion_model"], row["companion_firmware"]) if p)
    key = bytes(row["companion_key_prefix"]).hex() if row["companion_key_prefix"] else "?"
    lines.append(
        f"companion: {row['companion_name'] or '?'} ({model or '?'}) on {row['port'] or '?'}, key {key}, connected {fmt_age(row['connected_since'], now)};"
        f" last frame {fmt_age(row['last_frame_at'], now)}, last report {fmt_age(row['last_report_at'], now)}"
    )
    offset = row["companion_clock_offset_s"]
    if offset is not None and abs(offset) > 60:
        lines.append(
            f"! companion clock: {abs(offset)} s {'ahead of' if offset > 0 else 'behind'} the base's"
            + ("; it cannot be set back, reboot the companion" if offset > 0 else "")
        )
    return lines


# --- beacons -------------------------------------------------------------------------------------------------------------
#
# Beacons are addressed by their key prefix (as 'beaconctl status' shows it), or by the start of it, at least six digits.
# Names are what repeaters announced, for display; they are never used to find a beacon.


def _one_or_all(args: argparse.Namespace) -> bool:
    """True for --all. Exactly one of a beacon reference and --all must be given."""
    if args.all and args.ref:
        raise StoreError("give a beacon prefix or --all, not both")
    if not args.all and not args.ref:
        raise StoreError("give a beacon key prefix, or --all for every beacon on the allowlist")
    return args.all


def _beacon_label(b) -> str:
    return names.label(b["name"], b["prefix"])


def cmd_beacon_add(args: argparse.Namespace, cfg: Config) -> int:
    if args.all:
        if args.prefix or args.notes:
            raise StoreError("--all adds every beacon that was reported but is not on the allowlist; do not give a prefix")
        with _open(cfg) as store:
            added = store.add_heard_beacons(time.time() - args.hours * 3600)
        if not added:
            print(f"nothing to add: no beacons reported in the last {args.hours:g}h are missing from the allowlist")
            return 0
        for b in added:
            print(f"added beacon {bytes(b['prefix']).hex()} ({b['name'] or 'no name announced yet'})")
        print(f"added {len(added)} beacon(s); the next report from each becomes its baseline")
        return 0
    if not args.prefix:
        raise StoreError("usage: beacon add <prefix>   or   beacon add --all")
    with _open(cfg) as store:
        b = store.add_beacon(args.prefix, args.notes or "")
    print(f"added beacon {bytes(b['prefix']).hex()} ({b['name'] or 'no name announced yet'}); the next report becomes its baseline")
    return 0


def cmd_beacon_list(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as store:
        rows = [[b["name"] or "-", bytes(b["prefix"]).hex(), "yes" if b["enabled"] else "no", b["notes"]] for b in store.beacons()]
    print(table(rows, ["NAME", "PREFIX", "ENABLED", "NOTES"]) if rows else "no beacons")
    return 0


def cmd_beacon_remove(args: argparse.Namespace, cfg: Config) -> int:
    everything = _one_or_all(args)
    with _open(cfg) as store:
        gone = store.remove_all_beacons() if everything else [store.remove_beacon(args.ref)]
    if everything:
        print(f"removed {len(gone)} beacon(s) from the allowlist; their history is kept")
    else:
        print(f"removed beacon {_beacon_label(gone[0])}; its history is kept")
    return 0


def cmd_beacon_enable(args: argparse.Namespace, cfg: Config) -> int:
    everything = _one_or_all(args)
    word = "enabled" if args.enable else "disabled"
    with _open(cfg) as store:
        changed = store.set_all_beacons_enabled(args.enable) if everything else [store.set_beacon_enabled(args.ref, args.enable)]
    print(f"{len(changed)} beacon(s) {word}" if everything else f"beacon {_beacon_label(changed[0])} {word}")
    return 0


def _describe_reset(info) -> str:
    was = "no high-water mark" if info.old_hwm is None else f"high-water mark {info.old_hwm}"
    text = f"reset {names.label(info.name, info.prefix)}: was {was}"
    if info.rejects_since_accept:
        text += f", {info.rejects_since_accept} rejects since the last accepted report (last rejected counter {info.last_reject_counter})"
    return text


def cmd_beacon_reset(args: argparse.Namespace, cfg: Config) -> int:
    everything = _one_or_all(args)
    with _open(cfg) as store:
        infos = store.reset_all_beacons() if everything else [store.reset_beacon(args.ref)]
    for info in infos:
        print(_describe_reset(info))
    if everything and not infos:
        print("no beacons")
        return 0
    which = "each of these beacons" if everything else "this beacon"
    print(f"the next report from {which} becomes its new baseline; do this while they are transmitting")
    return 0


def cmd_beacon_status(args: argparse.Namespace, cfg: Config) -> int:
    now = time.time()
    with _open(cfg) as store:
        b = store.beacon(args.ref)
        h = next(x for x in health.assess(store, cfg.beacon, now) if bytes(x.beacon["prefix"]) == bytes(b["prefix"]))
        _, repeaters = store.names()
        heard = store.conn.execute(
            """SELECT repeater_prefix, count(*) AS n, max(rx_time) AS last, max(id) AS last_id
               FROM observations WHERE beacon_prefix = ? AND status = 'accepted' AND rx_time >= ?
               GROUP BY repeater_prefix ORDER BY last DESC""",
            (b["prefix"], now - 24 * 3600),
        ).fetchall()
        last = {
            bytes(r["repeater_prefix"]): store.conn.execute("SELECT rssi, snr_x4 FROM observations WHERE id = ?", (r["last_id"],)).fetchone()
            for r in heard
        }
    print(f"{b['name'] or bytes(b['prefix']).hex()}  [{h.state}]")
    print(f"  key prefix         {bytes(b['prefix']).hex()}")
    if b["notes"]:
        print(f"  notes              {b['notes']}")
    print(f"  enabled            {'yes' if b['enabled'] else 'no'}")
    hwm = "none (next report becomes the baseline)" if b["hwm"] is None else f"{b['hwm']} (since {fmt_time(b['hwm_at'])})"
    print(f"  high-water mark    {hwm}")
    print(f"  last heard         {fmt_time(b['last_heard_at'])} ({fmt_age(b['last_heard_at'], now)})")
    print(f"  last accepted      {fmt_time(b['last_accept_at'])}")
    print(f"  battery            {fmt_batt(h.batt_mv)}")
    if h.rejects:
        r = h.rejects
        print(f"  rejects            {r.count} since {fmt_time(r.first_at)}, counters {r.min_counter}-{r.max_counter}, via {', '.join(r.repeater_names)}")
    print("  heard by (24h)" + ("" if heard else "     -"))
    for r in heard:
        v = last[bytes(r["repeater_prefix"])]
        who = names.label(repeaters.get(bytes(r["repeater_prefix"])), r["repeater_prefix"])
        print(f"    {who:<24} {r['n']:>4} reports, last {fmt_age(r['last'], now)}, rssi {v['rssi']} dBm, snr {v['snr_x4'] / 4:+.2f} dB")
    return 0


# --- repeaters -----------------------------------------------------------------------------------------------------------


def _repeater_label(r) -> str:
    return names.label(r["name"], r["prefix"])


def _position_text(r) -> str:
    return f"{r['lat']:.6f}, {r['lon']:.6f}" if Store.is_located(r["lat"], r["lon"]) else "no location"


def cmd_repeater_add(args: argparse.Namespace, cfg: Config) -> int:
    if args.all:
        if args.key or args.lat is not None or args.lon is not None or args.name:
            raise StoreError("--all adds every repeater that reported but is not in the table; do not give a key, position or name")
        with _open(cfg) as store:
            added = store.add_heard_repeaters(time.time() - args.hours * 3600)
        if not added:
            print(f"nothing to add: no repeaters that reported in the last {args.hours:g}h are missing from the table")
            return 0
        for r in added:
            print(f"added repeater {_repeater_label(r)} ({_position_text(r)})")
        print(f"added {len(added)} repeater(s)")
        _warn_unlocated(added)
        return 0
    if not args.key:
        raise StoreError("usage: repeater add <key-or-prefix> [<lat> <lon>]   or   repeater add --all")
    with _open(cfg) as store:
        r = store.add_repeater(args.key, args.lat, args.lon, name=args.name, window_s=args.window)
    print(f"added repeater {_repeater_label(r)} (prefix {bytes(r['prefix']).hex()}), {_position_text(r)}")
    _warn_unlocated([r])
    return 0


def _warn_unlocated(rows) -> None:
    unlocated = [r for r in rows if not Store.is_located(r["lat"], r["lon"])]
    if unlocated:
        print(
            f"{len(unlocated)} of them have no location yet and are left out of positioning until one is advertised "
            "(set it on the repeater and send an advert) or given with 'beaconctl repeater locate'"
        )


def cmd_repeater_locate(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as store:
        r = store.locate_repeater(args.ref, args.lat, args.lon)
    print(f"repeater {_repeater_label(r)} is at {_position_text(r)}; the next advert with a position replaces it")
    return 0


def cmd_repeater_list(args: argparse.Namespace, cfg: Config) -> int:
    now = time.time()
    with _open(cfg) as store:
        rows = []
        for r in store.repeaters():
            located = Store.is_located(r["lat"], r["lon"])
            advert = store.repeater_advert(bytes(r["prefix"]))
            rows.append(
                [
                    r["name"] or "-",
                    bytes(r["prefix"]).hex(),
                    f"{r['lat']:.6f}" if located else "-",
                    f"{r['lon']:.6f}" if located else "-",
                    r["location_source"] if located else "-",
                    fmt_age(r["location_updated_at"], now) if located else "-",
                    "never" if advert is None or advert["last_heard"] is None else fmt_age(advert["last_heard"], now),
                    "-" if advert is None else fmt_advert_clock(advert["advert_timestamp"]),
                    "-" if r["window_s"] is None else f"{r['window_s']:g}s",
                    "yes" if r["enabled"] else "no",
                ]
            )
    print(table(rows, ["NAME", "PREFIX", "LAT", "LON", "SOURCE", "UPDATED", "HEARD", "ADVERT CLOCK", "WINDOW", "ENABLED"]) if rows else "no repeaters")
    return 0


def cmd_repeater_adverts(args: argparse.Namespace, cfg: Config) -> int:
    """What each repeater last advertised, as the companion passed it on, next to what the table uses."""
    now = time.time()
    with _open(cfg) as store:
        rows = []
        for a in store.repeater_adverts():
            advertised = f"{a['lat']:.6f}, {a['lon']:.6f}" if a["lat"] is not None else "none"
            used = "-"
            if a["trusted"]:
                used = f"{a['table_lat']:.6f}, {a['table_lon']:.6f}" if Store.is_located(a["table_lat"], a["table_lon"]) else "unlocated"
            rows.append(
                [
                    bytes(a["prefix"]).hex(),
                    a["name"] or "-",
                    advertised,
                    used,
                    "never" if a["last_heard"] is None else fmt_age(a["last_heard"], now),
                    fmt_advert_clock(a["advert_timestamp"]),
                    "yes" if a["trusted"] else "no",
                ]
            )
    print(table(rows, ["PREFIX", "ADVERTISED NAME", "ADVERTISED POSITION", "USED POSITION", "HEARD", "ADVERT CLOCK", "TRUSTED"]) if rows else "no adverts seen")
    return 0


def cmd_repeater_remove(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as store:
        r = store.remove_repeater(args.ref)
    print(f"removed repeater {_repeater_label(r)}")
    return 0


def cmd_repeater_enable(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as store:
        r = store.set_repeater_enabled(args.ref, args.enable)
    print(f"repeater {_repeater_label(r)} {'enabled' if args.enable else 'disabled'}")
    return 0


def cmd_repeater_window(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as store:
        r = store.set_repeater_window(args.ref, args.seconds)
    print(f"recorded beacon.window {args.seconds:g}s for {_repeater_label(r)}; set the same value on the repeater itself")
    return 0


# --- status and rejects --------------------------------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    now = time.time()
    with _open(cfg) as store:
        trusted, clock_line = clock_state(store, cfg)
        ingest = ingest_lines(store, now)
        assessed = health.assess(store, cfg.beacon, now)
        _, repeaters = store.names()
        unknown_b = store.unknown_beacons(now - args.hours * 3600)
        unknown_r = store.unknown_repeaters(now - args.hours * 3600)
        auto_b, auto_r = store.autoadd("beacons"), store.autoadd("repeaters")
        rows = []
        for h in assessed:
            b = h.beacon
            if h.state == health.REJECTED:
                r = h.rejects
                detail = (
                    f"{r.count} replays rejected since {fmt_clock(r.first_at)} (counters {r.min_counter}-{r.max_counter}, "
                    f"hwm {b['hwm']}) via {', '.join(r.repeater_names)}; fix: beaconctl beacon reset {bytes(b['prefix']).hex()}"
                )
            elif h.state == health.SILENT:
                detail = "never heard" if b["last_heard_at"] is None else f"not heard for {fmt_age(b['last_heard_at'], now).removesuffix(' ago')}"
                if h.baseline_pending:
                    detail += " (baseline pending)"
            elif h.state == health.OK:
                tx = store.latest_transmission(bytes(b["prefix"]))
                detail = f"counter {b['hwm']}, heard by {tx['n_repeaters']} repeater(s)" if tx else ""
            else:
                detail = ""
            rows.append(
                [
                    h.state,
                    b["name"] or "-",
                    bytes(b["prefix"]).hex(),
                    "-" if b["hwm"] is None else str(b["hwm"]),
                    fmt_age(b["last_heard_at"], now),
                    fmt_batt(h.batt_mv),
                    detail,
                ]
            )
    print("\n".join(ingest) + "\n")
    if not trusted:
        print(f"! clock {clock_line}\n")
    if auto_b or auto_r:
        on = " and ".join(k for k, v in (("repeaters", auto_r), ("beacons", auto_b)) if v)
        print(f"! auto-add is ON for {on}: new ones are trusted as they are seen. Lock with: beaconctl autoadd {'all' if auto_b and auto_r else ('beacons' if auto_b else 'repeaters')} off\n")
    print(table(rows, ["STATE", "NAME", "PREFIX", "HWM", "HEARD", "BATT", "DETAIL"]) if rows else "no beacons; add one with 'beaconctl beacon add'")
    if unknown_r:
        print(f"\nrepeaters seen but not in the repeater table, their reports are ignored (last {args.hours:g}h):")
        for u in unknown_r:
            prefix = bytes(u["prefix"]).hex()
            where = f"at {u['lat']:.6f}, {u['lon']:.6f}" if u["lat"] is not None else "no location advertised"
            seen = f"{u['n']} reports" if u["n"] else ("advert only" if u["advertised"] else "")
            print(
                f"  {prefix}  {repr(u['name']) if u['name'] else '(no name advertised)'}  {where}  {seen}, "
                f"last {fmt_age(u['last_seen'], now)}   add: beaconctl repeater add {prefix}"
            )
    if unknown_b:
        print(f"\nbeacons seen but not on the allowlist (last {args.hours:g}h):")
        for u in unknown_b:
            prefix = bytes(u["beacon_prefix"]).hex()
            via = "" if u["n_trusted"] else ", only via repeaters that are not trusted yet"
            print(
                f"  {prefix}  {repr(u['name']) if u['name'] else '(no name announced)'}  {u['n']} reports, "
                f"{u['n_repeaters']} repeater(s), last {fmt_age(u['last_seen'], now)}, counter {u['last_counter']}{via}"
                f"   add: beaconctl beacon add {prefix}"
            )
    return 0


def cmd_rejects(args: argparse.Namespace, cfg: Config) -> int:
    with _open(cfg) as store:
        rows = store.rejects(args.beacon, args.limit)
        bad = store.bad_reports(5) if args.beacon is None else []
        out = []
        for r in rows:
            what = r["status"] + (f"/{r['reason']}" if r["reason"] else "")
            hwm = f" (hwm {r['beacon_hwm']})" if r["beacon_hwm"] is not None and r["status"] == "replay" else ""
            out.append(
                [
                    fmt_time(r["rx_time"]) + ("" if r["time_trusted"] else "?"),
                    what,
                    names.label(r["beacon_name"], r["beacon_prefix"]),
                    names.label(r["repeater_name"], r["repeater_prefix"]),
                    f"counter {r['counter']}{hwm}",
                ]
            )
    print(table(out, ["TIME", "REASON", "BEACON", "VIA", "DETAIL"]) if out else "no rejected observations")
    if bad:
        print("\nmalformed reports:")
        for f in bad:
            print(f"  {fmt_time(f['rx_time'])}  {f['detail'].split(':', 1)[0]}  {bytes(f['payload']).hex()[:60]}")
    if any(r[0].endswith("?") for r in out):
        print("\n? = the clock was not set when this was heard; the time is provisional")
    return 0


# --- positions -----------------------------------------------------------------------------------------------------------


def cmd_positions_rebuild(args: argparse.Namespace, cfg: Config) -> int:
    """Estimate transmissions again: positions are worked out when a report arrives, so a repeater located afterwards (an advert, or
    'repeater locate') does not change the ones already stored until this is run."""
    from .estimate import Estimator
    from .pipeline import Pipeline

    since = None if args.hours is None else time.time() - args.hours * 3600
    with _open(cfg) as store:
        n = Pipeline(store, estimator=Estimator(cfg.estimate, cfg.radio.freq_khz)).rebuild_positions(since)
    print(f"{n} transmission(s) now have a position")
    return 0


# --- tiles ---------------------------------------------------------------------------------------------------------------


def cmd_tiles_check(args: argparse.Namespace, cfg: Config) -> int:
    """Say what is in a tiles file and whether beacon-web will show it where the repeaters are: the zoom levels, the metadata, how the rows
    are numbered. Exit status 1 when it will not show them."""
    from . import tiles as tiles_mod

    path = args.file or cfg.tiles_path
    if not path:
        raise StoreError("no tiles file: give one, or set web.tiles in the configuration")
    try:
        mb = tiles_mod.MBTiles(path, args.scheme or cfg.web.tiles_scheme)
    except tiles_mod.TilesError as e:
        print(f"problem: {e}")
        return 1
    with _open(cfg) as store:
        points = [(r["lat"], r["lon"]) for r in store.located_repeaters()]
    problems: list[str] = []
    print(f"file          {mb.path}")
    with mb._lock:
        levels = mb._conn.execute(
            "SELECT zoom_level, count(*), round(sum(length(tile_data)) / 1048576.0, 1) FROM tiles GROUP BY 1 ORDER BY 1"
        ).fetchall()
    print("zoom levels   " + (", ".join(f"{z}: {n} tiles ({mb_size} MB)" for z, n, mb_size in levels) if levels else "none"))
    if not levels:
        problems.append("the file has no tiles")
    meta = mb.metadata
    print(f"metadata      {', '.join(f'{k}={str(v)[:40]!r}' for k, v in sorted(meta.items())) if meta else 'none'}")
    if not meta:
        problems.append("no metadata: the map has no credit and no declared zoom range (docs/map-tiles.md, step 6)")
    elif not (cfg.web.tiles_attribution or meta.get("attribution")):
        problems.append("no attribution in the metadata and web.tiles_attribution is empty: the map shows no credit")
    declared = (meta.get("minzoom"), meta.get("maxzoom"))
    if all(declared) and levels and (int(declared[0]), int(declared[1])) != (levels[0][0], levels[-1][0]):
        problems.append(f"the metadata says zoom {declared[0]}-{declared[1]} but the tiles are zoom {levels[0][0]}-{levels[-1][0]}; the map uses the overlap")
    print(f"map zoom      {mb.info.min_zoom}-{mb.info.max_zoom}")
    counts = mb.resolve_scheme(points)
    if not points:
        print("repeaters     none is located, so the rows cannot be checked against them (beaconctl repeater list)")
    else:
        found, counts = tiles_mod.detect_scheme(mb, points)
        print(f"repeaters     {len(points)} located; with the rows read as TMS {counts['tms']} have a tile, as XYZ {counts['xyz']}")
    print(f"scheme        {mb.scheme} ({mb.scheme_source})")
    if points:
        if counts["tms"] == counts["xyz"] == 0:
            problems.append("no tile lies under any repeater at any zoom level: the file is for another area")
        elif found is None:
            problems.append("the repeaters do not say how the rows are numbered")
        elif found != mb.scheme:
            problems.append(f"the rows are numbered as {found}, but the scheme is set to {mb.scheme}: nothing will show; use web.tiles_scheme = \"{found}\"")
        elif mb.detected:
            print(f"              this file is not as the MBTiles specification says; set web.tiles_scheme = \"{found}\" to say so, or convert it (docs/map-tiles.md)")
    for p in problems:
        print(f"problem: {p}")
    if not problems:
        print("ok")
    return 1 if problems else 0


def cmd_tiles_write_metadata(args: argparse.Namespace, cfg: Config) -> int:
    """Make a tiles file ready for the map: renumber its rows from the south when they are numbered from the north, and write its metadata,
    the credit above all. Prints the whole metadata table afterwards."""
    from . import tiles as tiles_mod

    with _open(cfg) as store:
        points = [(r["lat"], r["lon"]) for r in store.located_repeaters()]
    try:
        r = tiles_mod.write_metadata(
            args.file, points, args.rows, name=args.name, attribution=args.attribution, description=args.description, dry_run=args.dry_run
        )
    except tiles_mod.TilesError as e:
        raise StoreError(str(e)) from None
    would = "" if r.written else "would be "
    print(f"file      {args.file}" + ("   (dry run: nothing is written)" if args.dry_run else ""))
    if r.renumbered:
        print(f"rows      {would}RENUMBERED from the north (XYZ) to the south (TMS), the MBTiles specification: {r.tiles} tiles ({r.rows_note})")
    else:
        print(f"rows      numbered from the south (TMS) already: left as they are ({r.rows_note})")
    print("levels    " + ", ".join(f"{z}: {n}" for z, n in r.levels))
    print("metadata  written:" if r.written else "metadata  as it would be written:")
    width = max(len(k) for k in r.metadata)
    for key, value in sorted(r.metadata.items()):
        print(f"  {key:<{width}}  {value}")
    if not (r.metadata.get("attribution") or "").strip():
        print("warning: the attribution is empty, so the map shows no credit")
    if r.written:
        print("check it with:  beaconctl tiles check " + str(args.file) + "   (restart beacon-web to use the new file)")
    return 0


# --- auto-add ------------------------------------------------------------------------------------------------------------


def cmd_autoadd(args: argparse.Namespace, cfg: Config) -> int:
    """Show or change whether unknown beacons and repeaters are trusted automatically (a setting in the database, so the
    running ingest service picks it up at once)."""
    with _open(cfg) as store:
        if args.what:
            if args.state is None:
                raise StoreError("say on or off: beaconctl autoadd {beacons,repeaters,all} {on,off}")
            for kind in ("repeaters", "beacons") if args.what == "all" else (args.what,):
                store.set_autoadd(kind, args.state == "on")
        state = {k: store.autoadd(k) for k in ("repeaters", "beacons")}
    for kind, on in state.items():
        print(f"auto-add {kind}: {'ON' if on else 'off'}")
    if any(state.values()):
        print("new ones are trusted as they are seen; lock the setup with 'beaconctl autoadd all off' once everything is discovered")
    return 0


# --- time ----------------------------------------------------------------------------------------------------------------


HOST_CLOCK_HINT = (
    "If beaconctl runs in a container it cannot set the host's clock: set the Pi's clock on the Pi "
    "(sudo timedatectl set-time 'YYYY-MM-DD HH:MM:SS'), then run 'beaconctl time confirm'; ingest also notices the change by itself."
)


def run_timedatectl(*args: str) -> str:
    """Run timedatectl, through sudo when not root. Isolated so tests can replace it."""
    cmd = ["timedatectl", *args]
    setting = bool(args) and args[0] == "set-time"
    if shutil.which("timedatectl") is None:
        raise StoreError("this machine has no timedatectl." + (f" {HOST_CLOCK_HINT}" if setting else ""))
    if os.geteuid() != 0 and setting:
        cmd = ["sudo", *cmd]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise StoreError(f"cannot run {' '.join(cmd)}: {e}" + (f". {HOST_CLOCK_HINT}" if setting else "")) from e
    if res.returncode != 0:
        raise StoreError(f"{' '.join(cmd)} failed: {(res.stderr or res.stdout).strip()}" + (f". {HOST_CLOCK_HINT}" if setting else ""))
    return res.stdout.strip()


def cmd_time(args: argparse.Namespace, cfg: Config) -> int:
    now = time.time()
    with _open(cfg) as store:
        _, line = clock_state(store, cfg)
        untrusted = store.conn.execute(
            "SELECT count(*) FROM observations WHERE boot_id = ? AND time_trusted = 0", (clock.boot_id(),)
        ).fetchone()[0]
    try:
        ntp = run_timedatectl("show", "-p", "NTPSynchronized", "--value")
        ntp = "yes" if ntp == "yes" else "no"
    except StoreError:
        ntp = "unknown"
    print(f"system clock      {fmt_time(now)} (local time)")
    print(f"boot id           {clock.boot_id()[:8]}, up {fmt_duration(time.monotonic())}")
    print(f"clock state       {line}")
    print(f"ntp synchronised  {ntp}")
    print(f"provisional       {untrusted} observation(s) this boot with an unconfirmed time")
    return 0


def _record_clock_event(cfg: Config, kind: str, offset_before: float) -> int:
    with _open(cfg) as store, store.transaction() as db:
        return clock.apply_clock_event(db, clock.boot_id(), kind, offset_before, clock.offset(), time.time())


def cmd_time_set(args: argparse.Namespace, cfg: Config) -> int:
    value = args.value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            when = datetime.datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    else:
        raise StoreError("time must look like 'YYYY-MM-DD HH:MM:SS' (local time)")
    before = clock.offset()
    run_timedatectl("set-time", when.strftime("%Y-%m-%d %H:%M:%S"))
    n = _record_clock_event(cfg, "set", before)
    print(f"clock set to {fmt_time(time.time())}; corrected the time of {n} earlier observation(s)")
    return 0


def cmd_time_confirm(args: argparse.Namespace, cfg: Config) -> int:
    """The clock is already right (set another way, or an RTC): trust it for this boot and fix provisional times."""
    n = _record_clock_event(cfg, "set", clock.offset())
    print(f"clock confirmed as {fmt_time(time.time())}; corrected the time of {n} earlier observation(s)")
    return 0


# --- health --------------------------------------------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace, cfg: Config) -> int:
    """One line and an exit status, for a container's HEALTHCHECK or a script: 0 when beacon-ingest is running (and, with --companion,
    has the companion connected), 1 otherwise. Does not open the serial port and does not create a database."""
    if not cfg.db_path.exists():
        print(f"unhealthy: no database at {cfg.db_path}; beacon-ingest has not started")
        return 1
    now = time.time()
    with _open(cfg) as store:
        row = store.status_row()
    state = heartbeat.state(row)
    if state != heartbeat.UP:
        why = {heartbeat.NEVER: "has never run", heartbeat.STOPPED: "was stopped", heartbeat.DOWN: "is not running"}[state]
        heard = f" (last heard from {fmt_age(row['updated_at'], now)})" if row is not None else ""
        print(f"unhealthy: beacon-ingest {why}{heard}")
        return 1
    if not row["connected"]:
        if args.companion:
            print("unhealthy: beacon-ingest is running but the companion is not connected")
            return 1
        print("ok: beacon-ingest is running; the companion is not connected")
        return 0
    print(f"ok: beacon-ingest is running, companion connected, last report {fmt_age(row['last_report_at'], now)}")
    return 0


# --- check ---------------------------------------------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace, cfg: Config) -> int:
    problems: list[str] = []
    interval = cfg.beacon.interval_s
    shortest = interval * (1 - cfg.beacon.jitter)
    advised = interval * 0.8
    with _open(cfg) as store:
        repeaters = store.repeaters()
        beacons = store.beacons()
        auto = [k for k in ("repeaters", "beacons") if store.autoadd(k)]
        status = store.status_row()
        channels = {bytes(r["prefix"]): store.latest_channel_reading(bytes(r["prefix"])) for r in repeaters}
    now = time.time()
    base_hash = remote.channel_hash(cfg.channel_key) if cfg.channel_key else None
    ingest = heartbeat.state(status)
    if ingest in (heartbeat.DOWN, heartbeat.STOPPED):
        problems.append(f"beacon-ingest is not running ({ingest}); reports are not being received")
    elif ingest == heartbeat.UP and not status["connected"]:
        problems.append("beacon-ingest is running but the companion is not connected")
    if auto:
        problems.append(
            f"auto-add is on for {' and '.join(auto)}, so anything on the channel is trusted; lock the setup once everything "
            "is discovered with 'beaconctl autoadd all off'"
        )
    if cfg.channel_key is None:
        problems.append("no report channel key; run 'beaconctl channel generate' (or 'channel set')")
    if not beacons:
        problems.append("no beacons on the allowlist")
    if not repeaters:
        problems.append("no repeaters in the repeater table")
    for r in repeaters:
        w = r["window_s"]
        reading = channels.get(bytes(r["prefix"]))
        if reading is not None:  # read over the mesh with 'beaconctl repeater remote'; compared with the key as it is now
            channel, read_at = reading
            if not channel["set"]:
                problems.append(
                    f"repeater {_repeater_label(r)}: no report channel set (read {fmt_age(read_at, now)}), so it sends no reports; set it "
                    "on its serial console with 'beacon.channel <key>' ('beaconctl channel show' prints the key)"
                )
            elif base_hash is not None and channel["hash"] != base_hash:
                problems.append(
                    f"repeater {_repeater_label(r)}: its report channel (hash {channel['hash']:02X}, read {fmt_age(read_at, now)}) is not "
                    f"this base's (hash {base_hash:02X}), so its reports cannot be read"
                )
        if not Store.is_located(r["lat"], r["lon"]):
            problems.append(
                f"repeater {_repeater_label(r)}: no location, so it is left out of positioning; set it on the repeater and send "
                "an advert ('advert' in its CLI), or use 'beaconctl repeater locate'"
            )
        if w is None:
            problems.append(f"repeater {_repeater_label(r)}: beacon.window not recorded; set it with 'beaconctl repeater window'")
        elif w >= shortest:
            problems.append(
                f"repeater {_repeater_label(r)}: beacon.window {w:g}s is not below the shortest beacon interval ({shortest:g}s); "
                "reports for one transmission will arrive after the next and be rejected as late"
            )
        elif w > advised:
            problems.append(f"repeater {_repeater_label(r)}: beacon.window {w:g}s is above the advised {advised:g}s (80% of the beacon interval)")
    for p in problems:
        print(f"warning: {p}")
    if not problems:
        print(f"ok: {len(beacons)} beacon(s), {len(repeaters)} repeater(s), repeater windows below {advised:g}s")
    return 1 if problems else 0
