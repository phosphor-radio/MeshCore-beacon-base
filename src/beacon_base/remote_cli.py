"""``beaconctl repeater remote``: read or change a repeater over the mesh.

beaconctl never talks to the companion. It queues a job in the database, ``beacon-ingest`` (which owns the serial port) runs it, and this
prints the result (``docs/plan/repeater-remote.md``). Only the operations in ``remote.py`` exist; there is no way to send other text.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

from . import heartbeat, names, remote
from .admin import fmt_age, fmt_duration, table
from .config import Config
from .store import Store, StoreError

PASSWORD_ENV = "BEACON_REPEATER_PASSWORD"
POLL = 0.2  # seconds between looks at a job while waiting for it
FINISHED = ("done", "failed", "expired")
EXIT_OK, EXIT_FAILED, EXIT_STILL_RUNNING = 0, 1, 3

USAGE = (
    "usage: beaconctl repeater remote <repeater> get|set <item> [value ...]\n"
    "       beaconctl repeater remote <repeater> clock sync | advert | reboot [--yes]\n"
    "       beaconctl repeater remote jobs [ID] [--all]\n"
    "items: " + ", ".join(k for k in remote.KINDS if k not in remote.RUN_ONLY) + "\n"
    "  clock sync   set the repeater's clock to the base's (forward only)   advert   send a flood advert now   reboot   restart the repeater\n"
    "  set name <text>                  set location <lat> <lon>          set advert.interval <minutes|off>\n"
    "  set flood.advert.interval <hours|off>   set beacon.window <seconds>   set beacon.names on|off   set beacon.name_refresh <hours>\n"
    "  get <item> for any item, 'get all' for everything but the counters; beacon.stats and beacon.channel can only be read"
)

_DECIMAL = re.compile(r"[+-]?\d+(\.\d+)?")
_WHOLE = re.compile(r"\d+")


# --- the request ----------------------------------------------------------------------------------------------------------


def _number(text: str, what: str) -> float:
    if not _DECIMAL.fullmatch(text):
        raise StoreError(f"{what} must be a number such as 47.123456, not {text!r}")
    return float(text)


def _whole(text: str, what: str, off: bool = False) -> int:
    if off and text.lower() == "off":
        return 0
    if not _WHOLE.fullmatch(text):
        raise StoreError(f"{what} must be a whole number{' or off' if off else ''}, not {text!r}")
    return int(text)


def parse_request(item: str, op: str, values: list[str], verify: bool = True, allow_zero: bool = False) -> dict:
    """The job parameters for a command line. Everything is checked again by remote.validate when the job is queued."""
    if item not in remote.KINDS:
        raise StoreError(f"unknown item {item!r}; the items are {', '.join(k for k in remote.KINDS if k not in remote.RUN_ONLY)}")
    if op == "run":
        if values:
            raise StoreError(f"'{item}' takes no value")
        return {"verify": verify} if item == "clock" else {}
    if item in remote.RUN_ONLY:
        raise StoreError(f"{item} is an action, not something to {op}: beaconctl repeater remote <repeater> {item}")
    if op == "get":
        if values:
            raise StoreError(f"'get {item}' takes no value; did you mean 'set'?")
        return {}
    if item not in remote.SETTABLE:
        why = "the channel key is never sent over the mesh; set it on the repeater's serial console" if item == "beacon.channel" else "it can only be read"
        raise StoreError(f"{item} cannot be set over the mesh: {why}")

    def exactly(n: int, what: str) -> None:
        if len(values) != n:
            raise StoreError(f"set {item} takes {what}")

    params: dict
    if item == "name":
        if not values:
            raise StoreError("set name takes the new name")
        params = {"name": " ".join(values)}
    elif item == "location":
        exactly(2, "a latitude and a longitude")
        params = {"lat": _number(values[0], "latitude"), "lon": _number(values[1], "longitude"), "allow_zero": allow_zero}
    elif item == "advert.interval":
        exactly(1, "minutes (0 or off, or an even number 60-240)")
        params = {"minutes": _whole(values[0], "minutes", off=True)}
    elif item == "flood.advert.interval":
        exactly(1, "hours (0 or off, or 3-168)")
        params = {"hours": _whole(values[0], "hours", off=True)}
    elif item == "beacon.window":
        exactly(1, "seconds (1-3600)")
        params = {"seconds": _whole(values[0], "seconds")}
    elif item == "beacon.names":
        exactly(1, "on or off")
        if values[0] not in ("on", "off"):
            raise StoreError("beacon.names is on or off")
        params = {"on": values[0] == "on"}
    else:  # beacon.name_refresh
        exactly(1, "hours (0-8760; 0 announces only on first sight or when the name changes)")
        params = {"hours": _whole(values[0], "hours")}
    params["verify"] = verify
    return params


def read_password(args: argparse.Namespace) -> str | None:
    """The admin password if the operator gave one: from a file, the environment or a prompt, never from the command line (where
    ``ps`` shows it)."""
    password: str | None = None
    if args.password_file:
        path = Path(args.password_file).expanduser()
        try:
            mode = path.stat().st_mode
            password = path.read_text().split("\n", 1)[0].rstrip("\r")
        except OSError as e:
            raise StoreError(f"cannot read the password file: {e}") from None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f"beaconctl: warning: {path} is readable by others; chmod 600 it", file=sys.stderr)
    elif os.environ.get(PASSWORD_ENV):
        password = os.environ[PASSWORD_ENV]
    elif args.ask_password:
        password = getpass.getpass("Repeater admin password: ")
    try:
        return remote.validate_password(password)
    except remote.RemoteError as e:
        raise StoreError(str(e)) from None


# --- running it -----------------------------------------------------------------------------------------------------------


def confirm_reboot(args: argparse.Namespace, label: str) -> None:
    """A reboot loses the batch the repeater holds and takes it off the air for a while, so it is asked for, or given with --yes."""
    if args.yes:
        return
    if sys.stdin.isatty():
        answer = input(f"Reboot {label}? It stops reporting for a short while and loses the batch it holds. [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            return
        raise StoreError("not rebooted")
    raise StoreError(f"rebooting {label} restarts it and loses the batch it holds; add --yes to confirm")


def not_ready(store: Store, repeater) -> tuple[str, str] | None:
    """(code, message) when a job cannot possibly run now, so it is not queued only to expire."""
    row = store.status_row()
    state = heartbeat.state(row)
    if state != heartbeat.UP:
        seen = {heartbeat.NEVER: "has never run", heartbeat.STOPPED: "was stopped", heartbeat.DOWN: "is not running"}[state]
        return "no_ingest", f"{remote.ERROR_TEXT['no_ingest']} (beacon-ingest {seen})"
    if not row["connected"]:
        return "no_ingest", "beacon-ingest is running but the base companion is not connected"
    if repeater["pubkey"] is None:
        return "no_key", remote.ERROR_TEXT["no_key"]
    return None


def wait_for(store: Store, job_id: int, timeout: float):
    """The job row once it has finished, or as it stands after timeout seconds. A queued job nobody picked up is expired."""
    deadline = time.monotonic() + timeout
    while True:
        row = store.job(job_id)
        if row["state"] in FINISHED:
            return row
        if row["state"] == "queued" and row["expires_at"] < time.time():
            store.expire_jobs()
            continue
        if time.monotonic() >= deadline:
            return row
        time.sleep(POLL)


def job_dict(store: Store, row) -> dict:
    return {
        "id": row["id"], "state": row["state"], "repeater": bytes(row["repeater_prefix"]).hex(), "item": row["kind"], "op": row["op"],
        "params": json.loads(row["params"]), "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "error_code": row["error_code"], "error": row["error"],
        "result": json.loads(row["result"]) if row["result"] else None,
    }


def cmd_repeater_remote(args: argparse.Namespace, cfg: Config) -> int:
    words = list(args.words)
    if words[0] == "jobs":
        return cmd_jobs(args, cfg, words[1:])
    ref, verb, rest = words[0], (words[1] if len(words) > 1 else ""), words[2:]
    if verb in ("get", "set") and rest:
        op, item, values = verb, rest[0], rest[1:]
    elif verb == "clock" and rest == ["sync"]:
        op, item, values = "run", "clock", []
    elif verb in ("advert", "reboot") and not rest:
        op, item, values = "run", verb, []
    else:
        raise StoreError(USAGE)
    params = parse_request(item, op, values, verify=not args.no_verify, allow_zero=args.allow_zero)
    password = read_password(args)
    with Store.open(cfg.db_path) as store:
        repeater = store.repeater(ref)
        label = names.label(repeater["name"], repeater["prefix"])
        if item == "reboot":
            confirm_reboot(args, label)
        problem = not_ready(store, repeater)
        if problem:
            return _report_failure(args, label, problem[0], problem[1])
        job_id = store.submit_job(ref, item, op, params, password)
        ahead = store.jobs_ahead(job_id)
        if args.no_wait:
            print(f"queued job {job_id}; see 'beaconctl repeater remote jobs {job_id}'")
            return EXIT_OK
        if ahead and not args.json:
            print(f"job {job_id} is queued behind {ahead} other job(s)", file=sys.stderr)
        row = wait_for(store, job_id, args.timeout)
        if row["state"] == "failed" and row["error_code"] == "needs_password" and password is None and sys.stdin.isatty() and not args.json:
            print(f"{label} does not have this base as an admin yet.", file=sys.stderr)
            password = remote.validate_password(getpass.getpass("Repeater admin password: ") or None)
            if password:
                job_id = store.submit_job(ref, item, op, params, password)
                row = wait_for(store, job_id, args.timeout)
        return _report(args, store, cfg, label, row)


def _report_failure(args: argparse.Namespace, label: str, code: str, message: str) -> int:
    if args.json:
        print(json.dumps({"state": "failed", "error_code": code, "error": message}, indent=2))
    else:
        print(f"{label}: failed ({code}): {message}", file=sys.stderr)
    return EXIT_FAILED


def _report(args: argparse.Namespace, store: Store, cfg: Config, label: str, row) -> int:
    state = row["state"]
    if args.json:
        print(json.dumps(job_dict(store, row), indent=2))
        return {"done": EXIT_OK, "failed": EXIT_FAILED, "expired": EXIT_FAILED}.get(state, EXIT_STILL_RUNNING)
    if state in ("queued", "running"):
        print(
            f"job {row['id']} is still {state} (the repeater has not answered yet); its result will be shown by "
            f"'beaconctl repeater remote jobs {row['id']}'",
            file=sys.stderr,
        )
        return EXIT_STILL_RUNNING
    print("\n".join(render(store, cfg, label, row)), file=sys.stdout if state == "done" else sys.stderr)
    return EXIT_OK if state == "done" else EXIT_FAILED


# --- showing it -----------------------------------------------------------------------------------------------------------


def _interval(value: int, unit: str) -> str:
    return "off" if value == 0 else f"{value} {unit}"


def _stats_line(stats: dict) -> str:
    if not stats.get("parsed"):
        return stats.get("raw", "?")
    text = (
        f"heard {stats['heard']}, reported {stats['reported']}, dropped {stats['dropped']}, send fail {stats['send_fail']}, "
        f"pending {stats['pending']}, names sent {stats['names_sent']}"
    )
    return text + (f", up {fmt_duration(stats['up_s'])}" if stats.get("up_s") is not None else "")


def stats_change(now: dict, before: dict, gap: float) -> str:
    """What the counters did since the previous reading, or that the repeater restarted."""
    keys = ("heard", "reported", "dropped", "send_fail", "names_sent")
    rebooted = (
        now.get("up_s") is not None and before.get("up_s") is not None and now["up_s"] < before["up_s"]
    ) or any(now[k] < before[k] for k in keys)
    if rebooted:
        return "the repeater restarted since the previous reading, so the counters start again"
    labels = {"send_fail": "send fail", "names_sent": "names sent"}
    return f"since the previous reading {fmt_duration(gap)} earlier: " + ", ".join(f"{labels.get(k, k)} +{now[k] - before[k]}" for k in keys)


def _clock_offset_text(offset: int | None) -> str:
    """How a repeater's clock compares with the base's. The repeater reports whole minutes, so anything within 90 s is in step."""
    if offset is None:
        return "not compared with the base"
    if abs(offset) <= remote.CLOCK_TOLERANCE_S:
        return "in step with the base, to the minute"
    return f"{abs(offset)} s {'behind' if offset > 0 else 'ahead of'} the base"


def _channel_text(channel: dict, base_hash: int | None) -> str:
    if not channel["set"]:
        return "not set: this repeater sends no reports"
    text = f"set, hash {channel['hash']:02X}"
    if base_hash is None:
        return text
    return text + (" (the same as this base's channel)" if channel["hash"] == base_hash else f" (DIFFERENT from this base's channel, hash {base_hash:02X})")


def _value_lines(store: Store, cfg: Config, row, values: dict, errors: dict) -> list[str]:
    base_hash = remote.channel_hash(cfg.channel_key) if cfg.channel_key else None
    out = []
    for item in [i for i in remote.KINDS if i != "all"]:
        if item in errors:
            out.append(f"  {item:<22}unavailable ({errors[item]}: {remote.ERROR_TEXT.get(errors[item], errors[item])})")
            continue
        if item not in values:
            continue
        v = values[item]
        if item == "location":
            text = f"{v['lat']}, {v['lon']}  (as the repeater prints it, good to about 1 m)"
        elif item == "advert.interval":
            text = _interval(v, "minutes") + (" (zero-hop adverts off)" if v == 0 else "")
        elif item == "flood.advert.interval":
            text = _interval(v, "hours")
        elif item == "beacon.window":
            text = f"{v} s"
        elif item == "beacon.names":
            text = "on" if v else "off"
        elif item == "beacon.name_refresh":
            text = "0 (announced on first sight or when the name changes)" if v == 0 else f"{v} hours"
        elif item == "beacon.channel":
            text = _channel_text(v, base_hash)
        elif item == "beacon.stats":
            text = _stats_line(v)
        elif item == "clock":
            text = f"{v['text']}  ({_clock_offset_text(v['offset_s'])})"
        else:
            text = str(v)
        out.append(f"  {item:<22}{text}")
        if item == "beacon.stats" and v.get("parsed"):
            before = store.previous_stats(bytes(row["repeater_prefix"]), row["id"])
            if before is not None and before[0].get("parsed"):
                out.append(f"  {'':<22}{stats_change(v, before[0], row['finished_at'] - before[1])}")
    return out


def render(store: Store, cfg: Config, label: str, row) -> list[str]:
    """The lines to print for a finished job."""
    result = json.loads(row["result"]) if row["result"] else {}
    item, op = row["kind"], row["op"]
    if row["state"] == "expired":
        return [f"{label}: job {row['id']} expired: ingest did not pick it up in time (is beacon-ingest running?)"]
    if row["state"] == "failed":
        lines = [f"{label}: {op} {item} failed ({row['error_code']}): {row['error']}"]
        if result.get("replies"):
            lines.append("  replies so far: " + "; ".join(f"{r['command']!r} -> {r['reply']!r}" for r in result["replies"]))
        return lines
    values, errors = result.get("values", {}), result.get("errors", {})
    lines = []
    if op == "run":
        lines += _run_lines(label, item, bytes(row["repeater_prefix"]).hex(), result)
    elif op == "get":
        lines.append(f"{label}:")
        lines += _value_lines(store, cfg, row, values, errors)
        if errors and item != "all":
            lines.append(f"  ({errors[item]})")
    else:
        what = _set_phrase(item, result.get("sent"))
        if result.get("verified"):
            lines.append(f"{label}: {what} set and confirmed by reading it back")
            if item == "location":
                got = values["location"]
                lines.append(f"  the repeater now prints {got['lat']}, {got['lon']} (a lossy read-back, about 1 m; the position recorded is the one sent)")
        else:
            lines.append(f"{label}: {what} set (not read back)")
        extra = values.get("advert.interval")
        if extra is not None and item != "advert.interval":
            lines.append(f"  advert.interval is {_interval(extra, 'minutes')}")
        for note in result.get("notes", []):
            lines.append(f"  note: {note}")
    recorded = result.get("recorded")
    if recorded:
        lines.append(f"  recorded at the base: {', '.join(recorded)}")
    elif op == "set" and not result.get("verified"):
        lines.append("  not recorded at the base (it was not read back)")
    flooded = " (flooded)" if result.get("flooded") else ""
    lines.append(f"  {result.get('round_trips', 0)} round trip(s) in {result.get('seconds', 0):g} s{flooded}")
    return lines


def _run_lines(label: str, item: str, prefix: str, result: dict) -> list[str]:
    if item == "advert":
        return [f"{label}: flood advert sent (the repeater sends it about 1.5 s after replying; the base learns a changed name or position from it)"]
    if item == "reboot":
        return [
            f"{label}: reboot command sent. The repeater cannot answer it (it restarts first), so this is not confirmed; it takes a short "
            f"while to come back. Check with: beaconctl repeater remote {prefix} get name"
        ]
    clock = result.get("values", {}).get("clock")
    if clock is None:
        return [f"{label}: clock set (not read back)"]
    how = "set" if result.get("set") else "left alone: it was already in step and cannot be set back"
    return [f"{label}: clock {how}; the repeater now reads {clock['text']} ({_clock_offset_text(clock['offset_s'])})"]


def _set_phrase(item: str, sent) -> str:
    if item == "name":
        return f"name {sent!r}"
    if item == "location":
        return f"position {sent['lat']}, {sent['lon']}"
    if item == "advert.interval":
        return f"advert.interval {_interval(sent, 'minutes')}"
    if item == "beacon.window":
        return f"beacon.window {sent} s"
    if item == "beacon.names":
        return f"beacon.names {'on' if sent else 'off'}"
    return f"{item} {_interval(sent, 'hours')}"


# --- the job list ---------------------------------------------------------------------------------------------------------


def cmd_jobs(args: argparse.Namespace, cfg: Config, rest: list[str]) -> int:
    with Store.open(cfg.db_path) as store:
        if rest:
            if len(rest) != 1 or not _WHOLE.fullmatch(rest[0]):
                raise StoreError("usage: beaconctl repeater remote jobs [ID] [--all]")
            return _show_job(args, store, cfg, int(rest[0]))
        rows = store.jobs(limit=200 if args.all else args.limit)
        if args.json:
            print(json.dumps([job_dict(store, r) for r in rows], indent=2))
            return EXIT_OK
        _, repeaters = store.names()
        now = time.time()
        out = []
        for r in rows:
            result = json.loads(r["result"]) if r["result"] else {}
            if r["state"] == "done":
                summary = "ok" + (f", recorded {', '.join(result['recorded'])}" if result.get("recorded") else "")
            elif r["state"] in ("failed", "expired"):
                summary = r["error_code"] or r["state"]
            else:
                summary = "waiting" if r["state"] == "queued" else "in progress"
            out.append(
                [
                    str(r["id"]), r["state"], fmt_age(r["created_at"], now),
                    names.label(repeaters.get(bytes(r["repeater_prefix"])), r["repeater_prefix"]), f"{r['op']} {r['kind']}", summary,
                ]
            )
    print(table(out, ["ID", "STATE", "AGE", "REPEATER", "WHAT", "RESULT"]) if out else "no remote jobs yet")
    return EXIT_OK


def _show_job(args: argparse.Namespace, store: Store, cfg: Config, job_id: int) -> int:
    row = store.job(job_id)
    if row is None:
        raise StoreError(f"no remote job {job_id}")
    if args.json:
        print(json.dumps(job_dict(store, row), indent=2))
        return EXIT_OK
    _, repeaters = store.names()
    label = names.label(repeaters.get(bytes(row["repeater_prefix"])), row["repeater_prefix"])
    if row["state"] in ("queued", "running"):
        print(f"job {job_id}: {row['op']} {row['kind']} on {label} is {row['state']}, queued {fmt_age(row['created_at'], time.time())}")
        return EXIT_STILL_RUNNING
    lines = render(store, cfg, label, row)
    result = json.loads(row["result"]) if row["result"] else {}
    if result.get("replies"):
        lines.append("  commands: " + "; ".join(f"{r['command']} -> {r['reply']}" for r in result["replies"]))
    print("\n".join(lines))
    return EXIT_OK if row["state"] == "done" else EXIT_FAILED
