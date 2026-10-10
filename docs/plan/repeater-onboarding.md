# Repeater and Beacon Onboarding Plan

Status: **complete.** R1 and R2 implemented, and **R3, the hardware check, passed** on real repeaters and beacons. Done before
B3.

How repeaters and beacons get onto the base's trusted lists with as little typing as possible:

- A repeater's position and name come from **its own adverts**, heard by the base companion, instead of being entered at the
  base.
- Everything seen on the channel is **listed at once**, trusted or not, so it can be added with `add --all`.
- Two **auto-add** modes let the mesh be discovered as it is set up and then locked.

Related: [beacon-base.md](beacon-base.md) (the pipeline and tables), [beacon-names.md](beacon-names.md) (beacons are
addressed by prefix, names are display only).

## Decisions

| # | Decision |
|---|---|
| 1 | **Every repeater advert is recorded**, with or without a position (a position is stored only when valid: not unset, not 0,0, in range). The mesh is independent, so the repeaters on it are ours; no filtering by interest. A position can arrive before the repeater is trusted. |
| 2 | **Last write wins.** Corrections are made on the repeater, not at the base. A hand-set position (`repeater add ... <lat> <lon>`, `repeater locate`) is for testing, for setting a position before the repeater has advertised, or when it cannot be set on the repeater; the next advert with a position replaces it. There is no lock and no switch back. An advert without a position never erases a known one. |
| 3 | **The name comes from the advert too, last write wins.** An advert from a trusted repeater sets its name even when it carries no position. `--name` at `repeater add` is only the initial value. |
| 4 | **The base companion runs in manual-add mode by default** (`companion.manual_add_contacts`, opt-out), written to the companion only when it differs since the companion stores it in flash. |
| 5 | **No move warnings.** Unlocated repeaters (0,0) are processed normally and **fail `beaconctl check`**. The development database is deleted whenever migration 1 is edited. |
| 6 | **Repeaters are checked before beacons in the pipeline.** An unknown repeater is always `unknown_repeater`, so repeaters are onboarded first. Found while building `add --all`: with beacons checked first, a report from an unknown repeater about an unlisted beacon was stored as `unknown_beacon`, and the repeater never appeared in the unknown list. |
| 7 | **Everything seen is listed, trusted or not.** `status` and `repeater add --all` cover repeaters that **reported or advertised**, including ones that only advertised and have no position. |
| 8 | **A first report is visible at once.** Beacons reported by repeaters that are not trusted yet are listed (marked "only via repeaters that are not trusted yet") and can be added immediately, instead of waiting for the next beacon cycle after the repeater is trusted. Their counters are not touched until a trusted repeater reports them. `beacon add --all` includes them. |
| 9 | **Beacon names are taken from any repeater**, trusted or not, so an unlisted beacon is recognisable before it is added. Name announcements are encrypted with the channel key, so only a holder of it can send one. (This changes the names plan, which trusted known repeaters only.) |
| 10 | **Two auto-add modes, off by default**, switched with `beaconctl autoadd {beacons,repeaters,all} {on,off}` and stored in the database, so a running ingest uses a change at once. **Repeaters:** an unknown repeater is trusted by a report or an advert (position optional), so this also trusts ordinary mesh repeaters that never report. **Beacons:** an unknown beacon is trusted by a report from a *trusted, enabled* repeater, and that report becomes its baseline; a repeater that is not trusted can never get a beacon trusted. Discover the mesh as it is set up, then lock it with `autoadd all off`. `status` warns and `check` fails while a mode is on. |

## Goals

- In the field a repeater is placed, and its position is set over the mesh from the Android app and a companion (not the base),
  using the phone's location. The repeater then advertises that position. The base learns it from the advert and keeps the
  repeater's `lat`/`lon` current, **whether the advert arrives before or after the repeater is added as trusted**.
- Repeaters and beacons that have been seen are listed without waiting for a second sighting, and `repeater add --all` and
  `beacon add --all` trust the lot.
- A mesh being set up can discover itself (auto-add) and then be locked.
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

### Learning positions and names (ingest)

`CompanionSession` handles the three paths above, for `ADV_TYPE_REPEATER` only: read the contact list once per connection,
process `NEW_ADVERT` pushes, and on a bare `ADVERT` push look the contact up by the key it carries (a node already known not
to be a repeater is skipped). Handler hook `on_repeater_advert(HeardRepeater)`: `public_key`, `name`, `lat`, `lon`,
`advert_timestamp`, `heard_at` (None when it came from the contact list rather than a live advert).

Option `companion.manual_add_contacts` (**default on**, decision 4): put the base companion in manual-add mode with nothing
auto-added, so **every** advert arrives as a full `NEW_ADVERT` and the companion's contact table never fills. It is sent with
`CMD_SET_OTHER_PARAMS` carrying only the first parameter (so the companion's telemetry and location settings are untouched)
and only when `APP_START`'s reply shows it is not already set. The bare-advert lookup stays as the fallback for contacts the
companion stored before it was switched.

### The pipeline

Per observation in a report, in this order (the full description is in [beacon-base.md](beacon-base.md)):

1. **Repeater.** Unknown: with auto-add for repeaters on it is trusted now (taking its position, name and key from its
   advert) and processing goes on; otherwise the observation is stored as `unknown_repeater` and changes nothing. Disabled:
   stored as `disabled`.
2. **Beacon.** Unknown: with auto-add for beacons on it is trusted now, and this report becomes its baseline; otherwise the
   observation is stored as `unknown_beacon`. Only a trusted, enabled repeater gets this far, so a repeater that is not
   trusted can never get a beacon trusted. Disabled: stored as `disabled`.
3. Then the high-water mark, dedupe and grouping as before.

Name announcements are accepted from any repeater (decision 9).

### Data model (migration 1 is edited, the dev database is deleted)

- `repeater_adverts`: `prefix` (primary key), `pubkey` (32), `name`, `lat`, `lon` (NULL until an advert carries a valid
  position), `advert_timestamp`, `first_seen`, `last_heard` (NULL when only known from the companion's contact list).
  Independent of the repeater table, so an advert can arrive before the repeater is trusted.
- `repeaters`: `lat` and `lon` stay `NOT NULL`, default `0, 0`. New `location_source` (`none`, `advert`, `manual`) and
  `location_updated_at`. `pubkey` is filled from the advert when known (useful later for report signatures).
- `settings`: key/value, holds `autoadd.beacons` and `autoadd.repeaters`.
- A repeater is **located** when `lat` and `lon` are not both 0. `Store.located_repeaters()` (enabled and located) is the one
  place that says so; the estimator will use it.

### Rules

- Every repeater advert updates `repeater_adverts`. A valid position also replaces `repeaters.lat/lon` for a trusted repeater,
  whatever wrote them before: the latest write wins (decision 2). An advert without a position never erases a known one. An
  advert from a trusted repeater also sets its name, with or without a position (decision 3).
- `repeater add` takes the position and name from the advert when none is given: `repeater add <prefix>` uses the heard
  ones, or 0,0 with source `none`. Giving `<lat> <lon>` sets source `manual`, until the next advert.
- Unlocated repeaters are processed normally: their reports are accepted and stored, they are only left out of positioning.

### Discovery (decisions 7 and 8)

- `Store.unknown_repeaters` merges unknown-repeater observations (reports) with `repeater_adverts` rows for repeaters not in
  the table, newest first, each with its advertised name and position and its report count (`0` for advert-only). The window
  for an advert-only repeater is its last live advert, or when it was first seen if it only came from the contact list.
- `Store.unknown_beacons` counts observations of status `unknown_beacon` (trusted repeater) and `unknown_repeater` (not
  trusted); `n_trusted` is how many came from trusted repeaters.
- `repeater add --all` and `beacon add --all` trust everything in those lists, in one transaction.

### Auto-add (decision 10)

- Auto-add happens in two places: `Pipeline` (a report from an unknown repeater, or an unknown beacon from a trusted one) and
  `Store.record_repeater_advert` (an advert from an unknown repeater). Both read the settings table inside the transaction,
  so a change takes effect on the next report or advert. Ingest logs `auto-added repeater ...` / `auto-added beacon ...`.
- Turning a mode on adds nothing already seen (use `add --all` for that); turning it off removes nothing.
- Risks of leaving it on: with auto-add for repeaters on, every repeater that advertises or reports is trusted, including
  ordinary mesh repeaters that never report (they fill the table and, until located, fail `check`); beacons can only be
  auto-added through trusted repeaters. `check` therefore fails while a mode is on, and `status` shows a warning with the
  command that locks it.

### CLI

| Command | Behavior |
|---|---|
| `repeater add <key-or-prefix> [<lat> <lon>] [--name N] [--window S]` | `lat`/`lon` optional; defaults to the advert's. |
| `repeater add --all [--hours H]` | Trusts every repeater that reported or advertised in the last `H` hours (default 24) and is not in the table, each with its advert position if known, else 0,0. |
| `repeater locate <ref> <lat> <lon>` | Set the position by hand (testing, before the repeater has advertised, or when it cannot be set on the repeater). The next advert with a position replaces it. |
| `repeater list` | `LAT`/`LON` show `-` when unlocated; `SOURCE` and `UPDATED` columns. |
| `beacon add --all [--hours H]` | Includes beacons seen only through repeaters that are not trusted yet. |
| `autoadd [beacons\|repeaters\|all [on\|off]]` | Show or set the auto-add modes. |
| `status` | Lists every repeater and beacon seen but not trusted, with advertised or announced name and position and the `add` command; warns while auto-add is on. |
| `check` | **Fails** (exit 1) for each unlocated repeater ("no location, excluded from positioning", with how to fix it) and while an auto-add mode is on. |

### Operating notes (also in the README)

- After setting a repeater's position from the app, send a flood advert (`advert` in its CLI) or the base waits up to 47 hours
  (`flood.advert.interval`) for the next one. Lowering `flood.advert.interval` on the repeaters shortens that.
- The advert must reach the base companion: within 8 hops of it and in radio range. A repeater whose advert never arrives is
  located by hand with `repeater locate`.
- The position in an advert is what the repeater was told; the base does not check it against anything.
- Setting up a mesh: `autoadd all on`, bring repeaters and beacons up, watch `status` and the ingest log, then
  `autoadd all off`. Or leave it off and use `add --all` when the lists look right.

## Testing

Table-driven store tests for the rules above (advert before and after add, last write wins in both directions, no position
never erases, 0,0 and out-of-range kept as no position, advert-only repeaters listed and added, the list windows, reporters
and advert-only merged, unknown beacons via untrusted repeaters); pipeline tests (repeaters checked first, a first report lists
both at once, every auto-add combination including a disabled repeater and locking); session tests against the fake companion
(contact list at connect, `NEW_ADVERT`, bare `ADVERT` lookup by key, non-repeater adverts ignored, manual-add mode set only when
it differs and opt-out); service tests (adverts update trusted and untrusted repeaters, auto-add from adverts and reports); CLI
tests for `add` with and without coordinates, `add --all`, `locate`, `list`, `status`, `check`, `autoadd`, and a discover-then-lock
run; the simulator emits repeater adverts, one without a position. Hardware: place a repeater, set its position from the app,
send an advert, see it located in `repeater list`.

## Phases

**R1. Ingest and store.** Companion advert handling, `repeater_adverts`, location rules, `Store.located_repeaters()`, discovery
lists, settings and auto-add, simulator.
**R2. CLI.** `repeater add` with optional coordinates, `--all` for repeaters and beacons, `locate`, `list`, `status`, `check`,
`autoadd`, docs.
**R3. Hardware check.** Real repeaters located from the Android app, adverts heard by the base companion, and a discover-then-lock
run with auto-add.

## Open questions

None.
