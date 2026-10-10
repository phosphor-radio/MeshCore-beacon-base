"""The fake repeater: its behaviour is what the firmware does (read from the source), so these pin the model the remote tests rely on."""

import pytest

from beacon_base.fake_repeater import MAX_CLIENTS, FakeRepeater, atof, ftoa

ME = b"m" * 32
OTHER = b"o" * 32


@pytest.fixture
def rpt():
    return FakeRepeater(b"r" * 32, name="ridge", now=lambda: 1_790_000_000)


def admin(rpt, key=ME, ts=1000):
    assert rpt.login(key, "password", ts).permissions == 3


def test_ftoa_is_lossy_like_the_firmware():
    assert ftoa(47.123456) == "47.123455"  # the values the firmware session measured
    assert ftoa(-122.335167) == "-122.3351669"
    assert ftoa(151.2092955) == "151.2092895"
    assert ftoa(0.0) == "0.0" and ftoa(5.0) == "5.0" and ftoa(-0.5) == "-0.5"


@pytest.mark.parametrize("text, value", [("47.5", 47.5), ("12.3abc", 12.3), ("abc", 0.0), ("", 0.0), (" -3e2x", -300.0), ("nan", 0.0), (".5", 0.5)])
def test_atof_turns_junk_into_zero(text, value):
    assert atof(text) == value


def test_admin_password_logs_in_as_admin_and_is_remembered(rpt):
    reply = rpt.login(ME, "password", 10)
    assert (reply.admin_flag, reply.permissions) == (1, 3)
    again = rpt.login(ME, "", 11)  # an empty password for a key in the ACL keeps its role
    assert (again.admin_flag, again.permissions) == (1, 3)


def test_empty_password_from_an_unknown_key_is_a_guest_login(rpt):
    reply = rpt.login(OTHER, "", 10)
    assert (reply.admin_flag, reply.permissions) == (0, 0)  # a success push, but not admin
    assert rpt.command(OTHER, 11, "get name") is None  # and the CLI is silently ignored


def test_wrong_password_gets_no_reply(rpt):
    assert rpt.login(ME, "wrong", 10) is None
    assert rpt.login(ME, "password"[:3], 10) is None


def test_guest_password_logs_in_as_guest(rpt):
    rpt.guest_password = "gp"
    assert rpt.login(ME, "gp", 10).permissions == 0
    assert rpt.command(ME, 11, "get name") is None


def test_password_login_needs_a_newer_timestamp(rpt):
    admin(rpt, ts=1000)
    assert rpt.login(ME, "password", 1000) is None  # equal: replay
    assert rpt.login(ME, "password", 999) is None
    assert rpt.login(ME, "password", 1001) is not None
    assert rpt.login(ME, "", 5) is not None  # the empty-password login skips the replay check


def test_cli_timestamp_rules(rpt):
    admin(rpt, ts=1000)
    assert rpt.command(ME, 999, "get name") is None  # older: ignored
    assert rpt.command(ME, 1000, "get name") is None  # equal to the login's: treated as a retry, not run
    assert rpt.command(ME, 1001, "get name") == "> ridge"
    assert rpt.command(ME, 1001, "get name") is None  # equal again: a retry
    assert rpt.command(ME, 1002, "get name") == "> ridge"


def test_cli_needs_an_acl_entry(rpt):
    assert rpt.command(ME, 5, "get name") is None


def test_reboot_keeps_admins_drops_guests_and_resets_the_timestamp(rpt):
    admin(rpt, ts=5000)
    rpt.login(OTHER, "", 5000)
    rpt.reboot()
    assert OTHER not in rpt.acl and ME in rpt.acl
    assert rpt.command(ME, 7, "get name") == "> ridge"  # last timestamp is 0 again, so an old stamp works
    assert rpt.login(ME, "password", 8) is not None


def test_full_acl_evicts_the_oldest_guest_instead_of_failing(rpt):
    admin(rpt)
    for n in range(MAX_CLIENTS - 1):
        rpt.login(bytes([n + 1]) * 32, "", 10)
        rpt.acl[bytes([n + 1]) * 32].last_activity = n
    assert len(rpt.acl) == MAX_CLIENTS
    assert rpt.login(OTHER, "", 10) is not None  # still answered
    assert len(rpt.acl) == MAX_CLIENTS and bytes([1]) * 32 not in rpt.acl and ME in rpt.acl


def test_full_acl_of_admins_overwrites_the_last_slot(rpt):
    for n in range(MAX_CLIENTS):
        rpt.login(bytes([n + 1]) * 32, "password", 10)
    assert rpt.login(OTHER, "password", 10).permissions == 3
    assert len(rpt.acl) == MAX_CLIENTS and OTHER in rpt.acl


def test_tag_prefix_is_reflected_before_any_command(rpt):
    admin(rpt)
    assert rpt.command(ME, 2000, "7f|get name") == "7f|> ridge"
    assert rpt.command(ME, 2001, "a3|beacon.bogus") == "a3|Err - unknown beacon command"
    assert rpt.command(ME, 2002, "zz|nonsense") == "zz|Unknown command"
    assert rpt.command(ME, 2003, "ab|") == "Unknown command"  # too short for a prefix (needs more than 4 characters)
    rpt.reflect_tag = False
    assert rpt.command(ME, 2004, "7f|get name") == "> ridge"


def test_get_and_set_simple_items(rpt):
    admin(rpt)
    run = lambda ts, c: rpt.command(ME, ts, c)
    assert run(2000, "set name new-name") == "OK" and run(2001, "get name") == "> new-name"
    assert run(2002, "set name bad[name") == "Error, bad chars" and rpt.name == "new-name"
    assert run(2003, "set lat 47.123456") == "OK" and run(2004, "get lat") == "> 47.123455"
    assert rpt.lat == 47.123456  # stored in full, only the read-back is lossy
    assert run(2005, "set lon abc") == "OK" and rpt.lon == 0.0  # atof: junk is 0 and the reply is still OK
    assert run(2006, "set flood.advert.interval 48") == "OK" and run(2007, "get flood.advert.interval") == "> 48"
    assert run(2008, "set flood.advert.interval 2").startswith("Error")
    assert run(2009, "set flood.advert.interval 0") == "OK" and rpt.flood_advert_interval == 0
    assert run(2010, "set flood.advert.interval 169").startswith("Error")


def test_every_set_turns_the_short_install_advert_interval_off(rpt):
    admin(rpt)
    assert rpt.command(ME, 2000, "get advert.interval") == "> 2"  # the install default, 2 minutes
    assert rpt.command(ME, 2001, "set name z") == "OK"
    assert rpt.command(ME, 2002, "get advert.interval") == "> 0"
    assert rpt.command(ME, 2003, "set advert.interval 61") == "OK"  # stored in 2-minute units: rounds down
    assert rpt.command(ME, 2004, "get advert.interval") == "> 60"
    assert rpt.command(ME, 2005, "set advert.interval 30").startswith("Error")
    assert rpt.command(ME, 2006, "set advert.interval 241").startswith("Error")
    assert rpt.command(ME, 2007, "set advert.interval 0") == "OK" and rpt.advert_interval_units == 0


def test_beacon_commands(rpt):
    admin(rpt)
    run = lambda ts, c: rpt.command(ME, ts, c)
    assert run(2000, "beacon.channel") == "> not set"
    rpt.channel_secret = bytes(range(16))
    from hashlib import sha256
    assert run(2001, "beacon.channel") == f"> set, hash {sha256(bytes(range(16))).digest()[0]:02X}"
    assert run(2002, "beacon.window") == "> 60 secs" and run(2003, "beacon.window 5") == "OK" and rpt.window == 5
    assert run(2004, "beacon.window 0").startswith("Err") and run(2005, "beacon.window 3601").startswith("Err")
    assert run(2006, "beacon.names") == "> on" and run(2007, "beacon.names off") == "OK" and not rpt.names_on
    assert run(2008, "beacon.names maybe") == "Err - usage: beacon.names on|off"
    assert run(2009, "beacon.name_refresh") == "> 4 hours" and run(2010, "beacon.name_refresh 0") == "OK"
    assert run(2011, "beacon.name_refresh") == "> 0 (only on first sight or change)"
    assert run(2012, "beacon.name_refresh 8761").startswith("Err") and run(2013, "beacon.name_refresh x").startswith("Err")
    assert run(2014, "beacon.stats") == "heard 0, reported 0, dropped 0, send fail 0, pending 0, names sent 0"
    rpt.uptime_in_stats = True
    assert run(2015, "beacon.stats").endswith("names sent 0, up 0s")


def test_builds_without_the_beacon_reporter_or_the_names_commands(rpt):
    admin(rpt)
    rpt.beacon_build = False
    assert rpt.command(ME, 2000, "beacon.stats") == "Unknown command"
    rpt.beacon_build, rpt.names_supported = True, False
    assert rpt.command(ME, 2001, "beacon.names") == "Err - unknown beacon command"
    assert rpt.command(ME, 2002, "beacon.window") == "> 60 secs"


def test_commands_are_recorded(rpt):
    admin(rpt)
    rpt.command(ME, 2000, "7f|get name")
    assert rpt.commands == ["get name"]
