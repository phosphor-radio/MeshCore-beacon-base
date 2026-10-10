"""beacon-ingest: the service that owns the companion's serial port and feeds the pipeline."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import threading
from collections import Counter

from . import clock, names
from .config import Config, ConfigError, load_config
from .ingest import CompanionSession, ConnectionInfo, Handler, HeardRepeater, RawFrame, ReceivedNames, ReceivedReport
from .link import DTR_MODES, CompanionError
from .pipeline import Pipeline
from .runtime import setup_logging, stop_on_signals
from .store import Store, StoreError

log = logging.getLogger("beacon-ingest")


class PipelineHandler(Handler):
    """Runs every decoded report through the replay/dedupe pipeline and watches for the system clock being set."""

    def __init__(self, store: Store, pipeline: Pipeline):
        self._store = store
        self._pipeline = pipeline
        self._offset = clock.offset()  # wall minus monotonic clock; a jump means someone set the clock
        self.counts: Counter[str] = Counter()

    def on_connected(self, info: ConnectionInfo) -> None:
        log.info("connected to companion %r", info.self_info.name)

    def on_synced(self) -> None:
        log.info("companion queue drained, now live")

    def clock_trusted(self) -> bool:
        return self._pipeline.time_trusted()

    def on_disconnected(self, error: str | None) -> None:
        log.warning("companion disconnected%s", f": {error}" if error else "")

    def on_report(self, rx: ReceivedReport) -> None:
        self._check_clock_step(rx)
        verdicts = self._pipeline.process(rx)
        _, repeaters = self._store.names()
        summary: Counter[str] = Counter()
        for v in verdicts:
            summary[v.status] += 1
            self.counts[v.status] += 1
            who = f"{names.label(v.beacon_name, v.beacon_prefix)} counter {v.counter} via {names.label(v.repeater_name, v.repeater_prefix)}"
            if v.auto_added:
                self.counts["auto_added"] += 1
                log.info("auto-added %s (%s)", v.auto_added.replace("+", " and "), who)
            if v.status == "accepted":
                log.debug("accepted %s", who)
            elif v.status == "duplicate":
                log.debug("duplicate %s", who)
            elif v.reason == "late":
                log.info("late report (a newer transmission was already accepted): %s", who)
            else:
                log.warning("%s%s: %s", v.status, f" ({v.reason})" if v.reason else "", who)
        log.info(
            "report from %s%s: %s",
            names.label(repeaters.get(bytes(rx.report.repeater_id)), rx.report.repeater_id),
            " (late)" if rx.late else "",
            ", ".join(f"{n} {status}" for status, n in sorted(summary.items())) or "no entries",
        )

    def on_repeater_advert(self, advert: HeardRepeater) -> None:
        effect = self._store.record_repeater_advert(
            advert.public_key, advert.name, advert.lat, advert.lon, advert.advert_timestamp, advert.heard_at
        )
        who = names.label(advert.name, advert.public_key[:8])
        if effect.added:
            self.counts["auto_added"] += 1
            log.info("auto-added repeater %s from its advert", who)
        if effect.position is not None:
            old = effect.old_position
            if effect.trusted and not effect.added and old not in (None, (0.0, 0.0)):
                log.info("repeater %s moved to %.6f, %.6f (was %.6f, %.6f)", who, *effect.position, *old)
            else:
                log.info("repeater %s is at %.6f, %.6f%s", who, *effect.position, "" if effect.trusted else " (not in the repeater table)")
            self.counts["repeater_positions"] += 1
        elif effect.trusted is False:
            log.debug("advert from repeater %s, which is not in the repeater table and has no position", who)
        if effect.trusted and not effect.added and effect.name is not None and effect.old_name != effect.name:
            log.info("repeater %s is named %r", names.prefix_label(advert.public_key), effect.name)

    def on_names(self, rx: ReceivedNames) -> None:
        changes = self._pipeline.process_names(rx)
        for c in changes:
            if c.old is None:
                log.info("learned the name of beacon %s: %r", names.prefix_label(c.beacon_prefix), c.new)
                self.counts["names_learned"] += 1
            elif c.changed:
                log.info("beacon %s renamed %r -> %r", names.prefix_label(c.beacon_prefix), c.old, c.new)
                self.counts["names_changed"] += 1
            else:
                log.debug("name of beacon %s unchanged: %r", names.prefix_label(c.beacon_prefix), c.new)

    def on_drop(self, reason: str, detail: str, raw: RawFrame | None = None) -> None:
        if raw is not None:
            self._pipeline.record_bad_report(raw, detail, "bad_names" if reason == "bad_names" else "bad_report")
            log.warning("dropped a malformed %s: %s", "name announcement" if reason == "bad_names" else "report", detail.split(":", 1)[0])
        else:
            log.debug("ignored frame (%s): %s", reason, detail)

    def _check_clock_step(self, rx: ReceivedReport) -> None:
        offset = rx.rx_wall - rx.rx_mono
        if abs(offset - self._offset) <= clock.STEP_THRESHOLD:
            return
        with self._store.transaction() as db:
            last = clock.last_event(db, self._pipeline.boot)
            if last is not None and abs(last["offset_after"] - offset) <= clock.STEP_THRESHOLD:
                n = None  # 'beaconctl time' already recorded and applied this change
            else:
                n = clock.apply_clock_event(db, self._pipeline.boot, "step", self._offset, offset, rx.rx_wall)
        if n is not None:
            log.warning("system clock was set (%+.0f s); corrected the time of %d earlier observations", offset - self._offset, n)
        self._offset = offset


def run(cfg: Config, stop: threading.Event) -> None:
    if cfg.channel_key is None:
        raise ConfigError("no report channel key; run 'beaconctl channel generate' first")
    with Store.open(cfg.db_path) as store:
        log.info("database %s (schema %d)", cfg.db_path, store.schema_version())
        pipeline = Pipeline(store, assume_synced=cfg.clock.assume_synced)
        CompanionSession(cfg, PipelineHandler(store, pipeline)).run(stop)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="beacon-ingest", description="Ingest beacon reports from a companion into the database")
    p.add_argument("-c", "--config", help="config file (default: $BEACON_BASE_CONFIG or ~/.config/beacon-base/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--port", help="companion serial port (overrides companion.port)")
    p.add_argument("--dtr", choices=DTR_MODES, help="DTR when opening the port (overrides companion.dtr; see docs/operations.md)")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
        changes = {k: v for k, v in (("port", args.port), ("dtr", args.dtr)) if v}
        if changes:
            cfg = dataclasses.replace(cfg, companion=dataclasses.replace(cfg.companion, **changes))
        run(cfg, stop_on_signals())
    except (ConfigError, CompanionError, StoreError) as e:
        print(f"beacon-ingest: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
