# Web API and map (B4)

Implements phase B4 of [beacon-base.md](beacon-base.md): `beacon-web`, a read API plus a Leaflet map for the data
`beacon-ingest` stores, with the operator actions (reset) behind a token and tiles from a local MBTiles file. Dated
2026-10-10. Work is on the branch `b4-web-map`.

Status: **B4.1-B4.5 are built and tested on this branch** (821 tests; the page was driven in a browser against `simulate --walk` with
real `beacon-ingest` and `beacon-web` processes). **Left**: carrying `beacon-web` in the Docker image (below), and the hardware run: real
beacons walked past real repeaters, with real tiles. Guide: [../web.md](../web.md).

## Decisions

| # | Decision |
|---|---|
| 1 | **The estimator runs in ingest, inside the report's transaction**, and writes a `positions` row per transmission (migration 3), as the base plan says. The web process only reads. A beacon's track is therefore stable history, and `beacon-web` needs no estimator code. A repeater that gets its position after a transmission was estimated does not change old estimates (the next transmission uses it). |
| 2 | **Placeholder estimator** (`estimate.py`, pure): one located repeater gives a *range circle* around it (radius from the RSSI through a log-distance path loss model); two or more give a *weighted centroid* (linear power weights), with a radius from the spread of the repeaters. Both are marked in `method` so the UI can draw them differently. Real estimation and tuning against walk tests is milestone 6. Unlocated (0, 0), disabled and unknown repeaters are never used (`Store.located_repeaters` rules). |
| 3 | **Static files live inside the package** (`src/beacon_base/static/`, package data), not in a top-level `web/`, so `pip install .` (the Docker image) carries them. Leaflet is vendored there; the page makes no request to any other host. |
| 4 | **FastAPI and uvicorn are an optional extra** (`pip install '.[web]'`), so an ingest-only install stays small. `beacon-web` explains what to install when they are missing. |
| 5 | **The token** lives in `secrets.toml` (`[web] token`, mode 0600, next to the channel key) or in `$BEACON_WEB_TOKEN`, and is sent as `Authorization: Bearer`. Without a token configured, operator actions are refused (the page shows read-only). The page keeps the token in `sessionStorage` only. Compared in constant time. |
| 6 | **Binds to 127.0.0.1 by default** (`[web] host`, `port`). Serving the LAN is an explicit choice; the token then travels in clear over plain HTTP, which the guide says. |
| 7 | **Operator actions are limited to reset** (the Reset buttons of the rejected banner), as the plan says. Everything else stays in `beaconctl`. |
| 8 | **Ages are computed from the server's clock.** Every response carries `now`; the browser's clock (a phone) is not trusted, and the Pi's own clock may be provisional, so the page also shows the "clock not set" banner. |
| 9 | **Tiles**: raster MBTiles (`[web] tiles`) served as `/tiles/{z}/{x}/{y}` (XYZ in the URL, flipped to TMS for the file). Without a file, a plain grid; `[web] online_tile_url` is a development option that is off by default. A tile cache builder stays in "Later". |

## Steps (all done unless marked)

Each ends with something runnable and committed on the branch.

- **B4.1 Estimator and positions.** `estimate.py`, migration 3 (`positions`), the pipeline calls the estimator for every accepted
  observation, `[estimate]` config (transmit power, path loss exponent). Tests: estimator table tests, pipeline writes and updates
  positions, unlocated and disabled repeaters excluded, a reset starts a new epoch.
- **B4.2 Read API.** `views.py` (store to JSON-able data, no HTTP), `api.py` (FastAPI app), `web.py` (`beacon-web`), `[web]` config,
  the token. Tests with the FastAPI test client.
- **B4.3 Tiles.** `tiles.py` (MBTiles reader), `/tiles`, `/api/map`.
- **B4.4 Map page.** `static/` page: repeaters, beacons, track, banners, Reset. Needs Leaflet vendored.
- **B4.5 Walk simulation and docs.** `beaconctl simulate --walk` (a beacon walked past located repeaters, RSSI from distance), the guide
  ([../web.md](../web.md)), the README, CLAUDE.md and the plans updated. *Not done:* the compose file and image (see "Left, and why").

## Done when

The map follows a beacon being walked past repeaters with no internet: shown here with `simulate --walk`, and then on the hardware
(not part of this branch's tests).

## What building it found

- **Leaflet reads a string tooltip as markup.** Names come from the air and `clean_name` keeps `<` and `>`, so a beacon named
  `<img src=x onerror=...>` would have been inserted as an element (the page's CSP would have stopped the script, but that is the second
  line of defence). Every tooltip is given an element holding text; `tests/test_static.py` fails on one that is not. Checked in the browser with a
  hostile name planted in the database.
- **The pane may be laid out after the map is made** (a hidden tab, a phone turned round): the map followed the size 0 it was created at
  and was fitted to nothing. A `ResizeObserver` re-measures and, until the map has been touched, fits again.
- **The heartbeat's "last report" is written every 15 s**, so "last report 17 s ago" was shown while reports arrived every 3 s. The health
  view uses the newest stored frame instead.
- **A centroid cannot leave the repeaters' outline**, so a walker outside the course reads as nearer than it is. This is the placeholder's
  nature (the median error on `simulate --walk` is a few hundred metres on a 500 m grid), not a bug; milestone 6 is where to improve it.
- **Large uncertainty circles hid the map**, so the circle of a centroid is drawn for the selected beacon only; a range circle (one
  repeater: its whole position) is always drawn.
- **Leaflet fetches its default marker image as soon as a marker is added**, even if it is replaced a line later; the 404s are avoided by
  giving the marker its icon at creation.

## Left, and why

- **Docker.** The image installs its dependencies in a stage that runs on the build machine and copies them to the target CPU
  ([service-packaging.md](service-packaging.md), decision 4), which needs pure-Python packages. FastAPI brings pydantic, which has
  compiled wheels (`pydantic-core`), so that stage would copy the wrong CPU's files into an arm image. Options: install the web extra
  with `pip install --platform ... --only-binary=:all:` for the target (needs the architecture, `uname -m` on the Pi, and a network
  at build time), or write the API on Starlette, which FastAPI is built on and which is pure Python (it also removes pydantic from a 512 MB
  Pi). Either is small; do it with B3.4, then add a `web` service to the compose file (publish `127.0.0.1:8080` unless the
  operator asks for the LAN) and run `tests/test_docker.py`. Not done here because the image cannot be built or run for the Pi from this machine.
- **A tile builder** (`beaconctl tiles`), which was already in "Later" in [beacon-base.md](beacon-base.md).
- **Tuning the estimator** on a walk with the real hardware.
