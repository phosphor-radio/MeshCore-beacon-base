"""Configuration and secrets.

Settings live in a hand-edited TOML file (see ``deploy/config.example.toml``). Secrets that ``beaconctl`` generates, such
as the report channel key, live in a separate ``secrets.toml`` next to it with mode 0600, so the main file can be shared
or committed without leaking them.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_ENV = "BEACON_BASE_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/beacon-base/config.toml")
SECRETS_FILENAME = "secrets.toml"
CHANNEL_KEY_LEN = 16


class ConfigError(Exception):
    """Bad or missing configuration. Not retried, the operator must fix it."""


@dataclass(frozen=True)
class RadioConfig:
    """Radio settings shared by every device in the mesh (project decision 14)."""

    freq_khz: int = 905775
    bw_hz: int = 62500
    sf: int = 8
    cr: int = 6  # 6 means 4/6
    path_hash_mode: int = 2  # 0-2: path hashes of 1-3 bytes in the packets a device floods; the mesh uses 2


@dataclass(frozen=True)
class CompanionConfig:
    port: str | None = None  # prefer /dev/serial/by-id/..., ttyACM numbers move
    baud: int = 115200
    # DTR when opening the port: "auto" picks by USB vendor (Espressif native USB stays low, anything else is raised, with one
    # automatic retry the other way if the companion does not answer), "on" or "off" force it. RTS is never raised.
    dtr: str = "auto"
    channel_index: int = 1  # slot 0 holds the built-in Public channel
    channel_name: str = "beacon-reports"
    manage_radio: bool = True  # apply [radio] (radio parameters and path hash mode) to the companion; false only warns when it differs
    poll_interval: float = 30.0  # safety-net queue drain, in case a MSG_WAITING push is missed
    learn_repeaters: bool = True  # take each repeater's position and name from the adverts the companion hears
    manual_add_contacts: bool = True  # run the companion in manual-add mode so every advert reaches us in full (stored in the companion)
    sync_clock: bool = True  # move the companion's clock forward to the base's once that clock is trusted (repeaters check logins for replays)
    command_timeout: float = 5.0


@dataclass(frozen=True)
class DatabaseConfig:
    path: str = "beacon.db"  # relative paths are resolved against the config file's directory


@dataclass(frozen=True)
class BeaconConfig:
    interval_s: float = 300.0  # expected beacon transmit interval (jitter is +/-10%)
    silent_intervals: float = 3.0  # a beacon not heard for this many intervals is "silent"
    jitter: float = 0.1


@dataclass(frozen=True)
class ClockConfig:
    assume_synced: bool = False  # trust the system clock without 'beaconctl time set' (NTP, RTC), e.g. on a dev machine


@dataclass(frozen=True)
class Config:
    companion: CompanionConfig = field(default_factory=CompanionConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    channel_key: bytes | None = None
    config_path: Path | None = None
    secrets_path: Path | None = None
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    beacon: BeaconConfig = field(default_factory=BeaconConfig)
    clock: ClockConfig = field(default_factory=ClockConfig)

    @property
    def db_path(self) -> Path:
        path = Path(self.database.path).expanduser()
        if path.is_absolute() or self.config_path is None:
            return path
        return self.config_path.parent / path


def resolve_config_path(explicit: str | os.PathLike[str] | None) -> tuple[Path, bool]:
    """Return (path, required). An explicit path or the environment variable must exist, the default may not."""
    if explicit:
        return Path(explicit).expanduser(), True
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser(), True
    return DEFAULT_CONFIG_PATH.expanduser(), False


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def parse_channel_key(text: str) -> bytes:
    text = text.strip()
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise ConfigError("channel key must be hexadecimal") from None
    if len(key) != CHANNEL_KEY_LEN:
        raise ConfigError(f"channel key must be {CHANNEL_KEY_LEN} bytes ({CHANNEL_KEY_LEN * 2} hex characters)")
    return key


def _section(data: dict, name: str, cls: type, path: Path):
    raw = data.get(name, {})
    allowed = set(cls.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{path}: unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}")
    try:
        return cls(**raw)
    except TypeError as e:
        raise ConfigError(f"{path}: [{name}]: {e}") from e


def secrets_path_for(config_path: Path, data: dict | None = None) -> Path:
    override = (data or {}).get("secrets_file")
    if override:
        return (config_path.parent / override).expanduser()
    return config_path.parent / SECRETS_FILENAME


def load_config(explicit_path: str | os.PathLike[str] | None = None) -> Config:
    path, required = resolve_config_path(explicit_path)
    data: dict = {}
    if path.exists():
        data = _read_toml(path)
    elif required:
        raise ConfigError(f"config file not found: {path}")

    unknown = set(data) - {"companion", "radio", "database", "beacon", "clock", "secrets_file"}
    if unknown:
        raise ConfigError(f"{path}: unknown section(s): {', '.join(sorted(unknown))}")

    companion = _section(data, "companion", CompanionConfig, path)
    radio = _section(data, "radio", RadioConfig, path)
    database = _section(data, "database", DatabaseConfig, path)
    beacon = _section(data, "beacon", BeaconConfig, path)
    clock = _section(data, "clock", ClockConfig, path)
    if isinstance(radio.path_hash_mode, bool) or not isinstance(radio.path_hash_mode, int) or not 0 <= radio.path_hash_mode <= 2:
        raise ConfigError("radio.path_hash_mode must be 0, 1 or 2")
    if companion.dtr not in ("auto", "on", "off"):
        raise ConfigError(f"companion.dtr must be \"auto\", \"on\" or \"off\", not {companion.dtr!r}")
    if not 0 <= companion.channel_index <= 255:
        raise ConfigError("companion.channel_index must be 0-255")
    if not 0 < len(companion.channel_name.encode("utf-8")) < 32:
        raise ConfigError("companion.channel_name must be 1-31 bytes")
    if companion.poll_interval <= 0 or companion.command_timeout <= 0:
        raise ConfigError("companion.poll_interval and companion.command_timeout must be positive")
    if beacon.interval_s <= 0 or beacon.silent_intervals <= 0 or not 0 <= beacon.jitter < 1:
        raise ConfigError("beacon.interval_s and beacon.silent_intervals must be positive and beacon.jitter in [0, 1)")

    spath = secrets_path_for(path, data)
    key = None
    if spath.exists():
        secrets = _read_toml(spath)
        raw_key = secrets.get("channel", {}).get("key")
        if raw_key is not None:
            key = parse_channel_key(str(raw_key))
    return Config(
        companion=companion,
        radio=radio,
        channel_key=key,
        config_path=path,
        secrets_path=spath,
        database=database,
        beacon=beacon,
        clock=clock,
    )


def _toml_value(v: object) -> str:
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"unsupported secret value type {type(v).__name__}")


def write_secrets(path: Path, data: dict[str, dict[str, str]]) -> None:
    """Atomically write the secrets file with mode 0600. ``data`` maps section name to string settings."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = []
    for section, values in data.items():
        lines.append(f"[{section}]")
        lines.extend(f"{k} = {_toml_value(v)}" for k, v in values.items())
        lines.append("")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".secrets-")  # created with mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def store_channel_key(cfg: Config, key: bytes, force: bool = False) -> Path:
    """Save the report channel key. Refuses to replace an existing key unless force is set, because changing it means
    reprovisioning every repeater."""
    if cfg.secrets_path is None:
        raise ConfigError("no secrets path")
    if cfg.channel_key is not None and not force:
        raise ConfigError(
            f"a channel key already exists in {cfg.secrets_path}; replacing it means reprovisioning every repeater "
            "(use --force to do so)"
        )
    existing = _read_toml(cfg.secrets_path) if cfg.secrets_path.exists() else {}
    existing.setdefault("channel", {})["key"] = key.hex()
    write_secrets(cfg.secrets_path, existing)
    return cfg.secrets_path
