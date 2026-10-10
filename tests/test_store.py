
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
        assert {"beacons", "repeaters", "raw_frames", "observations", "transmissions", "clock_events", "beacon_names"} <= tables
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
    b = store.add_beacon(BEACON_KEY[:8].hex(), "yellow tag", now=5.0)
    assert bytes(b["prefix"]) == BEACON_KEY[:8]
    assert b["hwm"] is None and b["enabled"] == 1 and b["notes"] == "yellow tag" and b["name"] is None


def test_a_full_key_is_accepted_but_only_its_prefix_is_kept(store):
    b = store.add_beacon(BEACON_KEY.hex().upper())
    assert bytes(b["prefix"]) == BEACON_KEY[:8]
    assert "pubkey" not in b.keys()


def test_duplicate_prefix_is_refused(store):
    store.add_beacon(BEACON_KEY[:8].hex())
    with pytest.raises(StoreError, match="already on the allowlist"):
        store.add_beacon(BEACON_KEY[:8].hex())
    with pytest.raises(StoreError, match="already on the allowlist"):
        store.add_beacon(BEACON_KEY.hex())  # a full key with the same prefix
    assert len(store.beacons()) == 1


@pytest.mark.parametrize("bad", ["zz", "00" * 7, "00" * 9, "00" * 31, "00" * 33, ""])
def test_beacon_key_validation(store, bad):
    with pytest.raises(StoreError):
        store.add_beacon(bad)


# --- finding beacons by prefix ---------------------------------------------------------------------------------------------


@pytest.fixture
def three(store):
    for key in ("f5b165224a58b791", "f5b16f0000000001", "7bd5d47e446fcec2"):
        store.add_beacon(key)
    return store


def test_a_beacon_is_found_by_its_prefix_or_the_start_of_it(three):
    for ref in ("f5b165224a58b791", "F5B165224A58B791", "f5b165224a58", "f5b165", "7bd5d4", "7bd5d47e446fcec2" + "00" * 24):
        assert bytes(three.beacon(ref)["prefix"]).hex() in ("f5b165224a58b791", "7bd5d47e446fcec2"), ref


def test_an_ambiguous_prefix_names_the_matches(three):
    assert bytes(three.beacon("f5b165")["prefix"]).hex() == "f5b165224a58b791"  # unique so far
    three.add_beacon("f5b165ffffffffff")
    with pytest.raises(StoreError, match="matches 2 beacons") as e:
        three.beacon("f5b165")
    assert "f5b165224a58b791".startswith("f5b165") and "f5b165" in str(e.value)
    assert bytes(three.beacon("f5b1652")["prefix"]).hex() == "f5b165224a58b791"  # one more digit tells them apart


def test_bad_references(three):
    with pytest.raises(StoreError, match="at least 6"):
        three.beacon("f5b1")
    with pytest.raises(StoreError, match="not a hex"):
        three.beacon("beacon-f5b165")
    with pytest.raises(StoreError, match="not a hex"):
        three.beacon("")
    with pytest.raises(StoreError, match="no beacon on the allowlist matches"):
        three.beacon("aabbccdd")
    for call in (three.remove_beacon, three.reset_beacon):
        with pytest.raises(StoreError, match="no beacon"):
            call("aabbccdd")


def test_a_name_never_finds_a_beacon(three):
    with three.transaction():
        three.record_name(bytes.fromhex("7bd5d47e446fcec2"), "Roof", bytes(8))
    with pytest.raises(StoreError, match="not a hex"):
        three.beacon("Roof")


# --- names ---------------------------------------------------------------------------------------------------------------


def test_announced_names_attach_to_beacons_and_the_latest_wins(store):
    p = BEACON_KEY[:8]
    store.add_beacon(p.hex())
    assert store.beacon(p.hex())["name"] is None
    with store.transaction():
        assert store.record_name(p, "beacon-010203", bytes(8), now=10.0) is None
        assert store.record_name(p, "Roof", bytes([1] * 8), now=20.0) == "beacon-010203"
    assert store.beacon(p.hex())["name"] == "Roof"
    row = store.conn.execute("SELECT * FROM beacon_names").fetchone()
    assert (row["first_seen"], row["updated_at"], bytes(row["repeater_prefix"])) == (10.0, 20.0, bytes([1] * 8))
    assert store.beacon_name(p) == "Roof" and store.beacon_name(bytes(8)) is None


def test_names_exist_before_the_beacon_is_added_and_survive_its_removal(store):
    p = BEACON2_KEY[:8]
    with store.transaction():
        store.record_name(p, "Shed", bytes(8))
    assert store.add_beacon(p.hex())["name"] == "Shed"
    store.remove_beacon(p.hex())
    assert store.beacon_name(p) == "Shed"


def test_names_are_not_unique(store):
    for key in (BEACON_KEY, BEACON2_KEY):
        store.add_beacon(key.hex())
        with store.transaction():
            store.record_name(key[:8], "beacon", bytes(8))
    assert [b["name"] for b in store.beacons()] == ["beacon", "beacon"]


def test_beacons_are_listed_by_name_then_prefix_with_unnamed_last(store):
    keys = {"b" * 16: "Zebra", "a" * 16: "apple", "c" * 16: None, "d" * 16: None, "9" * 16: "apple"}
    for prefix, name in keys.items():
        store.add_beacon(prefix)
        if name:
            with store.transaction():
                store.record_name(bytes.fromhex(prefix), name, bytes(8))
    assert [(b["name"], bytes(b["prefix"]).hex()[0]) for b in store.beacons()] == [
        ("apple", "9"), ("apple", "a"), ("Zebra", "b"), (None, "c"), (None, "d"),
    ]


# --- repeaters -----------------------------------------------------------------------------------------------------------


def test_add_repeater_with_prefix_or_full_key_and_an_optional_name(store):
    r1 = store.add_repeater(REPEATER_A_KEY[:8].hex(), 40.1, -75.2, name="north", window_s=20)
    assert r1["pubkey"] is None and r1["window_s"] == 20 and r1["name"] == "north"
    r2 = store.add_repeater(REPEATER_A_KEY[8:].hex() + "00" * 8, 40.0, -75.0)
    assert r2["name"] is None
    full = bytes(range(0x50, 0x70))
    r3 = store.add_repeater(full.hex(), 40.0, -75.0, name="east")
    assert bytes(r3["prefix"]) == full[:8] and bytes(r3["pubkey"]) == full
    assert r2["window_s"] is None and len(store.repeaters()) == 3


def test_repeater_names_are_cleaned_and_need_not_be_unique(store):
    r = store.add_repeater("11" * 8, 1, 1, name="  North	Ridge ")
    assert r["name"] == "North Ridge"
    assert store.add_repeater("22" * 8, 1, 1, name="North Ridge")["name"] == "North Ridge"
    assert store.add_repeater("33" * 8, 1, 1, name="")["name"] is None  # nothing left after cleaning


def test_repeater_validation(store):
    store.add_repeater(REPEATER_A_KEY[:8].hex(), 40.0, -75.0, name="north")
    with pytest.raises(StoreError, match="already in the repeater table"):
        store.add_repeater(REPEATER_A_KEY[:8].hex(), 40.0, -75.0, name="other")
    for lat, lon in ((91, 0), (-91, 0), (0, 181), (0, -181)):
        with pytest.raises(StoreError):
            store.add_repeater("22" * 8, lat, lon)
    with pytest.raises(StoreError):
        store.add_repeater("22" * 12, 0, 0)  # neither a prefix nor a key
    with pytest.raises(StoreError):
        store.add_repeater("22" * 8, 0, 0, window_s=0)


def test_repeaters_are_found_by_prefix_or_name(store):
    store.add_repeater("a1b2c3d4e5f60708", 1, 1, name="North Ridge")
    store.add_repeater("a1b2c3ffffffffff", 1, 1, name="north ridge")  # same name apart from case
    store.add_repeater("0f0f0f0f0f0f0f0f", 1, 1)
    assert bytes(store.repeater("0f0f0f")["prefix"]).hex() == "0f0f0f0f0f0f0f0f"
    assert bytes(store.repeater("a1b2c3d4")["prefix"]).hex() == "a1b2c3d4e5f60708"
    with pytest.raises(StoreError, match="matches 2 repeaters"):
        store.repeater("a1b2c3")
    with pytest.raises(StoreError, match="matches 2 repeaters"):
        store.repeater("North Ridge")  # two repeaters share the name
    with pytest.raises(StoreError, match="no repeater matches"):
        store.repeater("nope")
    with pytest.raises(StoreError, match="no repeater matches"):
        store.repeater("0f0f")  # too short to be a prefix


def test_a_repeater_name_that_looks_like_hex_is_still_found_by_name(store):
    store.add_repeater("11" * 8, 1, 1, name="deadbeef")
    assert bytes(store.repeater("deadbeef")["prefix"]) == bytes.fromhex("11" * 8)


def test_enable_disable_remove_and_window(store):
    store.add_beacon(BEACON_KEY.hex())
    store.set_beacon_enabled(BEACON_KEY[:8].hex(), False)
    assert store.beacon(BEACON_KEY[:8].hex())["enabled"] == 0
    store.add_repeater("11" * 8, 1, 1, name="r")
    store.set_repeater_enabled("r", False)
    assert store.repeater("r")["enabled"] == 0
    store.set_repeater_window("111111", 15)
    assert store.repeater("r")["window_s"] == 15
    store.set_repeater_window("r", None)
    assert store.repeater("r")["window_s"] is None
    with pytest.raises(StoreError):
        store.set_repeater_window("r", -1)
    store.remove_repeater("r")
    assert store.repeaters() == []


def test_concurrent_migration_is_safe(tmp_path):
    a = Store.open(tmp_path / "t.db")
    b = Store.open(tmp_path / "t.db")
    assert a.schema_version() == b.schema_version() == len(MIGRATIONS)
    a.close()
    b.close()


# --- bulk operations ----------------------------------------------------------------------------------------------------


def test_bulk_enable_disable_remove_and_reset(store):
    prefixes = [bytes([i + 1] * 8) for i in range(3)]
    for p in prefixes:
        store.add_beacon(p.hex())
    assert len(store.set_all_beacons_enabled(False)) == 3
    assert all(b["enabled"] == 0 for b in store.beacons())
    assert len(store.set_all_beacons_enabled(True)) == 3
    assert all(b["enabled"] == 1 for b in store.beacons())

    store.conn.execute("UPDATE beacons SET hwm = 50, rejects_since_accept = 2, last_reject_counter = 7 WHERE prefix = ?", (prefixes[1],))
    infos = store.reset_all_beacons()
    assert [i.prefix for i in infos] == prefixes
    assert (infos[1].old_hwm, infos[1].rejects_since_accept, infos[1].last_reject_counter) == (50, 2, 7)
    assert all(b["hwm"] is None and b["epoch"] == 1 and b["rejects_since_accept"] == 0 for b in store.beacons())

    assert len(store.remove_all_beacons()) == 3
    assert store.beacons() == []


def test_bulk_operations_on_an_empty_allowlist(store):
    assert store.set_all_beacons_enabled(True) == [] and store.reset_all_beacons() == [] and store.remove_all_beacons() == []


def _report_unknown(store, *prefixes, t=1000.0):
    from beacon_base.pipeline import Pipeline
    from helpers import REPEATER_A_KEY, obs, rx

    if not store.conn.execute("SELECT 1 FROM repeaters").fetchone():
        store.add_repeater(REPEATER_A_KEY.hex(), 1, 1, name="r")
    Pipeline(store, boot="b").process(rx(REPEATER_A_KEY, *[obs(1, beacon=p) for p in prefixes], t=t))


def test_add_heard_beacons_adds_only_unlisted_ones(store):
    known, new1, new2 = bytes([1] * 8), bytes.fromhex("f5b165224a58b791"), bytes.fromhex("7bd5d47e446fcec2")
    store.add_beacon(known.hex())
    _report_unknown(store, known, new1, new2, new1)
    added = store.add_heard_beacons(since=0, now=5.0)
    assert [bytes(r["prefix"]) for r in added] == [new2, new1]
    assert {bytes(b["prefix"]) for b in store.beacons()} == {known, new1, new2}
    assert store.add_heard_beacons(since=0) == []  # nothing left


def test_add_heard_beacons_picks_up_announced_names(store):
    p = bytes.fromhex("f5b165224a58b791")
    with store.transaction():
        store.record_name(p, "Roof", bytes(8))
    _report_unknown(store, p)
    (added,) = store.add_heard_beacons(since=0)
    assert added["name"] == "Roof"


def test_add_heard_beacons_respects_the_time_window(store):
    old, recent = bytes([9] * 8), bytes([8] * 8)
    _report_unknown(store, old, t=100.0)
    _report_unknown(store, recent, t=5000.0)
    assert [bytes(r["prefix"]) for r in store.add_heard_beacons(since=1000.0)] == [recent]


def test_add_heard_beacons_ignores_other_statuses(store):
    from beacon_base.pipeline import Pipeline
    from helpers import obs, rx

    store.add_beacon(BEACON_KEY.hex())
    store.set_beacon_enabled(BEACON_KEY[:8].hex(), False)
    store.add_repeater(bytes(range(0xA0, 0xC0)).hex(), 1, 1, name="r")
    rogue = bytes(range(0x30, 0x50))
    Pipeline(store, boot="b").process(rx(rogue, obs(1, beacon=BEACON_KEY[:8]), t=10.0))  # unknown repeater, known beacon
    assert store.add_heard_beacons(since=0) == []
