#!/usr/bin/env bash
# Construct the ISO only in the isolated Ubuntu image worker; never flash/install.
set -euo pipefail
if [[ $# != 2 ]]; then
    echo 'Usage: build-iso.sh /srv/desos-build/rootfs /srv/desos-build/output' >&2
    exit 2
fi
[[ $EUID == 0 ]] || { echo 'Root inside the isolated image worker required' >&2; exit 2; }
systemd-detect-virt --vm --quiet || { echo 'VM worker required' >&2; exit 2; }
[[ -f /etc/desos-build-worker ]] || { echo 'Missing worker marker' >&2; exit 2; }
source /etc/os-release
[[ ${ID:-} == ubuntu ]] || { echo 'Ubuntu image worker required' >&2; exit 2; }
for tool in python3 chroot update-initramfs unmkinitramfs mksquashfs grub-mkrescue xorriso mformat mount umount; do
    command -v "$tool" >/dev/null || { echo "Missing ISO build tool: $tool" >&2; exit 2; }
done
[[ -f /usr/lib/grub/i386-pc/cdboot.img && -f /usr/lib/grub/x86_64-efi/modinfo.sh ]] || {
    echo 'Both grub-pc-bin and grub-efi-amd64-bin are required' >&2; exit 2;
}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(realpath -m -- "$1")
output=$(realpath -m -- "$2")
# The helper also checks identity, privacy seal, package integration and modules.
# Pass original spellings so a supplied root/output symlink cannot be concealed.
python3 "$here/iso_recipe.py" preflight --root "$1" --output "$2" > /dev/null
mkdir -p "$output"
stage=$output/iso-staging
iso=$output/desos-0.1-preview-amd64.iso
if [[ -e $stage || -e $iso ]]; then
    echo 'Output already contains an ISO/staging tree; preserve it and choose a fresh output directory' >&2
    exit 2
fi
selection=$(python3 "$here/iso_recipe.py" prepare --root "$root" --output "$output" --stage "$stage")
upstream=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["upstream"])' "$selection")
compat=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["compat"])' "$selection")
mounts=()
cleanup() {
    local code=$?
    trap - EXIT
    for ((index=${#mounts[@]}-1; index>=0; index--)); do
        if ! umount -- "${mounts[index]}"; then
            echo "Failed to unmount build pseudo filesystem: ${mounts[index]}" >&2
            code=1
        fi
    done
    exit "$code"
}
trap cleanup EXIT
for directory in dev proc sys run tmp; do
    [[ ! -L $root/$directory ]] || { echo "Refusing rootfs pseudo-directory symlink: $directory" >&2; exit 2; }
    mkdir -p "$root/$directory"
done
mount --bind /dev "$root/dev"
mounts+=("$root/dev")
mount --make-private "$root/dev"
mount -t proc proc "$root/proc"
mounts+=("$root/proc")
mount -t sysfs sysfs "$root/sys"
mounts+=("$root/sys")
for flavor in upstream compat; do
    if [[ $flavor == upstream ]]; then version=$upstream; else version=$compat; fi
    [[ ! -L $root/boot ]] || { echo 'Refusing boot-directory symlink' >&2; exit 2; }
    action=-c
    [[ ! -e $root/boot/initrd.img-$version ]] || action=-u
    chroot "$root" /usr/bin/env CASPER_GENERATE_UUID=1 update-initramfs "$action" -k "$version"
    [[ -f $root/boot/vmlinuz-$version && ! -L $root/boot/vmlinuz-$version && -f $root/boot/initrd.img-$version && ! -L $root/boot/initrd.img-$version ]] || {
        echo 'Regular kernel/initrd payload files required' >&2; exit 1;
    }
    cp -- "$root/boot/vmlinuz-$version" "$stage/casper/vmlinuz-$flavor"
    cp -- "$root/boot/initrd.img-$version" "$stage/casper/initrd-$flavor"
    unpack=$output/initrd-audit-$flavor
    [[ ! -e $unpack ]] || { echo "Initrd audit destination exists: $unpack" >&2; exit 2; }
    unmkinitramfs "$stage/casper/initrd-$flavor" "$unpack"
    [[ -f $unpack/main/scripts/casper || -f $unpack/scripts/casper ]] || {
        echo "Generated initrd lacks Casper script: $flavor" >&2; exit 1;
    }
    [[ -d $unpack/main/usr/lib/modules/$version || -d $unpack/main/lib/modules/$version || -d $unpack/usr/lib/modules/$version || -d $unpack/lib/modules/$version ]] || {
        echo "Generated initrd lacks matching modules: $version" >&2; exit 1;
    }
    python3 "$here/iso_recipe.py" uuid --extraction "$unpack" --stage "$stage" --flavor "$flavor"
done
# Detach every temporary pseudo filesystem before privacy recheck and squashfs.
for ((index=${#mounts[@]}-1; index>=0; index--)); do
    umount -- "${mounts[index]}"
    unset 'mounts[index]'
done
python3 "$here/iso_recipe.py" squash --root "$root" --output "$output" --stage "$stage"
python3 "$here/iso_recipe.py" manifest --root "$root" --output "$output" --stage "$stage"
# No --directory: grub-mkrescue discovers both installed BIOS and EFI platforms.
# GRUB's xorriso backend adds El Torito BIOS+EFI, protective MBR and hybrid GPT.
LC_ALL=C python3 "$here/iso_recipe.py" rescue --root "$root" --output "$output" --stage "$stage" --iso "$iso"
# Preserve actual structures for parent review and reject an incomplete hybrid.
LC_ALL=C xorriso -indev "$iso" -report_el_torito plain -report_system_area plain \
    > "$output/iso-boot-structure.txt" 2>&1
python3 "$here/iso_recipe.py" check-boot-report --report "$output/iso-boot-structure.txt"
python3 "$here/iso_recipe.py" evidence --root "$root" --output "$output" --stage "$stage" --iso "$iso"
printf 'DesOS ISO constructed; actual BIOS/UEFI boot and install validation pending: %s\n' "$iso"
