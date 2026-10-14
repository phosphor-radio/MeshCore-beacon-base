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
OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noopener">OpenStreetMap</a> contributors'
DEFAULT_DESCRIPTION = "Rendered from OpenStreetMap data (ODbL). Data processed by Geofabrik GmbH."
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
        with self._lock:
            return row_coverage(self._conn, points)

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


def row_coverage(conn: sqlite3.Connection, points: list[tuple[float, float]]) -> dict[str, int]:
    """How many of the points have a tile at some zoom level of the file behind `conn`, reading its rows as `tms` and as `xyz`."""
    levels = [r[0] for r in conn.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level")]
    sql = "SELECT 1 FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? LIMIT 1"
    counts = {"tms": 0, "xyz": 0}
    for lat, lon in points:
        for scheme in counts:
            for z in reversed(levels):
                x, y = xyz_tile(lat, lon, z)
                if conn.execute(sql, (z, x, y if scheme == "xyz" else (1 << z) - 1 - y)).fetchone():
                    counts[scheme] += 1
                    break
    return counts


def _verdict(counts: dict[str, int]) -> str | None:
    if counts["tms"] > counts["xyz"]:
        return "tms"
    if counts["xyz"] > counts["tms"]:
        return "xyz"
    return None


def detect_scheme(tiles: MBTiles, points: list[tuple[float, float]]) -> tuple[str | None, dict[str, int]]:
    """Which row scheme puts the file's tiles where the points are: ('tms' or 'xyz', the counts), or (None, counts) when the points do not
    say (none given, or the same number under both, which includes none under either)."""
    counts = tiles.coverage(points)
    return _verdict(counts), counts


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


# --- writing the metadata --------------------------------------------------------------------------------------------------------


@dataclass
class MetadataResult:
    renumbered: bool  # the rows were (or, in a dry run, would be) renumbered from the north to the south
    rows_note: str  # why: what said the rows were numbered the way they were
    tiles: int  # tiles in the file
    levels: list[tuple[int, int]]  # (zoom level, tiles)
    metadata: dict[str, str]  # the whole metadata table after the write (or as it would be)
    written: bool  # False for a dry run


def _bounds(conn: sqlite3.Connection, zoom: int, rows_from_north: bool) -> str:
    """west,south,east,north of the tiles at one zoom level, from the extent of their columns and rows."""
    n = 1 << zoom
    x0, x1, r0, r1 = conn.execute(
        "SELECT min(tile_column), max(tile_column), min(tile_row), max(tile_row) FROM tiles WHERE zoom_level = ?", (zoom,)
    ).fetchone()
    y_top, y_bottom = (r0, r1) if rows_from_north else (n - 1 - r1, n - 1 - r0)  # northernmost and southernmost XYZ rows

    def lat(y: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))

    return f"{x0 / n * 360 - 180:.5f},{lat(y_bottom + 1):.5f},{(x1 + 1) / n * 360 - 180:.5f},{lat(y_top):.5f}"


def write_metadata(
    path: str | Path,
    points: list[tuple[float, float]] | None = None,
    rows: str = "auto",
    name: str | None = None,
    attribution: str | None = None,
    description: str | None = None,
    dry_run: bool = False,
) -> MetadataResult:
    """Make a raster MBTiles file ready for the map: number its rows from the south as the specification says (when they are numbered
    from the north, as MapProxy writes them), and write the metadata the map uses, taken from the tiles where it can be (zoom range, bounds,
    format) and otherwise from the arguments (name, attribution, description). All in one transaction: a failure leaves the file as it was.

    `rows` says how the rows are numbered now: ``tms`` (leave them), ``xyz`` (renumber them), or ``auto``: the file's own `scheme` metadata,
    else whichever makes the tiles lie under `points` (the located repeaters). Nothing to go on is an error rather than a guess, since
    renumbering a file that is already right would break it."""
    if rows not in SCHEMES:
        raise TilesError(f"rows must be one of {', '.join(SCHEMES)}, not {rows!r}")
    path = Path(path)
    if not path.is_file():
        raise TilesError(f"tiles file {path} not found")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro" if dry_run else str(path), uri=dry_run, isolation_level=None, timeout=30)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as e:
        raise TilesError(f"{path} is not an MBTiles file: {e}") from None
    try:
        kind = conn.execute("SELECT type FROM sqlite_master WHERE name = 'tiles'").fetchone()
        if kind is None:
            raise TilesError(f"{path} has no tiles table: not an MBTiles file")
        if kind[0] != "table":
            raise TilesError(f"{path}: `tiles` is a {kind[0]}, not a table, so its rows cannot be renumbered; export it to a plain table first")
        levels = [(z, n) for z, n in conn.execute("SELECT zoom_level, count(*) FROM tiles GROUP BY 1 ORDER BY 1")]
        if not levels:
            raise TilesError(f"{path} has no tiles")
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'metadata' AND type = 'table'").fetchone() is not None
        meta = dict(conn.execute("SELECT name, value FROM metadata").fetchall()) if has_meta else {}
        if (meta.get("format") or "png").lower() not in RASTER_FORMATS:
            raise TilesError(f"{path} holds {meta['format']!r} tiles; only raster tiles can be shown")

        # how the rows are numbered now
        declared = (meta.get("scheme") or "").lower()
        if rows != "auto":
            current, note = rows, "as you said (--rows)"
        elif declared in ("tms", "xyz"):
            current, note = declared, "as the file's scheme metadata says"
        else:
            found, counts = detect_scheme_on(conn, points or [])
            if found is None:
                why = "no repeater is located, so there is nothing to compare the file with" if not points else (
                    "no tile lies under any located repeater, or the repeaters do not agree" if counts["tms"] == counts["xyz"] else "the repeaters do not say")
                raise TilesError(f"cannot tell how {path} numbers its rows: {why}. Say it with --rows xyz (from the north: what MapProxy writes) or --rows tms")
            current = found
            note = f"{counts[found]} of {len(points or [])} located repeaters have tiles that way, {counts['xyz' if found == 'tms' else 'tms']} the other way"
        renumber = current == "xyz"

        # the metadata, from the tiles where they say it
        sample = conn.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()[0]
        fmt = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(content_type(bytes(sample), ""), (meta.get("format") or "png").lower())
        new = {
            "name": name or meta.get("name") or path.stem,
            "format": fmt,
            "minzoom": str(levels[0][0]),
            "maxzoom": str(levels[-1][0]),
            "bounds": _bounds(conn, levels[-1][0], rows_from_north=current == "xyz"),
            "attribution": attribution if attribution is not None else OSM_ATTRIBUTION,
            "description": description if description is not None else DEFAULT_DESCRIPTION,
            "scheme": "tms",
        }
        result = MetadataResult(renumber, note, sum(n for _, n in levels), levels, {**meta, **new}, written=not dry_run)
        if dry_run:
            return result
        conn.execute("BEGIN IMMEDIATE")
        try:
            if renumber:
                conn.execute(
                    "CREATE TABLE tiles_tms AS SELECT zoom_level, tile_column, ((1 << zoom_level) - 1 - tile_row) AS tile_row, tile_data FROM tiles"
                )
                conn.execute("DROP TABLE tiles")
                conn.execute("ALTER TABLE tiles_tms RENAME TO tiles")
                conn.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
            conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
            conn.execute(f"DELETE FROM metadata WHERE name IN ({','.join('?' * len(new))})", list(new))
            conn.executemany("INSERT INTO metadata VALUES (?, ?)", new.items())
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if renumber:
            conn.execute("VACUUM")  # the old table's pages are free: give them back
        return result
    except sqlite3.Error as e:
        raise TilesError(f"{path}: {e}") from None
    finally:
        conn.close()


def detect_scheme_on(conn: sqlite3.Connection, points: list[tuple[float, float]]) -> tuple[str | None, dict[str, int]]:
    """detect_scheme for an open connection."""
    counts = row_coverage(conn, points) if points else {"tms": 0, "xyz": 0}
    return _verdict(counts), counts
