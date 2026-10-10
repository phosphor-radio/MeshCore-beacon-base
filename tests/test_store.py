
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
