#!/usr/bin/env python3
"""Find out why a repeater's position keeps reverting at the base, using only the base companion's USB port.

Stop beacon-ingest first (only one program can hold the port), then run:

    python3 advert_probe.py /dev/serial/by-id/YOUR-COMPANION  <repeater name or key prefix>  [--password PW] [--dtr on|off] [--wait SECS]

It reads the repeater's contact from the companion, logs in to the repeater (as the base does), asks it for its clock and the
position it has set, tells it to send an advert, and captures that advert off the air from the companion's raw radio log. Then it
compares what the repeater says, what it transmitted, and what the companion stored, and prints which of them is wrong.

Changes it can make: it moves the companion's clock forward to this computer's if the companion is more than a minute behind (as
beacon-ingest does; --no-set-clock turns that off), and it makes the repeater send one flood advert. Nothing else.
"""

import argparse
import datetime
import struct
import sys
import time

import serial

# commands
APP_START, GET_CONTACTS, GET_TIME, SET_TIME, SYNC_NEXT, RESET_PATH = 1, 4, 5, 6, 10, 13
DEVICE_QUERY, GET_CONTACT_BY_KEY, SEND_LOGIN, SEND_TXT = 22, 30, 26, 2
# replies and pushes
RESP_OK, RESP_ERR, RESP_CONTACT, RESP_END_OF_CONTACTS, RESP_SENT = 0, 1, 3, 4, 6
RESP_CURR_TIME, RESP_NO_MORE = 9, 10
SYNC_REPLIES = {7, 8, 10, 16, 17, 27}
PUSH_ADVERT, PUSH_MSG_WAITING, PUSH_LOGIN_SUCCESS, PUSH_LOGIN_FAIL, PUSH_LOG_RX, PUSH_NEW_ADVERT = 0x80, 0x83, 0x85, 0x86, 0x88, 0x8A
ADV_TYPE_REPEATER = 2
CLOCK_FLOOR = 1772323200  # 1 March 2026, the firmware's own floor for a believable clock
POSITION_TOLERANCE = 1e-4  # degrees; `get lat` prints a 32-bit float, which is only good to about 1e-5


class CmdError(Exception):
    def __init__(self, code):
        super().__init__(f"the companion answered error {code}")
        self.code = code


class Quiet(Exception):
    pass


def utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Link:
    def __init__(self, port, dtr):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, 115200, 0.2
        s.dtr, s.rts = dtr, False  # an nRF52 companion only transmits while DTR is high; an ESP32-S3 can reset if it is toggled
        s.open()
        self.s, self.buf = s, bytearray()

    def send(self, payload):
        self.s.write(b"<" + len(payload).to_bytes(2, "little") + payload)

    def recv(self, timeout):
        end = time.monotonic() + timeout
        while True:
            f = self._take()
            if f is not None:
                return f
            if time.monotonic() >= end:
                return None
            self.buf += self.s.read(max(1, self.s.in_waiting))

    def _take(self):
        while True:
            i = self.buf.find(b">")
            if i < 0:
                self.buf.clear()
                return None
            del self.buf[:i]
            if len(self.buf) < 3:
                return None
            n = int.from_bytes(self.buf[1:3], "little")
            if n == 0 or n > 176:  # not a real frame header, skip the byte
                del self.buf[0]
                continue
            if len(self.buf) < 3 + n:
                return None
            frame = bytes(self.buf[3 : 3 + n])
            del self.buf[: 3 + n]
            return frame


class State:
    def __init__(self):
        self.waiting = False
        self.login = None  # ("ok", admin_flag, permissions) or ("fail",)
        self.raw_adverts = []  # (arrival time, parsed advert) for the repeater being tested
        self.advert_pushes = []  # arrival times of PUSH_ADVERT / PUSH_NEW_ADVERT for the repeater
        self.key = b""


def parse_raw_advert(raw):
    """Decode an advert packet as it came off the air. Returns None for anything else."""
    if len(raw) < 2 or ((raw[0] >> 2) & 0x0F) != 4:
        return None
    route = raw[0] & 3
    i = 1 + (4 if route in (0, 3) else 0)  # transport codes
    if len(raw) <= i:
        return None
    plen = raw[i]
    hops = plen & 63
    i += 1 + hops * ((plen >> 6) + 1)
    payload = raw[i:]
    if len(payload) < 100 + 1:
        return None
    key, ts, app = payload[:32], struct.unpack("<I", payload[32:36])[0], payload[100:]
    flags, j, lat, lon = app[0], 1, None, None
    if flags & 0x10:
        lat, lon = struct.unpack("<ii", app[j : j + 8])
        lat, lon, j = lat / 1e6, lon / 1e6, j + 8
    if flags & 0x20:
        j += 2
    if flags & 0x40:
        j += 2
    name = app[j:].split(b"\0")[0].decode(errors="replace") if flags & 0x80 else None
    return {"key": key, "timestamp": ts, "type": flags & 0x0F, "lat": lat, "lon": lon, "name": name, "hops": hops}


def note_push(state, f):
    code = f[0]
    if code == PUSH_LOG_RX:
        adv = parse_raw_advert(f[3:])
        if adv is not None and adv["key"] == state.key:
            state.raw_adverts.append((time.monotonic(), adv))
    elif code == PUSH_MSG_WAITING:
        state.waiting = True
    elif code == PUSH_LOGIN_SUCCESS:
        state.login = ("ok", f[1], f[12] if len(f) >= 13 else None)
    elif code == PUSH_LOGIN_FAIL:
        state.login = ("fail",)
    elif code in (PUSH_ADVERT, PUSH_NEW_ADVERT) and f[1:33] == state.key:
        state.advert_pushes.append(time.monotonic())


def call(link, state, payload, ok, timeout=6.0):
    link.send(payload)
    end = time.monotonic() + timeout
    while True:
        f = link.recv(max(0.05, end - time.monotonic()))
        if f is None:
            raise Quiet("the companion did not answer")
        if f[0] in ok:
            return f
        if f[0] == RESP_ERR:
            raise CmdError(f[1] if len(f) > 1 else 0)
        note_push(state, f)


def parse_contact(f):
    ts, lat, lon, mod = struct.unpack("<IiiI", f[132:148])
    return {
        "key": f[1:33], "type": f[33], "name": f[100:132].split(b"\0")[0].decode(errors="replace"),
        "timestamp": ts, "lat": lat / 1e6, "lon": lon / 1e6,
    }


def get_contacts(link, state):
    link.send(bytes([GET_CONTACTS]))
    out, end = [], time.monotonic() + 20
    while time.monotonic() < end:
        f = link.recv(3)
        if f is None:
            raise Quiet("the contact list stopped")
        if f[0] == RESP_END_OF_CONTACTS:
            return out
        if f[0] == RESP_CONTACT:
            out.append(parse_contact(f))
        elif f[0] == RESP_ERR:
            raise CmdError(f[1] if len(f) > 1 else 0)
        elif f[0] >= 0x80:
            note_push(state, f)
    raise Quiet("the contact list did not end")


def parse_message(f):
    if f[0] == 16 and len(f) >= 16:
        return {"prefix": f[4:10], "txt_type": f[11], "text": f[16:].decode(errors="replace")}
    if f[0] == 7 and len(f) >= 13:
        return {"prefix": f[1:7], "txt_type": f[8], "text": f[13:].decode(errors="replace")}
    return None


def login(link, state, key, password):
    for attempt in (1, 2):
        state.login = None
        sent = call(link, state, bytes([SEND_LOGIN]) + key + password.encode(), {RESP_SENT})
        est = struct.unpack("<I", sent[6:10])[0] / 1000
        end = time.monotonic() + min(max(est * 1.5, 10), 40)
        while time.monotonic() < end and state.login is None:
            f = link.recv(0.5)
            if f is not None:
                note_push(state, f)
        if state.login is not None:
            return state.login
        if attempt == 1:  # the repeater says nothing to a login it refuses; try once more over a fresh route
            call(link, state, bytes([RESET_PATH]) + key, {RESP_OK})
    return None


def cli(link, state, key, text, tag):
    sent = call(link, state, bytes([SEND_TXT, 1, 0]) + bytes(4) + key[:6] + f"{tag}|{text}".encode(), {RESP_SENT})
    est = struct.unpack("<I", sent[6:10])[0] / 1000
    end, last_poll = time.monotonic() + min(max(est * 2, 10), 40), 0.0
    while time.monotonic() < end:
        f = link.recv(0.3)
        if f is not None:
            note_push(state, f)
        if state.waiting or time.monotonic() - last_poll > 1.0:
            state.waiting, last_poll = False, time.monotonic()
            while True:
                r = call(link, state, bytes([SYNC_NEXT]), SYNC_REPLIES)
                if r[0] == RESP_NO_MORE:
                    break
                m = parse_message(r)
                if m and m["txt_type"] == 1 and m["text"].startswith(tag + "|"):
                    return m["text"][len(tag) + 1 :]
    return None


def number(reply):
    try:
        return float(reply.replace(">", "").strip())
    except (ValueError, AttributeError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("repeater", help="the repeater's name (or part of it) or the first 6 or more hex digits of its key")
    ap.add_argument("--password", default="", help="the repeater's admin password; not needed when the base is already an admin")
    ap.add_argument("--dtr", choices=["on", "off"], default="on", help="on for an nRF52 companion (Ikoka, XIAO nRF52), off for an ESP32-S3")
    ap.add_argument("--wait", type=float, default=45.0, help="seconds to wait for the advert (default 45)")
    ap.add_argument("--no-set-clock", action="store_true", help="never touch the companion's clock")
    args = ap.parse_args()

    state, link = State(), Link(args.port, args.dtr == "on")
    try:
        return run(args, state, link)
    except Quiet as e:
        print(f"\nStopped: {e}. Is beacon-ingest still running, or is --dtr wrong for this companion?")
    except CmdError as e:
        print(f"\nStopped: {e}.")
    return 1


def run(args, state, link):
    print("== companion")
    call(link, state, bytes([APP_START]) + bytes(7) + b"advert_probe", {5})
    call(link, state, bytes([DEVICE_QUERY, 3]), {13})  # declare protocol version 3 so messages arrive with their SNR
    comp = struct.unpack("<I", call(link, state, bytes([GET_TIME]), {RESP_CURR_TIME})[1:5])[0]
    host = int(time.time())
    print(f"clock: companion {utc(comp)}, this computer {utc(host)}")
    if comp < host - 60:
        if args.no_set_clock or host < CLOCK_FLOOR:
            print("  the companion is behind: the repeater may ignore its login until it is set (not changing it)")
        else:
            try:
                call(link, state, bytes([SET_TIME]) + host.to_bytes(4, "little"), {RESP_OK})
                print("  moved the companion's clock forward to this computer's")
            except CmdError as e:
                print(f"  could not set the companion's clock: {e}")
    elif comp > host + 60:
        print("  the companion is AHEAD of this computer; it cannot be set back (reboot it), logins still work")

    want = args.repeater.lower()
    matches = [c for c in get_contacts(link, state) if c["type"] == ADV_TYPE_REPEATER and (c["key"].hex().startswith(want) or want in c["name"].lower())]
    if len(matches) != 1:
        print(f"\n{len(matches)} repeater contacts match {args.repeater!r}" + (":" if matches else "."))
        for c in matches:
            print(f"  {c['name']}  {c['key'].hex()}")
        return 1
    before = matches[0]
    key = state.key = before["key"]
    print(f"\n== {before['name']}  (key {key.hex()})")
    print(f"companion's copy:  position {before['lat']:.6f}, {before['lon']:.6f}   last advert {utc(before['timestamp'])}")

    print("\n== login")
    result = login(link, state, key, args.password)
    if result is None:
        print("no answer to the login (wrong password, out of range, or the repeater ignores this clock). Try --password.")
        return 1
    if result[0] != "ok" or result[1] != 1:
        print(f"logged in, but not as an admin (admin flag {result[1] if len(result) > 1 else '-'}, role {result[2] if len(result) > 2 else '-'}): its CLI would ignore us. Try --password.")
        return 1
    print("logged in as admin")

    print("\n== the repeater says")
    said = {}
    for n, (label, text) in enumerate([("clock", "clock"), ("latitude", "get lat"), ("longitude", "get lon"), ("name", "get name")]):
        said[label] = cli(link, state, key, text, f"q{n}")
        print(f"{label:10} {said[label] if said[label] is not None else '(no reply)'}")
    set_lat, set_lon = number(said["latitude"]), number(said["longitude"])

    print("\n== advert")
    triggered = time.monotonic()
    reply = cli(link, state, key, "advert", "q9")
    print(f"told it to advert: {reply if reply is not None else '(no reply, trying anyway)'}")
    deadline, grace = triggered + args.wait, None
    while time.monotonic() < deadline:
        f = link.recv(0.5)
        if f is not None:
            note_push(state, f)
        heard = [a for t, a in state.raw_adverts if t >= triggered]
        if heard and grace is None:
            grace = time.monotonic() + 4  # let the companion finish handling it, and catch any repeat
        if grace is not None and time.monotonic() >= grace:
            break
    after_contact = next((c for c in get_contacts(link, state) if c["key"] == key), None)
    heard = [a for t, a in state.raw_adverts if t >= triggered]
    pushed = any(t >= triggered for t in state.advert_pushes)

    print(f"adverts from it heard off the air: {len(heard)}")
    adv = heard[-1] if heard else None
    if adv:
        pos = f"{adv['lat']:.6f}, {adv['lon']:.6f}" if adv["lat"] is not None else "NONE (the advert has no position)"
        print(f"  advert time {utc(adv['timestamp'])}   position {pos}   name {adv['name']!r}   hops {adv['hops']}")
    print(f"companion told the host about an advert from it: {'yes' if pushed else 'no'}")
    if after_contact:
        print(f"companion's copy now: position {after_contact['lat']:.6f}, {after_contact['lon']:.6f}   last advert {utc(after_contact['timestamp'])}")

    print("\n== verdict")
    verdict(adv, pushed, before, after_contact, set_lat, set_lon)
    print("\nRestart beacon-ingest when you are done.")
    return 0


def same(a, b):
    return a is not None and b is not None and abs(a - b) <= POSITION_TOLERANCE


def verdict(adv, pushed, before, after, set_lat, set_lon):
    if adv is None:
        print("INCONCLUSIVE: no advert from the repeater was heard. It may be out of range of the companion, or the advert was lost. Run it again,")
        print("or give it longer with --wait 90.")
        return
    if adv["lat"] is None:
        print("The repeater's advert carries NO position. Its advert-location setting is probably 'none', so the companion never learns the new")
        print("position and keeps whatever it saw last, which the base then reads back as if it had just been advertised. This is a firmware")
        print("settings problem on that repeater. Beacon repeater firmware from commit 9409103c on always advertises the configured position,")
        print("so reflash the repeater with a current beacon repeater build.")
        return
    if (adv["lat"], adv["lon"]) == (0.0, 0.0):
        print("The repeater advertises the position 0, 0 (unlocated). The base ignores that, so it is not what you are seeing.")
        return
    if set_lat is not None and set_lon is not None and not (same(adv["lat"], set_lat) and same(adv["lon"], set_lon)):
        print(f"The advert carries {adv['lat']:.6f}, {adv['lon']:.6f} but the repeater itself reports {set_lat}, {set_lon}.")
        print("The repeater is transmitting a different position from the one it says it has. That is unexpected: send me this output.")
        return
    if not pushed and after and after["timestamp"] >= adv["timestamp"]:
        print("The repeater advertises the right position, but the companion DROPPED the advert as a replay: its record of this repeater's last")
        print(f"advert ({utc(after['timestamp'])}) is not older than this one ({utc(adv['timestamp'])}). The repeater's clock has gone backwards (no RTC, or it")
        print("lost power). The companion keeps the old position until the repeater's clock passes that time. Fix: 'clock sync' from the base")
        print("(it can only move the clock forward), or remove the contact from the companion so it is learned again.")
        return
    if after and (same(after["lat"], adv["lat"]) and same(after["lon"], adv["lon"])):
        print("Everything on the radio side is right: the repeater advertised the new position and the companion holds it. If the base still")
        print("shows an old position, the problem is in the base. Check its log for 'repeater ... moved to' and its repeater_adverts table.")
        return
    print("The advert carries the right position and the companion accepted it, but the companion's copy has a different position.")
    print("That is unexpected: send me this output.")


if __name__ == "__main__":
    sys.exit(main())
