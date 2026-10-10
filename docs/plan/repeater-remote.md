# Remote Repeater Management Plan

Status: **reviewed and agreed 2026-10-10. RM1 (protocol and clock) is implemented; RM2 onwards is not.** Written after reading the firmware (`examples/simple_repeater`,
`examples/companion_radio`, `src/helpers/CommonCLI.cpp`) and this repo, then amended with the firmware session's findings
(documentation commit `e5761d02`, "Firmware follow-ups"). Done before the rest of B3; it includes the ingest heartbeat from B3
and nothing else from it. See "Phases".

A small, fixed set of operations on a repeater, sent **over the mesh** through the base companion, so a repeater in the field
can be read and corrected without a phone, a serial cable or a visit. This is **not a general remote CLI**: `beaconctl`
accepts only the operations below, validates their values, and never passes free text to a repeater.

Related: [beacon-base.md](beacon-base.md) (architecture, companion link), [repeater-onboarding.md](repeater-onboarding.md)
(positions and names from adverts, `repeater locate`), [beacon-names.md](beacon-names.md) (`beacon.names`,
`beacon.name_refresh`).

## Scope

| Item | Read | Change | Repeater CLI (firmware) |
|---|---|---|---|
| Name | yes | yes | `get name`, `set name <name>` |
| Location | yes | yes | `get lat` / `get lon`, `set lat <v>` / `set lon <v>` |
| Zero-hop advert interval | yes | yes | `get advert.interval`, `set advert.interval <minutes>` |
| Flood advert interval | yes | yes | `get flood.advert.interval`, `set flood.advert.interval <hours>` |
| Beacon counters | yes | no | `beacon.stats` |
| Beacon batching window | yes | yes | `beacon.window`, `beacon.window <secs>` |
| Report channel | **yes (is it set, its hash)** | **no, never over the mesh** | `beacon.channel` |
| Name announcements | yes | yes | `beacon.names`, `beacon.names on\|off` |
| Name refresh interval | yes | yes | `beacon.name_refresh`, `beacon.name_refresh <hours>` |

Not included, on purpose: `beacon.channel <hex>` / `clear` (the channel key must not cross the air, even encrypted;
provision and rotate it on the serial console), the admin or guest password, `reboot`, `erase`, radio parameters, ACL commands
(`setperm`), raw passthrough. `beaconctl repeater locate` stays the **base-only** position override and is unchanged.

## Decisions

| # | Decision |
|---|---|
| 1 | **`beaconctl` never opens the serial port.** It writes a job to the database and `beacon-ingest`, which owns the port, runs it and writes the result back. This keeps "one SQLite file is the only interface" and needs no new IPC. Consequence: remote operations need a running `beacon-ingest`. |
| 2 | **A closed set of job kinds**, one per row in the table above, each with typed, range-checked parameters. The command text sent to the repeater is built by `beacon-ingest` from the kind and parameters; the job table never holds command text. Anyone who can write the database can only request operations from this list. |
| 3 | **The admin password is supplied when needed and is not kept.** It comes from a prompt, `--password-file`, or `$BEACON_REPEATER_PASSWORD`, never an argument (visible in `ps`). It is held in the job row only until the job is picked up, then erased. See gap 2 and decision 4. |
| 4 | **With no password given, log in with an empty password (the ACL login); with one given, use it.** A repeater keeps an admin client's key in its ACL (saved to its flash) after one password login, and an empty-password login then succeeds with that role. So the password is needed once per repeater (and again after an ACL wipe or a replaced base companion), not per command. **A login success is never taken as admin:** the success push must say admin (byte 1 = 1 and ACL permission = 3). An empty-password login from a key the repeater does not know *succeeds as a guest* (the guest password is empty by default), so a guest result with no password supplied fails the job as `needs_password`; side effect, the base is listed in that repeater's ACL as a guest, which is not saved and is evicted first. |
| 5 | **Reads and changes are one `get`/`set` pair per item**, using the firmware's item names, e.g. `beaconctl repeater remote <repeater> get beacon.window`, `... set flood.advert.interval 48`. Items with no `set` refuse it. |
| 6 | **A `set` is verified by reading the item back** and the job reports the value the repeater now holds (`--no-verify` skips it). All sets here are idempotent, so a lost reply is retried safely. **Latitude and longitude read back lossy** (about 1 m, truncated, from a float formatter), so their check passes within 2e-5 degrees, and the base records the value it *sent*, never the read-back. |
| 7 | **A confirmed change is written through to the base's own record**: name and position (the value sent, see decision 6) into `repeaters`, `beacon.window` into `repeaters.window_s` (which `beaconctl check` already uses). Last write wins still holds: a later advert with a position or name replaces it, and the repeater will be advertising the same value. |
| 8 | **Every job is kept as an audit row** (what, when, result, never the password). Pruning belongs to the B3 retention job, which is not part of this plan; until then job rows are only ever few and small. |
| 9 | **No direct mode, ever.** `beaconctl` does not talk to the companion itself, even with ingest stopped. Jobs go through ingest only. |
| 10 | **The full ingest heartbeat is built here** (the `service_status` row of the base plan), not a stand-in setting. The rest of B3 (retention, systemd units, udev rule, install script) stays out of this plan. See "Heartbeat". |
| 11 | **Confirmed as recommended:** a repeater keeps the base's key as admin after the first password login (decision 4); sets are read back (decision 6); a remote position is recorded with the new `location_source = 'remote'`; the subcommand shape is `repeater remote <repeater> get\|set <item>`; job expiry 60 s and CLI wait 30 s (amended after review; see "Review questions"). |

## What the firmware does (read from the firmware repository)

The companion-side frames are documented in the firmware repository, `docs/companion_protocol.md`, section "Remote
Administration (Login and CLI)" (commit `e5761d02` on the `beacon` branch, written from the source, **not yet run on a
device**). That section is the reference; the summary below is what this plan relies on. The firmware session also answered
the review questions from the source (2026-10-10), and those answers are folded in here.

**Repeater side (`examples/simple_repeater/MyMesh.cpp`, `src/helpers/CommonCLI.cpp`, `src/helpers/ClientACL.cpp`):**

- A CLI command is accepted only from a client that is **admin** in the repeater's ACL. A command from anyone else (guest,
  read-only, read-write) is **ignored without a reply**.
- Login (`handleLoginReq`): the admin password gives admin, the guest password gives guest; **any other password gets no
  reply**, and so does a replayed timestamp. **The guest password is empty by default, so an empty password from a key that
  is not in the ACL succeeds as a guest**, with a normal success push. A login success is therefore **not** proof of admin
  rights: byte 1 of the push is 1 for admin and byte 12 is the ACL permission (3 for admin); both must be checked. An
  empty password from a key already in the ACL succeeds with the role it already has, and skips the password and replay
  checks.
- The ACL (key, role, path, shared secret) is saved to flash about 5 s after an admin login and restored after a reboot, so
  an empty-password login works again. Guests are not saved. The per-client last timestamp is **not** saved (0 after a reboot).
- A full ACL (32 clients) does not fail a login: the non-admin client with the oldest activity is evicted, and if all are
  admins the last slot is overwritten. (An earlier version of this plan said a full table returns nothing; it does not.)
- Replay protection: a password login needs a timestamp **greater** than the client's last one; CLI commands need **greater or
  equal**, and an **equal** one is treated as a retry (not run, no reply). Older ones are ignored without a reply. The timestamp
  is the companion's clock (gap 4).
- Replies are one packet: at most 160 characters, **157 with a tag prefix**; the firmware does not truncate. The reply is a
  `TXT_MSG` of type `TXT_TYPE_CLI_DATA` (1), sent along the stored path if there is one, else flooded.
- `handleCommand` reflects an optional 3-character prefix `NN|` at the start of the reply and runs the rest (`7f|get name`
  returns `7f|> name`). It is handled before any command, so it applies to every `beacon.*` command and to the error reply
  `Err - unknown beacon command`. It is the only request/reply correlation there is.
- Limits (`CommonCLI::handleSetCmd`): `advert.interval` 0 (off) or 60-240 minutes, stored in 2-minute units (so an odd value is
  rounded down); `flood.advert.interval` 0 (off) or 3-168 hours; `name` rejects `[ ] \ : , ? *` and is stored up to 31
  bytes; `lat`/`lon` use `atof` with no range check, so **text that is not a number silently becomes 0 and the reply is
  still `OK`**; passwords are at most 15 characters (longer ones are truncated silently); the default admin password is
  `password`.
- **`get lat` / `get lon` are lossy.** They print through a float formatter with 7 decimals, truncated: `47.123456` reads back
  as `47.123455`, `151.2092955` as `151.2092895` (about 0.7 m). The stored value and the advert are full precision; only the
  read-back is degraded. A value read back with `get` must not be compared exactly or sent back with `set`.
- `beacon.*` commands exist only in `WITH_BEACON_REPORTER` builds. `beacon.window` 1-3600 s, applied at once and saved, but a
  batch already pending keeps its flush time, so the new window applies from the **next** batch; `beacon.names off` drops the
  pending name batch at once; `beacon.name_refresh` 0-8760 h takes effect at the next beacon advert heard.
  `beacon.channel` answers `> set, hash XX` or `> not set`, where the hash is the first byte of SHA-256 of the 16-byte
  secret, so the base can compute what it should be. `beacon.stats` answers
  `heard N, reported N, dropped N, send fail N, pending N, names sent N`, counters since boot (the longest `beacon.*` reply
  is 113 characters). **The firmware session will append `, up Ns` (uptime in seconds)**, so a reply may or may not carry it
  depending on the build; the base parses it as optional.
- **`set name`, `set lat`, `set lon` and the advert interval commands do not advertise.** The next scheduled advert carries
  the new name and position, no reboot needed. Setting an advert interval restarts its countdown. The commands `advert`
  (flood) and `advert.zerohop` send one after about 1.5 s; they are not in this plan's scope.
- **Gotcha, every `savePrefs`:** a local (zero-hop) advert interval under 60 minutes is set to 0. A new install has a 2-minute
  local advert interval, so the **first `set` of anything** (name, lat, lon, ...) on a fresh repeater silently turns zero-hop
  adverts off until an interval of 60-240 minutes is set.
- Serial-only commands (`set freq`, `get prv.key`, `erase`, ...) answer `Unknown command` over the mesh.

**Companion side (`examples/companion_radio/MyMesh.cpp`)**, summarised; see the firmware document for the byte layouts:

| Step | Command | Reply |
|---|---|---|
| Make the repeater a contact (needs its **full 32-byte key**) | `CMD_ADD_UPDATE_CONTACT` 9; **send at least 136 bytes** (the firmware checks 36 but reads 136); out path length `0xFF` = unknown (flood) | OK, or table full |
| Log in | `CMD_SEND_LOGIN` 26: key32 + password (at most 15 characters, empty allowed) | `RESP_CODE_SENT` 6 (flood flag, tag = first 4 key bytes, estimated round trip in ms); later `PUSH_CODE_LOGIN_SUCCESS` 0x85 (admin byte, key prefix, repeater clock, ACL permission, firmware level). A repeater never sends a failure; the companion has **no timeout of its own** |
| Send a command | `CMD_SEND_TXT_MSG` 2: type 1 (`CLI_DATA`), attempt, 4 ignored bytes, 6-byte prefix, text of **1-160 bytes** (longer returns the misleading `ERR_CODE_TABLE_FULL`) | `RESP_CODE_SENT` meaning only "transmitted"; no ack; stamped from the companion's **own clock** |
| Receive the answer | `RESP_CODE_CONTACT_MSG_RECV_V3` 16 (or 7): SNR, prefix, path length (`0xFF` = direct), type 1, timestamp, text | via `CMD_SYNC_NEXT_MESSAGE` after `PUSH_CODE_MSG_WAITING`; a command with no reply text produces no message |
| Forget a stale path | `CMD_RESET_PATH` 13: key32 | OK; the next message floods and the reply teaches the new path |
| Clock | `CMD_GET_DEVICE_TIME` 5, `CMD_SET_DEVICE_TIME` 6 | `RESP_CODE_CURR_TIME` 9; OK, or `ERR_CODE_ILLEGAL_ARG` if **earlier** (equal is accepted) |
| Flood scope (if needed) | `CMD_SET_DEFAULT_FLOOD_SCOPE` 63 (persisted), `CMD_SET_FLOOD_SCOPE_KEY` 54 | see gap 8 |

Only one login can be pending at a time, and sending another login or request clears the previous one.

The offline queue holds 16 entries; when full it drops the **oldest channel message**, never a contact message, so a reply
is never lost to a burst of reports, but a long stall could cost reports.

## Design

### Job table

```
remote_jobs (
  id, created_at, expires_at,
  repeater_prefix,              -- 8 bytes, FK to repeaters
  kind,                         -- 'name' | 'location' | 'advert.interval' | 'flood.advert.interval'
                                --   | 'beacon.stats' | 'beacon.window' | 'beacon.channel'
                                --   | 'beacon.names' | 'beacon.name_refresh'
  op,                           -- 'get' | 'set'
  params,                       -- JSON, typed per kind
  secret,                       -- the password, NULL once picked up; null when not supplied
  state,                        -- queued | running | done | failed | expired
  started_at, finished_at,
  result,                       -- JSON: the value(s) now on the repeater, the raw reply text, round trips used
  error                         -- a code (see "Errors") plus text
)
```

`beaconctl` inserts a row and polls it (`--no-wait` to return the id, `repeater remote jobs` to list). `beacon-ingest` claims
the oldest `queued` row with a single atomic `UPDATE ... WHERE state = 'queued'`, erases `secret` at once, and runs it.
**One job at a time**, in order: the radio is shared with report reception and every step costs airtime.

- A job **expires** if not started within `expires_at` (60 s, fixed), so a `set` queued while ingest was down does not fire
  hours later.
- Jobs left `running` by a crash are marked `failed (interrupted)` at startup.
- No job is queued, and `beaconctl` says so at once, when the heartbeat shows ingest down or the companion not connected.

### Executor inside the session

The session thread must keep draining reports while a command waits for an answer, so the executor is a **small state machine
driven from the existing `CompanionSession` loop**, not a blocking call. A new handler hook gives the session the next job
and takes the result; the session knows nothing about the database (the same split as reports).

```
ensure clock  ->  ensure contact  ->  [ACL login | password login]  ->  send command  ->  wait for tagged reply
                                                                      \-> timeout: reset path, flood once more, then fail
```

- **Contact.** With the companion in manual-add mode, repeaters are not stored contacts. Before the first job for a
  repeater the session reads `CMD_GET_CONTACT_BY_KEY` and, if missing, adds it (`build_contact`, type repeater, out path
  length `0xFF`), so the companion's contact table (and flash) is written once per repeater, not per job. The frame must be
  at least 136 bytes (the firmware checks 36 but reads 136); `companion.build_contact(9, ...)` already produces 148 bytes with path length `0xFF`, so it can be used as is, with a test that pins the length.
- **Login** once per repeater per connection, redone after any timeout. With a password it is a password login; without one,
  an empty-password login. Either way the push is checked: admin needs byte 1 = 1 **and** ACL permission 3. A guest result
  fails the job `needs_password` (no password was given) or `not_admin` (one was, and it was the guest password). The
  password is at most 15 characters; a longer one is refused before sending because the firmware truncates it silently.
  Only one login can be pending on the companion, which is another reason for one job at a time.
- **Tag.** Each command is sent as `NN|<command>` with a rolling two-digit tag, and the answer is accepted only if it begins
  with the same tag from the same repeater. Anything else (a late answer to an earlier command, a message from a person) is
  dropped and counted.
- **Command text** is 1-160 bytes (157 with the tag); longer is refused before sending, since the companion would answer the
  misleading `ERR_CODE_TABLE_FULL`. Every command in scope is far shorter.
- **Timeouts and retries.** The companion has no timeout of its own, so the executor does. It uses the estimated timeout from `RESP_CODE_SENT`, scaled and clamped (config `[remote]`; a job
  that has not started expires after 60 s and `beaconctl` waits 30 s by default). One retry after `CMD_RESET_PATH`. The first attempt to a repeater is a flood (path unknown); the answer teaches the
  companion the path, so later commands go direct.
- **Replies** from the offline queue are routed by message type: channel data to the report path as today, contact messages
  to the executor, everything else to `_drop` as today.

### Heartbeat

The single-row `service_status` table from the base plan, written by `beacon-ingest`. It is what lets `beaconctl` (and later
`beacon-web`) tell "ingest is down" from "nothing is happening".

| Field | Meaning |
|---|---|
| `pid`, `started_at` | the running ingest process; a changed `started_at` is a restart |
| `updated_at` | wall time of the last write; **ingest is considered down when it is older than three heartbeat intervals** |
| `connected`, `port`, `connected_since` | the companion link |
| `companion_name`, `companion_model`, `companion_firmware`, `companion_key_prefix` | what answered `APP_START` and `DEVICE_QUERY` (the key prefix is what a repeater's ACL holds, see gap 18) |
| `last_frame_at` | last frame of any kind from the companion |
| `last_report_at` | last report stored |
| `stats` | the session counters that exist today (reports, name messages, drops by reason, reconnects), as JSON |
| `clock_trusted`, `companion_clock_offset_s` | whether the base clock is trusted and how far the companion's clock was found to be from it (RM1) |
| `remote_state` | idle, or the repeater and item of the job being run |

Written when something changes (connect, disconnect, a job starting or ending, clock state) and otherwise at most every
15 seconds (SD card wear: one small row, `synchronous=NORMAL`). A disconnect or a stop writes `connected = 0` before exiting
so a clean shutdown is distinguishable from a crash. `beaconctl status` shows it (ingest up/down, link, companion, age of the
last report) and `beaconctl check` fails when ingest is down. Not part of this plan: retention, systemd supervision.

### Companion clock

Replay protection on the repeater compares the companion's clock with the last timestamp it saw from this key (held in the
repeater's RAM, so it resets when the **repeater** reboots). The companion clock **only moves forward** (`CMD_SET_DEVICE_TIME`
is refused if earlier; equal is accepted). It is never written to flash:

- **nRF52:** volatile, starts at 15 May 2024 at every boot, unless an I2C RTC chip (DS3231, RV3028, PCF8563, RX8130CE) is found.
- **ESP32-S3:** kept in RTC memory across software, watchdog and crash resets, lost on power loss (then 1 March 2026). Whether
  it survives the USB DTR reset on this board is unknown until the hardware check.

The base does not set it today. If it is not set, a password login or command after a companion reboot can be ignored as a
replay (while the repeater still remembers a later timestamp) and, per gap 4, produces only a timeout. So:

- At connect, once the base clock is trusted (`clock.assume_synced`, or `time set|confirm` was done), the session sets the
  companion clock if the companion is behind (read first, write only if behind, never writes backward).
- When the base clock is **not** trusted, no job is run and `beaconctl` says "set the clock first". A wrong time must never
  be pushed forward onto the companion, because it cannot be moved back without a reboot and would then reject every later
  command.

### Command line

```
beaconctl repeater remote <repeater> get <item> [--password-file F]
beaconctl repeater remote <repeater> set <item> <value...> [--no-verify] [--password-file F]
beaconctl repeater remote <repeater> get all
beaconctl repeater remote jobs [--all]
```

Common options: `--timeout SECS` (how long `beaconctl` waits for the job, default 30; if the job is still running then, `beaconctl` says so and gives the job id, and `repeater remote jobs` shows the result later), `--no-wait`, `--json`. Items and values:

| Item | Value | Base-side validation | Notes |
|---|---|---|---|
| `name` | text | UTF-8, no control characters, none of `[ ] \ : , ? *`, at most 31 bytes | takes effect in the next scheduled advert, not at once |
| `location` | `LAT LON` | numbers, lat -90..90, lon -180..180, at most 6 decimals (the advert resolution), not both 0 without `--allow-zero` | two firmware commands, verified together within 2e-5 degrees (the read-back is lossy); refused if `atof` could misread it (strict number parse); the base keeps the value it sent |
| `advert.interval` | minutes | `0` (off) or an even number 60-240 | zero-hop advert only reaches direct neighbours; **the first `set` of anything on a fresh repeater turns zero-hop adverts off** (the 2-minute install default is under 60), so `get all` and every set report this interval |
| `flood.advert.interval` | hours | `0` (off) or 3-168 | |
| `beacon.window` | seconds | 1-3600 | write-through to `repeaters.window_s`; `check` warns if it is not below the shortest beacon interval; a batch already pending keeps its old flush time, so the new window starts with the next batch |
| `beacon.names` | `on` / `off` | | `off` drops the pending name batch at once |
| `beacon.name_refresh` | hours | 0-8760 | 0 = first sight or change only; takes effect at the next beacon advert the repeater hears |
| `beacon.stats` | (get only) | | parsed into counters, raw text kept; counters are since boot. The reply gets `, up Ns` appended by a coming firmware change, so uptime is optional in the parser; with it, a lower uptime than the previous sample means a reboot, and the change in counters can be shown as a rate |
| `beacon.channel` | (get only) | | reported as set / not set and the hash, compared with the hash of **this base's** channel key: "matches this base" or "DIFFERENT" |

`get all` reads name, location, both advert intervals, window, names, name_refresh and the channel hash: eight or so round trips,
so it is for bring-up and audits, not a poll loop.

### Errors

A job fails with a code the CLI explains in one line, because the firmware leaves most failures silent:

| Code | Meaning shown to the operator |
|---|---|
| `no_ingest` | `beacon-ingest` has not been seen recently; start it |
| `clock_untrusted` | base clock not set; run `beaconctl time set` |
| `no_key` | the base does not have this repeater's full key; it has to be heard advertising first |
| `no_reply` | no answer to login or command after retries: wrong password, repeater out of range or flood not forwarded (gap 8), or its replay window is ahead of the companion clock (gap 4) |
| `needs_password` | the empty-password login came back as guest, so this repeater does not have the base's key as admin: supply the admin password |
| `not_admin` | a password was supplied and the repeater granted a role below admin (it was the guest password), or the key is a guest, read-only or read-write in the ACL |
| `bad_value` | refused by the base before sending (range, characters, password or text too long) |
| `unsupported` | the repeater answered "Unknown command" or "unknown beacon command": not a beacon build, or older firmware |
| `rejected` | the repeater refused the value (its own error text is shown) |
| `mismatch` | read-back after a set did not match |
| `expired`, `interrupted` | job not started in time, or ingest stopped mid-job |

## Gaps found (review)

Ordered by how much they change the design. "Resolved" says where this plan handles it.

1. **Serial port ownership. Resolved by decision 1**, at a price: `beacon-ingest` must be running and `beaconctl listen`
   (which holds the port) blocks all jobs. `beaconctl` also needs to know that ingest is alive; that is the full heartbeat
   (decision 10, "Heartbeat"), built in RM2. A direct mode is **not** planned (decision 9).
2. **The password has to travel to ingest.** It cannot go on the command line, and putting it in the database writes it to
   disk (and the WAL) for a moment. Resolved by decisions 3 and 4 (erased on pickup, needed only for the first login), plus
   the database file and its directory must be mode 0600/0700, which they are not today (gap 15). The alternative, a local
   socket between `beaconctl` and ingest, is cleaner for secrets but is the IPC the architecture avoids; keep it as the fallback
   if the on-disk window is judged too wide.
3. **The repeater is not a contact on the base companion, and its full key may be unknown.** In manual-add mode adverts are
   reported but not stored, so `CMD_SEND_LOGIN` and `CMD_SEND_TXT_MSG` would fail `NOT_FOUND`. They also need the full 32-byte
   key, and `repeaters.pubkey` is NULL for a repeater added by prefix and never heard advertising. Resolved: the executor adds
   the contact on demand (once), and `no_key` is reported for the NULL case. `repeater add` already accepts a full key, which
   also fills it.
4. **The companion clock and replay protection.** The most likely cause of "it worked yesterday": see "Companion clock".
   Failure modes to know: companion rebooted and its clock is behind the repeater's record for this key; a wrong future time
   once pushed onto the companion (the repeater then ignores correctly-timed commands until the **repeater** reboots, because
   its per-client timestamp is not saved). All of these look like `no_reply`. The error text lists them. The empty-password
   login for a key in the ACL skips the replay check, but the CLI commands after it do not.
5. **Silent failures.** Wrong password, replay, guest-only (or read-only) CLI and unsupported commands mostly produce no reply
   at all, so the plan cannot distinguish them by reply; the login push's role bytes distinguish guest from admin (decision 4),
   and an "Unknown command" reply distinguishes unsupported. Anything else is `no_reply`. This is a limitation of the firmware, not something
   the base can fix.
6. **No request/reply correlation except the `NN|` prefix.** Relying on it is the plan, but it has to be **confirmed on the
   hardware** (it is only exercised by the phone app today), and the case of a repeater firmware that does not reflect it
   needs a fallback: if no reply carries the tag, accept the single oldest untagged reply from that repeater while exactly one
   command is in flight. One in-flight command per repeater, one job at a time, makes that safe.
7. **Reply length** is 160 characters (157 with the tag), never truncated by the firmware. The longest reply in scope is
   `beacon.stats` at 113 characters (129 with the uptime). Nothing to handle beyond the command-text limit above.
8. **Path and flood policy.** The first command to a repeater is an **un-scoped flood** (a companion's default scope is
   empty). A repeater forwards it unless its wildcard region is denied (`region denyf *`) or `flood.max.unscoped` is 0; a
   repeater within radio range handles it directly either way, so only multi-hop paths are affected, and its reply flood
   mirrors the request's scope. **Check on the real mesh (hardware script, steps 12-13).** If it is needed, the base sets a
   persisted default scope on the companion with `CMD_SET_DEFAULT_FLOOD_SCOPE` (63: region name and the first 16 bytes of
   SHA-256 of `#name`), written only when different, from a new `companion.flood_region` setting; the repeaters then also
   need that region created and allowed (`region put`, `region allowf`, `region save`; new regions deny flood by default).
   **Not built unless the hardware check shows it is needed.** A stale stored path is handled by the reset-path retry.
9. **Airtime and duty cycle.** Each command is a request and a reply, a login two more, over the same shared channel as the
   reports, with flood retries. One job at a time and `get all` only on request is the mitigation. There is no web
   endpoint that can trigger it in this plan (gap 17).
10. **Blocking would stall report ingest.** Handled by the state machine, but it is the part of the existing session most
    likely to get a subtle bug (draining while waiting, a reconnect mid-job: the job must fail `interrupted`, never replay a
    `set` after reconnect without the operator seeing it).
11. **Stale queued jobs.** A `set` entered while ingest was down must not run later. Handled by `expires_at`.
12. **Base state drift after a change.** Write-through (decision 7) updates `repeaters` immediately; otherwise the base would
    show the old name or position until the next advert, which can be 47 hours away. A written-through location is recorded
    as `location_source = 'remote'` (new value, shown in `repeater list`; decision 11). Migration 1 is edited and the
    development database deleted.
13. **Which repeaters can do what.** `beacon.*` needs a beacon build and the name commands need firmware from the names work
    onward. `unsupported` is returned per item; `get all` reports each item independently instead of failing the job.
14. **Counters need a baseline.** `beacon.stats` is since boot. A rate needs two samples; with the uptime the firmware session
    will add (`, up Ns`) a lower uptime than the previous sample identifies a reboot, and without it (older builds) a drop in
    a counter does. Keep each result in the job table; `beaconctl` can show the change against the previous sample for that
    repeater. Not trended beyond that (that would belong with B4).
15. **The database holds the audit trail and a secret window.** The database and its directory should be created with
    restrictive modes (not today), `secure_delete` considered for the job table, results contain no passwords, and the job
    table needs a retention rule (B3).
16. **Concurrent `beaconctl` runs** queue behind each other and the CLI tells the operator its position and the job in front.
17. **`beacon-web` (B4) is deliberately not given this.** The job table would let the web UI queue the same jobs later; that
    needs the password handling and the operator token worked out first, and a button that transmits is worth its own
    review. Out of scope here.
18. **A replaced base companion has a new key**, so no repeater has it in its ACL, and every repeater needs the password again.
    Say so in `docs/operations.md`. Likewise the default repeater admin password is `password`: this feature cannot change
    it (`password` is not in scope); note it in the field checklist.
19. **Firmware documentation gap. Resolved:** `docs/companion_protocol.md` now has the section (commit `e5761d02`). It is
    written from the source, not observed on a device, so the hardware script is what proves it.
20. **Precision of `lat`/`lon`. Resolved:** the read-back is lossy (about 1 m); verify within 2e-5 degrees, record the value
    sent, never re-send a read-back (decisions 6, 7).
21. **Silent side effect of the first `set`.** Any `set` on a fresh repeater turns zero-hop adverts off (interval under 60
    minutes is zeroed on every save). The base learns positions from flood adverts (every 47 hours by default) and from
    write-through, so it does not depend on zero-hop adverts, but the operator should know. Every `set` result and `get all`
    show `advert.interval`; the README field checklist says to set it (60-240) if zero-hop adverts are wanted.
22. **A change is not advertised.** `set name` / `lat` / `lon` take effect in the next scheduled advert, which can be days away
    (the base is not affected, it writes through). The repeater's `advert` command would send one at once; it is outside the
    agreed scope. A later `--advert` option on `set name` / `set location` is a small extension if wanted.
23. **An empty-password probe registers the base as a guest** in a repeater that does not know the key (decision 4). Harmless,
    but it can evict another non-admin client from a full table; only the 32-entry ACL is affected and admins are not.

## Testing

Everything must run without hardware, like the rest of the repo.

- `companion.py` unit tests: command builders (login, text, reset path, set/get time), parsers (`RESP_CONTACT_MSG_RECV_V3`
  and v1, login success/fail pushes, `RESP_CODE_SENT`, current time).
- `fake_companion.py` grows: a contact table that honours add/get/reset path, `CMD_SEND_LOGIN` and `CMD_SEND_TXT_MSG`, a clock
  that refuses to go backward, and a **fake repeater CLI** (a small model of `CommonCLI` / the beacon commands: its own
  ACL, last-timestamp replay rule with the equal-timestamp retry, admin vs guest vs read-only, an empty-password login that
  is a guest for an unknown key and the existing role for a known one, a full ACL that evicts, silent failures, tag
  reflection, the `atof` quirk and the lossy `get lat` / `get lon`, the zero-hop interval zeroed on save, `beacon.stats`
  with and without uptime) behind it, with switches to drop the reply, delay it, answer in a different order, or not reflect
  the tag.
- Executor tests through the session: login by ACL, by password, wrong password (timeout), guest, replay/behind-clock,
  retry with path reset, reply lost then retried, a report arriving mid-job still ingested, reconnect mid-job, job expiry,
  stale `running` rows at startup.
- Store tests: job lifecycle, atomic claim, `secret` erased at claim, expiry, write-through (`name`, location with
  source, `window_s`), audit rows without secrets.
- Validator tests: every limit in the table, including the `atof` traps (`12.3abc`, `nan`, `1e400`, empty), names with the
  rejected characters, odd `advert.interval`.
- CLI tests: parsing, password sources (prompt not used when file/env given, never from argv), exit codes and messages for each
  error code, `--json`.
- **Hardware check (RM4):** the script below, run first with raw frames (no base software) and then through `beaconctl`
  against the same repeater.

## Phases

Each ends with something runnable.

**RM1. Protocol and clock.** Companion builders and parsers; `fake_companion` contacts, login, text, clock; session sets the
companion clock when the base clock is trusted; message routing for contact messages. Done when a scripted login and a tagged
command round trip through the fake repeater in tests.

*Done.* `companion.py` has the builders and parsers (`add_update_contact`, `send_login`, `send_cli`, `reset_path`, device time,
`parse_sent`, `parse_login` with the strict `admin` test, `parse_contact_message`, tag helpers). `fake_repeater.py` models the
repeater (ACL, replay rules, guest-by-default empty password, silent failures, lossy `get lat`, tag reflection, the beacon
commands) and `fake_companion.py` gained contacts with routes, login, CLI messages, a forward-only clock and reboot behaviour.
The session moves the companion's clock forward once the base clock is trusted (`companion.sync_clock`, `Handler.clock_trusted`),
and hands login pushes and contact messages to the handler (`on_login`, `on_contact_message`). 63 new tests; the full suite is
400. The executor itself, and the contact-on-demand and login/command state machine, are RM2. Not yet seen on hardware.

**RM2. Heartbeat, jobs and executor.** The `service_status` heartbeat and its use in `status` and `check` (usable and tested on
its own first), then the `remote_jobs` table (edit migration 1, delete the dev database), the executor state machine in the
session, the handler hook in `service.py`, expiry and recovery. Done when the heartbeat shows ingest up/down and the link
state, and jobs submitted from a test run to completion with reports flowing at the same time.

**RM3. Commands.** `beaconctl repeater remote ...`, validators, password sources, write-through, `check` additions
(channel hash mismatch is a failing finding when a result is on record), README section, `docs/operations.md`, CLAUDE.md.
Done when every item reads and sets against the fake repeater from the command line.

**RM4. Hardware.** The check above on one repeater, then the rest. Findings go back into this plan.

The heartbeat is built here and counts as done for B3; update the B3 text in the base plan and the README when RM2 lands.

## Review questions: answers

All eight were answered on 2026-10-10 and are recorded as decisions 9-11, except where noted.

| # | Question | Answer |
|---|---|---|
| 1 | Subcommand shape | `repeater remote <repeater> get\|set <item>` |
| 2 | Password-less ACL login after the first password login | Accepted (decision 4) |
| 3 | Verify after every set | Yes (decision 6) |
| 4 | `location_source = 'remote'` | Yes |
| 5 | Heartbeat | The real heartbeat, built in RM2 as the only piece of B3 in this plan |
| 6 | Direct mode | Never (decision 9) |
| 7 | Job expiry, CLI wait | Amended: expiry 60 s, `beaconctl` wait 30 s. A job with a login and one retry can outlast the wait, so the CLI reports "still running" with the job id rather than failing; revisit after the hardware check (RM4) shows real round-trip times |
| 8 | Firmware follow-ups | Requested now, see below |

## Firmware follow-ups

Nothing is changed in the firmware repository from here. The request was handed to the firmware session on 2026-10-10.

| # | Request | Result |
|---|---|---|
| 1 | Document the host-visible login and CLI path | Done: "Remote Administration (Login and CLI)" in the firmware `docs/companion_protocol.md`, commit `e5761d02` on `beacon` (pushed). From the source, not run on a device. |
| 2 | Answer the plan's questions from the source | Done, folded into "What the firmware does" and the gaps: ACL persistence and eviction, the tag prefix, `get lat` precision, companion clocks, flood scope, adverts after `set`, `beacon.*` timing. |
| 3 | List what needs a real device | Done: the script below. |
| 4 | Uptime in the `beacon.stats` reply | **Approved; the firmware session will implement it** (`, up Ns` appended, reply grows from 113 to 129 characters). The base treats it as optional so older builds still parse. Re-check the reply format against that commit when it lands (before RM3). |

Source findings that changed this plan: a login success is not proof of admin (decision 4); a full ACL evicts instead of
failing (corrected); a read-back of `lat`/`lon` is lossy (decisions 6, 7); the first `set` on a fresh repeater turns zero-hop
adverts off (gap 21); the contact frame must be 136 bytes; text over 160 bytes returns a misleading error; flood scope fails
only when intermediate repeaters deny un-scoped floods (gap 8).

## Hardware script (RM4)

Provided by the firmware session; **not yet run.** `R` is a test repeater with its serial console, `C` the companion driven
from the host, `K` R's 32-byte key, `K6` its first 6 bytes, `ADMIN` the build's admin password. Frames are hex after the
`<` and 2-byte length header. Run it first with raw frames, then repeat the relevant steps with `beaconctl`.

1. R: `get public.key`, `get guest.password`, `clock` -> the key; empty; R's time.
2. C: `16 03`, then `05`, then `09` plus a time -> device info; the current time (nRF52 near 15 May 2024, S3 at least
   1 March 2026); OK.
3. C: add contact = `09` + K + `02 00 FF` + 64 x `00` + 32-byte name + `00 00 00 00` (136 bytes) -> OK.
4. C: `1A` + K + `ADMIN` -> `06 01 <tag> <timeout>`, then `85 01 ... 03 02` within the timeout (byte 1 = 1, byte 12 = 3).
5. C: `02 01 00 00000000` + K6 + `7f|get name`, then `0A` after the `83` push -> a `10...` frame, txt_type `01`, text `7f|> <name>`.
6. Repeat step 5 with `a1|beacon.stats`, `a2|beacon.window`, `a3|beacon.bogus` -> `a1|heard ...`, `a2|> 60 secs`,
   `a3|Err - unknown beacon command`.
7. C: log in with `wrong` -> `06 ...` and then nothing for 3x the timeout (no `85`, no `86`).
8. Wait over 10 s after a step-4 login. R: `reboot`. C: login with an empty password -> `85 01 ... 03`; `b1|get name` is
   answered (ACL restored, timestamp reset).
9. R: `setperm <K> 0`, then an admin login and R `reboot` within 2 s. After boot, C: empty-password login -> `85 00 ... 00`
   (guest), and `c1|get name` gets no reply.
10. R: `set guest.password gp`, `setperm <K> 0`. C: login with `gp` -> `85 00 ... 00`; `d1|get name` -> no reply. Log in as
    admin again -> role 3, replies work.
11. Run C on an nRF52 for at least 150 s, sending a few commands. Reboot C and log in as admin at about 10 s of uptime ->
    `06` but no `85`. Retry every 30 s -> it succeeds once C's uptime passes the earlier one. Then reboot R -> login works
    at once. (This is the companion-clock failure of gap 4; repeat with the base setting the companion clock first.)
12. Two or more hops (C cannot hear R, relays R1 and R2): C: `0D` + K, login as admin -> `06 01 ...` (flooded) then `85`;
    `e1|get name` -> reply whose path byte has low 6 bits of at least 2; another command -> `06 00 ...` (stored path used).
13. R1: `region denyf *`, `region save`. C: `0D` + K, login -> no `85`. C: `3F` + `test` padded to 31 + the first 16 bytes
    of SHA-256(`#test`) -> OK. R1: `region put test`, `region allowf test`, `region save`. C: `0D` + K, login -> `85` arrives.
    (Settles gap 8.)
14. C: `f1|set name rpt-x` -> `f1|OK`, and no advert from R for 60 s. Then `f2|advert.zerohop` ->
    `f2|OK - zerohop advert sent`, and an advert with the new name within about 3 s.
15. C: `f3|set lat 47.123456`, `f4|get lat` -> `f4|> 47.123455`, and the next advert decodes to 47.123456. `f5|set lat abc`
    -> `f5|OK`; `get lat` -> `> 0.0`.
16. On an erased R: `get advert.interval` -> `> 2`; `set name z`; `get advert.interval` -> `> 0`.
17. With the window at 60, trigger a beacon at t0, then within 10 s send `g1|beacon.window 5` -> `g1|OK`. The pending report
    arrives about 60 s after t0; the next beacon's report about 5 s after it.
18. (After the firmware change) `h1|beacon.stats` -> the reply ends with `, up <N>s`; reboot R -> a smaller N.
