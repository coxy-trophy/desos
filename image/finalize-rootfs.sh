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
# Optional: the kernel public provenance directory, only to cross-check that
# the signed evidence in the repository is the evidence of this kernel build.
provenance=${3:-}
[[ $repo == /* && -d $repo && ! -e $repo/.incomplete && -s $repo/dists/desos/InRelease && -s $repo/desos-archive-keyring.asc ]]
# Build evidence is signed on the host together with the repository; this
# worker holds no private key and installs exactly those signed bytes.
evidence=$repo/build-evidence
evidence_files=(kernel-provenance.json source-pin.json signature-status.txt signature-log.txt config-6.18.62-desos baseline.config desktop-pin.json)
[[ -s $evidence/SHA256SUMS && -s $evidence/SHA256SUMS.asc ]] || {
    echo 'Repository carries no signed build evidence; rebuild it with component_repo.py --evidence' >&2; exit 2; }
for name in "${evidence_files[@]}"; do
    [[ -s $evidence/$name ]] || { echo "Signed build evidence lacks $name" >&2; exit 2; }
    awk -v name="$name" '$2 == name { found = 1 } END { exit !found }' "$evidence/SHA256SUMS" || {
        echo "SHA256SUMS does not cover $name" >&2; exit 2; }
done
(cd "$evidence" && sha256sum --check --strict --quiet SHA256SUMS)
if [[ -n $provenance ]]; then
    [[ $provenance == /* && -s $provenance/kernel-provenance.json ]]
    cmp "$provenance/kernel-provenance.json" "$evidence/kernel-provenance.json" || {
        echo 'Signed build evidence is not from this kernel build' >&2; exit 2; }
fi
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
# Reviewed component trees (system/, undo/, ...) add their own packages/units.
# Plain assignments, so a refused component.json stops the build here.
component_packages=$(python3 "$here/components.py" packages --recipes "$recipes")
component_units=$(python3 "$here/components.py" units --recipes "$recipes")
[[ -z $component_packages ]] || mapfile -t -O "${#packages[@]}" packages <<< "$component_packages"
units=()
[[ -z $component_units ]] || mapfile -t units <<< "$component_units"
# The DesOS kernel is installed through its metapackages (exact version from
# kernel/source-pin.json), so the versioned kernel packages are dependencies
# that a later linux-image-desos can replace. Components delivered as DesOS
# packages (desos-verify) come from the same signed repository.
kernel_packages=$(python3 "$here/kernel_metapackage.py" names --pin "$recipes/kernel/source-pin.json")
packaged_components=$(python3 "$here/components.py" packaged --recipes "$recipes")
mapfile -t desos_packages <<< "$kernel_packages"
[[ -z $packaged_components ]] || mapfile -t -O "${#desos_packages[@]}" desos_packages <<< "$packaged_components"
chroot "$root" apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "${packages[@]}" \
    "${desos_packages[@]}"
# Reapply the final reviewed integration after every component is present.
python3 "$here/configure.py" --root "$root"
bash "$recipes/installer/install-into-rootfs.sh" "$root"
# Component files go in only after every package is installed, so they are
# never replaced by a package's own default configuration.
python3 "$here/components.py" install --recipes "$recipes" --root "$root"
chroot "$root" install -d -o _greetd -g _greetd -m 755 /var/lib/des-ui/greeter
chroot "$root" systemctl set-default graphical.target
chroot "$root" systemctl enable greetd NetworkManager systemd-resolved systemd-timesyncd bluetooth
if (( ${#units[@]} )); then chroot "$root" systemctl enable "${units[@]}"; fi
mkdir -p "$root/usr/share/doc/desos/build"
for name in "${evidence_files[@]}" SHA256SUMS SHA256SUMS.asc; do
    install -o 0 -g 0 -m 644 "$evidence/$name" "$root/usr/share/doc/desos/build/$name"
done
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
# The image must pass its own verifier before it is sealed: an offline quick
# check of the tree with the shipped tool. Checks that need a running DesOS
# kernel stay unknown here; a failed check or an unverified signature stops.
verify_status=0
python3 "$root/usr/bin/desos-verify" --root "$root" --quick --json > /srv/desos-build/verify-at-build.json || verify_status=$?
python3 - /srv/desos-build/verify-at-build.json "$verify_status" <<'PYTHON'
import json, sys
result = json.load(open(sys.argv[1]))
status = {check['id']: check['status'] for check in result['checks']}
print(result['summary'])
needed = ('signatures.key', 'signatures.manifest', 'signatures.release', 'signatures.evidence')
bad = [check['id'] + ': ' + check['reason'] for check in result['checks'] if check['status'] == 'fail']
bad += [name + ': not verified' for name in needed if status.get(name) != 'pass']
if sys.argv[2] != '0' or bad:
    sys.exit('Image does not pass desos-verify: ' + '; '.join(bad or ['exit status ' + sys.argv[2]]))
PYTHON
# Recursive private bind trees are detached only from this owned staging root.
umount -R "$root/dev"
umount -R "$root/sys"
umount "$root/proc"
python3 "$here/seal.py" --root "$root" > /srv/desos-build/seal-receipt.json
cat /srv/desos-build/seal-receipt.json
echo 'Clean rootfs finalized and sealed; ISO construction and runtime validation remain.'
