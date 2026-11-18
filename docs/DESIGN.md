# DesOS 0.1 Preview — live and installable distribution

The owner explicitly requested an unattended 5–8 hour build, delegated routine
technical decisions, and approved erasing the connected ProductCode USB, serial
6912801040691779938, size 134217728000 bytes. No further design check-ins are
required for this authorized scope. The host's internal disks, bootloader and
running business containers must be preserved.

Deliver a private preview ISO that boots as DesOS, runs DEs-UI directly on DRM,
offers an installer, installs onto an empty disk, and subsequently boots the
installed DesOS. Flash the approved USB only after live/installation validation,
then verify the written bytes. A first preview is not all-hardware certification.

## Chosen architecture

- Ubuntu 26.04 package base, with explicit `ID=desos`, Ubuntu-compatible identity
  metadata and transparent upstream attribution. No preinstalled Ubuntu needed.
- Reuse the verified Ubuntu-native desktop packages from frozen source d462986;
  do not rebuild Rust or copy the development VM's accounts/SSH keys/state.
- Build official unmodified Linux 6.18.62 LTS source, verify its detached upstream
  signature, and use a reproducible DesOS configuration. It is the only kernel:
  no Ubuntu kernel is shipped or installable (decided 18 Nov 2026); the previous
  DesOS kernel is the recovery choice.
- Build a clean filesystem with debootstrap. Casper supplies read-only squashfs
  plus a writable live overlay. GRUB hybrid ISO boots both BIOS and UEFI.
- Calamares supplies the established partition/install backend, configured and
  branded for DesOS. Actual installation is tested on disposable VM disks only.
- Live account `desos` may use standard live-media passwordless administration;
  installed systems must remove live auto-login/sudo rules and create the user's
  chosen credentials. Never ship the development VM's credentials or host keys.
- Installer configuration lives in `/etc/calamares`, branding in
  `/usr/share/calamares/branding/desos`, source filesystem at
  `/cdrom/casper/filesystem.squashfs`, and launcher `/usr/bin/desos-installer`.
- Native desktop/kernel and DesOS integration packages have local signed
  repository metadata; publish nothing externally. Retain manifests, checksums,
  source/build provenance and test results alongside the ISO.

Building an entire independent package ecosystem or a new disk-partitioning
engine would not fit the requested delivery window. This is a full installable
Ubuntu-derived distribution with its own kernel/build/installer integration.

## Safety and acceptance

Guest/build work is isolated from the host. A separate build VM is based on the
immutable prototype checkpoint; owner demo state is preserved. USB writes use
only the specifically authorized removable device, with stable identity checks,
no mounted target filesystems, source hash validation, explicit erase receipt,
and full read-back verification. The internal SATA disk is always prohibited.
The live installer never automatically erases a physical disk on boot.

Required evidence: kernel source signature/config/build, clean image identity,
UEFI and BIOS live startup, native desktop readiness/input, installer completion
onto a disposable empty disk, installed-system boot/login and live-account
cleanup, ISO integrity, approved-device preflight and USB write/read-back.
Secure Boot and all physical-device/performance combinations remain separate
certification; record any limitation honestly rather than declaring a false pass.

## File ownership and parallel interfaces

- `kernel/`: pinned upstream metadata, source verification/config/build scripts;
  outputs Debian kernel packages and a kernel provenance manifest.
- `installer/`: Calamares settings/modules/branding, launcher and target cleanup;
  accepts the source filesystem path above and tests configuration/security.
- `image/`: clean rootfs, DesOS identity/integration/repository, initramfs and ISO;
  consumes native desktop packages, kernel packages and installer directory.
- `tools/`: guarded USB verification/write helpers, with no implicit target and
  tests on synthetic inventories/files; only the parent performs real USB writes.
- `tests/`: image recipe and integration contracts; actual VM results in `work/`.
- `work/`, `output/`, `.private/`: ignored build state, artifacts and credentials.
