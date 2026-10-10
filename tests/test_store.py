
import pytest

from beacon_base.store import MIGRATIONS, Store, StoreError

from helpers import BEACON2_KEY, BEACON_KEY, REPEATER_A_KEY


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        yield s


def test_fresh_database_is_migrated_to_the_latest_version(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        assert s.schema_version() == len(MIGRATIONS)
        tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"beacons", "repeaters", "raw_frames", "observations", "transmissions", "clock_events"} <= tables
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with Store.open(tmp_path / "t.db") as s:  # reopening is a no-op
        assert s.schema_version() == len(MIGRATIONS)


def test_database_from_a_newer_version_is_refused(tmp_path):
    with Store.open(tmp_path / "t.db") as s:
        s.conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    with pytest.raises(StoreError, match="newer"):
        Store.open(tmp_path / "t.db")


def test_failed_transaction_rolls_back(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            db.execute("INSERT INTO clock_events (at_wall, boot_id, kind, offset_before, offset_after) VALUES (1, 'b', 'set', 0, 0)")
            raise RuntimeError("boom")
    assert store.conn.execute("SELECT count(*) FROM clock_events").fetchone()[0] == 0


def test_add_beacon_by_prefix(store):
    b = store.add_beacon("beacon-001", BEACON_KEY[:8].hex(), "yellow tag", now=5.0)
    assert bytes(b["prefix"]) == BEACON_KEY[:8]
    assert b["hwm"] is None and b["enabled"] == 1 and b["notes"] == "yellow tag"


def test_a_full_key_is_accepted_but_only_its_prefix_is_kept(store):
    b = store.add_beacon("beacon-001", BEACON_KEY.hex().upper())
    assert bytes(b["prefix"]) == BEACON_KEY[:8]
    assert "pubkey" not in b.keys()


def test_duplicate_prefix_and_name_are_refused(store):
    store.add_beacon("a", BEACON_KEY[:8].hex())
    with pytest.raises(StoreError, match="already has the prefix"):
        store.add_beacon("b", BEACON_KEY[:8].hex())
    with pytest.raises(StoreError, match="already has the prefix"):
        store.add_beacon("b", BEACON_KEY.hex())  # same prefix as a full key
    with pytest.raises(StoreError, match="already exists"):
        store.add_beacon("a", BEACON2_KEY[:8].hex())
    assert len(store.beacons()) == 1


@pytest.mark.parametrize("bad", ["zz", "00" * 7, "00" * 9, "00" * 31, "00" * 33, ""])
def test_beacon_key_validation(store, bad):
    with pytest.raises(StoreError):
        store.add_beacon("a", bad)


@pytest.mark.parametrize("name", ["", " ", "has space"])
def test_name_validation(store, name):
    with pytest.raises(StoreError):
        store.add_beacon(name, BEACON_KEY.hex())


def test_unknown_names_raise(store):
    for call in (store.beacon, store.remove_beacon, store.reset_beacon, store.repeater, store.remove_repeater):
        with pytest.raises(StoreError, match="no "):
            call("nope")


def test_add_repeater_with_prefix_or_full_key(store):
    r1 = store.add_repeater("north", REPEATER_A_KEY[:8].hex(), 40.1, -75.2, window_s=20)
    assert r1["pubkey"] is None and r1["window_s"] == 20
    r2 = store.add_repeater("south", REPEATER_A_KEY[8:].hex() + "00" * 8, 40.0, -75.0)
    assert len(store.repeaters()) == 2
    full = bytes(range(0x50, 0x70))
    r3 = store.add_repeater("east", full.hex(), 40.0, -75.0)
    assert bytes(r3["prefix"]) == full[:8] and bytes(r3["pubkey"]) == full
    assert r2["window_s"] is None


def test_repeater_validation(store):
    store.add_repeater("north", REPEATER_A_KEY[:8].hex(), 40.0, -75.0)
    with pytest.raises(StoreError, match="already has the prefix"):
        store.add_repeater("other", REPEATER_A_KEY[:8].hex(), 40.0, -75.0)
    with pytest.raises(StoreError, match="already exists"):
        store.add_repeater("north", "11" * 8, 40.0, -75.0)
    for lat, lon in ((91, 0), (-91, 0), (0, 181), (0, -181)):
        with pytest.raises(StoreError):
            store.add_repeater("x", "22" * 8, lat, lon)
    with pytest.raises(StoreError):
        store.add_repeater("x", "22" * 12, 0, 0)  # neither a prefix nor a key
    with pytest.raises(StoreError):
        store.add_repeater("x", "22" * 8, 0, 0, window_s=0)


def test_enable_disable_and_window(store):
    store.add_beacon("a", BEACON_KEY.hex())
    store.set_beacon_enabled("a", False)
    assert store.beacon("a")["enabled"] == 0
    store.add_repeater("r", "11" * 8, 1, 1)
    store.set_repeater_enabled("r", False)
    assert store.repeater("r")["enabled"] == 0
    store.set_repeater_window("r", 15)
    assert store.repeater("r")["window_s"] == 15
    store.set_repeater_window("r", None)
    assert store.repeater("r")["window_s"] is None
    with pytest.raises(StoreError):
        store.set_repeater_window("r", -1)


def test_concurrent_migration_is_safe(tmp_path):
    a = Store.open(tmp_path / "t.db")
    b = Store.open(tmp_path / "t.db")
    assert a.schema_version() == b.schema_version() == len(MIGRATIONS)
    a.close()
    b.close()


# --- bulk operations ----------------------------------------------------------------------------------------------------


def test_bulk_enable_disable_remove_and_reset(store):
    for i, name in enumerate(("a", "b", "c")):
        store.add_beacon(name, bytes([i + 1] * 8).hex())
    assert store.set_all_beacons_enabled(False) == ["a", "b", "c"]
    assert all(b["enabled"] == 0 for b in store.beacons())
    assert store.set_all_beacons_enabled(True) == ["a", "b", "c"]
    assert all(b["enabled"] == 1 for b in store.beacons())

    store.conn.execute("UPDATE beacons SET hwm = 50, rejects_since_accept = 2, last_reject_counter = 7 WHERE name = 'b'")
    infos = store.reset_all_beacons()
    assert [i.name for i in infos] == ["a", "b", "c"]
    assert (infos[1].old_hwm, infos[1].rejects_since_accept, infos[1].last_reject_counter) == (50, 2, 7)
    assert all(b["hwm"] is None and b["epoch"] == 1 and b["rejects_since_accept"] == 0 for b in store.beacons())

    assert store.remove_all_beacons() == ["a", "b", "c"]
    assert store.beacons() == []


def test_bulk_operations_on_an_empty_allowlist(store):
    assert store.set_all_beacons_enabled(True) == [] and store.reset_all_beacons() == [] and store.remove_all_beacons() == []


def _report_unknown(store, *prefixes, t=1000.0, status="unknown_beacon"):
    from beacon_base.pipeline import Pipeline
    from helpers import REPEATER_A_KEY, obs, rx

    if not store.conn.execute("SELECT 1 FROM repeaters").fetchone():
        store.add_repeater("r", REPEATER_A_KEY.hex(), 1, 1)
    Pipeline(store, boot="b").process(rx(REPEATER_A_KEY, *[obs(1, beacon=p) for p in prefixes], t=t))


def test_add_heard_beacons_adds_only_unlisted_ones_with_derived_names(store):
    known, new1, new2 = bytes([1] * 8), bytes.fromhex("f5b165224a58b791"), bytes.fromhex("7bd5d47e446fcec2")
    store.add_beacon("mine", known.hex())
    _report_unknown(store, known, new1, new2, new1)
    added = store.add_heard_beacons(since=0, now=5.0)
    assert [(r["name"], bytes(r["prefix"])) for r in added] == [("beacon-7bd5d4", new2), ("beacon-f5b165", new1)]
    assert {b["name"] for b in store.beacons()} == {"mine", "beacon-7bd5d4", "beacon-f5b165"}
    assert store.add_heard_beacons(since=0) == []  # nothing left


def test_add_heard_beacons_respects_the_time_window_and_prefix_option(store):
    old, recent = bytes([9] * 8), bytes([8] * 8)
    _report_unknown(store, old, t=100.0)
    _report_unknown(store, recent, t=5000.0)
    added = store.add_heard_beacons(since=1000.0, name_prefix="tag")
    assert [r["name"] for r in added] == ["tag-080808"]


def test_add_heard_beacons_finds_a_free_name(store):
    p = bytes.fromhex("f5b165224a58b791")
    store.add_beacon("beacon-f5b165", bytes([3] * 8).hex())  # the derived name is taken by another beacon
    _report_unknown(store, p)
    (added,) = store.add_heard_beacons(since=0)
    assert added["name"] == "beacon-f5b16522"


def test_add_heard_beacons_ignores_disabled_and_other_statuses(store):
    from helpers import BEACON_PREFIX

    rogue = bytes(range(0x30, 0x50))
    store.add_beacon("b", BEACON_PREFIX.hex())
    store.set_beacon_enabled("b", False)
    from beacon_base.pipeline import Pipeline
    from helpers import obs, rx

    store.add_repeater("r", bytes(range(0xA0, 0xC0)).hex(), 1, 1)
    Pipeline(store, boot="b").process(rx(rogue, obs(1, beacon=BEACON_PREFIX), t=10.0))  # unknown repeater, known beacon
    assert store.add_heard_beacons(since=0) == []
