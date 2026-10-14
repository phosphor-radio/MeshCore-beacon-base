# beacon-web on Starlette, in the Docker image (B4 follow-up)

Dated 2026-10-13. Follows [web-map.md](web-map.md) ("Left, and why") and [service-packaging.md](service-packaging.md). The map and API are on
`main`, built on FastAPI and run from a checkout; this puts them in the image and compose bundle that go to the Pi (a Pi Zero 2 W, **arm64**),
and moves the API from FastAPI to Starlette so that the image's build stays pure Python.

Status: **W1-W4 built and tested here (2026-10-13); W5, the run on the Pi, is to do.** The open questions were answered the same day; the answers are in
"Decisions" below and the original questions are kept at the end.

## Why Starlette

The image installs its dependencies in a stage that runs on the *build* machine and copies them into a base image of the *target* CPU, so an
arm64 image builds on an x86 machine with nothing emulated ([service-packaging.md](service-packaging.md), decision 4). That only works if every
package is pure Python. FastAPI needs pydantic, whose core is compiled, so the build stage would copy the wrong CPU's files.

Checked 2026-10-13 with pip's resolver asked for arm64 (`--platform manylinux2014_aarch64 --only-binary=:all:`, Python 3.13): the whole stack
Starlette needs is pure Python.

| Package | Wheel |
|---|---|
| starlette 1.8.0 | `py3-none-any` |
| uvicorn 0.54.0 | `py3-none-any` |
| anyio 4.15.1, click 8.5.0, h11 0.16.0, idna 3.20, typing_extensions 4.16.0 | `py3-none-any` |

Two cautions that follow: install `uvicorn`, **not** `uvicorn[standard]` (that adds `uvloop` and `httptools`, which are compiled); and a test
(below) fails the image build if any compiled library ever appears in it. It also takes FastAPI and pydantic out of the process: importing the web
stack takes about 31 MB resident with Starlette and 48 MB with FastAPI (measured here, an x86 machine on Python 3.14, peak RSS after the imports; the Pi will
differ), a difference that matters more on a 512 MB Pi.

## Decisions

| # | Decision |
|---|---|
| 1 | **Yours: Starlette, not FastAPI.** Recorded against decision 1 of [beacon-base.md](beacon-base.md), which says FastAPI. Same URLs, JSON and status codes, so the page and the API table in [../web.md](../web.md) do not change. |
| 2 | **Yours: the Pi is arm64.** The bundle script already builds `linux/arm64` (and `arm/v7`); only arm64 is tested. |
| 3 | **`views.py` and everything below it do not change.** Only `api.py` (the HTTP layer) is rewritten, `web.py` keeps its flags, and the tests that drive the API keep their assertions. They are the safety net for the move. |
| 4 | **The route table is plain Starlette**: `Route` objects, ordinary `def` handlers (Starlette runs them in its thread pool, as FastAPI did), a connection per request, `HTTPException` and exception handlers for the errors, `StaticFiles` and `FileResponse` for the page, and a small pure-ASGI middleware for the security headers (not `BaseHTTPMiddleware`). |
| 5 | **Validation is a few helpers**, replacing FastAPI's: integer and float query values with bounds return the same `422 {"detail": ...}`. A non-numeric `{transmission_id}` no longer matches its route and is a 404 (it was a 422): nothing relies on it. |
| 6 | **One image, two compose services.** (Asked as "one container or two services?"; the answer "one container is fine" was read as one *image*, since `docker compose restart web` was approved in the next answer, which needs a service of its own.) `ingest` as now, and a new `web` from the same image (command `web`). They share `./data` and the SQLite file, as `beaconctl` and ingest already do; WAL is safe across containers on one host. A crash or restart of one cannot take the other down. |
| 7 | **`web` is not given the serial device**, the host's `/dev` or the cgroup rules; it needs only `./data` (the database to read, to write a reset to, the config, the secrets and the tiles file). |
| 8 | **The image stays pure Python and a test keeps it so**: after the build, no `*.so` under `/opt/site`. |
| 10 | **The web port is published on every interface by default** (`BEACON_WEB_BIND=0.0.0.0`, port `BEACON_WEB_PORT=8080`, both in `.env`): the map is read from another machine, and the security of the read-only map is not a concern. Reset still needs the token. (Recommendation was `127.0.0.1`; `BEACON_WEB_BIND=127.0.0.1` gives it.) |
| 11 | **After `./beaconctl web token generate`, `docker compose restart web`.** The token is read when the web starts. |
| 12 | **A non-numeric transmission number is a 404** (the route does not match), not a 422. **The dev test client is `httpx2`.** `mem_limit: 128m` for `web`, and the memory cgroup is on as a step of preparing the Pi, so the limits apply. |
| 9 | **The OpenAPI schema (`/api/openapi.json`) goes**: it was FastAPI's, nothing uses it, and the API table in the guide is the contract. |

## What changes

**W1. The API on Starlette** (`api.py`, `pyproject.toml`, tests). The `web` extra becomes `starlette` and `uvicorn`; the `dev` extra's test client
becomes `httpx2` (Starlette 1.7 and later needs it and deprecates `httpx`). The test imports move from `fastapi.testclient` to
`starlette.testclient`. Done when `tests/test_api.py`, `test_views.py`, `test_tiles.py` and `test_web_main.py` pass with FastAPI and pydantic
uninstalled.

**W2. The image and compose file** (`deploy/docker/`).
- `Dockerfile`: install `".[web]"` in the build stage (nothing else changes); add the `beacon-web` wrapper next to `beacon-ingest`.
- `entrypoint.sh`: a `web` mode that waits, as `serve` does, until `./data` is writable and the configuration exists (the web needs neither
  the serial port nor the channel key), then runs `beacon-web --host 0.0.0.0` (inside the container the config's `web.host` means nothing; what
  limits who can reach it is where compose publishes the port).
- `compose.yaml`: the `web` service (the same user and group as `ingest`, `./data:/data`, `restart: unless-stopped`, `init`, a small
  `mem_limit`, the log options, a healthcheck that asks `/api/health` from inside with Python's `urllib`, no `curl` in the image) and its port:
  `${BEACON_WEB_BIND:-127.0.0.1}:${BEACON_WEB_PORT:-8080}:8080`.
- `.env.example` and `BUNDLE-README.txt`: the two new variables. `build-bundle.sh` needs no change (the pure-Python stage is why).
- `smoke-test.sh`: also starts `beacon-web` in its throwaway container and checks that `/api/state` lists the simulated beacons.

**W3. Tests** (`tests/test_docker.py`, opt-in with `BEACON_DOCKER_TESTS=1`, and unit tests). The compose file has the `web` service with the
settings above and without the device mounts; `web` waits for a configuration, then answers `/api/health`; ingest and web run together and the
map shows what ingest stored; a reset through the API (with a token in `secrets.toml`) is seen by `beaconctl status`; **no `*.so` in the image**;
and the image builds for `linux/arm64` on this x86 machine with nothing emulated. Unit: the validation helpers, the 422/404/403/401/405 cases.

**W4. Docs.** [../deployment.md](../deployment.md): opening the map, making it reachable from a laptop or phone (`BEACON_WEB_BIND`), the
token (`./beaconctl web token generate`, then `docker compose restart web`), putting the tiles file in `./data`, `./beaconctl tiles write-metadata`
in the container. [../web.md](../web.md): the "not done yet" Docker note goes, the OpenAPI row goes. [map-tiles.md](../map-tiles.md): step 7 for the
container. `README.md`, `CLAUDE.md`, [web-map.md](web-map.md) ("Left, and why" is resolved), and [service-packaging.md](service-packaging.md): its
decision 15 says "no extra containers" and is no longer true; it becomes "one image, two services".

**W5. The Pi (yours).** Build the arm64 bundle, load it, `./smoke-test.sh`, `docker compose up -d`, open the map from a laptop, `web token
generate`, restart `web`, reset a beacon. Record what the web process weighs (`ps -eo rss,args | grep beacon_base.web`, since the memory cgroup is
off on Raspberry Pi OS and `docker stats` shows nothing, see service-packaging.md) and that unplugging the companion leaves the map up.

## Risks

- **Two containers on one SQLite file.** WAL needs the processes to share a host (they do) and a filesystem that supports shared memory maps
  (the bind-mounted `./data` on the Pi's ext4 does). The first start can have either process create the database; both migrate under a
  lock, as `beaconctl` and ingest already do.
- **Liveness across containers.** The heartbeat is judged on the monotonic clock and the boot id, which containers share with the host
  ([service-packaging.md](service-packaging.md), decision 11), so the map's "ingest is down" banner works from the web container.
- **Behaviour drift in the rewrite.** The tests above are the net; the page is also looked at in a browser once more, after.
- **A clear-text token.** Publishing the port on the LAN sends the Reset token in the clear (plain HTTP). The guide says so where it says
  how to publish.

## Open questions

1. **Which host address does compose publish the port on by default?** *Recommendation: `127.0.0.1`* (decision 6 of web-map.md: serving the LAN is
   a choice), with the first-start checklist telling the operator to set `BEACON_WEB_BIND=0.0.0.0` (or the Pi's address) for other machines, and
   saying why. The alternative, `0.0.0.0` by default, is friendlier on a private event network but makes the map readable by anyone who joins it.
2. **One container running both processes, or two services?** *Recommendation: two services* (decision 6): independent restarts and limits, and
   the web can stay down without affecting ingest.
3. **After `web token generate`, should the web need a restart to see the token?** *Recommendation: yes, document `docker compose restart web`*:
   it is the same for every other setting. (The alternative is reading the secrets file on each reset request, which is a small code change
   and a second place that reads secrets.)
4. **Drop `/api/openapi.json`?** *Recommendation: drop* (decision 9).
5. **A non-numeric transmission number becomes a 404, not a 422.** *Recommendation: accept.*
6. **Switch the dev test client to `httpx2`?** *Recommendation: yes*; Starlette 1.7 and later requires it, and `httpx` already prints a
   deprecation warning in the current suite.
7. **May I pull the images to build and test here?** It needs `python:3.13-slim` for amd64 and arm64 (about 50 MB each) from Docker Hub and the
   packages from PyPI. *Recommendation: yes*; without it the Docker work is untested here, like the first draft of the B4 Docker notes was. An
   arm64 *run* here needs QEMU; whether it is set up on this machine I do not know, and the arm64 test is a build plus the no-compiled-files check
   either way.
8. **`mem_limit` for `web`?** *Recommendation: `128m`*, which is generous for Starlette and uvicorn and, like `ingest`'s 256 MB, is not enforced
   on a stock Raspberry Pi OS until the memory cgroup is turned on.

## What was done and tested (2026-10-13, on this x86 machine)

- **W1.** `api.py` is plain Starlette; `views.py` is untouched. The web tests (98 before, now more) pass with FastAPI, pydantic and `httpx` uninstalled from the
  virtualenv, and no longer print the `httpx` deprecation warning. New tests cover the validation helpers (422 with a message, the bounds,
  `nan` and `inf`), every error being `{"detail": ...}` with the security headers, `HEAD`, a busy database being a 503, and that no module imports FastAPI or pydantic.
  `beacon-web --check` reads the configuration and opens nothing (the container waits on it).
- **W2.** The image installs `".[web]"`; the build stage **fails if any `*.so` or `*.pyd` is in `/opt/site`**. Result: seven pure-Python packages (anyio, click,
  h11, idna, starlette, typing_extensions, uvicorn) plus pyserial and this package, 7.7 MB, zero compiled files, in an image of 186 MB. The `web` service,
  `.env.example`, the bundle readme and `smoke-test.sh` (which now also asks the map's API for the simulated beacons) are done.
- **W3.** `tests/test_docker.py`: 8 new tests, 16 of 16 pass, covering the compose settings and the default publish address, the map beside ingest and reading what
  ingest stores through one SQLite file in two containers, the web waiting for a configuration, the token and restart flow with a reset written by the web
  container, the offline-tiles workflow (a file numbered from the north, `write-metadata`, `web.tiles`, restart), no compiled library in the image, and the
  `linux/arm64` build. `smoke-test.sh` passes on the amd64 image.
- **arm64:** `docker buildx build --platform linux/arm64` succeeds on this machine with nothing emulated; the resulting root filesystem has an `aarch64` Python
  (`ELF 64-bit ... ARM aarch64`) and no compiled library under `/opt/site`. **Not run:** QEMU is not set up here (only Python is registered in `binfmt_misc`), so
  the arm64 image has never been *run* here.
- **W4.** Docs: [../deployment.md](../deployment.md) (the memory cgroup as a step of preparing the Pi, step 3.6 opening the map, the token, tiles, troubleshooting rows),
  [../web.md](../web.md), [../map-tiles.md](../map-tiles.md), the README, CLAUDE.md, [web-map.md](web-map.md), [service-packaging.md](service-packaging.md) (decision 15 and the cgroup note).

## To do: W5 (on the Pi)

Build the arm64 bundle (`deploy/docker/build-bundle.sh --platform linux/arm64`), copy it over, `./load-image.sh`, `./smoke-test.sh`, `docker compose up -d` (both services
`healthy`), open `http://<the Pi>:8080/` from another machine, make a token and restart `web`, reset a beacon, and put a tiles file in `data/`. Record what the web
process weighs (`ps -eo rss,args | grep beacon_base.web`, or `docker stats` now the cgroup is on) and that unplugging the companion leaves the map up and says so. Anything
found goes back here.

## The questions as asked (answers in "Decisions")


1. **Which host address does compose publish the port on by default?** *Recommendation: `127.0.0.1`* (decision 6 of web-map.md: serving the LAN is
   a choice), with the first-start checklist telling the operator to set `BEACON_WEB_BIND=0.0.0.0` (or the Pi's address) for other machines, and
   saying why. The alternative, `0.0.0.0` by default, is friendlier on a private event network but makes the map readable by anyone who joins it.
2. **One container running both processes, or two services?** *Recommendation: two services* (decision 6): independent restarts and limits, and
   the web can stay down without affecting ingest.
3. **After `web token generate`, should the web need a restart to see the token?** *Recommendation: yes, document `docker compose restart web`*:
   it is the same for every other setting. (The alternative is reading the secrets file on each reset request, which is a small code change
   and a second place that reads secrets.)
4. **Drop `/api/openapi.json`?** *Recommendation: drop* (decision 9).
5. **A non-numeric transmission number becomes a 404, not a 422.** *Recommendation: accept.*
6. **Switch the dev test client to `httpx2`?** *Recommendation: yes*; Starlette 1.7 and later requires it, and `httpx` already prints a
   deprecation warning in the current suite.
7. **May I pull the images to build and test here?** It needs `python:3.13-slim` for amd64 and arm64 (about 50 MB each) from Docker Hub and the
   packages from PyPI. *Recommendation: yes*; without it the Docker work is untested here, like the first draft of the B4 Docker notes was. An
   arm64 *run* here needs QEMU; whether it is set up on this machine I do not know, and the arm64 test is a build plus the no-compiled-files check
   either way.
8. **`mem_limit` for `web`?** *Recommendation: `128m`*, which is generous for Starlette and uvicorn and, like `ingest`'s 256 MB, is not enforced
   on a stock Raspberry Pi OS until the memory cgroup is turned on.
