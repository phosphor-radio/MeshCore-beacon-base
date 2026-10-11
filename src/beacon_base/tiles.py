"""Reading map tiles from an MBTiles file, so the map works with no internet (docs/plan/web-map.md, decision 9).

An MBTiles file is a SQLite database with a ``tiles`` table in the TMS scheme (row 0 at the south), where the web map asks for XYZ
(row 0 at the north); the row is flipped here. Only raster tiles (png, jpg, webp) are served: a file of vector tiles (pbf) is refused
with a message, because the page has no vector renderer.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MAX_ZOOM = 24
RASTER_FORMATS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
_SIGNATURES = ((b"\x89PNG", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"RIFF", "image/webp"))


class TilesError(Exception):
    """The tiles file cannot be used; the message says why, for the operator."""


@dataclass(frozen=True)
class TileInfo:
    name: str | None
    attribution: str | None
    min_zoom: int
    max_zoom: int
    bounds: tuple[float, float, float, float] | None  # west, south, east, north


def content_type(data: bytes, default: str) -> str:
    for magic, mime in _SIGNATURES:
        if data.startswith(magic):
            return mime
    return default


class MBTiles:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise TilesError(f"tiles file {self.path} not found")
        try:
            # read-only, so a tiles file on a read-only card or a shared mount works and is never modified
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._lock = threading.Lock()  # requests arrive on several threads and one connection serves them in turn
            meta = dict(self._conn.execute("SELECT name, value FROM metadata").fetchall())
            self._conn.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
        except sqlite3.Error as e:
            raise TilesError(f"{self.path} is not an MBTiles file: {e}") from None
        fmt = (meta.get("format") or "png").lower()
        if fmt not in RASTER_FORMATS:
            raise TilesError(f"{self.path} holds {fmt!r} tiles; only raster tiles ({', '.join(sorted(set(RASTER_FORMATS)))}) can be shown")
        self._default_type = RASTER_FORMATS[fmt]
        self.info = self._info(meta)

    def _info(self, meta: dict[str, str]) -> TileInfo:
        try:
            lo, hi = (int(meta[k]) for k in ("minzoom", "maxzoom"))
        except (KeyError, ValueError):
            with self._lock:
                lo, hi = self._conn.execute("SELECT min(zoom_level), max(zoom_level) FROM tiles").fetchone()
            lo, hi = (0, 0) if lo is None else (lo, hi)
        bounds = None
        try:
            west, south, east, north = (float(v) for v in meta["bounds"].split(","))
            if -180 <= west < east <= 180 and -90 <= south < north <= 90:
                bounds = (west, south, east, north)
        except (KeyError, ValueError):
            pass
        return TileInfo(meta.get("name"), meta.get("attribution"), lo, hi, bounds)

    def tile(self, z: int, x: int, y: int) -> tuple[bytes, str] | None:
        """The tile at an XYZ address and its content type, or None if the file has none there."""
        if not 0 <= z <= MAX_ZOOM or not (0 <= x < 1 << z and 0 <= y < 1 << z):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?", (z, x, (1 << z) - 1 - y)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        data = bytes(row[0])
        return data, content_type(data, self._default_type)

    def close(self) -> None:
        self._conn.close()


def open_tiles(path: Path | None) -> tuple[MBTiles | None, str | None]:
    """(the configured tiles file, None), or (None, why not) when there is none or it cannot be used: the map then falls back to its
    plain grid rather than failing to start. No path configured is (None, None)."""
    if path is None:
        return None, None
    try:
        tiles = MBTiles(path)
    except TilesError as e:
        log.warning("%s; the map shows a plain grid", e)
        return None, str(e)
    log.info("map tiles from %s (zoom %d-%d)", path, tiles.info.min_zoom, tiles.info.max_zoom)
    return tiles, None
