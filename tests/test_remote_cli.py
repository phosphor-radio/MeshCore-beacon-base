"""beaconctl repeater remote: parsing, and the whole command against a live session, fake companion and fake repeater."""

import json
import os
import time

import pytest

from beacon_base import executor, heartbeat, remote, remote_cli
from beacon_base.cli import main
from beacon_base.heartbeat import HeartbeatWriter
from beacon_base.store import Store, StoreError

from test_remote_session import KEY, RKEY, build

REF = RKEY[:8].hex()


# --- parsing --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item, op, values, expected",
    [
        ("name", "get", [], {}),
        ("name", "set", ["hill", "top", "2"], {"name": "hill top 2", "verify": True}),  # a name may have spaces
        ("location", "set", ["47.5", "-122.25"], {"lat": 47.5, "lon": -122.25, "allow_zero": False, "verify": True}),
        ("location", "set", ["+1", "2.0"], {"lat": 1.0, "lon": 2.0, "allow_zero": False, "verify": True}),
        ("advert.interval", "set", ["off"], {"minutes": 0, "verify": True}),
        ("advert.interval", "set", ["120"], {"minutes": 120, "verify": True}),
        ("flood.advert.interval", "set", ["OFF"], {"hours": 0, "verify": True}),
        ("flood.advert.interval", "set", ["48"], {"hours": 48, "verify": True}),
        ("beacon.window", "set", ["5"], {"seconds": 5, "verify": True}),
        ("beacon.names", "set", ["off"], {"on": False, "verify": True}),
        ("beacon.name_refresh", "set", ["0"], {"hours": 0, "verify": True}),
        ("all", "get", [], {}),
    ],
)
def test_parse_request(item, op, values, expected):
    assert remote_cli.parse_request(item, op, values) == expected


def test_flags_reach_the_parameters():
    assert remote_cli.parse_request("name", "set", ["x"], verify=False)["verify"] is False
    assert remote_cli.parse_request("location", "set", ["0", "0"], allow_zero=True)["allow_zero"] is True


@pytest.mark.parametrize(
    "item, op, values, message",
    [
        ("password", "get", [], "unknown item"),
        ("name", "get", ["x"], "takes no value"),
        ("beacon.channel", "set", ["00" * 16], "serial console"),
        ("beacon.stats", "set", [], "can only be read"),
        ("all", "set", [], "can only be read"),
        ("name", "set", [], "takes the new name"),
        ("location", "set", ["47.5"], "latitude and a longitude"),
        ("location", "set", ["47.5", "nan"], "must be a number"),
        ("location", "set", ["1_0", "2"], "must be a number"),
        ("location", "set", ["1e2", "2"], "must be a number"),
        ("location", "set", ["47,5", "2"], "must be a number"),
        ("advert.interval", "set", ["1.5"], "whole number"),
        ("advert.interval", "set", ["-60"], "whole number"),
        ("beacon.window", "set", ["5", "6"], "takes seconds"),
        ("beacon.names", "set", ["yes"], "on or off"),
        ("beacon.name_refresh", "set", ["off"], "whole number"),
    ],
)
def test_bad_requests_are_refused_with_a_reason(item, op, values, message):
    with pytest.raises(StoreError, match=message):
        remote_cli.parse_request(item, op, values)


def test_values_that_parse_but_are_out_of_range_are_refused_when_queued(tmp_path):
    with Store.open(tmp_path / "beacon.db") as s:
        s.add_repeater(RKEY.hex(), 1.0, 2.0, name="ridge")
        for item, values in (("advert.interval", ["61"]), ("beacon.window", ["0"]), ("flood.advert.interval", ["2"]), ("location", ["91", "0"])):
            params = remote_cli.parse_request(item, "set", values)
            with pytest.raises(StoreError):
                s.submit_job("ridge", item, "set", params)


# --- the command against a live session -----------------------------------------------------------------------------------


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "CLAIM_INTERVAL", 0.05)
    monkeypatch.setattr(remote_cli, "POLL", 0.02)
    monkeypatch.delenv(remote_cli.PASSWORD_ENV, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[clock]\nassume_synced = true\n")
    secrets = tmp_path / "secrets.toml"
    secrets.write_text(f'[channel]\nkey = "{KEY.hex()}"\n')
    os.chmod(secrets, 0o600)
    e, (stop, thread, server) = build(tmp_path)
    e.cfg = str(cfg)
    yield e
    stop.set()
    thread.join(timeout=5)
    e.store.close()
    server.close()
    e.fake.close()


def ctl(stack, capsys, *argv):
    code = main(["-c", stack.cfg, *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def remote_ctl(stack, capsys, *argv):
    return ctl(stack, capsys, "repeater", "remote", REF, *argv)


def password_file(tmp_path, text="password\n", mode=0o600):
    p = tmp_path / "pw"
    p.write_text(text)
    os.chmod(p, mode)
    return str(p)


def test_the_first_contact_needs_the_password_and_says_so(stack, capsys):
    code, out, err = remote_ctl(stack, capsys, "get", "name")
    assert code == 1 and out == "" and "failed (needs_password)" in err and "--password-file" in err


def test_get_with_a_password_file_then_without_one(stack, capsys, tmp_path):
    code, out, err = remote_ctl(stack, capsys, "get", "name", "--password-file", password_file(tmp_path))
    assert code == 0 and "ridge" in out and "name" in out and "round trip" in out and err == ""
    code, out, _ = remote_ctl(stack, capsys, "get", "beacon.window")
    assert code == 0 and "60 s" in out


def test_the_password_can_come_from_the_environment_or_a_prompt(stack, capsys, monkeypatch):
    monkeypatch.setenv(remote_cli.PASSWORD_ENV, "password")
    assert remote_ctl(stack, capsys, "get", "name")[0] == 0
    monkeypatch.delenv(remote_cli.PASSWORD_ENV)
    monkeypatch.setattr(remote_cli.getpass, "getpass", lambda prompt="": "password")
    code, out, _ = remote_ctl(stack, capsys, "get", "name", "--ask-password")
    assert code == 0 and "ridge" in out


def test_an_interactive_run_asks_when_the_repeater_wants_the_password(stack, capsys, monkeypatch):
    monkeypatch.setattr(remote_cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(remote_cli.getpass, "getpass", lambda prompt="": "password")
    code, out, err = remote_ctl(stack, capsys, "get", "name")
    assert code == 0 and "ridge" in out and "does not have this base as an admin" in err


def test_a_wrong_password_is_a_no_reply(stack, capsys, tmp_path):
    code, _, err = remote_ctl(stack, capsys, "get", "name", "--password-file", password_file(tmp_path, "wrong"))
    assert code == 1 and "failed (no_reply)" in err and "wrong admin password" in err


def test_a_password_file_that_others_can_read_is_warned_about(stack, capsys, tmp_path):
    code, _, err = remote_ctl(stack, capsys, "get", "name", "--password-file", password_file(tmp_path, mode=0o644))
    assert code == 0 and "readable by others" in err


def test_bad_passwords_and_files_are_operator_errors(stack, capsys, tmp_path):
    code, _, err = remote_ctl(stack, capsys, "get", "name", "--password-file", password_file(tmp_path, "x" * 16))
    assert code == 2 and "at most 15" in err
    code, _, err = remote_ctl(stack, capsys, "get", "name", "--password-file", str(tmp_path / "missing"))
    assert code == 2 and "cannot read the password file" in err


def test_set_name_confirms_records_and_lists(stack, capsys, tmp_path):
    code, out, _ = remote_ctl(stack, capsys, "set", "name", "hill", "top", "--password-file", password_file(tmp_path))
    assert code == 0 and "'hill top' set and confirmed by reading it back" in out and "recorded at the base: name" in out
    assert "zero-hop adverts are off" in out and "advert.interval is off" in out
    assert stack.repeater.name == "hill top"
    _, out, _ = ctl(stack, capsys, "repeater", "list")
    assert "hill top" in out


def test_set_location_is_recorded_as_remote_and_the_lossy_read_back_is_explained(stack, capsys, tmp_path):
    code, out, _ = remote_ctl(stack, capsys, "set", "location", "47.123456", "-122.335167", "--password-file", password_file(tmp_path))
    assert code == 0 and "position 47.123456, -122.335167 set and confirmed" in out and "lossy read-back" in out and "recorded at the base: position" in out
    _, out, _ = ctl(stack, capsys, "repeater", "list")
    assert "47.123456" in out and "remote" in out


def test_set_without_verify_says_it_was_not_read_back_or_recorded(stack, capsys, tmp_path):
    code, out, _ = remote_ctl(stack, capsys, "set", "beacon.window", "9", "--no-verify", "--password-file", password_file(tmp_path))
    assert code == 0 and "set (not read back)" in out and "not recorded at the base" in out
    assert stack.repeater.window == 9 and stack.store.repeater(REF)["window_s"] is None


def test_set_beacon_window_updates_what_check_uses(stack, capsys, tmp_path):
    code, out, _ = remote_ctl(stack, capsys, "set", "beacon.window", "20", "--password-file", password_file(tmp_path))
    assert code == 0 and "recorded at the base: window" in out
    assert stack.store.repeater(REF)["window_s"] == 20.0


def test_a_refused_value_fails_before_anything_is_sent(stack, capsys):
    code, _, err = remote_ctl(stack, capsys, "set", "advert.interval", "61")
    assert code == 2 and "even number of minutes" in err
    assert stack.store.jobs() == []


def test_the_channel_key_cannot_be_set_over_the_mesh_by_any_route(stack, capsys):
    for argv in (["set", "beacon.channel", "00" * 16], ["set", "beacon.channel", "clear"], ["set", "all"]):
        code, _, err = remote_ctl(stack, capsys, *argv)
        assert code == 2, argv
    assert stack.store.jobs() == [] and "beacon.channel" not in " ".join(stack.repeater.commands)


def test_get_all_lists_everything_and_flags_what_the_firmware_lacks(stack, capsys, tmp_path):
    stack.repeater.names_supported = False
    stack.repeater.channel_secret = KEY
    code, out, _ = remote_ctl(stack, capsys, "get", "all", "--password-file", password_file(tmp_path))
    assert code == 0
    assert "name" in out and "ridge" in out and "location" in out and "47.5" not in out  # the position is the repeater's own (0, 0 here)
    assert "beacon.names" in out and "unavailable (unsupported:" in out
    assert "set, hash" in out and "the same as this base's channel" in out
    assert "advert.interval" in out and "2 minutes" in out and "flood.advert.interval" in out and "47 hours" in out


def test_the_channel_check_in_output_and_in_beaconctl_check(stack, capsys, tmp_path):
    stack.repeater.channel_secret = bytes(range(100, 116))
    code, out, _ = remote_ctl(stack, capsys, "get", "beacon.channel", "--password-file", password_file(tmp_path))
    assert code == 0 and "DIFFERENT from this base's channel" in out
    _, out, _ = ctl(stack, capsys, "check")
    assert "its report channel (hash" in out and "is not this base's" in out
    stack.repeater.channel_secret = None
    remote_ctl(stack, capsys, "get", "beacon.channel")
    _, out, _ = ctl(stack, capsys, "check")
    assert "no report channel set" in out and "is not this base's" not in out
    stack.repeater.channel_secret = KEY
    remote_ctl(stack, capsys, "get", "beacon.channel")
    _, out, _ = ctl(stack, capsys, "check")
    assert "report channel" not in out


def test_stats_show_what_changed_since_the_previous_reading(stack, capsys, tmp_path):
    stack.repeater.uptime_in_stats = True
    stack.repeater._booted = time.monotonic() - 500  # up for a while
    stack.repeater.stats.update(heard=10, reported=8)
    remote_ctl(stack, capsys, "get", "beacon.stats", "--password-file", password_file(tmp_path))
    stack.repeater.stats.update(heard=25, reported=20, names_sent=3)
    code, out, _ = remote_ctl(stack, capsys, "get", "beacon.stats")
    assert code == 0 and "heard 25, reported 20" in out and "up " in out
    assert "since the previous reading" in out and "heard +15" in out and "reported +12" in out and "names sent +3" in out
    stack.repeater.stats.update(heard=2, reported=1, names_sent=0)
    stack.repeater.reboot()  # restarted: the uptime starts again
    _, out, _ = remote_ctl(stack, capsys, "get", "beacon.stats")
    assert "restarted since the previous reading" in out


def test_json_output(stack, capsys, tmp_path):
    code, out, _ = remote_ctl(stack, capsys, "get", "name", "--json", "--password-file", password_file(tmp_path))
    data = json.loads(out)
    assert code == 0 and data["state"] == "done" and data["item"] == "name" and data["result"]["values"] == {"name": "ridge"}
    code, out, _ = remote_ctl(stack, capsys, "get", "beacon.window", "--json")
    assert json.loads(out)["result"]["values"] == {"beacon.window": 60}
    stack.repeater.reachable = False
    code, out, _ = remote_ctl(stack, capsys, "get", "name", "--json", "--password-file", password_file(tmp_path))
    data = json.loads(out)
    assert code == 1 and data["state"] == "failed" and data["error_code"] == "no_reply"


def test_no_wait_and_the_job_list(stack, capsys, tmp_path):
    code, out, _ = remote_ctl(stack, capsys, "get", "name", "--no-wait", "--password-file", password_file(tmp_path))
    assert code == 0 and out.startswith("queued job 1")
    stack.wait(1)
    code, out, _ = ctl(stack, capsys, "repeater", "remote", "jobs")
    assert code == 0 and "ID" in out and "done" in out and "get name" in out and "ok, recorded" not in out
    code, out, _ = ctl(stack, capsys, "repeater", "remote", "jobs", "1")
    assert code == 0 and "ridge" in out and "commands: get name -> > ridge" in out.replace("7f|", "")
    code, out, err = ctl(stack, capsys, "repeater", "remote", "jobs", "99")
    assert code == 2 and "no remote job 99" in err
    code, out, _ = ctl(stack, capsys, "repeater", "remote", "jobs", "--json")
    assert [j["id"] for j in json.loads(out)] == [1]


def test_a_slow_repeater_leaves_the_job_running_and_the_exit_code_says_so(stack, capsys, tmp_path):
    stack.repeater.reachable = False
    code, out, err = remote_ctl(stack, capsys, "get", "name", "--timeout", "0.3", "--password-file", password_file(tmp_path))
    assert code == remote_cli.EXIT_STILL_RUNNING and "still" in err and "jobs 1" in err
    stack.wait(1)
    code, out, _ = ctl(stack, capsys, "repeater", "remote", "jobs")
    assert "no_reply" in out


def test_a_job_that_fails_shows_in_the_list_and_the_job_detail(stack, capsys):
    remote_ctl(stack, capsys, "get", "name")  # needs_password
    _, out, _ = ctl(stack, capsys, "repeater", "remote", "jobs")
    assert "failed" in out and "needs_password" in out
    code, out, _ = ctl(stack, capsys, "repeater", "remote", "jobs", "1")
    assert code == 1 and "failed (needs_password)" in out


def test_jobs_queue_behind_each_other_and_say_so(stack, capsys, tmp_path):
    stack.repeater.reachable = False
    first = stack.submit("name", password="password")
    code, out, err = remote_ctl(stack, capsys, "get", "beacon.window", "--timeout", "0.2")
    assert "queued behind 1 other job" in err
    stack.wait(first)


def test_the_password_does_not_stay_in_the_database_files(stack, capsys, tmp_path):
    secret = "Zq7-unmistaken"
    assert remote_ctl(stack, capsys, "get", "name", "--password-file", password_file(tmp_path, secret))[0] == 1  # wrong, but it was sent
    stack.store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    for f in tmp_path.iterdir():
        if f.name.startswith("beacon.db"):
            assert secret.encode() not in f.read_bytes(), f.name


def test_unknown_or_ambiguous_repeaters_are_operator_errors(stack, capsys):
    code, _, err = ctl(stack, capsys, "repeater", "remote", "nobody", "get", "name")
    assert code == 2 and "no repeater matches" in err
    code, _, err = ctl(stack, capsys, "repeater", "remote", REF)
    assert code == 2 and "usage: beaconctl repeater remote" in err
    code, _, err = ctl(stack, capsys, "repeater", "remote", REF, "reboot", "now")
    assert code == 2 and "usage:" in err


# --- when it cannot run ---------------------------------------------------------------------------------------------------


@pytest.fixture
def quiet(tmp_path, monkeypatch):
    """A database and a config with no ingest running."""
    monkeypatch.delenv(remote_cli.PASSWORD_ENV, raising=False)
    monkeypatch.setattr(remote_cli, "POLL", 0.02)
    cfg = tmp_path / "config.toml"
    cfg.write_text("[clock]\nassume_synced = true\n")
    with Store.open(tmp_path / "beacon.db") as s:
        s.add_repeater(RKEY.hex(), 1.0, 2.0, name="ridge")
        s.add_repeater(bytes(range(60, 68)).hex(), 3.0, 4.0, name="keyless")  # added by prefix only
    return str(cfg), tmp_path


def run_quiet(cfg, capsys, *argv):
    code = main(["-c", cfg, *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_nothing_is_queued_when_ingest_is_not_running(quiet, capsys):
    cfg, tmp = quiet
    code, _, err = run_quiet(cfg, capsys, "repeater", "remote", "ridge", "get", "name")
    assert code == 1 and "failed (no_ingest)" in err and "has never run" in err
    with Store.open(tmp / "beacon.db") as s:
        assert s.jobs() == []
        w = HeartbeatWriter(s)
        w.set(connected=1)
        w.write()
        w.stop()
    code, _, err = run_quiet(cfg, capsys, "repeater", "remote", "ridge", "get", "name")
    assert code == 1 and "was stopped" in err
    with Store.open(tmp / "beacon.db") as s:
        HeartbeatWriter(s, boot="an-older-boot").write()
    code, _, err = run_quiet(cfg, capsys, "repeater", "remote", "ridge", "get", "name")
    assert code == 1 and "is not running" in err


def test_nothing_is_queued_when_the_companion_is_not_connected(quiet, capsys):
    cfg, tmp = quiet
    with Store.open(tmp / "beacon.db") as s:
        HeartbeatWriter(s).write()
    code, _, err = run_quiet(cfg, capsys, "repeater", "remote", "ridge", "get", "name")
    assert code == 1 and "companion is not connected" in err


def test_a_repeater_without_a_known_key_is_reported_at_once(quiet, capsys):
    cfg, tmp = quiet
    with Store.open(tmp / "beacon.db") as s:
        w = HeartbeatWriter(s)
        w.set(connected=1)
        w.write()
    code, _, err = run_quiet(cfg, capsys, "repeater", "remote", "keyless", "get", "name")
    assert code == 1 and "failed (no_key)" in err and "heard advertising" in err
    with Store.open(tmp / "beacon.db") as s:
        assert s.jobs() == []


def test_a_job_nobody_picks_up_is_expired_while_the_command_waits(quiet, capsys):
    cfg, tmp = quiet
    with Store.open(tmp / "beacon.db") as s:
        w = HeartbeatWriter(s)
        w.set(connected=1)
        w.write()
        job_id = s.submit_job("ridge", "name", "get", ttl=0.1)
        row = remote_cli.wait_for(s, job_id, timeout=5)
        assert row["state"] == "expired" and row["error_code"] == "expired"
        lines = remote_cli.render(s, None, "ridge", row)
        assert "expired" in lines[0] and "beacon-ingest running" in lines[0]
