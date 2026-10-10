"""A model of a repeater's login and CLI, for tests of remote management without hardware.

Mirrors what the firmware does as read from ``examples/simple_repeater/MyMesh.cpp`` (``handleLoginReq``, ``onPeerDataRecv``,
``handleCommand``), ``src/helpers/CommonCLI.cpp`` and ``src/helpers/ClientACL.cpp``, including the awkward parts: failures that
produce no reply, an empty-password login that is a guest for an unknown key, replay checks on timestamps, the lossy ``get lat``,
``atof`` turning junk into 0, and every ``set`` zeroing a short zero-hop advert interval. Nothing here is observed on a device.
"""

from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable

PERM_GUEST = 0
PERM_ADMIN = 3
MAX_CLIENTS = 32
FIRMWARE_VER_LEVEL = 11
MIN_LOCAL_ADVERT_INTERVAL = 60  # minutes
MAX_REPLY = 160
BAD_NAME_CHARS = set("[]\\:,?*")


@dataclass
class Client:
    permissions: int
    last_timestamp: int = 0  # RAM only: 0 again after a reboot
    last_activity: int = 0


@dataclass(frozen=True)
class LoginReply:
    admin_flag: int
    permissions: int
    server_time: int
    firmware_level: int = FIRMWARE_VER_LEVEL


def atof(text: str) -> float:
    """C ``atof``: the number at the start of the text, 0.0 when there is none."""
    m = re.match(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text)
    return float(m.group(0)) if m else 0.0


def atoi(text: str) -> int:
    m = re.match(r"\s*[+-]?\d+", text)
    return int(m.group(0)) if m else 0


def ftoa(value: float) -> str:
    """``StrHelper::ftoa(float)``: a 32-bit float printed with 7 decimals truncated (not rounded), trailing zeros dropped."""
    f = struct.unpack("<f", struct.pack("<f", value))[0]
    sign = "-" if f < 0 else ""
    scaled = int(abs(f) * 1e7)
    whole, frac = divmod(scaled, 10_000_000)
    return f"{sign}{whole}.{str(frac).zfill(7).rstrip('0') or '0'}"


class FakeRepeater:
    def __init__(
        self,
        public_key: bytes,
        name: str = "rpt",
        admin_password: str = "password",
        guest_password: str = "",
        lat: float = 0.0,
        lon: float = 0.0,
        beacon_build: bool = True,
        names_supported: bool = True,
        uptime_in_stats: bool = False,
        now: Callable[[], int] | None = None,
    ):
        self.public_key = public_key
        self.name = name
        self.admin_password = admin_password[:15]
        self.guest_password = guest_password[:15]
        self.lat, self.lon = lat, lon
        self.advert_interval_units = 1  # 2 minutes, the install default; stored in 2-minute units
        self.flood_advert_interval = 47
        self.beacon_build = beacon_build
        self.names_supported = names_supported  # False models firmware from before the names work
        self.uptime_in_stats = uptime_in_stats
        self.channel_secret: bytes | None = None
        self.window = 60
        self.names_on = True
        self.name_refresh = 4
        self.stats = {"heard": 0, "reported": 0, "dropped": 0, "send_fail": 0, "pending": 0, "names_sent": 0}
        self._now = now or (lambda: int(time.time()))
        self._booted = time.monotonic()
        self.acl: dict[bytes, Client] = {}
        # test knobs
        self.reachable = True  # False: nothing gets to or from it
        self.drop_floods = False  # flooded packets are not forwarded (denied wildcard region); a stored route still works
        self.reflect_tag = True
        self.drop_replies = False  # the command runs but the reply is lost
        self.commands: list[str] = []  # every command text run, in order, for assertions
        self.saves = 0

    # --- the clock and boot -------------------------------------------------------------------------------------

    def now(self) -> int:
        return self._now()

    def uptime(self) -> int:
        return int(time.monotonic() - self._booted)

    def reboot(self) -> None:
        """The ACL's admin entries come back from flash, guests do not, and every client's last timestamp is 0 again."""
        self._booted = time.monotonic()
        self.acl = {k: Client(c.permissions) for k, c in self.acl.items() if c.permissions != PERM_GUEST}

    # --- ACL ------------------------------------------------------------------------------------------------------

    def _put_client(self, key: bytes) -> Client:
        """``ClientACL::putClient``: never fails; a full table evicts the non-admin client with the oldest activity, else the last slot."""
        client = self.acl.get(key)
        if client is not None:
            return client
        if len(self.acl) >= MAX_CLIENTS:
            victims = [(c.last_activity, k) for k, c in self.acl.items() if c.permissions != PERM_ADMIN]
            if victims:
                del self.acl[min(victims)[1]]
            else:
                del self.acl[list(self.acl)[-1]]
        client = Client(PERM_GUEST)
        self.acl[key] = client
        return client

    def login(self, client_key: bytes, password: str, timestamp: int) -> LoginReply | None:
        """``handleLoginReq``. None means no reply at all."""
        client = self.acl.get(client_key) if password == "" else None
        if client is None:
            if password == self.admin_password:
                perms = PERM_ADMIN
            elif password == self.guest_password:
                perms = PERM_GUEST
            else:
                return None
            client = self._put_client(client_key)
            if timestamp <= client.last_timestamp:
                return None  # a replayed login
            client.last_timestamp = timestamp
            client.last_activity = self.now()
            client.permissions = perms
        return LoginReply(1 if client.permissions == PERM_ADMIN else 0, client.permissions, self.now())

    def command(self, client_key: bytes, timestamp: int, text: str) -> str | None:
        """``onPeerDataRecv`` for a CLI message. None means nothing is sent back."""
        client = self.acl.get(client_key)
        if client is None or client.permissions != PERM_ADMIN:
            return None  # only an admin client may use the CLI; anyone else is ignored
        if timestamp < client.last_timestamp:
            return None
        retry = timestamp == client.last_timestamp
        client.last_timestamp = timestamp
        client.last_activity = self.now()
        if retry:
            return None  # an equal timestamp is a retry: not run again, no reply
        reply = self.handle_command(text)
        return reply if reply else None

    # --- the CLI ----------------------------------------------------------------------------------------------

    def handle_command(self, command: str) -> str:
        command = command.lstrip(" ")
        prefix = ""
        if len(command) > 4 and command[2] == "|":
            if self.reflect_tag:
                prefix = command[:3]
            command = command[3:]
        self.commands.append(command)
        return prefix + self._dispatch(command)

    def _save_prefs(self) -> None:
        self.saves += 1
        if self.advert_interval_units * 2 < MIN_LOCAL_ADVERT_INTERVAL:
            self.advert_interval_units = 0  # a manually configured device stops the 2-minute install default

    def _dispatch(self, command: str) -> str:
        if self.beacon_build and command.startswith("beacon."):
            return self._beacon(command)
        if command.startswith("get "):
            return self._get(command[4:])
        if command.startswith("set "):
            return self._set(command[4:])
        return "Unknown command"

    def _get(self, config: str) -> str:
        if config.startswith("flood.advert.interval"):
            return f"> {self.flood_advert_interval}"
        if config.startswith("advert.interval"):
            return f"> {self.advert_interval_units * 2}"
        if config.startswith("name"):
            return f"> {self.name}"
        if config.startswith("lat"):
            return f"> {ftoa(self.lat)}"
        if config.startswith("lon"):
            return f"> {ftoa(self.lon)}"
        return "Unknown config"

    def _set(self, config: str) -> str:
        if config.startswith("flood.advert.interval "):
            hours = atoi(config[22:])
            if (0 < hours < 3) or hours > 168:
                return "Error: interval range is 3-168 hours"
            self.flood_advert_interval = hours
            self._save_prefs()
            return "OK"
        if config.startswith("advert.interval "):
            mins = atoi(config[16:])
            if (0 < mins < MIN_LOCAL_ADVERT_INTERVAL) or mins > 240:
                return f"Error: interval range is {MIN_LOCAL_ADVERT_INTERVAL}-240 minutes"
            self.advert_interval_units = mins // 2
            self._save_prefs()
            return "OK"
        if config.startswith("name "):
            value = config[5:]
            if any(c in BAD_NAME_CHARS for c in value):
                return "Error, bad chars"
            self.name = value.encode("utf-8")[:31].decode("utf-8", "ignore")
            self._save_prefs()
            return "OK"
        if config.startswith("lat "):
            self.lat = atof(config[4:])
            self._save_prefs()
            return "OK"
        if config.startswith("lon "):
            self.lon = atof(config[4:])
            self._save_prefs()
            return "OK"
        return "unknown config: " + config

    def _beacon(self, command: str) -> str:
        unknown = "Err - unknown beacon command"
        if command == "beacon.channel":
            if self.channel_secret is None:
                return "> not set"
            return f"> set, hash {sha256(self.channel_secret[:16]).digest()[0]:02X}"
        if command.startswith("beacon.channel "):
            arg = command[15:]
            if arg == "clear":
                self.channel_secret = None
                return "OK - report channel cleared"
            if len(arg) not in (32, 64) or not re.fullmatch(r"[0-9a-fA-F]+", arg):
                return "Err - need 32 or 64 hex chars"
            self.channel_secret = bytes.fromhex(arg)[:32]
            return "OK"
        if command == "beacon.window":
            return f"> {self.window} secs"
        if command.startswith("beacon.window "):
            secs = int(m.group(0)) if (m := re.match(r"\d+", command[14:])) else 0
            if not 1 <= secs <= 3600:
                return "Err - window must be 1-3600 secs"
            self.window = secs
            return "OK"
        if command == "beacon.stats":
            s = self.stats
            text = (
                f"heard {s['heard']}, reported {s['reported']}, dropped {s['dropped']}, send fail {s['send_fail']}, "
                f"pending {s['pending']}, names sent {s['names_sent']}"
            )
            return text + (f", up {self.uptime()}s" if self.uptime_in_stats else "")
        if self.names_supported:
            if command == "beacon.names":
                return f"> {'on' if self.names_on else 'off'}"
            if command.startswith("beacon.names "):
                arg = command[13:]
                if arg not in ("on", "off"):
                    return "Err - usage: beacon.names on|off"
                self.names_on = arg == "on"
                return "OK"
            if command == "beacon.name_refresh":
                return f"> {self.name_refresh} hours" if self.name_refresh else "> 0 (only on first sight or change)"
            if command.startswith("beacon.name_refresh "):
                arg = command[20:]
                if not re.fullmatch(r"\d+", arg) or int(arg) > 8760:
                    return "Err - hours must be 0-8760 (0 = only on first sight or change)"
                self.name_refresh = int(arg)
                return "OK"
        return unknown
