"""beacon-web over HTTP: the read API, the token, the tiles and the page."""

import dataclasses
import sqlite3
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from beacon_base import heartbeat  # noqa: E402
from beacon_base.api import app_from_config, create_app  # noqa: E402
from beacon_base.config import load_config  # noqa: E402
from beacon_base.pipeline import Pipeline  # noqa: E402
from beacon_base.store import Store  # noqa: E402
from beacon_base.tiles import MBTiles, TilesError, open_tiles  # noqa: E402

from helpers import BEACON_KEY, BEACON_PREFIX, REPEATER_A_KEY, REPEATER_B_KEY, obs, rx  # noqa: E402

A, B = REPEATER_A_KEY, REPEATER_B_KEY
TOKEN = "operator-token-0123456789"
PNG = b"\x89PNG\r\n\x1a\n" + b"tile"
JPG = b"\xff\xd8\xff\xe0" + b"tile"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "config.toml").write_text('[database]\npath = "t.db"\n[clock]\nassume_synced = true\n')
    return load_config(tmp_path / "config.toml")


@pytest.fixture
def with_data(cfg):
    now = time.time()
    with Store.open(cfg.db_path) as store:
        store.add_beacon(BEACON_KEY.hex())
        store.add_repeater(A.hex(), 40.0, -75.0, name="north")
        store.add_repeater(B.hex(), 40.0, -74.99, name="south")
        heartbeat.HeartbeatWriter(store).write()
        p = Pipeline(store, assume_synced=True)
        p.process(rx(A, obs(100), t=now - 20, mono=1.0))
        p.process(rx(A, obs(40), t=now - 10, mono=2.0))  # a lockout
    return cfg


@pytest.fixture
def client(with_data):
    with TestClient(create_app(dataclasses.replace(with_data, web_token=TOKEN))) as c:
        yield c


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- the read API --------------------------------------------------------------------------------------------------------


def test_state_is_the_whole_map(client):
    r = client.get("/api/state")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert s["operator"] is True and abs(s["now"] - time.time()) < 5
    assert [b["state"] for b in s["beacons"]] == ["rejected"]
    assert s["beacons"][0]["rejects"]["count"] == 1
    assert [r["label"] for r in s["repeaters"]] == ["north (a0a1a2)", "south (b0b1b2)"]
    assert s["health"]["ingest"]["state"] == "up"


def test_health_endpoint(client):
    h = client.get("/api/health").json()
    assert h["ingest"]["state"] == "up" and h["problems"][0]["code"] == "companion_disconnected"


def test_beacon_endpoints(client):
    prefix = BEACON_PREFIX.hex()
    d = client.get(f"/api/beacons/{prefix}").json()
    assert d["beacon"]["prefix"] == prefix and len(d["transmissions"]) == 1
    tx = d["transmissions"][0]["id"]
    assert client.get(f"/api/beacons/{prefix}/track").json()["points"][0]["counter"] == 100
    t = client.get(f"/api/transmissions/{tx}").json()
    assert t["observations"][0]["repeater_label"] == "north (a0a1a2)"
    assert [b["prefix"] for b in client.get("/api/beacons").json()["beacons"]] == [prefix]
    assert [r["prefix"] for r in client.get("/api/repeaters").json()["repeaters"]] == [A[:8].hex(), B[:8].hex()]
    rejects = client.get("/api/rejects", params={"beacon": prefix}).json()["rejects"]
    assert [(x["status"], x["reason"]) for x in rejects] == [("replay", "below_hwm")]


@pytest.mark.parametrize(
    "path", ["/api/beacons/ffffffffffffffff", "/api/beacons/nonsense", "/api/beacons/nonsense/track", "/api/transmissions/99999", "/api/rejects?beacon=zz"]
)
def test_unknown_things_are_404_not_500(client, path):
    r = client.get(path)
    assert r.status_code == 404 and "detail" in r.json()


def test_limits_are_validated(client):
    assert client.get(f"/api/beacons/{BEACON_PREFIX.hex()}?limit=0").status_code == 422
    assert client.get(f"/api/beacons/{BEACON_PREFIX.hex()}/track?hours=-1").status_code == 422
    assert client.get("/api/transmissions/abc").status_code == 422


def test_the_api_can_be_read_with_ingest_never_having_run(cfg):
    with TestClient(create_app(cfg)) as c:
        s = c.get("/api/state").json()
    assert s["beacons"] == [] and s["operator"] is False and s["health"]["ingest"]["state"] == "never"


# --- the operator action -------------------------------------------------------------------------------------------------


def test_reset_needs_the_token(client, with_data):
    url = f"/api/beacons/{BEACON_PREFIX.hex()}/reset"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=bearer("wrong-token-0123456789")).status_code == 401
    assert client.post(url, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.post(url).headers["www-authenticate"] == "Bearer"
    with Store.open(with_data.db_path) as s:
        assert s.beacon(BEACON_PREFIX.hex())["hwm"] == 100  # nothing was changed by the refusals


def test_reset_with_the_token_clears_the_lockout(client, with_data):
    r = client.post(f"/api/beacons/{BEACON_PREFIX.hex()}/reset", headers=bearer())
    assert r.status_code == 200
    assert (r.json()["old_hwm"], r.json()["last_reject_counter"], r.json()["rejects_cleared"]) == (100, 40, 1)
    assert client.get("/api/state").json()["beacons"][0]["state"] != "rejected"
    assert client.post("/api/beacons/ffffffffffffffff/reset", headers=bearer()).status_code == 404
    assert client.post("/api/beacons/0102/reset", headers=bearer()).status_code == 404  # a full prefix, not a short reference


def test_without_a_configured_token_operator_actions_are_off(with_data):
    with TestClient(create_app(with_data)) as c:
        r = c.post(f"/api/beacons/{BEACON_PREFIX.hex()}/reset", headers=bearer())
        assert r.status_code == 403 and "no web token" in r.json()["detail"]
        assert c.get("/api/state").json()["operator"] is False


def test_reading_needs_no_token(client):
    assert client.get("/api/state").status_code == 200


def test_the_methods_that_change_nothing_are_not_offered(client):
    assert client.get(f"/api/beacons/{BEACON_PREFIX.hex()}/reset").status_code == 405
    assert client.delete(f"/api/beacons/{BEACON_PREFIX.hex()}", headers=bearer()).status_code == 405


# --- the page ------------------------------------------------------------------------------------------------------------


def test_the_page_and_its_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "https:" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/index.html").headers["cache-control"] == "no-cache"
    assert client.get("/static/missing.js").status_code == 404
    assert client.get("/docs").status_code == 404  # the interactive docs would load their scripts from a CDN


def online(cfg, url="https://tile.openstreetmap.org/{z}/{x}/{y}.png", **web):
    return dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, online_tile_url=url, **web))


def test_online_tiles_for_development_loosen_the_image_policy_only(with_data):
    with TestClient(create_app(online(with_data, "https://tiles.example/{z}/{x}/{y}.png"))) as c:
        assert "img-src 'self' data: https:" in c.get("/").headers["content-security-policy"]
        m = c.get("/api/map").json()
    assert (m["mode"], m["url"]) == ("online", "https://tiles.example/{z}/{x}/{y}.png")


def test_online_tiles_send_a_referer_because_openstreetmap_blocks_requests_without_one(with_data, client):
    """tile.openstreetmap.org answers a request with no Referer with a 403 image (its tile usage policy), so a page that loads its
    tiles must not set a policy that strips it. A map with no outside requests keeps no-referrer."""
    assert client.get("/").headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/state").headers["referrer-policy"] == "no-referrer"
    with TestClient(create_app(online(with_data))) as c:
        for path in ("/", "/api/state", "/static/app.js"):
            assert c.get(path).headers["referrer-policy"] == "strict-origin-when-cross-origin", path
        assert c.get("/api/map").json()["referrer_policy"] == "strict-origin-when-cross-origin"


def test_online_openstreetmap_tiles_carry_their_attribution(with_data):
    with TestClient(create_app(online(with_data))) as c:
        credit = c.get("/api/map").json()["attribution"]
    assert "OpenStreetMap" in credit and "openstreetmap.org/copyright" in credit
    with TestClient(create_app(online(with_data, "https://tiles.example/{z}/{x}/{y}.png"))) as c:
        assert c.get("/api/map").json()["attribution"] == ""  # not OpenStreetMap's, so not claimed
    with TestClient(create_app(online(with_data, "https://tiles.example/{z}/{x}/{y}.png", online_tile_attribution="(c) Example"))) as c:
        assert c.get("/api/map").json()["attribution"] == "(c) Example"


# --- tiles ---------------------------------------------------------------------------------------------------------------


def make_mbtiles(path, tiles, fmt="png", **meta):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    for k, v in {"name": "test", "format": fmt, **meta}.items():
        conn.execute("INSERT INTO metadata VALUES (?, ?)", (k, v))
    conn.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", tiles)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def tile_client(with_data, tmp_path):
    # TMS rows: at zoom 2 there are four rows, and XYZ y=0 (north) is TMS row 3
    path = make_mbtiles(
        tmp_path / "map.mbtiles", [(0, 0, 0, PNG), (2, 1, 3, PNG), (2, 1, 0, JPG)],
        minzoom="0", maxzoom="2", bounds="-76.0,39.0,-74.0,41.0", attribution="(c) test",
    )
    cfg = dataclasses.replace(with_data, web=dataclasses.replace(with_data.web, tiles=str(path)))
    with TestClient(app_from_config(cfg)) as c:
        yield c


def test_tiles_are_served_with_the_row_flipped_to_xyz(tile_client):
    r = tile_client.get("/tiles/2/1/0")  # the northern row
    assert (r.status_code, r.content, r.headers["content-type"]) == (200, PNG, "image/png")
    assert "max-age" in r.headers["cache-control"]
    assert tile_client.get("/tiles/2/1/3").headers["content-type"] == "image/jpeg"  # the file said png, the bytes say jpeg
    assert tile_client.get("/tiles/0/0/0").status_code == 200


@pytest.mark.parametrize("path", ["/tiles/2/1/1", "/tiles/3/0/0", "/tiles/2/4/0", "/tiles/2/-1/0", "/tiles/99/0/0", "/tiles/2/1/9"])
def test_a_missing_or_impossible_tile_is_404(tile_client, path):
    assert tile_client.get(path).status_code == 404


def test_map_config_describes_the_tile_file(tile_client):
    m = tile_client.get("/api/map").json()
    assert (m["mode"], m["url"], m["min_zoom"], m["max_zoom"], m["attribution"]) == ("mbtiles", "tiles/{z}/{x}/{y}", 0, 2, "(c) test")
    assert m["bounds"] == [-76.0, 39.0, -74.0, 41.0]


def test_the_configured_credit_replaces_or_fills_in_the_files_attribution(with_data, tmp_path, caplog):
    covered = make_mbtiles(tmp_path / "a.mbtiles", [(0, 0, 0, PNG)], attribution="from the file")
    bare = make_mbtiles(tmp_path / "b.mbtiles", [(0, 0, 0, PNG)])

    def credit(path, **web):
        cfg = dataclasses.replace(with_data, web=dataclasses.replace(with_data.web, tiles=str(path), **web))
        with TestClient(app_from_config(cfg)) as c:
            return c.get("/api/map").json()["attribution"]

    assert credit(covered) == "from the file"
    assert credit(covered, tiles_attribution="configured") == "configured"
    assert credit(bare, tiles_attribution="configured") == "configured"
    assert "no attribution" not in caplog.text
    assert credit(bare) == "" and "no attribution in its metadata" in caplog.text  # nothing to show, and the operator is told


def test_without_tiles_the_map_is_a_plain_grid(client):
    assert client.get("/api/map").json()["mode"] == "grid"
    assert client.get("/tiles/0/0/0").status_code == 404


def test_a_tiles_file_that_cannot_be_used_falls_back_to_the_grid_and_says_why(with_data, tmp_path):
    vector = make_mbtiles(tmp_path / "vector.mbtiles", [(0, 0, 0, b"x")], fmt="pbf")
    for bad in (tmp_path / "nope.mbtiles", vector, tmp_path / "config.toml"):  # missing, vector tiles, not a database
        cfg = dataclasses.replace(with_data, web=dataclasses.replace(with_data.web, tiles=str(bad)))
        with TestClient(app_from_config(cfg)) as c:
            m = c.get("/api/map").json()
        assert m["mode"] == "grid" and m["problem"], bad


def test_mbtiles_reader_directly(tmp_path):
    path = make_mbtiles(tmp_path / "a.mbtiles", [(1, 0, 1, PNG)])
    tiles = MBTiles(path)
    assert tiles.info.min_zoom == tiles.info.max_zoom == 1 and tiles.info.bounds is None  # zoom range read from the tiles
    assert tiles.tile(1, 0, 0) == (PNG, "image/png") and tiles.tile(1, 1, 0) is None
    tiles.close()
    with pytest.raises(TilesError, match="pbf"):
        MBTiles(make_mbtiles(tmp_path / "b.mbtiles", [], fmt="pbf"))
    assert open_tiles(None) == (None, None)
