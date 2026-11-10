#!/usr/bin/env bash
# Build only inside the explicitly authorized Ubuntu build VM. Never installs.
set -euo pipefail
if [[ ${1:-} != --guest-build || $# != 3 ]]; then
    echo 'Usage: build-guest.sh --guest-build /absolute/work/directory /absolute/baseline.config' >&2
    exit 2
fi
if ! systemd-detect-virt --vm --quiet; then
    echo 'Refusing kernel compilation outside a detected virtual machine' >&2
    exit 2
fi
# shellcheck source=/etc/os-release
source /etc/os-release
if [[ ${ID:-} != ubuntu ]]; then
    echo 'Kernel worker must use Ubuntu; this script does not provision the host' >&2
    exit 2
fi
work=$2
baseline=$3
if [[ $work != /* || $baseline != /* || ! -f $baseline || $work == / ]]; then
    echo 'Explicit absolute work directory and existing baseline config required' >&2
    exit 2
fi
for tool in python3 gpg make gcc ld flex bison dpkg-deb dpkg-buildpackage fakeroot rsync bc pahole; do
    command -v "$tool" >/dev/null || { echo "Missing guest build dependency: $tool" >&2; exit 2; }
done
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$work"
work=$(realpath "$work")
baseline=$(realpath "$baseline")
source_dir=$work/linux-6.18.62
build_dir=$work/build
packages=$work/packages
if [[ $work == *' '* ]]; then
    echo 'Kernel build work path must not contain spaces' >&2
    exit 2
fi
python3 "$here/kernel_tools.py" fetch --work "$work"
python3 "$here/kernel_tools.py" extract --work "$work"
# Freeze the original baseline independently of /boot and avoid copying accounts.
if [[ ! -e $work/baseline.config ]]; then
    cp -- "$baseline" "$work/baseline.config"
fi
if ! cmp -s -- "$baseline" "$work/baseline.config"; then
    echo 'Baseline changed since first build; use a new work directory' >&2
    exit 2
fi
export ARCH=x86_64 KBUILD_BUILD_USER=desos KBUILD_BUILD_HOST=desos-builder
export SOURCE_DATE_EPOCH=1793989063 KBUILD_BUILD_VERSION=1
export KBUILD_BUILD_TIMESTAMP='Fri Nov  6 18:17:43 UTC 2026'
export KDEB_PKGVERSION=6.18.62-1desos1 KDEB_CHANGELOG_DIST=desos
export DEB_BUILD_OPTIONS='parallel=2'
export LC_ALL=C TZ=UTC
python3 "$here/kernel_tools.py" configure --work "$work" --baseline "$work/baseline.config"
release=$(make --silent -C "$source_dir" O="$build_dir" kernelrelease)
if [[ $release != 6.18.62-desos ]]; then
    echo "Unexpected kernel release: $release" >&2
    exit 2
fi
# No sudo, make install, dpkg -i, source edits, or host boot changes.
# Upstream bindeb-pkg emits .deb next to O=; strip debug data from modules.
make -C "$source_dir" O="$build_dir" -j2 LOCALVERSION= INSTALL_MOD_STRIP=1 bindeb-pkg
python3 "$here/kernel_tools.py" check-config --work "$work"
python3 "$here/kernel_tools.py" verify-tree --work "$work"
mkdir -p "$packages"
shopt -s nullglob
artifacts=("$work"/*.deb)
if (( ${#artifacts[@]} == 0 )); then
    echo 'Kernel build produced no Debian packages' >&2
    exit 1
fi
for artifact in "${artifacts[@]}"; do
    version=$(dpkg-deb -f "$artifact" Version)
    if [[ $version != "$KDEB_PKGVERSION" ]]; then
        echo "Refusing stale package $artifact with version $version" >&2
        exit 1
    fi
    cp -- "$artifact" "$packages/"
done
image=("$packages"/linux-image-6.18.62-desos_*.deb)
headers=("$packages"/linux-headers-6.18.62-desos_*.deb)
if (( ${#image[@]} != 1 || ${#headers[@]} != 1 )); then
    echo 'Expected exactly one DesOS linux-image and linux-headers package' >&2
    exit 1
fi
python3 "$here/kernel_tools.py" provenance --work "$work" --baseline "$work/baseline.config" --packages "$packages"
(cd "$packages" && sha256sum -- *.deb config-* baseline.config source-pin.json signature-status.txt signature-log.txt kernel-provenance.json > SHA256SUMS)
printf 'Kernel packages and public provenance: %s\n' "$packages"
