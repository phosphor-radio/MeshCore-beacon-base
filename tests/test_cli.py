import json
import signal
import stat

import pytest

from beacon_base import wire
from beacon_base.cli import main
from beacon_base.fake_companion import FakeCompanion
from beacon_base.simulate import Simulator


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("")
    return str(p)


@pytest.fixture(autouse=True)
def restore_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def test_channel_generate_and_show(cfg_file, tmp_path, capsys):
    assert main(["-c", cfg_file, "channel", "generate"]) == 0
    out = capsys.readouterr().out
    secrets_file = tmp_path / "secrets.toml"
    assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600
    assert main(["-c", cfg_file, "channel", "show"]) == 0
    key = capsys.readouterr().out.strip()
    assert len(bytes.fromhex(key)) == 16
    assert f"beacon.channel {key}" in out


def test_channel_generate_refuses_to_replace(cfg_file, capsys):
    assert main(["-c", cfg_file, "channel", "generate"]) == 0
    capsys.readouterr()
    assert main(["-c", cfg_file, "channel", "show"]) == 0
    before = capsys.readouterr().out
    assert main(["-c", cfg_file, "channel", "generate"]) == 2
    assert "already exists" in capsys.readouterr().err
    main(["-c", cfg_file, "channel", "show"])
    assert capsys.readouterr().out == before
    assert main(["-c", cfg_file, "channel", "generate", "--force"]) == 0
    capsys.readouterr()
    main(["-c", cfg_file, "channel", "show"])
    assert capsys.readouterr().out != before


KEY = "000102030405060708090a0b0c0d0e0f"


def test_channel_set_stores_key_privately(cfg_file, tmp_path, capsys):
    assert main(["-c", cfg_file, "channel", "set", KEY]) == 0
    assert stat.S_IMODE((tmp_path / "secrets.toml").stat().st_mode) == 0o600
    capsys.readouterr()
    assert main(["-c", cfg_file, "channel", "show"]) == 0
    assert capsys.readouterr().out.strip() == KEY


def test_channel_set_requires_force_to_overwrite(cfg_file, capsys):
    other = "ff" * 16
    assert main(["-c", cfg_file, "channel", "set", KEY]) == 0
    capsys.readouterr()
    assert main(["-c", cfg_file, "channel", "set", other]) == 2
    assert "--force" in capsys.readouterr().err
    main(["-c", cfg_file, "channel", "show"])
    assert capsys.readouterr().out.strip() == KEY
    assert main(["-c", cfg_file, "channel", "set", other, "--force"]) == 0
    capsys.readouterr()
    main(["-c", cfg_file, "channel", "show"])
    assert capsys.readouterr().out.strip() == other


def test_channel_set_same_key_is_not_an_overwrite(cfg_file, capsys):
    assert main(["-c", cfg_file, "channel", "set", KEY]) == 0
    assert main(["-c", cfg_file, "channel", "set", KEY]) == 0
    assert "already holds" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["zz", "00", "00" * 32, ""])
def test_channel_set_rejects_bad_keys(cfg_file, tmp_path, capsys, bad):
    assert main(["-c", cfg_file, "channel", "set", bad]) == 2
    assert not (tmp_path / "secrets.toml").exists()


def test_channel_set_reads_stdin(cfg_file, monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["-c", cfg_file, "channel", "set", "-"]) == 0
    capsys.readouterr()
    main(["-c", cfg_file, "channel", "show"])
    assert capsys.readouterr().out.strip() == KEY


def test_show_without_key_fails(cfg_file, capsys):
    assert main(["-c", cfg_file, "channel", "show"]) == 2
    assert "channel generate" in capsys.readouterr().err


def test_listen_without_port_or_key_fails(cfg_file, capsys):
    assert main(["-c", cfg_file, "listen"]) == 2
    assert "channel" in capsys.readouterr().err
    main(["-c", cfg_file, "channel", "generate"])
    capsys.readouterr()
    assert main(["-c", cfg_file, "listen"]) == 2
    assert "port" in capsys.readouterr().err


def test_listen_prints_simulated_observations(cfg_file, capsys):
    main(["-c", cfg_file, "channel", "generate"])
    capsys.readouterr()
    with FakeCompanion() as fake:
        sim = Simulator(fake, beacons=2, repeaters=2, seed=7)
        fake.send_push_on_enqueue = False
        while len(fake.queue) < 2:
            sim.tick()
        assert main(["-c", cfg_file, "listen", "--port", fake.path, "--count", "3"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 3
    for line in lines:
        assert " late " in line  # drained from the queue right after connecting
        assert "repeater=" in line and "beacon=" in line and "ctr=" in line and "rssi=" in line
    assert any(b.hex() in lines[0] for b in sim.beacon_ids)


def test_listen_json(cfg_file, capsys):
    main(["-c", cfg_file, "channel", "generate"])
    capsys.readouterr()
    with FakeCompanion() as fake:
        fake.send_push_on_enqueue = False
        obs = wire.Observation(bytes(range(8)), 42, -101, -9, 3777)
        fake.enqueue_report(wire.encode_report(bytes(range(32)), [obs]), path_len=0xFF)
        assert main(["-c", cfg_file, "listen", "--port", fake.path, "--count", "1", "--json"]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["beacon"] == "0001020304050607" and rec["counter"] == 42
    assert (rec["rssi"], rec["snr"], rec["batt_mv"]) == (-101, -2.25, 3777)
    assert rec["hops"] == "direct" and rec["late"] is True


def test_listen_prints_name_announcements(cfg_file, capsys):
    main(["-c", cfg_file, "channel", "generate"])
    capsys.readouterr()
    with FakeCompanion() as fake:
        fake.send_push_on_enqueue = False
        key = bytes(range(32))
        fake.enqueue_report(
            wire.encode_names(key, [wire.NameEntry(bytes(range(8)), b"Roof"), wire.NameEntry(bytes(range(8, 16)), b"\x07")]),
            data_type=wire.NAMES_DATA_TYPE,
        )
        fake.enqueue_report(wire.encode_report(key, [wire.Observation(bytes(range(8)), 1, -90, -8, 3800)]))
        assert main(["-c", cfg_file, "listen", "--port", fake.path, "--count", "1"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2  # the empty-after-cleaning name is not printed
    assert "beacon=0001020304050607 name='Roof'" in lines[0] and "repeater=0001020304050607" in lines[0]
    assert "ctr=1" in lines[1]


def test_listen_json_includes_names(cfg_file, capsys):
    main(["-c", cfg_file, "channel", "generate"])
    capsys.readouterr()
    with FakeCompanion() as fake:
        fake.send_push_on_enqueue = False
        key = bytes(range(32))
        fake.enqueue_report(wire.encode_names(key, [wire.NameEntry(bytes(range(8)), b"Roof")]), data_type=wire.NAMES_DATA_TYPE)
        fake.enqueue_report(wire.encode_report(key, [wire.Observation(bytes(range(8)), 1, -90, -8, 3800)]))
        assert main(["-c", cfg_file, "listen", "--port", fake.path, "--count", "1", "--json"]) == 0
    recs = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert recs[0]["type"] == "name" and recs[0]["name"] == "Roof" and recs[0]["beacon"] == "0001020304050607"
    assert "type" not in recs[1] and recs[1]["counter"] == 1


def test_the_simulator_announces_names_in_packets_that_fit(cfg_file):
    with FakeCompanion() as fake:
        fake.send_push_on_enqueue = False
        sim = Simulator(fake, beacons=20, repeaters=2, seed=3)
        n = sim.announce_names()
        assert n == len(fake.queue) and n >= 4  # 20 beacons need several packets per repeater
        seen = {}
        from beacon_base import companion

        for frame in fake.queue:
            data = companion.parse_channel_data(frame)
            assert data.data_type == wire.NAMES_DATA_TYPE and len(data.payload) <= wire.MAX_GROUP_DATA_LENGTH
            for e in wire.decode_names(data.payload).entries:
                seen[e.beacon_id] = e.name.decode()
        assert seen == {bid: sim.beacon_name(i) for i, bid in enumerate(sim.beacon_ids)}
        assert any(v.startswith("beacon-") for v in seen.values()) and any(v.startswith("sim-beacon-") for v in seen.values())
