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
- `image/` bootstraps the clean system, builds the release identity package,
  the kernel metapackages, the `desos-verify` package and the signed offline
  component repository, configures services, seals transient identity and
  credentials, constructs the BIOS/UEFI live ISO, and prepares (offline) the
  tree for the network update channel.
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

## Building the signed repository (host, where the key is)

Nothing below has been run end to end into an image: the tools are covered by
the host-safe tests only. Paths are examples; every output directory must be
new. The signing key lives in a dedicated GnuPG home on the host and never
enters the image worker.

```sh
python3 image/release_package.py build --output /abs/desos-packages     # desos-release
python3 image/verify_package.py build --output /abs/desos-packages      # desos-verify
python3 image/kernel_metapackage.py build --output /abs/kernel-meta     # linux-image-desos, linux-headers-desos
python3 image/component_repo.py \
  --deb-input /abs/desktop-debs --deb-input /abs/kernel/linux-image-6.18.62-desos_6.18.62-1desos1_amd64.deb \
  --deb-input /abs/kernel/linux-headers-6.18.62-desos_6.18.62-1desos1_amd64.deb \
  --deb-input /abs/kernel-meta --deb-input /abs/desos-packages \
  --evidence /abs/kernel/kernel-provenance.json --evidence /abs/kernel/source-pin.json \
  --evidence /abs/kernel/signature-status.txt --evidence /abs/kernel/signature-log.txt \
  --evidence /abs/kernel/config-6.18.62-desos --evidence /abs/kernel/baseline.config \
  --evidence image/desktop-pin.json \
  --output /abs/component-repository --gpg-home /abs/dedicated-gnupg --key-fingerprint FULL_FINGERPRINT
```

- The worker's input directory `release/` must hold both `desos-release` and
  `desos-verify` (`build-rootfs.sh` installs them and refuses without the
  latter); `finalize-rootfs.sh ROOTFS REPOSITORY [KERNEL-PROVENANCE-DIR]` then
  installs the kernel through `linux-image-desos`/`linux-headers-desos`,
  copies the **signed** build evidence out of the repository and runs the
  shipped `desos-verify` against the tree before sealing it.
- `image/package-ledger.json` records the SHA-256 first seen for every package
  name, version and architecture. The repository builder refuses a package
  whose bytes differ from its entry (raise the version instead) and appends
  new entries after a successful build; **commit the ledger with the build**.
  Use `--ledger-check-only` for a trial build that should record nothing.
  `desos-release` moved to `0.1.0~preview1+1` for this reason: its content
  changed after `0.1.0~preview1` had shipped in the 0.1 image.

## Publishing the update channel (offline; operator steps)

`image/publish_channel.py` turns a signed component repository into the tree
to be served as the network apt source the image already carries, disabled,
in `/etc/apt/sources.list.d/desos-updates.sources` (same archive key,
`Origin: DesOS`, `Suite: desos`). It validates every signature, index and
package of its inputs, refuses unsigned or incomplete ones, signs the new tree
through the same dedicated GnuPG home, writes only inside `--output`, and
**does not upload**.

1. Build the new packages and a new signed component repository as above
   (for a kernel update: repin `kernel/source-pin.json`, build the kernel,
   then `kernel_metapackage.py` gives the metapackages that depend on it).
2. Prepare the tree. The first time:
   ```sh
   python3 image/publish_channel.py --repository /abs/component-repository \
     --output /abs/channel-1 --gpg-home /abs/dedicated-gnupg --key-fingerprint FULL_FINGERPRINT
   ```
   Every later time, name the tree that is being served now, so its packages
   stay available beside the new ones:
   ```sh
   python3 image/publish_channel.py --repository /abs/component-repository-2 --previous /abs/channel-1 \
     --output /abs/channel-2 --gpg-home /abs/dedicated-gnupg --key-fingerprint FULL_FINGERPRINT
   ```
   The JSON result lists what was `added` and `kept` and any `warnings` (a
   version that is not newer than what is published). A package name and
   version already published with other bytes is refused.
3. Check it: `gpg --no-options --homedir /abs/dedicated-gnupg --verify
   /abs/channel-2/dists/desos/InRelease`, and read `manifest.json`. A tree
   with an `.incomplete` marker must not be served.
4. Copy the tree, unchanged, to the web server directory that answers the
   channel URL, so that `URL/dists/desos/InRelease` and `URL/pool/...` exist
   (copy `pool/` before `dists/`). This step is yours; no tool here does it.
   Keep the tree: it is the next `--previous`.
5. Only when a real server exists: set `UPDATE_CHANNEL_URL` in
   `image/configure.py` (the one definition) for the next image, or edit the
   `URIs:` line on an installed system, then `sudo desos-update-channel
   enable` and `sudo apt update`. The shipped `.invalid` placeholder can never
   be enabled.

Not done by the tool: uploading, `Valid-Until` (a replayed old tree is not
detected by apt), removing old packages from the channel, key rotation. Old
DesOS kernels are removed from a machine by apt itself (`apt autoremove`,
and unattended-upgrades' unused-kernel removal) once the metapackage no longer
depends on them and they are not running; see `kernel/README.md`.

## Updates and release scope

Ubuntu's signed archive supplies base-system updates. The bundled signed offline
repository records the exact DesOS desktop, release, verifier and upstream
kernel packages used in the image. No public DesOS update service is
configured: a second, network apt source ships disabled with a placeholder
address, and nothing has been published. The metapackages `linux-image-desos`
and `linux-headers-desos` are what would let that channel deliver a newer
kernel; this is tested only as package metadata, not on a running system. The
preview signing key is a local development key, not a production
key-management service.
Maintaining upstream kernel security updates requires rebuilding and releasing
new DesOS packages; Ubuntu's compatibility-kernel updates do not update the
separate upstream kernel automatically.

Release checksums, signatures, component provenance and observed runtime results
belong alongside the final ISO. Do not infer completion from this repository's
existence or from an ISO being successfully constructed.
