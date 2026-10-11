"""An in-process watchdog: if the main loop stops making progress, the process exits so its supervisor restarts it.

A crash is restarted by Docker (``restart: unless-stopped``) or systemd (``Restart=always``) on its own; a hang is not, so ``beacon-ingest``
feeds this from its session loop and the thread exits the process with a status that is easy to spot if nothing has fed it for the timeout.
It is off unless asked for (``service.watchdog_s`` or ``$BEACON_WATCHDOG_S``), so a terminal run or a debugger is never killed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

EXIT_STALLED = 70  # EX_SOFTWARE


def _exit_process() -> None:
    logging.shutdown()  # flush the log lines that say why
    os._exit(EXIT_STALLED)


class Watchdog:
    def __init__(
        self,
        timeout: float,
        on_stall: Callable[[], None] = _exit_process,
        check_every: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if timeout <= 0:
            raise ValueError("watchdog timeout must be positive")
        self.timeout = timeout
        self._on_stall = on_stall
        self._check_every = check_every
        self._clock = clock
        self._last = clock()
        self._what = "starting"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = False

    def feed(self, what: str = "loop") -> None:
        """Say the loop is alive; ``what`` is what it was doing, for the log line if it later stalls."""
        self._last = self._clock()
        self._what = what

    def start(self) -> None:
        self.feed("starting")
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self._check_every):
            idle = self._clock() - self._last
            if idle > self.timeout:
                self.fired = True
                log.critical(
                    "no progress for %.0f s (limit %.0f s; last seen: %s); exiting so the supervisor restarts beacon-ingest",
                    idle, self.timeout, self._what,
                )
                self._on_stall()
                return
