# Beacon Base Plan

Covers the second half of milestone 3 (base ingest) and all of milestone 4 (allowlist, high-water mark, dedupe, reset)
in [beacon-project.md](beacon-project.md), and sets up the architecture for the map UI and location estimation
(milestone 6) so they slot in without rework.

Status: planning, nothing implemented. Decisions from the 2026-10-08 review are recorded in "Decisions" below.

## Decisions

| # | Decision |
|---|---|
| 1 | Python 3.11+ with `pyserial`, stdlib `sqlite3` and FastAPI. Static Leaflet page for the UI. |
| 2 | The base lives in a **separate repository** (the planning docs may move there too). See "Repository and wire format". |
| 3 | The map needs an **offline tile cache**. The deployment area is fixed, so a tile cache built from a manually set extent is a planned (future) feature. |
| 4 | **No counter-jump quarantine.** Any limit only reduces damage and does not prevent lockout, so instead the operator gets fast visibility of rejected beacons and a one-step reset. |
| 5 | **Late-report cutoff is in:** strict high-water mark plus the rule that the repeater report window stays below the beacon interval. |
| 6 | **Time is stamped on the base**, from the Pi's clock, set manually after boot. Repeater-side timestamps are parked (see "Time"). |
| 7 | Test base companion: **XIAO ESP32-S3 + Wio-SX1262** running `Xiao_S3_WIO_companion_radio_usb` (USB, no Bluetooth). |

## Goals and constraints

- Linux only. Target is a Raspberry Pi in the field; everything must also run on the Ubuntu development machine.
- Deployed **without internet**. The Pi is the only device that needs its clock set at startup.
- The base listens to a **companion node over USB**. Companion firmware changes are acceptable but should be minimal;
  processing lives in Linux processes.
- The map UI is **separate** from the service that talks to the companion. The UI can be restarted, redeployed or
  left off without losing reports.
- Must work with no hardware (fake companion, synthetic reports) so the pipeline and UI can be developed and tested at
  a desk.
- Nothing may need internet at runtime, including map tiles (offline tile cache, see B4).

## Architecture

```
 repeaters --flood GRP_DATA--> companion (USB CDC) --serial frames--> beacon-ingest ----> SQLite (WAL)
                                                                       (service)            ^     ^
                                                                                            |     |
                                                              beaconctl (CLI, provisioning) +     +--- beacon-web
                                                                                                       (HTTP API + map UI)
```

Three processes, one shared SQLite file as the only interface between them. No IPC, no message bus.

| Process | Job | Writes DB | Notes |
|---|---|---|---|
| `beacon-ingest` | Owns the serial port. Reconnects, provisions the channel, drains frames, runs the replay/dedupe pipeline, runs the estimator. | yes | systemd service, `Restart=always`. Only process that touches the port. |
| `beacon-web` | Read-mostly HTTP API and static map UI. | only operator actions (reset, edit), token protected | Binds to localhost by default. Can be stopped without affecting ingest. |
| `beaconctl` | Provisioning and diagnostics: add/remove beacons and repeaters, reset, generate channel key, dump frames, simulate. | yes | Changes take effect on the next report because ingest reads the allowlist and high-water marks from the DB each time. |

Why SQLite as the interface: ingest and UI never need to know about each other, operator changes (reset, allowlist) need
no restart or signalling, state survives restarts (required by the plan: high-water marks must persist), and it is easy
to inspect with the `sqlite3` CLI during field debugging. WAL mode allows one writer and many readers.

## Companion link

Uses the existing USB serial interface; **no firmware change is needed for the base to work**.

- Framing ([ArduinoSerialInterface.cpp](../../src/helpers/ArduinoSerialInterface.cpp)): host to device `'<'` + 2-byte
  little-endian length + payload; device to host `'>'` + length + payload. Max frame 176 bytes
  (`MAX_FRAME_SIZE`). Frames are length-prefixed, so resync after garbage means scanning for `'>'`, sanity-checking the
  length and the response code, and if that fails, closing and reopening the port and sending `APP_START` again.
- Startup sequence in `beacon-ingest`:
  1. Open the port by `/dev/serial/by-id/...` (not `ttyACM0`, which moves). Configurable path. The ESP32-S3 uses
     native USB, where opening the port with DTR/RTS toggling can reset the board: open with `dtr=False, rts=False`
     (set before opening) and treat an unexpected reboot as a normal reconnect.
  2. `CMD_APP_START` then `CMD_DEVICE_QUERY` (firmware version, `max_channels`).
  3. Optionally apply radio parameters with `CMD_SET_RADIO_PARAMS` (905775 kHz, 62500 Hz, SF 8, CR 6) when
     `manage_radio = true`. This avoids having to build a special companion image. Check that the companion persists
     them.
  4. `CMD_GET_CHANNEL` for the configured slot; `CMD_SET_CHANNEL` if the name or 16-byte secret differ. Only 128-bit
     secrets are supported by the companion, so the report channel key is 16 bytes (32 hex chars).
  5. Drain: send `CMD_SYNC_NEXT_MESSAGE` until `RESP_CODE_NO_MORE_MESSAGES`.
  6. Steady state: wake on `PUSH_CODE_MSG_WAITING` (0x83) and drain again; also poll every ~30 s as a safety net.
- Only `RESP_CODE_CHANNEL_DATA_RECV` (0x1B) frames with `data_type` 0xFFBE on the configured channel index are
  processed. Everything else is counted and dropped. (Draining also consumes any other queued companion messages, which
  is fine for a dedicated base companion.)
- Frame layout per [companion_protocol.md](../companion_protocol.md): SNR x4 at byte 1 (this is the SNR at the
  companion, not at the repeater, so it is stored but not used for estimation), channel index at byte 4, path length at
  byte 5 (0xFF = direct), data type at bytes 6-7, data length at byte 8, then the report.
- Timestamps: reports carry **no time**. The base stamps them with the Pi clock when drained (see "Time"). After an
  outage the companion's queue is replayed, so those timestamps are late. Mark observations drained in the first drain
  after connect as `late` rather than pretending they are live.

### Companion limits to be aware of

- The companion keeps received data in a RAM **offline queue**, dropping the oldest channel message when full, lost on
  reboot ([MyMesh.cpp:224](../../examples/companion_radio/MyMesh.cpp:224)). The default size is 16; both
  `Xiao_nrf52_companion_radio_usb` and `Xiao_S3_WIO_companion_radio_usb` set 256 (`OFFLINE_QUEUE_SIZE`), which is
  enough. The S3 env also enables an SSD1306 display; check it runs fine with no display attached.
- The companion only hears reports if it is within range of at least one repeater's rebroadcast. Where the base
  companion sits is a deployment decision.

## Firmware changes

| Change | Needed for MVP? | Notes |
|---|---|---|
| Companion firmware | **No** | Stock `Xiao_S3_WIO_companion_radio_usb` (test) or `Xiao_nrf52_companion_radio_usb` build. |
| Repeater report v2 | No, later | Optional batch sequence number (lets the base count lost batches, useful for milestone 5), an observation timestamp (parked, see "Time") and an optional signature (open question 2). The report `version` byte is already there; the base must reject and log unknown versions rather than guess. |

## Data model (SQLite)

Names are indicative; migrations are numbered from the start so schema changes are painless on a deployed Pi.

- `beacons`: `pubkey` (32 bytes, primary key), `prefix` (first 8 bytes, unique), `name`, `enabled`, `hwm` (highest
  accepted counter, NULL means "next report becomes the baseline"), `hwm_at`, `notes`. **This table is the allowlist.**
  Adding a beacon whose 8-byte prefix collides with an existing one is refused. Also denormalised for fast display:
  `last_accept_at`, `last_reject_at`, `last_reject_counter`, `rejects_since_accept`.
- `repeaters`: `prefix` (8 bytes, primary key), `pubkey` (optional), `name`, `lat`, `lon`, `enabled`. Authoritative
  repeater locations. Optionally importable from the companion's contact list (`CMD_GET_CONTACTS` returns lat/lon for
  repeaters that advertise), but the table wins.
- `raw_frames`: every beacon-report frame as received (`rx_time`, companion SNR, path length, payload, `late`). Audit
  trail and replay source for debugging; pruned by age.
- `observations`: one row per entry in a report: `rx_time` (wall clock), `rx_mono` (seconds since boot, from the
  monotonic clock), `boot_id`, `time_trusted`, `raw_id`, `repeater_prefix`, `beacon_prefix`, `counter`, `rssi`, `snr`,
  `batt_mv`, `status` and `reason`. Rejected observations are kept (with a reason) so the UI can explain
  why a beacon is not appearing. Unique index on `(repeater_prefix, beacon_prefix, counter)` for accepted rows.
- `transmissions`: the group for one `(beacon, counter)`: `first_seen`, `last_seen`, `n_repeaters`, `batt_mv`,
  `final` flag.
- `positions`: estimator output per transmission: `lat`, `lon`, `radius_m`, `method`, `n_repeaters`. Empty until the
  estimator exists.
- `service_status`: single row heartbeat from ingest (port connected, companion model and firmware, last frame time,
  counters, clock state) so the UI can show "ingest is down" instead of silently showing stale data.
- `clock_events`: each time the Pi clock is observed to step (or is set with `beaconctl time`), see "Time".

Retention: prune `raw_frames` and rejected observations by age (default 30 days); keep accepted observations and
transmissions. At 30 beacons, 5-minute intervals, ~3 repeaters per beacon this is roughly 26k observation rows a day,
small enough to keep for the whole deployment. SQLite on the Pi: WAL, `synchronous=NORMAL`, batched commits, to be kind
to the SD card.

## Replay and dedupe pipeline (milestone 4)

Implements "Replay checks at the base" from the main plan as a pure function over the store, one DB transaction per
report so it is idempotent and crash-safe. For each entry in a decoded report:

1. **Format.** Unknown report version or malformed length: log and drop the whole report.
2. **Allowlist.** Look up `beacon_prefix`. Not found: store as `unknown_beacon`, nothing else changes.
3. **Repeater known.** Unknown repeater prefix: store as `unknown_repeater` and do **not** touch the high-water mark
   (a rogue or misconfigured repeater must not be able to move it). It is shown in the UI as an action item.
4. **High-water mark.**
   - `hwm` is NULL: accept, set `hwm = counter` (baseline).
   - `counter < hwm`: reject as `replay`.
   - `counter == hwm`: same transmission as the current one, so it joins the group (not a replay).
   - `counter > hwm`: new transmission, accept and advance `hwm`.
5. **Dedupe.** Unique `(repeater, beacon, counter)`; a repeat is stored as `duplicate`. This also covers a replayed
   repeater report.
6. **Group.** Attach to the `(beacon, counter)` transmission and re-run the estimator for it.
7. **Reset.** `beaconctl reset <beacon>` (and later a UI button) sets `hwm = NULL`. Per the plan, do it while the beacon
   is known to be transmitting.

### Late-report cutoff

The high-water mark is strict: a report for transmission N that arrives after one for N+1 has been accepted is
rejected as `replay` (reason `late`). To keep that from dropping legitimate reports, **the repeater's `beacon.window`
must stay below the shortest jittered beacon interval** (270 s for the default 300 s +/-10%, so use at most ~240 s).
Within one report entries are in observation order, so one repeater never trips it; it only matters across repeaters
with different flush times. The base config records the expected beacon interval and `beaconctl check` warns when the
repeater windows it has been told about are too long. Note the current test build uses a 30 s beacon interval, so test
repeaters should use a window under ~25 s (`beacon.window 20`).

### Rejection visibility and quick reset

There is deliberately no counter-jump limit: any bound only reduces damage and cannot prevent a lockout (a lockout
being any state where a beacon's counter is at or below `hwm`, from a forged report, a flash-erased beacon or a
mistake). What the operator needs is to see it immediately and fix it in one step.

- Every beacon has a derived **health state**, computed from the denormalised columns on `beacons`:
  - `ok`: accepted a report within the last N expected intervals.
  - `rejected`: replay rejections since the last accept (`rejects_since_accept > 0`). Shows the offending counter
    against `hwm`, how many rejects and since when, which repeaters sent them. This is the lockout case.
  - `silent`: nothing heard (accepted or rejected) for more than N intervals.
  - `unconfigured`: reports seen from a beacon prefix that is not on the allowlist (listed separately, one line per
    prefix, with an "add" shortcut).
- **CLI.** `beaconctl status` prints one line per beacon with the state, sorted so `rejected` and `silent` come first.
  `beaconctl rejects [--beacon X]` lists recent rejected observations with reasons. `beaconctl beacon reset <name>` is
  the one-step fix; it prints the counters involved so the operator can sanity check it.
- **Web.** A banner whenever any beacon is `rejected`, listing them with a **Reset** button each (token protected),
  and the same states on the map markers (red for rejected, grey for silent).
- Reset sets `hwm = NULL`, so the next report becomes the new baseline. It does not stop a second forged report
  re-locking the beacon; the audit trail (`raw_frames`, rejected observations) is how the operator would notice.

## Time

Beacons have no clock and reports carry no time (decision 7 of the main plan), so the base stamps observations itself.

- The Pi has no internet in the field, so its clock is **set manually after boot** (`sudo timedatectl set-time ...`,
  wrapped by `beaconctl time`). Nothing else in the system needs a correct clock; replay protection uses counters only.
- Until it is set, the Pi's clock is wrong (typically the last shutdown time). So that early data is not lost:
  - each observation stores `rx_mono` (monotonic seconds since boot) and `boot_id` next to the wall-clock `rx_time`;
  - ingest watches for a clock step (wall minus monotonic changes by more than a few seconds) and records a
    `clock_events` row;
  - when a step happens, observations from the current boot get `rx_time` rewritten from `rx_mono` so they end up with
    correct times, and `time_trusted` becomes true;
  - until the operator has set the clock (or ingest has seen a step this boot), observations are `time_trusted = 0`,
    and the UI shows a "clock not set" banner. `beaconctl time` shows the current state.
- **Parked:** timestamping at the repeaters (the observation time at the repeater is the ideal timestamp). It needs the
  time set on every repeater at setup and a report v2 field; not needed for the solution to work. Also parked: a BLE
  GATT time service on the Pi so a phone can set the clock.

## Provisioning workflow

- `beaconctl channel generate` creates a random 16-byte key, stores it in the base config (mode 0600, never in git) and
  prints the hex to paste into each repeater's `beacon.channel`.
- `beaconctl beacon add <name> <pubkey-hex>`; the key comes from the beacon's serial `pubkey` command.
- `beaconctl repeater add <name> <pubkey-or-prefix> <lat> <lon>`; the key comes from the repeater's CLI.
- `beaconctl beacon reset <name>`, `... list`, `... status` (last heard, counter, battery, which repeaters hear it).
- `beaconctl time` shows and sets the clock state (see "Time"), `beaconctl status` and `beaconctl rejects` as above.
- `beaconctl listen` prints decoded reports live for bring-up (this is the "see it working" tool for the current
  hardware problem).
- `beaconctl simulate` feeds synthetic reports and a fake companion for UI work and tests.

## Testing

- **Shared wire format.** See "Repository and wire format": golden vectors from the firmware repo are checked into the
  base repo and its decoder tests consume them, so the two cannot drift.
- **Pipeline tests**, table driven, no hardware: lower counter rejected; equal counter from a different repeater
  grouped; same repeater twice deduped; unknown beacon; unknown repeater leaves `hwm` alone; reset then baseline;
  out-of-order within the allowed window and late beyond it; rejected state, `rejects_since_accept` and reset; 8-byte
  prefix collision at add time; clock step rewrites this boot's `rx_time`; crash between steps leaves a consistent DB.
- **Link tests** against a pty-based fake companion that speaks the frame protocol, including garbage bytes mid-stream,
  a dropped connection, the offline-queue drain and `MSG_WAITING` handling.
- **Hardware check** (checklist, not automated): beacon, repeater and base companion in range; confirm a report
  decodes in `beaconctl listen`, counters increase, a replayed capture is rejected, unplugging the companion and
  replugging recovers and drains.

## Phases

Each phase ends with something runnable.

**B0. Firmware repo prerequisite.**
Promote the host-side report test into the firmware repo and make it emit golden vectors (`BeaconReport.h`
encode, decode expectations as JSON). Small, done first so B1 has fixtures.

**B1. Ingest MVP (finishes milestone 3).**
New repo skeleton, wire decoder against the golden vectors, serial framing, companion startup sequence and channel
provisioning, `beaconctl listen`, `beaconctl channel generate`. Done when a real repeater's report is decoded and
printed from the XIAO S3 WIO companion.

**B2. Store and pipeline (milestone 4).**
Schema and migrations, allowlist, repeater table, high-water mark with the late-report cutoff, dedupe, grouping,
reset, rejection health states, `beaconctl status`/`rejects`/`time`, provisioning commands, full pipeline tests. Done
when the replay scenarios above pass, a lockout is visible in `beaconctl status` and cleared by one reset command, and
state survives a restart.

**B3. Service hardening and packaging.**
Reconnect and resync, heartbeat table, clock-step handling, retention job, systemd units, a udev rule giving the companion a stable name,
install script for a Pi and for the dev machine, logging to journald, a short operator README. Done when the service
survives unplugging the companion and reboots of the Pi unattended.

**B4. Web API and minimal map (start of milestone 6).**
`beacon-web`: read API (state, beacon history, transmission detail, ingest health) and a static Leaflet page showing
repeaters, last-heard beacons, battery, an ingest-health banner, the rejected-beacon banner with Reset buttons and a
"clock not set" banner. With a single repeater a beacon is drawn as a range circle around it; a placeholder
weighted-centroid estimator covers 2 or more. Operator actions behind a token. Tiles come from a local **MBTiles** file
served by `beacon-web` (`/tiles/{z}/{x}/{y}`), with online tiles allowed only as a development option and a plain
grid fallback when no tiles are present. Done when the map follows a beacon being walked past repeaters with no
internet.

**Later (not in this plan):**
- Real location estimation and walk-test tuning (milestone 6).
- A tile-cache builder: `beaconctl tiles` that downloads and packs tiles for a manually set extent and zoom range into
  an MBTiles file, run once on a connected machine and copied to the Pi. Check the tile provider's terms first;
  OpenStreetMap's public tile servers do not allow bulk downloads.
- Repeater timestamps and a Pi-side BLE time service (see "Time").
- Airtime measurement using batch sequence numbers (milestone 5); repeater signatures.

### Repository and wire format

The base is a separate repository. The report wire format is owned by the firmware repo
([BeaconReport.h](../../src/helpers/BeaconReport.h)) and mirrored in the base's `wire.py`. To stop them drifting:

- The firmware repo has a host test (B0) that writes `beacon_report_v1.json`: inputs and the exact encoded hex.
- The base repo checks a copy into `tests/fixtures/` with a note of the firmware commit it came from; its decoder test
  must pass against it. When the format changes (version byte), the fixture is regenerated and the base updated.
- The base repo also carries a short `docs/wire-format.md` so the format is documented where it is consumed.

Proposed layout of the base repo:

```
pyproject.toml, README.md
docs/              wire-format.md, operations.md (and the plan docs, if moved)
src/beacon_base/   wire.py  companion.py  link.py  store.py  pipeline.py  clock.py  estimate.py
                   ingest.py  api.py  cli.py
web/               static map UI (Leaflet)
tests/             unit, fake-companion link tests, fixtures/
deploy/            systemd units, udev rule, install script
```

## Risks

- **Pi clock.** Set by hand after each boot; if the operator forgets, timestamps are wrong until they do. The
  `rx_mono` rewrite and the "clock not set" banner limit the damage, and counters do not depend on time, so replay
  protection is unaffected.
- **Reports are only as good as the channel key.** Anyone with the key can inject reports (accepted risk 5 in the main
  plan). The unknown-repeater rule limits the damage and the rejected-beacon banner makes a lockout obvious; repeater
  signatures are the real fix.
- **Late and missing reports.** Companion queue overflow and an ingest outage both lose or delay reports silently. The
  heartbeat and `late` flag make it visible; batch sequence numbers (later) make it measurable.
- **Prefix identification.** Beacons and repeaters are identified by 8-byte prefixes. Collisions are refused at
  provisioning time and the prefix is not brute-forceable, but the base cannot independently verify the beacon
  signature, it trusts the repeater for that.

## Open questions

1. **Tile source for the offline cache.** Needs a provider whose terms allow bulk download (or self-generated tiles);
   decide before building `beaconctl tiles`.
2. **Companion on the Pi.** The test companion is the XIAO S3 WIO. If the field companion is a different board, check
   its USB build, queue size and how it behaves when the port is opened.
3. **Beacon interval vs repeater window.** The late-report rule needs the repeater window below the beacon interval.
   Decide whether the repeater firmware should clamp `beacon.window` (a small change) or whether the base-side warning
   is enough.
