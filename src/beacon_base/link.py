"""Serial framing and request/response handling for the companion link.

Framing (``src/helpers/ArduinoSerialInterface.cpp`` in the firmware repository): host to device is ``'<'`` + 2-byte
little-endian length + payload, device to host is ``'>'`` + length + payload. Frames are at most MAX_FRAME_SIZE bytes.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Iterable, Protocol

from . import companion

log = logging.getLogger(__name__)

MAX_FRAME_SIZE = 176
PARTIAL_FRAME_TIMEOUT = 2.0  # seconds a half-received frame may sit before the decoder gives up on it
READ_SLICE = 0.2


class CompanionError(Exception):
    """Base class for everything that makes the session drop the connection and reconnect."""


class LinkError(CompanionError):
    """The port failed or the companion stopped answering."""


class NoReply(LinkError):
    """The companion did not answer a command in time."""


class CommandError(CompanionError):
    """The companion answered a command with an error."""

    def __init__(self, code: int | None):
        super().__init__(companion.describe_error(code))
        self.code = code


def encode_frame(payload: bytes) -> bytes:
    """Frame a host to device command."""
    if not 0 < len(payload) <= MAX_FRAME_SIZE:
        raise ValueError(f"frame payload must be 1 to {MAX_FRAME_SIZE} bytes, got {len(payload)}")
    return b"<" + len(payload).to_bytes(2, "little") + payload


class FrameDecoder:
    """Extracts device to host frames from a byte stream and resynchronises after garbage.

    A candidate frame starts at ``'>'`` and is accepted only if its length is in range and its first payload byte is a
    code the companion can send. Anything else drops one byte and rescans, so debug text or a torn frame costs only
    the bytes that were actually damaged.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.discarded = 0  # bytes thrown away while resynchronising

    @property
    def pending(self) -> bool:
        return bool(self._buf)

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        frames: list[bytes] = []
        buf = self._buf
        while buf:
            start = buf.find(b">")
            if start < 0:
                self.discarded += len(buf)
                buf.clear()
                break
            if start > 0:
                self.discarded += start
                del buf[:start]
            if len(buf) < 4:
                break  # '>' + length + code byte not complete yet
            length = buf[1] | (buf[2] << 8)
            if not 0 < length <= MAX_FRAME_SIZE or not companion.plausible_code(buf[3]):
                self.discarded += 1
                del buf[0]
                continue
            if len(buf) < 3 + length:
                break
            frames.append(bytes(buf[3 : 3 + length]))
            del buf[: 3 + length]
        return frames

    def abandon_partial(self) -> list[bytes]:
        """Give up on the frame being assembled (it stalled) and rescan what follows its first byte."""
        if not self._buf:
            return []
        self.discarded += 1
        del self._buf[0]
        return self.feed(b"")


class Transport(Protocol):
    def read(self, timeout: float) -> bytes:
        """Wait up to timeout seconds for data. Returns b"" on timeout, raises LinkError if the port failed."""

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


ESPRESSIF_VID = 0x303A  # ESP32-S3 native USB (the XIAO ESP32-S3 companion)
DTR_MODES = ("auto", "on", "off")


def usb_vendor_id(path: str) -> int | None:
    """The USB vendor id of the device behind a serial port path (a /dev/serial/by-id link is followed), or None if it is not
    a USB serial device or cannot be found. Isolated so tests can replace it."""
    try:
        from serial.tools import list_ports

        real = os.path.realpath(path)
        for info in list_ports.comports():
            if os.path.realpath(info.device) == real:
                return info.vid
    except Exception as e:  # port listing is best effort; an unknown device just gets the default
        log.debug("could not look up the USB vendor of %s: %s", path, e)
    return None


def choose_dtr(path: str, mode: str = "auto") -> tuple[bool, str]:
    """Whether to hold DTR high when opening the companion's port, and why.

    "on" and "off" are taken as given. "auto" keeps DTR low for Espressif's native USB (VID 0x303A), where asserting DTR/RTS
    can reset the ESP32-S3, and holds it high for every other or unknown device: an nRF52 running Adafruit TinyUSB only sends
    while the host asserts DTR, so with DTR low every reply is silently dropped. RTS is never raised."""
    if mode == "on":
        return True, "companion.dtr = on"
    if mode == "off":
        return False, "companion.dtr = off"
    vid = usb_vendor_id(path)
    if vid == ESPRESSIF_VID:
        return False, f"auto: Espressif USB (vendor {vid:#06x}), whose native USB can reset on DTR"
    if vid is None:
        return True, "auto: unknown device"
    return True, f"auto: USB vendor {vid:#06x}"


class SerialTransport:
    """pyserial transport. Raises LinkError for any port failure so callers handle one exception type."""

    def __init__(self, path: str, baud: int = 115200, dtr: bool = False):
        import termios

        import serial

        # termios.error is not an OSError; a port that vanishes mid-write raises it from flush()
        self._errors = (OSError, serial.SerialException, termios.error)
        port = serial.Serial()
        port.port = path
        port.baudrate = baud
        port.timeout = READ_SLICE
        port.write_timeout = 5.0
        # The ESP32-S3 uses native USB, where toggling DTR/RTS on open can reset the board, so for it DTR and RTS stay low.
        # They must be set before open() so the lines are never asserted. An nRF52 (Adafruit TinyUSB) only transmits while
        # DTR is asserted, so for it DTR is raised (see choose_dtr). RTS is never raised.
        port.dtr = dtr
        port.rts = False
        port.exclusive = True  # only one process may talk to the companion
        try:
            port.open()
        except self._errors as e:
            raise LinkError(f"cannot open {path}: {e}") from e
        self._port = port

    def read(self, timeout: float) -> bytes:
        try:
            self._port.timeout = timeout
            data = self._port.read(1)
            if data:
                waiting = self._port.in_waiting
                if waiting:
                    data += self._port.read(waiting)
            return data
        except self._errors as e:
            raise LinkError(f"serial read failed: {e}") from e

    def write(self, data: bytes) -> None:
        try:
            self._port.write(data)
            self._port.flush()
        except self._errors as e:
            raise LinkError(f"serial write failed: {e}") from e

    def close(self) -> None:
        try:
            self._port.close()
        except self._errors:
            pass


class CompanionLink:
    """Sends commands and receives frames over a Transport. One thread uses it at a time."""

    def __init__(self, transport: Transport, command_timeout: float = 5.0):
        self._transport = transport
        self._decoder = FrameDecoder()
        self._ready: list[bytes] = []
        self._partial_since: float | None = None
        self.command_timeout = command_timeout
        self.on_push: Callable[[bytes], None] = lambda frame: None
        self.stray_frames = 0

    @property
    def discarded_bytes(self) -> int:
        return self._decoder.discarded

    def close(self) -> None:
        self._transport.close()

    def send(self, payload: bytes) -> None:
        self._transport.write(encode_frame(payload))

    def recv_frame(self, timeout: float) -> bytes | None:
        """Return the next frame, or None if none arrived within timeout seconds."""
        deadline = time.monotonic() + timeout
        while True:
            if self._ready:
                return self._ready.pop(0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            data = self._transport.read(min(remaining, READ_SLICE))
            if data:
                self._ready.extend(self._decoder.feed(data))
            if self._decoder.pending:
                now = time.monotonic()
                if self._partial_since is None or data:
                    self._partial_since = now
                elif now - self._partial_since > PARTIAL_FRAME_TIMEOUT:
                    log.warning("dropping a stalled partial frame")
                    self._ready.extend(self._decoder.abandon_partial())
                    self._partial_since = None
            else:
                self._partial_since = None

    def request(self, payload: bytes, expect: Iterable[int], timeout: float | None = None) -> bytes:
        """Send a command and wait for its reply.

        Pushes that arrive in the meantime go to on_push. A RESP_ERR reply raises CommandError, no reply in time raises
        LinkError, and replies of other types are counted and skipped.
        """
        expected = frozenset(expect)
        wait = self.command_timeout if timeout is None else timeout
        self.send(payload)
        deadline = time.monotonic() + wait
        while True:
            frame = self.recv_frame(max(deadline - time.monotonic(), 0.0))
            if frame is None:
                raise NoReply(f"no reply to command {payload[0]} within {wait:.0f}s")
            code = frame[0]
            if companion.is_push(code):
                self.on_push(frame)
            elif code == companion.RESP_ERR:
                raise CommandError(companion.error_code(frame))
            elif code in expected:
                return frame
            else:
                self.stray_frames += 1
                log.warning("skipping unexpected reply %#04x to command %d", code, payload[0])

    def request_until(self, payload: bytes, end_code: int, timeout: float | None = None) -> list[bytes]:
        """Send a command whose reply is a stream of frames (a contact list) and collect them up to end_code.

        timeout applies to each frame, not the whole stream. Pushes go to on_push, a RESP_ERR reply raises CommandError.
        The end frame is not included in the result.
        """
        wait = self.command_timeout if timeout is None else timeout
        self.send(payload)
        frames: list[bytes] = []
        while True:
            frame = self.recv_frame(wait)
            if frame is None:
                raise LinkError(f"reply to command {payload[0]} stalled for {wait:.0f}s after {len(frames)} frames")
            code = frame[0]
            if companion.is_push(code):
                self.on_push(frame)
            elif code == companion.RESP_ERR:
                raise CommandError(companion.error_code(frame))
            elif code == end_code:
                return frames
            else:
                frames.append(frame)
