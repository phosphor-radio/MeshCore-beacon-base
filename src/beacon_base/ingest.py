"""Companion session: connect, provision, drain and decode beacon reports.

``CompanionSession`` owns the serial port. It reconnects on any failure, sets up the report channel, drains the
companion's offline queue and then waits for new messages. Decoded reports go to a ``Handler``; the replay/dedupe
pipeline (phase B2) is a Handler.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from . import companion, wire
from .companion import DeviceInfo, SelfInfo
from .config import CompanionConfig, Config, ConfigError, RadioConfig
from .link import CompanionError, CompanionLink, CommandError, LinkError, SerialTransport, Transport

log = logging.getLogger(__name__)

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
DRAIN_LIMIT = 1000  # messages per drain call, a guard against a companion that never says "no more"
FREQ_TOLERANCE_KHZ = 1  # the companion reports its float frequency truncated to whole kHz


@dataclass(frozen=True)
class ConnectionInfo:
    self_info: SelfInfo
    device_info: DeviceInfo


@dataclass(frozen=True)
class ReceivedReport:
    report: wire.Report
    companion_snr_x4: int  # SNR at the companion, not at the repeater
    path_len: int  # 0xFF when the packet reached the companion by a direct route
    rx_wall: float  # Pi clock, seconds since the epoch (may be wrong until the clock is set)
    rx_mono: float  # monotonic clock, seconds
    late: bool  # drained from the companion's queue right after connecting, so possibly delayed
    payload: bytes  # the undecoded report, kept for the audit trail


class Handler:
    """Receives session events. Override what you need; every method may be left alone."""

    def on_connected(self, info: ConnectionInfo) -> None: ...

    def on_synced(self) -> None:
        """The companion's offline queue has been drained after connecting; reports from here on are live."""

    def on_report(self, rx: ReceivedReport) -> None: ...

    def on_drop(self, reason: str, detail: str) -> None:
        """A frame that is not a usable beacon report. reason is one of Session.DROP_REASONS."""

    def on_disconnected(self, error: str | None) -> None: ...


def radio_matches(info: SelfInfo, radio: RadioConfig) -> bool:
    return (
        abs(info.freq_khz - radio.freq_khz) <= FREQ_TOLERANCE_KHZ
        and abs(info.bw_hz - radio.bw_hz) <= 1
        and info.sf == radio.sf
        and info.cr == radio.cr
    )


def _describe_radio(freq_khz: int, bw_hz: int, sf: int, cr: int) -> str:
    return f"{freq_khz / 1000:.3f} MHz BW {bw_hz / 1000:g} kHz SF{sf} CR4/{cr}"


class CompanionSession:
    DROP_REASONS = ("other_message", "other_channel", "other_data_type", "bad_frame", "bad_report")

    def __init__(
        self,
        config: Config,
        handler: Handler,
        transport_factory: Callable[[str], Transport] | None = None,
    ):
        if config.channel_key is None:
            raise ConfigError("no report channel key; run 'beaconctl channel generate' first")
        if not config.companion.port:
            raise ConfigError("no companion serial port; set companion.port or pass --port")
        self._cfg = config
        self._handler = handler
        self._transport_factory = transport_factory or (lambda path: SerialTransport(path, config.companion.baud))
        self.stats: Counter[str] = Counter()
        self._msg_waiting = False
        self._first_drain = True
        self._stop = threading.Event()

    # --- connection loop -------------------------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Serve until stop is set. Connection failures are logged and retried with backoff."""
        self._stop = stop
        delay = RECONNECT_MIN
        while not stop.is_set():
            connected_at = None
            error: str | None = None
            link = None
            try:
                link = CompanionLink(self._transport_factory(self._cfg.companion.port), self._cfg.companion.command_timeout)
                link.on_push = self._on_push
                connected_at = time.monotonic()
                self._serve(link, stop)
            except ConfigError:
                raise
            except CompanionError as e:
                error = str(e)
                log.warning("companion link lost: %s", e)
            finally:
                if link is not None:
                    link.close()
            if connected_at is not None:
                self._handler.on_disconnected(error)
            if stop.is_set():
                return
            if connected_at is not None and time.monotonic() - connected_at > 10 * RECONNECT_MAX:
                delay = RECONNECT_MIN  # it ran for a while, so this is a new failure
            log.info("reconnecting in %.0fs", delay)
            stop.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    def _on_push(self, frame: bytes) -> None:
        if frame[0] == companion.PUSH_MSG_WAITING:
            self._msg_waiting = True
        else:
            self.stats["push_ignored"] += 1
            log.debug("ignoring push %#04x", frame[0])

    # --- one connection -------------------------------------------------------------------------------------------

    def _serve(self, link: CompanionLink, stop: threading.Event) -> None:
        info = self._start(link)
        self._handler.on_connected(info)
        self._first_drain = True
        self._drain(link)
        self._first_drain = False
        self._handler.on_synced()
        next_poll = time.monotonic() + self._cfg.companion.poll_interval
        while not stop.is_set():
            frame = link.recv_frame(min(0.5, max(next_poll - time.monotonic(), 0.0)))
            if frame is not None:
                if companion.is_push(frame[0]):
                    self._on_push(frame)
                else:
                    self.stats["stray_frames"] += 1
                    log.debug("ignoring unsolicited frame %#04x", frame[0])
            if self._msg_waiting or time.monotonic() >= next_poll:
                self._drain(link)
                next_poll = time.monotonic() + self._cfg.companion.poll_interval

    def _start(self, link: CompanionLink) -> ConnectionInfo:
        cc = self._cfg.companion
        self_info = companion.parse_self_info(link.request(companion.app_start(), [companion.RESP_SELF_INFO]))
        device = companion.parse_device_info(link.request(companion.device_query(), [companion.RESP_DEVICE_INFO]))
        log.info(
            "companion %r (%s) firmware %s v%d, %d channel slots, radio %s",
            self_info.name,
            device.model,
            device.version,
            device.fw_ver,
            device.max_channels,
            _describe_radio(self_info.freq_khz, self_info.bw_hz, self_info.sf, self_info.cr),
        )
        if cc.channel_index >= device.max_channels:
            raise ConfigError(
                f"companion.channel_index {cc.channel_index} is out of range, the companion has {device.max_channels} slots"
            )
        self._ensure_radio(link, self_info)
        self._ensure_channel(link)
        return ConnectionInfo(self_info, device)

    def _ensure_radio(self, link: CompanionLink, info: SelfInfo) -> None:
        radio = self._cfg.radio
        if radio_matches(info, radio):
            return
        want = _describe_radio(radio.freq_khz, radio.bw_hz, radio.sf, radio.cr)
        have = _describe_radio(info.freq_khz, info.bw_hz, info.sf, info.cr)
        if self._cfg.companion.manage_radio:
            log.info("setting companion radio to %s (was %s)", want, have)
            link.request(companion.set_radio_params(radio.freq_khz, radio.bw_hz, radio.sf, radio.cr), [companion.RESP_OK])
        else:
            log.warning("companion radio is %s but the mesh uses %s; set companion.manage_radio to fix it", have, want)

    def _ensure_channel(self, link: CompanionLink) -> None:
        cc = self._cfg.companion
        key = self._cfg.channel_key
        current = None
        try:
            current = companion.parse_channel_info(
                link.request(companion.get_channel(cc.channel_index), [companion.RESP_CHANNEL_INFO])
            )
        except CommandError as e:
            log.info("channel slot %d is not set (%s)", cc.channel_index, e)
        if current is not None and current.name == cc.channel_name and current.secret == key:
            log.info("channel %d %r already provisioned", cc.channel_index, cc.channel_name)
            return
        if current is not None and current.name:
            log.warning("overwriting channel slot %d (was %r)", cc.channel_index, current.name)
        link.request(companion.set_channel(cc.channel_index, cc.channel_name, key), [companion.RESP_OK])
        log.info("provisioned channel %d %r", cc.channel_index, cc.channel_name)

    # --- draining -------------------------------------------------------------------------------------------------

    def _drain(self, link: CompanionLink) -> None:
        self._msg_waiting = False
        for _ in range(DRAIN_LIMIT):
            if self._stop.is_set():
                return
            frame = link.request(companion.sync_next_message(), companion.MESSAGE_RESPONSES | {companion.RESP_NO_MORE_MESSAGES})
            if frame[0] == companion.RESP_NO_MORE_MESSAGES:
                return
            self._handle_message(frame)
        self._msg_waiting = True  # more may be queued, come straight back

    def _handle_message(self, frame: bytes) -> None:
        if frame[0] != companion.RESP_CHANNEL_DATA_RECV:
            self._drop("other_message", f"code {frame[0]}")
            return
        try:
            data = companion.parse_channel_data(frame)
        except companion.ProtocolError as e:
            self._drop("bad_frame", f"{e}: {frame.hex()}")
            return
        if data.channel_index != self._cfg.companion.channel_index:
            self._drop("other_channel", f"channel {data.channel_index}")
            return
        if data.data_type != wire.REPORT_DATA_TYPE:
            self._drop("other_data_type", f"data_type {data.data_type:#06x}")
            return
        try:
            report = wire.decode_report(data.payload)
        except wire.WireError as e:
            self._drop("bad_report", f"{e}: {data.payload.hex()}")
            return
        self.stats["reports"] += 1
        self._handler.on_report(
            ReceivedReport(
                report=report,
                companion_snr_x4=data.snr_x4,
                path_len=data.path_len,
                rx_wall=time.time(),
                rx_mono=time.monotonic(),
                late=self._first_drain,
                payload=data.payload,
            )
        )

    def _drop(self, reason: str, detail: str) -> None:
        self.stats[f"dropped_{reason}"] += 1
        log.debug("dropped frame (%s): %s", reason, detail)
        self._handler.on_drop(reason, detail)
