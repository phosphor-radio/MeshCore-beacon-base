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
