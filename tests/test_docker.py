"""The container image and the compose file, run for real. Opt-in: needs Docker and BEACON_DOCKER_TESTS=1.

    BEACON_DOCKER_TESTS=1 pytest tests/test_docker.py

It builds the image for this machine (a minute the first time), and runs the actual deploy/docker/compose.yaml and beaconctl wrapper in a
scratch project directory, under its own compose project name so a real stack on the same machine is not touched.
"""

import os
import shutil
import subprocess
import time
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
    env = {**os.environ, "COMPOSE_PROJECT_NAME": f"beacontest{uuid.uuid4().hex[:8]}"}

    class Stack:
        dir = project
        data = project / "data"
        env = None

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
