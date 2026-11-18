# DesOS live ISO constructor

Run `build-iso.sh` only as root inside the dedicated Ubuntu VM bearing
`/etc/desos-build-worker`. It refuses ordinary host execution and requires
explicit, separate rootfs/output paths beneath `/srv/desos-build`:

```sh
bash /opt/desos-build/recipe/image/build-iso.sh \
  /srv/desos-build/rootfs /srv/desos-build/output \
  > /srv/desos-build/iso-build.log 2>&1
```

The clean rootfs must already identify as `ID=desos`, contain the desktop,
Calamares launcher, Casper and initramfs-tools, and provide:

- `/boot/vmlinuz-6.18.62-desos` with matching `/lib/modules/6.18.62-desos`.
- No Ubuntu `/boot/vmlinuz-*-generic`: one in the rootfs is refused, so the ISO
  boots only the DesOS kernel.

The worker needs `squashfs-tools`, `initramfs-tools-core`, `grub-common`,
`grub-pc-bin`, `grub-efi-amd64-bin`, `xorriso`, `mtools`, Python 3 and ordinary
core/mount tools. The rootfs needs its own working `update-initramfs`.
The recipe checkout must include `boot-theme/` beside `image/`: the menu theme is
staged from `boot-theme/rootfs/boot/grub/themes/desos`, and the build refuses to
start without it or with a menu that fails `grub-script-check`.

Seal the rootfs before invocation: remove SSH host private keys and random
seeds, empty/uninitialize machine IDs, audit copied credentials, and unmount
all rootfs child mounts left by package provisioning. The constructor checks
these known seal invariants and the DesOS identity. It consumes the explicit
clean tree; it never copies the development VM root or home directories.
Parent provisioning remains responsible for the complete privacy audit.

A fresh output directory is mandatory. Existing staging/ISO artifacts are
preserved by refusal; choose a new directory for a deliberate rebuild.
The script only constructs files. It never installs GRUB to a device, flashes
USB, partitions a disk, starts a VM or certifies boot behavior.

## Initrd and Casper matching

Both initrds are regenerated **inside the rootfs**, after its DesOS
configuration and custom Casper live-user hook have been installed. The
constructor temporarily mounts the worker's `/dev`, proc and sysfs under the
clean tree for this operation and unmounts them before squashfs construction.
A trap attempts cleanup on errors. Refuse/review any failed unmount before reuse.

For each kernel, `CASPER_GENERATE_UUID=1 update-initramfs -c/-u -k VERSION`
generates its own matching image. The complete image, including any early
microcode archive, is copied without recompression. `unmkinitramfs` then extracts
it to an audit directory. The constructor requires the Casper runtime script,
the selected version's module directory, and one consistent canonical UUID.
It copies the **actual generated** `conf/uuid.conf` to:

```
ISO/.disk/casper-uuid-upstream
```

This matches the inspected Ubuntu Casper **26.04.2** package source in
`work/package-examples/casper`: its initramfs hook writes `$DESTDIR/conf/uuid.conf`
when `CASPER_GENERATE_UUID` is nonempty, and runtime `matches_uuid()` compares
its UUID with every `/.disk/casper-uuid*` on candidate media. Consequently both
kernel/initrd pairs identify this one ISO without disabling UUID checks.

The installer source remains exactly `/cdrom/casper/filesystem.squashfs`.
`/boot` and both module trees remain **inside** the squashfs so Calamares can
install the kernel and regenerate installed-system initrds. Installer cleanup
must remove Casper/live-only configuration before its final initramfs job;
that is handled by the separate installer recipe.

## Filesystem and boot layout

Squashfs uses zstd level 8, a 1 MiB block size, two processors and a 512 MiB
memory limit. `-one-file-system` prevents crossing unexpected filesystem
boundaries. Explicit wildcard exclusions omit the contents of `/proc`, `/sys`,
`/dev`, `/run`, `/tmp` and `/var/tmp` while retaining their empty mountpoint
directories. Cached `.deb` files and the bootstrap marker are omitted.
The constructor performs a second preflight before compression and refuses any
remaining rootfs mount, rather than traversing guest pseudo filesystems.

`grub-mkrescue` discovers both installed `i386-pc` and `x86_64-efi` GRUB
platforms; no platform-specific `--directory` narrows the build. Its xorriso
backend constructs BIOS/UEFI El Torito entries and hybrid MBR/GPT structures
for optical or raw USB use. Explicit `--mbr-force-bootable` requests an active
MBR entry even with a protective GPT partition. See the upstream
[xorrisofs manual](https://www.gnu.org/s/xorriso/man_1_xorrisofs.html) for the
GRUB MBR/EFI integration and active-partition semantics. A post-construction xorriso report must contain both
boot platforms, hybrid MBR/GPT metadata and a bootable MBR entry. The actual
report is retained for review. Secure Boot is not configured or certified.

GRUB shows the DesOS boot theme (`boot-theme/`, staged to
`/boot/grub/themes/desos` on the medium; a missing theme fails `prepare`). The
menu has these entries, each titled `Name - what it does`:

1. **Try DesOS**, default after eight seconds, upstream kernel.
2. **Install DesOS**, upstream kernel, adds only `desos.install=1`.
3. **Debug DesOS upstream**, verbose boot diagnostics.
4. **Boot from first hard disk**. UEFI: `exit 1`, so the firmware continues
   with its next boot entry. BIOS: chainloads the first disk that is not the
   live medium. A USB stick is `hd0` to the BIOS, so the entry then swaps
   `hd0`/`hd1` with `drivemap` and starts `hd1`; from optical media it starts
   `hd0`. It returns to the firmware console first, because a boot sector
   expects the BIOS text mode.
5. **UEFI firmware settings**, only on UEFI and only when
   `fwsetup --is-supported` succeeds.

### Graphics and fallback

`grub.cfg` enables the theme only when `theme.txt` exists on the medium, all
theme fonts load and `terminal_output gfxterm` succeeds, in that order. Any
failure leaves the standard text menu on the firmware console (light grey,
cyan highlight). The mode is `gfxmode=auto` with `gfxpayload=keep`, and the
graphical terminal is filled with the splash ground (`background_color
6,8,11`), so the screen does not flash black between the menu and Plymouth.

### Consoles

Entries 1 to 4 use `boot=casper live-media-path=casper username=desos
hostname=desos-live nopersistent ${desos_serial} console=tty0`.
Try, Install and Compatibility add `quiet splash
plymouth.ignore-serial-consoles`. Install differs from Try only by
`desos.install=1`. Debug omits `quiet splash` and adds `debug loglevel=7
systemd.show_status=1`.

Earlier revisions ended every entry with `console=tty0
console=ttyS0,115200n8`. The last `console=` becomes `/dev/console`, so on
ordinary hardware service output and the emergency shell went to a serial port
nobody was watching, and Plymouth, seeing a serial console, dropped the
graphical splash. Now:

- GRUB runs `serial --unit=0 --speed=115200 ...` inside an `if`. Its driver
  tests the UART's scratch register, so on a machine without one the command
  fails: no serial terminal is added and `${desos_serial}` stays empty (an
  empty unquoted variable adds no kernel argument).
- With a UART, GRUB adds the serial terminal beside the screen (QA sees the
  text menu on the serial line) and passes `console=ttyS0,115200n8` to the
  kernel: kernel messages and a serial login reach QA as before.
- `console=tty0` is always last, so `/dev/console` is the display on every
  machine, including one whose UART exists but is not connected.
- `plymouth.ignore-serial-consoles` keeps the graphical splash when the
  serial console is present.

For QA the difference from before is that `/dev/console` output (systemd
status lines, an emergency shell) is on the display, not on the serial line.
The kernel log, including systemd's messages under `debug`, and the serial
getty are unchanged. The rootfs owns graphical live autologin and the standard
serial login/getty configuration. The recipe contains no automatic disk
installation or wipe behavior.

## Artifacts and verification boundary

Output contains:

- `desos-0.1-preview-amd64.iso` and its `.sha256` sidecar.
- `iso-provenance.json`: exact kernel versions, Casper UUIDs, recipe/tool
  identities and stage/ISO hashes. Boot-validation status remains **pending**.
- `iso-boot-structure.txt`: actual xorriso El Torito/system-area report.
- `filesystem.manifest`, `filesystem.size` and retained `iso-staging/`.
- `initrd-audit-upstream/` for matched-module/UUID review.

The ISO includes `/casper/filesystem.manifest`, `filesystem.manifest-desktop`,
`filesystem.size`, both kernel/initrd pairs, `.disk` identity/UUID markers,
`/boot/grub/grub.cfg`, the boot theme under `/boot/grub/themes/desos` and
`/md5sum.txt`. The MD5 list supports Casper media
checking; SHA256 sidecars/provenance support artifact integrity. Bootloader
files added internally by grub-mkrescue are outside the prebuilt MD5 list.

Host-safe recipe verification:

```sh
python3 -m unittest discover -s tests -p test_iso_recipe.py -v
bash -n image/build-iso.sh
python3 -m py_compile image/iso_recipe.py
```

These validate command/menu/path/UUID contracts and run `grub-script-check`
over the generated menu when it is installed. `boot-theme/tools/preview.py`
boots the generated menu in a GRUB-only BIOS QEMU for a visual check; it does
not replace booting the ISO. Parent must still construct
the ISO in the worker, inspect the hybrid partition report, boot it using both
BIOS and UEFI, confirm DesOS/live desktop/input/network/kernel identities, run
Calamares against a disposable empty VM disk, and boot the installed system.
Successful construction and checksum verification alone do not establish any
of those runtime acceptance results.
