# MeshCore-beacon-base

Base station software for the MeshCore beacon tracking system. It collects beacon sightings reported by fixed repeaters,
rejects replayed or unknown beacons, and (later) estimates and maps where each beacon is.

> **Status: phase B1 (ingest MVP).** `beaconctl listen` decodes beacon reports from a companion and prints them; there is
> no database, allowlist or replay protection yet (B2) and no web UI (B4). Tested against a fake companion; the check
> against real hardware is outstanding. See [docs/plan/beacon-base.md](docs/plan/beacon-base.md).

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

Planned: three cooperating processes sharing one SQLite database (WAL mode) as their only interface. Only the first
exists so far, as the `beacon_base.ingest` session library driven by `beaconctl listen`:

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
beaconctl listen                             # provision the companion's channel and print decoded reports
```

`beaconctl` options: `-c <config>` (or `$BEACON_BASE_CONFIG`), `-v` for debug logging.

| Command | What it does |
|---|---|
| `channel generate [--force]` | Create the report channel key. Refuses to replace an existing key without `--force`, since every repeater would need updating. |
| `channel show` | Print the key again, for provisioning another repeater. |
| `listen [--port P] [--json] [--count N]` | Connect to the companion, set up the channel, drain its queue and print one line per observation. Entries drained right after connecting are marked `late`. |
| `simulate [--interval S] [--beacons N] [--repeaters N]` | Run a fake companion on a pseudo-terminal with synthetic reports. Run `beaconctl listen --port <printed path>` in another terminal. |

### Working without hardware

```bash
beaconctl channel generate
beaconctl simulate --interval 2          # prints the fake companion's path
beaconctl listen --port /tmp/fake-companion-XXXX/ttyFAKE
```

## Still to come

```bash
beaconctl beacon add <name> <pubkey-hex>    # allowlist a beacon (key from the beacon's serial `pubkey` command)
beaconctl repeater add <name> <key> <lat> <lon>
beaconctl status                            # one line per beacon; rejected and silent first
beaconctl beacon reset <name>               # clear a beacon's high-water mark
beaconctl time                              # show/set the Pi clock (no internet in the field)
```

## Roadmap

| Phase | Scope |
|---|---|
| B0 | Done. Firmware repo emits golden test vectors for the report format. |
| B1 | Code complete: wire decoder, serial framing, companion startup, `beaconctl listen`. Hardware check outstanding. |
| B2 | SQLite store, allowlist, high-water mark, dedupe, reset, rejection health states. |
| B3 | Hardening and packaging: reconnect, heartbeat, clock handling, systemd units, udev rule, install script. |
| B4 | Web API and minimal offline map (MBTiles). |

Later: location estimation, offline tile-cache builder, repeater timestamps, airtime measurement.

## License

Not yet chosen. The firmware fork is MIT licensed.
