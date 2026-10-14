"""The beacon-web HTTP application: a read API over the store, the map page, tiles, and the one operator action (reset).

Plain Starlette (pure Python, so the Docker image can copy it to another CPU; docs/plan/web-docker.md). Thin on purpose: the data comes
from ``views``. Handlers are ordinary functions, which Starlette runs on a thread pool, and each opens its own connection to the database
(cheap, and a connection must not be shared between threads); beacon-web never needs ingest to be running. See docs/plan/web-map.md.
"""

from __future__ import annotations

import hmac
import logging
import math
import sqlite3
import time
from functools import wraps
from importlib import resources
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import views
from .config import Config
from .store import Store, StoreError
from .tiles import OSM_ATTRIBUTION, MBTiles, open_tiles

log = logging.getLogger(__name__)

STATIC_DIR = Path(str(resources.files("beacon_base") / "static"))
NO_CACHE = {"Cache-Control": "no-cache"}


def content_security_policy(online_tiles: bool) -> str:
    """The page loads nothing from any other host; with the development option of online tiles it may load images from anywhere."""
    images = "'self' data: https:" if online_tiles else "'self' data:"
    return f"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src {images}; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


# A referrer policy that still sends the Referer cross-origin: tile.openstreetmap.org blocks requests without one (its tile usage policy)
ONLINE_REFERRER_POLICY = "strict-origin-when-cross-origin"


def online_attribution(cfg: Config) -> str:
    """What the map credits when it loads tiles from the internet: the configured text, else OpenStreetMap's for its own servers."""
    if cfg.web.online_tile_attribution:
        return cfg.web.online_tile_attribution
    host = urlsplit(cfg.web.online_tile_url.replace("{", "").replace("}", "")).hostname or ""
    return OSM_ATTRIBUTION if host == "openstreetmap.org" or host.endswith(".openstreetmap.org") else ""


class SecurityHeaders:
    """Adds the page's security headers, and the cache rules for the API and the static files, to every response that lacks them.
    Plain ASGI rather than BaseHTTPMiddleware, which would run each request through an extra task and stream."""

    def __init__(self, app: ASGIApp, policy: str, referrer_policy: str):
        self.app = app
        self.fixed = {"Content-Security-Policy": policy, "X-Content-Type-Options": "nosniff", "Referrer-Policy": referrer_policy}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]

        async def add_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.fixed.items():
                    headers.setdefault(name, value)
                if path.startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
                elif path.startswith("/static/"):
                    headers.setdefault("Cache-Control", "no-cache")  # revalidate (cheap, by ETag), so an upgrade is seen at once
            await send(message)

        await self.app(scope, receive, add_headers)


def query_number(request: Request, name: str, default: float, kind: type = int, low: float | None = None, high: float | None = None,
                 low_inclusive: bool = True) -> float:
    """A numeric query value within bounds, or the default when it is not given. A bad one is a 422 with a message, as the API always said."""
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise HTTPException(422, f"{name} must be {'a whole number' if kind is int else 'a number'}") from None
    if kind is float and not math.isfinite(value):
        raise HTTPException(422, f"{name} must be a number")
    if low is not None and (value < low if low_inclusive else value <= low):
        raise HTTPException(422, f"{name} must be {'at least' if low_inclusive else 'more than'} {low:g}")
    if high is not None and value > high:
        raise HTTPException(422, f"{name} must be at most {high:g}")
    return value


def create_app(cfg: Config, tiles: MBTiles | None = None, tiles_problem: str | None = None) -> Starlette:
    """The application for a configuration. `tiles` is the opened tile file (see tiles.open_tiles), None for the plain grid."""
    online = bool(cfg.web.online_tile_url) and tiles is None
    policy = content_security_policy(online)
    # No referrer is sent from a map that loads nothing from other hosts; online tiles need one (see ONLINE_REFERRER_POLICY)
    referrer_policy = ONLINE_REFERRER_POLICY if online else "no-referrer"
    if tiles is not None and not (cfg.web.tiles_attribution or tiles.info.attribution):
        # Map data usually needs a credit (OpenStreetMap's licence, the ODbL, does): say so rather than show an uncredited map in silence
        log.warning("the tiles file has no attribution in its metadata, so the map shows no credit; set web.tiles_attribution if one is required")

    def reading(handler: Callable[[Request, Store], Response]) -> Callable[[Request], Response]:
        """A handler that gets its own connection to the database for the length of the request."""

        @wraps(handler)
        def wrapper(request: Request) -> Response:
            with Store.open(cfg.db_path) as store:
                return handler(request, store)

        return wrapper

    def require_operator(request: Request) -> None:
        token = cfg.web_token
        if token is None:
            raise HTTPException(403, "operator actions are off: no web token is configured (beaconctl web token generate)")
        authorization = request.headers.get("authorization")
        given = authorization[7:].strip() if authorization and authorization[:7].lower() == "bearer " else ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "wrong or missing operator token", headers={"WWW-Authenticate": "Bearer"})

    # --- errors: every one is {"detail": ...} -----------------------------------------------------------------------------

    def http_error(_: Request, e: HTTPException) -> Response:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code, headers=getattr(e, "headers", None))

    def not_found(_: Request, e: views.NotFound) -> Response:
        return JSONResponse({"detail": str(e)}, status_code=404)

    def store_error(_: Request, e: StoreError) -> Response:
        return JSONResponse({"detail": str(e)}, status_code=400)

    def busy(_: Request, e: sqlite3.OperationalError) -> Response:
        log.warning("database error: %s", e)
        return JSONResponse({"detail": "the database is busy; try again"}, status_code=503)

    # --- read API -------------------------------------------------------------------------------------------------------

    @reading
    def api_health(request: Request, store: Store) -> Response:
        return JSONResponse(views.health_view(store, cfg, time.time()))

    @reading
    def api_state(request: Request, store: Store) -> Response:
        return JSONResponse(views.state_view(store, cfg, time.time(), operator=cfg.web_token is not None))

    @reading
    def api_beacons(request: Request, store: Store) -> Response:
        now = time.time()
        return JSONResponse({"now": now, "beacons": views.beacons_view(store, cfg, now)})

    @reading
    def api_beacon(request: Request, store: Store) -> Response:
        limit = query_number(request, "limit", 50, int, 1, 500)
        return JSONResponse(views.beacon_view(store, cfg, request.path_params["prefix"], time.time(), limit))

    @reading
    def api_track(request: Request, store: Store) -> Response:
        hours = query_number(request, "hours", 6.0, float, 0, 24 * 31, low_inclusive=False)
        limit = query_number(request, "limit", 200, int, 1, 2000)
        return JSONResponse(views.track_view(store, request.path_params["prefix"], time.time(), hours, limit))

    @reading
    def api_transmission(request: Request, store: Store) -> Response:
        return JSONResponse(views.transmission_view(store, request.path_params["transmission_id"], time.time()))

    @reading
    def api_repeaters(request: Request, store: Store) -> Response:
        now = time.time()
        return JSONResponse({"now": now, "repeaters": views.repeaters_view(store, now)})

    @reading
    def api_rejects(request: Request, store: Store) -> Response:
        limit = query_number(request, "limit", 50, int, 1, 500)
        return JSONResponse(views.rejects_view(store, time.time(), request.query_params.get("beacon"), limit))

    # --- operator action ------------------------------------------------------------------------------------------------

    def api_reset(request: Request) -> Response:
        require_operator(request)  # before the database is opened, so a wrong token costs nothing
        with Store.open(cfg.db_path) as store:
            result = views.reset_beacon(store, request.path_params["prefix"])
        log.info("operator reset beacon %s (was high-water mark %s)", result["label"], result["old_hwm"])
        return JSONResponse(result)

    # --- map ------------------------------------------------------------------------------------------------------------

    def api_map(request: Request) -> Response:
        """How the page should draw its background: local tiles, a development URL, or the plain grid."""
        if tiles is not None:
            info = tiles.info
            return JSONResponse({
                "mode": "mbtiles", "url": "tiles/{z}/{x}/{y}", "min_zoom": info.min_zoom, "max_zoom": info.max_zoom,
                "bounds": info.bounds, "attribution": cfg.web.tiles_attribution or info.attribution or "", "name": info.name,
            })
        if cfg.web.online_tile_url:
            return JSONResponse({
                "mode": "online", "url": cfg.web.online_tile_url, "min_zoom": 0, "max_zoom": 19, "bounds": None,
                "attribution": online_attribution(cfg), "referrer_policy": ONLINE_REFERRER_POLICY, "problem": tiles_problem,
            })
        return JSONResponse({"mode": "grid", "url": None, "min_zoom": 0, "max_zoom": 22, "bounds": None, "attribution": "", "problem": tiles_problem})

    def tile(request: Request) -> Response:
        p = request.path_params
        found = tiles.tile(p["z"], p["x"], p["y"]) if tiles is not None else None
        if found is None:
            return Response(status_code=404)
        data, mime = found
        return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})

    # --- the page -------------------------------------------------------------------------------------------------------

    def index(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

    routes = [
        Route("/api/health", api_health),
        Route("/api/state", api_state),
        Route("/api/beacons", api_beacons),
        Route("/api/beacons/{prefix}", api_beacon),
        Route("/api/beacons/{prefix}/track", api_track),
        Route("/api/beacons/{prefix}/reset", api_reset, methods=["POST"]),
        Route("/api/transmissions/{transmission_id:int}", api_transmission),
        Route("/api/repeaters", api_repeaters),
        Route("/api/rejects", api_rejects),
        Route("/api/map", api_map),
        Route("/tiles/{z:int}/{x:int}/{y:int}", tile),
        Route("/", index),
    ]
    if STATIC_DIR.is_dir():
        routes.append(Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"))
    return Starlette(
        routes=routes,
        middleware=[Middleware(SecurityHeaders, policy=policy, referrer_policy=referrer_policy)],
        exception_handlers={
            HTTPException: http_error,
            views.NotFound: not_found,
            StoreError: store_error,
            sqlite3.OperationalError: busy,
        },
    )


def located_points(cfg: Config) -> list[tuple[float, float]]:
    """Where the base's enabled repeaters are, to check a tiles file against. Nothing when the database cannot be read: that is not a
    reason for the map not to start."""
    try:
        with Store.open(cfg.db_path) as store:
            return [(r["lat"], r["lon"]) for r in store.located_repeaters()]
    except (sqlite3.Error, StoreError, OSError):
        return []


def app_from_config(cfg: Config) -> Starlette:
    tiles, problem = open_tiles(cfg.tiles_path, cfg.web.tiles_scheme, located_points(cfg) if cfg.tiles_path else None)
    return create_app(cfg, tiles, problem)
