"""MBTiles row numbering (TMS or XYZ), zoom ranges, and `beaconctl tiles check`."""

import dataclasses
import logging
import sqlite3

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx2")
from starlette.testclient import TestClient  # noqa: E402

from beacon_base.api import app_from_config  # noqa: E402
from beacon_base.cli import main  # noqa: E402
from beacon_base.config import ConfigError, load_config  # noqa: E402
from beacon_base.store import Store  # noqa: E402
from beacon_base.tiles import MBTiles, TilesError, detect_scheme, open_tiles, xyz_tile  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"tile"
SITE = (40.0, -75.0)  # where the test repeater is
Z = 15


def make(path, rows, **meta):
    """An MBTiles file whose tiles table holds exactly `rows`: (zoom, column, row)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    for k, v in {"format": "png", **meta}.items():
        conn.execute("INSERT INTO metadata VALUES (?, ?)", (k, v))
    conn.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [(z, x, r, PNG) for z, x, r in rows])
    conn.commit()
    conn.close()
    return path


def site_rows(scheme, zooms=(Z,)):
    """The tile over SITE at each zoom, numbered the way `scheme` says."""
    out = []
    for z in zooms:
        x, y = xyz_tile(*SITE, z)
        out.append((z, x, y if scheme == "xyz" else (1 << z) - 1 - y))
    return out


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.toml").write_text('[database]\npath = "t.db"\n[clock]\nassume_synced = true\n')
    return load_config(tmp_path / "config.toml")


@pytest.fixture
def located(cfg):
    with Store.open(cfg.db_path) as s:
        s.add_repeater("a0a1a2a3a4a5a6a7", *SITE, name="north")
    return cfg


def served(cfg, path, **web):
    cfg = dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, tiles=str(path), **web))
    return TestClient(app_from_config(cfg))


# --- the row scheme --------------------------------------------------------------------------------------------------------


def test_the_specification_numbers_rows_from_the_south(tmp_path):
    tiles = MBTiles(make(tmp_path / "a.mbtiles", site_rows("tms")))
    x, y = xyz_tile(*SITE, Z)
    assert tiles.scheme == "tms" and tiles.tile(Z, x, y) == (PNG, "image/png")
    assert tiles.tile(Z, x, y + 1) is None


def test_the_files_own_scheme_metadata_is_believed(tmp_path):
    tiles = MBTiles(make(tmp_path / "a.mbtiles", site_rows("xyz"), scheme="xyz"))
    assert (tiles.scheme, tiles.scheme_source) == ("xyz", "the file's scheme metadata")
    assert tiles.tile(Z, *xyz_tile(*SITE, Z)) is not None


def test_the_configured_scheme_wins_over_the_files(tmp_path):
    path = make(tmp_path / "a.mbtiles", site_rows("xyz"), scheme="tms")
    tiles = MBTiles(path, "xyz")
    assert tiles.scheme == "xyz" and tiles.tile(Z, *xyz_tile(*SITE, Z)) is not None
    with pytest.raises(TilesError, match="scheme"):
        MBTiles(path, "sideways")


def test_a_file_numbered_from_the_north_is_noticed_by_where_the_repeaters_are(tmp_path, caplog):
    """MapProxy's MBTiles cache writes XYZ rows: read as TMS such a file shows nothing anywhere."""
    caplog.set_level(logging.INFO)
    path = make(tmp_path / "flipped.mbtiles", site_rows("xyz"))
    tiles, problem = open_tiles(path, "auto", [SITE])
    assert problem is None and tiles.scheme == "xyz" and tiles.detected
    assert tiles.tile(Z, *xyz_tile(*SITE, Z)) is not None
    assert "from the north" in caplog.text and "web.tiles_scheme" in caplog.text


def test_a_file_that_follows_the_specification_stays_tms(tmp_path, caplog):
    tiles, _ = open_tiles(make(tmp_path / "a.mbtiles", site_rows("tms")), "auto", [SITE])
    assert tiles.scheme == "tms" and tiles.detected and "from the north" not in caplog.text


def test_with_nothing_to_go_on_a_file_is_read_as_the_specification_says(tmp_path):
    path = make(tmp_path / "a.mbtiles", site_rows("xyz"))
    assert open_tiles(path, "auto", None)[0].scheme == "tms"  # no repeaters known
    assert open_tiles(path, "auto", [(-40.0, 75.0)])[0].scheme == "tms"  # none of them has a tile
    assert detect_scheme(MBTiles(path), [(-40.0, 75.0)])[0] is None


def test_a_file_for_another_area_is_warned_about(tmp_path, caplog):
    open_tiles(make(tmp_path / "a.mbtiles", site_rows("tms")), "auto", [(-40.0, 75.0)])
    assert "no tile under any located repeater" in caplog.text


def test_the_configured_scheme_is_not_second_guessed(tmp_path):
    path = make(tmp_path / "a.mbtiles", site_rows("xyz"))
    assert open_tiles(path, "tms", [SITE])[0].scheme == "tms"


def test_beacon_web_serves_a_flipped_file_without_being_told(located, tmp_path):
    path = make(tmp_path / "flipped.mbtiles", site_rows("xyz"), name="x", minzoom="15", maxzoom="15")
    x, y = xyz_tile(*SITE, Z)
    with served(located, path) as c:
        assert c.get(f"/tiles/{Z}/{x}/{y}").content == PNG
        assert c.get(f"/tiles/{Z}/{x}/{y + 1}").status_code == 404


def test_the_scheme_setting_is_validated(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[web]\ntiles_scheme = "sideways"\n')
    with pytest.raises(ConfigError, match="tiles_scheme"):
        load_config(p)


# --- the zoom range --------------------------------------------------------------------------------------------------------


def test_the_zoom_range_is_what_the_tiles_hold_even_if_the_metadata_says_more(tmp_path):
    """A declared maximum above the real one had the page ask for tiles at levels that are not in the file."""
    rows = site_rows("tms", (14, 15, 16))
    assert MBTiles(make(tmp_path / "a.mbtiles", rows, minzoom="10", maxzoom="19")).info.max_zoom == 16
    assert MBTiles(make(tmp_path / "b.mbtiles", rows, minzoom="10", maxzoom="19")).info.min_zoom == 14
    assert MBTiles(make(tmp_path / "c.mbtiles", rows, minzoom="15", maxzoom="15")).info.min_zoom == 15  # a narrower range is respected
    info = MBTiles(make(tmp_path / "d.mbtiles", rows)).info  # no metadata at all
    assert (info.min_zoom, info.max_zoom) == (14, 16)
    odd = MBTiles(make(tmp_path / "e.mbtiles", rows, minzoom="20", maxzoom="22")).info  # a range that misses every tile is ignored
    assert (odd.min_zoom, odd.max_zoom) == (14, 16)


def test_a_file_with_no_metadata_is_warned_about(tmp_path, caplog):
    path = tmp_path / "bare.mbtiles"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    conn.commit()
    conn.close()
    open_tiles(path)
    assert "no metadata" in caplog.text


# --- beaconctl tiles check -------------------------------------------------------------------------------------------------


def run_check(cfg, capsys, *args):
    code = main(["-c", str(cfg.config_path), "tiles", "check", *args])
    return code, capsys.readouterr().out


def test_check_passes_a_complete_file(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", site_rows("tms"), minzoom="15", maxzoom="15", attribution="(c) test", name="x")
    code, out = run_check(located, capsys, str(path))
    assert code == 0 and out.rstrip().endswith("ok") and "15: 1 tiles" in out
    assert "with the rows read as TMS 1 have a tile, as XYZ 0" in out


def test_check_explains_a_flipped_file_and_what_to_do(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", site_rows("xyz"))
    code, out = run_check(located, capsys, str(path))
    assert code == 1
    assert "as TMS 0 have a tile, as XYZ 1" in out and "scheme        xyz (detected" in out
    assert 'web.tiles_scheme = "xyz"' in out and "no attribution" in out


def test_check_fails_a_file_whose_scheme_is_set_the_wrong_way_round(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", site_rows("xyz"), attribution="x", minzoom="15", maxzoom="15")
    code, out = run_check(located, capsys, str(path), "--scheme", "tms")
    assert code == 1 and "nothing will show" in out


def test_check_says_when_the_file_is_for_another_area(located, tmp_path, capsys):
    code, out = run_check(located, capsys, str(make(tmp_path / "a.mbtiles", [(15, 1, 1)], attribution="x")))
    assert code == 1 and "for another area" in out


def test_check_without_a_file_or_a_configured_one_asks_for_one(cfg, capsys):
    assert main(["-c", str(cfg.config_path), "tiles", "check"]) == 2
    assert "no tiles file" in capsys.readouterr().err


def test_check_reports_a_file_that_is_not_mbtiles(located, tmp_path, capsys):
    code, out = run_check(located, capsys, str(tmp_path / "config.toml"))
    assert code == 1 and "not an MBTiles file" in out


# --- beaconctl tiles write-metadata -----------------------------------------------------------------------------------------------


def write_args(path, *extra):
    return ["tiles", "write-metadata", str(path), *extra]


def table(path, name="metadata"):
    with sqlite3.connect(path) as db:
        return dict(db.execute(f"SELECT name, value FROM {name}").fetchall())


def many_rows(scheme):
    """Tiles at two zoom levels over SITE and a neighbour, numbered the way `scheme` says."""
    out = []
    for z in (14, 15):
        x, y = xyz_tile(*SITE, z)
        for dx, dy in ((0, 0), (1, 0), (0, 1)):
            out.append((z, x + dx, (y + dy) if scheme == "xyz" else (1 << z) - 1 - (y + dy)))
    return out


def run(cfg, capsys, *args):
    code = main(["-c", str(cfg.config_path), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_file_numbered_from_the_north_is_renumbered_and_given_its_metadata(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("xyz"))
    code, out, _ = run(located, capsys, *write_args(path))
    assert code == 0
    assert "RENUMBERED from the north (XYZ) to the south (TMS)" in out and "6 tiles" in out
    meta = table(path)
    assert {k: meta[k] for k in ("format", "minzoom", "maxzoom", "scheme", "name")} == {
        "format": "png", "minzoom": "14", "maxzoom": "15", "scheme": "tms", "name": "a"
    }
    assert "OpenStreetMap" in meta["attribution"] and "openstreetmap.org/copyright" in meta["attribution"]
    for key, value in meta.items():  # the output echoes every metadata entry in full
        assert f"  {key}" in out and value in out
    tiles = MBTiles(path)  # and the map reads it as the specification says, with no detection involved
    assert (tiles.scheme, tiles.scheme_source) == ("tms", "the file's scheme metadata")
    x, y = xyz_tile(*SITE, 15)
    assert tiles.tile(15, x, y) is not None and tiles.tile(15, x + 1, y + 1) is None


def test_the_bounds_come_from_the_tiles_and_hold_the_repeater(located, tmp_path, capsys):
    for scheme in ("xyz", "tms"):
        path = make(tmp_path / f"{scheme}.mbtiles", many_rows(scheme))
        assert run(located, capsys, *write_args(path))[0] == 0
        west, south, east, north = (float(v) for v in table(path)["bounds"].split(","))
        assert west < SITE[1] < east and south < SITE[0] < north, scheme
        assert MBTiles(path).info.bounds == (west, south, east, north)


def test_a_file_that_follows_the_specification_is_left_alone_but_given_metadata(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("tms"))
    code, out, _ = run(located, capsys, *write_args(path))
    assert code == 0 and "numbered from the south (TMS) already: left as they are" in out and "RENUMBERED" not in out
    assert "1 of 1 located repeaters have tiles that way" in out
    assert MBTiles(path).tile(15, *xyz_tile(*SITE, 15)) is not None


def test_running_it_twice_does_not_renumber_twice(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("xyz"))
    run(located, capsys, *write_args(path))
    before = sqlite3.connect(path).execute("SELECT zoom_level, tile_column, tile_row FROM tiles ORDER BY 1, 2, 3").fetchall()
    code, out, _ = run(located, capsys, *write_args(path))
    assert code == 0 and "RENUMBERED" not in out and "as the file's scheme metadata says" in out
    assert sqlite3.connect(path).execute("SELECT zoom_level, tile_column, tile_row FROM tiles ORDER BY 1, 2, 3").fetchall() == before


def test_it_will_not_guess_without_a_repeater_to_go_by(cfg, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("xyz"))
    original = path.read_bytes()
    code, _, err = run(cfg, capsys, *write_args(path))
    assert code == 2 and "cannot tell how" in err and "--rows xyz" in err
    assert path.read_bytes() == original  # nothing was touched
    code, out, _ = run(cfg, capsys, *write_args(path, "--rows", "xyz"))
    assert code == 0 and "RENUMBERED" in out and "as you said (--rows)" in out
    assert MBTiles(path).tile(15, *xyz_tile(*SITE, 15)) is not None


def test_a_file_for_another_area_is_not_guessed_at_either(located, tmp_path, capsys):
    code, _, err = run(located, capsys, *write_args(make(tmp_path / "a.mbtiles", [(15, 1, 1), (15, 2, 2)])))
    assert code == 2 and "cannot tell how" in err


def test_a_dry_run_changes_nothing_and_says_what_it_would_do(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("xyz"))
    original = path.read_bytes()
    code, out, _ = run(located, capsys, *write_args(path, "--dry-run"))
    assert code == 0 and "nothing is written" in out and "would be RENUMBERED" in out and "as it would be written" in out
    assert path.read_bytes() == original


def test_the_credit_name_and_description_can_be_given(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("tms"), name="old", custom="kept")
    run(located, capsys, *write_args(path, "--name", "Event", "--attribution", "(c) Example", "--description", "d"))
    meta = table(path)
    assert (meta["name"], meta["attribution"], meta["description"], meta["custom"]) == ("Event", "(c) Example", "d", "kept")
    run(located, capsys, *write_args(path))  # a later run keeps the name it was given
    assert table(path)["name"] == "Event"


def test_an_empty_attribution_is_a_warning(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("tms"))
    code, out, _ = run(located, capsys, *write_args(path, "--attribution", ""))
    assert code == 0 and "the map shows no credit" in out


def test_a_file_with_no_metadata_table_and_extra_columns_is_handled(located, tmp_path, capsys):
    path = tmp_path / "mapproxy.mbtiles"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB, last_modified TEXT)")
    conn.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?, 'now')", [(z, x, r, PNG) for z, x, r in many_rows("xyz")])
    conn.execute("CREATE UNIQUE INDEX idx_tile ON tiles (zoom_level, tile_column, tile_row)")
    conn.commit()
    conn.close()
    code, out, _ = run(located, capsys, *write_args(path))
    assert code == 0 and "RENUMBERED" in out
    assert table(path)["maxzoom"] == "15" and MBTiles(path).tile(14, *xyz_tile(*SITE, 14)) is not None
    assert sqlite3.connect(path).execute("SELECT count(*) FROM tiles").fetchone()[0] == 6


def test_the_format_is_taken_from_the_tiles(located, tmp_path, capsys):
    path = make(tmp_path / "a.mbtiles", many_rows("tms"), format="png")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tiles SET tile_data = ?", (b"\xff\xd8\xff\xe0jpeg",))
    run(located, capsys, *write_args(path))
    assert table(path)["format"] == "jpg"


@pytest.mark.parametrize("what", ["vector", "empty", "view", "not-a-database", "missing"])
def test_files_that_cannot_be_made_ready_are_refused_with_a_reason(located, tmp_path, capsys, what):
    path = tmp_path / "x.mbtiles"
    if what == "vector":
        make(path, many_rows("tms"), format="pbf")
    elif what == "empty":
        make(path, [])
    elif what == "view":
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE images (tile_id TEXT, tile_data BLOB)")
        conn.execute("CREATE TABLE map (zoom_level INT, tile_column INT, tile_row INT, tile_id TEXT)")
        conn.execute("CREATE VIEW tiles AS SELECT zoom_level, tile_column, tile_row, tile_data FROM map JOIN images USING (tile_id)")
        conn.commit()
        conn.close()
    elif what == "not-a-database":
        path.write_text("hello, this is not sqlite at all")
    code, _, err = run(located, capsys, *write_args(path))
    assert code == 2 and err.startswith("beaconctl: ")
    if what == "vector":
        assert "only raster" in err
    if what == "view":
        assert "not a table" in err


def test_a_failure_part_way_leaves_the_file_as_it_was(located, tmp_path, capsys, monkeypatch):
    """The renumbering is one transaction: if the metadata cannot be written, the rows are not renumbered either."""
    path = make(tmp_path / "a.mbtiles", many_rows("xyz"))
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER no_metadata BEFORE INSERT ON metadata BEGIN SELECT RAISE(ABORT, 'refused'); END")
    code, _, err = run(located, capsys, *write_args(path))
    assert code == 2 and "refused" in err
    rows = sqlite3.connect(path).execute("SELECT zoom_level, tile_column, tile_row FROM tiles ORDER BY 1, 2, 3").fetchall()
    assert rows == sorted(many_rows("xyz"))
