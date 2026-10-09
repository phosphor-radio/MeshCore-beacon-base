"""A fake companion radio on a pseudo-terminal, for tests and desk development.

Speaks enough of the companion protocol for ``CompanionSession``: APP_START, DEVICE_QUERY, GET/SET_CHANNEL,
SET_RADIO_PARAMS and SYNC_NEXT_MESSAGE, with an offline queue and MSG_WAITING pushes like the real firmware
(``examples/companion_radio/MyMesh.cpp``). Test hooks inject reports, garbage and disconnects.
"""

from __future__ import annotations

import errno
import os
import pty
import select
import struct
import tempfile
import threading
import tty
from collections import deque
from pathlib import Path

from . import companion, wire
from .link import MAX_FRAME_SIZE

OFFLINE_QUEUE_SIZE = 256


class FakeCompanion:
    def __init__(
        self,
        max_channels: int = 40,
        radio: tuple[int, int, int, int] = (905775, 62500, 8, 6),
        public_key: bytes = bytes(range(0x80, 0xA0)),
        fw_ver: int = 10,
    ):
        self.max_channels = max_channels
        self.radio = radio
        self.public_key = public_key
        self.fw_ver = fw_ver
        self.channels: dict[int, tuple[str, bytes]] = {0: ("Public", bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72"))}
        self.queue: deque[bytes] = deque()
        self.commands: list[bytes] = []  # every command frame received, for assertions
        self.send_push_on_enqueue = True
        self._lock = threading.RLock()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fake-companion-")
        self.path = str(Path(self._tmpdir.name) / "ttyFAKE")  # stable symlink, the pty behind it changes on reconnect
        self._master = self._slave = -1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._open_pty()

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    def _open_pty(self) -> None:
        master, slave = pty.openpty()
        tty.setraw(slave)  # no echo or line editing, or the framing would be mangled
        with self._lock:
            self._master, self._slave = master, slave
        link = Path(self.path)
        tmp = link.with_name("ttyFAKE.new")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(os.ttyname(slave))
        os.replace(tmp, link)
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, args=(master,), daemon=True, name="fake-companion")
        self._thread.start()

    def _close_pty(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            for fd in (self._master, self._slave):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._master = self._slave = -1

    def disconnect(self) -> None:
        """Unplug the USB cable: the port goes away and reads on the host fail."""
        self._close_pty()

    def reconnect(self, reboot: bool = False) -> None:
        """Plug it back in. A reboot also loses the RAM state (offline queue) but keeps flash state (channels, radio)."""
        if self._master >= 0:
            self._close_pty()
        if reboot:
            with self._lock:
                self.queue.clear()
        self._open_pty()

    def close(self) -> None:
        self._close_pty()
        self._tmpdir.cleanup()

    def __enter__(self) -> "FakeCompanion":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- test hooks -------------------------------------------------------------------------------------------------

    def enqueue_report(
        self,
        report_payload: bytes,
        channel_index: int = 1,
        data_type: int = wire.REPORT_DATA_TYPE,
        snr_x4: int = 20,
        path_len: int = 1,
    ) -> None:
        """Queue a received GRP_DATA packet, as if a repeater's report had arrived over the air."""
        self.enqueue_frame(companion.build_channel_data(snr_x4, channel_index, path_len, data_type, report_payload))

    def enqueue_frame(self, frame: bytes) -> None:
        with self._lock:
            if len(self.queue) >= OFFLINE_QUEUE_SIZE:
                self.queue.popleft()  # the firmware drops the oldest channel message
            self.queue.append(frame)
        if self.send_push_on_enqueue:
            self.write_frame(bytes([companion.PUSH_MSG_WAITING]))

    def write_frame(self, payload: bytes) -> None:
        self.write_raw(b">" + len(payload).to_bytes(2, "little") + payload)

    def write_raw(self, data: bytes) -> None:
        """Write bytes to the host as they are, for garbage and torn frames."""
        with self._lock:
            if self._master < 0:
                return
            try:
                os.write(self._master, data)
            except OSError:
                pass

    # --- the device ---------------------------------------------------------------------------------------------------

    def _serve(self, master: int) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([master], [], [], 0.05)
                if not ready:
                    continue
                data = os.read(master, 4096)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    return
                raise
            buf += data
            while True:
                start = buf.find(b"<")
                if start < 0:
                    buf.clear()
                    break
                del buf[:start]
                if len(buf) < 3:
                    break
                length = buf[1] | (buf[2] << 8)
                if length == 0 or length > MAX_FRAME_SIZE:
                    del buf[0]
                    continue
                if len(buf) < 3 + length:
                    break
                cmd = bytes(buf[3 : 3 + length])
                del buf[: 3 + length]
                with self._lock:
                    self.commands.append(cmd)
                    self._handle(cmd)

    def _ok(self) -> None:
        self.write_frame(bytes([companion.RESP_OK]))

    def _err(self, code: int) -> None:
        self.write_frame(bytes([companion.RESP_ERR, code]))

    def _handle(self, cmd: bytes) -> None:
        op = cmd[0]
        if op == companion.CMD_APP_START and len(cmd) >= 8:
            freq, bw, sf, cr = self.radio
            frame = bytes([companion.RESP_SELF_INFO, 1, 22, 22]) + self.public_key
            frame += struct.pack("<iiBBBB", 0, 0, 0, 0, 0, 0)
            frame += struct.pack("<IIBB", freq, bw, sf, cr) + b"fake-companion"
            self.write_frame(frame)
        elif op == companion.CMD_DEVICE_QUERY and len(cmd) >= 2:
            frame = bytes([companion.RESP_DEVICE_INFO, self.fw_ver, 50, self.max_channels]) + bytes(4)
            frame += b"08 Oct 2026".ljust(12, b"\0") + b"Fake Companion".ljust(40, b"\0") + b"v1.fake".ljust(20, b"\0")
            frame += bytes([0, 0])
            self.write_frame(frame)
        elif op == companion.CMD_GET_CHANNEL and len(cmd) >= 2:
            entry = self.channels.get(cmd[1]) if cmd[1] < self.max_channels else None
            if entry is None:
                self._err(2)
            else:
                name, secret = entry
                self.write_frame(bytes([companion.RESP_CHANNEL_INFO, cmd[1]]) + name.encode().ljust(32, b"\0") + secret)
        elif op == companion.CMD_SET_CHANNEL and len(cmd) >= 2 + 32 + 16:
            if cmd[1] >= self.max_channels:
                self._err(2)
            else:
                name = cmd[2:34].split(b"\0", 1)[0].decode()
                self.channels[cmd[1]] = (name, bytes(cmd[34:50]))
                self._ok()
        elif op == companion.CMD_SET_RADIO_PARAMS and len(cmd) >= 11:
            self.radio = struct.unpack_from("<IIBB", cmd, 1)
            self._ok()
        elif op == companion.CMD_SYNC_NEXT_MESSAGE:
            if self.queue:
                self.write_frame(self.queue.popleft())
            else:
                self.write_frame(bytes([companion.RESP_NO_MORE_MESSAGES]))
        else:
            self._err(1)
