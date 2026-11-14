# verify — an OS that proves what it is

`desos-verify` compares the running system with the signed evidence every DesOS
image already carries, and says for each comparison **pass**, **fail** or
**unknown** with a one-line reason.

Read [What a pass does and does not prove](#what-a-pass-does-and-does-not-prove)
before relying on it. In short: a pass means the system is consistent with what
the shipped archive key signed. It detects accidental damage, partial upgrades,
local edits and unsophisticated tampering. It does **not** defeat an attacker
with root, who can replace this tool or the key it trusts.

Nothing here has run on a booted DesOS image yet. The logic is tested against
fixture systems; unit behaviour under systemd on the target is unverified. The
signed per-file lists, the signed build evidence and the `desos-verify`
package described below exist from the 0.2 build recipe on and have never been
built into an image. A 0.1 image has none of them: there the two checks that
need them report **unknown** (never fail) and the kernel checks fall back to
the weaker comparisons.

## Using it

```sh
sudo desos-verify              # full check, human summary; exit 1 if anything failed
sudo desos-verify --quick      # cheap checks only (what runs at every boot)
desos-verify --json            # same result as JSON, schema desos-verify-v1
cat /run/desos/verify-status   # the one line the greeter/desktop shows
```

Run it as root: `/boot/vmlinuz-*` is readable only by root, and without it
`kernel.image` is reported as unknown.

| Option | Effect |
| --- | --- |
| `--quick` | Skip `repository.pool` and `packages.files`; compare the kernel and the programs with the signed per-file lists instead of the packages (see table). |
| `--json` | Print the result object instead of the summary. |
| `--write` | Also store the result (see [Files](#files-written)). Needs root. |
| `--quiet` | Print nothing. |
| `--strict` | Exit 3 when nothing failed but something is unknown. |
| `--root DIR` | Check a system mounted at `DIR` from other media. Running-kernel and firmware checks become unknown. |
| `--key FILE` | Trust this armored key instead of the one inside the system. |
| `--expect-fingerprint FPR` | Fail unless the archive key has this fingerprint. |

Exit status: `0` nothing failed (unknowns allowed), `1` at least one check
failed, `2` usage error or the result could not be stored, `3` unknowns under
`--strict`.

The 0.1 Preview archive key fingerprint, as recorded in
`docs/validation/2026-11-10-build.md`, is
`CDBC1835BA4F06D5F0BD4A9000B6AE1830FF9F59`. That key is a local development
key, not a production one.

## Checks

| Id | Quick | What is compared | Fails when |
| --- | --- | --- | --- |
| `signatures.key` | yes | `/usr/share/keyrings/desos-archive-keyring.asc` holds exactly one primary key; its fingerprint is computed and reported | missing, unparsable, more than one key, or not the `--expect-fingerprint` value |
| `signatures.manifest` | yes | `manifest.json.asc` over `manifest.json`, with `gpgv` and a temporary keyring containing only that key | bad signature, another signer, malformed manifest, `.incomplete` marker |
| `signatures.release` | yes | `Release.gpg` over `Release`; `InRelease`; the text signed inside `InRelease` equals `Release` | any of those do not hold |
| `signatures.evidence` | yes | `SHA256SUMS.asc` over `SHA256SUMS` in `/usr/share/doc/desos/build`; that `SHA256SUMS` is the one the signed manifest names; every file in the directory is listed and matches | bad signature, another signer, a `SHA256SUMS` from another build, an edited, missing or unlisted file, or the manifest naming evidence that is absent. *Unknown* on an image whose manifest names none and that ships no `SHA256SUMS` (0.1) |
| `repository.index` | yes | manifest `artifacts` hashes, `Release` → `Packages` hash, `Packages` entries ↔ manifest packages, trusted keyring = repository copy | any disagreement |
| `repository.pool` | no | SHA-256 of every `.deb` in the shipped pool against the signed manifest | a package is missing or differs |
| `packages.installed` | yes | dpkg's status database: each manifest package is `install ok installed` at the manifest version, or newer (`upgraded`) | `des-ui` or `desos-release` removed, an older version, broken state |
| `packages.key-files` | quick only | programs, units and DesOS helpers (`/usr/bin/*`, `/usr/sbin/*`, `/usr/lib/systemd/system/*`, `/usr/lib/desos/*`) of every installed manifest package, SHA-256 against the package's signed per-file list (an upgraded package: MD5 against dpkg's record). No `.deb` is unpacked | a listed file is missing, not a regular file, or differs; a list is missing or is not the one the manifest names. *Unknown* when the manifest carries no lists (0.1), or when files were left unhashed by the 256 MiB limit |
| `packages.files` | no | every file of every manifest package, re-hashed against the payload of its signature-checked `.deb`; the signed per-file list, where present, must describe that payload exactly (an upgraded package: MD5 against dpkg's record) | content, symlink target or mode bits differ, a file is missing, or list and package disagree |
| `kernel.provenance` | yes | `kernel-provenance.json` names the kernel image package by the same SHA-256 as the signed manifest; `source-pin.json`, `signature-status.txt` and `desktop-pin.json` agree with it | the evidence contradicts the signed manifest |
| `kernel.release` | yes | running release (`/proc/sys/kernel/osrelease`) against the provenance record | running a kernel that is neither the DesOS one nor an installed Ubuntu kernel package |
| `kernel.image` | yes | `/boot/vmlinuz-<release>` (and `/cdrom/casper/vmlinuz-<release>` in a live session). Full: SHA-256 against the file inside the signed kernel package. Quick: SHA-256 against the signed per-file list of that package; on an image without lists, MD5 against dpkg's own record (weak) | the file differs |
| `kernel.config` | yes | `/boot/config-<release>`, the shipped evidence copy and, if the kernel exposes it, `/proc/config.gz`, against `config_sha256` in the provenance. That value is also checked against the signed kernel package (full) or its signed per-file list (quick); without lists the quick check stops at the provenance | any differs |
| `identity.os-release` | yes | `ID=desos`, and the file is byte-identical to the one in the signed `desos-release` package (quick: by its signed per-file list; without lists by unpacking that tiny package) | another identity or an edited file |
| `identity.apt-sources` | yes | exactly one enabled source for the shipped repository, defined as shipped; nothing else uses the DesOS key except the update channel in `desos-updates.sources`; no source disables signature checking. The sources DesOS configures pass when they use their own keyrings: Brave (`brave-browser.sources`), Visual Studio Code (`vscode.sources`) and DEs-UI developer channels (`desos-dev.sources`, `desos-dev-published.sources`); any other source is `unknown` | see below |
| `state.secure-boot` | yes | firmware `SecureBoot` variable | never; reports `enabled` / `disabled` / `unsupported` |
| `state.live-session` | yes | `boot=casper` on the kernel command line | never; reports `live` / `installed` |
| `state.root-filesystem` | yes | filesystem type of `/` (btrfs or ext4 when installed, overlay when live) | never; another filesystem is *unknown* because only those are validated |

Conventions that consumers should know:

- **Nothing unsigned is trusted.** If the manifest signature does not verify,
  every check that needs the manifest is `unknown`, never `pass`.
- **Missing newer evidence is `unknown`, not `fail`.** An image built before
  the per-file lists and the signed `SHA256SUMS` existed is not accused of
  anything: `signatures.evidence` and `packages.key-files` are `unknown`, so
  `overall` is `unknown` and the exit status stays 0. The reverse does not
  hold: once the signed manifest names build evidence or a list, removing it
  is a `fail`, so the evidence cannot be stripped to get back to `unknown`.
- **A newer DesOS kernel from the update channel is `unknown`.** A running
  `*-desos` kernel that is installed as a package but is not the one in this
  image's manifest gives `kernel.release` `unknown` with `state.kernel`
  `other`.
- **A DesOS package upgraded after install passes, as `upgraded`.** When the
  installed version is newer than the one in the signed manifest (Debian
  version order, as `dpkg --compare-versions`), `packages.installed` passes and
  lists it in `details.upgraded` (`package`, `shipped`, `installed`). Its files
  can no longer be compared with the signed per-file lists, which describe the
  shipped version, so `packages.key-files` (programs and units) and
  `packages.files` (every file) compare them with dpkg's own MD5 record of the
  version now installed (`details.upgraded`, `upgraded_to`). This still
  catches damage and partial upgrades, but it is weaker than the signed check:
  the record lives on the system being checked, so it says nothing about a
  root-level attacker who installs a higher version, which no self-check can
  rule out anyway (see *What a pass does and does not prove*). Trust in an upgrade rests on apt, which
  installed it only from a signed source; `identity.apt-sources` reports those.
  An upgraded `desos-release` is checked the same way: `/etc/os-release` must be
  the file dpkg recorded for the installed version. An **older** version than
  the manifest's still fails, as does a removed `des-ui` or `desos-release`.
- **The Ubuntu compatibility kernel is not a failure.** `kernel.release`,
  `kernel.image` and `kernel.config` become `unknown` with a reason saying the
  Ubuntu kernel is running and DesOS evidence does not cover it;
  `state.kernel` is `ubuntu-compatibility`; overall is `unknown`; exit status
  stays 0. Its image is still compared with dpkg's MD5 record and fails if that
  differs.
- **Additional apt sources are `unknown`, not `fail`.** A PPA or mirror can
  publish a package called `des-ui`, so the claim "DesOS packages come only
  from the signed repository" no longer holds, but adding one is a normal
  thing to do. `fail` is reserved for: the DesOS source missing, duplicated or
  changed; the DesOS key trusted for another location; any source with
  `Trusted`/`Allow-Insecure`. Expected others are `archive.ubuntu.com`,
  `security.ubuntu.com`, `ports.ubuntu.com` and `*.archive.ubuntu.com` signed
  by `ubuntu-archive-keyring.gpg`. apt pinning, `apt.conf` overrides and
  `/etc/apt/trusted.gpg.d` are not inspected.
- **Removing `des-ui` or `desos-release` is a failure** of `packages.installed`.
  Any other DesOS package may be uninstalled by its owner: the check still
  passes and lists it under `details.removed`.
- **Edited configuration files are not failures.** Files dpkg records as
  conffiles are listed under `conffiles_changed` and mentioned in the reason.
  Files absent because of a dpkg `path-exclude` rule are counted as `excluded`.

### How installed files are checked, and what it costs

dpkg's own record (`/var/lib/dpkg/info/*.md5sums`) is MD5, unsigned, and lives
on the disk being checked. The full check does not use it. Instead:

1. hash each `.deb` in `/usr/share/desos/repository/pool` and compare with the
   SHA-256 in the signed manifest;
2. stream that package's payload with `dpkg-deb --fsys-tarfile` (never written
   to disk) and SHA-256 every member;
3. SHA-256 the installed file at the same path (honouring dpkg diversions) and
   compare content, symlink targets and mode bits.

So each installed file is tied to the signature by SHA-256 alone. The cost is
decompressing and hashing everything twice. For the 0.1 packages that is about
144 MB of `.deb` and 660 MB of payload. Measured on the build laptop with a warm
cache: hashing the pool 0.7 s, streaming the kernel image package 4.0 s and
`des-ui` 0.6 s. Re-hashing the installed copies was **not** measured (there is
no installed system here); expect a similar amount again, so roughly 10–20 s on
an SSD and noticeably longer from a live USB stick. The service runs it at idle
CPU and I/O priority. Ownership (uid/gid) is not compared. Files that belong to
no package are not noticed.

The quick check cannot afford this. It uses the **signed per-file lists**
instead: `image/component_repo.py` writes, for every package,
`file-lists/NAME_VERSION_ARCH.sha256` (plain `sha256sum` format, paths relative
to `/`) and puts each list's SHA-256 into the signed manifest. The quick check
verifies the list against the manifest and then hashes only the kernel image,
the kernel configuration, `os-release` and the programs and units named under
`packages.key-files`; it runs no `dpkg-deb`. For the 0.1 packages those key
files are about 85 MB, which has **not** been timed on a target; hashing stops
at 256 MiB and says so. Mode bits, symlinks, libraries, kernel modules and
everything else stay with the full check.

`details.method` records the comparison (`signed-sha256`, `provenance-sha256`
or `dpkg-md5`) and `details.evidence` where the expected value came from
(`package`, `file-list`, `signed-provenance`, `unsigned-provenance`). On an
image without lists the quick `kernel.image` is still only dpkg's MD5 and
says much less than a full one.

### Where each signature is made

The private key exists only in the dedicated GnuPG home on the host. The image
worker never holds it and signs nothing.

| Evidence | Signed | How it reaches the image |
| --- | --- | --- |
| `manifest.json`, `Release`, `InRelease` | host, `component_repo.py` | repository copied in by `finalize-rootfs.sh` |
| per-file lists | host, same run: hashed into the signed manifest | inside the repository (`file-lists/`) |
| build evidence `SHA256SUMS` | host, same run (`--evidence` files, all of which exist before the image is built): detached `SHA256SUMS.asc`, and its hash in the signed manifest | `finalize-rootfs.sh` copies `build-evidence/` from the repository to `/usr/share/doc/desos/build` and refuses a repository without it |
| `desos-verify` itself | host: `image/verify_package.py` builds the package, which is one more entry of the signed repository | installed as a package; `image/components.py` no longer copies `verify/rootfs` |

Evidence that only exists after the worker has run (the rootfs package
manifest, the seal receipt, the ISO hash) is therefore **not** signed and not
checked by this tool; it belongs with the release artifacts. Before sealing,
`finalize-rootfs.sh` runs the shipped tool against the tree (offline, quick)
and stops on any failed check or unverified signature.

## JSON result (schema `desos-verify-v1`)

Stable contract: fields listed here keep their name, type and meaning within
`desos-verify-v1`. New fields and new check ids may be added; consumers must
ignore what they do not know. `details` is diagnostic and may change, except
for the keys named below.

```json
{
  "schema": "desos-verify-v1",
  "tool_version": 2,
  "mode": "quick",
  "overall": "pass",
  "summary": "DesOS consistent with its signed evidence (16 of 16 quick checks passed)",
  "counts": {"pass": 16, "fail": 0, "unknown": 0},
  "started": "2026-11-12T09:00:00Z",
  "finished": "2026-11-12T09:00:01Z",
  "duration_seconds": 0.412,
  "root": "/",
  "offline": false,
  "boot_id": "11111111-2222-3333-4444-555555555555",
  "key_fingerprint": "CDBC1835BA4F06D5F0BD4A9000B6AE1830FF9F59",
  "key_pinned": false,
  "manifest_sha256": "…",
  "state": {
    "kernel": "desos",
    "kernel_release": "6.18.62-desos",
    "secure_boot": "disabled",
    "root_filesystem": "ext4",
    "session": "installed"
  },
  "checks": [
    {"id": "signatures.manifest", "group": "signatures", "status": "pass",
     "reason": "manifest.json is signed by the shipped key and lists 7 packages",
     "details": {}}
  ],
  "skipped": ["repository.pool", "packages.files"],
  "last_full": {"overall": "pass", "finished": "2026-11-11T21:14:03Z",
                "counts": {"pass": 17, "fail": 0, "unknown": 0},
                "failed": [], "same_manifest": true},
  "assurance": "self-reported by software on the system being checked; …"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | Always `desos-verify-v1` for this contract. |
| `mode` | `quick` \| `full` | Which set of checks ran. |
| `overall` | `pass` \| `fail` \| `unknown` | `fail` if any check failed; else `unknown` if any is unknown; else `pass`. |
| `summary` | string | One printable line, at most 240 characters; identical to `/run/desos/verify-status`. |
| `counts` | object | Number of checks per status. |
| `started`, `finished` | string | UTC, `YYYY-MM-DDTHH:MM:SSZ`. |
| `boot_id` | string \| null | Kernel boot id, to tell whether a stored result is from this boot. |
| `offline` | bool | True when `--root` pointed at a mounted system. |
| `key_fingerprint` | string \| null | Primary fingerprint of the key everything was verified against. |
| `key_pinned` | bool | True only when `--expect-fingerprint` supplied it from outside. |
| `manifest_sha256` | string \| null | SHA-256 of the verified manifest; null if it did not verify. |
| `state.kernel` | `desos` \| `ubuntu-compatibility` \| `other` \| `unknown` | Which kernel is running. |
| `state.kernel_release` | string \| null | Running release. |
| `state.secure_boot` | `enabled` \| `disabled` \| `unsupported` \| `unknown` | `unsupported` = legacy BIOS boot. |
| `state.root_filesystem` | string \| null | Filesystem type of `/`. |
| `state.session` | `live` \| `installed` \| `unknown` | |
| `checks[]` | array | In a fixed order. Each has `id`, `group` (the part of `id` before the dot), `status`, `reason` (one printable line, at most 300 characters) and `details`. |
| `tool_version` | integer | 2 since `signatures.evidence` and `packages.key-files` exist (1 before). The schema is unchanged: both are new check ids, which consumers were already told to expect. |
| `checks[].details.method` | string | On `kernel.image` and `kernel.config`: `signed-sha256`, `provenance-sha256` or `dpkg-md5`. |
| `checks[].details.evidence` | string | On passing `kernel.image` and `kernel.config`: `package`, `file-list`, `signed-provenance` or `unsigned-provenance`. Absent when dpkg's MD5 was used. |
| `checks[].details.evidence_signed` | bool | On `kernel.provenance`: whether `signatures.evidence` passed in this run. |
| `checks[].details.value` | string | On the three `state.*` checks: the same value as in `state`. |
| `skipped` | array of string | Check ids not run in this mode. They do not appear in `checks`. Quick: `repository.pool`, `packages.files`. Full: `packages.key-files` (covered by `packages.files`). |
| `last_full` | object \| null | Quick results only: outcome of the stored last full check. `same_manifest` is false if it was made against a different manifest. |
| `assurance` | string | Fixed reminder of the limits below. |

Text in `reason` and `summary` can contain names read from the system; control
characters are replaced with `?` and newlines with spaces, so both are safe to
print on a terminal or draw in the greeter. The result contains package names,
versions, paths and hashes, and no secrets or machine identifiers other than
`boot_id`.

## Files written

Only with `--write` (the services use it). Each is written to a temporary file
in the same directory, fsynced, set to mode 0644 and renamed over the old one,
so a reader never sees a partial file.

| Path | Content |
| --- | --- |
| `/run/desos/verify.json` | Latest result of this boot, quick or full. |
| `/run/desos/verify-status` | `summary` plus a newline. For the greeter and desktop to display as is. |
| `/var/lib/desos/verify-full.json` | Last full result; survives reboots (not in a live session, where `/var` is temporary). |

A consumer that wants a colour or icon should read `overall` from the JSON
rather than parse the status line. If `/run/desos/verify-status` does not
exist, the check has not run yet this boot.

## Units

| Unit | When | What |
| --- | --- | --- |
| `desos-verify-quick.service` | every boot, `WantedBy=multi-user.target`, ordered before `greetd.service` | `desos-verify --quick --write --quiet` |
| `desos-verify-full.timer` | 10 minutes after boot, then every 24 hours of uptime, with up to 5 minutes random delay | starts the full service |
| `desos-verify-full.service` | from the timer | `desos-verify --write --quiet` at `Nice=19`, idle CPU and I/O scheduling |

`usr/lib/tmpfiles.d/desos-verify.conf` creates `/run/desos` and
`/var/lib/desos` (0755 root). Both services run as root with
`ProtectSystem=strict`, write access to those two directories only, no network
and a private `/tmp` for the temporary keyring. A failed check makes the unit
fail (exit 1), so `systemctl --failed` shows it; ordering before greetd does
not make greetd depend on the result.

`component.json` asks for `desos-verify-quick.service` and
`desos-verify-full.timer` to be enabled and for `python3`, `gpgv` and `dpkg`
to be present. Its `package` key names `desos-verify`: the tool, units and
tmpfiles rule under `rootfs/` are the source of that package
(`image/verify_package.py`, no maintainer scripts) and reach the image only
inside it. Any change under `rootfs/` needs a new `VERSION` there, or the
repository builder's ledger refuses the rebuilt package. An upgrade of the
package does not enable or restart the units by itself.

## What a pass does and does not prove

The chain is: shipped key → signature on `manifest.json` → SHA-256 of each
`.deb` and of each per-file list → SHA-256 of each installed file, kernel image
and kernel config; and shipped key → signature on `SHA256SUMS` (also named in
the manifest) → SHA-256 of each build evidence file.

**It proves**, when the full check passes, that the installed DesOS packages
(now including `desos-verify`), kernel image file, kernel configuration file
and release identity are byte-identical to what the holder of that key signed,
and that the build evidence in `/usr/share/doc/desos/build` is the evidence
signed with that repository and does not contradict it. On a 0.1 image the
evidence is unsigned and only shown not to contradict the manifest.

**It does not prove any of the following.**

1. *That the key is the DesOS key.* The key is read from the system being
   checked. Someone with root can install their own key, re-sign a modified
   manifest and pass every check; `tests/test_verify.py` demonstrates exactly
   this. The fingerprint is printed so a person can compare it with one
   obtained elsewhere, and `--expect-fingerprint` makes that comparison
   mechanical, but a tool on the same disk can be replaced too.
2. *That this tool is the real tool.* `desos-verify`, its units and Python
   itself are files on the same disk. Since 0.2 the verifier is a signed
   package, so its files are in the manifest and are re-hashed by
   `packages.key-files` and `packages.files`; that catches damage and careless
   edits. It does not close this point: a replaced verifier can report
   anything about itself, and Python and `gpgv` come from Ubuntu and are not
   checked here.
3. *What is running in memory.* The kernel image check hashes the file the
   boot loader would be given, not the code that booted. Loaded modules, a
   kexec'd kernel and running processes are not examined. The kernel has
   `CONFIG_IKCONFIG` off, so the running kernel's built-in configuration
   cannot be read either.
4. *Anything about the initramfs, GRUB, the EFI partition or firmware.* The
   initramfs is generated on the machine and has no signed reference. Secure
   Boot is off in this preview, so nothing checked any of them before this
   tool started.
5. *Anything about the Ubuntu base.* Roughly all of userspace except seven
   packages comes from Ubuntu and is not examined here (`debsums` would give
   an MD5-level answer). Files owned by no package, user accounts, systemd
   units added by hand, cron jobs and home directories are not examined.
6. *Anything between runs.* The quick check runs once per boot and the full
   check daily; the stored result describes the moment it was taken.
7. *Everything, in quick mode.* With per-file lists the quick check ties the
   kernel image, kernel configuration, release identity and the DesOS programs
   and units to the signature by SHA-256, but not libraries, kernel modules,
   data files, mode bits or symlinks. Without lists (0.1 image) it hashes no
   package files and compares the kernel image with an MD5 that the same
   attacker can rewrite.
8. *That the builder was honest.* The signed lists and the signed
   `SHA256SUMS` say what the key holder's tools wrote down on the build host.
   They add no protection against whoever holds the key, and none of the
   evidence produced later inside the image worker is signed.

The result files are root-writable, so a displayed "verified" line is only as
trustworthy as root on that machine.

### Getting a stronger answer today

Boot other trusted media (for example the DesOS ISO whose SHA-256 you checked
on another machine), mount the installed system read-only, and run that
media's copy of the tool against it with the fingerprint supplied by you:

```sh
sudo mount -o ro /dev/sdXN /mnt
sudo desos-verify --root /mnt --expect-fingerprint CDBC1835BA4F06D5F0BD4A9000B6AE1830FF9F59
```

This removes points 1–3 for the files on disk as far as you trust the media,
the firmware and the machine. Points 4 and 5 remain.

### What would close the gap

- **Verified boot**: Secure Boot with a DesOS-controlled key, a signed kernel,
  and a unified kernel image so the initramfs and command line are covered by
  the signature. The kernel already has module signing and lockdown support
  built in; lockdown is not forced.
- **Measured boot**: TPM PCR measurements of firmware, boot loader, kernel and
  initramfs, with this tool's result (or its inputs) extended into a PCR and
  checked by remote attestation or by sealing the disk key to the expected
  values. The kernel has TPM and IMA support compiled in.
- **A read-only verified system partition**: dm-verity (or IMA appraisal with
  signed hashes) over `/usr`, with the root hash in the signed kernel image.
  That replaces periodic re-hashing with enforcement on every read and covers
  the Ubuntu base as well.
- **A real key**: an offline or hardware-held release key with a published
  fingerprint and a rotation and revocation story, in place of the development
  key.

Until at least verified boot exists, treat this tool as an integrity and
consistency checker, not as a security boundary.

## Testing

`python3 -m unittest tests.test_verify` builds three small packages, generates
two throwaway keys in temporary GnuPG homes, signs a repository (with per-file
lists and build evidence) with the real `image/component_repo.py`, installs it
into a temporary root and runs the tool against it. The same repository with
the lists and the evidence signature stripped and the manifest signed again
stands in for a 0.1 image. `tests.test_desos_packages` builds the
`desos-verify` package and checks it holds exactly `verify/rootfs`. Every path is resolved beneath an injectable root
(`Config(root=…)`), absolute symlinks included, and commands go through an
injectable `Runner`, so no test needs root or reads the user's keyring. The
tests need `gpg`, `gpgv`, `dpkg-deb` and `apt-ftparchive` or
`dpkg-scanpackages`; without them the fixture tests are skipped.
