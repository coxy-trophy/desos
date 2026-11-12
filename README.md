# DesOS

DesOS is an x86-64 Linux desktop distribution combining the native DEs-UI
desktop, unmodified upstream Linux **6.18.62**, and the Ubuntu **26.04** package
base. This repository builds a clean live system and a Calamares installer; it
does not copy the development machine or install over the host system.

The first release is **0.1 Preview**. It has its own release identity, kernel
configuration, live boot menu, installer integration and signed component
repository. Ubuntu remains the package base and supplies a clearly labelled
compatibility kernel. This is a derivative distribution, with the upstream
projects' authorship and licenses preserved.

## Booting the preview

The hybrid ISO is intended for BIOS and UEFI boot. The themed menu offers Try
DesOS, Install DesOS, the Ubuntu compatibility kernel, upstream kernel
diagnostics, booting the first hard disk and, on UEFI, firmware setup. It falls
back to a plain text menu if the theme cannot load.
Try boots a temporary desktop without installing. Install opens Calamares;
partition changes require its own explicit confirmation.

The public live account is **desos**, password **desos**, with passwordless live
administration. Installation creates a separate account and removes the live
account, live privileges, automatic login and Casper startup hooks. Live changes
are temporary; USB persistence is not configured.

Secure Boot and encrypted installation are not enabled in this preview. The
installer now defaults to btrfs with subvolumes (ext4 stays selectable); the
only installation validated so far is the 0.1 image onto an empty ext4 disk. Existing-OS
preservation, encrypted storage and physical hardware support require their own
tests. VM results do not establish those guarantees.

## Build organization

- `kernel/` pins the official source, maintainer signature and configuration,
  compiles Debian packages in an isolated Ubuntu VM, checks the unchanged source
  tree and scans generated packages for private signing material.
- `image/` bootstraps the clean system, builds the release identity package and
  signed offline component repository, configures services, seals transient
  identity and credentials, and constructs the BIOS/UEFI live ISO.
- `installer/` configures the packaged Calamares implementation, DesOS branding,
  fixed-command live elevation and installed-system cleanup.
- `system/`, `undo/`, `verify/`, `boot-theme/` and `phonekey/` are components:
  a `component.json` (packages, units) and a `rootfs/` tree that
  `image/components.py` installs into the image. Each has its own README.
  - `system/`: AppArmor, a default-deny firewall with a LAN exception for DES
    Connect, unattended Ubuntu security updates, zram swap, and the disabled
    network update channel (`desos-update-channel`).
  - `undo/`: `desos-undo` snapshots around every package change, rollback, and
    automatic recovery after repeated failed starts (btrfs installs only).
  - `verify/`: `desos-verify` checks the running system against the signed
    build record at every boot and in full once a day.
  - `boot-theme/`: the GRUB theme for the live medium and installed systems.
  - `phonekey/`: phone approval for administrator actions; ships switched off.
- `tools/` contains the separately guarded, explicitly authorized USB workflow.
- `tests/` exercises configuration, package behavior, path protections, cleanup,
  signing metadata and USB refusal conditions using disposable fixtures.

Read [the design](docs/DESIGN.md), [build plan](docs/PLAN.md),
[kernel recipe](kernel/README.md), [ISO recipe](image/ISO.md),
[installer contract](installer/README.md) and [USB procedure](tools/usb-README.md)
before reproducing the build. Build scripts require a dedicated Ubuntu VM with
`/etc/desos-build-worker` and an explicit rootfs beneath `/srv/desos-build`.
Private signing keys, guest access material, temporary VM disks and output
artifacts are excluded from version control.

Run host-safe contract checks with:

```sh
python3 -m unittest discover -s tests -v
```

Everything added after the 0.1 image (the components above, the btrfs default,
the installer redesign and the new boot menu) has passed only these host-safe
tests. None of it has been built into an image or booted; the boot menu alone
was rendered, in BIOS GRUB under QEMU.

Construction and unit checks are separate from runtime acceptance. A releasable
artifact requires actual BIOS/UEFI boot, native desktop/input, empty-disk
installation, installed-system boot and live-account cleanup evidence. USB
delivery additionally requires full written-span read-back matching the tested
ISO's SHA-256.

## Updates and release scope

Ubuntu's signed archive supplies base-system updates. The bundled signed offline
repository records the exact DesOS desktop, release and upstream kernel packages
used in the image. No public DesOS update service is configured: a second,
network apt source ships disabled with a placeholder address, and kernel
packages are still named by version, so a newer kernel needs a metapackage
before any channel could deliver it. The preview
signing key is a local development key, not a production key-management service.
Maintaining upstream kernel security updates requires rebuilding and releasing
new DesOS packages; Ubuntu's compatibility-kernel updates do not update the
separate upstream kernel automatically.

Release checksums, signatures, component provenance and observed runtime results
belong alongside the final ISO. Do not infer completion from this repository's
existence or from an ISO being successfully constructed.
