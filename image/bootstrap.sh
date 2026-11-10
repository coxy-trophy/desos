#!/bin/bash
# Run only in the owner-authorized, independent Ubuntu build VM.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root inside the image worker' >&2; exit 2; }
systemd-detect-virt --vm --quiet || { echo 'VM worker required' >&2; exit 2; }
[[ -f /etc/desos-build-worker ]] || { echo 'Missing worker marker' >&2; exit 2; }
source /etc/os-release
[[ $ID == ubuntu ]] || { echo 'Ubuntu worker required' >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
root=$(python3 "$here/paths.py" --root "${1:?explicit rootfs required}")
mkdir -p /srv/desos-build
exec > >(tee -a /srv/desos-build/bootstrap.log) 2>&1
trap 'code=$?; echo "$code" > /srv/desos-build/bootstrap.exit' EXIT
if [[ -f $root/.desos-bootstrap-complete ]]; then
    echo 'Verified bootstrap already exists'
    exit 0
fi
[[ ! -e $root ]] || { echo 'Incomplete/preexisting rootfs; inspect before reuse' >&2; exit 2; }
debootstrap --arch=amd64 --variant=minbase \
    --keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg \
    --components=main,universe,restricted,multiverse \
    --include=ca-certificates,gnupg,locales,systemd-sysv,dbus,dbus-user-session,libpam-systemd \
    resolute "$root" http://archive.ubuntu.com/ubuntu
touch "$root/.desos-bootstrap-complete"
