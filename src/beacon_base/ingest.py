"""Companion session: connect, provision, drain and decode beacon reports.

``CompanionSession`` owns the serial port. It reconnects on any failure, sets up the report channel, drains the
companion's offline queue and then waits for new messages. Decoded reports go to a ``Handler``; the replay/dedupe
pipeline (phase B2) is a Handler.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable

from . import companion, wire
from .companion import DeviceInfo, SelfInfo
from .config import Config, ConfigError, RadioConfig
from .link import CompanionError, CompanionLink, CommandError, NoReply, SerialTransport, Transport, choose_dtr

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
class RawFrame:
    """A frame on the report channel that did not decode, kept for the audit trail."""

    payload: bytes
    companion_snr_x4: int
    path_len: int
    rx_wall: float
    rx_mono: float
    late: bool


@dataclass(frozen=True)
class ReceivedReport:
    report: wire.Report
    companion_snr_x4: int  # SNR at the companion, not at the repeater
    path_len: int  # 0xFF when the packet reached the companion by a direct route
    rx_wall: float  # Pi clock, seconds since the epoch (may be wrong until the clock is set)
    rx_mono: float  # monotonic clock, seconds
    late: bool  # drained from the companion's queue right after connecting, so possibly delayed
    payload: bytes  # the undecoded report, kept for the audit trail


@dataclass(frozen=True)
class HeardRepeater:
    """A repeater advert as the base companion reports it: the key, name and position the repeater advertises."""

    public_key: bytes
    name: str
    lat: float | None  # 0.0, 0.0 means the repeater has not been told where it is
    lon: float | None
    advert_timestamp: int
    heard_at: float | None  # Pi clock; None when it comes from the companion's contact list rather than a live advert


@dataclass(frozen=True)
class ReceivedNames:
    """A name announcement: what a repeater says its heard beacons call themselves."""

    announcement: wire.NameAnnouncement
    companion_snr_x4: int
    path_len: int
    rx_wall: float
    rx_mono: float
    late: bool
    payload: bytes


class Handler:
    """Receives session events. Override what you need; every method may be left alone."""

    def on_connected(self, info: ConnectionInfo) -> None: ...

    def on_synced(self) -> None:
        """The companion's offline queue has been drained after connecting; reports from here on are live."""

    def on_report(self, rx: ReceivedReport) -> None: ...

    def on_repeater_advert(self, advert: HeardRepeater) -> None:
        """The base companion heard a repeater advertise, or listed one among its contacts."""

    def on_names(self, rx: ReceivedNames) -> None:
        """A repeater announced the names of beacons it hears."""

    def on_drop(self, reason: str, detail: str, raw: RawFrame | None = None) -> None:
        """A frame that is not a usable beacon report. reason is one of CompanionSession.DROP_REASONS. raw is set for
        ``bad_report`` or ``bad_names``, a frame on the report channel that failed to decode."""

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


class _ReopenWithOtherDtr(Exception):
    """The first APP_START got no reply; reconnect at once with the opposite DTR setting."""


class CompanionSession:
    DROP_REASONS = ("other_message", "other_channel", "other_data_type", "bad_frame", "bad_report", "bad_names")

    def __init__(
        self,
        config: Config,
        handler: Handler,
        transport_factory: Callable[[str, bool], Transport] | None = None,
    ):
        """transport_factory(path, dtr) opens the port with DTR held as given; the default opens a real serial port."""
        if config.channel_key is None:
            raise ConfigError("no report channel key; run 'beaconctl channel generate' first")
        if not config.companion.port:
            raise ConfigError("no companion serial port; set companion.port or pass --port")
        self._cfg = config
        self._handler = handler
        self._transport_factory = transport_factory or (lambda path, dtr: SerialTransport(path, config.companion.baud, dtr))
        # DTR handling (companion.dtr = auto): the value that got an APP_START reply is kept for later reconnects, and while
        # nothing has answered yet a single fallback to the opposite setting is tried per connection attempt.
        self._dtr_confirmed: bool | None = None
        self._dtr_fallback: bool | None = None
        self._dtr_in_use = False
        self.stats: Counter[str] = Counter()
        self._msg_waiting = False
        self._first_drain = True
        self._stop = threading.Event()
        self._contacts: dict[bytes, companion.Contact] = {}  # every node the companion has told us about, this connection
        self._advert_pushes: deque[bytes] = deque(maxlen=1000)

    # --- connection loop -------------------------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Serve until stop is set. Connection failures are logged and retried with backoff."""
        self._stop = stop
        delay = RECONNECT_MIN
        while not stop.is_set():
            connected_at = None
            error: str | None = None
            link = None
            reopen = False
            try:
                dtr = self._pick_dtr()
                link = CompanionLink(self._transport_factory(self._cfg.companion.port, dtr), self._cfg.companion.command_timeout)
                link.on_push = self._on_push
                connected_at = time.monotonic()
                self._serve(link, stop)
            except ConfigError:
                raise
            except _ReopenWithOtherDtr:
                reopen = True
                connected_at = None  # never got as far as on_connected
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
            if reopen:
                continue  # straight away, no backoff
            if connected_at is not None and time.monotonic() - connected_at > 10 * RECONNECT_MAX:
                delay = RECONNECT_MIN  # it ran for a while, so this is a new failure
            log.info("reconnecting in %.0fs", delay)
            stop.wait(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    def _pick_dtr(self) -> bool:
        """The DTR level to open the port with this time, and why (logged)."""
        mode = self._cfg.companion.dtr
        if mode == "auto" and self._dtr_confirmed is not None:
            dtr, why = self._dtr_confirmed, "auto: the setting that worked earlier"
        elif mode == "auto" and self._dtr_fallback is not None:
            dtr, why = self._dtr_fallback, "auto: fallback, the other way from the first attempt"
        else:
            dtr, why = choose_dtr(self._cfg.companion.port, mode)
        self._dtr_in_use = dtr
        log.info("opening %s with DTR %s (%s)", self._cfg.companion.port, "high" if dtr else "low", why)
        return dtr

    def _app_start(self, link: CompanionLink) -> bytes:
        """The first command on a connection. With companion.dtr = auto, no reply to it means the DTR guess may be wrong (an
        nRF52 stays silent with DTR low), so try once with the opposite setting and remember whichever works."""
        auto = self._cfg.companion.dtr == "auto"
        try:
            reply = link.request(companion.app_start(), [companion.RESP_SELF_INFO])
        except NoReply:
            level = "high" if self._dtr_in_use else "low"
            if auto and self._dtr_confirmed is None and self._dtr_fallback is None:
                self._dtr_fallback = not self._dtr_in_use
                log.info(
                    "no reply to APP_START with DTR %s; reopening with DTR %s (companion.dtr = auto)",
                    level, "low" if self._dtr_in_use else "high",
                )
                raise _ReopenWithOtherDtr() from None
            if auto:
                self._dtr_fallback = None  # neither worked: the next connection attempt starts over
                log.warning("no reply to APP_START with DTR high or low")
            else:
                log.warning(
                    "no reply to APP_START with DTR %s; an nRF52 companion needs companion.dtr = on, an ESP32-S3 off "
                    "(docs/operations.md)", level,
                )
            raise
        if auto and self._dtr_confirmed is None:
            if self._dtr_fallback is not None:
                log.info("the companion answers with DTR %s; keeping it", "high" if self._dtr_in_use else "low")
            self._dtr_confirmed, self._dtr_fallback = self._dtr_in_use, None
        return reply

    def _on_push(self, frame: bytes) -> None:
        if frame[0] == companion.PUSH_MSG_WAITING:
            self._msg_waiting = True
        elif frame[0] in (companion.PUSH_NEW_ADVERT, companion.PUSH_ADVERT) and self._cfg.companion.learn_repeaters:
            # handled in the main loop: it needs commands of its own, which cannot nest inside another request
            self._advert_pushes.append(frame)
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
        self._contacts.clear()
        self._advert_pushes.clear()
        if self._cfg.companion.learn_repeaters:
            self._sync_contacts(link)  # after the drain, so it never delays a report
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
            if self._advert_pushes:
                self._process_adverts(link)

    def _start(self, link: CompanionLink) -> ConnectionInfo:
        cc = self._cfg.companion
        self_info = companion.parse_self_info(self._app_start(link))
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
        self._ensure_manual_add(link, self_info)
        return ConnectionInfo(self_info, device)

    def _ensure_manual_add(self, link: CompanionLink, info: SelfInfo) -> None:
        """Run the base companion in manual-add mode so every advert reaches us as a full contact with its position, and its
        contact table does not fill with the mesh. Only written when it differs, since the companion stores it in flash."""
        cc = self._cfg.companion
        if not (cc.learn_repeaters and cc.manual_add_contacts) or info.manual_add_contacts & 1:
            return
        link.request(companion.set_manual_add_contacts(True), [companion.RESP_OK])
        log.info("set the companion to manual-add mode (companion.manual_add_contacts)")

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

    # --- repeater adverts ------------------------------------------------------------------------------------------

    def _sync_contacts(self, link: CompanionLink) -> None:
        """Read the companion's contact list once per connection. Best effort: it never stops reports."""
        try:
            frames = link.request_until(companion.get_contacts(), companion.RESP_END_OF_CONTACTS)
        except CommandError as e:
            log.warning("could not read the companion's contacts: %s", e)
            return
        for frame in frames:
            if frame[0] == companion.RESP_CONTACT:
                self._note_contact(frame, live=False)
        repeaters = sum(1 for c in self._contacts.values() if c.adv_type == companion.ADV_TYPE_REPEATER)
        log.info("companion knows %d nodes, %d of them repeaters", len(self._contacts), repeaters)

    def _note_contact(self, frame: bytes, live: bool) -> None:
        try:
            contact = companion.parse_contact(frame)
        except companion.ProtocolError as e:
            self.stats["bad_contact"] += 1
            log.debug("ignoring a contact frame: %s", e)
            return
        self._contacts[contact.public_key] = contact
        if contact.adv_type == companion.ADV_TYPE_REPEATER:
            self._handler.on_repeater_advert(
                HeardRepeater(
                    contact.public_key, contact.name, contact.lat, contact.lon, contact.advert_timestamp,
                    time.time() if live else None,
                )
            )

    def _process_adverts(self, link: CompanionLink) -> None:
        while self._advert_pushes:
            frame = self._advert_pushes.popleft()
            if frame[0] == companion.PUSH_NEW_ADVERT:
                self._note_contact(frame, live=True)
                continue
            try:
                key = companion.parse_bare_advert(frame)
            except companion.ProtocolError:
                self.stats["bad_contact"] += 1
                continue
            known = self._contacts.get(key)
            if known is not None and known.adv_type != companion.ADV_TYPE_REPEATER:
                continue  # a node we are not interested in
            try:  # a stored contact advertised again: the push has no position, so read the contact for the new one
                reply = link.request(companion.get_contact_by_key(key), [companion.RESP_CONTACT])
            except CommandError:
                self._contacts[key] = companion.Contact(key, 0, "", 0, None, None)  # remember not to ask again
                continue
            self._note_contact(reply, live=True)

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
        if data.data_type not in (wire.REPORT_DATA_TYPE, wire.NAMES_DATA_TYPE):
            self._drop("other_data_type", f"data_type {data.data_type:#06x}")
            return
        rx_wall, rx_mono = time.time(), time.monotonic()
        if data.data_type == wire.NAMES_DATA_TYPE:
            self._handle_names(data, rx_wall, rx_mono)
            return
        try:
            report = wire.decode_report(data.payload)
        except wire.WireError as e:
            raw = RawFrame(data.payload, data.snr_x4, data.path_len, rx_wall, rx_mono, self._first_drain)
            self._drop("bad_report", f"{e}: {data.payload.hex()}", raw)
            return
        self.stats["reports"] += 1
        self._handler.on_report(
            ReceivedReport(
                report=report,
                companion_snr_x4=data.snr_x4,
                path_len=data.path_len,
                rx_wall=rx_wall,
                rx_mono=rx_mono,
                late=self._first_drain,
                payload=data.payload,
            )
        )

    def _handle_names(self, data: companion.ChannelData, rx_wall: float, rx_mono: float) -> None:
        try:
            announcement = wire.decode_names(data.payload)
        except wire.WireError as e:
            raw = RawFrame(data.payload, data.snr_x4, data.path_len, rx_wall, rx_mono, self._first_drain)
            self._drop("bad_names", f"{e}: {data.payload.hex()}", raw)
            return
        self.stats["name_messages"] += 1
        self._handler.on_names(
            ReceivedNames(announcement, data.snr_x4, data.path_len, rx_wall, rx_mono, self._first_drain, data.payload)
        )

    def _drop(self, reason: str, detail: str, raw: RawFrame | None = None) -> None:
        self.stats[f"dropped_{reason}"] += 1
        log.debug("dropped frame (%s): %s", reason, detail)
        self._handler.on_drop(reason, detail, raw)
