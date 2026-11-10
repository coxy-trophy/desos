#!/bin/bash
# Bounded container entrypoint. Never run directly on the host.
set -Eeuo pipefail
stage=tools
trap 'rc=$?; printf "{\"status\":\"failed\",\"stage\":\"%s\",\"exit_code\":%d}\n" "$stage" "$rc"; exit "$rc"' ERR
[[ $# == 3 ]]
expected_dev=$1
bytes=$2
expected_hash=$3
[[ "$expected_dev" =~ ^[0-9]+:[0-9]+$ ]]
[[ "$bytes" =~ ^[0-9]+$ && "$expected_hash" =~ ^[a-f0-9]{64}$ ]]
(( bytes > 0 && bytes <= 134217728000 && bytes % 2048 == 0 ))
# Ubuntu 26.04 defaults to uutils dd. This path deliberately requires the
# separately supplied GNU implementation before opening or querying a device.
dd_version=$(/usr/bin/gnudd --version)
[[ "$dd_version" == 'dd (coreutils) '* || "$dd_version" == 'dd (GNU coreutils) '* ]]
[[ "$dd_version" == *'Free Software Foundation'* ]]
stage=identity
[[ -b /dev/desos-target && -f /source.iso && ! -w /source.iso ]]
# Verify kernel identity inside the restricted device mapping too.
read -r hex_major hex_minor < <(stat -c '%t %T' /dev/desos-target)
actual_dev="$((16#$hex_major)):$((16#$hex_minor))"
[[ "$actual_dev" == "$expected_dev" ]]
sys=$(readlink -f "/sys/dev/block/$actual_dev")
[[ -d "$sys" && ! -e "$sys/partition" && "$sys" == *'/usb'* ]]
[[ "$(cat "$sys/removable")" == 1 && "$(cat "$sys/ro")" == 0 ]]
usb_ancestor=$sys
while [[ "$usb_ancestor" != / && ! -f "$usb_ancestor/idVendor" ]]; do
    usb_ancestor=${usb_ancestor%/*}
done
[[ -f "$usb_ancestor/idVendor" && -f "$usb_ancestor/idProduct" ]]
[[ "$(cat "$usb_ancestor/serial")" == 6912801040691779938 ]]
model=$(cat "$sys/device/model")
model="${model#"${model%%[![:space:]]*}"}"; model="${model%"${model##*[![:space:]]}"}"
[[ "$model" == ProductCode ]]
sectors=$(cat "$sys/size")
[[ "$sectors" =~ ^[0-9]+$ ]]
(( sectors * 512 == 134217728000 && bytes <= sectors * 512 ))
stage=source_validation
[[ "$(stat -c '%s' /source.iso)" == "$bytes" ]]
read -r source_hash _ < <(sha256sum /source.iso)
[[ "$source_hash" == "$expected_hash" ]]
stage=write_fsync
/usr/bin/gnudd if=/source.iso of=/dev/desos-target bs=4M count="$bytes" iflag=count_bytes,fullblock conv=fsync status=none
stage=direct_readback
# pipefail makes short/error reads or failed hashes fatal; GNU dd count_bytes
# reads precisely the ISO size, including the final 2048-aligned partial block.
readback=$(/usr/bin/gnudd if=/dev/desos-target bs=4M count="$bytes" iflag=direct,count_bytes status=none | sha256sum)
readback=${readback%% *}
[[ "$readback" == "$expected_hash" ]]
stage=source_revalidation
read -r final_hash _ < <(sha256sum /source.iso)
[[ "$final_hash" == "$expected_hash" && "$(stat -c '%s' /source.iso)" == "$bytes" ]]
printf '{"status":"verified","source_sha256":"%s","readback_sha256":"%s","bytes_verified":%s,"write_fsync":true,"direct_read":true,"source_unchanged":true}\n' "$source_hash" "$readback" "$bytes"
