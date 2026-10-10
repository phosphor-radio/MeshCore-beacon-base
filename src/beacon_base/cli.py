"""beaconctl: provisioning and diagnostics."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import logging
import secrets
import sys
import threading

from . import __version__, admin
from .config import CHANNEL_KEY_LEN, Config, ConfigError, load_config, parse_channel_key, store_channel_key
from .ingest import CompanionSession, ConnectionInfo, Handler, ReceivedNames, ReceivedReport
from .link import CompanionError
from .names import sanitize_name
from .runtime import setup_logging, stop_on_signals
from .store import Store, StoreError

log = logging.getLogger("beaconctl")


def _hops(path_len: int) -> str:
    return "direct" if path_len == 0xFF else str(path_len & 0x3F)  # low 6 bits are the hop count


class PrintHandler(Handler):
    """Prints one line per observation: the bring-up view of what the repeaters report."""

    def __init__(self, stop: threading.Event, as_json: bool = False, limit: int | None = None):
        self._stop = stop
        self._json = as_json
        self._limit = limit
        self.printed = 0

    def on_connected(self, info: ConnectionInfo) -> None:
        log.info("connected to %s (%s)", info.self_info.name, info.device_info.model)

    def on_report(self, rx: ReceivedReport) -> None:
        if self._stop.is_set():
            return  # shutting down, the rest of a drain is not printed
        when = datetime.datetime.fromtimestamp(rx.rx_wall).isoformat(timespec="seconds")
        for o in rx.report.observations:
            if self._json:
                print(
                    json.dumps(
                        {
                            "time": when,
                            "late": rx.late,
                            "repeater": rx.report.repeater_id.hex(),
                            "hops": _hops(rx.path_len),
                            "companion_snr": rx.companion_snr_x4 / 4,
                            "beacon": o.beacon_id.hex(),
                            "counter": o.counter,
                            "rssi": o.rssi,
                            "snr": o.snr,
                            "batt_mv": o.batt_mv,
                        }
                    ),
                    flush=True,
                )
            else:
                print(
                    f"{when}{' late' if rx.late else '     '} repeater={rx.report.repeater_id.hex()} "
                    f"hops={_hops(rx.path_len)} csnr={rx.companion_snr_x4 / 4:+.2f} "
                    f"beacon={o.beacon_id.hex()} ctr={o.counter} rssi={o.rssi} snr={o.snr:+.2f} batt={o.batt_mv}mV",
                    flush=True,
                )
            self.printed += 1
            if self._limit is not None and self.printed >= self._limit:
                self._stop.set()
                return

    def on_names(self, rx: ReceivedNames) -> None:
        if self._stop.is_set():
            return
        when = datetime.datetime.fromtimestamp(rx.rx_wall).isoformat(timespec="seconds")
        for e in rx.announcement.entries:
            name = sanitize_name(e.name)
            if name is None:
                continue
            if self._json:
                print(json.dumps({"type": "name", "time": when, "repeater": rx.announcement.repeater_id.hex(), "beacon": e.beacon_id.hex(), "name": name}), flush=True)
            else:
                print(f"{when}      repeater={rx.announcement.repeater_id.hex()} beacon={e.beacon_id.hex()} name={name!r}", flush=True)

    def on_drop(self, reason: str, detail: str, raw=None) -> None:
        print(f"dropped ({reason}): {detail}", file=sys.stderr, flush=True)


def _with_port(cfg: Config, port: str | None) -> Config:
    if port:
        return dataclasses.replace(cfg, companion=dataclasses.replace(cfg.companion, port=port))
    return cfg


def cmd_channel_generate(args: argparse.Namespace, cfg: Config) -> int:
    key = secrets.token_bytes(CHANNEL_KEY_LEN)
    path = store_channel_key(cfg, key, force=args.force)
    print(f"saved to {path} (mode 0600)")
    print(f"channel key: {key.hex()}")
    print("set it on each repeater with:  beacon.channel " + key.hex())
    return 0


def cmd_channel_set(args: argparse.Namespace, cfg: Config) -> int:
    text = sys.stdin.readline() if args.key == "-" else args.key
    key = parse_channel_key(text)
    if cfg.channel_key == key:
        print(f"{cfg.secrets_path} already holds this key")
        return 0
    path = store_channel_key(cfg, key, force=args.force)
    print(f"saved to {path} (mode 0600)")
    return 0


def cmd_channel_show(args: argparse.Namespace, cfg: Config) -> int:
    if cfg.channel_key is None:
        raise ConfigError("no report channel key; run 'beaconctl channel generate' first")
    print(cfg.channel_key.hex())
    return 0


def cmd_listen(args: argparse.Namespace, cfg: Config) -> int:
    cfg = _with_port(cfg, args.port)
    stop = stop_on_signals()
    handler = PrintHandler(stop, as_json=args.json, limit=args.count)
    session = CompanionSession(cfg, handler)
    session.run(stop)
    return 0


def cmd_simulate(args: argparse.Namespace, cfg: Config) -> int:
    from .fake_companion import FakeCompanion
    from .simulate import Simulator

    stop = stop_on_signals()
    with FakeCompanion() as fake:
        sim = Simulator(fake, cfg.companion.channel_index, args.beacons, args.repeaters, args.seed)
        if args.provision:
            with Store.open(cfg.db_path) as store:
                for beacon_id in sim.beacon_ids:
                    store.add_beacon(beacon_id.hex(), "simulated")
                for i, key in enumerate(sim.repeater_keys, 1):
                    store.add_repeater(key.hex(), 40.0 + 0.01 * i, -75.0 + 0.01 * i, name=f"sim-repeater-{i}", window_s=20)
            print(f"added the simulated beacons and repeaters to {cfg.db_path}")
        print(f"fake companion on {fake.path}")
        print(f"  ingest with:  beacon-ingest --port {fake.path}   (or: beaconctl listen --port ...)")
        for i, beacon_id in enumerate(sim.beacon_ids, 1):
            print(f"  beacon {i}:    {beacon_id.hex()}")
        for i, key in enumerate(sim.repeater_keys, 1):
            print(f"  repeater {i}:  {key.hex()}", flush=True)
        sim.announce_names()
        ticks = 0
        while not stop.wait(args.interval):
            sim.tick()
            ticks += 1
            if ticks % args.names_every == 0:
                sim.announce_names()
    return 0


def cmd_ingest(args: argparse.Namespace, cfg: Config) -> int:
    from . import service

    service.run(_with_port(cfg, args.port), stop_on_signals())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="beaconctl", description="Beacon base provisioning and diagnostics")
    p.add_argument("-c", "--config", help="config file (default: $BEACON_BASE_CONFIG or ~/.config/beacon-base/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"beaconctl {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    channel = sub.add_parser("channel", help="report channel key").add_subparsers(dest="channel_command", required=True)
    gen = channel.add_parser("generate", help="create a new random channel key")
    gen.add_argument("--force", action="store_true", help="replace an existing key (every repeater must be updated)")
    gen.set_defaults(func=cmd_channel_generate)
    setk = channel.add_parser("set", help="store a predetermined channel key, for example one already on the repeaters")
    setk.add_argument("key", help="32 hex characters (16 bytes), or - to read it from stdin and keep it out of shell history")
    setk.add_argument("--force", action="store_true", help="replace an existing key (every repeater must be updated)")
    setk.set_defaults(func=cmd_channel_set)
    show = channel.add_parser("show", help="print the channel key, for provisioning a repeater")
    show.set_defaults(func=cmd_channel_show)

    listen = sub.add_parser("listen", help="print decoded reports live")
    listen.add_argument("--port", help="companion serial port (overrides companion.port)")
    listen.add_argument("--json", action="store_true", help="one JSON object per observation")
    listen.add_argument("--count", type=int, help="exit after this many observations")
    listen.set_defaults(func=cmd_listen)

    ingest = sub.add_parser("ingest", help="run the ingest service in the foreground (same as beacon-ingest)")
    ingest.add_argument("--port", help="companion serial port (overrides companion.port)")
    ingest.set_defaults(func=cmd_ingest)

    _add_admin_commands(sub)

    sim = sub.add_parser("simulate", help="run a fake companion on a pseudo-terminal with synthetic reports")
    sim.add_argument("--interval", type=float, default=5.0, help="seconds between beacon transmissions")
    sim.add_argument("--beacons", type=int, default=3)
    sim.add_argument("--repeaters", type=int, default=3)
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--names-every", type=int, default=12, metavar="N", help="repeat the repeaters' name announcements every N ticks")
    sim.add_argument("--provision", action="store_true", help="also add the simulated beacons and repeaters to the database")
    sim.set_defaults(func=cmd_simulate)
    return p


REF_HELP = "the beacon's key prefix, or the first six or more hex digits of it, as 'beaconctl status' shows"
REPEATER_REF_HELP = "the repeater's key prefix (six or more hex digits) or its name"


def _add_admin_commands(sub) -> None:
    beacon = sub.add_parser("beacon", help="beacon allowlist").add_subparsers(dest="beacon_command", required=True)
    p = beacon.add_parser("add", help="allowlist a beacon by the 8-byte key prefix its reports carry, or every reported one with --all")
    p.add_argument("prefix", nargs="?", help="16 hex characters, as listed by 'beaconctl status' (a full 64-character key is also accepted)")
    p.add_argument("--notes")
    p.add_argument("-a", "--all", action="store_true", help="add every beacon repeaters have reported that is not on the allowlist")
    p.add_argument("--hours", type=float, default=24.0, help="with --all: how far back to look for reported beacons")
    p.set_defaults(func=admin.cmd_beacon_add)
    beacon.add_parser("list", help="list allowlisted beacons").set_defaults(func=admin.cmd_beacon_list)
    p = beacon.add_parser("status", help="details of one beacon")
    p.add_argument("ref", metavar="prefix", help=REF_HELP)
    p.set_defaults(func=admin.cmd_beacon_status)

    def one_or_all(parser) -> None:
        parser.add_argument("ref", nargs="?", metavar="prefix", help=REF_HELP)
        parser.add_argument("-a", "--all", action="store_true", help="every beacon on the allowlist")

    p = beacon.add_parser("reset", help="clear the high-water mark; the next report becomes the new baseline")
    one_or_all(p)
    p.set_defaults(func=admin.cmd_beacon_reset)
    p = beacon.add_parser("remove", help="remove a beacon from the allowlist (history is kept)")
    one_or_all(p)
    p.set_defaults(func=admin.cmd_beacon_remove)
    for verb, enable in (("enable", True), ("disable", False)):
        p = beacon.add_parser(verb, help=f"{verb} a beacon without removing it")
        one_or_all(p)
        p.set_defaults(func=admin.cmd_beacon_enable, enable=enable)

    repeater = sub.add_parser("repeater", help="repeater table").add_subparsers(dest="repeater_command", required=True)
    p = repeater.add_parser("add", help="add a repeater and its location")
    p.add_argument("key", help="public key (64 hex characters) or its 8-byte prefix (16 hex characters)")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--name", help="optional display name; not unique, and never required")
    p.add_argument("--window", type=float, help="the repeater's beacon.window in seconds, checked by 'beaconctl check'")
    p.set_defaults(func=admin.cmd_repeater_add)
    repeater.add_parser("list", help="list repeaters").set_defaults(func=admin.cmd_repeater_list)
    p = repeater.add_parser("remove", help="remove a repeater")
    p.add_argument("ref", metavar="prefix-or-name", help=REPEATER_REF_HELP)
    p.set_defaults(func=admin.cmd_repeater_remove)
    for verb, enable in (("enable", True), ("disable", False)):
        p = repeater.add_parser(verb, help=f"{verb} a repeater without removing it")
        p.add_argument("ref", metavar="prefix-or-name", help=REPEATER_REF_HELP)
        p.set_defaults(func=admin.cmd_repeater_enable, enable=enable)
    p = repeater.add_parser("window", help="record a repeater's beacon.window")
    p.add_argument("ref", metavar="prefix-or-name", help=REPEATER_REF_HELP)
    p.add_argument("seconds", type=float)
    p.set_defaults(func=admin.cmd_repeater_window)

    p = sub.add_parser("status", help="one line per beacon, rejected and silent first")
    p.add_argument("--hours", type=float, default=24.0, help="how far back to look for unlisted beacons and repeaters")
    p.set_defaults(func=admin.cmd_status)
    p = sub.add_parser("rejects", help="recent observations that were not accepted")
    p.add_argument("--beacon", metavar="prefix", help="only this beacon (key prefix, six or more hex digits)")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=admin.cmd_rejects)

    p = sub.add_parser("time", help="show the clock state; 'set' and 'confirm' fix it")
    p.set_defaults(func=admin.cmd_time)
    tsub = p.add_subparsers(dest="time_command")
    q = tsub.add_parser("set", help="set the system clock (uses sudo timedatectl) and fix provisional times")
    q.add_argument("value", help="local time, 'YYYY-MM-DD HH:MM:SS'")
    q.set_defaults(func=admin.cmd_time_set)
    tsub.add_parser("confirm", help="the system clock is already right: trust it and fix provisional times").set_defaults(
        func=admin.cmd_time_confirm
    )

    sub.add_parser("check", help="sanity-check the setup, including repeater report windows").set_defaults(func=admin.cmd_check)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args, load_config(args.config))
    except (ConfigError, CompanionError, StoreError) as e:
        print(f"beaconctl: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
