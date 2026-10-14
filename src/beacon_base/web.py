"""beacon-web: the HTTP API and map page. A separate process from beacon-ingest; it only needs the database."""

from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import logging
import sys

from .config import ConfigError, load_config
from .runtime import setup_logging
from .store import StoreError

log = logging.getLogger("beacon-web")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="beacon-web", description="Serve the beacon map and its API from the database")
    p.add_argument("-c", "--config", help="config file (default: $BEACON_BASE_CONFIG or ~/.config/beacon-base/config.toml)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging, and a line per request")
    p.add_argument("--host", help="address to listen on (overrides web.host; the default is 127.0.0.1)")
    p.add_argument("--port", type=int, help="port to listen on (overrides web.port)")
    p.add_argument("--check", action="store_true", help="only check that the configuration can be read, then exit (the container's entry point waits on this)")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        import uvicorn

        from .api import app_from_config
    except ImportError as e:
        print(f"beacon-web: {e.name or e} is not installed; install the web extra:  pip install 'beacon-base[web]'", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
        changes = {k: v for k, v in (("host", args.host), ("port", args.port)) if v}
        if changes:
            cfg = dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, **changes))
        if args.check:  # nothing is opened: no database is created and no tiles file is read
            print(f"configuration ok: database {cfg.db_path}, serving {cfg.web.host}:{cfg.web.port}")
            return 0
        app = app_from_config(cfg)
    except (ConfigError, StoreError) as e:
        print(f"beacon-web: {e}", file=sys.stderr)
        return 2
    web = cfg.web
    if not _is_loopback(web.host):
        log.warning(
            "listening on %s:%d, which other machines can reach; the pages are served over plain HTTP, so %s",
            web.host, web.port,
            "the operator token travels in clear" if cfg.web_token else "anyone on the network can read the map (operator actions are off: no web token)",
        )
    if cfg.web_token is None:
        log.info("no web token: the map is read-only (beaconctl web token generate)")
    log.info("database %s; serving http://%s:%d/", cfg.db_path, web.host, web.port)
    try:
        uvicorn.run(app, host=web.host, port=web.port, log_config=None, access_log=args.verbose, server_header=False)
    except OSError as e:  # the port is taken, or the address is not ours
        print(f"beacon-web: cannot listen on {web.host}:{web.port}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
