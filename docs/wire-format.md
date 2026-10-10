# Beacon report wire format

A beacon repeater batches the beacons it has heard into one report and sends it as a `GRP_DATA` packet on the private
report channel. The base receives it from the companion as `RESP_CODE_CHANNEL_DATA_RECV` and decodes the payload with
[`wire.py`](../src/beacon_base/wire.py).

The format is owned by the firmware repository (`src/helpers/BeaconReport.h`). This page documents it where it is
consumed; if they disagree the firmware wins, and the golden vectors in
[`tests/fixtures/beacon_report_v1.json`](../tests/fixtures/README.md) are what keeps the two in step.

## Companion frame (`RESP_CODE_CHANNEL_DATA_RECV`, 0x1B)

| Byte | Field |
|---|---|
| 0 | `0x1B` |
| 1 | SNR at the **companion** (int8, dB x 4). Not the repeater's SNR; it is stored but not used for estimation. |
| 2-3 | reserved |
| 4 | channel index |
| 5 | path length: encoded hop count when flooded, `0xFF` when the packet arrived by a direct route |
| 6-7 | `data_type`, little-endian. Beacon reports use `0xFFBE`. |
| 8 | data length |
| 9+ | data: the report below |

The base ignores frames whose channel index is not the configured report channel or whose `data_type` is not `0xFFBE`.

## Report (version 1)

All integers are little-endian.

Header, 10 bytes:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | version, must be `1` |
| 1 | 8 | repeater public key prefix |
| 9 | 1 | entry count, 0 to 9 |

Followed by `count` entries of 16 bytes:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | beacon public key prefix |
| 8 | 4 | beacon counter (uint32) |
| 12 | 1 | RSSI at the repeater (int8, dBm) |
| 13 | 1 | SNR at the repeater (int8, dB x 4) |
| 14 | 2 | beacon battery (uint16, mV) |

A full packet is 9 entries, 154 of the 165 data bytes a `GRP_DATA` packet can carry.

## Name announcements (`data_type` 0xFFBF)

A repeater tells the base what the beacons it hears call themselves. It is a separate message on the same channel, so the
report format above is unchanged. Sent on first sight of a beacon since the repeater booted, when the name changes, and
every `beacon.name_refresh` hours (default 4). Golden vectors: [`tests/fixtures/beacon_names_v1.json`](../tests/fixtures/README.md),
decoder: `wire.decode_names`.

Header, 10 bytes (as for reports): version `1`, repeater key prefix (8), entry count (1). Then `count` variable-length entries:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | beacon public key prefix |
| 8 | 1 | name length |
| 9 | name length | name, UTF-8, no NUL |

- Same drop rules as reports: an unknown version, a short header, a count above 17 or an entry running past the end drops the
  whole message and is logged. Trailing bytes are ignored and zero-length names are skipped.
- A beacon's name is at most 18 bytes, so an entry is at most 27 bytes and a message holds 5 of them (7 with the 13-byte
  default names, `beacon-` plus the first three key bytes in hex).
- Names are untrusted display text. The base cleans them (`names.sanitize_name`: invalid UTF-8 replaced, control, format
  and separator characters turned into spaces, whitespace collapsed, at most 32 bytes). Announcements are taken from any
  repeater, trusted or not, so an unlisted beacon can be recognised before it is added.

## Decoder rules

- An unknown version, a header shorter than 10 bytes, a count above 9, or fewer bytes than the count needs is an error:
  the whole report is dropped and counted, never guessed at.
- Trailing bytes after the last entry are ignored, and a report with zero entries is valid.
- Reports carry **no time**. The base stamps them when it receives them.
