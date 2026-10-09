# Beacon Project Plan

## Goal

Mobile **beacons** periodically transmit a signed, minimal packet. Specialized **beacon repeaters** with known
locations hear those packets and, instead of forwarding them, publish their own report ("I heard beacon X at RSSI Y")
into the mesh on a private channel. A **base** collects the reports and estimates each beacon's position from the known
repeater locations.

```
 beacon (nRF52) --zero-hop advert--> beacon repeater (fixed, known location)
                                          |  batched report: beacon id, counter, RSSI, SNR, battery
                                          v
                              private channel (GRP_DATA, flood through mesh)
                                          |
                                          v
                       base: companion node + host app (Pi) -> estimates beacon location
```

Scale: about **30 beacons** and **10 repeaters**.

## Status

Last updated: 2026-10-08. Statuses: Not started, In progress, Code complete (builds, not verified on hardware), Done
(verified).

| # | Milestone | Status | Notes |
|---|---|---|---|
| 1 | Beacon firmware (`examples/beacon`) | In progress | Flashed and transmitting; advert received by a USB companion. Android app, sleep current and power not yet checked. |
| 2 | Beacon repeater: filter and RSSI/SNR capture | Code complete | Build env `Xiao_nrf52_beacon_repeater`. Not run on hardware. |
| 3 | Batched report packet, base ingest | In progress | Repeater side is code complete (format in `src/helpers/BeaconReport.h`, host-tested). Base ingest not started. |
| 4 | Base: allowlist, high-water mark, dedupe, reset | Not started | |
| 5 | Airtime measurement, batching and hop tuning | Not started | |
| 6 | Location estimation, walk tests | Not started | |

### Milestone 1 detail

Implemented:
- Signed zero-hop `ADV_TYPE_SENSOR` advert with battery (`feat1`) and beacon marker (`feat2`, 0xBE01), an optional
  name, and 8 zeroed reserved bytes. Encoding is in `src/helpers/BeaconAdvert.h`, shared with the future repeater.
- Counter-as-timestamp clock, with reserve-ahead persistence (one flash write per 256 sends).
- Own timer, 300 s interval with +/-10% jitter, randomised first send after boot.
- Radio warm-sleeps after each send; MCU idles in `delay()`. Beacon never enters RX.
- Serial CLI: `pubkey`, `ver`, `advert`, `reboot`, `get interval|name|counter|batt|radio|tx`, `set interval|name`,
  `set radio <freq>,<bw>,<sf>,<cr>`, `set tx <dbm>`.
- Radio settings are saved in the beacon's prefs and applied immediately. The beacon build defaults are 905.775 MHz,
  BW 62.5 kHz, SF 8, CR 4/6 (`LORA_CR=6`) and 22 dBm, set in the `Xiao_nrf52_beacon` env. Repeaters and the base must
  use the same values, so decision 14 now also implies CR 4/6 and 905.775 MHz.

Deviations from the plan text:
- Sends the raw packet straight to the radio instead of calling `sendZeroHop()`, because the dispatcher loop would put
  the radio into RX. Wire format is identical.
- When a name is present the reserved bytes follow a NUL after the name, so name parsers ignore them.
- No separate timer-only `board.sleep()` path was written; sleep relies on FreeRTOS tickless idle (unmeasured).

Verified on hardware (2026-10-06):
- The beacon transmits a signed advert on the configured radio settings, and a USB companion shows `beacon-001`.
- Bug found and fixed: after warm sleep, `startTransmit()` does not wake the radio (unlike `startReceive()`), so the first
  send failed. `sendAdvert()` now calls `wakeUp()` first. Failures report a reason on serial.

Still to do for milestone 1:
- Confirm the advert, with its feature fields, name and reserved bytes, displays correctly in the Android app (open
  question 3). The companion saw it, but the app has not been checked.
- Confirm the repeater-side view of the advert is intact over several sends (counter increments, signature valid).
- Measure sleep and average current, and compare with the sizing estimates.
- Decide whether to turn off the TX LED to save power.

### Milestone 2 and repeater side of milestone 3 detail

Implemented (in `examples/simple_repeater`, compiled only with `-D WITH_BEACON_REPORTER=1`, so the stock repeater is
unchanged; build with `pio run -e Xiao_nrf52_beacon_repeater`, which sets the shared radio settings from decision 14).
For testing on an ESP32-S3 use `Xiao_S3_beacon_repeater` (SX1262 module on the header pins) or
`Xiao_S3_WIO_beacon_repeater` (Wio-SX1262 kit), which use the same flags and are the same code:
- `MyMesh::onAdvertRecv` recognises a beacon: valid signed advert, **direct** route with an empty path (a flood advert
  carrying the marker is ignored, since real beacons never flood), `ADV_TYPE_SENSOR` and the `feat2` marker. The beacon
  packet is never forwarded (`Mesh` does not route direct adverts).
- RSSI is captured in `logRxRaw()`, right after the radio read, so it belongs to the packet being processed. SNR comes
  from the packet. No allowlist, replay check or rate limit at the repeater.
- Report wire format in `src/helpers/BeaconReport.h`, shared with the future base: `GRP_DATA`, `data_type` 0xFFBE (dev
  range), header 10 bytes (version, repeater key prefix 8, count) plus 16-byte entries (beacon key prefix 8, counter 4,
  RSSI 1, SNR x4 1, battery mV 2). A full packet carries 9 entries (154 of 165 data bytes).
- Batching: a packet is flushed when full or `beacon.window` seconds after its first entry (default 60). Sent with
  `sendFloodScoped()` on the repeater's default scope. If no packet can be allocated the batch is kept and retried after
  5 s; new observations are counted as dropped while it is full.
- CLI (serial or remote admin): `beacon.channel <32|64 hex>` / `beacon.channel clear` / `beacon.channel` (shows set and
  the hash byte only, never the secret), `beacon.window [secs]`, `beacon.log on|off` (prints each beacon heard),
  `beacon.stats`. The channel secret and window are saved in `/beacon_rpt`. Without a channel set the repeater only
  logs, which is the milestone 2 behaviour.

Not yet done or verified:
- Never run on hardware: no check that RSSI/SNR look right, that the beacon is seen and not forwarded, or that a report
  reaches a companion on the same channel.
- Hop limit for reports (open question 1) is not applied; reports flood like any other `GRP_DATA`.
- Repeater signatures on reports (open question 2) and forwarding the reserved beacon bytes (open question 3) are not
  implemented.
- The report window, 8-byte prefixes and entry layout are first guesses pending the airtime measurements in milestone 5.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Beacon hardware is **nRF52** | Minimal power consumption is the key requirement. See also [nrf52_power_management.md](../nrf52_power_management.md). |
| 2 | Beacon firmware is a **new `examples/beacon`**, not a companion or modified SensorMesh | Companion drags in BLE, UI and contact/channel storage. SensorMesh keeps the radio in RX and carries an ACL, telemetry, alerts and region maps. The beacon needs none of those. |
| 3 | Beacon transmits a **signed zero-hop advert** | Ed25519 signature makes identity unforgeable. `sendZeroHop()` uses direct routing with `path_len = 0` ([Mesh.cpp:717](../../src/Mesh.cpp:717)), so normal repeaters never forward it. |
| 4 | Beacon identity = its **public key** | Already unique and carried in every advert; no extra ID field needed. |
| 5 | Beacon **never receives** | TX only, to save power. Radio sleeps between beacons. |
| 6 | Beacon interval is about **5 minutes** | Stock `advert.interval` has a 60-minute minimum, so the beacon needs its own timer. |
| 7 | Beacons have **no clock**. The advert timestamp field carries a **local monotonic counter** | Avoids setting the clock on every beacon. The field is covered by the signature, so no format change is needed. |
| 8 | **Replay protection lives only at the base**, using a per-beacon high-water mark and report dedupe | Repeaters stay simple and hold no per-beacon state. Resetting a beacon's last-seen value is done in one place, the base. No wall-clock plausibility check; dedupe rejecting old values is sufficient for this use case. |
| 9 | **Beacon allowlist at the base only** | Enforcing it at repeaters means updating every repeater whenever a beacon is added or removed, for minimal value. |
| 10 | **Airtime/DoS attacks are out of scope** | There are many other ways to jam a LoRa mesh without spoofing a beacon, so no defense is built for it (no repeater rate limit). |
| 11 | Beacon advert reuses **`ADV_TYPE_SENSOR`** | Supported by the Android app, which helps troubleshooting. See "Distinguishing beacons" below. |
| 12 | Reports are **`PAYLOAD_TYPE_GRP_DATA` on a private channel** | Avoids configuring every repeater with the base's identity. Repeaters only need the channel PSK. |
| 13 | **Base is a companion node plus a host app** (likely on a Pi) | The companion delivers inbound `GRP_DATA` to the host as `RESP_CODE_CHANNEL_DATA_RECV` ([companion_protocol.md](../companion_protocol.md)). |
| 14 | Radio settings are **SF 8, BW 62.5 kHz** on **all devices in the mesh** (beacons, repeaters, base); beacon TX power is **22 dBm** | Range-oriented. A repeater has one radio, so beacons and repeaters must share settings; this is the plan, so no repeater retuning is needed. See "Sizing" for the airtime this implies. |
| 15 | Beacon advert carries **battery voltage** and reserves **8-16 extra bytes** | Fleet monitoring now, room for future data without a format change. |

## Security

### What the signature gives us
An advert is signed over `pub_key + timestamp + app_data` ([Mesh.cpp:413-435](../../src/Mesh.cpp:413)), and verified at
[Mesh.cpp:272-288](../../src/Mesh.cpp:272). Nobody without the private key can forge an advert for a given beacon.

### What it does not give us: replay
`Mesh` does not check advert timestamps. Its only protection is the finite `wasSeen` duplicate table, so a captured
advert can be replayed later or elsewhere and a repeater would accept it and report it. The companion path does check
for increasing timestamps ([BaseChatMesh.cpp:133](../../src/helpers/BaseChatMesh.cpp:133)); the repeater's
`onAdvertRecv` ([MyMesh.cpp:650](../../examples/simple_repeater/MyMesh.cpp:650)) does not, and we are deliberately
leaving it that way.

### Beacon counter
Beacons have no clock. The advert timestamp field carries a counter that increments on every transmission. The
counter is signed along with the key, so it cannot be altered. A `uint32` at one send per 5 minutes does not overflow
in practice.

### Replay checks at the base (authoritative)
1. **Allowlist.** Only provisioned beacon keys are tracked.
2. **High-water mark per beacon key.** Reject any report whose counter is not greater than the highest accepted counter.
   Reports with the same counter from different repeaters are the same transmission and are grouped, not rejected.
3. **Group by `(beacon key, counter)`.** All repeater reports for one transmission share this key and feed one
   location estimate.
4. **Report dedupe.** Drop repeats of the same `(repeater, beacon key, counter)`, which also covers replayed repeater
   reports on the channel and rejects future replays of old values.
5. **Reset.** An operator action at the base clears a beacon's high-water mark. The next report becomes the new
   baseline, so do this when the beacon is known to be transmitting.

### Accepted residual risks
- **Delayed replay of an unheard advert**: an advert captured somewhere no repeater heard it, and replayed later, is
  newer than the high-water mark and will be accepted. No wall-clock check is applied; accepted for this use case.
- **Wormhole/relay**: an attacker relaying a live advert from one place to another cannot be stopped cryptographically.
  The base can still sanity-check, for example the same beacon appearing at distant repeaters at once.
- **Beacon trackability**: adverts are public and carry the public key, so anyone with a radio can track a beacon even
  though the report channel is private.
- **Airtime attacks**: out of scope (decision 10).
- **Forged reports**: anyone holding the channel PSK can forge reports. Whether repeaters should sign reports is an open
  question.

## Beacon firmware (`examples/beacon`)

### Behavior
Each wake cycle:
1. Wake from the RTC timer.
2. Build the advert with `createAdvert()` and send with `sendZeroHop()`.
3. Put the SX126x to sleep (`CustomSX1262Wrapper` already provides a sleep call).
4. Sleep the MCU until the next interval.

The beacon never enters RX.

### Advert contents
`MAX_ADVERT_DATA_SIZE` is 32 bytes ([MeshCore.h:12](../../src/MeshCore.h:12)). Layout of `app_data`
([AdvertDataHelpers.h](../../src/helpers/AdvertDataHelpers.h)):

| Bytes | Field |
|---|---|
| 1 | flags: `ADV_TYPE_SENSOR` + `ADV_FEAT1_MASK` + `ADV_FEAT2_MASK` (+ `ADV_NAME_MASK` if a name is included) |
| 2 | `feat1`: battery voltage in mV (must be non-zero, or the encoder omits the field) |
| 2 | `feat2`: beacon marker/version (see below) |
| 8-16 | reserved extra bytes |
| optional | short name for the Android app |

That leaves roughly 27 bytes after the flags and two feature fields, so reserved bytes plus a short name fit. No
location. The counter is in the packet's timestamp field, not in `app_data`.

`AdvertDataBuilder` only encodes valid UTF-8 names, so the beacon builds the reserved bytes itself rather than passing
them as a name. How the Android app displays or tolerates the extra bytes must be tested (open question).

### Distinguishing beacons from real sensors
Both use `ADV_TYPE_SENSOR`, and real sensors also send zero-hop adverts. The repeater therefore identifies a beacon by
a fixed marker value in `feat2` (the feature fields are currently unused, marked FUTURE). Real sensors will not carry
it, so repeaters do not report them. The marker is not a security control; the base's allowlist is.

### Sizing
Estimates, to be validated by measurement.
- An advert is about 125 bytes (32 key + 4 counter + 64 signature + roughly 20-30 `app_data` + header). At SF8,
  BW 62.5 kHz, CR 4/5, 8-symbol preamble that is roughly **0.7 s** of airtime. The signature is the airtime price of
  anti-spoofing.
- 30 beacons at one send per 5 minutes is about 7% channel utilization from beacons alone.
- Beacons cannot listen before talking. With unsynchronized timers, pure-ALOHA arithmetic suggests on the order of
  10-15% of beacon transmissions may collide at a given receiver. Add small **random jitter** to the interval so two
  beacons whose timers drift together do not collide repeatedly.
- At 22 dBm the SX126x draws very roughly 100-120 mA during TX (check the datasheet and measure the board). That is
  about 0.02 mAh per beacon transmission, around 6-7 mAh per day at 288 sends, before sleep current.

### Reuse from existing code
- Identity generation and storage, prefs, and serial CLI from the sensor/repeater, for provisioning (view public key,
  set interval).
- nRF52 board support and sleep hooks ([NRF52Board.h](../../src/helpers/NRF52Board.h)).

### New work
- Timer-driven transmit loop with its own interval (not `advert.interval`), with jitter.
- Timer-only sleep path. Existing `board.sleep()` is written around waking on a received packet.
- Skip listen-before-talk/CAD where acceptable, since the beacon has no RX.
- A small `RTCClock` subclass whose `getCurrentTime()` returns the counter and increments it on each send, so
  `createAdvert()` ([Mesh.cpp:418](../../src/Mesh.cpp:418)) stamps the counter with no other changes.
- **Flash wear**: writing the counter every send (about 288 per day) would wear out nRF52 flash. Use reserve-ahead:
  persist `counter + 256` every 256 sends (about once a day), and on boot resume from the persisted value. The counter
  may skip numbers after a reboot, which is harmless because only increase matters.
- **Counter loss**: if flash is erased but the private key survives, the counter restarts low and the base rejects the
  beacon. Store the counter with the key, and treat any reset as a key rotation or an operator reset at the base.
- Battery voltage read each cycle into `feat1`.

## Beacon repeater firmware (customization of `simple_repeater`)

### Behavior
- Receive zero-hop adverts, verified by `Mesh`, then in `onAdvertRecv`:
  1. Confirm it is zero-hop, `ADV_TYPE_SENSOR`, and carries the beacon marker in `feat2`.
  2. Read `getLastRSSI()` and `getLastSNR()` from the radio driver ([Dispatcher.h:79](../../src/Dispatcher.h:79)).
  3. Add an observation to the pending batch.
- **No allowlist, no replay checks, no rate limit** at the repeater (decisions 8-10).
- Do not forward the beacon packet. Zero-hop is not forwarded by `Mesh` anyway, but the hook must not re-flood it.
- Batch observations and flush on a timer. Batching is required, not optional (see below).

### Report packet
- `PAYLOAD_TYPE_GRP_DATA` via `createGroupDatagram()` then `sendFloodScoped()` ([Mesh.cpp:540](../../src/Mesh.cpp:540)).
  `BaseChatMesh::sendGroupData` ([BaseChatMesh.cpp:539](../../src/helpers/BaseChatMesh.cpp:539)) is the reference.
- The 16-bit `data_type` comes from the dev range `0xFF00-0xFFFF` until the project works, then an allocation is
  requested in [number_allocations.md](../number_allocations.md).
- Payload is a batch of entries. Proposed entry (about 16 bytes):
  - beacon public key **prefix, 8 bytes**. Shorter prefixes could be brute-forced by an attacker generating keys with a
    matching prefix and attributed to the real beacon; 8 bytes makes that infeasible.
  - beacon counter, 4 bytes (could be truncated to the low bits, since the base knows the high-water mark)
  - RSSI and SNR, 1 byte each
  - battery, 2 bytes
- Plus a small header (repeater identification, batch info). The base maps the prefix to a full key through its
  allowlist.
- Capacity is about 165 data bytes per packet, so roughly 9-10 entries per packet.
- The reserved 8-16 beacon bytes are not forwarded by default (every repeater would repeat the same bytes). Whether to
  forward them is an open question.

### Repeater provisioning
- Private channel PSK. Not shipped in source.
- Repeater location (already available via `lat`/`lon` prefs).

## Base

Detailed plan, architecture and phases: [beacon-base.md](beacon-base.md).

- A companion node on the private channel; the host app receives `RESP_CODE_CHANNEL_DATA_RECV` frames.
- Enforces everything in "Replay checks at the base", with state persisted so a restart does not lose high-water marks.
- Keeps a table of repeater locations and of the latest observations per beacon, and records battery levels.
- Estimates location from RSSI/SNR across repeaters that reported the same `(beacon, counter)` (weighted centroid
  first, trilateration or RSSI path-loss modelling later).
- Provides the operator reset for a beacon and manages the beacon allowlist.

## Sizing: reports are the airtime risk

Back-of-envelope, to be validated. Assume each beacon is heard by about 3 of the 10 repeaters.
- Per 5-minute cycle that is about 90 observations, about 1.4 KB, about 9 full packets, about 8 s of airtime for the
  originals (roughly 3%).
- Flood routing makes each repeater rebroadcast each report once. In a well-connected cluster that multiplies the
  airtime by up to the number of repeaters, so the real figure could be many times higher and on top of the 7% from
  beacons.
- Unbatched, one report per observation would be far worse.

Options to keep this workable:
- Longer batch window (for example one flush per beacon cycle), at the cost of location latency.
- Smaller entries (truncated counter, drop fields).
- Limit flood hops for reports, or place the base so repeaters reach it zero-hop.
- Drop or amortize per-report signatures (one signature per batch rather than per entry).

## Open questions

1. **Report transport and airtime.** Pick the batch window, hop limit/zero-hop strategy and entry format after
   measuring real overlap between repeaters. This is the largest sizing risk.
2. **Repeater signatures on reports.** Signing adds 64 bytes per batch but stops anyone with only the PSK from forging
   reports; the base would need the repeater public keys. Alternatively rely on the PSK alone.
3. **Reserved beacon bytes.** What goes in them, and are they forwarded in reports. Also test how the Android app
   displays an `ADV_TYPE_SENSOR` advert with feature fields set, a short name, and trailing bytes.
4. **Beacon key rotation** or revocation if a beacon is lost, and the operator procedure for resetting a beacon at the
   base. Deliberately left open for now.

## Suggested milestones

1. `examples/beacon`: nRF52 build that sends a signed zero-hop advert (counter, battery, marker) on a jittered timer,
   with the radio and MCU asleep between sends. Verify it appears in the Android app. Measure sleep and average
   current.
2. Beacon repeater: beacon filter and RSSI/SNR capture, logged to serial.
3. Batched report packet on the private channel; base ingests it through a companion node.
4. Base: allowlist, high-water mark, dedupe, reset, per-beacon observation table.
5. Measure real airtime with several repeaters, tune batching and hop limits.
6. Location estimation, then tune against real-world walk tests.
