# CLAUDE.md

Guidance for working in this repository.

## Project overview

Base station software for the MeshCore beacon tracker. Mobile beacons send signed zero-hop adverts; fixed beacon
repeaters hear them and publish batched reports on a private `GRP_DATA` channel; this repo's software receives those
reports through a MeshCore **companion node over USB**, enforces replay protection, stores everything, and serves a map.
Scale is about 30 beacons and 10 repeaters.

**Status: planning only.** No source code exists yet. The plans are the source of truth:

- [docs/plan/beacon-project.md](docs/plan/beacon-project.md): whole-project plan, decisions, security model, milestones.
- [docs/plan/beacon-base.md](docs/plan/beacon-base.md): this repo's design: architecture, companion link, data model,
  replay pipeline, phases B0-B4, open questions.

Read the base plan before designing or implementing anything. If a change contradicts a recorded decision, say so and
update the plan rather than silently diverging. The plan docs are dated and carry a "Decisions" table; keep them current.

## Repository locations

| Path | What |
|---|---|
| `.` (repo root) | **This repo** (read/write). Base software and plan docs. |
| `../MeshCore-beacon-client` | Firmware fork, **read-only**, expected as a sibling checkout (added as a working directory). Do not edit it from here. |

Paths in checked-in docs must be relative (this is a public repo): no absolute paths, home directories or usernames.

Relative links in `docs/plan/*.md` (`../../src/...`, `../companion_protocol.md`, `../number_allocations.md`) were written
for the firmware repo layout and resolve there, not here. Map them to the firmware repo:

| Needed for | Firmware repo file |
|---|---|
| Report wire format (authoritative) | `src/helpers/BeaconReport.h` |
| Serial framing (`<` / `>` + 2-byte LE length, max 176) | `src/helpers/ArduinoSerialInterface.cpp` |
| Companion command and response codes, frame layouts | `docs/companion_protocol.md` |
| Companion behaviour (offline queue, channels) | `examples/companion_radio/MyMesh.cpp` |
| Beacon advert format | `src/helpers/BeaconAdvert.h`, `examples/beacon/` |
| Repeater reporter | `examples/simple_repeater/MyMesh.cpp` (`WITH_BEACON_REPORTER`) |
| Data type allocations | `docs/number_allocations.md` |

When the wire format or a protocol detail matters, read it from the firmware repo; do not rely on memory or the plan's
prose alone.

## Architecture (planned)

Three processes, one shared SQLite file (WAL) as the only interface. No IPC, no message bus.

- `beacon-ingest`: systemd service; the only process that opens the serial port; reconnects, provisions the channel,
  drains frames, runs the replay/dedupe pipeline, writes the DB.
- `beacon-web`: FastAPI HTTP API plus static Leaflet UI; read-mostly; operator actions (reset, edit) behind a token;
  binds to localhost by default.
- `beaconctl`: CLI for provisioning, status, rejects, reset, clock, listen, simulate.

Planned package layout (from the base plan):

```
pyproject.toml, README.md, CLAUDE.md
docs/              plan/, wire-format.md, operations.md
src/beacon_base/   wire.py companion.py link.py store.py pipeline.py clock.py estimate.py ingest.py api.py cli.py
web/               static map UI (Leaflet)
tests/             unit, fake-companion link tests, fixtures/
deploy/            systemd units, udev rule, install script
```

## Constraints to keep in mind

- **Linux only.** Targets a Raspberry Pi in the field and the Ubuntu dev machine. Python 3.11+ (dev machine has 3.14),
  `pyserial`, stdlib `sqlite3`, FastAPI.
- **No internet at runtime**, including map tiles (local MBTiles). Pi clock is set manually after boot, so observation
  times are stamped on the base, with `rx_mono`/`boot_id` kept to repair them after a clock step.
- **Everything must run without hardware**: fake companion (pty) and synthetic reports drive tests and the UI.
- **Replay protection is solely at the base**: allowlist, per-beacon high-water mark, dedupe by
  `(repeater, beacon, counter)`, operator reset. Unknown repeaters must never move a high-water mark. There is
  deliberately no counter-jump limit; the answer to lockout is visibility plus one-step reset.
- **No companion firmware change is needed.** Open the serial port with `dtr=False, rts=False` (ESP32-S3 native USB can
  reset otherwise), use `/dev/serial/by-id/...`, and treat reboots as ordinary reconnects.
- Radio settings across all devices: 905.775 MHz, BW 62.5 kHz, SF 8, CR 4/6. The report channel key is 16 bytes.
- Reject and log unknown report versions; never guess at a format.
- Use numbered schema migrations from the start. Batch commits and keep `synchronous=NORMAL` (SD card wear).
- Secrets (channel key, web token) live in a mode 0600 config file and never in git.

## Build, test, run

Nothing is set up yet. When the skeleton is created (phase B1), keep these conventional entry points and update this
section with the real commands:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'     # editable install with test deps
pytest                       # unit, pipeline and fake-companion tests; must not need hardware
beaconctl listen             # bring-up: print decoded reports from the real companion
```

The firmware repo builds with PlatformIO, not from here. Environments the base cares about (run in the firmware repo):

| Env | Use |
|---|---|
| `Xiao_S3_WIO_companion_radio_usb` | Test base companion (XIAO ESP32-S3 + Wio-SX1262) |
| `Xiao_nrf52_companion_radio_usb` | Possible field base companion |
| `Xiao_nrf52_beacon` | Beacon |
| `Xiao_nrf52_beacon_repeater`, `Xiao_S3_beacon_repeater`, `Xiao_S3_WIO_beacon_repeater` | Beacon repeaters |

## Wire format and test fixtures

The firmware repo owns the report format; `wire.py` here mirrors it. Phase B0 (not done yet; the firmware repo has no
golden-vector test today) adds `beacon_report_v1.json` there. Copy it into `tests/fixtures/` with a note of the firmware
commit it came from, and make the decoder test consume it. Never hand-edit the fixture; regenerate it from the firmware
repo when the format changes.

## Working conventions

- Phases B0-B4 in the base plan are the work order; each ends with something runnable.
- Pipeline logic is a pure function over the store, one DB transaction per report, idempotent and crash-safe.
- Tests are table driven for the pipeline (see "Testing" in the base plan for the required scenarios).
- Do not commit to the firmware repo or modify it from here; if firmware needs to change (for example B0), describe the
  change and let the user do it there.
