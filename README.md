# MeshCore-beacon-base

Base station software for the MeshCore beacon tracking system. It collects beacon sightings reported by fixed repeaters,
rejects replayed or unknown beacons, and (later) estimates and maps where each beacon is.

> **Status: phase B2 (done) plus beacon names and repeater onboarding (done).** `beacon-ingest` decodes reports and beacon name announcements from a companion,
> applies the allowlist, high-water mark and dedupe, and stores everything in SQLite; `beaconctl` provisions beacons and
> repeaters by key prefix and shows a lockout and its one-step fix. B1, B2, the beacon names (repeater
> firmware `28bb4985` in the firmware repository) and the repeater onboarding (positions from adverts, discovery and
> auto-add) are verified on hardware.
> No web UI yet (B4) and no packaging (B3). See [docs/plan/beacon-base.md](docs/plan/beacon-base.md) and
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

Three cooperating processes sharing one SQLite database (WAL mode) as their only interface. The web UI does not exist yet:

| Process | Job |
|---|---|
| `beacon-ingest` | systemd service. Owns the companion's serial port, decodes reports, runs the replay/dedupe pipeline. |
| `beacon-web` | HTTP API and Leaflet map UI. Separate from ingest, so it can be restarted or left off without losing reports. |
| `beaconctl` | CLI for provisioning (beacons, repeaters, channel key), status, rejects, reset, clock, simulation. |

Everything must work without hardware: a fake companion and synthetic reports allow the pipeline and UI to be built and
tested at a desk.

## Related repositories

| Repo | Role |
|---|---|
| [MeshCore-beacon-client](https://github.com/phosphor-radio/MeshCore-beacon-client) | Firmware fork: beacon (`examples/beacon`), beacon repeater (`examples/simple_repeater` with `WITH_BEACON_REPORTER`) and the stock companion firmware the base uses. Owns the report wire format (`src/helpers/BeaconReport.h`). |
| this repo | Base software and the project planning docs. |

## Documentation

- [docs/plan/beacon-project.md](docs/plan/beacon-project.md): overall project plan, decisions, security model, milestones.
- [docs/plan/beacon-base.md](docs/plan/beacon-base.md): base design, data model, pipeline, phases B0-B4.
- [docs/wire-format.md](docs/wire-format.md): the report format and the companion frame that carries it.
- [docs/operations.md](docs/operations.md): running on real hardware, starting with the companion's USB serial (DTR) settings.

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
| `repeater list` / `remove` / `enable` / `disable` / `window <S>` | The repeater table, which also shows where each position came from. Commands take the key prefix (six or more hex digits) or the name. Reports from repeaters not in the table are stored but not counted. |
| `status [--hours H]` | Per-beacon state (`rejected`, `silent`, `ok`, `disabled`), then every repeater and beacon that has been seen but is not trusted yet, ready to add. |
| `autoadd [beacons\|repeaters\|all [on\|off]]` | Show or set automatic trust for newly seen repeaters and beacons (both off by default). A setting in the database, so a running `beacon-ingest` uses it at once. `status` warns and `check` fails while it is on. |
| `rejects [--beacon X] [--limit N]` | Recent observations that were not accepted, with the reason. |
| `time` / `time set "YYYY-MM-DD HH:MM:SS"` / `time confirm` | Show or fix the clock state. The Pi has no internet, so its clock is set by hand; times stay provisional until then. |
| `check` | Fails on missing setup, repeaters with no location and repeater report windows that are too long for the beacon interval. |
| `listen [--port P] [--json] [--count N]` | Bring-up view: print decoded reports and name announcements without storing them. Only one of `listen` and `beacon-ingest` can have the port. |
| `ingest [--port P]` | Same as `beacon-ingest`. |
| `simulate [--provision] ...` | Run a fake companion on a pseudo-terminal with synthetic reports. |

### What the base sets on the companion at startup

Every time `beacon-ingest` or `beaconctl listen` connects it makes the companion match the mesh, writing a setting only when
it differs (the companion keeps them in flash):

| Setting | From | Notes |
|---|---|---|
| Radio: frequency, bandwidth, SF, CR | `[radio]` `freq_khz`, `bw_hz`, `sf`, `cr` (905775 kHz, 62500 Hz, SF 8, CR 6) | Sent without the client-repeat flag, so the companion does not repeat. `companion.manage_radio = false` only warns instead. |
| Path hash mode | `[radio]` `path_hash_mode` (default `2`, 0-2) | Hash size in the paths of the packets the companion floods; kept the same as the rest of the mesh. Needs companion firmware v10. A failure is logged and ignored. |
| Report channel | `companion.channel_index` / `channel_name`, the key in `secrets.toml` | Slot 0 holds the built-in Public channel, so the default slot is 1. |
| Manual-add mode | `companion.manual_add_contacts` (on) | Every advert reaches the base in full; see "Repeater positions and names". |

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

## Roadmap

| Phase | Scope |
|---|---|
| B0 | Done. Firmware repo emits golden test vectors for the report format. |
| B1 | Done and verified on hardware: wire decoder, serial framing, companion startup, `beaconctl listen`. |
| B2 | Done and verified on hardware: SQLite store, allowlist, high-water mark, dedupe, reset, rejection health states, clock handling. |
| B3 | Hardening and packaging: heartbeat table, retention, systemd units, udev rule, install script. |
| B4 | Web API and minimal offline map (MBTiles). |

Later: location estimation, offline tile-cache builder, repeater timestamps, airtime measurement.

## License

Not yet chosen. The firmware fork is MIT licensed.
