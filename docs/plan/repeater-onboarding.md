# Repeater Onboarding Plan

Status: **R1 and R2 implemented** and tested without hardware (fake companion); **R3, the hardware check, is outstanding.** Done
before B2 is called complete and before B3.

Repeater locations come from the repeaters' own adverts instead of being typed in at the base, and `repeater add` gets an
`--all` form like `beacon add --all`.

Related: [beacon-base.md](beacon-base.md) (repeater table, decision 8), [beacon-names.md](beacon-names.md) (beacons are
addressed by prefix, names are display only).

## Decisions (review)

| # | Decision |
|---|---|
| 1 | **Every repeater advert with a valid position is recorded.** The mesh is independent, so the repeaters on it are ours; no filtering by interest. An advert with no usable position (unset, 0,0, out of range) from a repeater that is not trusted yet is ignored. |
| 2 | **Last write wins.** Corrections are made on the repeater, not at the base. A hand-set position (`repeater add ... <lat> <lon>`, `repeater locate`) is for testing, for setting a position before the repeater has advertised, or when it cannot be set on the repeater; the next advert with a position replaces it. There is no lock and no `--advert` switch. An advert without a position never erases a known one. |
| 3 | **The name comes from the advert too, last write wins.** An advert from a trusted repeater sets its name even when it carries no position. `--name` at `repeater add` is only the initial value. |
| 4 | **The base companion runs in manual-add mode by default** (`companion.manual_add_contacts`, opt-out). It is written to the companion only when it differs, since the companion stores it in flash. |
| 5 | **No move warnings.** Unlocated repeaters **fail `beaconctl check`**. The development database is deleted again (migration 1 edited). |
| 6 | **Repeaters are checked before beacons in the pipeline.** Found while building `add --all`: with beacons checked first, a report from an unknown repeater of an unlisted beacon was stored as `unknown_beacon`, so the repeater never appeared in the unknown list until a beacon had been added. Now an unknown repeater is always `unknown_repeater`, repeaters are onboarded first, and beacons (and their announced names) are only taken from trusted repeaters. |

## Goals

- In the field a repeater is placed, and its position is set over the mesh from the Android app and a companion (not the base),
  using the phone's location. The repeater then advertises that position. The base learns it from the advert and keeps the
  repeater's `lat`/`lon` current, **whether the advert arrives before or after the repeater is added as trusted**.
- `repeater add --all` adds every unknown repeater (one that has sent reports but is not in the table).
- A repeater with no known position is added at `0, 0`, still works as a reporter, and is **excluded from positioning** once
  the estimator exists.
- No repeater GPS module and no firmware change: this uses what the repeaters already advertise.

Non-goals: setting a repeater's position from the base, announcing positions in reports, GPS.

## What the firmware already does (checked in the firmware repository)

- A repeater's advert (`MyMesh::createSelfAdvert`, `CommonCLI::buildAdvertData`) carries its **name and lat/lon** according to
  `advert_loc_policy`, for the type `ADV_TYPE_REPEATER`, and is signed with its key. The report `repeater` field is the first 8
  bytes of that same key, so an advert's key maps straight to the prefix on reports.
- Adverts go out as a **flood** advert every `flood.advert.interval` hours (**default 47**) and, on new installs, a zero-hop
  advert every 2 minutes (zero-hop is heard only by direct neighbours). The CLI `advert` command sends a flood advert at once.
  A forwarding repeater drops adverts beyond `flood.max.advert` hops (**default 8**).
- Builds default to `ADVERT_LAT=0.0`, `ADVERT_LON=0.0`, so a repeater nobody has located advertises 0,0.
- A companion tells its host about adverts in three ways (`examples/companion_radio/MyMesh.cpp`, `BaseChatMesh::onAdvertRecv`):
  - `PUSH_CODE_NEW_ADVERT` (0x8A): a **full contact** (32-byte key, type, name, advert timestamp, **lat/lon**) when the node is
    not one of its stored contacts, **and for every advert when it is not auto-adding that type**.
  - `PUSH_CODE_ADVERT` (0x80): just the 32-byte key, when the node is already a stored contact. The new position is **not** in
    it; the host has to ask (`CMD_GET_CONTACT_BY_KEY`, which needs the full key, there is no prefix lookup).
  - `CMD_GET_CONTACTS`: the stored contact list, with positions as of each node's last advert.
- The companion verifies the advert signature and ignores a replayed (not newer) timestamp for stored contacts.

## Design

### Learning positions (ingest)

`CompanionSession` handles the three paths above, for `ADV_TYPE_REPEATER` only: read the contact list once per connection,
process `NEW_ADVERT` pushes, and on a bare `ADVERT` push look the contact up by the key it carries (a node already known not
to be a repeater is skipped). Handler hook `on_repeater_advert(HeardRepeater)`: `public_key`, `name`, `lat`, `lon`,
`advert_timestamp`, `heard_at` (None when it came from the contact list rather than a live advert).

Option `companion.manual_add_contacts` (**default on**, decision 4): put the base companion in manual-add mode with nothing
auto-added, so **every** advert arrives as a full `NEW_ADVERT` and the companion's contact table never fills. It is sent with
`CMD_SET_OTHER_PARAMS` carrying only the first parameter (so the companion's telemetry and location settings are untouched)
and only when `APP_START`'s reply shows it is not already set. The bare-advert lookup stays as the fallback for contacts the
companion stored before it was switched.

### Data model (migration 1 is edited again, the dev database is deleted)

- New `repeater_adverts`: `prefix` (primary key), `pubkey` (32), `name`, `lat`, `lon`, `advert_timestamp`, `first_seen`,
  `last_heard`. Only adverts with a valid, non-0,0 position are kept (decision 1). Independent of the repeater table, so a
  position can arrive before the repeater is trusted.
- `repeaters`: `lat` and `lon` stay `NOT NULL`, default `0, 0`. New `location_source` (`none`, `advert`, `manual`) and
  `location_updated_at`. `pubkey` is filled from the advert when known (useful later for report signatures).
- A repeater is **located** when `lat` and `lon` are not both 0. `Store.located_repeaters()` is the one place that says so; the
  estimator will use it.

### Rules

- An advert with a valid, non-0,0 position updates `repeater_adverts`, and also `repeaters.lat/lon` for that prefix, whatever
  wrote them before: the latest write wins (decision 2). An advert without a position never erases a known one. An advert
  from a trusted repeater also sets its name, with or without a position (decision 3).
- `repeater add` takes the position and name from the advert when none is given: `repeater add <prefix>` uses the heard
  ones, or 0,0 with source `none`. Giving `<lat> <lon>` sets source `manual`, until the next advert.
- Unlocated repeaters are processed normally: their reports are accepted and stored, they are only left out of positioning.

### CLI

| Command | Change |
|---|---|
| `repeater add <key-or-prefix> [<lat> <lon>] [--name N] [--window S]` | `lat`/`lon` optional. |
| `repeater add --all [--hours H]` | Adds every repeater that reported in the last `H` hours (default 24) but is not in the table, each with its advert position if known, else 0,0. Only repeaters that have **reported** are candidates, not every repeater whose advert was heard, so other people's repeaters in range are never added. |
| `repeater locate <ref> <lat> <lon>` | Set the position by hand (testing, before the repeater has advertised, or when it cannot be set on the repeater). The next advert with a position replaces it. |
| `repeater list` | `LAT`/`LON` show `-` when unlocated; new `SOURCE` and `UPDATED` columns. |
| `status` | The "repeaters heard but not in the table" lines show the advertised name and position, and the `add` command. |
| `check` | **Fails** (exit 1) for each unlocated repeater: "no location, excluded from positioning", with how to fix it. |

### Operating notes (also go in the README)

- After setting a repeater's position from the app, send a flood advert (`advert` in its CLI) or the base waits up to 47 hours
  (`flood.advert.interval`) for the next one. Lowering `flood.advert.interval` on the repeaters shortens that.
- The advert must reach the base companion: within 8 hops of it and in radio range. A repeater whose advert never arrives is
  located by hand with `repeater locate`.
- The position in an advert is what the repeater was told; the base does not check it against anything.

## Testing

Table-driven store tests for the rules above (advert before and after add, last write wins in both directions, no position
never erases, 0,0 and out-of-range ignored); session tests against the fake companion (contact list at connect,
`NEW_ADVERT`, bare `ADVERT` lookup by key, non-repeater adverts ignored, manual-add mode); CLI tests for `add` with and without
coordinates, `add --all`, `locate`, `list`, `status`, `check`; simulator emits repeater adverts, some without a position;
hardware: place a repeater, set its position from the app, send an advert, see it located in `repeater list`.

## Phases

**R1. Ingest and store.** Companion advert handling, `repeater_adverts`, location rules, `Store.located_repeaters()`, simulator.
**R2. CLI.** `repeater add` with optional coordinates, `--all`, `locate`, `list`, `status`, `check`, docs.
**R3. Hardware check.** Real repeaters located from the Android app, adverts heard by the base companion.

## Open questions

None. The unlocated rule, the move warning and the contact mode are settled in the decisions above.
