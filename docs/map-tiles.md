# Making offline map tiles from OpenStreetMap data

`beacon-web` draws its map from a **raster MBTiles file** ([web.md](web.md)), because the Pi has no internet at the event. This guide
is how to make one for a small area from a regional OpenStreetMap extract such as Geofabrik's
[Ontario](https://download.geofabrik.de/north-america/canada/ontario.html), on a machine that has internet, before the event.

> **Status: verified (route A).** The whole of route A below was run end to end for this project on 2026-10-13: an Ontario extract cut to a
> 10 km by 6 km area, imported, seeded to zoom 14 to 19, made ready with `beaconctl tiles write-metadata`, and then shown by `beacon-web`
> with no internet connection. Running it found the row-numbering fault in step 5 and the empty metadata (so no credit), both now handled
> by step 6. Routes B and C are outlines from the tools' documentation and have **not** been run; a detail
> marked **(check)** there is one I could not confirm.

## What `beacon-web` needs

- An MBTiles file (a SQLite database) of **raster** tiles: PNG, JPEG or WebP. Vector tiles (`.pbf`) are refused.
- A `tiles` table in the usual MBTiles layout, and a `metadata` table. The page reads `format` and, if present, `minzoom`, `maxzoom`,
  `bounds` (`west,south,east,north`), `name` and `attribution`. A file with no `metadata` table is refused.
- The zoom levels you will actually use: for an event field, about 12 to 17. The page can be zoomed three levels past the file's last
  level (the tiles are stretched), so a file that stops at 17 still zooms to 20, blurred.

## Credit and licence: what has to stay

OpenStreetMap data is under the [ODbL](https://opendatacommons.org/licenses/odbl/1-0/). Tiles drawn from it are a derived picture of
the data and must carry the credit. The OSM Foundation's
[attribution guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines) say, in short: name OpenStreetMap, make it clear the
data is under the ODbL (a link to `https://www.openstreetmap.org/copyright` does that), show it where people see it without having to
click (a corner of an interactive map, any corner), and keep the licence and origin reachable. "© OpenStreetMap contributors" is
accepted wording. Geofabrik asks for its own line on data it processes: "Data processed by Geofabrik GmbH and created by OpenStreetMap
Contributors."

How this project keeps the credit:

- **On the page.** The map shows the file's `attribution` metadata in its bottom-right corner (it is HTML). If the file has none,
  `beacon-web` logs a warning and the map shows no credit, so **always write the attribution into the file** (`beaconctl tiles write-metadata`, step 6) or set
  `web.tiles_attribution` in the configuration, which replaces whatever the file says.
- **In the code and docs.** The README has a *Credits* section naming OpenStreetMap, the ODbL and Geofabrik. Keep it. Anything that
  ships tiles or an extract (a deployment bundle, a repository) should carry the same notice.
- **The style.** The OpenStreetMap Carto stylesheet used in the recommended route is CC0, so it adds no condition. I did not check the
  licences of the fonts and symbol sets the Docker image bundles (**check** before redistributing the image or its files). The
  OpenMapTiles schema used by route B has its own required credit, "OpenMapTiles".

Do not fill an MBTiles file by downloading tiles from OpenStreetMap's own servers or another provider's. OSM's
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/) prohibits bulk downloading, pre-seeding and tile archives, and
this guide is the legitimate way round that: render the tiles yourself from the data.

## Which tools

There is no single tool that goes from a `.osm.pbf` to a raster MBTiles file. Raster tiles come from a renderer that applies a style to the
data; the choice is which renderer and which style.

| | Route | Style | Moving parts | Fits when |
|---|---|---|---|---|
| **A (recommended)** | `osmium extract` → the `overv/openstreetmap-tile-server` Docker image (osm2pgsql, PostGIS, renderd, Mapnik, OpenStreetMap Carto) → **MapProxy** seeds a raster MBTiles file | The familiar OpenStreetMap look, CC0 | Docker, Python | You want the standard map, once, with tools that are well documented and need no styling work. |
| B | `planetiler` (vector MBTiles, OpenMapTiles schema) → `tileserver-gl` renders rasters from a style → MapProxy seeds MBTiles | Any MapLibre/Mapbox GL style (OpenMapTiles styles), you choose | Java, Node (native build), a style with fonts and sprites | You want a different look (a plain, light basemap that lets the beacons stand out) or a quick import of a large area. |
| C | **QGIS**: load the extract (or Geofabrik's shapefile/GeoPackage download) and run *Generate XYZ tiles (MBTiles)* | Whatever you style by hand | One desktop application | A small area, a hand-made look (for example the event site picked out), no servers. The styling is the work. |
| D (not built) | Vector MBTiles drawn in the browser by MapLibre GL JS | Any | Changes to `beacon-web`: a vector renderer, glyphs and sprites served locally | Later, if rasters prove too big or too plain. The archive is much smaller. |

Route A is written out below. B and C are summarised after it.

## Route A, step by step

You need Docker, `osmium-tool` (`apt install osmium-tool` on Debian and Ubuntu), Python 3 and enough disk for the extract and the
database. Work in a scratch directory outside the repository.

### 1. Choose the area and the zoom levels

Take a bounding box `west,south,east,north` (longitude and latitude, in that order) around the repeaters with a margin of a kilometre or
two, so tiles at the edge are not cut short and a walker just outside the course is still on the map. The positions are in
`beaconctl repeater list`.

Count the tiles before you start. Each zoom level has about four times the tiles of the one before:

```python
import math

def tile(lat, lon, z):
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

def count(west, south, east, north, zmin, zmax):
    total = 0
    for z in range(zmin, zmax + 1):
        x0, y0 = tile(north, west, z)
        x1, y1 = tile(south, east, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total

print(count(-75.7256, 45.4020, -75.6744, 45.4380, 12, 17))   # about 4 km by 4 km: 534 tiles
```

That 4 km square is 534 tiles for zoom 12 to 17 (1, 4, 9, 30, 110 and 380 per level); level 18 would add 1,482. The area that was
actually run, about 10 km by 6 km at zoom 14 to 19, came to this:

| Zoom | 14 | 15 | 16 | 17 | 18 | 19 | Total |
|---|---|---|---|---|---|---|---|
| Tiles | 24 | 84 | 336 | 1,316 | 5,170 | 20,570 | 27,500 |
| Size (MB) | 0.2 | 0.6 | 1.7 | 4.8 | 13.5 | 42.0 | 91 |

Each level has about four times the tiles of the one before and about three times the size, so the last level or two is nearly all of
the file: dropping zoom 19 would have made it about a third of the size. A tile averaged 3 KB (2 KB at zoom 19, where many tiles show
little). Work out the count for your own area, add up what you can afford to carry, and choose the highest zoom from that. To read the
coordinates of a box you have in mind, OpenStreetMap's [export page](https://www.openstreetmap.org/export) shows them as you draw it.
Rendering time grows with the tile count and with how dense the area is.

### 2. Download the regional extract and cut out your area

```bash
curl -LO https://download.geofabrik.de/north-america/canada/ontario-latest.osm.pbf
osmium extract --bbox=-75.7256,45.4020,-75.6744,45.4380 --strategy=smart ontario-latest.osm.pbf -o area.osm.pbf
```

Geofabrik's page lists `ontario-latest.osm.pbf` at about 1 GB, rebuilt daily, with the time the data runs up to. Its public files leave out
user names and changeset IDs. `osmium extract` takes `-b`/`--bbox` as left,bottom,right,top, and the `smart` strategy also keeps
whole multipolygons (lakes, parks, buildings) that touch the box; it reads the input three times, so allow a few minutes. A `.poly` file works instead of a box (`--polygon`) if the area is not a rectangle.

Cutting first matters: the import in the next step then handles a few megabytes, not a gigabyte, and finishes in minutes.

### 3. Import the extract into the tile server image

```bash
docker volume create osm-data-event
docker run \
    -v "$(pwd)/area.osm.pbf:/data/region.osm.pbf" \
    -v osm-data-event:/data/database/ \
    overv/openstreetmap-tile-server \
    import
```

These are the commands from the image's README. The container exits quietly when the import is done, and the import needs internet (it
fetches the style's auxiliary data); running the server afterwards does not. `OSM2PGSQL_EXTRA_ARGS="-C 4096"` raises the import's
memory cache from 800 MB if you hit memory errors. Do not set `UPDATES=enabled`: you want a still picture of the data on the day.

### 4. Run the tile server

```bash
docker run --name osm-tiles --shm-size="192m" \
    -p 8081:80 \
    -v osm-data-event:/data/database/ \
    -d overv/openstreetmap-tile-server \
    run
```

Port 8081, because `beacon-web` uses 8080. The README suggests `--shm-size="192m"` against "could not resize shared memory segment" errors
(Docker's default is 64 MB). Open `http://localhost:8081/` for a demo map, and check one tile in your area:
`http://localhost:8081/tile/15/9493/11733.png` (the zoom 15 tile at the centre of the example box; `tile()` from step 1 gives the address for any point). The server renders a tile when it is first
asked for, so the first requests are slow; this is what the next step takes advantage of.

### 5. Seed an MBTiles file from the local server

[MapProxy](https://mapproxy.org/)'s `mapproxy-seed` walks the tile pyramid of an area and fetches every tile from a source into a cache,
and a cache can be an MBTiles file. The source is your own server on `localhost`, so nothing here touches anyone else's.

```bash
python3 -m venv seed-venv && . seed-venv/bin/activate
pip install MapProxy
```

`mapproxy.yaml`:

```yaml
services:
  tms:

layers:
  - name: area
    title: Event area
    sources: [area_cache]

caches:
  area_cache:
    grids: [GLOBAL_WEBMERCATOR]
    sources: [local_osm]
    format: image/png
    cache:
      type: mbtiles
      filename: ./area.mbtiles

sources:
  local_osm:
    type: tile
    url: http://localhost:8081/tile/%(z)s/%(x)s/%(y)s.png
    grid: GLOBAL_WEBMERCATOR
```

`GLOBAL_WEBMERCATOR` is MapProxy's name for the usual web map grid, numbered from the north-west corner as OpenStreetMap's tile URLs
are. `seed.yaml`:

```yaml
seeds:
  area:
    caches: [area_cache]
    grids: [GLOBAL_WEBMERCATOR]
    coverages: [site]
    levels:
      from: 12
      to: 17

coverages:
  site:
    bbox: [-75.7256, 45.4020, -75.6744, 45.4380]
    srs: 'EPSG:4326'
```

```bash
mapproxy-seed -f mapproxy.yaml -s seed.yaml -c 2     # -n for a dry run, --summary to list the tasks
```

> **MapProxy numbers the rows of its MBTiles cache from the north (XYZ), not from the south as the MBTiles specification says.** It
> stores each tile under its grid's own row, and `GLOBAL_WEBMERCATOR` counts from the north-west corner. A file seeded this way is correct
> for MapProxy and wrong for anything that follows the specification: read the usual way it shows no tiles at all (every request is a
> 404). `beacon-web` notices this by itself where it can (see step 7), and step 6 converts the file so that nothing has to.

`-c` is the number of concurrent workers; match it to the server's `THREADS` (4 by default). If the run is interrupted, use
`--progress-file` and `--continue` to resume. It walks the pyramid depth first, skipping tiles outside the coverage.

> Alternative without MapProxy: the image's `render_list` renders into renderd's own "meta tile" files and Geofabrik's `meta2tile`
> converts them to MBTiles (a write-up of that route: [Creating self-hosted tile maps from OpenStreetMap data](https://terinstock.com/post/2023/06/Creating-Self-Hosted-Tile-Maps-from-OpenStreetMap-Data/)).
> It means compiling `meta2tile`; the seed route above does not.

### 6. Make the file ready for the map

```bash
beaconctl tiles write-metadata area.mbtiles
```

One command does both jobs, in one transaction (a failure leaves the file as it was):

- **Rows.** MapProxy numbers its tile rows from the north; the MBTiles specification says from the south. If the file is numbered from the
  north the command renumbers it, and reports it (`rows  RENUMBERED from the north (XYZ) to the south (TMS)`). It works out which it is from the
  file's own `scheme` metadata, else from where your **located repeaters** are (so add them first), and stops with a message rather than guess
  when it cannot tell; then say `--rows xyz` or `--rows tms`. A file already numbered from the south is left alone, so running it again is safe.
- **Metadata.** It writes the `metadata` table the map uses: `minzoom`, `maxzoom`, `bounds` and `format` from the tiles themselves, `name`
  (from `--name`, else the file's, else the file name), `description`, `scheme = tms`, and the **attribution**: by default
  `© OpenStreetMap contributors` with a link to the licence, which is the credit the map must show. `--attribution TEXT` (HTML) and
  `--description TEXT` change them; other metadata the file already has is kept.

It prints the whole metadata table afterwards. `--dry-run` shows what it would do and changes nothing. Stop `beacon-web` while it runs, or
restart it afterwards; the file is rewritten, so keep a copy of the original if the seed took long. Then look at the result:

```bash
beaconctl tiles check area.mbtiles      # zoom levels with tile counts and sizes, and whether the map will show it: should end with "ok"
```

The tile counts should match your count from step 1; a level with far fewer tiles means the seed did not finish.

### 7. Use it

Copy `area.mbtiles` next to the base's configuration and set it:

```toml
[web]
tiles = "area.mbtiles"      # relative to the config file's directory
```

If the file is not numbered as the specification says (a MapProxy file that was not converted, or one from another tool that does the same),
`beacon-web` reads the rows the right way round by itself when it can tell from where your located repeaters are, and logs a warning
saying so; `web.tiles_scheme = "xyz"` states it outright, and `"tms"` does the opposite. A file that follows the specification needs
nothing.

In the Pi's Docker deployment the file goes into `data/` and the path is relative to the configuration, which the container sees as `/data`, so
`tiles = "area.mbtiles"` finds `data/area.mbtiles`; run the commands of step 6 with the container's path (`./beaconctl tiles write-metadata
/data/area.mbtiles`) and `docker compose restart web` ([deployment.md](deployment.md), step 3.6).

Restart `beacon-web`, open the map, and check that: the tiles are drawn, with no 403s or blank squares; the credit is in the bottom-right
corner; and zooming to the lowest and highest levels still shows tiles. A log line says what was opened (`map tiles from ... (zoom 12-17, rows read as tms: ...)`),
or why not. Unplug the network for the real test.

### 8. Clean up

```bash
docker rm -f osm-tiles
docker volume rm osm-data-event     # the database; keep it if you will render the same data again
```

For the next event, start again at step 1: the extract is cheap to cut, and a fresh download picks up the latest edits.

## Routes B and C in outline

**B: a different look, via vector tiles.** `planetiler` builds a vector MBTiles file in the OpenMapTiles schema from an extract in a few
minutes (it needs Java 21 or Docker; its documentation puts RAM at about half the extract's size and disk at 5 to 10 times):
`java -Xmx1g -jar planetiler.jar --osm-path=area.osm.pbf --output=area-vector.mbtiles` (`--osm-path` and `--output` are as its README
shows; its README also has `--download --area=<geofabrik name>` to fetch an extract by itself). `--bounds` and `--polygon` limit the area
**(check with `--help`)**, though cutting with `osmium` first, as above, avoids relying on them. To get rasters out, serve that file with
`tileserver-gl` (the full package: the `-light` one cannot render rasters) and a style JSON for the schema; its endpoint is
`/styles/{id}/{z}/{x}/{y}.png`. Seed those into MBTiles with MapProxy exactly as in step 5, pointing `url:` at that endpoint. The extra
work is the style, its fonts and sprites, none of which this guide has tried. Credit: OpenStreetMap, and also OpenMapTiles (planetiler's
README says maps made with its OpenMapTiles profile must credit it). `tilemaker` is an alternative to planetiler that also writes vector MBTiles.

**C: QGIS.** Open the `.osm.pbf` (QGIS reads it through GDAL; the lines and multipolygons layers hold the roads, buildings and
land use) or Geofabrik's `-free.shp.zip` / `-free.gpkg.zip` downloads (about 2 GB for Ontario: clip to your area first), style the layers,
then run Processing's *Generate XYZ tiles (MBTiles)*: choose the extent, minimum and maximum zoom (it defaults to 12 and 12),
DPI (96), tile format (PNG or JPG), background colour (transparent unless set) and metatile size (4; larger renders faster
with fewer cut-off labels but needs more memory). It renders the current project, so the style is whatever the project shows. Then do
step 6 for the metadata. QGIS is the easiest way to try a map style on a small area, and the least repeatable for the next event unless you save
the project.

## If something looks wrong

| Symptom | Likely cause |
|---|---|
| Every tile is a 404 (the map is blank, the log shows `GET /tiles/...` returning 404) | The rows are numbered from the north, which is how MapProxy writes them. Run `beaconctl tiles check area.mbtiles`; `beaconctl tiles write-metadata area.mbtiles` (step 6) converts the file. |
| `beacon-web` says the file is "not an MBTiles file" | No `metadata` or `tiles` table: do step 6; check that seeding wrote to `area.mbtiles` and not another file. |
| The map is a grid, with a note about vector tiles | The file's `format` is `pbf`: it came from planetiler or tilemaker directly. Render rasters from it (route B). |
| Tiles missing at one zoom level | The seed stopped early, or its levels did not cover that zoom: compare step 6's count with step 1's. |
| Blank or half-drawn tiles at the edge of the area | The extract was cut too tight: cut a larger box, import again. |
| Import fails with memory or shared-memory errors | `OSM2PGSQL_EXTRA_ARGS="-C 4096"` on `import`; `--shm-size` on `run`. |
| Tiles are slow to appear while seeding | They are rendered on first request. Wait, or lower `-c`. |
| No credit on the map | The file's metadata has no `attribution` and `web.tiles_attribution` is empty (step 6). `beaconctl tiles check` says so. |

## Sources

[Geofabrik Ontario downloads](https://download.geofabrik.de/north-america/canada/ontario.html) ·
[osmium extract manual](https://osmcode.org/osmium-tool/manual.html) ·
[overv/openstreetmap-tile-server](https://github.com/Overv/openstreetmap-tile-server) ·
[OpenStreetMap Carto](https://github.com/openstreetmap-carto/openstreetmap-carto) (CC0) ·
[MapProxy seeding](https://mapproxy.github.io/mapproxy/latest/seed.html) ·
[planetiler](https://github.com/onthegomap/planetiler) ·
[tilemaker](https://github.com/systemed/tilemaker) ·
[TileServer GL endpoints](https://tileserver.readthedocs.io/en/latest/endpoints.html) ·
[QGIS: Generate XYZ tiles (MBTiles)](https://docs.qgis.org/latest/en/docs/user_manual/processing_algs/qgis/rastertools.html#generate-xyz-tiles-mbtiles) ·
[OSMF attribution guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines) ·
[OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/)
