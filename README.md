# MeshCore-beacon-base

Base station software for the MeshCore beacon tracking system. It collects beacon sightings reported by fixed repeaters,
rejects replayed or unknown beacons, and (later) estimates and maps where each beacon is.

> **Status: phase B2 (done) plus beacon names and repeater onboarding (done).** `beacon-ingest` decodes reports and beacon name announcements from a companion,
> applies the allowlist, high-water mark and dedupe, and stores everything in SQLite; `beaconctl` provisions beacons and
> repeaters by key prefix and shows a lockout and its one-step fix. B1, B2, the beacon names (repeater
> firmware `28bb4985` in the firmware repository) and the repeater onboarding (positions from adverts, discovery and
> auto-add) are verified on hardware.
> **Remote repeater management over the mesh is done** ([docs/plan/repeater-remote.md](docs/plan/repeater-remote.md)): `beaconctl repeater remote`
> reads and changes a repeater's name, position, advert intervals and `beacon.*` settings, syncs its clock, sends an advert and reboots it, through
> `beacon-ingest` and the base companion, and `beacon-ingest` keeps a heartbeat that `beaconctl status` shows. It has been used on the hardware with
> three repeaters in one room; paths of more than zero hops have not been tested.
> For the Raspberry Pi there is a Docker image and compose file (`deploy/docker/`; `build-bundle.sh` makes the folder to carry to a Pi with no
> internet) and an operator guide, [docs/deployment.md](docs/deployment.md) (B3, [docs/plan/service-packaging.md](docs/plan/service-packaging.md)); the
> Pi run itself (B3.4) is still to do.
> **The map and API (B4, branch `b4-web-map`) are built and tested here**: `beacon-web` serves a Leaflet map with local tiles, the repeaters, the beacons
> with a placeholder position estimate, banners for a down ingest, an unset clock and locked-out beacons, and a token-protected Reset
> ([docs/web.md](docs/web.md), [docs/plan/web-map.md](docs/plan/web-map.md)). It is not in the Docker image yet, and it has not been tried with real
> beacons walking past real repeaters. See [docs/plan/beacon-base.md](docs/plan/beacon-base.md) and
> [docs/plan/beacon-names.md](docs/plan/beacon-names.md) and [docs/plan/repeater-onboarding.md](docs/plan/repeater-onboarding.md).

## How the system works

```
 beacon (nRF52) --zero-hop advert--> beacon repeater (fixed, known location)
                                          |  batched report: beacon id, counter, RSSI, SNR, battery
                                          v
                              private channel (GRP_DATA, flood through mesh)
                                          |
                                          v
                       base: companion node + this software (Pi) -> beacon locations
```

- **Beacons** are small battery-powered nRF52 nodes that periodically send a signed, zero-hop advert (about every
  5 minutes). They never listen.
- **Beacon repeaters** are MeshCore repeaters at known locations. They hear beacons, do not forward them, and batch up
  "I heard beacon X at RSSI Y" reports onto a private channel.
- **The base** (this repo) is a MeshCore companion node on USB plus the software here. It enforces the allowlist,
  per-beacon high-water mark and dedupe, stores everything, and serves a map.

Scale: about 30 beacons and 10 repeaters. The base runs on a Raspberry Pi in the field with **no internet**, and must
also run on an ordinary Ubuntu machine for development.

## What is in this repo

Three cooperating processes sharing one SQLite database (WAL mode) as their only interface:

| Process | Job |
|---|---|
| `beacon-ingest` | systemd service. Owns the companion's serial port, decodes reports, runs the replay/dedupe pipeline. |
| `beacon-web` | HTTP API and Leaflet map ([docs/web.md](docs/web.md)). Separate from ingest, so it can be restarted or left off without losing reports. |
| `beaconctl` | CLI for provisioning (beacons, repeaters, channel key), status, rejects, reset, clock, simulation, and reading or changing repeaters over the mesh. |

Everything must work without hardware: a fake companion and synthetic reports allow the pipeline and UI to be built and
tested at a desk.

Besides the package (`src/beacon_base/`) there is `utilities/`: stand-alone diagnostic scripts, not installed and not used by the
package. See [utilities/README.md](utilities/README.md).

## Related repositories

| Repo | Role |
|---|---|
| [MeshCore-beacon-client](https://github.com/phosphor-radio/MeshCore-beacon-client) | Firmware fork: beacon (`examples/beacon`), beacon repeater (`examples/simple_repeater` with `WITH_BEACON_REPORTER`) and the stock companion firmware the base uses. Owns the report wire format (`src/helpers/BeaconReport.h`). |
| this repo | Base software and the project planning docs. |

## Documentation

- [docs/plan/beacon-project.md](docs/plan/beacon-project.md): overall project plan, decisions, security model, milestones.
- [docs/plan/beacon-base.md](docs/plan/beacon-base.md): base design, data model, pipeline, phases B0-B4.
- [docs/plan/repeater-remote.md](docs/plan/repeater-remote.md): managing repeaters over the mesh (a small fixed set of operations), the ingest heartbeat.
- [docs/web.md](docs/web.md): the map and API: running `beacon-web`, reading the page, tiles with no internet, the API, safety notes.
- [docs/map-tiles.md](docs/map-tiles.md): making an offline MBTiles map for your area from a Geofabrik OpenStreetMap extract, and the credit it needs.
- [docs/wire-format.md](docs/wire-format.md): the report format and the companion frame that carries it.
- [docs/deployment.md](docs/deployment.md): running the base on a Raspberry Pi in Docker with no internet: building the bundle, first start, the event, troubleshooting.
- [docs/operations.md](docs/operations.md): running on real hardware: the companion's USB serial (DTR) settings, managing repeaters, troubleshooting.
- [utilities/README.md](utilities/README.md): one-off and diagnostic scripts that are not part of the package, such as `advert_probe.py` (why a repeater's position keeps reverting).

Links inside the plan docs that point at `../../src/...` or `../companion_protocol.md` refer to files in the firmware
repo, not this one.

## Requirements

- Linux (Raspberry Pi OS or Ubuntu).
- Python 3.11 or newer.
- A MeshCore companion node on USB. The test setup is a XIAO ESP32-S3 + Wio-SX1262 flashed with the
  `Xiao_S3_WIO_companion_radio_usb` firmware build; the field companion may be a XIAO nRF52 (`Xiao_nrf52_companion_radio_usb`)
  or an Ikoka stick. The two want different DTR settings on the serial port, which `companion.dtr = "auto"` (the default)
  chooses by USB vendor; see below.
- All devices in the mesh use the same radio settings: 905.775 MHz, BW 62.5 kHz, SF 8, CR 4/6.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                                       # no hardware needed

mkdir -p ~/.config/beacon-base
cp deploy/config.example.toml ~/.config/beacon-base/config.toml    # then set companion.port
beaconctl channel generate                   # new 16-byte report channel key, saved to secrets.toml (mode 0600)
                                             # paste the printed key into each repeater: beacon.channel <hex>
beacon-ingest                                # own the companion port and store reports (systemd service later)
beaconctl status                             # lists repeaters that report but are not trusted yet, then the same for beacons
beaconctl repeater add --all                 # trust them; position and name come from their adverts (or: repeater add <prefix>)
beaconctl beacon add --all                   # then the beacons they report (or: beacon add <prefix>)
beaconctl check                              # sanity-check the setup
beaconctl status                             # one line per beacon, rejected and silent first

pip install -e '.[web]'                      # the map (included in '.[dev]'); see docs/web.md
beaconctl web token generate                 # the token the map's Reset button asks for
beacon-web                                   # http://127.0.0.1:8080/
```

`beaconctl` options: `-c <config>` (or `$BEACON_BASE_CONFIG`), `-v` for debug logging. The database defaults to
`beacon.db` next to the config file.

| Command | What it does |
|---|---|
| `channel generate [--force]` / `channel set <hex\|-> [--force]` / `channel show` | Create, store or print the report channel key. Replacing an existing key needs `--force`, since every repeater would need updating. |
| `beacon add <prefix>` | Allowlist a beacon by the 8-byte key prefix its reports carry (16 hex characters, shown by `status`). A full 64-character key, for example from the beacon's serial `pubkey` command, is accepted and reduced to its prefix. A prefix already on the list is refused. |
| `beacon add --all [--hours H]` | Add every beacon that has been reported (default last 24 h) and is not on the allowlist, including ones seen only through repeaters that are not trusted yet. It adds whatever was reported, so check `status` first if other people's beacons may be in range. |
| `beacon list` / `beacon status <prefix>` | The allowlist, and one beacon in detail. |
| `beacon enable\|disable\|remove\|reset <prefix>` or `--all` / `-a` | One beacon, or every beacon on the allowlist. `remove` keeps history; `reset` clears the high-water mark so the next report becomes the new baseline (do it while the beacon is transmitting). |
| `repeater add <key-or-prefix> [<lat> <lon>] [--name N] [--window S]` | Trust a repeater. Without `<lat> <lon>` its position comes from its advert; if none was heard it is added unlocated at 0, 0. The advertised name replaces `--name`. |
| `repeater add --all [--hours H]` | Trust every repeater that has sent reports **or been heard advertising** (default last 24 h) and is not in the table. |
| `repeater locate <prefix-or-name> <lat> <lon>` | Set a position by hand (testing, before the repeater has advertised, or when it can't be set on the repeater). The next advert with a position replaces it. |
| `repeater adverts` | What each repeater last advertised (name, position, the clock in the advert, when it was heard), trusted or not, next to the position the table uses. `repeater list` also shows `HEARD` and `ADVERT CLOCK`. `UPDATED` is when the position last *changed*, so an advert that repeats what the base already has does not move it. |
| `repeater list` / `remove` / `enable` / `disable` / `window <S>` | The repeater table, which also shows where each position came from. Commands take the key prefix (six or more hex digits) or the name. Reports from repeaters not in the table are stored but not counted. |
| `status [--hours H]` | Per-beacon state (`rejected`, `silent`, `ok`, `disabled`), then every repeater and beacon that has been seen but is not trusted yet, ready to add. |
| `autoadd [beacons\|repeaters\|all [on\|off]]` | Show or set automatic trust for newly seen repeaters and beacons (both off by default). A setting in the database, so a running `beacon-ingest` uses it at once. `status` warns and `check` fails while it is on. |
| `rejects [--beacon X] [--limit N]` | Recent observations that were not accepted, with the reason. |
| `time` / `time set "YYYY-MM-DD HH:MM:SS"` / `time confirm` | Show or fix the clock state. The Pi has no internet, so its clock is set by hand; times stay provisional until then. |
| `check` | Fails on missing setup, repeaters with no location and repeater report windows that are too long for the beacon interval. |
| `health` / `health --companion` | One line and an exit status: 0 when `beacon-ingest` is running (with `--companion`, and the companion connected), 1 otherwise. For a container healthcheck or a script; it does not open the serial port or create a database. |
| `listen [--port P] [--json] [--count N]` | Bring-up view: print decoded reports and name announcements without storing them. Only one of `listen` and `beacon-ingest` can have the port. |
| `ingest [--port P]` | Same as `beacon-ingest`. |
| `simulate [--provision] [--walk] ...` | Run a fake companion on a pseudo-terminal with synthetic reports. `--walk` carries the first beacon round a course past located repeaters, for the map. |
| `web token generate [--force]` / `web token show` | The operator token `beacon-web` asks for before it resets a beacon, kept in `secrets.toml` next to the channel key. |
| `tiles check [file] [--scheme S]` | What a map tiles file holds, how it numbers its rows, and whether the map will show it where the repeaters are ([docs/map-tiles.md](docs/map-tiles.md)). |
| `positions rebuild [--hours H]` | Estimate stored transmissions again with the repeaters' positions as they are now (positions are worked out once, when a report arrives). |

### What the base sets on the companion at startup

Every time `beacon-ingest` or `beaconctl listen` connects it makes the companion match the mesh, writing a setting only when
it differs (the companion keeps them in flash):

| Setting | From | Notes |
|---|---|---|
| Radio: frequency, bandwidth, SF, CR | `[radio]` `freq_khz`, `bw_hz`, `sf`, `cr` (905775 kHz, 62500 Hz, SF 8, CR 6) | Sent without the client-repeat flag, so the companion does not repeat. `companion.manage_radio = false` only warns instead. |
| Path hash mode | `[radio]` `path_hash_mode` (default `2`, 0-2) | Hash size in the paths of the packets the companion floods; kept the same as the rest of the mesh. Needs companion firmware v10. A failure is logged and ignored. |
| Report channel | `companion.channel_index` / `channel_name`, the key in `secrets.toml` | Slot 0 holds the built-in Public channel, so the default slot is 1. |
| Manual-add mode | `companion.manual_add_contacts` (on) | Every advert reaches the base in full; see "Repeater positions and names". |
| Clock | `companion.sync_clock` (on) | Moved **forward** to the base's clock, and only once that clock is trusted (`beaconctl time set` or `confirm`, or `clock.assume_synced`). Never backward (the companion refuses), never from an untrusted clock, and kept in RAM, not flash. An nRF52 companion restarts at May 2024 on every boot; repeaters ignore a login or command stamped earlier than one they have seen, which matters for remote repeater management (`docs/plan/repeater-remote.md`). A companion found ahead of the base is only warned about. |

The serial port's DTR level is chosen separately, below.

### Companion serial port: DTR

An ESP32-S3 companion (Espressif native USB, vendor `0x303A`) must be opened with **DTR low**, or it can reset. An nRF52
companion (XIAO nRF52, Ikoka stick) only transmits while the host holds **DTR high**, so with DTR low it never answers and the
log fills with `companion link lost: no reply to command 1 within 5s`. RTS is never raised.

| `companion.dtr` | Behavior |
|---|---|
| `"auto"` (default) | Espressif keeps DTR low, any other or unknown USB device gets DTR high. If the first `APP_START` gets no reply, the port is reopened once with the opposite setting and the one that works is kept for later reconnects. Logged at INFO. |
| `"on"` / `"off"` | Force DTR high / low. No fallback. |

`beaconctl listen`, `beaconctl ingest` and `beacon-ingest` take `--dtr auto|on|off` to override it for one run (handy for
bring-up). Details and troubleshooting: [docs/operations.md](docs/operations.md).

### Repeater positions and names

Repeaters get their position from the Android app, over the mesh, and then advertise it. The base companion hears those
adverts and `beacon-ingest` records them: the position, the name and the repeater's full public key. It works before or
after the repeater is trusted. The latest advert wins, and an advert without a position (a repeater nobody has located
advertises 0, 0) never erases a known one. A repeater with no position is `unlocated`: its reports are accepted as usual, it
is flagged in `repeater list` and fails `beaconctl check`, and the position estimator will leave it out.

- The base companion runs in manual-add mode so it stores no contacts and every advert reaches the base in full
  (`companion.manual_add_contacts`, on by default; it is saved in the companion).
- Repeaters send a flood advert every 47 hours by default. After setting a position, run `advert` in the repeater's CLI or
  the base waits for the next one. The advert has to reach the base companion (within 8 hops and in radio range); otherwise
  use `repeater locate`.
- This relies on the repeater's adverts **carrying its position**. If a position keeps reverting to an old value, the adverts
  probably carry none; see "A repeater's position keeps reverting to an old value" in [docs/operations.md](docs/operations.md)
  and `utilities/advert_probe.py`.
- Repeaters are onboarded **before** beacons: a report from a repeater that is not trusted is stored but changes no
  beacon's counters. It is not lost to the operator though: the repeater, and the beacons in its report, show up in `status`
  straight away (a beacon is marked "only via repeaters that are not trusted yet") and can be added at once.

### Discovering the mesh, then locking it

Everything seen on the channel is listed in `status`, trusted or not: repeaters that sent a report or were heard
advertising (with their advertised name and position), and beacons that were reported (with their announced name).
`repeater add --all` and `beacon add --all` trust the lot. For a mesh that is being set up, switch on automatic trust
instead and lock it when everything has been found:

```bash
beaconctl autoadd all on        # or just repeaters, or just beacons
beaconctl status                # watch the mesh appear; ingest logs "auto-added ..." for each
beaconctl autoadd all off       # locked: new arrivals are listed again but no longer trusted
```

With auto-add for **repeaters** on, an unknown repeater is trusted when it sends a report or an advert (whether or not the
advert carries a position). With auto-add for **beacons** on, an unknown beacon is trusted when a *trusted* repeater reports
it, and that report becomes its baseline; a repeater that is not trusted can never get a beacon trusted. Turning auto-add
on adds nothing already seen (use `add --all` for that), and turning it off removes nothing. Every repeater on the channel is
trusted while it is on, including ones that never report (ordinary mesh repeaters), so lock it once the mesh is found.

### Beacons, prefixes and names

Everything about a beacon is keyed by the first 8 bytes of its public key, the **prefix**: reports carry nothing more, and
the allowlist, high-water mark and dedupe all work on it. Commands take the prefix exactly as `beaconctl status` prints it,
so copy and paste works, or the first six or more hex digits of it when that is unambiguous.

A beacon's **name** is display text only. It is set on the beacon (`set name <name>` on its serial CLI; with no name set a
beacon uses `beacon-` plus the first three bytes of its key, `beacon-f5b165`), travels in the beacon's signed advert, and
is announced to the base by every repeater that hears it (on first sight, when it changes, and every few hours). The base
stores the latest announcement per prefix, including for beacons not on the allowlist yet, cleans it (control characters
and escape sequences are removed) and shows it. Names are not unique and never used to find a beacon. A beacon whose name
has not been announced yet is shown by its prefix.

Onboarding: configure the beacons, let them transmit, run `beaconctl status`, and add each one it lists under "heard but
not on the allowlist" with `beaconctl beacon add <prefix>`, or all at once with `beaconctl beacon add --all`. To pre-register
a beacon before it transmits, use the prefix (or the full key) from its serial `pubkey` command.

### When a beacon is locked out

A beacon is locked out when its counter is at or below the stored high-water mark, for example after a forged report with
a huge counter or after its flash was erased. `beaconctl status` shows it first, as `rejected`, with the counters, the
repeaters that sent them and the fix:

```
STATE     NAME        PREFIX            HWM      HEARD   BATT   DETAIL
rejected  Roof  a0a1a2a3a4a5a6a7  1000000  12s ago 3.98V  3 replays rejected since 14:00:03 (counters 300-302, hwm 1000000) via north-ridge; fix: beaconctl beacon reset a0a1a2a3a4a5a6a7
```

`beaconctl beacon reset a0a1a2a3a4a5a6a7` clears it in one step. A report that arrives after a newer one from the same beacon was
accepted (a repeater with a long `beacon.window`) is rejected as `late` but is not a lockout; keep every repeater's
window below the beacon interval (`beaconctl check` warns).

### Is ingest running?

`beacon-ingest` keeps a one-row heartbeat in the database, written when something changes (connect, disconnect, a job) and at least
every 15 seconds. `beaconctl status` prints it first:

```
ingest: running (pid 1234, started 3h ago)
companion: base (Xiao nRF52 v1.12) on /dev/serial/by-id/..., key 808182838485, connected 3h ago; last frame 2s ago, last report 40s ago
```

and flags (`!`) a companion that is not connected, an ingest that is **not running** (its row is older than 45 s, or from before the
last boot, or it was stopped cleanly) or that **has never run**. Liveness uses the monotonic clock, so setting the Pi's clock by hand
cannot make a dead ingest look alive. `beaconctl check` fails when ingest is down or the companion is not connected, but not for an
ingest that has never run (a fresh setup is checked before its first start). The key prefix shown is the companion's, which is what a
repeater lists in its access list once the base has logged in to it.

### Working without hardware

```bash
beaconctl channel generate
beaconctl simulate --provision --interval 2     # adds simulated beacons/repeaters, prints the fake companion's path
beacon-ingest --port /tmp/fake-companion-XXXX/ttyFAKE
beaconctl status
```

## Beacon repeater CLI

The repeaters are MeshCore repeaters built with the beacon reporter (firmware repository, `WITH_BEACON_REPORTER`; the
`Xiao_nrf52_beacon_repeater`, `Xiao_S3_beacon_repeater` and `Xiao_S3_WIO_beacon_repeater` environments). They have the stock
repeater CLI plus the `beacon.*` commands below, over the repeater's serial port or remotely from the MeshCore app. A repeater
hears beacon adverts, does not forward them, and reports them to the base on a private channel: observations (`0xFFBE`) every
`beacon.window` seconds, and the beacons' names (`0xFFBF`) when it first hears a beacon, when a name changes and on a refresh
timer. **Nothing is sent until a channel is set.** The commands are documented in full in the firmware repository's
`docs/cli_commands.md`.

| Command | What it does |
|---|---|
| `beacon.channel` | Show whether the report channel is set, and its hash byte. The secret is never shown. |
| `beacon.channel <hex>` | Set the channel secret: 32 hex characters (128 bits, the only size companions support) or 64. Use the key from `beaconctl channel show`. Saved. |
| `beacon.channel clear` | Forget the channel; the repeater goes back to only logging. |
| `beacon.window` / `beacon.window <secs>` | Show or set how long a partial batch waits before it is sent (1-3600, default 60). Keep it below the shortest beacon interval (80% of it is the advice, 240 s for the default 300 s) or the base rejects the late reports. For a 30 s test interval use about 20. Tell the base with `beaconctl repeater window`; `beaconctl check` compares them. |
| `beacon.names` / `beacon.names on\|off` | Show or switch name announcements (default on). Beacons without a name are not announced. |
| `beacon.name_refresh` / `beacon.name_refresh <hours>` | Show or set how often a beacon's name is announced again (0-8760, default 4; `0` announces only on first sight or when the name changes). |
| `beacon.log on\|off` | Print each beacon heard (`BEACON <id> counter=... rssi=... snr=... batt=...mV name="..."`) and each name queued (`NAME <id> "..." (first\|changed\|refresh)`) to the serial terminal. Not saved; off after a reboot. |
| `beacon.stats` | Counters: beacons heard, observations reported, dropped, send failures, pending in the current batch, names sent. |

Setting up a repeater so the base picks it up (see "Repeater positions and names" and "Discovering the mesh, then locking it"
above):

```
beacon.channel <hex from 'beaconctl channel show'>
beacon.window 60            # below 80% of the beacon interval
set lat <degrees>           # normally set from the app using the phone's location
set lon <degrees>
set name <name>             # optional; the base shows the advertised name
advert                      # send a flood advert now, instead of waiting for flood.advert.interval (default 47 h)
beacon.log on               # bring-up: watch the beacons it hears
```

Stock repeater commands that matter here: `advert` (flood advert now), `set lat` / `set lon` (the position the base learns),
`set name`, `get public.key`, and `get` / `set flood.advert.interval <hours>` (3-168, default 47) for how soon a changed
position or name reaches the base. After a change, `beaconctl status` and `beaconctl repeater list` on the base show what
arrived.

## Managing repeaters over the mesh

`beaconctl repeater remote` reads or changes a repeater from the base, over the mesh, without a phone or a serial cable. It is a small
fixed set of operations, not a remote shell: nothing else can be sent. `beaconctl` never talks to the companion itself; it queues a job
and `beacon-ingest` (which owns the companion) runs it, so **`beacon-ingest` must be running** and the companion connected
(`beaconctl status` shows both). The design and the firmware behaviour behind it are in
[docs/plan/repeater-remote.md](docs/plan/repeater-remote.md).

```bash
beaconctl repeater remote north get all                       # everything below that can be read, in one go
beaconctl repeater remote north get name
beaconctl repeater remote north set name Hilltop North        # a name may have spaces
beaconctl repeater remote north set location 47.123456 -122.335167
beaconctl repeater remote north set advert.interval 120       # zero-hop advert, minutes: off, or an even number 60-240
beaconctl repeater remote north set flood.advert.interval 48  # flood advert, hours: off, or 3-168
beaconctl repeater remote north set beacon.window 45          # seconds, 1-3600
beaconctl repeater remote north set beacon.names off          # on | off
beaconctl repeater remote north set beacon.name_refresh 4     # hours, 0-8760
beaconctl repeater remote north get beacon.stats              # counters since boot, and what changed since the last reading
beaconctl repeater remote north get beacon.channel            # is it set, its hash, and whether it is this base's channel
beaconctl repeater remote north get clock                     # its clock, compared with the base's
beaconctl repeater remote north clock sync                    # set its clock to the base's (forward only), then read it back
beaconctl repeater remote north advert                        # send a flood advert now
beaconctl repeater remote north reboot --yes                  # restart it (asks first if you leave out --yes)
beaconctl repeater remote jobs                                # recent jobs; 'jobs 12' shows one in full
```

`north` is the repeater's name or its key prefix. Three things are deliberately **not** possible: setting or clearing `beacon.channel`
(the channel key never crosses the air; use the repeater's serial console), changing a password, and sending any other command.

**The three actions.** `clock sync` sets the repeater's clock to the base's time as it is when the command is sent, then reads the clock
back. A repeater's clock only moves forward, and it reports whole minutes, so a repeater already within a minute of the base is reported as
"already in step" (a success), and one that is *ahead* fails as `clock_ahead` (it cannot be set back; its clock stamps its adverts, and
nodes that have heard a later advert ignore an earlier one). The base's own clock has to be trusted (`beaconctl time set`). `advert` sends
a flood advert about 1.5 s after the repeater replies; use it after changing a name or position so the rest of the mesh hears it. `reboot`
restarts the repeater, which stops reporting for a short while and **loses the batch it is holding**; it asks for confirmation unless you
give `--yes`, and with no terminal it requires `--yes`. The board resets before it can answer, so a reboot is only ever reported as *sent*, never
confirmed: look for the repeater coming back (`get name`, or the next report). The base logs in again for the next job after a reboot.
**The admin password.** The first time, the repeater has to be given its admin password (the stock default is `password`; change it).
After one successful password login the repeater keeps this base's companion key as an admin in its access list, saved in its flash, so
later commands need no password, even after the repeater reboots. Supply it with `--password-file FILE` (first line, mode 600),
`$BEACON_REPEATER_PASSWORD`, or `--ask-password`; an interactive run asks by itself when the repeater says it needs one. It is **never**
an argument (visible in `ps`), it is at most 15 bytes (the firmware truncates silently), and it is erased from the database as soon as
ingest picks the job up. If the base's companion is ever replaced, its key changes and every repeater needs the password again.

**What you get back.** A `set` is read back to confirm it (`--no-verify` skips that, and then nothing is recorded). What the repeater
confirms is also written into the base's record at once, so `repeater list` and `check` do not wait for the next advert: a confirmed
name, position (`SOURCE` shows `remote`) and `beacon.window`, and the name and window of any `get`. The position recorded is the one you
sent: `get lat` / `get lon` print a 32-bit float truncated to 7 decimals, so a read-back is about 1 m off and is never recorded. A
later advert still replaces any of it (last write wins). `beaconctl check` also fails for a repeater whose report channel was read as
unset or different from this base's.

**Pacing.** The radio is shared and half duplex, and a reply is rebroadcast by the repeaters that hear it. After anything it receives (a
login answer, a reply, a late reply) or a timeout, the base waits `remote.step_gap_s` (default 3 s) before it transmits again, so a `get all`
(9 round trips) or a `set` (3) takes a few seconds longer but does not talk over its own echoes. A lost reply is first sent again on the same
route (a late answer to the first try still counts); only a second timeout resets the route and logs in again. Each job records its `retries`
and `route_resets` (`--json`, `jobs N`), which tell you how rough the path is. Lower `step_gap_s` for a quiet room, raise it for a long
multi-hop path.

**Waiting.** The command waits 30 s (`--timeout`); a flooded, multi-hop exchange can be slower. If the repeater has not answered yet it
says so, exits with 3, and the job carries on (`repeater remote jobs N` shows the result). `--no-wait` queues and returns the number,
`--json` prints the job. A job not started within 60 s expires, so nothing queued while ingest was down runs later. Exit codes: 0 done,
1 failed, 2 a mistake in the command, 3 still running.

**When it fails**, the message says why, because the repeater's firmware stays silent about most failures:

| Failure | Meaning |
|---|---|
| `no_ingest` | `beacon-ingest` is not running, or its companion is not connected. Nothing is queued. |
| `clock_untrusted` | The base's clock is not set (`beaconctl time set`). The base moves the companion's clock forward to its own, and must not do that from a wrong clock. |
| `no_key` | The base has never heard this repeater advertise, so it does not have its full key. |
| `needs_password` | The repeater does not have this base as an admin yet: give the admin password. |
| `not_admin` | A password was given but it was the guest password (or this key is not an admin there). |
| `no_reply` | Wrong password, out of range, a flood not forwarded, or the companion's clock behind what the repeater has seen from it. Tried three times: again on the same route, then after resetting the route and logging in again. |
| `unsupported` | The repeater does not know the command: not a beacon build, or firmware older than the names work. |
| `rejected` / `mismatch` | The repeater refused the value, or accepted it and reads back something else. |
| `clock_ahead` | `clock sync` only: the repeater's clock is ahead of the base's and cannot be set back. |
| `interrupted` | Ingest stopped, or lost the companion, while the job was running. The repeater may or may not have acted on it; read it back. |

Things to know in the field. The first change of anything on a fresh repeater turns its zero-hop adverts off (a firmware quirk: any
save zeroes an interval under 60 minutes), so every `set` reports `advert.interval`; set it to 60-240 if you want them. A changed
name or position is not advertised at once (the base already has it, being told directly); the repeater's own `advert` command sends one
now. If floods do not reach a repeater two or more hops away, a repeater with a denied wildcard region or `flood.max.unscoped 0` is
dropping the base's unscoped flood; see the plan for the fix (a default flood scope on the companion).

## Roadmap

| Phase | Scope |
|---|---|
| B0 | Done. Firmware repo emits golden test vectors for the report format. |
| B1 | Done and verified on hardware: wire decoder, serial framing, companion startup, `beaconctl listen`. |
| B2 | Done and verified on hardware: SQLite store, allowlist, high-water mark, dedupe, reset, rejection health states, clock handling. |
| B3 | Packaging for a week-long event on a Pi Zero 2 W: Docker image and compose bundle, watchdog, health check, operator guide. Done except the run on the Pi (B3.4). No retention or systemd install: each event starts a new database. |
| B4 | Web API and minimal offline map (MBTiles). |
| Also | Done and used on hardware: beacon names, repeater onboarding (positions and names from adverts, discovery, auto-add) and remote repeater management over the mesh. |

Later: location estimation, offline tile-cache builder, repeater timestamps, airtime measurement.

## License

Not yet chosen. The firmware fork is MIT licensed.

## Credits

Map data is © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available under the
[Open Data Commons Open Database Licence (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/). Regional extracts come from
[Geofabrik](https://download.geofabrik.de/) ("Data processed by Geofabrik GmbH and created by OpenStreetMap Contributors"). The map page shows the
credit in its corner and must keep doing so for any tiles made from that data ([docs/map-tiles.md](docs/map-tiles.md)). The map is drawn
with [Leaflet](https://leafletjs.com/) (BSD 2-Clause, vendored in `src/beacon_base/static/vendor/leaflet/`).
