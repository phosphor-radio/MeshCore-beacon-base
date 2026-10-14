#!/bin/sh
# Check that the image runs on this machine, with no companion attached: a fake companion and simulated beacons run inside one throwaway
# container together with beacon-ingest and beacon-web, and this waits until reports have been stored and the map's API shows them.
#
#   ./smoke-test.sh [IMAGE]        (default beacon-base:latest)
#
# Run it on the Pi after `docker load`, to know the image works on that CPU before the hardware is connected. It uses a temporary data
# directory and removes everything it made.
set -u
image="${1:-beacon-base:latest}"
name="beacon-smoke-$$"
data="$(mktemp -d)"
cleanup() { docker rm -f "$name" >/dev/null 2>&1; rm -rf "$data"; }
trap cleanup EXIT INT TERM

fail() { printf 'smoke test FAILED: %s\n' "$1" >&2; echo "--- container log ---" >&2; docker logs "$name" 2>&1 | tail -20 >&2; exit 1; }

echo "starting $image ..."
docker run -d --name "$name" --user "$(id -u):$(id -g)" -v "$data:/data" -e BEACON_WATCHDOG_S=60 --entrypoint sh "$image" -c '
    cp /opt/beacon-base/config.example.toml /data/config.toml
    sed -i "s/^assume_synced = false/assume_synced = true/" /data/config.toml
    beaconctl channel generate >/dev/null
    beaconctl simulate --provision --interval 1 > /tmp/simulate.log 2>&1 &
    for i in $(seq 1 100); do
        port="$(sed -n "s/^fake companion on //p" /tmp/simulate.log)"
        [ -n "$port" ] && break
        sleep 0.2
    done
    [ -n "$port" ] || { echo "the simulator did not start"; cat /tmp/simulate.log; exit 1; }
    beacon-web --host 127.0.0.1 --port 8080 > /tmp/web.log 2>&1 &
    exec beacon-ingest --port "$port"
' >/dev/null || fail "docker run"

echo "waiting for beacon-ingest to connect and store reports ..."
i=0
while [ "$i" -lt 90 ]; do
    if docker exec "$name" beaconctl health --companion >/dev/null 2>&1 \
        && docker exec "$name" beaconctl status 2>/dev/null | grep -q "last report [0-9]*s ago"; then
        # the map's API, asked from inside: ingest is up and it lists the simulated beacons
        if docker exec "$name" python -c "
import json, sys, urllib.request
state = json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/state', timeout=5))
sys.exit(0 if state['health']['ingest']['state'] == 'up' and len(state['beacons']) >= 1 else 1)" >/dev/null 2>&1; then
            docker exec "$name" beaconctl status | sed -n '1,6p'
            echo "the map answers: /api/state shows ingest running and the simulated beacons"
            echo "smoke test passed"
            exit 0
        fi
    fi
    [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ] || fail "the container stopped"
    i=$((i + 1))
    sleep 1
done
fail "no reports were stored, or the map did not show them, within 90 seconds"
