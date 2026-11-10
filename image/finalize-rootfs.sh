#!/bin/bash
# Install signed DesOS kernel/components, check integration and seal the tree.
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Root in the isolated worker required' >&2; exit 2; }
systemd-detect-virt --vm --quiet
[[ -f /etc/desos-build-worker ]] || { echo 'Wrong machine' >&2; exit 2; }
source /etc/os-release
[[ $ID == ubuntu ]] || { echo 'Ubuntu worker required' >&2; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
recipes=$(dirname "$here")
root=$(python3 "$here/paths.py" --root "${1:?explicit rootfs required}")
repo=${2:?explicit signed component repository directory required}
provenance=${3:?explicit kernel public provenance directory required}
[[ $repo == /* && -d $repo && ! -e $repo/.incomplete && -s $repo/dists/desos/InRelease && -s $repo/desos-archive-keyring.asc ]]
[[ $provenance == /* && -s $provenance/kernel-provenance.json ]]
[[ -f $root/.desos-packages-complete ]] || { echo 'Complete rootfs package stage first' >&2; exit 2; }
exec > >(tee -a /srv/desos-build/finalize.log) 2>&1
trap 'code=$?; echo "$code" > /srv/desos-build/finalize.exit' EXIT
if [[ -L $root/etc/resolv.conf ]]; then unlink "$root/etc/resolv.conf"; fi
cp -L /etc/resolv.conf "$root/etc/resolv.conf"
mountpoint -q "$root/proc" || mount -t proc proc "$root/proc"
mountpoint -q "$root/sys" || mount --rbind /sys "$root/sys"
mount --make-rslave "$root/sys"
mountpoint -q "$root/dev" || mount --rbind /dev "$root/dev"
mount --make-rslave "$root/dev"
mkdir -p "$root/usr/share/desos/repository" "$root/usr/share/keyrings" "$root/etc/apt/sources.list.d"
cp -a "$repo/." "$root/usr/share/desos/repository/"
chown -R 0:0 "$root/usr/share/desos/repository"
cp "$repo/desos-archive-keyring.asc" "$root/usr/share/keyrings/desos-archive-keyring.asc"
cat > "$root/etc/apt/sources.list.d/desos.sources" <<'EOF'
Types: deb
URIs: file:/usr/share/desos/repository
Suites: desos
Components: main
Signed-By: /usr/share/keyrings/desos-archive-keyring.asc
EOF
export DEBIAN_FRONTEND=noninteractive
chroot "$root" apt-get -o DPkg::Lock::Timeout=300 update
mapfile -t packages < <(grep -Ev '^\s*(#|$)' "$here/packages.list")
chroot "$root" apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends "${packages[@]}" \
    linux-image-6.18.62-desos=6.18.62-1desos1 linux-headers-6.18.62-desos=6.18.62-1desos1
# Reapply the final reviewed integration after every component is present.
python3 "$here/configure.py" --root "$root"
bash "$recipes/installer/install-into-rootfs.sh" "$root"
chroot "$root" systemctl set-default graphical.target
chroot "$root" systemctl enable greetd NetworkManager systemd-resolved systemd-timesyncd bluetooth
mkdir -p "$root/usr/share/doc/desos/build"
for name in kernel-provenance.json source-pin.json signature-status.txt signature-log.txt config-6.18.62-desos baseline.config; do
    cp "$provenance/$name" "$root/usr/share/doc/desos/build/"
done
cp "$here/desktop-pin.json" "$root/usr/share/doc/desos/build/"
# Preserve root's normal locked-password policy; no static human accounts.
chroot "$root" passwd -l root
chroot "$root" dpkg --audit > /srv/desos-build/dpkg-audit.txt
[[ ! -s /srv/desos-build/dpkg-audit.txt ]] || { cat /srv/desos-build/dpkg-audit.txt; exit 1; }
chroot "$root" apt-get check
for binary in /usr/bin/des-ui /usr/bin/des-comp /usr/bin/des-ui-ctl /usr/bin/calamares; do
    [[ -x $root$binary ]] || { echo "Missing executable $binary" >&2; exit 1; }
    chroot "$root" ldd "$binary" > /srv/desos-build/ldd-current.txt
    if grep -q 'not found' /srv/desos-build/ldd-current.txt; then cat /srv/desos-build/ldd-current.txt; exit 1; fi
done
chroot "$root" dpkg-query -W -f='${Package} ${Version}\n' > /srv/desos-build/rootfs-package-manifest.txt
python3 "$here/configure.py" --root "$root" --audit-only
# Recursive private bind trees are detached only from this owned staging root.
umount -R "$root/dev"
umount -R "$root/sys"
umount "$root/proc"
python3 "$here/seal.py" --root "$root" > /srv/desos-build/seal-receipt.json
cat /srv/desos-build/seal-receipt.json
echo 'Clean rootfs finalized and sealed; ISO construction and runtime validation remain.'
