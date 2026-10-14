"""The container image and the compose file, run for real. Opt-in: needs Docker and BEACON_DOCKER_TESTS=1.

    BEACON_DOCKER_TESTS=1 pytest tests/test_docker.py

It builds the image for this machine (a minute the first time), and runs the actual deploy/docker/compose.yaml and beaconctl wrapper in a
scratch project directory, under its own compose project name so a real stack on the same machine is not touched.
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = ROOT / "deploy" / "docker"
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("BEACON_DOCKER_TESTS") != "1" or shutil.which("docker") is None, reason="set BEACON_DOCKER_TESTS=1 and install Docker"),
]
IMAGE = "beacon-base:dockertest"


def run(*cmd, cwd=None, env=None, timeout=120, check=True):
    res = subprocess.run(list(cmd), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed ({res.returncode}):\n{res.stdout}\n{res.stderr}")
    return res


def until(predicate, timeout=60.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def image():
    run("docker", "buildx", "build", "--load", "-f", str(DOCKER_DIR / "Dockerfile"), "--build-arg", "VERSION=dockertest", "-t", IMAGE, str(ROOT), timeout=600)
    return IMAGE


@pytest.fixture
def stack(image, tmp_path):
    project = tmp_path / "beacon"
    project.mkdir()
    for name in ("compose.yaml", "beaconctl"):
        shutil.copy(DOCKER_DIR / name, project / name)
    (project / ".env").write_text(f"BEACON_UID={os.getuid()}\nBEACON_GID={os.getgid()}\nBEACON_TTY_GID=20\nBEACON_VERSION=dockertest\nTZ=UTC\n")
    (project / "data").mkdir()
    with socket.socket() as sock:  # a free port, so a map already running on 8080 is left alone
        sock.bind(("127.0.0.1", 0))
        web_port = sock.getsockname()[1]
    env = {**os.environ, "COMPOSE_PROJECT_NAME": f"beacontest{uuid.uuid4().hex[:8]}"}
    web_env = {"BEACON_WEB_PORT": str(web_port), "BEACON_WEB_BIND": "127.0.0.1"}  # the tests keep the map to this machine; the default is tested apart
    env.update(web_env)

    class Stack:
        dir = project
        data = project / "data"
        env = None
        port = web_port

        def compose(self, *args, **kw):
            return run("docker", "compose", *args, cwd=project, env=env, **kw)

        def ctl(self, *args, check=True):
            return run(str(project / "beaconctl"), *args, cwd=project, env=env, check=check)

    s = Stack()
    s.env = env
    try:
        yield s
    finally:
        subprocess.run(["docker", "compose", "down", "-v", "--timeout", "5"], cwd=project, env=env, capture_output=True)


def configure(stack):
    (stack.data / "config.toml").write_text('[companion]\nport = "/hostdev/serial/by-id/usb-none-if00"\n[clock]\nassume_synced = true\n')
    secrets = stack.data / "secrets.toml"
    secrets.write_text('[channel]\nkey = "000102030405060708090a0b0c0d0e0f"\n')
    secrets.chmod(0o600)


def test_the_compose_file_is_valid_and_says_what_the_plan_says(stack):
    out = stack.compose("config").stdout
    for wanted in ("restart: unless-stopped", "init: true", "/hostdev", "c 166:* rmw", "c 188:* rmw", "BEACON_WATCHDOG_S", "max-size: 10m", "beaconctl", "--companion"):
        assert wanted in out, wanted
    assert "mem_limit" in out or "256" in out  # 268435456 bytes


def test_it_waits_for_the_operator_instead_of_crashing_and_starts_once_configured(stack):
    stack.compose("up", "-d")
    until(lambda: (stack.data / "config.toml").exists(), what="the example configuration to be created")
    log = lambda: stack.compose("logs", "--no-color").stdout
    until(lambda: "created /data/config.toml from the example" in log(), what="the first-start message")
    until(lambda: "no companion serial port" in log(), what="the message about the port")  # it does not start on a placeholder
    assert "# port = \"/hostdev/serial/by-id/" in (stack.data / "config.toml").read_text()
    # no restart loop: it is idling, and the files it made belong to the login user
    assert (stack.data / "config.toml").stat().st_uid == os.getuid()
    state = run("docker", "inspect", "-f", "{{.State.Running}} {{.RestartCount}}", stack.compose("ps", "-q", "ingest").stdout.strip()).stdout.split()
    assert state == ["true", "0"]
    assert stack.ctl("health", check=False).returncode == 1
    configure(stack)
    until(lambda: stack.ctl("health", check=False).returncode == 0, timeout=60, what="beacon-ingest to start (it re-checks every 10 s)")
    assert "ingest: running" in stack.ctl("status").stdout


def test_the_companion_is_missing_but_the_service_runs_and_says_so(stack):
    configure(stack)
    stack.compose("up", "-d")
    until(lambda: stack.ctl("health", check=False).returncode == 0, what="beacon-ingest to start")
    res = stack.compose("exec", "-T", "ingest", "beaconctl", "health", "--companion", check=False)
    assert res.returncode == 1 and "companion is not connected" in res.stdout  # what the container's healthcheck runs
    assert "not connected" in stack.ctl("status").stdout


def test_the_hosts_dev_is_visible_and_the_containers_own_dev_pts_still_works(stack):
    configure(stack)
    stack.compose("up", "-d")
    until(lambda: stack.ctl("health", check=False).returncode == 0, what="beacon-ingest to start")
    assert stack.compose("exec", "-T", "ingest", "test", "-e", "/hostdev/null").returncode == 0
    assert stack.compose("exec", "-T", "ingest", "test", "-d", "/dev/pts").returncode == 0
    if shutil.which("script"):  # docker exec -it, the way the beaconctl wrapper is used from a terminal
        res = run("script", "-qec", "docker compose exec ingest tty", "/dev/null", cwd=stack.dir, env=stack.env, check=False)
        assert "/dev/pts/" in res.stdout, res.stdout


def test_the_image_runs_as_the_login_user_with_the_settings_from_the_plan(stack):
    configure(stack)
    stack.compose("up", "-d")
    until(lambda: stack.ctl("health", check=False).returncode == 0, what="beacon-ingest to start")
    cid = stack.compose("ps", "-q", "ingest").stdout.strip()
    info = run("docker", "inspect", "-f", "{{.Config.User}}|{{.HostConfig.Memory}}|{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.Init}}", cid).stdout.strip()
    assert info == f"{os.getuid()}:{os.getgid()}|268435456|unless-stopped|true"
    assert stack.compose("exec", "-T", "ingest", "printenv", "BEACON_WATCHDOG_S").stdout.strip() == "120"
    assert stack.compose("exec", "-T", "ingest", "printenv", "BEACON_BASE_CONFIG").stdout.strip() == "/data/config.toml"
    assert stack.compose("exec", "-T", "ingest", "id", "-u").stdout.strip() == str(os.getuid())
    assert (stack.data / "beacon.db").stat().st_uid == os.getuid() and oct((stack.data / "beacon.db").stat().st_mode & 0o777) == "0o600"


def test_a_killed_service_is_restarted_with_its_state_intact(stack):
    configure(stack)
    stack.compose("up", "-d")
    until(lambda: stack.ctl("health", check=False).returncode == 0, what="beacon-ingest to start")
    stack.ctl("beacon", "add", "a1" * 8)
    cid = stack.compose("ps", "-q", "ingest").stdout.strip()
    kill = "import os, signal\nfor p in os.listdir('/proc'):\n    if p.isdigit() and b'beacon_base.service' in open(f'/proc/{p}/cmdline', 'rb').read():\n        os.kill(int(p), signal.SIGKILL)\n"
    stack.compose("exec", "-T", "ingest", "python", "-c", kill, check=False)
    until(lambda: run("docker", "inspect", "-f", "{{.RestartCount}}", cid).stdout.strip() != "0", timeout=60, what="Docker to restart the container")
    until(lambda: stack.ctl("health", check=False).returncode == 0, timeout=90, what="beacon-ingest to be running again")
    assert "a1a1a1a1a1a1a1a1" in stack.ctl("beacon", "list").stdout  # the allowlist survived


def test_beaconctl_works_with_the_stack_down_by_using_a_throwaway_container(stack):
    configure(stack)
    res = stack.ctl("beacon", "list")
    assert "no beacons" in res.stdout and "not running" in res.stderr


def test_time_set_inside_the_container_says_to_set_the_clock_on_the_pi(stack):
    configure(stack)
    res = stack.ctl("time", "set", "2026-10-10 12:00:00", check=False)
    assert res.returncode == 2 and "no timedatectl" in res.stderr and "set the Pi's clock on the Pi" in res.stderr


# --- the map: beacon-web as the second service --------------------------------------------------------------------------------------


def get(stack, path, token=None, method="GET", timeout=10):
    """(status, parsed body or bytes) from the published web port."""
    request = urllib.request.Request(f"http://127.0.0.1:{stack.port}{path}", method=method, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            body, status, kind = r.read(), r.status, r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        body, status, kind = e.read(), e.code, e.headers.get("content-type", "")
    return status, json.loads(body) if kind.startswith("application/json") else body


def map_answers(stack):
    try:
        return get(stack, "/api/health")[0] == 200
    except (urllib.error.URLError, ConnectionError, OSError):
        return False


def default_service(stack, name):
    """A service as the compose file defines it with none of the tests' overrides: what an operator who sets nothing gets."""
    env = {k: v for k, v in stack.env.items() if k not in ("BEACON_WEB_PORT", "BEACON_WEB_BIND")}
    return json.loads(run("docker", "compose", "config", "--format", "json", cwd=stack.dir, env=env).stdout)["services"][name]


def test_the_web_service_is_the_same_image_with_only_the_data_directory(stack):
    web = default_service(stack, "web")
    assert web["image"].startswith("beacon-base:") and web["command"] == ["web"]
    assert web["restart"] == "unless-stopped" and web["init"] is True and web["mem_limit"] == str(128 * 1024 * 1024)
    assert [v["target"] for v in web["volumes"]] == ["/data", "/etc/localtime"]  # no serial device, no /hostdev
    assert "device_cgroup_rules" not in web and "group_add" not in web
    assert web["healthcheck"]["test"][:2] == ["CMD", "python"]
    # published on every interface unless the operator says otherwise, on 8080
    assert [(p["host_ip"], p["published"], p["target"]) for p in web["ports"]] == [("0.0.0.0", "8080", 8080)]
    assert default_service(stack, "ingest")["image"] == web["image"]  # one image


def test_the_web_address_and_port_are_set_in_dot_env(stack):
    (stack.dir / ".env").write_text((stack.dir / ".env").read_text() + "BEACON_WEB_BIND=127.0.0.1\nBEACON_WEB_PORT=9090\n")
    env = {k: v for k, v in stack.env.items() if k not in ("BEACON_WEB_PORT", "BEACON_WEB_BIND")}
    web = json.loads(run("docker", "compose", "config", "--format", "json", cwd=stack.dir, env=env).stdout)["services"]["web"]
    assert [(p["host_ip"], p["published"], p["target"]) for p in web["ports"]] == [("127.0.0.1", "9090", 8080)]


def test_the_map_runs_beside_ingest_and_sees_what_it_stores(stack):
    configure(stack)
    stack.compose("up", "-d")
    until(lambda: stack.ctl("health", check=False).returncode == 0, what="beacon-ingest to start")
    until(lambda: map_answers(stack), what="the map to answer")
    state = get(stack, "/api/state")[1]
    assert state["health"]["ingest"]["state"] == "up" and state["operator"] is False  # read-only without a token
    assert [p["code"] for p in state["health"]["problems"]] == ["companion_disconnected"]
    stack.ctl("beacon", "add", "a1" * 8)  # written through ingest's container, read through the web container: one SQLite file
    assert [b["prefix"] for b in get(stack, "/api/state")[1]["beacons"]] == ["a1" * 8]
    status, page = get(stack, "/")
    assert status == 200 and b"Beacon map" in page
    assert get(stack, "/static/vendor/leaflet/leaflet.js")[0] == 200  # the package data is in the image
    container = stack.compose("ps", "-q", "web").stdout.strip()
    health = until(lambda: run("docker", "inspect", "-f", "{{.State.Health.Status}}", container).stdout.strip() == "healthy", timeout=90, what="web to be healthy")
    assert health


def test_the_web_waits_for_a_configuration_instead_of_crashing(stack):
    stack.compose("up", "-d", "web")  # without ingest, which is what creates the configuration
    log = lambda: stack.compose("logs", "--no-color", "web").stdout
    until(lambda: "waiting for /data/config.toml" in log(), what="the waiting message")
    cid = stack.compose("ps", "-q", "web").stdout.strip()
    assert run("docker", "inspect", "-f", "{{.State.Running}} {{.RestartCount}}", cid).stdout.split() == ["true", "0"]
    (stack.data / "config.toml").write_text("[clock]\nassume_synced = true\n")
    until(lambda: map_answers(stack), timeout=60, what="the map to start (it re-checks every 10 s)")


def test_a_reset_needs_the_token_and_the_web_restarted_after_it_is_made(stack):
    configure(stack)
    stack.compose("up", "-d")
    until(lambda: map_answers(stack), what="the map to answer")
    stack.ctl("beacon", "add", "a1" * 8)
    url = "/api/beacons/" + "a1" * 8 + "/reset"
    assert get(stack, url, method="POST")[0] == 403  # no token configured yet: operator actions are off
    token = [l for l in stack.ctl("web", "token", "generate").stdout.splitlines() if l.startswith("web token: ")][0].split(": ")[1]
    assert get(stack, url, token, method="POST")[0] == 403  # the running web has not read it
    stack.compose("restart", "web")
    until(lambda: map_answers(stack), what="the map to answer again")
    assert get(stack, "/api/state")[1]["operator"] is True
    assert get(stack, url, "wrong-token-0123456789", method="POST")[0] == 401
    status, body = get(stack, url, token, method="POST")  # the web container writes to the shared database
    assert status == 200 and body["beacon"] == "a1" * 8 and body["old_hwm"] is None
    assert (stack.data / "secrets.toml").stat().st_mode & 0o777 == 0o600


def test_offline_tiles_work_from_a_file_in_the_data_directory(stack):
    """The guide's last steps, inside the containers: a MapProxy-style file (rows from the north), write-metadata, web.tiles, restart."""
    configure(stack)
    lat, lon, zoom = 45.0, -75.75, 15
    stack.ctl("repeater", "add", "b1" * 8, str(lat), str(lon))
    n = 1 << zoom
    import math

    x, y = int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    path = stack.data / "area.mbtiles"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    db.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (zoom, x, y, b"\x89PNG\r\n\x1a\nfake"))  # row = y: numbered from the north
    db.commit()
    db.close()
    stack.compose("up", "-d")
    until(lambda: map_answers(stack), what="the map to answer")
    out = stack.ctl("tiles", "write-metadata", "/data/area.mbtiles").stdout
    assert "RENUMBERED" in out and "OpenStreetMap" in out
    (stack.data / "config.toml").write_text((stack.data / "config.toml").read_text() + '\n[web]\ntiles = "area.mbtiles"\n')
    stack.compose("restart", "web")
    until(lambda: map_answers(stack), what="the map to answer again")
    status, tile = get(stack, f"/tiles/{zoom}/{x}/{y}")
    assert status == 200 and tile.startswith(b"\x89PNG")
    mapping = get(stack, "/api/map")[1]
    assert mapping["mode"] == "mbtiles" and "OpenStreetMap" in mapping["attribution"]


# --- the image stays pure Python ---------------------------------------------------------------------------------------------------


def test_the_image_holds_no_compiled_library(image):
    """The dependencies are installed on the build machine and copied to the Pi's CPU: a .so in there would be the wrong CPU's."""
    out = run("docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "find /opt/site -name '*.so' -o -name '*.so.*'").stdout
    assert out.strip() == ""
    assert "starlette" in run("docker", "run", "--rm", "--entrypoint", "ls", image, "/opt/site").stdout


def test_the_image_builds_for_the_pi_on_this_machine_without_emulation(image):
    """linux/arm64 is the Pi. The build stage runs here and fails if a compiled library gets in; nothing is run for the target."""
    run("docker", "buildx", "build", "--platform", "linux/arm64", "-f", str(DOCKER_DIR / "Dockerfile"), "--build-arg", "VERSION=dockertest-arm64",
        "--output", "type=cacheonly", str(ROOT), timeout=900)
