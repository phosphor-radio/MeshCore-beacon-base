# Service Hardening and Packaging Plan (B3)

Status: **review questions answered 2026-10-10; B3.1 (watchdog, `beaconctl health`, the clock message) is done; B3.2 (image and compose) is done and tested here; B3.3 (the guide) is done; B3.4 (the Pi run) is done (2026-10-14): the bundle was installed and run on the Pi Zero 2 W through a reboot, power cuts, a companion unplug and replug and a clock set forward by 40 hours, and was still healthy with the memory limit enforced and 10.7 MiB in use (see the results).** This is phase B3 of [beacon-base.md](beacon-base.md). The first draft assumed a
long-lived systemd install with retention; the review changed that: the base runs for **a week-long event** and is set up afresh each time, the
Pi has **no internet**, and a **Docker** deployment should be considered. The ingest heartbeat, listed under B3, was already built with the
remote repeater work ([repeater-remote.md](repeater-remote.md)).

**Done when** the base runs on a Raspberry Pi from a copied bundle with no internet, comes back by itself after the companion is unplugged and
replugged and after the Pi is rebooted or loses power, a hung `beacon-ingest` is restarted without a person, and an operator can do everything
from a short guide.

## Decisions

| # | Decision |
|---|---|
| 1 | **No retention, no pruning, no migration 3.** A week at the project's scale is about 60 MB (measured: 8.7 MB a day for 30 beacons and 10 repeaters), and each event starts with a new database. The first draft's findings about pruning (the `raw_id` reference stops raw frames being deleted; `observations_boot` is 15% of the file) are kept in "Notes for later" in case the base is ever run for months. |
| 2 | **Docker Compose is the deployment for the Pi** (and works on the development machine). One image, one long-running service, `restart: unless-stopped`. **Local development is unchanged**: the login user runs `pip install -e` and `beacon-ingest` directly; nothing about groups or permissions is designed for it. |
| 3 | **No bare-metal installer, systemd unit or udev installer** (the first draft's installer, `sdnotify` and the generated udev rule are dropped; no example unit either, confirmed in review). |
| 4 | **The image is built where there is internet and carried to the Pi.** `deploy/docker/build-bundle.sh` builds the image for the Pi's platform and writes a folder: `image.tar` (`docker save`), `compose.yaml`, `config.example.toml`, `.env.example` and a one-page `README`. It goes to the Pi on a USB stick or by `scp`; there `docker load` and `docker compose up -d` are all that is needed (Docker and the compose plugin are already installed on the Pi). No registry. **The target is a Raspberry Pi Zero 2 W** (quad Cortex-A53, **512 MB RAM**, one micro-USB OTG port for the companion through an adapter, Raspberry Pi OS 13). That OS comes in 64-bit (`linux/arm64`) and 32-bit (`linux/arm/v7`) images and I do not know which is installed (`uname -m` says: `aarch64` or `armv7l`), so the script builds either with `--platform`, and builds both by default into separate tars. The Dockerfile is multi-stage: the dependencies (pure Python: `pyserial` and this package) are installed in a stage that runs on the *build* machine and copied into a base image of the *target* platform, so building for arm64 needs no emulation. |
| 5 | **Base image `python:3.13-slim`** (Debian trixie), matching Raspberry Pi OS 13's Python 3.13. The image runs as a non-root user chosen at run time (`user: "${BEACON_UID:-1000}:${BEACON_GID:-1000}"`, with the serial group added), so the files in the data directory belong to the login user. |
| 6 | **State lives in one host directory, `./data`, bind-mounted at `/data`**: `config.toml`, `secrets.toml` (mode 0600) and `beacon.db`. `BEACON_BASE_CONFIG=/data/config.toml` is set in the image. To start a new event, stop the stack and move the directory away. |
| 7 | **The companion is reached through the host's `/dev`, mounted read-only at `/hostdev`** (`volumes: /dev:/hostdev:ro`), with `device_cgroup_rules` for the serial majors (`c 166:*` ttyACM, `c 188:*` ttyUSB) and the `dialout` group added; `companion.port` is `/hostdev/serial/by-id/usb-...` (the by-id links are relative, so they resolve inside the mount). A new device node and its by-id name appear as they do on the host, so the existing reconnect loop handles unplug and replug unchanged, **with no restart** (a requirement), and a container that starts before the board is plugged in simply waits. Mounting over the container's own `/dev` was rejected: it hides the container's `/dev/pts`, which `docker exec -it` (used by the `beaconctl` wrapper) needs; the DTR detection (`usb_vendor_id`) therefore matches the port by its device name, not its full path. The strict alternative, `devices: - /dev/serial/by-id/...`, is fixed at container start, so a replug would need a restart; it is a documented fallback only. |
| 8 | **Hang detection is an in-process watchdog**, not `sd_notify`: a thread that exits the process with a non-zero status if the session loop has not ticked for `service.watchdog_s` seconds, so Docker's restart policy brings it back. Default `0` (off) in the config so a debugger or a terminal run is never killed; the image sets `BEACON_WATCHDOG_S=120` (confirmed in review). A hung loop and a crash are then handled the same way. Implemented in B3.1. |
| 9 | **`beaconctl health`** (new): exit 0 when the heartbeat says ingest is running (and, with `--companion`, that the companion is connected), one line of output, 1 otherwise. It is the container's `HEALTHCHECK`, so `docker compose ps` shows `healthy` or `unhealthy` (an unplugged companion shows as unhealthy). Docker itself does not restart an unhealthy container; the watchdog and the exit-on-crash do that. |
| 10 | **`beaconctl` runs inside the container** through a wrapper, `deploy/docker/beaconctl`, which is `docker compose exec` with a terminal (so prompts and `--ask-password` work). Files for `--password-file` go in `./data`. It shares the running container's database, so there is one writer process family on one file. |
| 11 | **The clock.** The container shares the host's clock, monotonic clock and boot id, so observation times and the heartbeat's liveness work as on the host. It cannot set the host's clock: `beaconctl time set` needs `sudo timedatectl` on the host. In the container it says so and tells the operator to set the Pi's clock on the Pi (`sudo timedatectl set-time ...`); ingest notices the step and repairs provisional times by itself (or `beaconctl time confirm`). Confirmed in review. The image does not wait for any clock sync. The Zero 2 W has no RTC either. Implemented in B3.1. |
| 12 | **Logs** stay as they are (timestamps in the text; Docker adds its own), written to stdout/stderr and kept by the `json-file` driver with `max-size: 10m`, `max-file: 3` set in the compose file, so they cannot fill the SD card. |
| 13 | **An operator guide**, `docs/deployment.md`: building the bundle, installing Docker on the Pi with no internet, loading the image, the first-start checklist (config, channel key, clock, repeaters), daily use, unplug/replug and power-loss behaviour, a new event, troubleshooting. The README links to it. |
| 14 | **A host-side udev rule is optional**, shipped as `deploy/99-beacon-companion.rules.example` and explained in the guide: it gives the board a stable name and, on a system that has ModemManager (desktop images), tells it to leave the port alone, since it would otherwise probe it with AT commands. |
| 15 | **Sized for 512 MB.** The compose file sets `mem_limit: 256m` (the container is expected to use well under 100 MB, and a leak is then killed and restarted by the container's own policy rather than starving the Pi), `init: true` (a proper PID 1 that forwards SIGTERM and reaps children), and, at first, no extra containers (`beaconctl` runs inside the one that exists). **Updated 2026-10-13:** a second service, `web` (the map and API), runs from the same image with `mem_limit: 128m` ([web-docker.md](web-docker.md)); the memory cgroup is turned on as part of preparing the Pi, so both limits apply. |

## What exists and what is missing

Exists: `beacon-ingest` reconnects with backoff (a missing port is just a failed attempt), stops cleanly on SIGTERM and says so in its heartbeat,
writes its heartbeat and tolerates a busy database; WAL with `synchronous=NORMAL` (a power cut can lose the last transactions but not corrupt
the file); `beaconctl` and ingest share the database safely; jobs left running by a crash are failed on start; reports held by the companion are
drained and marked late on connect; `config.example.toml`.

Missing: the watchdog, `beaconctl health`, the `time set` message in a container, the image, the compose file, the bundle script, the udev
example and the guide.

## Components

**Watchdog (`watchdog.py`).** `Watchdog(timeout, on_stall)` with `feed()`, `start()`, `stop()`; a daemon thread checks once a second. It is fed
from `PipelineHandler.on_tick` (about four times a second, also while waiting to reconnect), at start-up and after a blocking call returns. The
longest legitimate stall is a companion command (5 s) or a busy database (10 s), far below the 120 s default. On a stall it logs a critical line
with what the loop was last doing and calls `os._exit(70)` (an exit status that is easy to spot in `docker compose ps`).

**`beaconctl health`.** Reads the heartbeat (`heartbeat.state`), prints `ok: ingest running, companion connected, last report 40s ago`
or the reason, and returns 0 or 1. Does not open the port.

**Image and compose (`deploy/docker/`).** `Dockerfile` (multi-stage, two platforms), `compose.yaml` (service, `/data` and `/hostdev` mounts, cgroup
rules, `group_add`, `restart`, `init`, `mem_limit`, `stop_grace_period: 15s`, the healthcheck, log options, `BEACON_WATCHDOG_S`), `.env.example` (`BEACON_UID`,
`BEACON_GID`, `BEACON_TTY_GID`, `TZ`), `beaconctl` wrapper, `build-bundle.sh`, `smoke-test.sh`. The entry point runs `beacon-ingest` with the
port from the config; the first start with no `config.toml` copies the example into `/data` and exits with a message, rather than guessing a
port.

**Time zone.** `TZ` in `.env` and `/etc/localtime` mounted read-only, so `beaconctl` prints local times as the host does.

## Gaps and risks

1. **Docker on the Pi with no internet.** Docker and the compose plugin must already be on the Pi (Debian packages `docker.io` and
   `docker-compose-v2`, or Docker's own packages). With no internet at install they come from the OS image or from `.deb` files carried over; the
   guide lists the packages and the order, but I cannot test that here (open question 1).
2. **`/dev` bind mount is broad.** It gives the container sight of every host device, limited by the cgroup rules to serial ports. Acceptable
   for a single-purpose Pi on an isolated network, and the strict `devices:` form is the documented fallback.
3. **Device numbers.** `c 166` and `c 188` are the Linux majors for `ttyACM` and `ttyUSB`; a board that enumerates differently (some CDC
   devices use other nodes) needs another rule. `ls -l /dev/ttyACM0` shows it; the guide says how to check.
4. **`docker compose exec` needs the container running.** `beaconctl` reading the database while ingest is down is possible
   (`docker compose run --rm`), and the wrapper falls back to it with a note.
5. **Restart loops.** A container that exits immediately (a bad config) would restart forever; the first-start check prints the problem and
   sleeps rather than exiting, and `restart: unless-stopped` does not back off beyond Docker's own delay.
6. **Data-directory ownership.** If the data directory is created by Docker as root, ingest (running as the login user) cannot write it. The
   guide has the user create it first, and the entry point checks it is writable and says what to do.
7. **Power loss and the SD card.** `fsync` behaviour is unchanged; a cut can lose the last few transactions. Docker's own state is not
   critical (the bundle can be reloaded). The guide recommends a clean `docker compose down` and shutdown at the end of the event.
8. **Bitness of the Zero 2 W's OS** is not known; both images are built (decision 4), and the guide has the one-line check.
9. **512 MB of RAM** is enough for Docker's daemon plus one small Python container, but not for much else on the Pi. No swap tuning is
   attempted; `mem_limit` and `restart` contain a leak, and `docker stats` shows the use.
10. **One USB port.** The companion goes in the OTG port through an adapter and the Pi is powered from the other micro-USB port, so no hub is
    needed; a board that draws more than the Pi's supply can give (an unpowered Wio-SX1262 transmitting) is a power problem to watch for
    during the acceptance run.

## Testing

- **Watchdog:** fires after the timeout with no feed, not while fed, `stop()` cancels it, `0` disables, the exit status; a stalled fake loop
  is detected within the timeout.
- **`beaconctl health`:** every heartbeat state (never run, stopped, down, running without the companion, running), `--companion`.
- **`time set` in a container:** the message and exit code when `timedatectl` is missing or fails.
- **Image (opt-in, `pytest -m docker`, skipped without Docker):** `docker build` for the host platform; start the container with the fake
  companion inside it (`beaconctl simulate` and `beacon-ingest` in one container), `beaconctl health` is healthy, reports reach the database, kill
  the container with SIGKILL and let compose restart it, state intact, `docker compose config` validates the file.
- **Cross-build:** `build-bundle.sh --platform linux/arm64` produces a tar that `docker load` accepts and whose manifest says arm64.
- **On the Pi (manual, the real acceptance test):** load the bundle, `up -d`, unplug and replug the companion, `reboot`, pull the power, set the
  clock, run for a day, watch `beaconctl status`, `docker compose ps` and the size of `./data`.

## Phases

**B3.1 Service behaviour.** `watchdog.py` and `[service] watchdog_s` / `BEACON_WATCHDOG_S`, `beaconctl health`, the `time set` message,
tests. Runnable on the development machine without Docker.

**B3.2 Image and compose.** Dockerfile, compose file, `.env.example`, `beaconctl` wrapper, `build-bundle.sh`, `smoke-test.sh`, the opt-in
Docker tests. Done when the smoke test passes here and the arm64 bundle builds.

*Done (2026-10-10).* What was built, and what the work found:

- **Files** (`deploy/docker/`): `Dockerfile`, `compose.yaml`, `.env.example`, `entrypoint.sh`, `bin/beaconctl` and `bin/beacon-ingest` (two-line wrappers
  around `python -m`), `beaconctl` (the host-side wrapper), `build-bundle.sh`, `load-image.sh`, `smoke-test.sh`, `BUNDLE-README.txt`; `.dockerignore`
  at the root. `pyproject.toml` registers the `docker` pytest marker.
- **Cross-building works without emulation**, as designed: both `image-arm64.tar` (42 MB) and `image-armv7.tar` (38 MB) were built on an x86 machine and
  their manifests checked (arm64; arm v7), with `SHA256SUMS`. `load-image.sh` picks the file from `uname -m` (`aarch64` / `armv7l`). The final stage
  of the Dockerfile has no `RUN` (that would execute for the target platform); the example configuration is prepared in the build stage.
- **The container's first-run configuration is its own example**: the repository's with `companion.port` commented out and pointing at
  `/hostdev/serial/by-id/...`. The repository example has an active placeholder port, which would have let the container start on a port that
  does not exist.
- **Waiting, not crash-looping.** `entrypoint.sh` creates `data/config.toml`, then loops every ten seconds on `beacon-ingest --check` (new: a
  configuration check, exit 2 with the reason) printing each problem once, and starts ingest by itself when the port and the channel key are there.
  A data directory the container user cannot write is reported the same way. Any other command (`beaconctl status`) is run as given.
- **`/hostdev` instead of `/dev`** (decision 7) was checked: `docker exec -it` still gets a `/dev/pts` terminal and `/hostdev` shows the host's devices.
  The DTR lookup (`usb_vendor_id`) now falls back to the device name when the path differs (`/hostdev/ttyACM0` against the kernel's `/dev/ttyACM0`), and
  refuses to guess when two listed ports share a name.
- **Tests:** `deploy/docker/smoke-test.sh` runs a fake companion, simulated beacons and `beacon-ingest` in one throwaway container and waits for stored
  reports (it passes here, and is in the bundle to run on the Pi). `BEACON_DOCKER_TESTS=1 pytest tests/test_docker.py` (8 tests, about 35 s) runs the
  real `compose.yaml` and wrapper in a scratch project: the file validates, the container idles and explains until configured and then starts, the
  companion-less state is reported as not connected, `/hostdev` and `/dev/pts` are both there, the user, memory limit, restart policy, `init` and
  watchdog are as specified, a SIGKILLed service is restarted with the allowlist intact, `beaconctl` works through a throwaway container with the
  stack down, and `time set` gives the clock message. The ordinary suite skips them.
- **Not tested here, and needing the Pi:** running on the Pi's CPU (the smoke test is the first step), a real companion with unplug and replug through
  `/hostdev` (the cgroup rules for majors 166 and 188 are as documented for Linux but the hot-plug itself needs a device), memory use on 512 MB, and a
  power cut.

**B3.3 Guide.** `docs/deployment.md`, the udev example, README and CLAUDE.md updates.

*Done (2026-10-10).* `docs/deployment.md` covers what the plan lists: building the bundle, installing on the Pi, the first start step by step (find the
port, edit `port` with `/hostdev` in front, channel key, clock including `set-ntp false`), onboarding and locking the mesh, what to run during the event,
unplug/replug, reboot and power loss, a new event (keep config and key, new database), upgrades, a troubleshooting table, and the optional host udev rule
(`deploy/99-beacon-companion.rules.example`, syntax-checked with `udevadm verify`). The bundle ships the guide as `DEPLOYMENT.md` and the rule. `tests/test_docs.py`
checks that the documents' relative links resolve, that the files and commands the guide names exist, and that its `./beaconctl` examples parse.

**B3.4 Pi run.** The manual acceptance test above. Done when the criterion at the top holds.

*Results so far (2026-10-11, on the Pi Zero 2 W):*

- Loading the bundle's image, the smoke test, `docker compose up -d` with the real companion, and unplugging and replugging the companion all worked.
- **Reboot:** the container came back by itself and the clock was kept.
- **Power cut:** the container came back by itself; the clock carried on from where it stopped, so it was behind by the length of the outage. That was
  expected (no RTC, no internet) and is the case the guide already tells the operator to handle by setting the clock again. The guide now says plainly
  which of the two (reboot or power cut) keeps the time, and what is refused or provisional until it is set.
- **Ten hours of running (2026-10-12):** the container was `Up 10 hours (healthy)` with a restart count of 0; `beaconctl status` showed `ingest: running`, the
  companion (an Ikoka Stick-E22 / Xiao nRF52, firmware v1.17.1) connected for the whole time with its last frame 12 s old, two beacons `ok`, each heard by
  three repeaters, the last report 2 minutes old. `./data` was 4.4 MB (two beacons, three repeaters; the simulated 9 MB a day was for 30 beacons). The log
  held only the start-up lines: the watchdog notice and two `unknown_repeater` warnings from the first minutes, before the repeaters were added. `check`
  warned that `beacon.window` was not recorded for the three repeaters, which is configuration (`beaconctl repeater window`, or any `repeater remote ... get
  beacon.window`, which records it), not a fault. The operator ended the session at ten hours instead of a full day.
- **Memory could not be measured, and the limit was not enforced (until the cgroup was turned on).** `docker stats` showed `0B / 0B` for memory: Raspberry Pi OS ships with the kernel's memory
  cgroup off, so Docker cannot account for or limit memory, and the compose file's `mem_limit: 256m` (decision 15) does nothing on a stock install. Nothing
  went wrong in ten hours (no restarts, healthy), so a leak is unlikely, but it is unproven. To turn the controller on, add `cgroup_enable=memory
  cgroup_memory=1` to the single line in `/boot/firmware/cmdline.txt` (and remove `cgroup_disable=memory` if it is there) and reboot; the operator has since done this and made it a step of preparing the Pi (2026-10-13), so the limits apply. With it on, one `docker stats` sample on the Pi with both services
  running read: ingest 6.0 MiB of 256 MiB, web 28.1 MiB of 128 MiB ([web-docker.md](web-docker.md), W5). Without it, the process's size is visible from the host with `ps -eo rss,args | grep beacon_base.service`, and `free -m` shows the Pi as a whole.
  The guide now says all of this.
- **Longer run, after the memory cgroup was turned on (2026-10-14):** the setting above works on the Pi Zero 2 W: `docker stats` shows `10.7MiB / 256MiB`
  (4.2%), so the container is far under the 256 MB limit and the limit is now enforced. CPU 0.9%, 3 processes, `./data` 5.2 MB (two beacons, three repeaters),
  `healthy`, restart count 0, the companion connected with its last frame 6 s old, both beacons `ok` and heard by three repeaters. The log held the start-up
  lines and one warning, **`system clock was set (+145346 s); corrected the time of 423 earlier observations`**: the clock had been behind after a power cut,
  the operator set it forward by about 40 hours, ingest noticed the step by itself and repaired the provisional times, which is the designed behaviour seen on
  the real hardware for the first time.
- **Ages after a clock jump are skewed.** Docker's `Up 46 hours`, `beaconctl status`'s `started 2d ago` and `connected 2d ago` are computed from wall-clock times saved
  before the jump, so they include it (here about 40 hours of the 46). They do not show continuous uptime. The monotonic clock does: `uptime` on the Pi, or
  `cat /proc/uptime`. The restart count of 0 is also not proof of no restarts: after a Pi reboot Docker starts the container again without counting a policy
  restart. The guide says both.
- **Outcome.** The acceptance criterion holds as far as it was exercised: the base came back by itself after a reboot, power cuts and an unplug and replug of the
  companion, the clock correction worked, the guide was enough to operate it, and memory is small. **Not exercised on the Pi:** the watchdog firing (it is tested
  against the fakes; no hang occurred), a run of the full week, and multi-hop paths. B3 is complete.

## Review questions: answers (2026-10-10)

| # | Question | Answer |
|---|---|---|
| 1 | Docker on the Pi | Docker and the compose plugin are already installed. |
| 2 | Bare metal | The systemd unit is dropped. |
| 3 | Container user | The login user (`BEACON_UID`/`BEACON_GID`). Local development also runs as the login user; no group permissions are designed for it. |
| 4 | Which Pi | A Raspberry Pi Zero 2 W (512 MB, one OTG port); bitness unknown, so both platforms are built. |
| 5 | Device access | The host `/dev` is mounted, and replug with no restart is wanted (done as `/hostdev`, decision 7). |
| 6 | Watchdog default | 120 s in the image, off elsewhere. |
| 7 | The clock | Set on the Pi; ingest repairs the times. |

## Notes for later (not in B3)

If the base is ever run for months: batch-prune old raw frames, non-accepted observations and finished remote jobs; make
`observations.raw_id` nullable with `ON DELETE SET NULL` (it is `NOT NULL REFERENCES raw_frames`, so raw frames cannot be deleted while an
observation points at them); make the `observations_boot` index partial (`WHERE time_trusted = 0`, 1.3 of 8.7 MB a day); keep accepted
observations for a chosen period. A pass must run in small batches, since ingest has one thread and a long delete holds the write lock.
