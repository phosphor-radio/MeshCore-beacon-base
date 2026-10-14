# The map and API (`beacon-web`)

`beacon-web` shows what `beacon-ingest` has stored: a map with the repeaters and the beacons, banners for anything that needs
attention, and a small read-only API. It is a separate process that only reads the database (plus one operator action, reset), so it
can be stopped, restarted or left off without losing a report. The design is in [plan/web-map.md](plan/web-map.md).

## Running it

```bash
pip install -e '.[web]'        # Starlette and uvicorn; the '.[dev]' extra includes them
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

In the Pi's Docker deployment it is the compose file's second service, `web`, on port 8080 ([deployment.md](deployment.md), step 3.6): it publishes on every
interface by default, and the token is made with `./beaconctl web token generate` followed by `docker compose restart web`.

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

To make a file for your area from a regional OpenStreetMap extract (Geofabrik's Ontario file, say), see
[map-tiles.md](map-tiles.md): which tools, the steps, and the credit that has to stay on the map. **Do not build one by downloading
tiles from OpenStreetMap's servers:** their [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) prohibits bulk
downloading, pre-seeding areas and building tile archives such as `.mbtiles`. Render your own from the data instead.

`beaconctl tiles check [file]` says what a file holds (zoom levels with counts and sizes, metadata) and whether the map will show it
where your repeaters are. In particular, MBTiles specifies rows counted from the south while some tools (MapProxy among them) write them from
the north, and such a file shows no tiles at all. `web.tiles_scheme` (`auto`, `tms`, `xyz`) says which; `auto` reads the file's `scheme`
metadata, else tries both against the located repeaters and logs a warning if it had to read the file as XYZ.

`beaconctl tiles write-metadata FILE` makes a file ready: it renumbers rows written from the north and writes the metadata, with the
OpenStreetMap credit by default ([map-tiles.md](map-tiles.md), step 6).

The credit is the file's `attribution` metadata, shown in the map's corner; `web.tiles_attribution` overrides it, and the server logs a
warning if there is neither.

### Online tiles (development only)

`web.online_tile_url` (a `{z}/{x}/{y}` template) makes the page load tiles from a server on the internet, when there is no usable tiles
file. It is for a development machine and is off by default. The browser fetches the images itself, so the server's CSP allows images
from `https:` in this mode only.

If you point it at OpenStreetMap's public server (`https://tile.openstreetmap.org/{z}/{x}/{y}.png`), their
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/) applies, and a request that breaks it is answered with a
tile image saying *"403 Access blocked ... not following the tile usage policy"*
([Blocked tiles](https://wiki.openstreetmap.org/wiki/Blocked_tiles)). What the page does for it:

- **Sends a Referer.** The browser must send one, and a restrictive `Referrer-Policy` blocks it. `beacon-web` normally sends
  `Referrer-Policy: no-referrer` (nothing leaves the page), but with online tiles it sends `strict-origin-when-cross-origin`, and the
  tile layer is given the same policy. (Until 2026-10 it sent `no-referrer` here too, which is what produced the 403 tiles.) A privacy
  extension that strips the Referer will still cause the block: allow `tile.openstreetmap.org`.
- **Shows attribution** (OpenStreetMap contributors, with a link) in the map's corner. `web.online_tile_attribution` sets the text for
  another provider (HTML; empty gives none).
- **Does not prefetch, bulk download or cache tiles itself**, and does not strip the server's cache headers: the browser's own cache
  does the caching. Keep it to interactive viewing. A block lifts by itself once the cause is fixed; if it does not, see the Blocked tiles page.

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

## Safety notes

- Everything the page shows is built as text, never as markup, and the server sends a `Content-Security-Policy` that allows scripts and
  styles from the page's own origin only (images from `https:` only when `web.online_tile_url` is set, in which case it also sends a Referer, see above).
- The token is compared in constant time and is never logged; wrong guesses are not rate limited, so use the generated token (24
  random bytes), not a chosen one.
- The reset action does what `beaconctl beacon reset` does. It is the only write. Everything else (adding and removing beacons and
  repeaters, the clock, remote repeater management) stays in `beaconctl`.

## Not done yet

- A tile builder (`beaconctl tiles` makes no tiles itself: [map-tiles.md](map-tiles.md) describes the tools), and a walk test of the placeholder
  estimator on the hardware.
