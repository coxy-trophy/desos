# DesOS Calamares integration

This tree targets Ubuntu 26.04's **Calamares 3.3.14-0ubuntu25.26.04.1** and
**Casper 26.04.2**, using the shipped real Calamares modules. No partitioning,
formatting, bootloader-installation or password-setting implementation is replaced.

The configuration was checked against [Calamares v3.3.14 source and example
configuration](https://github.com/calamares/calamares/tree/v3.3.14), commit
`21ea803527735cfaf54fa6059e71d1ef65004864`, and the extracted Ubuntu module
descriptors/Python schemas. Ubuntu's bootloader module adds persistent GRUB
debconf install-device selection; that packaged implementation remains in use.
Module configuration follows the upstream [deployer documentation](https://github.com/calamares/calamares/wiki/Deployer's-Guide).

## Rootfs contract

Set DesOS identity first, install the packages below, then run as root:

```sh
installer/install-into-rootfs.sh /absolute/path/to/clean-desos-rootfs
```

The entry point refuses `/`, unsafe top-level directories, relative/symlinked
root paths, destinations containing symlinks, non-DesOS identity and non-root
execution. It copies root-owned configuration/assets only. It does not enter the
rootfs, mount filesystems, run a service, install a bootloader, or operate a disk.
Standard `/etc/os-release -> ../usr/lib/os-release` is accepted only inside the
selected rootfs.

The build supplies:

- A clean Ubuntu 26.04 root filesystem, `ID=desos`, normal locked root account,
  `/bin/bash`, existing `sudo`, `audio`, `video` and `render` groups, standard
  Ubuntu `%sudo` password-based privileges, and the native DEs-UI packages.
- `calamares`, `pkexec`/`polkitd`, `python3`, the matching Qt Wayland client plugin,
  QtQuick QML runtime, Qt SVG support, `rsync`, `squashfs-tools`, `locales`,
  `console-setup`/`ckbcomp`, `initramfs-tools`, `sudo`, `grub-common`, `grub2-common`,
  `grub-pc-bin`, `grub-efi-amd64-bin` and `efibootmgr`. Keep both BIOS and UEFI GRUB
  binary packages. Preview Secure Boot is not supported; no signing is claimed.
- Built kernel images and initrds in `/boot`; modules in `/lib/modules`.
- A live-only `desos` user and `/etc/sudoers.d/90-desos-live` (live sudo is owned
  by the image recipe). The live greetd configuration has an `initial_session`
  running `/usr/lib/desos/live-session` as `desos`; this wrapper normally execs
  `/usr/bin/des-ui-session`. Installed greetd uses the normal
  `/usr/bin/des-ui-greeter` running as `_greetd` on VT 7, matching the pinned
  Ubuntu greetd unit's `Conflicts=getty@tty7.service`. This avoids competing with
  the ordinary getty on VT 1.
- The original source image at `/cdrom/casper/filesystem.squashfs`, mounted by
  Casper. The boot command line must contain the exact `boot=casper` token.

The installer copies this **original SquashFS**, never the mutated live overlay.
This prevents Casper's runtime `update-initramfs` diversion, extra `.pkla`
privileges and live session data from being copied to the target.

## Elevation and session display

The desktop entry launches `/usr/bin/desos-installer` with no arguments. It asks
pkexec to run one fixed root helper. The policy grants only
`org.desos.installer.launch` to the local, active `desos` account; it does not
grant arbitrary commands or create a general administrator rule.

The helper verifies caller identity, active logind user, live boot token and
SquashFS source magic. It accepts only local numeric X displays and a
`wayland-N` socket, checks socket ownership, constrains Xauthority files to the
live home/runtime directory, drops inherited environment variables, and execs
exactly `/usr/bin/calamares` from `/`. Its normal root-owned search paths load
`/etc/calamares` and `/usr/share/calamares/branding/desos`; the testing-only `-c`
option is deliberately avoided because it would hide the separate branding path.

Native Wayland takes precedence when `WAYLAND_DISPLAY` is present; this requires
the matching Qt Wayland plugin in the live rootfs. Xwayland uses the caller's
explicit `DISPLAY` and `XAUTHORITY` when no Wayland display is supplied. No
`xhost +`, inherited plugin paths, caller-selected config or root shell is used.
Calamares logs are root-only because the helper sets umask 077. Installer errors
can be inspected by running `desos-installer` from the live terminal.

## Installation sequence and cleanup

The workflow presents language/timezone, keyboard, disk, account and summary
pages. No disk operation is selected by default; Calamares asks for confirmation
before executing. Installation is offline; no package downloads or remote GeoIP,
telemetry, slideshow or log-upload endpoint is configured.

After extracting the rootfs and creating the chosen user, `desoscleanup` validates
that Calamares's target is a mounted DesOS filesystem and that the chosen user
exists with an enabled password. `desos` is a forbidden new username. Before
mutating, it rejects symlink parents for every path it will touch. It then:

1. Purges Casper from the target using offline `apt-get purge`.
2. Deletes the live account and home, live sudo/polkit privileges, installer
   launch/elevation files, root SSH material and SSH host keys, live getty/greetd
   overrides, the image's `99-desos-live` initramfs hook and live-session wrapper,
   and Casper-only configuration.
3. Writes greetd configuration with only its authenticated `default_session`.
   The new user's own account/home is preserved. The root account remains locked.
4. Writes a small receipt at `/var/lib/desos/installation.json` containing no
   account names, answers or credentials.

Calamares then updates **all installed initrds**, creates GRUB configuration with
only normal installed boot parameters, installs BIOS or UEFI GRUB according to
the firmware used to boot the live medium, and unmounts the target. Do not add
`boot=casper` to installed GRUB settings. On failure, Calamares's emergency umount
job remains available and the installer does not claim successful cleanup.

The image integration owns `/usr/lib/desos/select-upstream-kernel`. The final
`shellprocess` job executes that helper inside the target **after bootloader**
has generated GRUB's menu and **before umount**. A missing helper or nonzero exit
is fatal. The installer first requires a nonempty `/boot/grub/grub.cfg`; a
bootloader module that skipped installation cannot silently pass this final job.
This installer-only guard leaves normal package post-install behavior unchanged.
The helper selects the discovered `*-desos` kernel using generated
stable menu IDs and `grub-set-default`, while preserving Ubuntu's compatibility
kernel as recovery. The image supplies `/etc/default/grub.d/10-desos.cfg` with
`GRUB_DEFAULT=saved` and `GRUB_TOP_LEVEL` set to the newest DesOS kernel, so
the first menu line, "DesOS GNU/Linux", starts the DesOS kernel; the helper
chooses that line, or the kernel's own entry under Advanced options if the top
line starts something else. It updates the choice after later kernel changes.
This prevents a numerically higher Ubuntu kernel becoming the default or taking
the top line (on the first real install the menu opened on "Advanced options",
and moving to the top line started Ubuntu's kernel). No kernel
release is hardcoded here. `grubcfg` leaves integration files in
`/etc/default/grub.d/` intact.

## Preview scope and validation

The preview supports the empty-disk ext4 installation path on x86-64 BIOS and
UEFI. It requires at least 16 GiB available disk and 2 GiB RAM; use a 24–32 GiB
or larger disposable VM disk for verification. "Erase disk" opens selected:
it makes the EFI partition and root itself (the first real install was
partitioned by hand because no swap could be asked for). Swap is a choice:
none (zram only, the default), a small swap partition, or one the size of RAM
for hibernation, passed to the kernel as `resume=` (10-desos.cfg adds `splash`
without replacing the command line). There is no swap-file choice: an active
swap file in `@` stops btrfs snapshotting it. Hibernation has not yet been
tested on hardware. Automatic encryption and LVM are disabled pending
dedicated unlock tests. Manual partitioning
remains available; Calamares may offer Alongside/Replace on populated disks,
because 3.3.14 has no supported filtering key for these choices. Those paths
are outside the current acceptance scope. Never claim existing-OS preservation
from empty-disk VM results.

Automatic login is disabled visibly via the named Calamares checkbox's Qt
`enabled` property. Cleanup enforces authenticated greetd even if an upstream UI
change bypasses that property. Qt/QML own keyboard and form behavior; native
rendering, focus, scaling and the disabled checkbox must be inspected in the
live VM before visual acceptance.

Run local contracts without touching any disk:

```sh
python3 -m unittest discover -s tests -p 'test_installer*.py'
python3 installer/verify-config.py --modules-directory /path/to/extracted/calamares/usr/lib/x86_64-linux-gnu/calamares/modules
```

The parent image builder performs real BIOS/UEFI live boots, GUI installation on
disposable empty disks, then installed-system boot/new-user sign-in and checks
absence of `desos`, Casper hooks, live sudo/polkit rules, initial_session and SSH
keys. Local configuration/unit checks are not a substitute for these results.

## Changes after the 0.1 image (not yet built or booted)

- Branding is described in [DESIGN.md](DESIGN.md): a QML step rail
  (`sidebar: qml`; set `sidebar: widget` in `branding.desc` to return to the
  stock list), a six-card slideshow and PNG artwork generated by
  `branding/build_art.py`.
- The partition module defaults to btrfs and `mount.conf` creates the
  subvolumes `desos-undo` expects (see `../undo/README.md`); ext4 remains
  selectable.
- The GRUB theme arrives through `/etc/default/grub.d/20-desos-theme.cfg`
  (see `../boot-theme/README.md`). `grubcfg.conf` must not set
  `GRUB_TERMINAL_OUTPUT`, which would turn the theme off.
- Target cleanup runs with `DESOS_UNDO_DISABLE=1` so the snapshot hooks stay
  inert while the live state is removed.
