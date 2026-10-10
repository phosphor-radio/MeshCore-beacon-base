# Beacon Names Plan

Status: **reviewed, ready to implement** (decisions below). Nothing implemented yet. To be done before B3.

Repeaters forward each beacon's advertised name to the base, so beacons are named once, on the beacon, and the base shows
that name. The base then keys everything on the 8-byte prefix and stops asking the operator for names.

Related: [beacon-base.md](beacon-base.md) (decision 8: beacons are identified by the 8-byte prefix),
[beacon-project.md](beacon-project.md) (open question 3, reserved beacon bytes).

## Decisions (2026-10-09 review)

| # | Decision |
|---|---|
| 1 | **A beacon's default name is derived from its key**: `beacon-` + the first 3 key bytes in hex (`beacon-f5b165`, 13 bytes, within the 18-byte limit). Names are therefore unique out of the box, explicit naming is optional, and the default follows the key if it ever changes. An explicit name overrides it. |
| 2 | **Duplicate announcements are fine.** Every repeater that hears a beacon announces it; no suppression. |
| 3 | **A refresh is needed; the default is every 4 hours** (`beacon.name_refresh 4`). |
| 4 | **Last-writer-wins.** No counter in name entries, no replay protection beyond that. |
| 5 | **The development database is deleted.** Migration 1 is edited, no migration 2. |
| 6 | **Names are a separate message type** (`0xFFBF`), not a change to report v1. |
| 7 | **Names are stored for any prefix** announced by a known repeater, allowlisted or not, pruned by age. |
| 8 | **Latest name only**, plus a log line when it changes. No rename history. |
| 9 | **Prefix abbreviation:** any unique leading hex string of at least 6 digits is accepted. |
| 10 | **`--notes` stays** as the only free-text field on an allowlist entry. |
| 11 | **Repeater names stay operator-assigned and become optional.** The base never hears a repeater's advert, and repeater names matter even less than beacon names. See "CLI". |
| 12 | **The project stays in the dev data-type range** (`0xFF00`-`0xFFFF`); `0xFFBE` and `0xFFBF` are not registered. |
| 13 | **Existing beacons need a manual `set name auto`.** There is only one test beacon, so that is fine. |

## Goals

- A beacon's name is set where it already is today: `set name` on the beacon's serial CLI. It travels in the beacon's signed
  advert, so a repeater that hears the beacon knows it.
- The repeater tells the base `(prefix, name)`. The base stores it keyed by prefix and shows it wherever a beacon is shown,
  including beacons that are **not yet on the allowlist**, so `status` can say "this unlisted beacon is `Roof`" before you
  add it.
- No name argument on any base command, no rename command. Duplicate names are fine, the prefix is the identity.
- Cheap on air: names are announced rarely (see "Airtime").

Non-goals: announcing repeater names (they stay operator-assigned and optional, decision 11), operator aliases for beacons, authenticating
names beyond what reports already have.

## Current state this builds on

- The beacon's advert `app_data` is `[flags][feat1 battery][feat2 marker][name\0][reserved]`; the name is optional and at most
  `BEACON_MAX_NAME_LEN` = 18 bytes (`src/helpers/BeaconAdvert.h`). The advert is signed, so the repeater has verified the name
  along with the key.
- The repeater parses the advert in `MyMesh::onAdvertRecv` (`examples/simple_repeater`), where the name is already in hand.
  It batches `(prefix, counter, rssi, snr, battery)` observations and sends them as `GRP_DATA` `0xFFBE`. Report entries are
  fixed 16 bytes with no room for a name. The repeater transmits on the report channel and relays other nodes' packets on it,
  but cannot decrypt anything on it: it holds the channel secret only to encrypt what it sends, and does not override
  `Mesh::searchChannelsByHash` or `onGroupDataRecv`, so received group packets are never decrypted or delivered to any
  handler (`src/Mesh.cpp`).
- The base identifies beacons by prefix only; its allowlist currently has an operator-chosen, unique `name`.
- The beacon keeps its name in its saved prefs, initialised from the `BEACON_NAME` build flag (`"beacon-001"` in
  `variants/xiao_nrf52/platformio.ini`), so every beacon flashed from that env starts with the same name. Decision 1 changes
  this. The key is generated on first boot and kept in the identity store; there is no command to change it, a new key means
  the identity store was erased.

## Design

### Separate message, not a bigger report

A second `GRP_DATA` message type, `data_type` **`0xFFBF`** (dev range, same private channel):

- Report format v1 and its golden vectors stay untouched.
- A base that does not know `0xFFBF` already ignores it (`other_data_type`), and a repeater that does not send it just means
  names are missing. A mixed fleet works; the base falls back to showing the prefix.
- Names change rarely while observations are constant, so they should not ride in every report.

Payload, little-endian, variable-length entries:

```
header (10 bytes):  [version:1 = 1][repeater key prefix:8][entry count:1]
entry:              [beacon key prefix:8][name length:1][name: UTF-8, no NUL]
```

Example (repeater key `00..1f`, two entries), 42 bytes:

```
01 0001020304050607 02
   a0a1a2a3a4a5a6a7 0a "beacon-001"
   1122334455667788 04 "Roof"
= 01000102030405060702a0a1a2a3a4a5a6a70a626561636f6e2d303031112233445566778804526f6f66
```

An entry is 9 bytes plus the name, so at most 5 entries with 18-byte names and 7 with the 13-byte derived default names fit
in the 165 data bytes. Decoder rules mirror the report decoder: unknown version, short header, or an entry that runs past the end
means the whole message is dropped and logged; trailing bytes are ignored; a zero-length name entry is skipped.

### Repeater behavior (firmware repo, `WITH_BEACON_REPORTER`)

On each verified beacon advert that carries a name:

1. Look the beacon up in a small cache (about 64 entries of `{prefix:8, name hash:4, last announced:4}`, roughly 1 KB, least
   recently used eviction).
2. Announce when the beacon is **not in the cache** (first heard since boot), its **name hash changed**, or the **last
   announcement is older than the refresh interval**. Add `(prefix, name)` to a name batch and update the cache entry.
3. The name batch flushes when full or `beacon.window` seconds after its first entry, like the report batch, using the same
   flood send and retry on allocation failure. Reports keep priority.

A beacon with no name is never announced. Because the name is read from the advert that triggers the announcement, the
cache stores only a hash, not the name.

New CLI: `beacon.names on|off` (default on), `beacon.name_refresh <hours>` (default **4**, 0 = only on first sight or
change), both saved with the other beacon prefs; `beacon.stats` gains a names-sent counter.

### Beacon behavior (firmware repo, `examples/beacon`)

Decision 1, the default name follows the key:

- The saved name may be **empty, meaning automatic**. The advert then carries `beacon-` + hex of the first 3 bytes of the
  beacon's own public key, computed when the advert is built, so it can never go stale if the key changes.
- `set name <name>` stores an explicit name as today. `set name auto` (or `set name` with no argument) clears it back to
  automatic. `get name` prints the name in use and whether it is automatic or explicit.
- The `BEACON_NAME` build flag stops defaulting to a fixed string: unset means automatic, and the `Xiao_nrf52_beacon` env
  drops `-D BEACON_NAME='"beacon-001"'`. A build can still set the flag to force an explicit default.
- Beacons already flashed keep the explicit `beacon-001` in their saved prefs. Run `set name auto` on each, or reflash with a
  prefs reset.
- The derived default is the same first 6 hex digits the base uses to abbreviate a prefix, so a name and a copied prefix
  visibly match.

### Base behavior

- New data type handled in the ingest session next to reports: decode, then upsert `(prefix, name)`.
- Accept names only from repeaters in the repeater table, as for reports (a stranger cannot fill the table). Unknown or
  disabled repeaters are counted and ignored.
- Any announced prefix gets a name row, allowlisted or not. Latest announcement wins; a change is logged
  (`beacon f5b165 renamed 'a' -> 'b'`).
- Names are untrusted display text. Before storing: valid UTF-8 (replace otherwise), control characters and escape sequences
  stripped (they would otherwise reach the terminal), length capped at 32 bytes, empty after cleaning means ignore.
- A name never affects the high-water mark, dedupe or any pipeline decision.
- Last writer wins (decision 4): the newest announcement received replaces the stored name.

### Data model

Edit migration 1 (pre-deployment, decision 5; delete the dev database `beacon.db` and its `-wal`/`-shm` files):

- `beacons`: **drop `name` and its UNIQUE constraint.** Keep `prefix` (primary key), `enabled`, `hwm`..., `notes`.
- New `beacon_names`: `prefix` (primary key), `name`, `first_seen`, `updated_at`, `repeater_prefix` (who announced it last).
  Independent of the allowlist, so names exist before a beacon is added and survive its removal.
- `repeaters`: `name` becomes nullable and loses its UNIQUE constraint (decision 11); `prefix` stays the primary key.
- Display name = announced name, else the first 12 hex digits of the prefix.
- Retention (B3): prune names not updated for 90 days that are not on the allowlist.

### CLI

Everything that took a beacon name takes the **prefix**; commands stay copy-and-paste friendly.

| Before | After |
|---|---|
| `beacon add <name> <prefix>` | `beacon add <prefix> [--notes ...]` (a 64-character key is still accepted and reduced) |
| `beacon add --all [--name-prefix P]` | `beacon add --all` (no naming, `--name-prefix` removed) |
| `beacon status\|reset\|remove\|enable\|disable <name>` | `... <prefix>` or `--all` |

- A prefix may be abbreviated to any **unique** leading hex string of at least 6 digits among the beacons concerned (like git
  abbreviations); ambiguous or unknown input is an error naming the matches.
- Output shows both: tables have `NAME` (announced name or `-`) and `PREFIX` columns, sorted by state, then name, then prefix.
  Logs and one-line messages use `name (f5b165)` so duplicate names stay distinguishable.
- `status`: the "heard but not on the allowlist" lines show the announced name and `add: beaconctl beacon add <prefix>`.
- `rejects`, `beacon list`, `beacon status`, ingest logs and `simulate` use the same display rule.
- Repeaters (decision 11): the name becomes optional, shown as `-` or the prefix when absent, and not required to be unique.
  `repeater add <key-or-prefix> <lat> <lon> [--name N] [--window S]`, and the other `repeater` commands take the prefix
  (same abbreviation rule) or the name when it is unambiguous. Location is still required.

## Airtime

A name message is about 145 bytes at most, roughly 1 s on air at SF8/BW62.5/CR4-6, like a full report. Per repeater, per
announcement round, `ceil(beacons heard / 5)` messages (7 per message with the derived names).
With about 3 beacons heard per repeater and 10 repeaters, a round is about 10 messages, around 10 s of original airtime
before flood rebroadcasts. At the default 4-hour refresh that is 6 rounds, about 60 s a day, against about 2300 s a day of
original report airtime (about 8 s every 5 minutes): roughly **2.5% on top of the reports**, and the flood multiplier applies
to both alike. A 1-hour refresh would be about 10%, which is why the interval is configurable per repeater; set it to 0 to
turn the refresh off. First sights after a repeater boots and renames add a few messages.

The refresh also bounds how stale a name can be (4 hours after a beacon is renamed, unless the rename itself triggers an
announcement, which it does as soon as a repeater hears the next advert) and how long a base that missed an announcement
shows prefixes.

Repeaters cannot read each other's announcements (they relay channel packets but cannot decrypt them), so every repeater
that hears a beacon announces it; there is no duplicate suppression (decision 2). For the same reason the base cannot ask
repeaters for names over the channel, which is why they announce on a timer.

## Security

- The name is signed by the beacon, verified by the repeater, and trusted by the base on the repeater's word, like the counter.
  Anyone holding the channel key can forge names, and a captured name message can be replayed. The result is a wrong
  display name until the next real announcement, never a different beacon identity or a changed high-water mark.
- Names reach the terminal and, later, the web UI: sanitise on input and escape on output.
- A beacon's name is public in its advert, like its key, so no new exposure.

## Testing

- **Firmware repo:** host test for the name encode/decode with golden vectors written to `beacon_names_v1.json`, as for
  reports: valid cases (one entry, several, longest name, UTF-8), and decode-only cases (empty, short header, wrong version,
  entry running past the end, zero-length name, trailing bytes). The repeater cache logic (first sight, hash change, refresh
  due, eviction) as a pure host-testable class.
- **Base:** decoder against the fixture; sanitiser table (control characters, ANSI escapes, invalid UTF-8, over-long, empty);
  upsert and rename; unknown/disabled repeater ignored; names for unlisted prefixes; ingest with the fake companion
  (`simulate` sends name messages); prefix abbreviation (unique, ambiguous, too short, unknown); every CLI command on
  prefixes; output with duplicate and missing names.
- **Hardware:** repeater with new firmware, rename a beacon on its serial CLI, see the new name in `status` after the next
  announcement.

## Phases

**N0. Firmware repo (separate session, base repo cannot edit it).**
`BeaconNames.h` (wire format and cache), repeater integration and CLI (refresh default 4 hours), golden-vector test, beacon
default name derived from the key with `set name auto` (and a host test for the derivation), `docs` note. Done when the
vectors exist, a repeater announces names on a bench test, and a freshly flashed beacon advertises `beacon-<6 hex>`.

**N1. Base: wire, store, ingest.**
`wire.py` names decoder against the fixture, sanitiser, `beacon_names` table, drop `beacons.name`, ingest handler,
`simulate` support, logs. Done when `simulate` shows names in `status` for unlisted beacons.

**N2. Base: CLI on prefixes.**
Prefix-only arguments with abbreviation (beacons; repeaters take an optional name), display rule everywhere, removal of `--name-prefix` and name uniqueness code and
tests, README and CLAUDE.md. Done when no command asks for a name and the full suite passes.

**N3. Hardware check.**
Real repeater and beacons: names appear, rename propagates, nothing breaks with an old repeater.

N1 can start from a hand-derived fixture (the example above) before N0 lands, then switch to the firmware's vectors.

## Open questions

None. Repeater names are optional (decision 11, confirmed) and that work is part of N2.
