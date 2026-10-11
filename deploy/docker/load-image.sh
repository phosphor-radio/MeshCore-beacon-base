#!/bin/sh
# Load the image that matches this machine's CPU into Docker. Run it in the folder with the image-*.tar files.
set -eu
cd "$(dirname "$0")"
case "$(uname -m)" in
    aarch64|arm64) arch=arm64 ;;
    armv7l|armv8l) arch=armv7 ;;
    x86_64|amd64) arch=amd64 ;;
    *) echo "no image for $(uname -m)" >&2; exit 1 ;;
esac
file="image-$arch.tar"
[ -f "$file" ] || { echo "$file is not in this folder (this machine is $(uname -m))" >&2; exit 1; }
if [ -f SHA256SUMS ]; then
    grep " $file\$" SHA256SUMS | sha256sum -c - || { echo "$file is damaged; copy it again" >&2; exit 1; }
fi
echo "loading $file ..."
docker load -i "$file"
