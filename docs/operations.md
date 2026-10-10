# Operations notes

Things that matter when running the base on real hardware. Setup and commands are in the [README](../README.md).

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
