"""Remote jobs end to end: a job in the database, run by the session through the fake companion to a fake repeater, with reports flowing."""

import json
import threading
import time
from dataclasses import dataclass

import pytest

from beacon_base import clock, companion, executor, wire
from beacon_base.config import ClockConfig, CompanionConfig, Config, RadioConfig, RemoteConfig
from beacon_base.fake_companion import FakeCompanion
from beacon_base.fake_repeater import FakeRepeater
from beacon_base.ingest import CompanionSession
from beacon_base.pipeline import Pipeline
from beacon_base.service import PipelineHandler
from beacon_base.store import Store

KEY = bytes(range(16))
RKEY = bytes(range(1, 33))
BEACON = bytes.fromhex("a1" * 8)
FINISHED = ("done", "failed", "expired")


def until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


@dataclass
class Env:
    fake: FakeCompanion
    repeater: FakeRepeater
    store: Store  # a second connection, as beaconctl would have
    session: CompanionSession
    handler: PipelineHandler
    path: object

    def submit(self, kind, op="get", params=None, password=None, **kw):
        return self.store.submit_job("ridge", kind, op, params, password, **kw)

    def wait(self, job_id):
        until(lambda: self.store.job(job_id)["state"] in FINISHED)
        return self.store.job(job_id)

    def run(self, kind, op="get", params=None, password=None):
        return self.wait(self.submit(kind, op, params, password))

    def result(self, row):
        return json.loads(row["result"])


def build(tmp_path, assume_synced=True, rtc_start=1_715_770_351):
    fake = FakeCompanion(rtc_start=rtc_start)
    fake.est_timeout_ms = 100
    repeater = fake.add_repeater(FakeRepeater(RKEY, name="ridge", now=lambda: int(time.time())))
    path = tmp_path / "beacon.db"
    server = Store.open(path)
    server.add_repeater(RKEY.hex(), 47.5, -122.25, name="ridge")
    server.add_beacon(BEACON.hex())
    cfg = Config(
        companion=CompanionConfig(port=fake.path, command_timeout=2.0, poll_interval=5.0), radio=RadioConfig(), channel_key=KEY,
        clock=ClockConfig(assume_synced=assume_synced),
        remote=RemoteConfig(min_timeout_s=0.4, max_timeout_s=0.8, timeout_factor=1.5, max_job_s=20.0),
    )
    handler = PipelineHandler(server, Pipeline(server, assume_synced=assume_synced), fake.path)
    session = CompanionSession(cfg, handler)
    handler.bind(session)
    stop = threading.Event()
    thread = threading.Thread(target=session.run, args=(stop,), daemon=True)
    thread.start()
    env = Env(fake, repeater, Store.open(path), session, handler, path)
    until(lambda: session.stats["logins"] >= 0 and env.store.status_row() is not None and env.store.status_row()["connected"] == 1)
    until(lambda: session.clock_synced or not assume_synced)
    return env, (stop, thread, server)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "CLAIM_INTERVAL", 0.05)
    e, (stop, thread, server) = build(tmp_path)
    yield e
    stop.set()
    thread.join(timeout=5)
    e.store.close()
    server.close()
    e.fake.close()


# --- enrolment and the ACL ------------------------------------------------------------------------------------------------


def test_a_repeater_that_does_not_know_the_base_needs_the_password_once(env):
    row = env.run("name")
    assert row["state"] == "failed" and row["error_code"] == "needs_password"  # an empty password logs in as a guest
    assert env.run("name", password="wrong")["error_code"] == "no_reply"
    row = env.run("name", password="password")
    assert row["state"] == "done" and env.result(row)["values"] == {"name": "ridge"}
    assert env.result(row)["login"]["admin"] is True and row["secret"] is None
    row = env.run("beacon.window")  # no password: the same connection is still logged in
    assert row["state"] == "done" and env.result(row)["values"] == {"beacon.window": 60}


def test_after_enrolment_the_empty_password_login_works_even_after_a_reconnect_and_a_repeater_reboot(env):
    assert env.run("name", password="password")["state"] == "done"
    env.repeater.reboot()  # admins come back from flash, the last timestamp is 0 again
    env.fake.reconnect()
    until(lambda: env.session.stats["logins"] >= 1 and env.store.status_row()["connected"] == 1)
    time.sleep(0.3)
    row = env.run("name")
    assert row["state"] == "done" and env.result(row)["values"] == {"name": "ridge"}
    assert env.result(row)["login"]["permissions"] == 3


def test_the_guest_password_is_not_admin(env):
    env.repeater.guest_password = "guestpw"
    row = env.run("name", password="guestpw")
    assert row["state"] == "failed" and row["error_code"] == "not_admin"


def test_the_companion_key_in_the_heartbeat_is_what_the_repeater_lists(env):
    assert env.run("name", password="password")["state"] == "done"
    prefix = bytes(env.store.status_row()["companion_key_prefix"])
    assert [k[:6] for k in env.repeater.acl] == [prefix]


# --- sets -----------------------------------------------------------------------------------------------------------------


def test_set_name_and_location_are_verified_and_recorded(env):
    row = env.run("name", "set", {"name": "hilltop"}, password="password")
    r = env.result(row)
    assert row["state"] == "done" and r["verified"] and r["sent"] == "hilltop" and env.repeater.name == "hilltop"
    assert r["notes"] and "zero-hop" in r["notes"][0]  # the first set on a fresh repeater turned them off
    row = env.run("location", "set", {"lat": 47.123456, "lon": -122.335167})
    r = env.result(row)
    assert row["state"] == "done" and r["sent"] == {"lat": 47.123456, "lon": -122.335167} and r["verified"]
    assert (env.repeater.lat, env.repeater.lon) == (47.123456, -122.335167)
    assert r["values"]["location"]["lon"] == -122.3351669  # the lossy read-back, not what is recorded


def test_the_beacon_settings_round_trip(env):
    for kind, params, attr, value in [
        ("beacon.window", {"seconds": 5}, "window", 5), ("beacon.names", {"on": False}, "names_on", False),
        ("beacon.name_refresh", {"hours": 12}, "name_refresh", 12), ("flood.advert.interval", {"hours": 24}, "flood_advert_interval", 24),
        ("advert.interval", {"minutes": 90}, "advert_interval_units", 45),
    ]:
        row = env.run(kind, "set", params, password="password")
        assert row["state"] == "done", (kind, row["error"])
        assert getattr(env.repeater, attr) == value


def test_a_firmware_without_the_names_commands_is_reported_as_unsupported(env):
    env.repeater.names_supported = False
    row = env.run("beacon.names", "set", {"on": False}, password="password")
    assert row["state"] == "failed" and row["error_code"] == "unsupported"
    assert env.run("beacon.window")["state"] == "done"


def test_get_all_and_the_channel_check(env):
    env.repeater.channel_secret = KEY  # the base's own key
    row = env.run("all", password="password")
    r = env.result(row)
    assert row["state"] == "done" and r["values"]["beacon.channel"]["matches_base"] is True and r["values"]["name"] == "ridge"
    assert r["round_trips"] == 9
    env.repeater.channel_secret = bytes(range(100, 116))
    assert env.result(env.run("beacon.channel"))["values"]["beacon.channel"]["matches_base"] is False


def test_the_stats_reply_with_and_without_uptime(env):
    env.repeater.stats["heard"] = 7
    assert env.result(env.run("beacon.stats", password="password"))["values"]["beacon.stats"]["up_s"] is None
    env.repeater.uptime_in_stats = True
    stats = env.result(env.run("beacon.stats"))["values"]["beacon.stats"]
    assert stats["heard"] == 7 and stats["up_s"] is not None


def test_a_lost_reply_is_retried_and_a_set_is_idempotent(env):
    assert env.run("name", password="password")["state"] == "done"
    env.repeater.drop_next_replies = 1
    row = env.run("name", "set", {"name": "again"})
    assert row["state"] == "done" and env.repeater.name == "again" and env.result(row)["verified"]
    assert env.repeater.commands.count("set name again") == 2  # the lost one ran, so it ran twice


def test_a_lost_login_reply_is_retried_after_the_route_is_reset(env):
    env.repeater.drop_next_logins = 1
    row = env.run("name", password="password")
    assert row["state"] == "done"
    assert any(c[0] == companion.CMD_RESET_PATH for c in env.fake.commands)


def test_an_unreachable_repeater_fails_as_no_reply_without_hanging(env):
    env.repeater.reachable = False
    started = time.monotonic()
    row = env.run("name", password="password")
    assert row["state"] == "failed" and row["error_code"] == "no_reply" and time.monotonic() - started < 6
    env.repeater.reachable = True
    assert env.run("name", password="password")["state"] == "done"  # and the executor is free again


def test_a_flood_the_mesh_drops_is_a_no_reply_until_a_route_exists(env):
    env.repeater.drop_floods = True
    assert env.run("name", password="password")["error_code"] == "no_reply"


# --- the queue ------------------------------------------------------------------------------------------------------------


def test_the_contact_is_added_to_the_companion_once(env):
    env.run("name", password="password")
    env.run("beacon.window")
    env.run("beacon.names")
    adds = [c for c in env.fake.commands if c[0] == companion.CMD_ADD_UPDATE_CONTACT]
    assert len(adds) == 1 and adds[0][1:33] == RKEY and len(adds[0]) == 148
    assert env.fake.contacts[RKEY][1] == "ridge"


def test_jobs_run_in_order_one_at_a_time(env):
    ids = [env.submit("name", password="password"), env.submit("beacon.window"), env.submit("beacon.names")]
    rows = [env.wait(i) for i in ids]
    assert [r["state"] for r in rows] == ["done"] * 3
    assert rows[0]["started_at"] <= rows[0]["finished_at"] <= rows[1]["started_at"] <= rows[1]["finished_at"] <= rows[2]["started_at"]


def test_a_job_that_waited_past_its_expiry_never_runs(env):
    env.repeater.reachable = False
    blocker = env.submit("name", password="password")  # holds the executor for about a second
    stale = env.submit("name", "set", {"name": "never"}, password="password", ttl=0.3)
    env.wait(blocker)
    row = env.wait(stale)
    assert row["state"] == "expired" and row["error_code"] == "expired" and row["secret"] is None
    assert "set name never" not in env.repeater.commands and env.repeater.name == "ridge"


def test_a_repeater_whose_key_is_not_known_fails_with_no_key(env):
    env.store.add_repeater(bytes(range(40, 48)).hex(), 1.0, 2.0, name="mystery")  # by prefix only
    job_id = env.store.submit_job("mystery", "name", "get")
    row = env.wait(job_id)
    assert row["state"] == "failed" and row["error_code"] == "no_key"


def test_the_heartbeat_shows_the_job_being_run(env):
    env.repeater.reachable = False
    job_id = env.submit("name", password="password")
    until(lambda: env.store.status_row()["remote_state"].startswith(f"job {job_id}"))
    assert "get name" in env.store.status_row()["remote_state"]
    env.wait(job_id)
    until(lambda: env.store.status_row()["remote_state"] == "idle")


def test_reports_are_still_ingested_while_a_job_waits(env):
    env.repeater.reachable = False
    job_id = env.submit("name", password="password")
    until(lambda: env.store.job(job_id)["state"] == "running")
    for counter in range(1, 6):
        env.fake.enqueue_report(wire.encode_report(RKEY[:8], [wire.Observation(BEACON, counter, -90, -12, 3800)]))
    until(lambda: env.store.conn.execute("SELECT count(*) FROM observations WHERE status = 'accepted'").fetchone()[0] == 5)
    assert env.store.job(job_id)["state"] == "running"  # still waiting on the repeater
    assert env.wait(job_id)["error_code"] == "no_reply"


def test_losing_the_companion_mid_job_fails_it_as_interrupted_and_the_next_one_runs(env):
    assert env.run("name", password="password")["state"] == "done"
    env.repeater.reachable = False
    job_id = env.submit("name", "set", {"name": "half"})
    until(lambda: env.store.job(job_id)["state"] == "running")
    env.fake.disconnect()
    row = env.wait(job_id)
    assert row["state"] == "failed" and row["error_code"] == "interrupted"
    env.repeater.reachable = True
    env.fake.reconnect()
    until(lambda: env.store.status_row()["connected"] == 1 and env.session.stats["logins"] >= 1)
    assert env.run("name", password="password")["state"] == "done"


def test_jobs_left_running_by_a_crash_are_failed_at_startup(tmp_path):
    with Store.open(tmp_path / "beacon.db") as s:
        s.add_repeater(RKEY.hex(), 1.0, 2.0, name="ridge")
        job_id = s.submit_job("ridge", "name", "set", {"name": "x"}, password="pw")
        s.claim_job()
    with Store.open(tmp_path / "beacon.db") as s:  # the next start of beacon-ingest
        assert s.recover_jobs() == 1
        assert s.job(job_id)["error_code"] == "interrupted"


# --- the clock ------------------------------------------------------------------------------------------------------------


def test_an_untrusted_base_clock_blocks_jobs_until_it_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "CLAIM_INTERVAL", 0.05)
    e, (stop, thread, server) = build(tmp_path, assume_synced=False)
    try:
        row = e.run("name", password="password")
        assert row["state"] == "failed" and row["error_code"] == "clock_untrusted" and not e.repeater.commands
        assert not [c for c in e.fake.commands if c[0] == companion.CMD_SEND_LOGIN]
        with e.store.transaction() as db:  # beaconctl time set
            clock.apply_clock_event(db, clock.boot_id(), "set", 0.0, clock.offset(), time.time())
        row = e.run("name", password="password")
        assert row["state"] == "done"
        assert abs(e.fake.rtc() - time.time()) <= 3  # and the companion's clock was set forward to the base's
    finally:
        stop.set()
        thread.join(timeout=5)
        e.store.close()
        server.close()
        e.fake.close()


def test_a_login_works_after_a_companion_reboot_only_because_the_clock_was_set(env):
    assert env.run("name", password="password")["state"] == "done"
    now = int(time.time())
    env.repeater.acl[env.fake.public_key].last_timestamp = now - 5  # what the repeater saw from the companion before it rebooted
    env.fake.reconnect(reboot=True)  # the companion's clock is back in May 2024
    until(lambda: env.session.clock_synced and abs(env.fake.rtc() - time.time()) <= 3)
    row = env.run("name", password="password")
    assert row["state"] == "done"
