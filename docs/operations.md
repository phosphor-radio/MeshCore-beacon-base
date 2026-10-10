# Operations notes

Things that matter when running the base on real hardware. Setup and commands are in the [README](../README.md).

## Managing repeaters from the base

`beaconctl repeater remote` (README, "Managing repeaters over the mesh") needs `beacon-ingest` running with the companion connected,
and a few things true in the field:

- **The base's clock is set.** The companion's clock only moves forward and an nRF52 companion restarts at May 2024 on every boot.
  A repeater ignores a login or command stamped earlier than the last it saw from the same key, and says nothing. `beacon-ingest`
  moves the companion's clock forward to the base's once the base's clock is trusted (`beaconctl time set`, or `clock.assume_synced`),
  and a remote job is refused (`clock_untrusted`) until then. If the companion is *ahead* of the base (a wrong time was once pushed
  on it) `beaconctl status` says so; it cannot be set back, so reboot it.
- **The repeaters' clocks.** A repeater stamps its adverts with its own clock, and nodes that have heard a later advert from it ignore an
  earlier one, so a repeater that boots with a wrong clock (no RTC) can go unheard. `beaconctl repeater remote <r> get clock` shows how far
  it is from the base, and `clock sync` sets it forward to the base's (never backward; one that is ahead needs a reboot with its clock
  reset on its serial console, then a sync).
- **Rebooting a repeater** from the base loses the report batch it is holding and the reboot cannot be confirmed (it never answers). The
  repeater keeps the base as an admin through the reboot, so the next command works once it is back.
- **Each repeater has had one password login.** The stock admin password is `password`; change it on the repeater's serial console.
  After the first login the repeater keeps the base as an admin in its flash. **A replaced companion is a new key**, so every repeater
  needs the password again.
- **The mesh forwards the base's floods.** The first message to a repeater is an unscoped flood. A repeater two or more hops away is
  only reached if the repeaters between forward unscoped floods; if they do not (`region denyf *`, or `flood.max.unscoped 0`) logins fail
  as `no_reply` until the companion has a default flood scope matching a region those repeaters allow.
- **The database is private to its user.** A new database file is created mode 600 (a job can hold an admin password for a moment); an
  existing one is not changed. If `beaconctl` and `beacon-ingest` run as different users, share it deliberately (a group and
  `chmod 660`) as part of the deployment.

## Companion serial connection: DTR

The base talks to its companion over USB serial, and the two kinds of companion board want opposite things from the DTR line
when the port is opened:

| Companion | USB vendor | DTR | Why |
|---|---|---|---|
| XIAO ESP32-S3 (Wio-SX1262), `Xiao_S3_WIO_companion_radio_usb` | Espressif, `0x303A` | **low** | Its native USB can reset the board when the host toggles DTR/RTS on open. |
| nRF52 boards: XIAO nRF52, Ikoka stick (`Xiao_nrf52_companion_radio_usb` and the like) | Adafruit TinyUSB, other | **high** | Adafruit TinyUSB's serial only transmits while the host asserts DTR (its write loop is `while (remain && tud_cdc_n_connected())`), and the companion firmware always reports itself connected, so with DTR low every reply is silently dropped. |

RTS is never raised. The MeshCore web app works with nRF52 boards because Web Serial asserts DTR.

### The `companion.dtr` option

```toml
[companion]
dtr = "auto"   # "auto" (default), "on" or "off"
```

- **`auto`** (default) picks by the USB vendor id of the port: Espressif (`0x303A`) keeps DTR low, anything else, or a device
  whose vendor cannot be found, gets DTR high. If the very first `APP_START` of a connection gets no reply within
  `companion.command_timeout`, the port is closed and reopened once with the opposite setting, and whichever setting gets a
  reply is kept for later reconnects in that process. The log says which setting is used and when it falls back, for example
  `no reply to APP_START with DTR low; reopening with DTR high (companion.dtr = auto)`. If neither setting gets a reply the
  attempt ends like any failed connection (retry with backoff) and the next attempt starts over.
- **`on`** / **`off`** force it and never fall back. Use them if `auto` guesses wrong for a board and you do not want the
  extra attempt on every start, or while diagnosing.

`beaconctl listen --dtr auto|on|off`, `beaconctl ingest --dtr ...` and `beacon-ingest --dtr ...` override the option for one
run.

### Symptom of the wrong setting

`beaconctl listen` or `beacon-ingest` logs

```
companion link lost: no reply to command 1 within 5s
```

over and over (command 1 is `APP_START`), although the board is plugged in and the port opens. With `companion.dtr = auto` this
should not happen; if it does with `off` set explicitly on an nRF52 board (the usual cause), set `on` or `auto`. With `on`
forced on an ESP32-S3 the symptom is the board resetting when the port is opened. Checks:

```bash
ls -l /dev/serial/by-id            # the board's port; the name usually shows the vendor
beaconctl listen --dtr on          # an nRF52 board
beaconctl listen --dtr off         # an ESP32-S3 board
```

Only one process can own the port at a time (it is opened exclusively), so stop `beacon-ingest` before running `listen`.
