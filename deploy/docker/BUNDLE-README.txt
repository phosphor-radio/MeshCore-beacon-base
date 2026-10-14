beacon-base @VERSION@ -- base station for the MeshCore beacon tracker
=====================================================================

This folder holds everything the Raspberry Pi needs; it does not use the internet. Docker and the compose plugin must already be installed.
The full guide, with troubleshooting, is DEPLOYMENT.md in this folder (docs/deployment.md in the repository).

First time
----------
1. Copy this folder to the Pi, for example to ~/beacon-base, and go into it.
2. ./load-image.sh                 loads the image that matches the Pi's CPU (uname -m)
3. ./smoke-test.sh                 optional: checks the image runs here, with no hardware (about a minute)
4. cp .env.example .env            then edit it if your user id is not 1000 (id -u, id -g)
5. mkdir data                      create it yourself, so it belongs to you
6. docker compose up -d
7. docker compose logs -f          it creates data/config.toml and tells you what to do next:
     - edit data/config.toml: set companion.port to /hostdev/serial/by-id/<your companion>   (ls /dev/serial/by-id)
     - ./beaconctl channel generate  (or: ./beaconctl channel set <key>)
   The container starts by itself once the configuration is complete.
8. Set the Pi's clock (it has no internet):  sudo timedatectl set-time 'YYYY-MM-DD HH:MM:SS'
9. ./beaconctl status             and ./beaconctl check
10. The map: open http://<the Pi's address>:8080/ in a browser on another machine (the Pi serves it on every interface; set
    BEACON_WEB_BIND=127.0.0.1 in .env to keep it to the Pi, BEACON_WEB_PORT to change the port).
    To let its Reset button work: ./beaconctl web token generate, then docker compose restart web.
    Map tiles: put your .mbtiles file in data/, set web.tiles = "yourfile.mbtiles" under [web] in data/config.toml, ./beaconctl tiles write-metadata
    /data/yourfile.mbtiles (once; /data is where the container sees data/), docker compose restart web.

Every day
---------
  ./beaconctl status              the beacons, the repeaters, whether ingest is running
  ./beaconctl <anything>          beaconctl runs inside the container
  docker compose ps               healthy / unhealthy (ingest unhealthy: the companion is not connected); web is the map
  docker compose restart web     after changing the web settings, the token or the tiles file
  docker compose logs --tail 50

A new event
-----------
  docker compose down
  mv data data-<date>             keep the old database; a new one is created on the next start
  mkdir data && docker compose up -d
