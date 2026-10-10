# CLAUDE.md

Guidance for working in this repository.

## Project overview

Base station software for the MeshCore beacon tracker. Mobile beacons send signed zero-hop adverts; fixed beacon
repeaters hear them and publish batched reports on a private `GRP_DATA` channel; this repo's software receives those
reports through a MeshCore **companion node over USB**, enforces replay protection, stores everything, and serves a map.
Scale is about 30 beacons and 10 repeaters.

**Status: B1, B2 (store and pipeline) and the beacon names work (plan `docs/plan/beacon-names.md`, N0-N3) are done and
verified on hardware, as is the repeater onboarding work (`docs/plan/repeater-onboarding.md`, R1-R3: positions and names
from repeater adverts, plus discovery and auto-add). B3 onwards is not started.** The plans are the source of truth:

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

Package layout (`+` marks planned modules that do not exist yet):

```
pyproject.toml, README.md, CLAUDE.md
docs/              plan/, wire-format.md, operations.md
src/beacon_base/   wire.py       report and name-announcement decoders/encoders, mirror the firmware formats
                   names.py      cleaning untrusted names (sanitize_name), how names are shown (label)
                   companion.py  companion protocol: command builders, response parsers (pure, no I/O)
                   link.py       serial framing, FrameDecoder resync, CompanionLink request/response
                   ingest.py     CompanionSession: reconnect loop, channel provisioning, queue drain; Handler hooks
                   store.py      SQLite schema + numbered migrations, Store with the operator queries
                   pipeline.py   replay/dedupe pipeline: allowlist, high-water mark, grouping (one transaction per report)
                   health.py     per-beacon state: rejected / silent / ok / disabled
                   clock.py      boot id, monotonic offset, clock events and fixing provisional times
                   service.py    beacon-ingest: PipelineHandler (pipeline + clock-step detection) and entry point
                   admin.py      beaconctl commands that use the database (beacon, repeater, status, rejects, time, check)
                   config.py     TOML config + secrets.toml (mode 0600)
                   cli.py        beaconctl parser, listen, simulate, channel commands
                   runtime.py    logging and signal setup shared by entry points
                   fake_companion.py, simulate.py   fake companion on a pty and synthetic traffic
                   (+ estimate.py api.py)
web/               (+) static map UI (Leaflet)
tests/             unit, pipeline, fake-companion session tests, fixtures/ (golden vectors from the firmware repo)
deploy/            config.example.toml        (+ systemd units, udev rule, install script)
```

`CompanionSession` takes a `Handler`; `service.PipelineHandler` plugs the pipeline in (`on_report` receives a
`ReceivedReport` with `rx_wall`, `rx_mono`, `late`, the companion SNR and the raw payload; `on_drop` carries the raw frame
of a report that failed to decode). `on_synced` fires once the offline queue has been drained after connecting.

Repeater positions and names come from the **repeaters' own adverts**, heard by the base companion (`CompanionSession`:
contact list at connect, `PUSH_NEW_ADVERT`, bare `PUSH_ADVERT` then `CMD_GET_CONTACT_BY_KEY`; the companion is put in
manual-add mode so every advert is a full one) and applied by `Store.record_repeater_advert`: adverts with a valid non-0,0
position are kept in `repeater_adverts` whether or not the repeater is trusted; for a trusted repeater position and name are
overwritten, last write wins (`repeater locate` is the same, until the next advert); an advert without a position never
erases one. 0, 0 means **unlocated**: `Store.is_located` / `located_repeaters()` is the one place that decides, and the
estimator must use it. `repeater add --all` and the unknown-repeater list cover repeaters that reported **or advertised** (`Store.unknown_repeaters`).
**Auto-add** (`Store.autoadd/set_autoadd`, settings table, `beaconctl autoadd`, off by default, read per report so a running ingest sees changes):
repeaters are trusted by a report or an advert from an unknown one; beacons only by a report from a *trusted, enabled* repeater (that report
becomes the baseline). `Verdict.auto_added` / `AdvertEffect.added` say what was added. Names are accepted from any repeater.

Pipeline rules worth remembering (full text in the base plan): unknown repeater/beacon and disabled entries are stored but
change nothing (the repeater is checked first, so repeaters are onboarded before beacons); an unknown repeater can never move a high-water mark; a counter below the mark is `replay`, with reason
`late` if that transmission was already seen (not a lockout, not counted in `rejects_since_accept`) or `below_hwm`
(counts, and makes the beacon `rejected`); reset clears the mark and bumps `epoch`, and dedupe/grouping are per epoch.
Beacons are identified by the 8-byte prefix only (no full beacon key is stored; the reports are the source of truth).
Commands take a beacon as that prefix, or the first 6+ hex digits of it (`Store.beacon(ref)`); repeaters as a prefix or a
name (`Store.repeater(ref)`). **Names never identify anything**: beacon names are display text announced by repeaters
(`wire` type 0xFFBF, handled by `Pipeline.process_names`, only from known enabled repeaters, cleaned by
`names.sanitize_name`, latest wins, stored in `beacon_names` keyed by prefix whether or not the beacon is allowlisted) and
repeater names are optional operator text. Neither is unique. In messages use `names.label(name, prefix)`.
Once a database is deployed, schema changes must be a new numbered migration in `store.MIGRATIONS`; before the first
deployment migration 1 may still be edited, and the development database deleted.

## Constraints to keep in mind

- **Linux only.** Targets a Raspberry Pi in the field and the Ubuntu dev machine. Python 3.11+ (dev machine has 3.14),
  `pyserial`, stdlib `sqlite3`, FastAPI.
- **No internet at runtime**, including map tiles (local MBTiles). Pi clock is set manually after boot, so observation
  times are stamped on the base, with `rx_mono`/`boot_id` kept to repair them after a clock step.
- **Everything must run without hardware**: fake companion (pty) and synthetic reports drive tests and the UI.
- **Replay protection is solely at the base**: allowlist, per-beacon high-water mark, dedupe by
  `(repeater, beacon, counter)`, operator reset. Unknown repeaters must never move a high-water mark. There is
  deliberately no counter-jump limit; the answer to lockout is visibility plus one-step reset.
- **No companion firmware change is needed.** Open the serial port with RTS low always, and DTR by `companion.dtr`:
  `auto` keeps it low for Espressif native USB (VID 0x303A, the ESP32-S3 can reset otherwise) and raises it for every other
  or unknown device (an nRF52 on Adafruit TinyUSB only transmits while DTR is high), with one reopen the other way if the first
  `APP_START` gets no reply (`link.choose_dtr`, `CompanionSession._app_start`). Use `/dev/serial/by-id/...` and treat reboots
  as ordinary reconnects. See `docs/operations.md`.
- Radio settings across all devices: 905.775 MHz, BW 62.5 kHz, SF 8, CR 4/6. The report channel key is 16 bytes.
- Reject and log unknown report versions; never guess at a format.
- Use numbered schema migrations from the start. Batch commits and keep `synchronous=NORMAL` (SD card wear).
- Secrets (channel key, web token) live in a mode 0600 config file and never in git.

## Build, test, run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'     # editable install; needs pyserial, pytest
pytest                       # ~150 tests, ~10 s, no hardware needed (fake companion on a pty)
beaconctl simulate --provision   # fake companion + synthetic reports (+ matching beacons/repeaters); prints the pty path
beacon-ingest --port <path>      # store reports; then 'beaconctl status', 'rejects', 'beacon status <name>'
beaconctl listen                 # bring-up: print decoded reports without storing (only one of listen/ingest owns the port)
```

Config is `~/.config/beacon-base/config.toml` (or `-c`, or `$BEACON_BASE_CONFIG`); see `deploy/config.example.toml`. The
SQLite database defaults to `beacon.db` next to it. Set `[clock] assume_synced = true` on the dev machine, otherwise
observation times stay provisional until `beaconctl time set|confirm`.
`beaconctl channel generate` writes the channel key to `secrets.toml` next to it. Never commit either file; `.gitignore`
covers `config.toml`/`secrets.toml` at the repo root only, so keep real ones outside the repo.

Hardware: `ls -l /dev/serial/by-id` lists attached boards. Do not open a port or send commands to a board you have not
been told is the base companion: `listen` provisions a channel slot on whatever answers, and a beacon or repeater on the
same bus would receive binary frames as CLI input.

The firmware repo builds with PlatformIO, not from here. Environments the base cares about (run in the firmware repo):

| Env | Use |
|---|---|
| `Xiao_S3_WIO_companion_radio_usb` | Test base companion (XIAO ESP32-S3 + Wio-SX1262) |
| `Xiao_nrf52_companion_radio_usb` | Possible field base companion |
| `Xiao_nrf52_beacon` | Beacon |
| `Xiao_nrf52_beacon_repeater`, `Xiao_S3_beacon_repeater`, `Xiao_S3_WIO_beacon_repeater` | Beacon repeaters |

## Wire format and test fixtures

The firmware repo owns the report and name-announcement formats; `wire.py` here mirrors them. Phase B0 (firmware commit
`55fe473a`) added the golden-vector test for reports and the names work (`28bb4985`) the one for names; the fixtures
`tests/fixtures/beacon_report_v1.json` and `beacon_names_v1.json` are their output, and `tests/fixtures/README.md` records
the commits and how to regenerate. `tests/test_wire.py` consumes both. Never hand-edit the fixture; regenerate it from the
firmware repo when the format changes. `docs/wire-format.md` documents the format on this side.

## Working conventions

- Phases B0-B4 in the base plan are the work order; each ends with something runnable.
- Pipeline logic is a pure function over the store, one DB transaction per report, idempotent and crash-safe.
- Tests are table driven for the pipeline (see "Testing" in the base plan for the required scenarios).
- Do not commit to the firmware repo or modify it from here; if firmware needs to change (for example B0), describe the
  change and let the user do it there.
