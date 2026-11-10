#!/bin/bash
# Provision only the clean DesOS image tree inside the dedicated Ubuntu worker.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Root in the isolated build worker required' >&2; exit 2; }
systemd-detect-virt --vm --quiet
[[ -f /etc/desos-build-worker ]] || { echo 'Wrong machine' >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
recipes=$(dirname "$here")
root=$(python3 "$here/paths.py" --root "${1:?rootfs path required}")
inputs=${2:?absolute input package directory required}
[[ $inputs == /* && -d $inputs/desktop && -d $inputs/release ]] || { echo 'Missing explicit inputs' >&2; exit 2; }
[[ -f $root/.desos-bootstrap-complete ]] || { echo 'Bootstrap must finish first' >&2; exit 2; }
exec > >(tee -a /srv/desos-build/rootfs-packages.log) 2>&1
trap 'code=$?; echo "$code" > /srv/desos-build/rootfs-packages.exit' EXIT
mkdir -p "$root/etc/apt/sources.list.d" "$root/usr/sbin" "$root/run"
# Keep only the signed official package bases and our offline component repo.
: > "$root/etc/apt/sources.list"
cat > "$root/etc/apt/sources.list.d/ubuntu.sources" <<'EOF'
Types: deb
URIs: http://archive.ubuntu.com/ubuntu
Suites: resolute resolute-updates
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu
Suites: resolute-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
if [[ -L $root/etc/resolv.conf ]]; then unlink "$root/etc/resolv.conf"; fi
cp -L /etc/resolv.conf "$root/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$root/usr/sbin/policy-rc.d"
chmod 755 "$root/usr/sbin/policy-rc.d"
mountpoint -q "$root/proc" || mount -t proc proc "$root/proc"
mountpoint -q "$root/sys" || mount --rbind /sys "$root/sys"
mount --make-rslave "$root/sys"
mountpoint -q "$root/dev" || mount --rbind /dev "$root/dev"
mount --make-rslave "$root/dev"
export DEBIAN_FRONTEND=noninteractive
chroot "$root" apt-get -o DPkg::Lock::Timeout=300 update
mapfile -t packages < <(grep -Ev '^\s*(#|$)' "$here/packages.list")
chroot "$root" apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends "${packages[@]}"
mkdir -p "$root/tmp/desos-packages"
cp "$inputs"/desktop/*.deb "$inputs"/release/*.deb "$root/tmp/desos-packages/"
debs=()
for archive in "$root"/tmp/desos-packages/*.deb; do
    [[ -f $archive ]] || { echo 'Missing component package archives' >&2; exit 2; }
    debs+=("/tmp/desos-packages/$(basename "$archive")")
done
chroot "$root" apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends "${debs[@]}"
python3 "$here/configure.py" --root "$root"
bash "$recipes/installer/install-into-rootfs.sh" "$root"
chroot "$root" locale-gen en_US.UTF-8
printf 'LANG=en_US.UTF-8\n' > "$root/etc/default/locale"
printf 'Africa/Accra\n' > "$root/etc/timezone"
ln -sfn /usr/share/zoneinfo/Africa/Accra "$root/etc/localtime"
chroot "$root" systemctl set-default graphical.target
chroot "$root" systemctl enable greetd NetworkManager systemd-resolved bluetooth
# No SSH daemon or build/development accounts are part of the live image.
chroot "$root" dpkg-query -W -f='${Package} ${Version}\n' > /srv/desos-build/rootfs-package-manifest.txt
touch "$root/.desos-packages-complete"
echo 'Clean DesOS rootfs and installer packages configured.'
