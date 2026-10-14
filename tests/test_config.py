import os
import stat

import pytest

from beacon_base import config
from beacon_base.config import ConfigError, load_config, store_channel_key


def write(path, text):
    path.write_text(text)
    return path


def test_defaults_when_default_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.companion.port is None
    assert cfg.companion.channel_index == 1
    assert (cfg.radio.freq_khz, cfg.radio.bw_hz, cfg.radio.sf, cfg.radio.cr) == (905775, 62500, 8, 6)
    assert cfg.channel_key is None


def test_explicit_path_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_env_var_selects_config(tmp_path, monkeypatch):
    p = write(tmp_path / "c.toml", '[companion]\nport = "/dev/ttyX"\n')
    monkeypatch.setenv(config.CONFIG_ENV, str(p))
    assert load_config().companion.port == "/dev/ttyX"


def test_reads_settings_and_secrets(tmp_path):
    p = write(tmp_path / "config.toml", '[companion]\nchannel_index = 3\nmanage_radio = true\n[radio]\nsf = 9\n')
    write(tmp_path / "secrets.toml", '[channel]\nkey = "000102030405060708090a0b0c0d0e0f"\n')
    cfg = load_config(p)
    assert cfg.companion.channel_index == 3 and cfg.companion.manage_radio
    assert cfg.radio.sf == 9 and cfg.radio.freq_khz == 905775
    assert cfg.channel_key == bytes(range(16))


@pytest.mark.parametrize(
    "text",
    [
        "[nonsense]\nx = 1\n",
        "[companion]\nbogus = 1\n",
        "[companion]\nchannel_name = \"\"\n",
        "[companion]\nchannel_name = \"" + "x" * 32 + "\"\n",
        "[companion]\npoll_interval = 0\n",
        "this is not toml",
    ],
)
def test_rejects_bad_config(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path / "config.toml", text))


@pytest.mark.parametrize("key", ["zz", "00", "00" * 32])
def test_rejects_bad_channel_key(tmp_path, key):
    p = write(tmp_path / "config.toml", "")
    write(tmp_path / "secrets.toml", f'[channel]\nkey = "{key}"\n')
    with pytest.raises(ConfigError):
        load_config(p)


def test_store_channel_key_is_private_and_not_replaced_silently(tmp_path):
    p = write(tmp_path / "config.toml", "")
    cfg = load_config(p)
    path = store_channel_key(cfg, bytes(range(16)))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    cfg = load_config(p)
    assert cfg.channel_key == bytes(range(16))

    with pytest.raises(ConfigError, match="already exists"):
        store_channel_key(cfg, bytes(16))
    assert load_config(p).channel_key == bytes(range(16))

    store_channel_key(cfg, bytes(16), force=True)
    assert load_config(p).channel_key == bytes(16)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert list(tmp_path.glob(".secrets-*")) == []  # no temp files left behind


def test_store_creates_missing_directory(tmp_path):
    cfg = load_config(write(tmp_path / "config.toml", '[companion]\n'))
    nested = config.Config(cfg.companion, cfg.radio, None, cfg.config_path, tmp_path / "new" / "secrets.toml")
    store_channel_key(nested, bytes(16))
    assert (tmp_path / "new" / "secrets.toml").exists()


def test_secrets_file_override(tmp_path):
    p = write(tmp_path / "config.toml", 'secrets_file = "other.toml"\n')
    write(tmp_path / "other.toml", '[channel]\nkey = "' + "11" * 16 + '"\n')
    assert load_config(p).channel_key == bytes([0x11] * 16)


def test_the_radio_settings_default_to_the_mesh_and_are_applied_by_default(tmp_path):
    cfg = load_config(write(tmp_path / "config.toml", ""))
    assert cfg.companion.manage_radio is True
    assert (cfg.radio.freq_khz, cfg.radio.bw_hz, cfg.radio.sf, cfg.radio.cr, cfg.radio.path_hash_mode) == (905775, 62500, 8, 6, 2)


def test_path_hash_mode_is_read_and_checked(tmp_path):
    assert load_config(write(tmp_path / "c.toml", "[radio]\npath_hash_mode = 1\n")).radio.path_hash_mode == 1
    for bad in ("3", "-1", "true", '"2"', "1.5"):
        with pytest.raises(ConfigError, match="path_hash_mode"):
            load_config(write(tmp_path / "bad.toml", f"[radio]\npath_hash_mode = {bad}\n"))


def test_example_config_loads():
    from pathlib import Path

    cfg = load_config(Path(__file__).parent.parent / "deploy" / "config.example.toml")
    assert cfg.companion.channel_index == 1 and cfg.radio.cr == 6


def test_sync_clock_defaults_on_and_can_be_switched_off(tmp_path):
    assert load_config(write(tmp_path / "a.toml", "")).companion.sync_clock is True
    assert load_config(write(tmp_path / "b.toml", "[companion]\nsync_clock = false\n")).companion.sync_clock is False


def test_the_remote_pacing_gap_defaults_to_three_seconds_and_can_be_changed_or_turned_off(tmp_path):
    assert load_config(write(tmp_path / "a.toml", "")).remote.step_gap_s == 3.0
    assert load_config(write(tmp_path / "b.toml", "[remote]\nstep_gap_s = 0\n")).remote.step_gap_s == 0
    assert load_config(write(tmp_path / "c.toml", "[remote]\nstep_gap_s = 5.5\n")).remote.step_gap_s == 5.5
    with pytest.raises(ConfigError, match="step_gap_s"):
        load_config(write(tmp_path / "d.toml", "[remote]\nstep_gap_s = -1\n"))


def test_estimate_and_web_sections_have_defaults_and_validate(tmp_path):
    cfg = load_config(write(tmp_path / "c.toml", ""))
    assert (cfg.web.host, cfg.web.port, cfg.tiles_path, cfg.web_token) == ("127.0.0.1", 8080, None, None)
    assert cfg.estimate.tx_power_dbm == 22.0
    cfg = load_config(write(tmp_path / "c.toml", '[web]\nport = 9000\ntiles = "map.mbtiles"\n[estimate]\npath_loss_exponent = 3.0\n'))
    assert cfg.web.port == 9000 and cfg.estimate.path_loss_exponent == 3.0
    assert cfg.tiles_path == tmp_path / "map.mbtiles"  # relative to the config file


@pytest.mark.parametrize(
    "text",
    [
        "[web]\nport = 0\n",
        "[web]\nport = \"80\"\n",
        "[web]\nhost = \"\"\n",
        "[web]\nonline_tile_url = \"https://example.invalid/tiles.png\"\n",
        "[web]\nbogus = 1\n",
        "[web]\nonline_tile_attribution = 3\n",
        "[estimate]\npath_loss_exponent = 0\n",
        "[estimate]\nmin_radius_m = 100\nmax_radius_m = 50\n",
        "[estimate]\ntx_power_dbm = \"loud\"\n",
    ],
)
def test_bad_web_and_estimate_settings_are_refused(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path / "c.toml", text))


def test_web_token_comes_from_secrets_or_the_environment(tmp_path, monkeypatch):
    p = write(tmp_path / "c.toml", "")
    monkeypatch.delenv(config.WEB_TOKEN_ENV, raising=False)
    write(tmp_path / "secrets.toml", '[web]\ntoken = "0123456789abcdefghij"\n')
    assert load_config(p).web_token == "0123456789abcdefghij"
    monkeypatch.setenv(config.WEB_TOKEN_ENV, "environment-token-wins")
    assert load_config(p).web_token == "environment-token-wins"
    monkeypatch.setenv(config.WEB_TOKEN_ENV, "short")
    with pytest.raises(ConfigError, match="at least"):
        load_config(p)
