"""The closed set of remote operations: validation, the commands each sends and how replies are read, checked against the fake repeater."""

import pytest

from beacon_base import remote
from beacon_base.fake_repeater import FakeRepeater
from beacon_base.remote import RemoteError

ME = b"m" * 32


def run_job(rpt, kind, op, params=None, base_hash=None, ts=[5000]):
    """What the executor does, without a link: send each planned command to the repeater as an admin and interpret the replies."""
    if ME not in rpt.acl:
        rpt.login(ME, "password", 1)
    params = remote.validate(kind, op, params)
    replies = []
    for step in remote.plan(kind, op, params):
        ts[0] += 1
        reply = rpt.command(ME, ts[0], "7f|" + step.command)
        assert reply is not None and reply.startswith("7f|"), (step.command, reply)
        replies.append(reply[3:])
        if remote.step_failed(step, reply[3:]):
            break
    return remote.interpret(kind, op, params, replies, base_hash)


@pytest.fixture
def rpt():
    return FakeRepeater(b"r" * 32, name="ridge", lat=47.123456, lon=-122.335167)


# --- validation -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, params",
    [
        ("name", {"name": "ridge"}),
        ("name", {"name": "é" * 15}),  # 30 bytes
        ("name", {"name": "a" * 31}),
        ("location", {"lat": 47.5, "lon": -122.25}),
        ("location", {"lat": -90, "lon": 180}),
        ("location", {"lat": 0, "lon": 0, "allow_zero": True}),
        ("advert.interval", {"minutes": 0}),
        ("advert.interval", {"minutes": 60}),
        ("advert.interval", {"minutes": 240}),
        ("flood.advert.interval", {"hours": 0}),
        ("flood.advert.interval", {"hours": 3}),
        ("flood.advert.interval", {"hours": 168}),
        ("beacon.window", {"seconds": 1}),
        ("beacon.window", {"seconds": 3600}),
        ("beacon.names", {"on": False}),
        ("beacon.name_refresh", {"hours": 0}),
        ("beacon.name_refresh", {"hours": 8760}),
    ],
)
def test_valid_values_are_accepted_and_normalised(kind, params):
    out = remote.validate(kind, "set", params)
    assert out["verify"] is True
    assert remote.validate(kind, "set", out) == out  # normalising twice changes nothing


@pytest.mark.parametrize(
    "kind, params",
    [
        ("name", {"name": ""}),
        ("name", {}),
        ("name", {"name": " padded "}),
        ("name", {"name": "a" * 32}),
        ("name", {"name": "é" * 16}),  # 32 bytes
        ("name", {"name": "bad[name"}),
        ("name", {"name": "a:b"}),
        ("name", {"name": "a,b"}),
        ("name", {"name": "line\nbreak"}),
        ("name", {"name": 5}),
        ("location", {"lat": 91, "lon": 0}),
        ("location", {"lat": 0, "lon": -181}),
        ("location", {"lat": float("nan"), "lon": 1}),
        ("location", {"lat": float("inf"), "lon": 1}),
        ("location", {"lat": "47.5", "lon": 1}),
        ("location", {"lat": True, "lon": 1}),
        ("location", {"lat": 47.5}),
        ("location", {"lat": 0, "lon": 0}),  # 0, 0 means unlocated
        ("advert.interval", {"minutes": 30}),
        ("advert.interval", {"minutes": 61}),  # odd: the repeater stores 2-minute units
        ("advert.interval", {"minutes": 242}),
        ("advert.interval", {"minutes": 60.0}),
        ("advert.interval", {"minutes": True}),
        ("flood.advert.interval", {"hours": 1}),
        ("flood.advert.interval", {"hours": 169}),
        ("beacon.window", {"seconds": 0}),
        ("beacon.window", {"seconds": 3601}),
        ("beacon.names", {"on": "on"}),
        ("beacon.names", {}),
        ("beacon.name_refresh", {"hours": 8761}),
        ("beacon.name_refresh", {"hours": -1}),
        ("name", {"name": "ok", "surprise": 1}),
        ("name", {"name": "ok", "verify": "yes"}),
    ],
)
def test_bad_values_are_refused_before_anything_is_sent(kind, params):
    with pytest.raises(RemoteError):
        remote.validate(kind, "set", params)


def test_read_only_items_cannot_be_set_and_the_channel_key_never_crosses_the_air():
    for kind in ("beacon.stats", "beacon.channel", "all"):
        with pytest.raises(RemoteError, match="cannot be set"):
            remote.validate(kind, "set", {})
    with pytest.raises(RemoteError, match="never sent over the mesh"):
        remote.validate("beacon.channel", "set", {"hex": "00" * 16})


def test_unknown_items_operations_and_get_values_are_refused():
    for args in (("password", "get", {}), ("name", "delete", {}), ("name", "get", {"name": "x"}), ("reboot", "set", {})):
        with pytest.raises(RemoteError):
            remote.validate(*args)


def test_the_planned_commands_are_exactly_these():
    commands = lambda kind, op, params=None: [s.command for s in remote.plan(kind, op, params or {})]
    assert commands("name", "get") == ["get name"]
    assert commands("location", "get") == ["get lat", "get lon"]
    assert commands("name", "set", {"name": "ridge"}) == ["set name ridge", "get name", "get advert.interval"]
    assert commands("name", "set", {"name": "ridge", "verify": False}) == ["set name ridge"]
    assert commands("location", "set", {"lat": 47.5, "lon": -122.25}) == [
        "set lat 47.500000", "set lon -122.250000", "get lat", "get lon", "get advert.interval"]
    assert commands("advert.interval", "set", {"minutes": 60}) == ["set advert.interval 60", "get advert.interval"]
    assert commands("flood.advert.interval", "set", {"hours": 48}) == ["set flood.advert.interval 48", "get flood.advert.interval", "get advert.interval"]
    assert commands("beacon.window", "set", {"seconds": 5})[:2] == ["beacon.window 5", "beacon.window"]
    assert commands("beacon.names", "set", {"on": True})[0] == "beacon.names on"
    assert commands("beacon.name_refresh", "set", {"hours": 4})[0] == "beacon.name_refresh 4"
    assert commands("beacon.stats", "get") == ["beacon.stats"] and commands("beacon.channel", "get") == ["beacon.channel"]
    assert len(commands("all", "get")) == 9


def test_no_planned_command_can_clear_or_set_the_channel_or_do_anything_else_destructive():
    steps = []
    for kind in remote.KINDS:
        for op in remote.OPS:
            try:
                steps += remote.plan(kind, op, _params_for(kind, op))
            except RemoteError:
                assert op == "set" and kind in remote.READ_ONLY  # only the read-only items are refused
    texts = [s.command for s in steps]
    assert len(texts) > 30
    assert not [t for t in texts if t.startswith("beacon.channel ") or "password" in t or t in ("reboot", "erase", "clear stats")]
    assert all(len(t.encode()) <= 157 for t in texts)  # fits in a command with its tag


def _params_for(kind, op):
    if op == "get":
        return {}
    return {
        "name": {"name": "x"}, "location": {"lat": 1.5, "lon": 2.5}, "advert.interval": {"minutes": 60}, "flood.advert.interval": {"hours": 4},
        "beacon.window": {"seconds": 5}, "beacon.names": {"on": True}, "beacon.name_refresh": {"hours": 4},
    }.get(kind, {})


def test_passwords_are_checked_before_sending():
    assert remote.validate_password(None) is None and remote.validate_password("") is None
    assert remote.validate_password("x" * 15) == "x" * 15
    for bad in ("x" * 16, "é" * 8, "a\0b"):
        with pytest.raises(RemoteError):
            remote.validate_password(bad)


# --- reading replies ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply, kind",
    [
        ("> ridge", remote.VALUE), ("> ", remote.VALUE), ("OK", remote.OK), ("OK - report channel cleared", remote.OK),
        ("Unknown command", remote.UNSUPPORTED), ("Err - unknown beacon command", remote.UNSUPPORTED), ("unknown config: x", remote.UNSUPPORTED),
        ("Err - window must be 1-3600 secs", remote.ERROR), ("Error, bad chars", remote.ERROR), ("Error: interval range is 60-240 minutes", remote.ERROR),
        ("heard 1, reported 2", remote.RAW),
    ],
)
def test_classify(reply, kind):
    assert remote.classify(reply)[0] == kind


def test_stats_are_parsed_with_or_without_uptime():
    old = remote.parse_stats("heard 10, reported 8, dropped 1, send fail 2, pending 3, names sent 4")
    assert old == {"heard": 10, "reported": 8, "dropped": 1, "send_fail": 2, "pending": 3, "names_sent": 4, "up_s": None, "parsed": True}
    new = remote.parse_stats("heard 10, reported 8, dropped 1, send fail 2, pending 3, names sent 4, up 86400s")
    assert new["up_s"] == 86400 and new["heard"] == 10
    assert remote.parse_stats("something else") == {"raw": "something else", "parsed": False}


def test_channel_hash_is_the_first_byte_of_sha256_of_the_16_byte_key():
    from hashlib import sha256

    key = bytes(range(16))
    assert remote.channel_hash(key) == sha256(key).digest()[0]
    assert remote.channel_hash(key + bytes(16)) == remote.channel_hash(key)  # only the 16-byte key counts


# --- every operation against the fake repeater ----------------------------------------------------------------------------


def test_get_every_item(rpt):
    rpt.channel_secret = bytes(range(16))
    rpt.uptime_in_stats = True
    values = lambda kind: run_job(rpt, kind, "get", base_hash=remote.channel_hash(bytes(range(16)))).result["values"]
    assert values("name") == {"name": "ridge"}
    assert values("location") == {"location": {"lat": 47.123455, "lon": -122.3351669}}  # as the repeater prints them: lossy
    assert values("advert.interval") == {"advert.interval": 2}
    assert values("flood.advert.interval") == {"flood.advert.interval": 47}
    assert values("beacon.window") == {"beacon.window": 60}
    assert values("beacon.names") == {"beacon.names": True}
    assert values("beacon.name_refresh") == {"beacon.name_refresh": 4}
    assert values("beacon.channel")["beacon.channel"]["matches_base"] is True
    assert values("beacon.stats")["beacon.stats"]["up_s"] is not None


def test_the_channel_check_tells_a_different_key_from_the_base_s_and_an_unset_channel(rpt):
    assert run_job(rpt, "beacon.channel", "get").result["values"]["beacon.channel"] == {"set": False, "hash": None, "matches_base": None}
    rpt.channel_secret = bytes(range(16))
    right = remote.channel_hash(bytes(range(16)))
    other = run_job(rpt, "beacon.channel", "get", base_hash=(right + 1) % 256).result["values"]["beacon.channel"]
    assert other["set"] and other["matches_base"] is False and other["hash"] == right
    assert run_job(rpt, "beacon.channel", "get").result["values"]["beacon.channel"]["matches_base"] is None  # the base's hash unknown


def test_get_all_reads_everything_and_carries_on_past_an_unsupported_item(rpt):
    out = run_job(rpt, "all", "get")
    assert out.ok and set(out.result["values"]) == {
        "name", "location", "advert.interval", "flood.advert.interval", "beacon.window", "beacon.names", "beacon.name_refresh", "beacon.channel"}
    old = FakeRepeater(b"o" * 32, names_supported=False)
    out = run_job(old, "all", "get")
    assert out.ok and out.result["errors"] == {"beacon.names": "unsupported", "beacon.name_refresh": "unsupported"}
    assert out.result["values"]["beacon.window"] == 60 and out.message.startswith("read, some items unavailable")
    plain = FakeRepeater(b"p" * 32, beacon_build=False)
    out = run_job(plain, "all", "get")
    assert out.ok and out.result["values"].keys() == {"name", "location", "advert.interval", "flood.advert.interval"}
    assert set(out.result["errors"].values()) == {"unsupported"}


def test_a_single_unsupported_get_fails_as_unsupported():
    plain = FakeRepeater(b"p" * 32, beacon_build=False)
    out = run_job(plain, "beacon.window", "get")
    assert not out.ok and out.code == "unsupported"


def test_set_name_is_read_back_and_confirmed(rpt):
    out = run_job(rpt, "name", "set", {"name": "new-name"})
    assert out.ok and out.result["verified"] and out.result["values"]["name"] == "new-name" and out.result["sent"] == "new-name"
    assert rpt.name == "new-name"


def test_set_location_records_what_was_sent_even_though_it_reads_back_lossy(rpt):
    out = run_job(rpt, "location", "set", {"lat": 47.123456, "lon": 151.2092954})
    assert out.ok and out.result["verified"]
    assert out.result["sent"] == {"lat": 47.123456, "lon": 151.209295}  # rounded to the advert's 6 decimals
    got = out.result["values"]["location"]  # what 'get' prints: truncated from a 32-bit float
    assert got != out.result["sent"] and abs(got["lat"] - 47.123456) < 2e-5 and abs(got["lon"] - 151.209295) < 2e-5
    assert (rpt.lat, rpt.lon) == (47.123456, 151.209295)


def test_the_first_set_on_a_fresh_repeater_warns_that_zero_hop_adverts_are_off(rpt):
    out = run_job(rpt, "name", "set", {"name": "x"})
    assert out.result["values"]["advert.interval"] == 0 and out.result["notes"] == [remote.ZERO_HOP_WARNING]
    out = run_job(rpt, "advert.interval", "set", {"minutes": 60})
    assert out.ok and out.result["values"] == {"advert.interval": 60} and out.result["notes"] == []
    out = run_job(rpt, "name", "set", {"name": "y"})
    assert out.result["values"]["advert.interval"] == 60 and out.result["notes"] == []


@pytest.mark.parametrize(
    "kind, params, attr, expected",
    [
        ("flood.advert.interval", {"hours": 48}, "flood_advert_interval", 48),
        ("flood.advert.interval", {"hours": 0}, "flood_advert_interval", 0),
        ("advert.interval", {"minutes": 120}, "advert_interval_units", 60),
        ("beacon.window", {"seconds": 5}, "window", 5),
        ("beacon.names", {"on": False}, "names_on", False),
        ("beacon.name_refresh", {"hours": 0}, "name_refresh", 0),
        ("beacon.name_refresh", {"hours": 24}, "name_refresh", 24),
    ],
)
def test_every_other_set_is_confirmed(rpt, kind, params, attr, expected):
    out = run_job(rpt, kind, "set", params)
    assert out.ok and out.result["verified"], out
    assert getattr(rpt, attr) == expected


def test_set_without_verify_sends_only_the_set(rpt):
    out = run_job(rpt, "beacon.window", "set", {"seconds": 9, "verify": False})
    assert out.ok and out.result["verified"] is False and out.result["values"] == {"beacon.window": 9}
    assert len(out.result["replies"]) == 1


def test_a_refused_set_stops_before_the_read_back(rpt):
    rpt.names_supported = False
    out = run_job(rpt, "beacon.names", "set", {"on": False})
    assert not out.ok and out.code == "unsupported" and len(out.result["replies"]) == 1


def test_a_value_the_repeater_changes_is_a_mismatch(rpt, monkeypatch):
    original = rpt.handle_command

    def odd(command):  # a repeater that answers OK and keeps another value
        reply = original(command)
        if "set name" in command:
            rpt.name = "something else"
        return reply

    monkeypatch.setattr(rpt, "handle_command", odd)
    out = run_job(rpt, "name", "set", {"name": "wanted"})
    assert not out.ok and out.code == "mismatch" and "something else" in out.message and out.result["verified"] is False


def test_replies_that_did_not_arrive_are_no_reply():
    rpt = FakeRepeater(b"r" * 32)
    params = remote.validate("name", "set", {"name": "x"})
    assert remote.interpret("name", "set", params, []).code == "no_reply"
    assert remote.interpret("name", "set", params, ["OK"]).code == "no_reply"  # the read-back never came
    assert remote.interpret("name", "get", {}, []).code == "no_reply"
    assert remote.interpret("name", "set", params, ["Error, bad chars"]).code == "rejected"
