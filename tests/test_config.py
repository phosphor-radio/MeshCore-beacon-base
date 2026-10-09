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


def test_example_config_loads():
    from pathlib import Path

    cfg = load_config(Path(__file__).parent.parent / "deploy" / "config.example.toml")
    assert cfg.companion.channel_index == 1 and cfg.radio.cr == 6
