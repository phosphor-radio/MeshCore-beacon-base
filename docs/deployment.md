# Deploying the base station on a Raspberry Pi

How to run the base station for an event on a Raspberry Pi (written for a Pi Zero 2 W with no internet), in Docker, and what to do when something
goes wrong. For the design and the reasons see [plan/service-packaging.md](plan/service-packaging.md); for what the commands do see the
[README](../README.md).

What runs: one container, `beacon-base`, holding `beacon-ingest` (the program that owns the companion's USB port and stores the reports).
`beaconctl` runs inside the same container; a small script, `./beaconctl`, hides that. Everything the base keeps is in one folder, `./data`.

Contents: [What you need](#what-you-need) · [1. Build the bundle](#1-build-the-bundle-on-a-machine-with-internet) ·
[2. Install on the Pi](#2-install-on-the-pi) · [3. First start](#3-first-start) · [4. Onboard the mesh](#4-onboard-the-repeaters-and-beacons) ·
[5. During the event](#5-during-the-event) · [6. Unplugging, rebooting, power loss](#6-unplugging-rebooting-power-loss) ·
[7. Next event, upgrades](#7-the-next-event-and-upgrades) · [8. Troubleshooting](#8-troubleshooting) · [Optional: udev](#optional-a-fixed-name-and-modemmanager)

## What you need

- A Raspberry Pi with **Docker and the compose plugin already installed** (`docker --version`, `docker compose version`), Raspberry Pi OS 13, and
  your login user allowed to run Docker (`groups` lists `docker`; if not, `sudo usermod -aG docker $USER` and log in again). Docker should start at
  boot (`systemctl is-enabled docker` says `enabled`), so the base comes back after a power cut.
- The companion (a MeshCore companion radio with USB serial firmware) in the Pi's data USB port. On a Pi Zero 2 W that is the inner micro-USB
  port, through an OTG adapter; the other micro-USB port is the power input.
- A machine **with internet and Docker** (with buildx) to build the bundle once.
- Which CPU the Pi is running: `uname -m` says `aarch64` (64-bit Raspberry Pi OS) or `armv7l` (32-bit). The bundle has an image for each.

## 1. Build the bundle (on a machine with internet)

From a checkout of this repository:

```bash
deploy/docker/build-bundle.sh              # both Pi platforms; writes dist/beacon-base-<version>/
```

The folder holds `image-arm64.tar` and `image-armv7.tar` (about 40 MB each), `compose.yaml`, `.env.example`, `load-image.sh`, `smoke-test.sh`,
the `beaconctl` wrapper, `README.txt`, this guide as `DEPLOYMENT.md`, the optional udev rule, and `SHA256SUMS`. The images are built without emulation, so this
works on an ordinary x86 laptop. Use `--platform linux/arm64` to build only one. Copy the folder to the Pi with a USB stick or `scp`.

## 2. Install on the Pi

In the folder on the Pi (for example `~/beacon-base`):

```bash
./load-image.sh              # loads the image for this CPU and checks its checksum
./smoke-test.sh              # optional, about a minute: runs the base against a fake companion to prove the image works on this Pi
cp .env.example .env         # then edit .env if `id -u` / `id -g` are not 1000 (see below)
mkdir data                   # make the folder yourself, so it belongs to you
docker compose up -d
```

`.env` has four values: `BEACON_UID` and `BEACON_GID` (your `id -u` and `id -g`; the container runs as you, so the files in `./data` are yours),
`BEACON_TTY_GID` (the group that owns the serial port: `ls -ln /dev/ttyACM0` shows it; `20`, `dialout`, on Raspberry Pi OS), and `TZ`.

## 3. First start

```bash
docker compose logs -f
```

The container creates `data/config.toml` from an example and then waits, telling you what is missing; it starts by itself when the
configuration is complete (it looks every ten seconds), so there is nothing to restart.

1. **Find the companion's name.** `ls /dev/serial/by-id` lists attached USB serial devices; the companion is the one that appeared when you plugged
   it in. Do not open the port or type to it: a repeater or beacon on the same bus would take binary frames as commands.
2. **Edit `data/config.toml`**: uncomment `port` under `[companion]` and give it that name, **with `/hostdev` in front of `/dev`**:
   `port = "/hostdev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_AA:BB:CC:DD:EE:FF-if00"`. (The container sees the Pi's `/dev` as
   `/hostdev`; that is how a companion that is unplugged and plugged in again is found again without restarting anything.)
3. **Make the channel key**: `./beaconctl channel generate` (or `./beaconctl channel set <key>` to use a key the repeaters already have). It is
   written to `data/secrets.toml` (mode 600). `./beaconctl channel show` prints it again when you need it for a repeater.
4. **Set the Pi's clock.** The Pi has no internet and no battery clock, so it starts with the time of its last shutdown. On the Pi (not in the
   container):

   ```bash
   sudo timedatectl set-ntp false                          # only needed if the Pi says automatic time synchronisation is on
   sudo timedatectl set-time '2026-10-10 14:30:00'         # local time
   ```

   Times stored before the clock is set are marked provisional and are corrected as soon as it is: `beacon-ingest` notices the change by itself
   (or run `./beaconctl time confirm`). `./beaconctl time` shows the state. Check it again after every reboot, until the Pi has a clock of its own.
5. **Check:** `./beaconctl status` should start with `ingest: running` and `companion: ... connected`. `docker compose ps` should say `healthy`
   (the first minute it says `starting`).

## 4. Onboard the repeaters and beacons

Each repeater needs the channel key and a window below the beacon interval (on its serial console, or remotely from the app):

```
beacon.channel <the key from ./beaconctl channel show>
beacon.window 60
```

Then let the mesh introduce itself and lock it:

```bash
./beaconctl autoadd all on         # trust whatever repeaters and beacons report, as they appear
./beaconctl status                 # watch them arrive; repeaters get their position and name from their own adverts
./beaconctl autoadd all off        # once everything you expect is listed: lock it
./beaconctl check                  # fails with a reason for anything left to do
```

Repeaters and beacons that were not trusted are listed by `./beaconctl status` and can be added one by one with `beacon add` / `repeater add`.
The README sections "Repeater positions and names", "Discovering the mesh, then locking it" and "Beacons, prefixes and names" explain what each
of these does, and "Managing repeaters over the mesh" covers changing a repeater's name, position, clock and settings from the base
(`./beaconctl repeater remote <repeater> ...`), which needs each repeater's admin password once.

## 5. During the event

| To see | Run |
|---|---|
| Is everything working? The beacons, the repeaters | `./beaconctl status` |
| Anything wrong with the setup | `./beaconctl check` |
| Is the container up and the companion connected? | `docker compose ps` (`healthy` / `unhealthy`; unhealthy usually means the companion is not connected) |
| A single line for a script | `./beaconctl health --companion` (exit 0 is good) |
| What it is doing | `docker compose logs --tail 50`, or `-f` to follow |
| A beacon that stopped being accepted | `./beaconctl rejects`, then `./beaconctl beacon reset <prefix>` |
| What each repeater last advertised | `./beaconctl repeater adverts` |
| Any other `beaconctl` command | `./beaconctl <command>` |

`./beaconctl` works with the stack stopped too, using a temporary container on the same `./data`.

The database grows by about 9 MB a day at 30 beacons and 10 repeaters, a few tens of megabytes over a week; nothing needs pruning.

## 6. Unplugging, rebooting, power loss

- **The companion is unplugged and plugged in again**: nothing to do. The base reports `companion: not connected` (and `unhealthy`) while it
  is out, finds it again when it is back, and the reports the companion held meanwhile are stored and marked `late`. If you plug it into a
  different port the `by-id` name stays the same; a different *board* has a different name and key (change `port`; the repeaters will want the admin password
  again for remote management, as the base's key changed).
- **The Pi reboots or the power is cut**: Docker starts the container again at boot, and ingest takes up where it was. The database survives an unclean
  stop (a cut can lose the last few seconds of reports). The clock does not: set it again (step 3.4) and check with `./beaconctl time`.
- **`beacon-ingest` crashes or hangs**: Docker restarts it; a hang is detected after two minutes with no progress and the process exits so Docker
  restarts it. `docker compose ps` shows the restart count; `docker compose logs` shows why.
- **To stop cleanly** at the end: `docker compose down`, then `sudo shutdown now`.

## 7. The next event, and upgrades

**A new event** starts with an empty database; keep the configuration and key if the mesh is the same:

```bash
docker compose down
mv data data-event1                                     # keep the old database; nothing is deleted
mkdir data
cp data-event1/config.toml data-event1/secrets.toml data/
docker compose up -d                                    # then onboard the mesh again (step 4)
```

**A new version**: build a new bundle, copy it to the Pi, and in the old folder `docker compose down`; in the new one run `./load-image.sh`,
copy over `.env`, move or point to the same `data` folder (`mv ../old/data .`), and `docker compose up -d`. The database is upgraded when ingest
starts; a database from a newer version is refused rather than damaged.

## 8. Troubleshooting

| What you see | What it means and what to do |
|---|---|
| The log says `... is not writable by user ...` | `./data` belongs to someone else (Docker creates a missing folder as root). `sudo chown -R $(id -u):$(id -g) data`, or `rmdir` it and `mkdir data` as yourself. |
| The log says `no companion serial port` | `companion.port` in `data/config.toml` is still commented out (step 3.2). |
| The log says `no report channel key` | Run `./beaconctl channel generate` (step 3.3). |
| `unhealthy`, and `status` says `companion: not connected` | The port name is wrong, or the companion is unplugged. `ls /dev/serial/by-id` on the Pi; the config needs `/hostdev` in front. `docker compose logs` shows the open error. |
| The log says `Permission denied` opening the port | The container user is not in the port's group: set `BEACON_TTY_GID` in `.env` to the group id from `ls -ln /dev/ttyACM0`, then `docker compose up -d`. |
| The log says `no reply to APP_START` | The board answers only with DTR high (an nRF52) or low (an ESP32-S3) and `companion.dtr = auto` could not decide; set `dtr = "on"` or `"off"` in the config. See [operations.md](operations.md). |
| `exec format error` | The wrong image for this CPU. `uname -m`, and `./load-image.sh` again (it picks by that); `docker image inspect beacon-base:latest -f '{{.Architecture}}'`. |
| `status` warns the clock is NOT SET, or times look wrong | Step 3.4. Observation times are corrected once the clock is set. |
| Reports stop and `status` shows beacons `silent` | Is `companion: connected`? Are the repeaters on and on the right channel (`./beaconctl rejects`; `./beaconctl check`)? |
| A beacon shows `rejected` | See the README "When a beacon is locked out": usually `./beaconctl beacon reset <prefix>`. |
| The container keeps restarting | `docker compose logs --tail 100`. The exit code 70 means the watchdog fired (no progress for two minutes); anything else is in the log. |
| The Pi is slow or runs out of memory | A Pi Zero 2 W has 512 MB; the container is limited to 256 MB. `docker stats --no-stream`. Do not run other things on the Pi. |
| `./beaconctl time set` fails | It cannot set the host's clock from inside the container: use `sudo timedatectl set-time` on the Pi, then `./beaconctl time confirm`. |

If `docker compose ps` is empty after a reboot, Docker is not starting at boot (`sudo systemctl enable --now docker`) or the stack was stopped
with `docker compose down` rather than left running; `docker compose up -d` starts it.

## Optional: a fixed name and ModemManager

`99-beacon-companion.rules.example` (in the bundle, and `deploy/` in the repository) is an udev rule for the Pi. It gives the companion a short fixed name,
`/dev/beacon-companion` (seen in the container as `/hostdev/beacon-companion`), and tells ModemManager to leave it alone. ModemManager is present
on desktop images, not on Raspberry Pi OS Lite; if it is, it opens new serial devices and sends them AT commands. The file explains how to fill
in the three ids. The `/dev/serial/by-id/...` name already stays the same for one board, so the rule is not needed otherwise.

## What this setup does not do

It does not run on a schedule, back itself up, or prune the database (a week needs none). It does not set the Pi's clock, update itself, or
reach the internet. A backup of a running database is `sqlite3 data/beacon.db ".backup copy.db"` on any machine that has `sqlite3`, or copy
`./data` after `docker compose down`.
