#!/bin/sh
# Build the folder that is carried to the Raspberry Pi: the image for the Pi's CPU as a tar file, the compose file and the helper scripts.
# Run it on a machine with internet and Docker (with buildx), from anywhere in the repository. The Pi needs only Docker and the compose plugin.
#
#   deploy/docker/build-bundle.sh                       # both Pi platforms, into dist/beacon-base-<version>/
#   deploy/docker/build-bundle.sh --platform linux/arm64 --out /tmp/bundle
#
# Platforms: linux/arm64 (64-bit Raspberry Pi OS, `uname -m` says aarch64) and linux/arm/v7 (32-bit, armv7l). A Pi Zero 2 W can run either.
# The dependencies are pure Python, so building for the Pi needs no emulation.
set -eu

root="$(cd "$(dirname "$0")/../.." && pwd)"
here="$root/deploy/docker"
platforms=""
out="$root/dist"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)"

while [ $# -gt 0 ]; do
    case "$1" in
        --platform) platforms="$platforms $2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        --version) version="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -n "$platforms" ] || platforms="linux/arm64 linux/arm/v7"

arch_of() {  # a file name part for a platform
    case "$1" in
        linux/arm64|linux/arm64/v8) echo arm64 ;;
        linux/arm/v7) echo armv7 ;;
        linux/amd64) echo amd64 ;;
        *) echo "unsupported platform: $1" >&2; exit 2 ;;
    esac
}

bundle="$out/beacon-base-$version"
rm -rf "$bundle"
mkdir -p "$bundle"

for platform in $platforms; do
    arch="$(arch_of "$platform")"
    echo "building $platform ..."
    docker buildx build --platform "$platform" -f "$here/Dockerfile" --build-arg "VERSION=$version" \
        -t "beacon-base:$version" -t beacon-base:latest \
        --output "type=docker,dest=$bundle/image-$arch.tar" "$root"
done

cp "$here/compose.yaml" "$here/beaconctl" "$here/smoke-test.sh" "$here/load-image.sh" "$bundle/"
cp "$here/.env.example" "$bundle/.env.example"
sed "s/@VERSION@/$version/g" "$here/BUNDLE-README.txt" > "$bundle/README.txt"
printf '%s\n' "$version" > "$bundle/VERSION"
chmod 755 "$bundle/beaconctl" "$bundle/smoke-test.sh" "$bundle/load-image.sh"
( cd "$bundle" && sha256sum image-*.tar > SHA256SUMS )

echo
echo "bundle: $bundle"
( cd "$bundle" && ls -l image-*.tar | awk '{print "  " $5/1048576 " MB  " $9}' )
echo "copy that folder to the Pi (USB stick or scp), then follow README.txt"
