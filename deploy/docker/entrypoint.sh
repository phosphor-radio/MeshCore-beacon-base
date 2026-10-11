#!/bin/sh
# Container entry point. "serve" (the default) runs beacon-ingest once the configuration is complete; anything else is run as given, so
# `docker run IMAGE beaconctl status` works.
#
# When something the operator has to fix is missing (a writable data directory, the configuration, the channel key) this waits and says
# so, instead of exiting: an exit would be restarted by Docker in a loop. It starts by itself as soon as the problem is fixed.
set -u

if [ "${1:-serve}" != "serve" ]; then
    exec "$@"
fi

config="${BEACON_BASE_CONFIG:-/data/config.toml}"
data="$(dirname "$config")"
last=""

say() {  # print a problem once, not every ten seconds
    if [ "$1" != "$last" ]; then
        printf 'beacon-base: %s\n' "$1" >&2
        last="$1"
    fi
}

while true; do
    if [ ! -d "$data" ] || [ ! -w "$data" ]; then
        say "$data is not writable by user $(id -u):$(id -g). Create the data directory (the ./data directory next to compose.yaml) as the user that runs the container, or set BEACON_UID and BEACON_GID in .env to its owner."
    elif [ ! -e "$config" ]; then
        cp /opt/beacon-base/config.example.toml "$config" 2>/dev/null
        say "created $config from the example. Edit it (companion.port at least), then run: ./beaconctl channel generate"
    else
        problem="$(beacon-ingest --check 2>&1)" && break
        say "${problem#beacon-ingest: }"
    fi
    sleep 10
done

exec beacon-ingest
