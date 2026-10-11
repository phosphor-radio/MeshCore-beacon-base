"""The beacon-web HTTP application: a read API over the store, the map page, tiles, and the one operator action (reset).

Thin on purpose: the data comes from ``views``. Each request opens its own connection to the database (cheap, and a connection
must not be shared between the threads FastAPI runs requests on); beacon-web never needs ingest to be running. See
docs/plan/web-map.md.
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
import time
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import views
from .config import Config
from .store import Store, StoreError
from .tiles import MBTiles, open_tiles

log = logging.getLogger(__name__)

STATIC_DIR = Path(str(resources.files("beacon_base") / "static"))
NO_CACHE = {"Cache-Control": "no-cache"}


def content_security_policy(online_tiles: bool) -> str:
    """The page loads nothing from any other host; with the development option of online tiles it may load images from anywhere."""
    images = "'self' data: https:" if online_tiles else "'self' data:"
    return f"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src {images}; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noopener">OpenStreetMap</a> contributors'
# A referrer policy that still sends the Referer cross-origin: tile.openstreetmap.org blocks requests without one (its tile usage policy)
ONLINE_REFERRER_POLICY = "strict-origin-when-cross-origin"


def online_attribution(cfg: Config) -> str:
    """What the map credits when it loads tiles from the internet: the configured text, else OpenStreetMap's for its own servers."""
    if cfg.web.online_tile_attribution:
        return cfg.web.online_tile_attribution
    host = urlsplit(cfg.web.online_tile_url.replace("{", "").replace("}", "")).hostname or ""
    return OSM_ATTRIBUTION if host == "openstreetmap.org" or host.endswith(".openstreetmap.org") else ""


def create_app(cfg: Config, tiles: MBTiles | None = None, tiles_problem: str | None = None) -> FastAPI:
    """The application for a configuration. `tiles` is the opened tile file (see tiles.open_tiles), None for the plain grid."""
    app = FastAPI(title="beacon-web", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    online = bool(cfg.web.online_tile_url) and tiles is None
    policy = content_security_policy(online)
    # No referrer is sent from a map that loads nothing from other hosts; online tiles need one (see ONLINE_REFERRER_POLICY)
    referrer_policy = ONLINE_REFERRER_POLICY if online else "no-referrer"

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", policy)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", referrer_policy)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")  # revalidate (cheap, by ETag), so an upgrade is seen at once
        return response

    @app.exception_handler(views.NotFound)
    async def not_found(_: Request, e: views.NotFound):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(StoreError)
    async def store_error(_: Request, e: StoreError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(sqlite3.OperationalError)
    async def busy(_: Request, e: sqlite3.OperationalError):
        log.warning("database error: %s", e)
        return JSONResponse({"detail": "the database is busy; try again"}, status_code=503)

    def get_store() -> Iterator[Store]:
        store = Store.open(cfg.db_path)
        try:
            yield store
        finally:
            store.close()

    def require_operator(authorization: str | None = Header(default=None)) -> None:
        token = cfg.web_token
        if token is None:
            raise HTTPException(403, "operator actions are off: no web token is configured (beaconctl web token generate)")
        given = authorization[7:].strip() if authorization and authorization[:7].lower() == "bearer " else ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "wrong or missing operator token", headers={"WWW-Authenticate": "Bearer"})

    # --- read API -------------------------------------------------------------------------------------------------------

    @app.get("/api/health")
    def api_health(store: Store = Depends(get_store)):
        return views.health_view(store, cfg, time.time())

    @app.get("/api/state")
    def api_state(store: Store = Depends(get_store)):
        return views.state_view(store, cfg, time.time(), operator=cfg.web_token is not None)

    @app.get("/api/beacons")
    def api_beacons(store: Store = Depends(get_store)):
        now = time.time()
        return {"now": now, "beacons": views.beacons_view(store, cfg, now)}

    @app.get("/api/beacons/{prefix}")
    def api_beacon(prefix: str, limit: int = Query(50, ge=1, le=500), store: Store = Depends(get_store)):
        return views.beacon_view(store, cfg, prefix, time.time(), limit)

    @app.get("/api/beacons/{prefix}/track")
    def api_track(
        prefix: str, hours: float = Query(6.0, gt=0, le=24 * 31), limit: int = Query(200, ge=1, le=2000), store: Store = Depends(get_store)
    ):
        return views.track_view(store, prefix, time.time(), hours, limit)

    @app.get("/api/transmissions/{transmission_id}")
    def api_transmission(transmission_id: int, store: Store = Depends(get_store)):
        return views.transmission_view(store, transmission_id, time.time())

    @app.get("/api/repeaters")
    def api_repeaters(store: Store = Depends(get_store)):
        now = time.time()
        return {"now": now, "repeaters": views.repeaters_view(store, now)}

    @app.get("/api/rejects")
    def api_rejects(beacon: str | None = None, limit: int = Query(50, ge=1, le=500), store: Store = Depends(get_store)):
        return views.rejects_view(store, time.time(), beacon, limit)

    # --- operator action ------------------------------------------------------------------------------------------------

    @app.post("/api/beacons/{prefix}/reset", dependencies=[Depends(require_operator)])
    def api_reset(prefix: str, store: Store = Depends(get_store)):
        result = views.reset_beacon(store, prefix)
        log.info("operator reset beacon %s (was high-water mark %s)", result["label"], result["old_hwm"])
        return result

    # --- map ------------------------------------------------------------------------------------------------------------

    @app.get("/api/map")
    def api_map():
        """How the page should draw its background: local tiles, a development URL, or the plain grid."""
        if tiles is not None:
            info = tiles.info
            return {
                "mode": "mbtiles", "url": "tiles/{z}/{x}/{y}", "min_zoom": info.min_zoom, "max_zoom": info.max_zoom,
                "bounds": info.bounds, "attribution": info.attribution or "", "name": info.name,
            }
        if cfg.web.online_tile_url:
            return {"mode": "online", "url": cfg.web.online_tile_url, "min_zoom": 0, "max_zoom": 19, "bounds": None,
                    "attribution": online_attribution(cfg), "referrer_policy": ONLINE_REFERRER_POLICY, "problem": tiles_problem}
        return {"mode": "grid", "url": None, "min_zoom": 0, "max_zoom": 22, "bounds": None, "attribution": "", "problem": tiles_problem}

    @app.get("/tiles/{z}/{x}/{y}")
    def tile(z: int, x: int, y: int):
        found = tiles.tile(z, x, y) if tiles is not None else None
        if found is None:
            return Response(status_code=404)
        data, mime = found
        return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})

    # --- the page -------------------------------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def app_from_config(cfg: Config) -> FastAPI:
    tiles, problem = open_tiles(cfg.tiles_path)
    return create_app(cfg, tiles, problem)
