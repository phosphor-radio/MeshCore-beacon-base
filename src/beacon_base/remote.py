"""The closed set of remote repeater operations: what may be asked, the commands each one sends and how the replies are read.

This is the only place that turns a job into repeater CLI text. A job carries a kind, ``get`` or ``set`` and typed parameters,
never command text, so whoever can write the database can request only what is listed here (``docs/plan/repeater-remote.md``).
Values are checked here against the limits the firmware enforces, and against the traps it does not: ``set lat`` reads junk as 0
and still answers ``OK``, passwords and names have length limits it truncates silently. Pure functions, no I/O.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

KINDS = (
    "name", "location", "advert.interval", "flood.advert.interval",
    "beacon.stats", "beacon.window", "beacon.channel", "beacon.names", "beacon.name_refresh",
    "all",
)
# read-only over the mesh: the channel key must not cross the air, and the counters are not settable
READ_ONLY = frozenset({"beacon.stats", "beacon.channel", "all"})
SETTABLE = frozenset(KINDS) - READ_ONLY
OPS = ("get", "set")

NAME_BAD_CHARS = frozenset("[]\\:,?*")  # the firmware refuses a name containing any of these
NAME_MAX_BYTES = 31
LOCATION_DECIMALS = 6  # the resolution of an advert
LOCATION_TOLERANCE = 2e-5  # degrees: 'get lat' prints a 32-bit float truncated to 7 decimals, so it reads back about 1 m off
ZERO_HOP_WARNING = (
    "zero-hop adverts are off (the first change on a repeater turns the 2-minute install default off); "
    "set advert.interval 60-240 to turn them on"
)

# job error codes and what the operator is told
ERROR_TEXT = {
    "no_ingest": "beacon-ingest is not running; start it",
    "clock_untrusted": "the base's clock is not set; run 'beaconctl time set' first",
    "no_key": "the base does not have this repeater's full key; it has to be heard advertising first",
    "no_reply": (
        "no answer from the repeater after retrying: a wrong admin password, the repeater out of range or its flood not forwarded, "
        "or its replay window ahead of the companion's clock"
    ),
    "needs_password": "the repeater does not have the base as an admin; supply the admin password (--password-file)",
    "not_admin": "the repeater granted a role below admin (the guest password, or this key is not an admin there)",
    "unsupported": "the repeater does not know that command: not a beacon build, or older firmware",
    "rejected": "the repeater refused the value",
    "mismatch": "the repeater answered OK but reads back a different value",
    "bad_value": "refused before sending",
    "expired": "the job was not started in time",
    "interrupted": "ingest stopped or lost the companion while the job was running",
    "contact_failed": "the companion would not take the repeater as a contact",
    "companion_refused": "the companion would not send it",
}


class RemoteError(ValueError):
    """A value that cannot be sent. ``code`` is one of ERROR_TEXT's keys."""

    def __init__(self, message: str, code: str = "bad_value"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RemoteJob:
    """A job as ingest runs it: what to do, to whom, and the password if one was given."""

    id: int
    repeater_prefix: bytes
    pubkey: bytes | None  # the repeater's full key, None if the base has never heard it advertise
    name: str | None
    lat: float
    lon: float
    kind: str
    op: str
    params: dict
    password: str | None
    created_at: float


@dataclass
class JobOutcome:
    ok: bool
    code: str | None = None  # an ERROR_TEXT key when not ok
    message: str = ""
    result: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    command: str  # the CLI text, without the tag
    role: str  # 'set' (must answer OK), 'get', 'verify' (read back after a set), 'extra' (advert.interval after a set)


# --- validation -----------------------------------------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in(params: dict, key: str, low: int, high: int, what: str) -> int:
    value = params.get(key)
    if not _is_int(value):
        raise RemoteError(f"{what} must be a whole number")
    if not low <= value <= high:
        raise RemoteError(f"{what} must be {low}-{high}")
    return value


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise RemoteError("a name is required")
    if name != name.strip():
        raise RemoteError("a name must not start or end with spaces")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise RemoteError("a name must not contain control characters")
    bad = sorted(set(name) & NAME_BAD_CHARS)
    if bad:
        raise RemoteError(f"a repeater name cannot contain {' '.join(bad)}")
    if len(name.encode("utf-8")) > NAME_MAX_BYTES:
        raise RemoteError(f"a name is at most {NAME_MAX_BYTES} bytes")
    return name


def _check_degrees(value: Any, low: float, high: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RemoteError(f"{what} must be a number")
    if not low <= value <= high:
        raise RemoteError(f"{what} must be between {low:g} and {high:g}")
    return round(float(value), LOCATION_DECIMALS)


def _check_interval(minutes: int) -> int:
    if minutes != 0 and (minutes < 60 or minutes > 240 or minutes % 2):
        raise RemoteError("the zero-hop advert interval is 0 (off) or an even number of minutes, 60-240")
    return minutes


def _check_hours(hours: int) -> int:
    if hours != 0 and not 3 <= hours <= 168:
        raise RemoteError("the flood advert interval is 0 (off) or 3-168 hours")
    return hours


def validate(kind: str, op: str, params: dict | None) -> dict:
    """The normalised parameters for a job, or RemoteError. Run when a job is submitted and again before it is sent."""
    params = dict(params or {})
    if kind not in KINDS:
        raise RemoteError(f"unknown item {kind!r}")
    if op not in OPS:
        raise RemoteError(f"unknown operation {op!r}")
    if op == "get":
        if params:
            raise RemoteError("a get takes no values")
        return {}
    if kind not in SETTABLE:
        why = "the channel key is never sent over the mesh" if kind == "beacon.channel" else "it can only be read"
        raise RemoteError(f"{kind} cannot be set over the mesh: {why}")
    verify = params.pop("verify", True)
    if not isinstance(verify, bool):
        raise RemoteError("verify must be true or false")
    if kind == "name":
        out = {"name": _check_name(params.pop("name", None))}
    elif kind == "location":
        allow_zero = params.pop("allow_zero", False)
        if not isinstance(allow_zero, bool):
            raise RemoteError("allow_zero must be true or false")
        lat = _check_degrees(params.pop("lat", None), -90.0, 90.0, "latitude")
        lon = _check_degrees(params.pop("lon", None), -180.0, 180.0, "longitude")
        if lat == 0.0 and lon == 0.0 and not allow_zero:
            raise RemoteError("0, 0 means 'no position' to the base; give a real position, or allow_zero to set it anyway")
        out = {"lat": lat, "lon": lon, "allow_zero": allow_zero}
    elif kind == "advert.interval":
        out = {"minutes": _check_interval(_int_in(params, "minutes", 0, 240, "minutes"))}
        params.pop("minutes")
    elif kind == "flood.advert.interval":
        out = {"hours": _check_hours(_int_in(params, "hours", 0, 168, "hours"))}
        params.pop("hours")
    elif kind == "beacon.window":
        out = {"seconds": _int_in(params, "seconds", 1, 3600, "the window")}
        params.pop("seconds")
    elif kind == "beacon.names":
        on = params.pop("on", None)
        if not isinstance(on, bool):
            raise RemoteError("beacon.names is on or off")
        out = {"on": on}
    else:  # beacon.name_refresh
        out = {"hours": _int_in(params, "hours", 0, 8760, "hours")}
        params.pop("hours")
    if params:
        raise RemoteError(f"unexpected values for {kind}: {', '.join(sorted(params))}")
    out["verify"] = verify
    return out


def validate_password(password: str | None) -> str | None:
    """The admin password as it will be sent: at most 15 bytes, which the firmware would otherwise truncate silently."""
    if password is None or password == "":
        return None
    raw = password.encode("utf-8")
    if len(raw) > 15:
        raise RemoteError("a repeater password is at most 15 bytes")
    if "\0" in password:
        raise RemoteError("a password cannot contain NUL")
    return password


# --- the commands ---------------------------------------------------------------------------------------------------------

_GETS = {
    "name": ("get name",),
    "location": ("get lat", "get lon"),
    "advert.interval": ("get advert.interval",),
    "flood.advert.interval": ("get flood.advert.interval",),
    "beacon.stats": ("beacon.stats",),
    "beacon.window": ("beacon.window",),
    "beacon.channel": ("beacon.channel",),
    "beacon.names": ("beacon.names",),
    "beacon.name_refresh": ("beacon.name_refresh",),
}
_ALL = ("name", "location", "advert.interval", "flood.advert.interval", "beacon.window", "beacon.names", "beacon.name_refresh", "beacon.channel")


def _set_commands(kind: str, p: dict) -> tuple[str, ...]:
    if kind == "name":
        return (f"set name {p['name']}",)
    if kind == "location":
        return (f"set lat {p['lat']:.{LOCATION_DECIMALS}f}", f"set lon {p['lon']:.{LOCATION_DECIMALS}f}")
    if kind == "advert.interval":
        return (f"set advert.interval {p['minutes']}",)
    if kind == "flood.advert.interval":
        return (f"set flood.advert.interval {p['hours']}",)
    if kind == "beacon.window":
        return (f"beacon.window {p['seconds']}",)
    if kind == "beacon.names":
        return (f"beacon.names {'on' if p['on'] else 'off'}",)
    return (f"beacon.name_refresh {p['hours']}",)


def plan(kind: str, op: str, params: dict) -> list[Step]:
    """The commands to send for a validated job, in order."""
    params = validate(kind, op, params)
    if op == "get":
        items = _ALL if kind == "all" else (kind,)
        return [Step(c, "get") for item in items for c in _GETS[item]]
    steps = [Step(c, "set") for c in _set_commands(kind, params)]
    if params["verify"]:
        steps += [Step(c, "verify") for c in _GETS[kind]]
        if kind != "advert.interval":
            steps.append(Step(_GETS["advert.interval"][0], "extra"))
    return steps


# --- the replies ----------------------------------------------------------------------------------------------------------

VALUE, OK, UNSUPPORTED, ERROR, RAW = "value", "ok", "unsupported", "error", "raw"
_STATS = re.compile(
    r"heard (\d+), reported (\d+), dropped (\d+), send fail (\d+), pending (\d+), names sent (\d+)(?:, up (\d+)s)?$"
)


def classify(reply: str) -> tuple[str, str]:
    """(kind, text): a value after '> ', an OK, an unsupported command, an error text, or raw text (the counters)."""
    text = reply.strip()
    if text.startswith(">"):
        return VALUE, text[1:].strip()
    if text == "OK" or text.startswith("OK "):
        return OK, text
    if text in ("Unknown command", "Unknown config", "Err - unknown beacon command") or text.startswith("unknown config"):
        return UNSUPPORTED, text
    if text.lower().startswith(("err", "error")):
        return ERROR, text
    return RAW, text


def step_failed(step: Step, reply: str) -> bool:
    """True when the reply to a 'set' step means the remaining steps should not be sent."""
    return step.role == "set" and classify(reply)[0] != OK


def _number(text: str) -> float | None:
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


def _leading_int(text: str) -> int | None:
    m = re.match(r"\d+", text)
    return int(m.group(0)) if m else None


def parse_stats(text: str) -> dict:
    m = _STATS.fullmatch(text.strip())
    if not m:
        return {"raw": text, "parsed": False}
    heard, reported, dropped, send_fail, pending, names_sent, up = m.groups()
    out = {
        "heard": int(heard), "reported": int(reported), "dropped": int(dropped), "send_fail": int(send_fail),
        "pending": int(pending), "names_sent": int(names_sent), "up_s": None if up is None else int(up), "parsed": True,
    }
    return out


def channel_hash(key: bytes) -> int:
    """The first byte of SHA-256 of a 16-byte channel key, which is what a repeater reports for its report channel."""
    return sha256(key[:16]).digest()[0]


def _item_value(item: str, replies: list[tuple[str, str]], base_channel_hash: int | None) -> Any:
    """The value of one item from its (classified) replies; raises RemoteError for an unsupported or unreadable one."""
    kinds = {k for k, _ in replies}
    if UNSUPPORTED in kinds:
        raise RemoteError(ERROR_TEXT["unsupported"], "unsupported")
    if ERROR in kinds or OK in kinds:
        raise RemoteError(next(t for k, t in replies if k in (ERROR, OK)), "rejected")
    if item == "beacon.stats":
        return parse_stats(replies[0][1])
    texts = [t for _, t in replies]
    if item == "name":
        return texts[0]
    if item == "location":
        lat, lon = _number(texts[0]), _number(texts[1])
        if lat is None or lon is None:
            raise RemoteError(f"could not read a position from {texts!r}", "rejected")
        return {"lat": lat, "lon": lon}
    if item == "beacon.channel":
        if texts[0] == "not set":
            return {"set": False, "hash": None, "matches_base": None}
        m = re.fullmatch(r"set, hash ([0-9A-Fa-f]{2})", texts[0])
        if not m:
            raise RemoteError(f"could not read the channel from {texts[0]!r}", "rejected")
        h = int(m.group(1), 16)
        return {"set": True, "hash": h, "matches_base": None if base_channel_hash is None else h == base_channel_hash}
    if item == "beacon.names":
        if texts[0] not in ("on", "off"):
            raise RemoteError(f"could not read on/off from {texts[0]!r}", "rejected")
        return texts[0] == "on"
    value = _leading_int(texts[0])  # the intervals and the window: '60 secs', '4 hours', '0 (only on first sight ...)', '120'
    if value is None:
        raise RemoteError(f"could not read a number from {texts[0]!r}", "rejected")
    return value


def _sent_value(kind: str, p: dict) -> Any:
    if kind == "name":
        return p["name"]
    if kind == "location":
        return {"lat": p["lat"], "lon": p["lon"]}
    if kind == "beacon.names":
        return p["on"]
    return next(v for k, v in p.items() if k not in ("verify", "allow_zero"))


def _same(kind: str, sent: Any, got: Any) -> bool:
    if kind == "location":
        return abs(sent["lat"] - got["lat"]) <= LOCATION_TOLERANCE and abs(sent["lon"] - got["lon"]) <= LOCATION_TOLERANCE
    return sent == got


def interpret(kind: str, op: str, params: dict, replies: list[str], base_channel_hash: int | None = None) -> JobOutcome:
    """Read the replies to plan(kind, op, params) (fewer if a 'set' was refused and the rest was not sent) into an outcome.

    result: ``values`` maps each item to what the repeater reports; for a set also ``sent`` (what was written, the value to record
    at the base, since the position reads back lossy), ``verified`` and ``notes``."""
    params = validate(kind, op, params)
    steps = plan(kind, op, params)
    classified = [classify(r) for r in replies]
    result: dict[str, Any] = {"replies": [{"command": s.command, "reply": r} for s, r in zip(steps, replies)]}
    if op == "get":
        return _interpret_get(kind, steps, classified, result, base_channel_hash)

    sets = [(s, c) for s, c in zip(steps, classified) if s.role == "set"]
    for s, (k, text) in sets:
        if k != OK:
            code = "unsupported" if k == UNSUPPORTED else "rejected"
            return JobOutcome(False, code, f"{s.command!r}: {text}", result)
    if len(sets) < len([s for s in steps if s.role == "set"]):
        return JobOutcome(False, "no_reply", "the reply to a set command did not arrive", result)
    sent = _sent_value(kind, params)
    result["sent"] = sent
    notes: list[str] = []
    if not params["verify"]:
        result.update(values={kind: sent}, verified=False, notes=notes)
        return JobOutcome(True, None, "set (not read back)", result)

    verify_replies = [c for s, c in zip(steps, classified) if s.role == "verify"]
    wanted = len(_GETS[kind])
    if len(verify_replies) < wanted:
        return JobOutcome(False, "no_reply", "set, but the read-back did not arrive", result)
    try:
        got = _item_value(kind, verify_replies, base_channel_hash)
    except RemoteError as e:
        return JobOutcome(False, e.code, f"read-back: {e}", result)
    result["values"] = {kind: got}
    if not _same(kind, sent, got):
        result["verified"] = False
        return JobOutcome(False, "mismatch", f"sent {sent!r} but the repeater reports {got!r}", result)
    result["verified"] = True
    extra = [c for s, c in zip(steps, classified) if s.role == "extra"]
    if extra:
        try:
            interval = _item_value("advert.interval", extra, None)
        except RemoteError:
            interval = None
        if interval is not None:
            result["values"]["advert.interval"] = interval
            if interval == 0:
                notes.append(ZERO_HOP_WARNING)
    result["notes"] = notes
    return JobOutcome(True, None, "set and confirmed", result)


def _interpret_get(kind: str, steps: list[Step], classified: list[tuple[str, str]], result: dict, base_hash: int | None) -> JobOutcome:
    items = _ALL if kind == "all" else (kind,)
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    i = 0
    for item in items:
        n = len(_GETS[item])
        chunk = classified[i : i + n]
        i += n
        if len(chunk) < n:
            errors[item] = "no_reply"
            continue
        try:
            values[item] = _item_value(item, chunk, base_hash)
        except RemoteError as e:
            errors[item] = e.code
    result["values"] = values
    if errors:
        result["errors"] = errors
    if kind != "all" and errors:
        code = errors[kind]
        return JobOutcome(False, code, ERROR_TEXT.get(code, code), result)
    if kind == "all" and not values:
        return JobOutcome(False, "no_reply", ERROR_TEXT["no_reply"], result)
    return JobOutcome(True, None, "read" if not errors else "read, some items unavailable", result)
