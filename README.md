# MeshCore-beacon-base

Base station software for the MeshCore beacon tracking system. It collects beacon sightings reported by fixed repeaters,
rejects replayed or unknown beacons, and (later) estimates and maps where each beacon is.

> **Status: planning.** The design is written; nothing is implemented yet. See [docs/plan/beacon-base.md](docs/plan/beacon-base.md).

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

Links inside the plan docs that point at `../../src/...` or `../companion_protocol.md` refer to files in the firmware
repo, not this one.

## Requirements

- Linux (Raspberry Pi OS or Ubuntu).
- Python 3.11 or newer.
- A MeshCore companion node on USB. The test setup is a XIAO ESP32-S3 + Wio-SX1262 flashed with the
  `Xiao_S3_WIO_companion_radio_usb` firmware build; the field companion may be a XIAO nRF52 (`Xiao_nrf52_companion_radio_usb`).
- All devices in the mesh use the same radio settings: 905.775 MHz, BW 62.5 kHz, SF 8, CR 4/6.

## Planned workflow

Not available yet; this is the intended shape (see the phases in the base plan).

```bash
beaconctl channel generate                  # new 16-byte report channel key; paste into each repeater's beacon.channel
beaconctl beacon add <name> <pubkey-hex>    # allowlist a beacon (key from the beacon's serial `pubkey` command)
beaconctl repeater add <name> <key> <lat> <lon>
beaconctl listen                            # print decoded reports live (bring-up)
beaconctl status                            # one line per beacon; rejected and silent first
beaconctl beacon reset <name>               # clear a beacon's high-water mark
beaconctl time                              # show/set the Pi clock (no internet in the field)
```

## Roadmap

| Phase | Scope |
|---|---|
| B0 | Firmware repo emits golden test vectors for the report format (prerequisite, lives in the firmware repo). |
| B1 | Ingest MVP: wire decoder, serial framing, companion startup, `beaconctl listen`. |
| B2 | SQLite store, allowlist, high-water mark, dedupe, reset, rejection health states. |
| B3 | Hardening and packaging: reconnect, heartbeat, clock handling, systemd units, udev rule, install script. |
| B4 | Web API and minimal offline map (MBTiles). |

Later: location estimation, offline tile-cache builder, repeater timestamps, airtime measurement.

## License

Not yet chosen. The firmware fork is MIT licensed.
