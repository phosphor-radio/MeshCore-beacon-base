"""beaconctl against a database: the allowlist by prefix, lockout visibility and one-step reset, time, check."""

import time

import pytest

from beacon_base import admin, clock
from beacon_base.cli import main
from beacon_base.pipeline import Pipeline
from beacon_base.store import Store

from helpers import B1, B2, BEACON2_KEY, BEACON2_PREFIX, BEACON_KEY, BEACON_PREFIX, REPEATER_A_KEY, REPEATER_B_KEY, obs, rx


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


def announce(tmp_path, prefix, name, repeater=REPEATER_A_KEY):
    """What a repeater's name announcement leaves in the database."""
    with Store.open(tmp_path / "beacon.db") as store, store.transaction():
        store.record_name(prefix, name, repeater[:8])


def provision(cfg_file, capsys):
    assert run(cfg_file, "beacon", "add", B1) == 0
    assert run(cfg_file, "beacon", "add", BEACON2_KEY.hex()) == 0  # a full key is accepted, its prefix is kept
    assert run(cfg_file, "repeater", "add", REPEATER_A_KEY.hex(), "40.1", "-75.2", "--name", "north", "--window", "20") == 0
    assert run(cfg_file, "repeater", "add", REPEATER_B_KEY[:8].hex(), "40.2", "-75.1") == 0  # no name
    capsys.readouterr()


# --- provisioning ---------------------------------------------------------------------------------------------------------


def test_beacon_and_repeater_provisioning(cfg_file, tmp_path, capsys):
    announce(tmp_path, BEACON_PREFIX, "Roof")
    assert run(cfg_file, "beacon", "add", B1) == 0
    assert run(cfg_file, "repeater", "add", REPEATER_A_KEY.hex(), "40.1", "-75.2", "--name", "north", "--window", "20") == 0
    assert run(cfg_file, "repeater", "add", REPEATER_B_KEY[:8].hex(), "40.2", "-75.1") == 0
    capsys.readouterr()
    _, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    row = out.splitlines()[1].split()
    assert row[:3] == ["Roof", B1, "yes"]
    _, out, _ = run(cfg_file, "repeater", "list", capsys=capsys)
    assert "north" in out and "20s" in out and "40.200000" in out
    lines = out.splitlines()
    assert lines[1].split()[0] == "north" and lines[2].split()[0] == "-"  # named first, then the unnamed one
    assert run(cfg_file, "beacon", "disable", B1) == 0
    assert run(cfg_file, "repeater", "window", "east", "15") != 0  # no repeater has that name
    assert run(cfg_file, "repeater", "window", REPEATER_B_KEY[:3].hex() + REPEATER_B_KEY[3:4].hex(), "15") == 0
    assert run(cfg_file, "repeater", "disable", "north") == 0
    assert run(cfg_file, "repeater", "remove", "north") == 0
    assert run(cfg_file, "beacon", "remove", B1[:8]) == 0
    capsys.readouterr()
    _, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert "no beacons" in out


def test_add_prints_the_announced_name_or_says_there_is_none(cfg_file, tmp_path, capsys):
    announce(tmp_path, BEACON_PREFIX, "Roof")
    _, out, _ = run(cfg_file, "beacon", "add", B1, capsys=capsys)
    assert f"added beacon {B1} (Roof)" in out
    _, out, _ = run(cfg_file, "beacon", "add", B2, capsys=capsys)
    assert "no name announced yet" in out


def test_provisioning_errors_exit_2(cfg_file, capsys):
    provision(cfg_file, capsys)
    code, _, err = run(cfg_file, "beacon", "add", B1, capsys=capsys)
    assert code == 2 and "already on the allowlist" in err
    code, _, err = run(cfg_file, "beacon", "add", "1234", capsys=capsys)
    assert code == 2 and "8 bytes" in err
    code, _, err = run(cfg_file, "beacon", "reset", "aabbccdd", capsys=capsys)
    assert code == 2 and "no beacon on the allowlist matches" in err
    code, _, err = run(cfg_file, "beacon", "reset", "abc", capsys=capsys)
    assert code == 2 and "at least 6" in err
    code, _, err = run(cfg_file, "beacon", "reset", "Roof", capsys=capsys)
    assert code == 2 and "not a hex" in err
    code, _, err = run(cfg_file, "repeater", "add", "11" * 8, "95", "0", capsys=capsys)
    assert code == 2
    code, _, err = run(cfg_file, "repeater", "disable", "nobody", capsys=capsys)
    assert code == 2 and "no repeater matches" in err


def test_beacons_are_addressed_by_an_abbreviated_prefix(cfg_file, capsys):
    provision(cfg_file, capsys)
    assert run(cfg_file, "beacon", "disable", B1[:6]) == 0
    assert run(cfg_file, "beacon", "enable", B1[:12]) == 0
    assert run(cfg_file, "beacon", "enable", B1 + "ff" * 24) == 0  # a full key is read as its prefix
    code, out, _ = run(cfg_file, "beacon", "status", B1[:7], capsys=capsys)
    assert code == 0 and B1 in out


# --- lockout ------------------------------------------------------------------------------------------------------------


def test_lockout_is_visible_in_status_and_cleared_by_one_reset(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    announce(tmp_path, BEACON_PREFIX, "Roof")
    now = time.time()
    feed(tmp_path, rx(REPEATER_A_KEY, obs(1_000_000), t=now - 600))  # a forged high counter sets the mark
    feed(tmp_path, *[rx(REPEATER_A_KEY, obs(300 + i), t=now - 60 + i) for i in range(3)])  # the real beacon is rejected

    code, out, _ = run(cfg_file, "status", capsys=capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[1].startswith("rejected") and "Roof" in lines[1] and B1 in lines[1]  # most urgent first
    assert "3 replays rejected" in lines[1] and "counters 300-302" in lines[1] and "hwm 1000000" in lines[1]
    assert "north" in lines[1]
    assert f"beaconctl beacon reset {B1}" in lines[1]  # a prefix that can be pasted as it is
    assert lines[2].startswith("silent") and B2 in lines[2]

    code, out, _ = run(cfg_file, "rejects", capsys=capsys)
    assert out.count("replay/below_hwm") == 3 and "(hwm 1000000)" in out and "Roof (" + B1[:6] + ")" in out

    code, out, _ = run(cfg_file, "beacon", "reset", B1, capsys=capsys)
    assert code == 0
    assert "high-water mark 1000000" in out and "3 rejects" in out and "last rejected counter 302" in out and "Roof" in out

    code, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "rejected" not in out
    feed(tmp_path, rx(REPEATER_A_KEY, obs(303), t=time.time()))
    code, out, _ = run(cfg_file, "status", capsys=capsys)
    b1 = [l for l in out.splitlines() if B1 in l][0]
    assert b1.startswith("ok") and "Roof" in b1 and "303" in b1


def test_state_survives_a_restart_between_commands(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    feed(tmp_path, rx(REPEATER_A_KEY, obs(50), t=time.time()))
    feed(tmp_path, rx(REPEATER_A_KEY, obs(10), t=time.time()))  # a separate "process" still knows the mark
    _, out, _ = run(cfg_file, "rejects", capsys=capsys)
    assert "replay/below_hwm" in out


def test_status_lists_unlisted_beacons_with_their_name_and_unknown_repeaters(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    stranger = bytes(range(0xE0, 0xE8))
    nameless = bytes(range(0xD0, 0xD8))
    rogue = bytes(range(0x30, 0x50))
    announce(tmp_path, stranger, "Shed")
    now = time.time()
    feed(tmp_path, rx(REPEATER_A_KEY, obs(7, beacon=stranger), obs(3, beacon=nameless), obs(1), t=now - 10), rx(rogue, obs(9), t=now - 5))
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not on the allowlist" in out
    assert f"{stranger.hex()}  'Shed'" in out and f"add: beaconctl beacon add {stranger.hex()}" in out
    assert f"{nameless.hex()}  (no name announced)" in out
    assert "not in the repeater table" in out and rogue[:8].hex() in out
    assert f"no location advertised  1 observations" in out and f"add: beaconctl repeater add {rogue[:8].hex()}" in out


def test_status_on_an_empty_database(cfg_file, capsys):
    code, out, _ = run(cfg_file, "status", capsys=capsys)
    assert code == 0 and "no beacons" in out


def test_beacon_status_details(cfg_file, tmp_path, capsys):
    provision(cfg_file, capsys)
    announce(tmp_path, BEACON_PREFIX, "Roof")
    now = time.time()
    feed(tmp_path, rx(REPEATER_A_KEY, obs(12, rssi=-97, snr_x4=-21, batt=3987), t=now - 30), rx(REPEATER_B_KEY, obs(12, batt=3987), t=now - 28))
    code, out, _ = run(cfg_file, "beacon", "status", B1, capsys=capsys)
    assert code == 0
    assert out.startswith("Roof  [ok]") and f"key prefix         {B1}" in out
    assert "high-water mark    12" in out and "3.99V" in out
    assert "north" in out and "-97 dBm" in out and "-5.25 dB" in out and BEACON_PREFIX.hex() and REPEATER_B_KEY[:6].hex() in out


def test_beacon_status_without_a_name_is_headed_by_the_prefix(cfg_file, capsys):
    provision(cfg_file, capsys)
    _, out, _ = run(cfg_file, "beacon", "status", B2, capsys=capsys)
    assert out.startswith(f"{B2}  [silent]")


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

    feed(tmp_path, rx(REPEATER_A_KEY, obs(50), obs(60, beacon=BEACON2_PREFIX), t=now - 5))
    feed(tmp_path, rx(REPEATER_A_KEY, obs(10), obs(20, beacon=BEACON2_PREFIX), t=now - 4))
    with Store.open(tmp_path / "beacon.db") as store:
        Pipeline(store, assume_synced=True).record_bad_report(Bad, "unknown report version 9")
        Pipeline(store, assume_synced=True).record_bad_report(Bad, "unknown name message version 9", "bad_names")
    _, out, _ = run(cfg_file, "rejects", capsys=capsys)
    assert out.count("replay") == 2 and "malformed reports" in out and "unknown report version 9" in out
    assert "unknown name message version 9" in out
    _, out, _ = run(cfg_file, "rejects", "--beacon", B2[:6], capsys=capsys)
    assert out.count("replay") == 1 and "malformed" not in out


def test_rejects_marks_provisional_times(tmp_path, capsys):
    cfg = tmp_path / "c2.toml"
    cfg.write_text("")  # clock not trusted
    run(str(cfg), "beacon", "add", B1)
    run(str(cfg), "repeater", "add", REPEATER_A_KEY.hex(), "1", "1", "--name", "north")
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
    run(str(cfg), "beacon", "add", B1)
    run(str(cfg), "repeater", "add", REPEATER_A_KEY.hex(), "1", "1", "--name", "north")
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


def _secrets(tmp_path):
    (tmp_path / "secrets.toml").write_text('[channel]\nkey = "' + "ab" * 16 + '"\n')


def test_check_flags_repeater_windows(cfg_file, tmp_path, capsys):
    _secrets(tmp_path)
    run(cfg_file, "beacon", "add", B1)
    run(cfg_file, "repeater", "add", "11" * 8, "1", "1", "--name", "good", "--window", "60")
    run(cfg_file, "repeater", "add", "22" * 8, "1", "1", "--name", "long", "--window", "250")
    run(cfg_file, "repeater", "add", "33" * 8, "1", "1", "--name", "toolong", "--window", "280")
    run(cfg_file, "repeater", "add", "44" * 8, "1", "1")  # no name, no window
    capsys.readouterr()
    code, out, _ = run(cfg_file, "check", capsys=capsys)
    assert code == 1
    assert "good" not in out
    assert "long (222222): beacon.window 250s is above the advised 240s" in out
    assert "toolong (333333): beacon.window 280s is not below the shortest beacon interval (270s)" in out
    assert "444444444444: beacon.window not recorded" in out


def test_check_passes_on_a_good_setup(cfg_file, tmp_path, capsys):
    _secrets(tmp_path)
    run(cfg_file, "beacon", "add", B1)
    run(cfg_file, "repeater", "add", "11" * 8, "1", "1", "--window", "60")
    capsys.readouterr()
    code, out, _ = run(cfg_file, "check", capsys=capsys)
    assert code == 0 and out.startswith("ok:")


def test_check_uses_the_configured_beacon_interval(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text("[beacon]\ninterval_s = 30\n")
    _secrets(tmp_path)
    run(str(cfg), "beacon", "add", B1)
    run(str(cfg), "repeater", "add", "11" * 8, "1", "1", "--name", "r", "--window", "20")
    run(str(cfg), "repeater", "add", "22" * 8, "1", "1", "--name", "slow", "--window", "60")
    capsys.readouterr()
    code, out, _ = run(str(cfg), "check", capsys=capsys)
    assert code == 1 and "r (111111): beacon.window 20s" not in out
    assert "slow (222222): beacon.window 60s is not below the shortest beacon interval (27s)" in out


def test_listed_beacons_and_repeaters_leave_the_unlisted_sections(cfg_file, tmp_path, capsys):
    rogue = bytes(range(0x30, 0x50))
    run(cfg_file, "beacon", "add", B1)
    run(cfg_file, "repeater", "add", REPEATER_A_KEY.hex(), "1", "1", "--name", "north")
    now = time.time()
    feed(tmp_path, rx(rogue, obs(5), t=now - 5), rx(REPEATER_A_KEY, obs(1, beacon=BEACON2_PREFIX), t=now - 4))
    capsys.readouterr()
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not in the repeater table" in out and "not on the allowlist" in out
    run(cfg_file, "repeater", "add", rogue.hex(), "1", "1")
    run(cfg_file, "beacon", "add", B2)
    capsys.readouterr()
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not in the repeater table" not in out and "not on the allowlist" not in out


# --- bulk options (--all) -------------------------------------------------------------------------------------------------


PREFIXES = [bytes([i + 1] * 8).hex() for i in range(3)]


def add_three(cfg_file, capsys):
    for p in PREFIXES:
        assert run(cfg_file, "beacon", "add", p) == 0
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
    run(cfg_file, "repeater", "add", REPEATER_A_KEY.hex(), "1", "1", "--name", "north")
    announce(tmp_path, bytes.fromhex(PREFIXES[0]), "Roof")
    feed(tmp_path, rx(REPEATER_A_KEY, obs(100, beacon=bytes.fromhex(PREFIXES[0])), obs(7, beacon=bytes.fromhex(PREFIXES[1])), t=time.time() - 5))
    feed(tmp_path, rx(REPEATER_A_KEY, obs(3, beacon=bytes.fromhex(PREFIXES[0])), t=time.time() - 4))  # the first is locked out
    capsys.readouterr()
    code, out, _ = run(cfg_file, "beacon", "reset", "--all", capsys=capsys)
    assert code == 0
    assert "reset Roof (010101): was high-water mark 100, 1 rejects" in out and "was high-water mark 7" in out
    assert "was no high-water mark" in out and "each of these beacons" in out
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


def test_a_prefix_and_all_are_mutually_exclusive_and_one_is_required(cfg_file, capsys):
    add_three(cfg_file, capsys)
    for verb in ("enable", "disable", "reset", "remove"):
        code, _, err = run(cfg_file, "beacon", verb, PREFIXES[0], "--all", capsys=capsys)
        assert code == 2 and "not both" in err, verb
        code, _, err = run(cfg_file, "beacon", verb, capsys=capsys)
        assert code == 2 and "--all" in err, verb
    _, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert all(p in out for p in PREFIXES)  # nothing was touched


def test_single_beacon_forms_still_work(cfg_file, capsys):
    add_three(cfg_file, capsys)
    assert run(cfg_file, "beacon", "disable", PREFIXES[1]) == 0
    assert run(cfg_file, "beacon", "reset", PREFIXES[0]) == 0
    assert run(cfg_file, "beacon", "remove", PREFIXES[2]) == 0
    capsys.readouterr()
    code, out, _ = run(cfg_file, "beacon", "list", capsys=capsys)
    assert PREFIXES[2] not in out and PREFIXES[0] in out


def test_add_all_adds_every_reported_beacon_that_is_not_listed(cfg_file, tmp_path, capsys):
    run(cfg_file, "repeater", "add", REPEATER_A_KEY.hex(), "1", "1", "--name", "north")
    run(cfg_file, "beacon", "add", PREFIXES[0])
    new1, new2 = bytes.fromhex("f5b165224a58b791"), bytes.fromhex("7bd5d47e446fcec2")
    announce(tmp_path, new1, "Roof")
    feed(tmp_path, rx(REPEATER_A_KEY, obs(1, beacon=bytes.fromhex(PREFIXES[0])), obs(5, beacon=new1), obs(9, beacon=new2), t=time.time() - 5))
    capsys.readouterr()
    code, out, _ = run(cfg_file, "beacon", "add", "--all", capsys=capsys)
    assert code == 0
    assert f"added beacon {new1.hex()} (Roof)" in out and f"added beacon {new2.hex()} (no name announced yet)" in out
    assert "added 2 beacon(s)" in out and PREFIXES[0] not in out
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not on the allowlist" not in out
    code, out, _ = run(cfg_file, "beacon", "add", "-a", capsys=capsys)
    assert code == 0 and "nothing to add" in out


def test_add_all_options_and_conflicts(cfg_file, tmp_path, capsys):
    run(cfg_file, "repeater", "add", REPEATER_A_KEY.hex(), "1", "1", "--name", "north")
    feed(tmp_path, rx(REPEATER_A_KEY, obs(5, beacon=bytes([8] * 8)), t=time.time() - 3 * 3600))
    code, out, _ = run(cfg_file, "beacon", "add", "--all", "--hours", "1", capsys=capsys)
    assert code == 0 and "nothing to add" in out  # reported 3 hours ago
    for argv in (["x" * 16, "--all"], ["--all", "--notes", "n"]):
        code, _, err = run(cfg_file, "beacon", "add", *argv, capsys=capsys)
        assert code == 2 and "--all" in err
    code, _, err = run(cfg_file, "beacon", "add", capsys=capsys)
    assert code == 2 and "usage" in err
    code, out, _ = run(cfg_file, "beacon", "add", "--all", capsys=capsys)
    assert code == 0 and "added 1 beacon(s)" in out


# --- repeater onboarding: positions come from adverts --------------------------------------------------------------------


RK = bytes(range(0x60, 0x80))
RP = RK[:8].hex()


def heard_advert(tmp_path, key=RK, name="North Ridge", lat=40.5, lon=-75.25):
    with Store.open(tmp_path / "beacon.db") as store:
        store.record_repeater_advert(key, name, lat, lon, 1, time.time())


def test_repeater_add_takes_the_heard_position_and_name(cfg_file, tmp_path, capsys):
    heard_advert(tmp_path)
    code, out, _ = run(cfg_file, "repeater", "add", RP, capsys=capsys)
    assert code == 0 and "North Ridge" in out and "40.500000, -75.250000" in out and "left out of positioning" not in out
    _, out, _ = run(cfg_file, "repeater", "list", capsys=capsys)
    row = out.splitlines()[1].split()
    assert row[:5] == ["North", "Ridge", RP, "40.500000", "-75.250000"] and "advert" in row


def test_repeater_add_without_a_position_is_unlocated_and_says_so(cfg_file, capsys):
    code, out, _ = run(cfg_file, "repeater", "add", RP, capsys=capsys)
    assert code == 0 and "no location" in out and "left out of positioning" in out
    _, out, _ = run(cfg_file, "repeater", "list", capsys=capsys)
    row = out.splitlines()[1].split()
    assert row[:4] == ["-", RP, "-", "-"] and "0.000000" not in out


def test_repeater_add_with_a_position_is_manual(cfg_file, capsys):
    code, out, _ = run(cfg_file, "repeater", "add", RP, "41.5", "-76.5", "--name", "Mine", capsys=capsys)
    assert code == 0 and "Mine" in out and "41.500000, -76.500000" in out
    _, out, _ = run(cfg_file, "repeater", "list", capsys=capsys)
    assert "manual" in out


def test_repeater_add_needs_both_coordinates(cfg_file, capsys):
    code, _, err = run(cfg_file, "repeater", "add", RP, "41.5", capsys=capsys)
    assert code == 2 and "both latitude and longitude" in err


def test_a_later_advert_replaces_a_hand_set_position(cfg_file, tmp_path, capsys):
    run(cfg_file, "repeater", "add", RP, "41.5", "-76.5")
    code, out, _ = run(cfg_file, "repeater", "locate", RP[:6], "10", "20", capsys=capsys)
    assert code == 0 and "10.000000, 20.000000" in out and "next advert" in out
    heard_advert(tmp_path)  # the repeater was told where it is
    with Store.open(tmp_path / "beacon.db") as store:
        r = store.repeater(RP)
        assert (r["lat"], r["lon"], r["location_source"]) == (40.5, -75.25, "advert")
    _, out, _ = run(cfg_file, "repeater", "list", capsys=capsys)
    assert "advert" in out and "manual" not in out


def test_locate_validates(cfg_file, capsys):
    run(cfg_file, "repeater", "add", RP)
    code, _, err = run(cfg_file, "repeater", "locate", RP, "95", "0", capsys=capsys)
    assert code == 2 and "latitude" in err
    code, _, err = run(cfg_file, "repeater", "locate", "nobody", "1", "1", capsys=capsys)
    assert code == 2 and "no repeater matches" in err


def test_repeater_add_all_adds_reporters_with_what_was_heard(cfg_file, tmp_path, capsys):
    run(cfg_file, "beacon", "add", B1)
    other = bytes(range(150, 182))
    heard_advert(tmp_path)
    feed(tmp_path, rx(RK, obs(1), t=time.time() - 5), rx(other, obs(2), t=time.time() - 4))
    capsys.readouterr()
    code, out, _ = run(cfg_file, "repeater", "add", "--all", capsys=capsys)
    assert code == 0
    assert "North Ridge" in out and "40.500000, -75.250000" in out and f"{other[:6].hex()}" in out and "no location" in out
    assert "added 2 repeater(s)" in out and "1 of them have no location" in out
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert "not in the repeater table" not in out
    code, out, _ = run(cfg_file, "repeater", "add", "-a", capsys=capsys)
    assert code == 0 and "nothing to add" in out


def test_repeater_add_all_ignores_repeaters_that_only_advertised(cfg_file, tmp_path, capsys):
    heard_advert(tmp_path)  # in range of the base companion, but never reported on our channel
    code, out, _ = run(cfg_file, "repeater", "add", "--all", capsys=capsys)
    assert code == 0 and "nothing to add" in out
    _, out, _ = run(cfg_file, "repeater", "list", capsys=capsys)
    assert "no repeaters" in out


def test_repeater_add_all_conflicts(cfg_file, capsys):
    for argv in ([RP, "--all"], ["--all", "--name", "x"], ["--all", "1", "2"]):
        code, _, err = run(cfg_file, "repeater", "add", *argv, capsys=capsys)
        assert code == 2 and "--all" in err
    code, _, err = run(cfg_file, "repeater", "add", capsys=capsys)
    assert code == 2 and "usage" in err


def test_status_describes_unknown_repeaters_by_their_advert(cfg_file, tmp_path, capsys):
    run(cfg_file, "beacon", "add", B1)
    heard_advert(tmp_path)
    feed(tmp_path, rx(RK, obs(1), t=time.time() - 5))
    _, out, _ = run(cfg_file, "status", capsys=capsys)
    assert f"{RP}  'North Ridge'  at 40.500000, -75.250000" in out and f"add: beaconctl repeater add {RP}" in out


def test_check_fails_for_unlocated_repeaters(cfg_file, tmp_path, capsys):
    _secrets(tmp_path)
    run(cfg_file, "beacon", "add", B1)
    run(cfg_file, "repeater", "add", "11" * 8, "1", "1", "--name", "placed", "--window", "60")
    run(cfg_file, "repeater", "add", "22" * 8, "--name", "lost", "--window", "60")
    capsys.readouterr()
    code, out, _ = run(cfg_file, "check", capsys=capsys)
    assert code == 1
    assert "lost (222222): no location, so it is left out of positioning" in out and "placed" not in out
    run(cfg_file, "repeater", "locate", "lost", "5", "6")
    capsys.readouterr()
    code, out, _ = run(cfg_file, "check", capsys=capsys)
    assert code == 0 and out.startswith("ok:")
