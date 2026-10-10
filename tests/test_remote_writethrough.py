"""What a finished remote job writes into the repeater's record at the base, and what it must not."""

import json
import os
import stat

import pytest

from beacon_base import remote
from beacon_base.remote import JobOutcome
from beacon_base.store import Store

from helpers import REPEATER_A_KEY

PREFIX = REPEATER_A_KEY[:8].hex()  # a rename is written through, so the tests address the repeater by prefix


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "beacon.db") as s:
        s.add_repeater(REPEATER_A_KEY.hex(), 40.0, -75.0, name="north", window_s=20.0)
        yield s


def finish(store, kind, op, params, result, ok=True, **kw):
    job_id = store.submit_job(PREFIX, kind, op, params, **kw)
    store.claim_job()
    outcome = JobOutcome(ok, None if ok else "mismatch", "", result)
    store.finish_job(job_id, outcome, now=5000.0)
    return json.loads(store.job(job_id)["result"]), store.repeater(PREFIX)


def test_a_confirmed_name_is_recorded_as_sent(store):
    result, r = finish(store, "name", "set", {"name": "hilltop"}, {"verified": True, "sent": "hilltop", "values": {"name": "hilltop"}})
    assert r["name"] == "hilltop" and result["recorded"] == ["name"]


def test_a_confirmed_position_is_the_value_sent_not_the_lossy_read_back(store):
    result, r = finish(
        store, "location", "set", {"lat": 47.123456, "lon": -122.335167},
        {"verified": True, "sent": {"lat": 47.123456, "lon": -122.335167}, "values": {"location": {"lat": 47.123455, "lon": -122.3351669}}},
    )
    assert (r["lat"], r["lon"], r["location_source"], r["location_updated_at"]) == (47.123456, -122.335167, "remote", 5000.0)
    assert result["recorded"] == ["position"]


def test_a_position_set_to_zero_zero_is_unlocated_again(store):
    _, r = finish(store, "location", "set", {"lat": 0, "lon": 0, "allow_zero": True}, {"verified": True, "sent": {"lat": 0.0, "lon": 0.0}, "values": {}})
    assert (r["lat"], r["lon"], r["location_source"]) == (0.0, 0.0, "none") and not Store.is_located(r["lat"], r["lon"])


def test_a_confirmed_window_is_recorded_for_check(store):
    result, r = finish(store, "beacon.window", "set", {"seconds": 5}, {"verified": True, "sent": 5, "values": {"beacon.window": 5}})
    assert r["window_s"] == 5.0 and result["recorded"] == ["window"]


def test_a_set_that_was_not_read_back_or_did_not_match_records_nothing(store):
    result, r = finish(store, "name", "set", {"name": "x", "verify": False}, {"verified": False, "sent": "x", "values": {"name": "x"}})
    assert r["name"] == "north" and "recorded" not in result
    result, r = finish(store, "beacon.window", "set", {"seconds": 5}, {"verified": False, "sent": 5, "values": {}}, ok=False)
    assert r["window_s"] == 20.0 and "recorded" not in result


def test_reading_the_name_and_window_records_them_but_reading_the_position_does_not(store):
    result, r = finish(store, "all", "get", {}, {"values": {
        "name": "ridge", "beacon.window": 45, "location": {"lat": 1.5, "lon": 2.5}, "advert.interval": 60}})
    assert r["name"] == "ridge" and r["window_s"] == 45.0 and result["recorded"] == ["name", "window"]
    assert (r["lat"], r["lon"]) == (40.0, -75.0)  # a lossy read-back is never taken for a position


def test_values_that_have_not_changed_are_not_reported_as_recorded(store):
    result, _ = finish(store, "name", "get", {}, {"values": {"name": "north"}})
    assert "recorded" not in result
    result, _ = finish(store, "beacon.window", "get", {}, {"values": {"beacon.window": 20}})
    assert "recorded" not in result


def test_a_name_read_from_a_repeater_is_cleaned_like_an_advertised_one(store):
    _, r = finish(store, "name", "get", {}, {"values": {"name": "  ridge \n top "}})
    assert r["name"] == "ridge top"
    _, r = finish(store, "name", "get", {}, {"values": {"name": ""}})
    assert r["name"] == "ridge top"  # an empty name does not erase one


def test_a_failed_job_writes_nothing_through(store):
    result, r = finish(store, "name", "get", {}, {"values": {"name": "other"}}, ok=False)
    assert r["name"] == "north"


# --- the password does not outlive the job ---------------------------------------------------------------------------------


def test_a_new_database_is_private_and_an_existing_one_is_left_alone(tmp_path):
    new = tmp_path / "new" / "beacon.db"
    with Store.open(new):
        pass
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    old = tmp_path / "old.db"
    old.write_bytes(b"")
    os.chmod(old, 0o640)
    with Store.open(old):
        pass
    assert stat.S_IMODE(old.stat().st_mode) == 0o640


def test_the_password_is_gone_from_the_database_files_once_the_job_is_picked_up(tmp_path):
    secret = "Zq7-unmistaken"
    path = tmp_path / "beacon.db"
    with Store.open(path) as s, Store.open(path) as ingest:
        s.add_repeater(REPEATER_A_KEY.hex(), 40.0, -75.0, name="north")
        job_id = s.submit_job(PREFIX, "name", "get", password=secret)
        assert secret in (s.job(job_id)["secret"] or "")
        assert ingest.claim_job().password == secret
        ingest.finish_job(job_id, JobOutcome(True, result={"values": {"name": "north"}}))
        s.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        files = [p for p in tmp_path.iterdir() if p.name.startswith("beacon.db")]
        assert files
        for f in files:
            assert secret.encode() not in f.read_bytes(), f.name
