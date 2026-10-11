# The map and API (`beacon-web`)

`beacon-web` shows what `beacon-ingest` has stored: a map with the repeaters and the beacons, banners for anything that needs
attention, and a small read-only API. It is a separate process that only reads the database (plus one operator action, reset), so it
can be stopped, restarted or left off without losing a report. The design is in [plan/web-map.md](plan/web-map.md).

## Running it

```bash
pip install -e '.[web]'        # FastAPI and uvicorn; the '.[dev]' extra includes them
beaconctl web token generate   # once: the token the Reset button asks for (saved in secrets.toml, mode 0600)
beacon-web                     # http://127.0.0.1:8080/
```

Options: `-c <config>`, `--host`, `--port`, `-v` (a log line per request). The `[web]` section of the configuration sets `host` and
`port`; see `deploy/config.example.toml`.

- **Only this machine can reach it by default** (`127.0.0.1`). To read the map from a phone or another laptop, set `web.host` to
  `"0.0.0.0"` (or the machine's address). The page is plain HTTP: anyone on that network can read the map, and the operator token is
  sent in clear when it is used. Use a network you trust.
- **Without a token the map is read-only.** The Reset button is replaced by the `beaconctl beacon reset` command to run instead.
  The token can also be given in `$BEACON_WEB_TOKEN` (at least 16 characters), which wins over `secrets.toml`.
- It does not need `beacon-ingest` to be running: it says so in a banner and shows what was stored.

The Docker image and compose file of the Pi deployment do not run `beacon-web` yet; see "Not done yet" below.

## Reading the page

- **Top bar**: is ingest running, is the companion connected and when the last report arrived, is the clock set. Ages are counted from
  the base's own clock, not the browser's.
- **Banners**: ingest not running or the companion not connected (red), the clock not set (the times are provisional until
  `beaconctl time set`), auto-add on, and a red banner for every beacon that is **locked out** (its counters are at or below its
  high-water mark; see "Rejection visibility" in [plan/beacon-base.md](plan/beacon-base.md)) with a **Reset** button. Reset asks
  to confirm, then for the token (kept for this tab only), and clears the high-water mark: the next report becomes the new baseline,
  so do it while the beacon is transmitting.
- **Map**: repeaters are diamonds (a repeater with no location, 0, 0, is not drawn and is listed as such). Beacons are circles
  coloured by state (green ok, red rejected, grey silent or disabled). A beacon one repeater heard has a **dashed circle** around that
  repeater, whose radius is how far away its signal suggests it was; for two or more repeaters it is placed at their weighted centroid,
  and its solid uncertainty circle is shown when it is selected. **Fit** zooms to everything; **Labels** turns the names on and off.
- **Selecting a beacon** (a click on the map or the list; Esc closes) draws the path of its last six hours of estimated positions and a
  line to each repeater that heard its latest transmission, and shows its counter, battery, who heard it with what RSSI and SNR,
  and its recent transmissions.

Names are display text announced over the air by anyone holding the channel key. The page shows them as text only; they identify
nothing.

## Where positions come from

`beacon-ingest` estimates a position for every transmission when a report arrives and stores it (the `positions` table). The estimator
is a **placeholder** ([plan/web-map.md](plan/web-map.md), decision 2): one located repeater gives a range circle, two or more a centroid
of the repeaters weighted by received power, which can never be outside the repeaters' own outline. Real estimation, tuned on walk
tests, is milestone 6. Only enabled repeaters with a location are used.

- `[estimate]` (`tx_power_dbm`, `path_loss_exponent`) set how far a given RSSI is taken to be. Tune them from a walk test.
- A position is worked out once. If a repeater gets its location after transmissions were stored (or `[estimate]` changed), run
  `beaconctl positions rebuild [--hours N]` to estimate them again.

## Map tiles with no internet

The Pi has no internet at the event, so tiles come from a file made beforehand: a **raster MBTiles** file (PNG, JPEG or WebP tiles;
vector `.pbf` tiles are refused). Set `web.tiles` to its path and restart `beacon-web`; the file is opened read-only and served as
`/tiles/{z}/{x}/{y}`. The page may be zoomed three levels past the file's last zoom (the tiles are stretched).

Without a file the map is a plain grid labelled with coordinates, which is enough to see the repeaters and beacons in relation to each
other. A file that is missing or unusable is logged and the map falls back to the grid, with the reason in the note under the lists.

To make a file, render your area at the zoom levels you need on a machine with internet, for example with QGIS's *Generate XYZ tiles
(MBTiles)* algorithm over a layer you may use, then copy it to the Pi. (Not tested here.) Check the provider's terms first:
OpenStreetMap's public tile servers do not allow bulk downloads. A builder for this (`beaconctl tiles`) is on the list in
[plan/beacon-base.md](plan/beacon-base.md), not built. `web.online_tile_url` is **for development only** (a machine with internet,
a tile URL template): it makes the page load images from that host and is off by default.

## Trying it without hardware

```bash
beaconctl simulate --provision --walk --interval 3     # fake companion; one beacon is carried round a course past three repeaters
beacon-ingest --port <the fake companion path it prints>
beacon-web                                              # http://127.0.0.1:8080/
```

Set `[clock] assume_synced = true` first, as for any development run. `--walk-step METRES` changes how far the walker moves per tick.
Select the first beacon on the map to see its track. The other simulated beacons report at random.

## API

JSON, read with `GET`, no token needed. Prefixes are 16 lowercase hex digits and are the only way to name a beacon (a short
reference, which `beaconctl` accepts, is a 404 here). Every response carries `now`, the base's clock.

| Request | Returns |
|---|---|
| `/api/state` | Everything the map shows: `health`, `repeaters`, `beacons` (state, counter, battery, latest position, reject summary), counts of beacons and repeaters seen but not trusted, `operator` (whether Reset works). The page polls this every 5 s. |
| `/api/health` | `ok`, the ingest heartbeat (state, companion, last frame and report), the clock, auto-add, and a `problems` list of what a banner would say. |
| `/api/beacons`, `/api/repeaters` | The two lists from `state`. |
| `/api/beacons/{prefix}?limit=50` | One beacon and its latest transmissions (counter, times, how many repeaters, battery, position). |
| `/api/beacons/{prefix}/track?hours=6&limit=200` | Its estimated positions, oldest first. |
| `/api/transmissions/{id}` | One transmission: each repeater's observation (RSSI, SNR, status) and where it was put. |
| `/api/rejects?beacon={prefix}&limit=50` | Recent observations that were not accepted, with the reason (as `beaconctl rejects`). |
| `POST /api/beacons/{prefix}/reset` | **Needs `Authorization: Bearer <token>`.** Clears the high-water mark; returns what was cleared. 403 when no token is configured, 401 for a wrong one. |
| `/api/map` | How to draw the background (`mbtiles`, `online` or `grid`). |
| `/api/openapi.json` | The schema FastAPI generates (the interactive `/docs` page is off: it would load its scripts from the internet). |

## Safety notes

- Everything the page shows is built as text, never as markup, and the server sends a `Content-Security-Policy` that allows scripts and
  styles from the page's own origin only (images from anywhere only when `web.online_tile_url` is set).
- The token is compared in constant time and is never logged; wrong guesses are not rate limited, so use the generated token (24
  random bytes), not a chosen one.
- The reset action does what `beaconctl beacon reset` does. It is the only write. Everything else (adding and removing beacons and
  repeaters, the clock, remote repeater management) stays in `beaconctl`.

## Not done yet

- **The Docker image and compose file do not include `beacon-web`.** The image installs the dependencies in a stage that runs on the
  build machine and copies them for the target CPU, which only works for pure-Python packages
  ([plan/service-packaging.md](plan/service-packaging.md), decision 4). FastAPI needs pydantic, which has compiled wheels, so the
  Dockerfile must install the web extra for the target platform (or the API be moved to Starlette, which is pure Python). To be done
  with the Pi run (B3.4), when the Pi's CPU architecture is known.
- A tile builder, and a walk test of the placeholder estimator on the hardware.
