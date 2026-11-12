# Guarded DesOS USB write

Only the parent operator executes this workflow after observing passing live ISO
and installation tests. The tools never unmount, format, repartition, power off,
or discover an alternate target. No invocation below should be run by a test.
Only this owner's explicit authorization applies:

- by-id `/dev/disk/by-id/usb-VendorCo_ProductCode_6912801040691779938-0:0`
- model `ProductCode`, USB serial `6912801040691779938`, removable
- exact kernel-reported capacity `134217728000` bytes
- internal `/dev/sda`, serial `ES72N072110902A1X`, always prohibited

The existing USB60GB/exFAT metadata has a size mismatch. Successful readback will
prove only the ISO byte span. It cannot certify all advertised 128 GB or the
hardware's long-term reliability. Writing the ISO also leaves data beyond its
span untouched; this is not a secure whole-drive wipe.

## Prerequisites

The owner approved erasure in `.private/usb-authorization.json`. Do not edit it
or read other private files. Keep the ISO and helper unchanged during the run.
The ISO must be a regular file, aligned to 2048 bytes and smaller than the target.
Use absolute artifact paths. Existing receipt files are never overwritten.

The tested-ISO JSON must be generated from actual observed tests, not from the
example values below. It binds the exact canonical source path, size and SHA256:

```json
{
  "iso_path": "/absolute/path/to/desos.iso",
  "sha256": "<actual 64-character lowercase SHA256>",
  "size_bytes": 1234567890,
  "tests": {
    "bios_live": true,
    "uefi_live": true,
    "native_desktop": true,
    "installer_empty_disk": true,
    "installed_boot": true,
    "live_account_cleanup": true,
    "iso_integrity": true
  }
}
```

The example size is a placeholder; the actual size must be divisible by 2048.
A receipt is trusted local evidence from the build operator, not a signed
attestation service.

Use the already pulled official Ubuntu 26.04 image and retain its pull/RepoDigest
evidence. The current operator-recorded image is:
`ubuntu@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c`.
The helper refuses tag-only or nonofficial image names and uses `--pull=never`.
The Docker daemon and locally stored image are part of the trusted host.
The pinned image supplies GNU coreutils 9.7 as `/usr/bin/gnudd`; its default
`dd` is uutils 0.10.0. The helper explicitly checks the GNU version banner before
any device access and uses `/usr/bin/gnudd` for both writing and direct readback.
An absent or unexpected GNU implementation fails at the `tools` stage without
opening the target.

If the target or any partition is mounted, the operator must explicitly unmount
only its identified mounted partitions using the authorized desktop storage
mechanism (for example `udisksctl unmount -b /dev/sdb1` for the confirmed current
partition). Recheck identity after unmounting. This helper cannot unmount it and
will refuse all mounted targets. Do not operate host services or remove internal
mounts. Do not browse the target filesystem.

## Manual calls

From the `desos` directory, substitute the actual absolute artifact paths and
new receipt names. Both calls independently require authorization, target,
source test evidence, image and audit destination:

```sh
python3 tools/usb_guard.py preflight \
  --authorization .private/usb-authorization.json \
  --target /dev/disk/by-id/usb-VendorCo_ProductCode_6912801040691779938-0:0 \
  --iso /absolute/path/to/desos.iso \
  --tested-receipt /absolute/path/to/iso-tested.json \
  --image ubuntu@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c \
  --receipt /absolute/path/to/usb-preflight.json
```

Only after preflight and actual image tests pass, the parent operator may run:

```sh
python3 tools/usb_guard.py flash \
  --authorization .private/usb-authorization.json \
  --target /dev/disk/by-id/usb-VendorCo_ProductCode_6912801040691779938-0:0 \
  --iso /absolute/path/to/desos.iso \
  --tested-receipt /absolute/path/to/iso-tested.json \
  --image ubuntu@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c \
  --receipt /absolute/path/to/usb-write.json
```

A zero exit status with `status=verified` is the only successful flash result.
Failures return nonzero JSON and retain an audit receipt. A failure or interrupted
process after write intent may leave a partial ISO; never infer successful media
from a recognizable partition label. Abrupt process termination can leave the last
persisted status `writing`/`write_pending`, which is an incomplete result. Inspect
Docker state after an abrupt host interruption; do not start a second writer.

## Bounds and verification

Host checks use `lsblk`, actual block-device `stat`, `/proc/self/mountinfo` and
`/proc/swaps`. They reject the wrong identity, capacity, model, transport,
read-only flag, removable flag, duplicate approved serial, mapped descendants,
and mounted or swap target partitions. Checks run again immediately before the
Docker launch. The exact by-id input is mandatory. Docker's colon-separated
`--device` syntax cannot represent the colon in this by-id name, so only its
just-verified canonical block path is mapped; there is no fallback search.

The container gets one approved read/write device node plus two read-only binds:
the source ISO and `usb_helper.sh`. It uses no host PID/mount namespace, no host
root bind, no network, no capabilities, a read-only root filesystem and no-new-
privileges. Device access uses the owner's explicitly authorized Docker access;
there is no sudo, privilege escalation, or privileged container. The helper is a
container entrypoint, never a direct host script. It verifies the mapped block
node's major/minor and its kernel sysfs USB serial/model/removable/size again.

GNU `/usr/bin/gnudd` writes at most the tested ISO size with `oflag=direct` and
`conv=fsync`. Direct writes keep dirty device pages out of the container's
memory budget. The helper then reads exactly every ISO byte using
`iflag=direct,count_bytes` and `sha256sum`,
compares to the tested source hash and rehashes the source again. Direct readback
bypasses the host page cache. No broad cache dropping or privileged BLKFLSBUF ioctl
is used. `fsync` is the recorded flush outcome; storage firmware may still have
its own behavior, and power-cycle hardware certification is outside this test.
Errors from either read/hash pipeline stage fail closed.

The audit records source/test/authorization/helper hashes, device identity,
revalidation times, exact bounded container command/image ID, exit/stderr,
readback hash/byte count, and fsync/direct-read outcomes. JSON stdout is concise.
The only persistent host writes are explicitly named audit receipts and the
approved target ISO span.

There remains an unavoidable interval between checking host mount state and
opening the device inside Docker; another actor or desktop automounter must not
mount or replace the target during this operation. Do not change the device or
source while running. The tool detects identity changes at its checkpoints; it
does not lock every host mount namespace or control host automount services.

## Nondestructive verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_usb*.py'
bash -n tools/usb_helper.sh
```

Tests use synthetic inventories, temporary ordinary source files and a fake
runner. The production target block-device check has no regular-file escape
hatch. The tests never invoke Docker, unmount, format, or touch real USB data.
