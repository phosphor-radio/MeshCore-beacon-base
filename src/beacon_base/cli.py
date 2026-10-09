"""beaconctl: provisioning and diagnostics."""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import logging
import secrets
import signal
import sys
import threading

from . import __version__, wire
from .config import CHANNEL_KEY_LEN, Config, ConfigError, load_config, parse_channel_key, store_channel_key
from .ingest import CompanionSession, ConnectionInfo, Handler, ReceivedReport
from .link import CompanionError

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

    def on_drop(self, reason: str, detail: str) -> None:
        print(f"dropped ({reason}): {detail}", file=sys.stderr, flush=True)


def _stop_on_signals() -> threading.Event:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


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
    stop = _stop_on_signals()
    handler = PrintHandler(stop, as_json=args.json, limit=args.count)
    session = CompanionSession(cfg, handler)
    session.run(stop)
    return 0


def cmd_simulate(args: argparse.Namespace, cfg: Config) -> int:
    from .fake_companion import FakeCompanion
    from .simulate import Simulator

    stop = _stop_on_signals()
    with FakeCompanion() as fake:
        sim = Simulator(fake, cfg.companion.channel_index, args.beacons, args.repeaters, args.seed)
        print(f"fake companion on {fake.path}")
        print(f"  listen with:  beaconctl listen --port {fake.path}")
        print("  beacons:  " + " ".join(b.hex() for b in sim.beacon_ids))
        print("  repeaters:  " + " ".join(k[: wire.ID_LEN].hex() for k in sim.repeater_keys), flush=True)
        while not stop.wait(args.interval):
            sim.tick()
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

    sim = sub.add_parser("simulate", help="run a fake companion on a pseudo-terminal with synthetic reports")
    sim.add_argument("--interval", type=float, default=5.0, help="seconds between beacon transmissions")
    sim.add_argument("--beacons", type=int, default=3)
    sim.add_argument("--repeaters", type=int, default=3)
    sim.add_argument("--seed", type=int, default=1)
    sim.set_defaults(func=cmd_simulate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return args.func(args, load_config(args.config))
    except (ConfigError, CompanionError) as e:
        print(f"beaconctl: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
