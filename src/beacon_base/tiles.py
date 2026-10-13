"""Reading map tiles from an MBTiles file, so the map works with no internet (docs/plan/web-map.md, decision 9).

An MBTiles file is a SQLite database with a ``tiles`` table. The specification numbers its rows in the TMS scheme (row 0 at the south),
where the web map asks for XYZ (row 0 at the north), so the row is flipped. Some tools write XYZ rows into the file all the same
(MapProxy's MBTiles cache does, from a grid that counts from the north), and such a file shows no tiles at all: every request misses.
So the scheme is a setting: ``tms`` (the specification), ``xyz``, or ``auto``, which takes the file's ``scheme`` metadata if it has one and
otherwise looks at where the located repeaters are (``detect_scheme``) and falls back to ``tms``.

Only raster tiles (png, jpg, webp) are served: a file of vector tiles (pbf) is refused with a message, because the page has no vector
renderer.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MAX_ZOOM = 24
SCHEMES = ("auto", "tms", "xyz")
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


def xyz_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """The XYZ address (x, y) of the tile holding a point, clamped to the world."""
    n = 1 << zoom
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


class MBTiles:
    def __init__(self, path: str | Path, scheme: str = "auto"):
        if scheme not in SCHEMES:
            raise TilesError(f"tile scheme must be one of {', '.join(SCHEMES)}, not {scheme!r}")
        self.path = Path(path)
        if not self.path.is_file():
            raise TilesError(f"tiles file {self.path} not found")
        try:
            # read-only, so a tiles file on a read-only card or a shared mount works and is never modified
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._lock = threading.Lock()  # requests arrive on several threads and one connection serves them in turn
            self.metadata = dict(self._conn.execute("SELECT name, value FROM metadata").fetchall())
            self._conn.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
        except sqlite3.Error as e:
            raise TilesError(f"{self.path} is not an MBTiles file: {e}") from None
        fmt = (self.metadata.get("format") or "png").lower()
        if fmt not in RASTER_FORMATS:
            raise TilesError(f"{self.path} holds {fmt!r} tiles; only raster tiles ({', '.join(sorted(set(RASTER_FORMATS)))}) can be shown")
        self._default_type = RASTER_FORMATS[fmt]
        self.requested_scheme = scheme
        self.detected = False
        declared = (self.metadata.get("scheme") or "").lower()
        self.scheme, self.scheme_source = self._first_scheme(scheme, declared)
        self.info = self._info(self.metadata)

    @staticmethod
    def _first_scheme(requested: str, declared: str) -> tuple[str, str]:
        if requested != "auto":
            return requested, "configured"
        if declared in ("tms", "xyz"):
            return declared, "the file's scheme metadata"
        return "tms", "the MBTiles specification (not confirmed)"

    def _levels(self) -> list[int]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]

    def _info(self, meta: dict[str, str]) -> TileInfo:
        """The zoom range is what the tiles table holds, clipped to the metadata's range when it gives one: a declared range wider than the
        tiles would have the page ask for levels that are not there."""
        levels = self._levels()
        lo, hi = (levels[0], levels[-1]) if levels else (0, 0)
        try:
            lo, hi = max(lo, int(meta["minzoom"])), min(hi, int(meta["maxzoom"]))
            if lo > hi:
                lo, hi = (levels[0], levels[-1]) if levels else (0, 0)
        except (KeyError, ValueError):
            pass
        bounds = None
        try:
            west, south, east, north = (float(v) for v in meta["bounds"].split(","))
            if -180 <= west < east <= 180 and -90 <= south < north <= 90:
                bounds = (west, south, east, north)
        except (KeyError, ValueError):
            pass
        return TileInfo(meta.get("name"), meta.get("attribution"), lo, hi, bounds)

    def row(self, z: int, y: int, scheme: str | None = None) -> int:
        """The row to look up for an XYZ `y` at zoom `z`."""
        return y if (scheme or self.scheme) == "xyz" else (1 << z) - 1 - y

    def has_tile(self, z: int, x: int, y: int, scheme: str | None = None) -> bool:
        if not 0 <= z <= MAX_ZOOM or not (0 <= x < 1 << z and 0 <= y < 1 << z):
            return False
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? LIMIT 1", (z, x, self.row(z, y, scheme))
            ).fetchone() is not None

    def tile(self, z: int, x: int, y: int) -> tuple[bytes, str] | None:
        """The tile at an XYZ address and its content type, or None if the file has none there."""
        if not 0 <= z <= MAX_ZOOM or not (0 <= x < 1 << z and 0 <= y < 1 << z):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?", (z, x, self.row(z, y))
            ).fetchone()
        if row is None or row[0] is None:
            return None
        data = bytes(row[0])
        return data, content_type(data, self._default_type)

    def coverage(self, points: list[tuple[float, float]]) -> dict[str, int]:
        """How many of the points (latitude, longitude) have a tile at some zoom level of the file, reading its rows as `tms` and as `xyz`.
        A file with the right scheme covers the places it was made for; with the wrong one it covers almost nothing."""
        levels = self._levels()
        return {
            scheme: sum(1 for lat, lon in points if any(self.has_tile(z, *xyz_tile(lat, lon, z), scheme=scheme) for z in reversed(levels)))
            for scheme in ("tms", "xyz")
        }

    def resolve_scheme(self, points: list[tuple[float, float]]) -> dict[str, int]:
        """With scheme `auto` and nothing in the file saying how its rows are numbered, take the scheme under which the points have tiles
        (`detected` says whether that happened). Returns the coverage counts, empty when nothing was checked."""
        self.detected = False
        if self.requested_scheme != "auto" or self.scheme_source.startswith("the file") or not points:
            return {}
        found, counts = detect_scheme(self, points)
        if found is not None:
            self.scheme, self.detected = found, True
            self.scheme_source = f"detected: {counts[found]} of {len(points)} repeaters have tiles that way, {counts['xyz' if found == 'tms' else 'tms']} the other way"
        return counts

    def close(self) -> None:
        self._conn.close()


def detect_scheme(tiles: MBTiles, points: list[tuple[float, float]]) -> tuple[str | None, dict[str, int]]:
    """Which row scheme puts the file's tiles where the points are: ('tms' or 'xyz', the counts), or (None, counts) when the points do not
    say (none given, or the same number under both, which includes none under either)."""
    counts = tiles.coverage(points)
    if counts["tms"] > counts["xyz"]:
        return "tms", counts
    if counts["xyz"] > counts["tms"]:
        return "xyz", counts
    return None, counts


def open_tiles(
    path: Path | None, scheme: str = "auto", points: list[tuple[float, float]] | None = None
) -> tuple[MBTiles | None, str | None]:
    """(the configured tiles file, None), or (None, why not) when there is none or it cannot be used: the map then falls back to its
    plain grid rather than failing to start. No path configured is (None, None).

    With scheme ``auto``, a file that does not say how its rows are numbered is checked against `points` (where the located repeaters are):
    if its tiles are under them only when the rows are read the other way, they are read that way, and the log says so."""
    if path is None:
        return None, None
    try:
        tiles = MBTiles(path, scheme)
    except TilesError as e:
        log.warning("%s; the map shows a plain grid", e)
        return None, str(e)
    counts = tiles.resolve_scheme(points or [])
    if tiles.detected and tiles.scheme == "xyz":
        log.warning(
            "%s numbers its tile rows from the north (XYZ), not as the MBTiles specification says; reading it that way. "
            "Set web.tiles_scheme = \"xyz\" to say so, or convert the file (docs/map-tiles.md)", path,
        )
    elif counts and counts["tms"] == counts["xyz"] == 0:
        log.warning("%s has no tile under any located repeater: is it for another area? (beaconctl tiles check)", path)
    if not tiles.metadata:
        log.warning("%s has no metadata, so it has no attribution and no declared zoom range (docs/map-tiles.md, step 6)", path)
    log.info("map tiles from %s (zoom %d-%d, rows read as %s: %s)", path, tiles.info.min_zoom, tiles.info.max_zoom, tiles.scheme, tiles.scheme_source)
    return tiles, None
