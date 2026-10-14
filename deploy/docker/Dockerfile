# syntax=docker/dockerfile:1
#
# The base station software as a container image, built where there is internet and carried to the Raspberry Pi as a tar file
# (deploy/docker/build-bundle.sh). The context is the repository root.
#
# The dependencies are pure Python (pyserial, Starlette, uvicorn and this package), so they are installed in a stage that always runs on the
# machine doing the build and copied into a base image of the *target* platform. Nothing is executed for the target platform, which means
# building an arm image on an x86 machine needs no emulation. That is why the web API is on Starlette and not FastAPI (whose pydantic has a
# compiled core), and why uvicorn is installed without its [standard] extra (uvloop and httptools are compiled): the build fails below if
# a compiled library ever gets in (docs/plan/web-docker.md).

ARG PYTHON=3.13-slim

FROM --platform=$BUILDPLATFORM python:${PYTHON} AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --target /opt/site ".[web]" \
    && if find /opt/site \( -name '*.so' -o -name '*.so.*' -o -name '*.pyd' \) | grep -q .; then \
         echo "a compiled library is in the image; it would be the wrong CPU's on the Pi:" >&2; \
         find /opt/site \( -name '*.so' -o -name '*.so.*' -o -name '*.pyd' \) >&2; exit 1; fi
# The configuration the container creates on its first start: the repository's example with the serial port left for the operator (and
# pointing at /hostdev, where the compose file mounts the host's /dev), so the container waits for it instead of starting on a placeholder.
COPY deploy/config.example.toml /src/config.example.toml
RUN sed -e 's|^port = "/dev/serial|# port = "/hostdev/serial|' \
        -e '1i # Container configuration: set companion.port to /hostdev/serial/by-id/<your companion> (ls /dev/serial/by-id on the Pi).' \
        /src/config.example.toml > /opt/config.example.toml

FROM python:${PYTHON}
ARG VERSION=dev
LABEL org.opencontainers.image.title="beacon-base" \
      org.opencontainers.image.description="Base station software for the MeshCore beacon tracker" \
      org.opencontainers.image.version="${VERSION}"

# HOME is set because the container runs as whatever user the compose file names, which has no entry in /etc/passwd.
ENV PYTHONPATH=/opt/site \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TZ=UTC \
    BEACON_BASE_CONFIG=/data/config.toml \
    BEACON_WATCHDOG_S=120

COPY --from=build /opt/site /opt/site
COPY --from=build /opt/config.example.toml /opt/beacon-base/config.example.toml
COPY --chmod=755 deploy/docker/bin/beaconctl deploy/docker/bin/beacon-ingest deploy/docker/bin/beacon-web deploy/docker/entrypoint.sh /usr/local/bin/

# /data holds the configuration, the secrets and the database. The compose file mounts a host directory there.
WORKDIR /data
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# "serve" runs beacon-ingest; the compose file's second service runs the same image with the command "web" (beacon-web, the map and API).
CMD ["serve"]
