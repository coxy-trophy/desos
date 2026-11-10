#!/bin/sh
set -eu
exec /usr/bin/python3 -I "$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && pwd)/install_into_rootfs.py" "$@"
