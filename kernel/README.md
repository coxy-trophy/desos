# DesOS preview kernel

This recipe builds official, unmodified Linux **6.18.62** as
`6.18.62-desos`, Debian package version `6.18.62-1desos1`. It never installs a
kernel or changes a bootloader. Compilation is refused without `--guest-build`,
a detected VM, and Ubuntu identification. Only the parent runs the worker VM.

## Upstream authentication and pin

`source-pin.json` pins the source archive, detached signature, uncompressed tar,
release metadata snapshot and bundled public key by SHA256. The version was a
maintained `longterm` entry (`iseol=false`) in
[official releases.json](https://www.kernel.org/releases.json) on 2026-11-10.
The complete public JSON response is retained in `releases-2026-11-10.json`.

The detached signature is over the **uncompressed tar**, as documented by
[kernel.org signature instructions](https://www.kernel.org/signature.html).
That independently fetched primary documentation lists Greg Kroah-Hartman's
primary fingerprint:

```
647F 2865 4894 E3BD 4571 99BE 38DB BDC8 6092 693E
```

The public `gregkh.asc` key was fetched using the official documented WKD method:

```
gpg --homedir work/kernel/gnupg --batch --auto-key-locate clear,wkd --locate-keys gregkh@kernel.org
gpg --homedir work/kernel/gnupg --armor --export-options export-minimal --export 647F28654894E3BD457199BE38DBBDC86092693E
```

The key fingerprint and signature's `VALIDSIG` primary fingerprint must both
match the pinned fingerprint exactly. A merely successful import, key ID,
`GOODSIG`, or checksum alone is insufficient. Bad, expired or revoked signature
statuses are rejected. GnuPG uses an isolated work keyring with no automatic key
retrieval, and the bundled public key is imported only after its hash and full
primary fingerprint are checked. GnuPG's normal unknown-owner-trust warning is
expected: identity is established by the separately consulted official
fingerprint, without changing personal trust settings. No maintainer secret keys are needed or shipped.

Replaying the recipe uses the frozen metadata snapshot; it does not silently
advance the selected version when upstream releases.json changes. Updates must
explicitly repin and verify the upstream status and maintainer fingerprint.

## Worker setup and commands

Use an isolated Ubuntu x86-64 worker with two build jobs by default, around 5 GiB RAM plus
swap, and at least 25 GiB free (a full inherited generic config can require more).
Provision ordinary Ubuntu package dependencies in the worker, never the host:

```sh
sudo apt-get install build-essential flex bison libssl-dev libelf-dev libdw-dev kmod bc rsync \
  fakeroot dpkg-dev debhelper dwarves gnupg python3 xz-utils
```

Copy this entire `kernel/` directory to `/opt/desos-build/recipe/kernel/` and the
complete original Ubuntu generic `/boot/config-*` to the explicit baseline path.
Existing authenticated downloads can be copied to the work directory to avoid
redownloading. `.tar.xz` and `.tar.sign` keep their exact upstream filenames.
Do not copy a development home directory, SSH state or private keys.

```sh
bash /opt/desos-build/recipe/kernel/build-guest.sh --guest-build \
  /opt/desos-build/kernel /opt/desos-build/kernel/baseline.config \
  > /opt/desos-build/kernel-build.log 2>&1
```

The baseline is mandatory; there is no `localmodconfig` or host-config fallback.
The script freezes its original baseline, configures via upstream
`scripts/config` and `olddefconfig`, and checks the final effective config.
An existing valid build config is preserved to resume an interrupted build;
use a new work directory after changing the baseline or compiler/config recipe.
The concurrency-only `DESOS_BUILD_JOBS` setting accepts exactly **1, 2, 3 or 4**;
unset defaults to 2 and invalid/empty values fail before work begins. It controls
both outer make and Debian's nested make via `DEB_BUILD_OPTIONS`, and the final
invocation's actual bounded count is written as `build_jobs` in provenance.

```sh
DESOS_BUILD_JOBS=4 bash /opt/desos-build/recipe/kernel/build-guest.sh --guest-build \
  /opt/desos-build/kernel /opt/desos-build/kernel/baseline.config
```

To increase concurrency, first gracefully stop the active build and wait until
all old make/compiler processes have exited. Then resume the same work directory
with the unchanged source, baseline, `.config`, toolchain and build identity.
Do not clean the build or run two make trees against one `O=` directory. Kbuild
tracks completed objects/commands and deletes interrupted outputs; changing job
slots alone does not change compiler flags or require a clean rebuild. Record
any earlier concurrency separately in the parent build audit: the final field
records the resumed invocation, not a claim that every object used that count.

Rootless preparatory commands are also available without invoking compilation:

```sh
python3 kernel/kernel_tools.py fetch --work work/kernel
python3 kernel/kernel_tools.py extract --work work/kernel
python3 kernel/kernel_tools.py configure --work work/kernel --baseline work/kernel/baseline.config
python3 kernel/kernel_tools.py check-config --work work/kernel
python3 -m unittest discover -s tests -p 'test_kernel*.py' -v
```

`extract` authenticates before extraction, then compares all source files to the
signed tar. `verify-tree` repeats that comparison and rejects extra source files.
The kernel source is never patched. All generated build products go to `O=build`.
The recipe uses upstream `bindeb-pkg`, fixed release timestamp/build identity,
`-j${DESOS_BUILD_JOBS:-2}` bounded to four, and `INSTALL_MOD_STRIP=1`. Source, full baseline, final config, toolchain
versions and package hashes make the build inspectable and repeatable. The generated module
signing key also makes independent fresh builds differ byte for byte. Bitwise
identity across different toolchain versions or fresh signing keys is not claimed; provenance captures
the actual guest tools. Debian changelog/buildinfo timestamps can also vary.

## Configuration choices and device support

The complete Ubuntu generic baseline retains broad device and desktop coverage.
The checked fragment promotes boot essentials, EFI stub/partition support,
ACPI, virtio, storage, USB, FAT/ext4, ISO9660, squashfs xz/zstd, overlay, networking,
namespaces/cgroups and AppArmor/security support. Legacy BIOS boot uses GRUB;
UEFI uses GRUB with the EFI-capable kernel. These config checks do not substitute
for actual BIOS and UEFI boot tests.

Explicit device modules include i915 for Intel HD 620 (`8086:5916`), nouveau for
NVIDIA 940MX (`10de:134d`), iwlwifi/iwlmvm for Intel 3168 (`8086:24fb`), r8169 for
Realtek Ethernet, and HDA Intel/Realtek/HDMI audio. The image must supply matching
firmware packages, especially Intel Wi-Fi firmware. Nouveau is the preview path;
this recipe does not certify proprietary NVIDIA DKMS or all GPU power features.

Debug information/BTF, Rust-in-kernel, WERROR and embedded kernel headers are
disabled to fit the preview build budget. Desktop Rust applications are separate.
Ubuntu certificate filenames are removed through `.config` string settings;
there are no source changes. Modules are signed with the ordinary build-generated
`O=build/certs/signing_key.pem` and embedded public certificate. Signature
enforcement is not forced (`MODULE_SIG_FORCE=n`). The generated private key stays
only in the ignored worker build directory; every actual `.deb` payload is
checked for private-key filenames and PEM material before provenance is emitted.
Never ship or copy the worker build directory. Secure Boot is not certified. Packaging/integration must generate an initramfs
with needed modules and firmware, retain the Ubuntu recovery kernel, and test
boot and installation in disposable VMs.

The Ubuntu 7.0 baseline has four inherited symbol type warnings when processed
by 6.18: `ANDROID_BINDER_IPC`, `ANDROID_BINDERFS`,
`BOOTPARAM_SOFTLOCKUP_PANIC` and `BOOTPARAM_HUNG_TASK_PANIC`. Upstream
`olddefconfig` normalizes these. Android Binder IPC is disabled in this preview;
Android-container compatibility is deferred. Ordinary Docker/namespaces/cgroups
and DesConnect do not require Binder. The final required-config guard passes.

## Output contract

All deliverable kernel artifacts are in **`WORK/packages/`**:

- `linux-image-6.18.62-desos_6.18.62-1desos1_amd64.deb`
- `linux-headers-6.18.62-desos_6.18.62-1desos1_amd64.deb`
- Any additional upstream-produced `.deb`, e.g. `linux-libc-dev` (not needed to
  install the boot kernel; do not replace the base libc headers unnecessarily).
- `kernel-provenance.json`, final `config-6.18.62-desos`, `baseline.config`,
  `source-pin.json`, signature status/log and `SHA256SUMS`.

The provenance contains public hashes, tool versions, fixed build identity and
upstream fingerprint only. It does not recursively enumerate the worker or copy
signing material. The image builder should authenticate its local repository
metadata separately and consume the image/headers packages by exact version.
The script does not run `dpkg -i`, initramfs generation or GRUB configuration.
