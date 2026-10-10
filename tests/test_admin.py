"""beaconctl against a database: the allowlist, lockout visibility and one-step reset, time, check."""

import time

import pytest

from beacon_base import admin, clock
from beacon_base.cli import main
from beacon_base.pipeline import Pipeline
from beacon_base.store import Store

from helpers import BEACON2_KEY, BEACON_KEY, REPEATER_A_KEY, REPEATER_B_KEY, obs, rx


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[clock]\nassume_synced = true\n")
    return str(p)


def run(cfg_file, *argv, capsys=None):
    code = main(["-c", cfg_file, *argv])
    if capsys is None:
        return code
    out = capsys.readouterr()
    return code, out.out, out.err


def feed(tmp_path, *reports):
    with Store.open(tmp_path / "beacon.db") as store:
        pipeline = Pipeline(store, assume_synced=True)
        for r in reports:
            pipeline.process(r)


def provision(cfg_file, capsys):
    assert run(cfg_file, "beacon", "add", "b1", BEACON_KEY[:8].hex()) == 0
    assert run(cfg_file, "beacon", "add", "b2", BEACON2_KEY.hex()) == 0  # a full key is accepted, its prefix is kept
    assert run(cfg_file, "repeater", "add", "north", REPEATER_A_KEY.hex(), "40.1", "-75.2", "--window", "20") == 0
    assert run(cfg_file, "repeater", "add", "east", REPEATER_B_KEY[:8].hex(), "40.2", "-75.1") == 0
    capsys.readouterr()


def test_beacon_and_repeater_provisioning(cfg_file, capsys):
    provision(cfg_file, capsys)
    code, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert code == 0 and "b1" in out and BEACON_KEY[:8].hex() in out
    code, out, _ = run(cfg_file, "repeater", "list", capsys=capsys)
    assert "north" in out and "20s" in out and "40.200000" in out
    assert run(cfg_file, "beacon", "disable", "b2") == 0
    assert run(cfg_file, "beacon", "remove", "b2") == 0
    assert run(cfg_file, "repeater", "window", "east", "15") == 0
    assert run(cfg_file, "repeater", "disable", "east") == 0
    assert run(cfg_file, "repeater", "remove", "east") == 0
    capsys.readouterr()
    code, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert "b1" in out and "b2" not in out


def test_provisioning_errors_exit_2(cfg_file, capsys):
    provision(cfg_file, capsys)
    code, _, err = run(cfg_file, "beacon", "add", "b3", BEACON_KEY[:8].hex(), capsys=capsys)
    assert code == 2 and "already has the prefix" in err
    code, _, err = run(cfg_file, "beacon", "add", "b3", "1234", capsys=capsys)
    assert code == 2 and "8 bytes" in err
    code, _, err = run(cfg_file, "beacon", "reset", "ghost", capsys=capsys)
    assert code == 2 and "no beacon named" in err
    code, _, err = run(cfg_file, "repeater", "add", "bad", "11" * 8, "95", "0", capsys=capsys)
    assert code == 2


def test_lockout_is_visible_in_status_and_cleared_by_one_reset(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    now = time.time()
    feed(tmp_path, rx(REPEATER_A_KEY, obs(1_000_000), t=now - 600))  # a forged high counter sets the mark
    feed(tmp_path, *[rx(REPEATER_A_KEY, obs(300 + i), t=now - 60 + i) for i in range(3)])  # the real beacon is rejected

    code, out, _ = run(cfg_file, "status", capsys=capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[1].startswith("rejected") and "b1" in lines[1]  # most urgent first
    assert "3 replays rejected" in lines[1] and "counters 300-302" in lines[1] and "hwm 1000000" in lines[1]
    assert "north" in lines[1]
    assert "beaconctl beacon reset b1" in lines[1]
    assert lines[2].startswith("silent") and "b2" in lines[2]

    code, out, _ = run(cfg_file, "rejects", capsys=capsys)
    assert out.count("replay/below_hwm") == 3 and "(hwm 1000000)" in out

    code, out, _ = run(cfg_file, "beacon", "reset", "b1", capsys=capsys)
    assert code == 0
    assert "high-water mark 1000000" in out and "3 rejects" in out and "last rejected counter 302" in out

    code, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "rejected" not in out
    feed(tmp_path, rx(REPEATER_A_KEY, obs(303), t=time.time()))
    code, out, _ = run(cfg_file, "status", capsys=capsys)
    b1 = [l for l in out.splitlines() if " b1 " in l][0]
    assert b1.startswith("ok") and "303" in b1


def test_state_survives_a_restart_between_commands(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    feed(tmp_path, rx(REPEATER_A_KEY, obs(50), t=time.time()))
    feed(tmp_path, rx(REPEATER_A_KEY, obs(10), t=time.time()))  # a separate "process" still knows the mark
    _, out, _ = run(cfg_file, "rejects", capsys=capsys)
    assert "replay/below_hwm" in out


def test_status_lists_unconfigured_beacons_with_the_add_command_and_unknown_repeaters(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    stranger = bytes(range(0xE0, 0xE8))
    rogue = bytes(range(0x30, 0x50))
    now = time.time()
    feed(tmp_path, rx(REPEATER_A_KEY, obs(7, beacon=stranger), obs(1), t=now - 10), rx(rogue, obs(9), t=now - 5))
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not on the allowlist" in out and stranger.hex() in out
    assert f"beaconctl beacon add <name> {stranger.hex()}" in out
    assert "not in the repeater table" in out and rogue[:8].hex() in out and "beaconctl repeater add" in out


def test_status_on_an_empty_database(cfg_file, capsys):
    code, out, _ = run(cfg_file, "status", capsys=capsys)
    assert code == 0 and "no beacons" in out


def test_beacon_status_details(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    now = time.time()
    feed(tmp_path, rx(REPEATER_A_KEY, obs(12, rssi=-97, snr_x4=-21, batt=3987), t=now - 30), rx(REPEATER_B_KEY, obs(12, batt=3987), t=now - 28))
    code, out, _ = run(cfg_file, "beacon", "status", "b1", capsys=capsys)
    assert code == 0
    assert "[ok]" in out and "key prefix         " + BEACON_KEY[:8].hex() in out and "high-water mark    12" in out and "3.99V" in out
    assert "north" in out and "-97 dBm" in out and "-5.25 dB" in out and "east" in out


def test_rejects_filter_and_bad_reports(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    now = time.time()

    class Bad:
        payload = b"\x09junk"
        companion_snr_x4 = 0
        path_len = 1
        rx_wall = now
        rx_mono = 1.0
        late = False

    feed(tmp_path, rx(REPEATER_A_KEY, obs(50), obs(60, beacon=BEACON2_KEY[:8]), t=now - 5))
    feed(tmp_path, rx(REPEATER_A_KEY, obs(10), obs(20, beacon=BEACON2_KEY[:8]), t=now - 4))
    with Store.open(tmp_path / "beacon.db") as store:
        Pipeline(store, assume_synced=True).record_bad_report(Bad, "unknown report version 9")
    _, out, _ = run(cfg_file, "rejects", capsys=capsys)
    assert out.count("replay") == 2 and "malformed reports" in out and "unknown report version 9" in out
    _, out, _ = run(cfg_file, "rejects", "--beacon", "b2", capsys=capsys)
    assert out.count("replay") == 1 and "malformed" not in out


def test_rejects_marks_provisional_times(tmp_path, capsys):
    cfg = tmp_path / "c2.toml"
    cfg.write_text("")  # clock not trusted
    run(str(cfg), "beacon", "add", "b1", BEACON_KEY.hex())
    run(str(cfg), "repeater", "add", "north", REPEATER_A_KEY.hex(), "1", "1")
    with Store.open(tmp_path / "beacon.db") as store:
        p = Pipeline(store)
        p.process(rx(REPEATER_A_KEY, obs(50), t=1000.0))
        p.process(rx(REPEATER_A_KEY, obs(5), t=1001.0))
    capsys.readouterr()
    _, out, _ = run(str(cfg), "rejects", capsys=capsys)
    assert "?" in out and "provisional" in out
    _, out, _ = run(str(cfg), "status", capsys=capsys)
    assert "clock NOT SET" in out


# --- time ---------------------------------------------------------------------------------------------------------------


def test_time_set_confirms_the_clock_and_corrects_provisional_times(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    calls = []
    monkeypatch.setattr(admin, "run_timedatectl", lambda *a: calls.append(a) or "")
    run(str(cfg), "beacon", "add", "b1", BEACON_KEY.hex())
    run(str(cfg), "repeater", "add", "north", REPEATER_A_KEY.hex(), "1", "1")
    mono = time.monotonic() - 50
    with Store.open(tmp_path / "beacon.db") as store:
        Pipeline(store).process(rx(REPEATER_A_KEY, obs(1), t=1000.0, mono=mono))  # stamped by a wrong clock
    capsys.readouterr()

    code, out, _ = run(str(cfg), "time", capsys=capsys)
    assert code == 0 and "NOT SET" in out and "1 observation" in out

    code, out, _ = run(str(cfg), "time", "set", "2026-10-09 12:30:00", capsys=capsys)
    assert code == 0 and "corrected the time of 1 earlier" in out
    assert [c for c in calls if c[0] == "set-time"] == [("set-time", "2026-10-09 12:30:00")]
    with Store.open(tmp_path / "beacon.db") as store:
        o = store.conn.execute("SELECT rx_time, time_trusted FROM observations").fetchone()
        assert o["time_trusted"] == 1 and o["rx_time"] == pytest.approx(mono + clock.offset(), abs=2)
        ev = store.conn.execute("SELECT kind, boot_id FROM clock_events").fetchone()
        assert (ev["kind"], ev["boot_id"]) == ("set", clock.boot_id())

    code, out, _ = run(str(cfg), "time", capsys=capsys)
    assert "set with beaconctl" in out and "0 observation" in out


def test_time_set_rejects_a_bad_value(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    monkeypatch.setattr(admin, "run_timedatectl", lambda *a: pytest.fail("must not run"))
    code, _, err = run(str(cfg), "time", "set", "tomorrow", capsys=capsys)
    assert code == 2 and "YYYY-MM-DD" in err


def test_time_set_failure_changes_nothing(tmp_path, monkeypatch, capsys):
    from beacon_base.store import StoreError

    cfg = tmp_path / "c.toml"
    cfg.write_text("")

    def fail(*a):
        raise StoreError("timedatectl failed: Automatic time synchronization is enabled")

    monkeypatch.setattr(admin, "run_timedatectl", fail)
    code, _, err = run(str(cfg), "time", "set", "2026-10-09 12:30", capsys=capsys)
    assert code == 2 and "synchronization" in err
    with Store.open(tmp_path / "beacon.db") as store:
        assert store.conn.execute("SELECT count(*) FROM clock_events").fetchone()[0] == 0


def test_time_confirm(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    monkeypatch.setattr(admin, "run_timedatectl", lambda *a: "no")
    code, out, _ = run(str(cfg), "time", "confirm", capsys=capsys)
    assert code == 0 and "confirmed" in out
    code, out, _ = run(str(cfg), "time", capsys=capsys)
    assert "set with beaconctl" in out and "ntp synchronised  no" in out


# --- check --------------------------------------------------------------------------------------------------------------


def test_check_flags_repeater_windows(cfg_file, tmp_path, capsys):
    (tmp_path / "secrets.toml").write_text('[channel]\nkey = "' + "ab" * 16 + '"\n')
    run(cfg_file, "beacon", "add", "b1", BEACON_KEY.hex())
    run(cfg_file, "repeater", "add", "good", "11" * 8, "1", "1", "--window", "60")
    run(cfg_file, "repeater", "add", "long", "22" * 8, "1", "1", "--window", "250")
    run(cfg_file, "repeater", "add", "toolong", "33" * 8, "1", "1", "--window", "280")
    run(cfg_file, "repeater", "add", "unknown", "44" * 8, "1", "1")
    capsys.readouterr()
    code, out, _ = run(cfg_file, "check", capsys=capsys)
    assert code == 1
    assert "good" not in out
    assert "long: beacon.window 250s is above the advised 240s" in out
    assert "toolong: beacon.window 280s is not below the shortest beacon interval (270s)" in out
    assert "unknown: beacon.window not recorded" in out


def test_check_passes_on_a_good_setup(cfg_file, tmp_path, capsys):
    (tmp_path / "secrets.toml").write_text('[channel]\nkey = "' + "ab" * 16 + '"\n')
    run(cfg_file, "beacon", "add", "b1", BEACON_KEY.hex())
    run(cfg_file, "repeater", "add", "r", "11" * 8, "1", "1", "--window", "60")
    capsys.readouterr()
    code, out, _ = run(cfg_file, "check", capsys=capsys)
    assert code == 0 and out.startswith("ok:")


def test_check_uses_the_configured_beacon_interval(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[beacon]\ninterval_s = 30\n")
    (tmp_path / "secrets.toml").write_text('[channel]\nkey = "' + "ab" * 16 + '"\n')
    run(str(cfg), "beacon", "add", "b1", BEACON_KEY.hex())
    run(str(cfg), "repeater", "add", "r", "11" * 8, "1", "1", "--window", "20")
    run(str(cfg), "repeater", "add", "slow", "22" * 8, "1", "1", "--window", "60")
    capsys.readouterr()
    code, out, _ = run(str(cfg), "check", capsys=capsys)
    assert code == 1 and "r: beacon.window 20s is not below the shortest beacon interval (27s)" not in out
    assert "r: beacon.window 20s is above the advised 24s" not in out
    assert "slow: beacon.window 60s is not below the shortest beacon interval (27s)" in out


def test_listed_beacons_and_repeaters_leave_the_unlisted_sections(cfg_file, tmp_path, capsys):
    rogue = bytes(range(0x30, 0x50))
    run(cfg_file, "beacon", "add", "b1", BEACON_KEY[:8].hex())
    run(cfg_file, "repeater", "add", "north", REPEATER_A_KEY.hex(), "1", "1")
    now = time.time()
    feed(tmp_path, rx(rogue, obs(5), t=now - 5), rx(REPEATER_A_KEY, obs(1, beacon=BEACON2_KEY[:8]), t=now - 4))
    capsys.readouterr()
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not in the repeater table" in out and "not on the allowlist" in out
    run(cfg_file, "repeater", "add", "new", rogue.hex(), "1", "1")
    run(cfg_file, "beacon", "add", "b2", BEACON2_KEY[:8].hex())
    capsys.readouterr()
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not in the repeater table" not in out and "not on the allowlist" not in out


# --- bulk options (--all) -------------------------------------------------------------------------------------------------


def add_three(cfg_file, capsys):
    for i, name in enumerate(("b1", "b2", "b3")):
        assert run(cfg_file, "beacon", "add", name, bytes([i + 1] * 8).hex()) == 0
    capsys.readouterr()


def test_enable_disable_all(cfg_file, capsys):
    add_three(cfg_file, capsys)
    code, out, _ = run(cfg_file, "beacon", "disable", "--all", capsys=capsys)
    assert code == 0 and "3 beacon(s) disabled" in out
    _, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert sum(l.endswith(" no") for l in out.splitlines()) == 3
    code, out, _ = run(cfg_file, "beacon", "enable", "-a", capsys=capsys)
    assert code == 0 and "3 beacon(s) enabled" in out
    _, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert sum(l.endswith(" yes") for l in out.splitlines()) == 3


def test_reset_all_reports_each_beacon(cfg_file, tmp_path, capsys):
    add_three(cfg_file, capsys)
    run(cfg_file, "repeater", "add", "north", REPEATER_A_KEY.hex(), "1", "1")
    feed(tmp_path, rx(REPEATER_A_KEY, obs(100, beacon=bytes([1] * 8)), obs(7, beacon=bytes([2] * 8)), t=time.time() - 5))
    feed(tmp_path, rx(REPEATER_A_KEY, obs(3, beacon=bytes([1] * 8)), t=time.time() - 4))  # b1 is locked out
    capsys.readouterr()
    code, out, _ = run(cfg_file, "beacon", "reset", "--all", capsys=capsys)
    assert code == 0
    assert "reset b1: was high-water mark 100, 1 rejects" in out and "reset b2: was high-water mark 7" in out
    assert "reset b3: was no high-water mark" in out and "each of these beacons" in out
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "rejected" not in out


def test_remove_all(cfg_file, capsys):
    add_three(cfg_file, capsys)
    code, out, _ = run(cfg_file, "beacon", "remove", "--all", capsys=capsys)
    assert code == 0 and "removed 3 beacon(s)" in out
    _, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert "no beacons" in out


def test_all_on_an_empty_allowlist_is_not_an_error(cfg_file, capsys):
    for verb in ("enable", "disable", "reset", "remove"):
        code, out, _ = run(cfg_file, "beacon", verb, "--all", capsys=capsys)
        assert code == 0, verb


def test_name_and_all_are_mutually_exclusive_and_one_is_required(cfg_file, capsys):
    add_three(cfg_file, capsys)
    for verb in ("enable", "disable", "reset", "remove"):
        code, _, err = run(cfg_file, "beacon", verb, "b1", "--all", capsys=capsys)
        assert code == 2 and "not both" in err, verb
        code, _, err = run(cfg_file, "beacon", verb, capsys=capsys)
        assert code == 2 and "--all" in err, verb
    _, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert out.count("b1") == 1 and "b3" in out  # nothing was touched


def test_single_beacon_forms_still_work(cfg_file, capsys):
    add_three(cfg_file, capsys)
    assert run(cfg_file, "beacon", "disable", "b2") == 0
    assert run(cfg_file, "beacon", "reset", "b1") == 0
    assert run(cfg_file, "beacon", "remove", "b3") == 0
    capsys.readouterr()
    code, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert "b3" not in out and "b1" in out


def test_add_all_adds_every_reported_beacon_that_is_not_listed(cfg_file, tmp_path, capsys):
    run(cfg_file, "repeater", "add", "north", REPEATER_A_KEY.hex(), "1", "1")
    run(cfg_file, "beacon", "add", "mine", bytes([1] * 8).hex())
    new1, new2 = bytes.fromhex("f5b165224a58b791"), bytes.fromhex("7bd5d47e446fcec2")
    feed(tmp_path, rx(REPEATER_A_KEY, obs(1, beacon=bytes([1] * 8)), obs(5, beacon=new1), obs(9, beacon=new2), t=time.time() - 5))
    capsys.readouterr()
    code, out, _ = run(cfg_file, "beacon", "add", "--all", capsys=capsys)
    assert code == 0
    assert "added beacon beacon-7bd5d4 (prefix 7bd5d47e446fcec2)" in out and "added beacon beacon-f5b165" in out
    assert "added 2 beacon(s)" in out and "mine" not in out
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not on the allowlist" not in out
    code, out, _ = run(cfg_file, "beacon", "add", "-a", capsys=capsys)
    assert code == 0 and "nothing to add" in out


def test_add_all_options_and_conflicts(cfg_file, tmp_path, capsys):
    run(cfg_file, "repeater", "add", "north", REPEATER_A_KEY.hex(), "1", "1")
    feed(tmp_path, rx(REPEATER_A_KEY, obs(5, beacon=bytes([8] * 8)), t=time.time() - 3 * 3600))
    code, out, _ = run(cfg_file, "beacon", "add", "--all", "--hours", "1", capsys=capsys)
    assert code == 0 and "nothing to add" in out  # reported 3 hours ago
    code, out, _ = run(cfg_file, "beacon", "add", "--all", "--name-prefix", "tag", capsys=capsys)
    assert code == 0 and "tag-080808" in out
    for argv in (["x", "--all"], ["x", bytes(8).hex(), "--all"], ["--all", "--notes", "n"]):
        code, _, err = run(cfg_file, "beacon", "add", *argv, capsys=capsys)
        assert code == 2 and "--all" in err
    code, _, err = run(cfg_file, "beacon", "add", capsys=capsys)
    assert code == 2 and "usage" in err
    code, _, err = run(cfg_file, "beacon", "add", "only-a-name", capsys=capsys)
    assert code == 2 and "usage" in err
