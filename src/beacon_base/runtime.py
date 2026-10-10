"""Process plumbing shared by the entry points."""

from __future__ import annotations

import logging
import signal
import sys
import threading


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def stop_on_signals() -> threading.Event:
    """An event that is set by SIGINT or SIGTERM, so loops can finish what they are doing and exit."""
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop
