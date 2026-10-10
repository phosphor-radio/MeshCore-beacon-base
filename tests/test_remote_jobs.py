"""The remote job queue in the database: submit, claim, expire, finish and recover."""

import json

import pytest

from beacon_base import remote
from beacon_base.remote import JobOutcome
from beacon_base.store import Store, StoreError

from helpers import REPEATER_A_KEY, REPEATER_B_KEY


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "beacon.db") as s:
        s.add_repeater(REPEATER_A_KEY.hex(), 47.5, -122.25, name="ridge")
        s.add_repeater(REPEATER_B_KEY[:8].hex(), 1.0, 2.0)  # added by prefix: its full key is not known
        yield s


def prefix_a():
    return REPEATER_A_KEY[:8].hex()


def test_submit_checks_the_values_and_the_repeater(store):
    with pytest.raises(StoreError, match="whole number"):
        store.submit_job(prefix_a(), "beacon.window", "set", {"seconds": "five"})
    with pytest.raises(StoreError, match="cannot be set"):
        store.submit_job(prefix_a(), "beacon.channel", "set", {})
    with pytest.raises(StoreError, match="at most 15"):
        store.submit_job(prefix_a(), "name", "get", password="x" * 16)
    with pytest.raises(StoreError, match="no repeater matches"):
        store.submit_job("abcdef", "name", "get")
    assert store.jobs() == []


def test_a_job_is_found_by_name_or_prefix_and_stores_normalised_params(store):
    a = store.submit_job("ridge", "location", "set", {"lat": 47.1234567, "lon": -122.0})
    b = store.submit_job(prefix_a()[:8], "name", "get")
    assert (a, b) == (1, 2)
    row = store.job(a)
    assert row["state"] == "queued" and row["op"] == "set" and bytes(row["repeater_prefix"]) == REPEATER_A_KEY[:8]
    assert json.loads(row["params"]) == {"lat": 47.123457, "lon": -122.0, "allow_zero": False, "verify": True}


def test_claim_takes_the_oldest_job_marks_it_running_and_erases_the_password(store):
    first = store.submit_job("ridge", "name", "get", password="hunter2", now=100.0)
    store.submit_job("ridge", "beacon.window", "get", now=101.0)
    job = store.claim_job(now=102.0)
    assert (job.id, job.kind, job.op, job.password) == (first, "name", "get", "hunter2")
    assert job.pubkey == REPEATER_A_KEY and job.name == "ridge" and (job.lat, job.lon) == (47.5, -122.25)
    row = store.job(first)
    assert row["state"] == "running" and row["started_at"] == 102.0 and row["secret"] is None  # the table no longer holds it
    assert store.claim_job(now=103.0).kind == "beacon.window"
    assert store.claim_job(now=104.0) is None


def test_a_repeater_added_by_prefix_has_no_key_to_give_the_executor(store):
    store.submit_job(REPEATER_B_KEY[:8].hex(), "name", "get")
    assert store.claim_job().pubkey is None


def test_claiming_with_nothing_queued_writes_nothing(store):
    before = store.conn.total_changes
    for _ in range(5):
        assert store.claim_job() is None
    assert store.conn.total_changes == before  # polled four times a second: no write lock, no SD card wear


def test_a_job_that_waited_too_long_expires_without_keeping_its_password(store):
    old = store.submit_job("ridge", "name", "set", {"name": "late"}, password="hunter2", ttl=60.0, now=1000.0)
    fresh = store.submit_job("ridge", "name", "get", ttl=60.0, now=1050.0)
    job = store.claim_job(now=1061.0)  # the first expired a second ago, the second has not
    assert job.id == fresh
    row = store.job(old)
    assert row["state"] == "expired" and row["error_code"] == "expired" and row["secret"] is None and row["finished_at"] == 1061.0


def test_the_default_expiry_is_sixty_seconds(store):
    job_id = store.submit_job("ridge", "name", "get", now=500.0)
    assert store.job(job_id)["expires_at"] == 560.0


def test_finish_records_the_outcome(store):
    done = store.submit_job("ridge", "name", "get")
    failed = store.submit_job("ridge", "beacon.window", "get")
    store.claim_job()
    store.claim_job()
    store.finish_job(done, JobOutcome(True, result={"values": {"name": "ridge"}}), now=10.0)
    store.finish_job(failed, JobOutcome(False, "no_reply", "nobody home", {"round_trips": 0}), now=11.0)
    row = store.job(done)
    assert row["state"] == "done" and json.loads(row["result"]) == {"values": {"name": "ridge"}} and row["error_code"] is None and row["error"] is None
    row = store.job(failed)
    assert (row["state"], row["error_code"], row["error"], row["finished_at"]) == ("failed", "no_reply", "nobody home", 11.0)


def test_finish_does_not_touch_a_job_that_is_not_running(store):
    queued = store.submit_job("ridge", "name", "get")
    store.finish_job(queued, JobOutcome(True, result={}))
    assert store.job(queued)["state"] == "queued"


def test_jobs_left_running_by_a_crash_are_failed_as_interrupted(store):
    a = store.submit_job("ridge", "name", "set", {"name": "x"}, password="pw")
    b = store.submit_job("ridge", "name", "get")
    store.claim_job()
    assert store.recover_jobs(now=5.0) == 1
    row = store.job(a)
    assert (row["state"], row["error_code"], row["secret"]) == ("failed", "interrupted", None)
    assert store.job(b)["state"] == "queued"  # still waiting its turn
    assert store.recover_jobs() == 0


def test_listing_jobs(store):
    ids = [store.submit_job("ridge", "name", "get") for _ in range(3)]
    store.claim_job()
    assert [r["id"] for r in store.jobs()] == ids[::-1]
    assert [r["id"] for r in store.jobs(active_only=True)] == ids[::-1]
    store.finish_job(ids[0], JobOutcome(True, result={}))
    assert [r["id"] for r in store.jobs(active_only=True)] == ids[:0:-1]
    assert len(store.jobs(limit=2)) == 2


def test_removing_a_repeater_removes_its_jobs(store):
    store.submit_job("ridge", "name", "get")
    store.remove_repeater("ridge")
    assert store.jobs() == []
