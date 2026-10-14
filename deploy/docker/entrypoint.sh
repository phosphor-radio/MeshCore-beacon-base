#!/bin/sh
# Container entry point. "serve" (the default) runs beacon-ingest and "web" runs beacon-web (the map and API), each once its configuration is
# complete; anything else is run as given, so `docker run IMAGE beaconctl status` works.
#
# When something the operator has to fix is missing (a writable data directory, the configuration, the channel key) this waits and says
# so, instead of exiting: an exit would be restarted by Docker in a loop. It starts by itself as soon as the problem is fixed.
set -u

mode="${1:-serve}"
case "$mode" in
    serve|web) ;;
    *) exec "$@" ;;
esac

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
        if [ "$mode" = serve ]; then
            cp /opt/beacon-base/config.example.toml "$config" 2>/dev/null
            say "created $config from the example. Edit it (companion.port at least), then run: ./beaconctl channel generate"
        else
            say "waiting for $config, which the ingest service creates from the example on its first start"
        fi
    elif [ "$mode" = serve ]; then
        problem="$(beacon-ingest --check 2>&1)" && break
        say "${problem#beacon-ingest: }"
    else
        problem="$(beacon-web --check 2>&1)" && break
        say "${problem#beacon-web: }"
    fi
    sleep 10
done

if [ "$mode" = serve ]; then
    exec beacon-ingest
fi
# Inside the container it listens on every interface and on a fixed port: what decides who can reach it is where compose publishes the port
# (BEACON_WEB_BIND and BEACON_WEB_PORT in .env), not web.host or web.port in the configuration.
exec beacon-web --host 0.0.0.0 --port 8080
