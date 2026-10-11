# Utilities

One-off and diagnostic scripts that are **not part of the `beacon_base` package**: they are not installed, `beaconctl` does not call
them, and nothing in the package imports them. Each is a single file you run directly, and says at the top what it needs. They are
for finding out what is going on when the base misbehaves; the base itself does not depend on them.

A utility that talks to the companion owns its serial port while it runs, so stop `beacon-ingest` first (only one program can hold
the port) and start it again afterwards.

| Script | What it is for |
|---|---|
| [advert_probe.py](advert_probe.py) | Finds out why a repeater's position at the base does not follow the one set on the repeater. |

The scripts are covered by tests that run against the fake companion and repeater (`tests/test_advert_probe.py`), so they stay
working as the fakes and the companion protocol evolve.

## advert_probe.py

### Purpose

Shows, side by side, **what a repeater says its position is**, **what it actually transmitted in an advert**, and **what the
companion stored**, and says which of them is wrong. It uses only the base companion's USB port: no second radio, no phone, no
serial cable to the repeater.

### The case it was written for

A beacon repeater (an ESP32-S3) kept having its map position reset to an old value at every advert, although `repeater locate`
had set a new one and the repeater's own `get lat` / `get lon` showed the new value.

The cause, confirmed on hardware: the repeater's adverts carried **no position at all** (a saved "no position in adverts" setting).
A companion only updates a stored contact's position when the advert carries one, so it kept the old coordinates. The base reads
the companion's contact after each advert push and treats what it finds as what the repeater advertised, so every advert put the old
position back.

Firmware fix: firmware repository commit `9409103c` ("always advertise the configured position on beacon repeaters") forces the
configured position into adverts on beacon repeater builds. The setting cannot be changed from the CLI in those builds, so the
repeater has to be reflashed. After reflashing, the position updated correctly. See "A repeater's position keeps reverting to an old
value" in [../docs/operations.md](../docs/operations.md).

### When to use it

- A repeater's position at the base (`beaconctl repeater list`, `repeater adverts`) does not follow the position set on the
  repeater, or goes back to an old value after each advert.
- `repeater adverts` shows a repeater whose position never changes, or one that is "heard" but never located.
- You want to know whether a repeater's adverts are reaching the companion at all, and what is in them.

It is **not** needed when the repeater is simply unlocated (0, 0), or when no advert has been sent since the position was set: send
an advert first (`beaconctl repeater remote <repeater> advert`, or `advert` on the repeater's CLI) and look at
`repeater adverts`.

### How to run it

Needs Python 3.11+ and `pyserial` (the package's virtual environment has it). It imports nothing from `beacon_base`.

Stop `beacon-ingest` first, then:

```bash
# nRF52 companion (XIAO nRF52, Ikoka stick): DTR on, which is the default
python3 utilities/advert_probe.py /dev/serial/by-id/<companion> <repeater> --dtr on

# ESP32-S3 companion: DTR off
python3 utilities/advert_probe.py /dev/serial/by-id/<companion> <repeater> --dtr off
```

`<repeater>` is the repeater's name (or part of it) or the first 6 or more hex digits of its key; it must match exactly one repeater
contact in the companion. Options:

| Option | Meaning |
|---|---|
| `--dtr on\|off` | DTR level for the port. `on` for an nRF52 companion (it only transmits while DTR is high), `off` for an ESP32-S3 (it can reset otherwise). Default `on`. The script does not detect the board; the wrong setting shows up as "the companion did not answer". |
| `--password PW` | The repeater's admin password. Not needed when the base is already an admin there (it is after the first password login). On a command line it is visible to other users of the machine, so prefer to run it where that does not matter. |
| `--wait SECS` | How long to wait for the advert after asking for it (default 45). Raise it (90) if the repeater is far away. |
| `--no-set-clock` | Never touch the companion's clock (see below). |

Restart `beacon-ingest` when it finishes.

### What it changes

Exactly two things, and nothing is written to the base's database:

1. **The companion's clock**, moved *forward* to this computer's if it is more than a minute behind (as `beacon-ingest` does; a
   repeater ignores a login stamped earlier than ones it has seen). It never moves it back, and does nothing if this computer's
   clock is clearly wrong (before 1 March 2026). `--no-set-clock` turns this off.
2. **The repeater sends one flood advert**, because the script asks it to with the `advert` command after logging in. This reaches
   every node in range, like any advert.

It also logs in to the repeater as the base does, which registers the base in the repeater's access list if it was not there (with
`--password`). It sets nothing on the repeater and removes nothing from the companion.

### Reading the output

The script prints sections: the companion (clock), the repeater and the companion's current copy of it (position and time of the last
advert), the login, what the repeater says (clock, latitude, longitude, name), and the advert (heard off the air, and the companion's
copy afterwards). It ends with a verdict:

| Verdict | Meaning | What to do |
|---|---|---|
| **The repeater's advert carries NO position** | The repeater transmitted an advert without coordinates, so the companion keeps whatever it saw last and the base reads that back. Probably the repeater's advert-location setting is "none". This is the case above. | Reflash the repeater with a beacon repeater build from firmware commit `9409103c` on. Until then the repeater cannot be fixed from the base. |
| **The companion DROPPED the advert as a replay** | The advert is right, but the companion's record of this repeater's last advert is not older than it, because the repeater's clock went backwards (no RTC, or it lost power). | `beaconctl repeater remote <repeater> clock sync` with `beacon-ingest` running again (sets its clock forward only), or remove the contact from the companion so it is learned afresh. A repeater whose clock is ahead needs a reboot with its clock reset on its console, then a sync. |
| **Everything on the radio side is right** | The repeater advertised the new position and the companion holds it. | The fault is in the base. Check its log for `repeater ... moved to` and `beaconctl repeater adverts`. |
| **The advert carries X but the repeater itself reports Y** | The repeater transmits a different position from the one it says it has. Unexpected. | Keep the whole output and report it. |
| **INCONCLUSIVE: no advert was heard** | The advert was lost, or the repeater is out of range of the companion. | Run it again, or give it longer with `--wait 90`; check the radio path. |
| **The repeater advertises 0, 0** | It is unlocated. The base ignores 0, 0, so this is not the cause. | Set a position on the repeater. |

Other messages mean the run stopped early: "the companion did not answer" (beacon-ingest still running, or `--dtr` wrong for this
board), "no answer to the login" (wrong password, out of range, or the repeater ignoring the companion's clock), "logged in, but not as
an admin", or "N repeater contacts match" (be more specific; they are listed).

### Limits

- It needs the repeater to be a **contact in the companion** and within radio reach for the login; it cannot diagnose a repeater the
  companion has never heard.
- It sees only the **one advert** it triggered. A repeater whose adverts differ from time to time (for example zero-hop and flood
  adverts with different settings) can pass once and fail later.
- It reads the advert from the companion's **raw radio log**, so it needs a companion firmware that provides it. It does not verify
  the advert's signature.
- It does not look at the base's database, so "everything on the radio side is right" only rules the radio side out.
- Positions are compared to about 0.0001 degrees (the repeater's `get lat` is a 32-bit float, so reads are not exact).
- It cannot fix anything. A repeater that cannot be read back (no admin access) stops it at the login.
