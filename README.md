# MeshCore-beacon-base

Base station software for the MeshCore beacon tracking system. It collects beacon sightings reported by fixed repeaters,
rejects replayed or unknown beacons, and (later) estimates and maps where each beacon is.

> **Status: phase B2 (store and pipeline).** `beacon-ingest` decodes reports from a companion, applies the allowlist,
> high-water mark and dedupe, and stores everything in SQLite; `beaconctl` provisions beacons and repeaters and shows a
> lockout and its one-step fix. B1 is verified on hardware; B2 is tested against a fake companion only. No web UI yet (B4)
> and no packaging (B3). See [docs/plan/beacon-base.md](docs/plan/beacon-base.md).

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

Links inside the plan docs that point at `../../src/...` or `../companion_protocol.md` refer to files in the firmware
repo, not this one.

## Requirements

- Linux (Raspberry Pi OS or Ubuntu).
- Python 3.11 or newer.
- A MeshCore companion node on USB. The test setup is a XIAO ESP32-S3 + Wio-SX1262 flashed with the
  `Xiao_S3_WIO_companion_radio_usb` firmware build; the field companion may be a XIAO nRF52 (`Xiao_nrf52_companion_radio_usb`).
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
beaconctl status                             # lists beacons the repeaters report that are not on the allowlist yet
beaconctl beacon add beacon-001 <prefix>     # add one by the 16-character key prefix status shows
beaconctl repeater add north-ridge <key-or-prefix> 40.1234 -75.5678 --window 20
beaconctl check                              # sanity-check the setup
beacon-ingest                                # own the companion port and store reports (systemd service later)
beaconctl status                             # one line per beacon, rejected and silent first
```

`beaconctl` options: `-c <config>` (or `$BEACON_BASE_CONFIG`), `-v` for debug logging. The database defaults to
`beacon.db` next to the config file.

| Command | What it does |
|---|---|
| `channel generate [--force]` / `channel set <hex\|-> [--force]` / `channel show` | Create, store or print the report channel key. Replacing an existing key needs `--force`, since every repeater would need updating. |
| `beacon add <name> <prefix>` | Allowlist a beacon by the 8-byte key prefix its reports carry (16 hex characters, shown by `status`). A full 64-character key, for example from the beacon's serial `pubkey` command, is accepted and reduced to its prefix. A prefix or name that is already used is refused. |
| `beacon list` / `status <name>` / `remove` / `enable` / `disable` | The allowlist. |
| `beacon reset <name>` | Clear the high-water mark; the next report becomes the new baseline. Do it while the beacon is transmitting. |
| `repeater add <name> <key> <lat> <lon> [--window S]` / `list` / `remove` / `enable` / `disable` / `window <name> <S>` | The repeater table. Reports from repeaters not in it are stored but ignored. |
| `status [--hours H]` | Per-beacon state (`rejected`, `silent`, `ok`, `disabled`), plus beacons and repeaters heard but not on the lists. |
| `rejects [--beacon X] [--limit N]` | Recent observations that were not accepted, with the reason. |
| `time` / `time set "YYYY-MM-DD HH:MM:SS"` / `time confirm` | Show or fix the clock state. The Pi has no internet, so its clock is set by hand; times stay provisional until then. |
| `check` | Warn about missing setup and repeater report windows that are too long for the beacon interval. |
| `listen [--port P] [--json] [--count N]` | Bring-up view: print decoded reports without storing them. Only one of `listen` and `beacon-ingest` can have the port. |
| `ingest [--port P]` | Same as `beacon-ingest`. |
| `simulate [--provision] ...` | Run a fake companion on a pseudo-terminal with synthetic reports. |

### Adding beacons

Repeater reports identify a beacon by an 8-byte prefix of its public key, and that prefix is all the base keeps or needs:
the allowlist, high-water mark and dedupe all work on it. So the onboarding workflow is: configure the beacons, let them
transmit, run `beaconctl status`, and add each one it lists under "heard but not on the allowlist" with
`beaconctl beacon add <name> <prefix>`. To pre-register a beacon before it transmits, use the prefix (or the full key) from
its serial `pubkey` command.

### When a beacon is locked out

A beacon is locked out when its counter is at or below the stored high-water mark, for example after a forged report with
a huge counter or after its flash was erased. `beaconctl status` shows it first, as `rejected`, with the counters, the
repeaters that sent them and the fix:

```
STATE     NAME        PREFIX            HWM      HEARD   BATT   DETAIL
rejected  beacon-001  a0a1a2a3a4a5a6a7  1000000  12s ago 3.98V  3 replays rejected since 14:00:03 (counters 300-302, hwm 1000000) via north-ridge; fix: beaconctl beacon reset beacon-001
```

`beaconctl beacon reset beacon-001` clears it in one step. A report that arrives after a newer one from the same beacon was
accepted (a repeater with a long `beacon.window`) is rejected as `late` but is not a lockout; keep every repeater's
window below the beacon interval (`beaconctl check` warns).

### Working without hardware

```bash
beaconctl channel generate
beaconctl simulate --provision --interval 2     # adds simulated beacons/repeaters, prints the fake companion's path
beacon-ingest --port /tmp/fake-companion-XXXX/ttyFAKE
beaconctl status
```

## Roadmap

| Phase | Scope |
|---|---|
| B0 | Done. Firmware repo emits golden test vectors for the report format. |
| B1 | Done and verified on hardware: wire decoder, serial framing, companion startup, `beaconctl listen`. |
| B2 | Code complete, not yet run against real beacons: SQLite store, allowlist, high-water mark, dedupe, reset, rejection health states, clock handling. |
| B3 | Hardening and packaging: heartbeat table, retention, systemd units, udev rule, install script. |
| B4 | Web API and minimal offline map (MBTiles). |

Later: location estimation, offline tile-cache builder, repeater timestamps, airtime measurement.

## License

Not yet chosen. The firmware fork is MIT licensed.
