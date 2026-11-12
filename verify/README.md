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
fixture systems and against the real 0.1 repository, key and kernel evidence in
`output/`; unit behaviour under systemd on the target is unverified.

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
| `--quick` | Skip `repository.pool` and `packages.files`; use weaker kernel comparisons (see table). |
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
| `repository.index` | yes | manifest `artifacts` hashes, `Release` → `Packages` hash, `Packages` entries ↔ manifest packages, trusted keyring = repository copy | any disagreement |
| `repository.pool` | no | SHA-256 of every `.deb` in the shipped pool against the signed manifest | a package is missing or differs |
| `packages.installed` | yes | dpkg's status database: each manifest package is `install ok installed` at the manifest version | `des-ui` or `desos-release` removed, other version, broken state |
| `packages.files` | no | every file of every manifest package, re-hashed against the payload of its signature-checked `.deb` | content, symlink target or mode bits differ, or a file is missing |
| `kernel.provenance` | yes | `kernel-provenance.json` names the kernel image package by the same SHA-256 as the signed manifest; `source-pin.json`, `signature-status.txt` and `desktop-pin.json` agree with it | the unsigned evidence contradicts the signed manifest |
| `kernel.release` | yes | running release (`/proc/sys/kernel/osrelease`) against the provenance record | running a kernel that is neither the DesOS one nor an installed Ubuntu kernel package |
| `kernel.image` | weak | `/boot/vmlinuz-<release>` (and `/cdrom/casper/vmlinuz-<release>` in a live session). Full: SHA-256 against the file inside the signed kernel package. Quick: MD5 against dpkg's own record | the file differs |
| `kernel.config` | weak | `/boot/config-<release>`, the shipped evidence copy and, if the kernel exposes it, `/proc/config.gz`, against `config_sha256` in the provenance. Full also checks that value against the signed kernel package | any differs |
| `identity.os-release` | yes | `ID=desos`, and the file is byte-identical to the one in the signed `desos-release` package (that package is tiny, so this is done in quick mode too) | another identity or an edited file |
| `identity.apt-sources` | yes | exactly one enabled source for the shipped repository, defined as shipped; nothing else uses the DesOS key; no source disables signature checking | see below |
| `state.secure-boot` | yes | firmware `SecureBoot` variable | never; reports `enabled` / `disabled` / `unsupported` |
| `state.live-session` | yes | `boot=casper` on the kernel command line | never; reports `live` / `installed` |
| `state.root-filesystem` | yes | filesystem type of `/` (btrfs or ext4 when installed, overlay when live) | never; another filesystem is *unknown* because only those are validated |

Conventions that consumers should know:

- **Nothing unsigned is trusted.** If the manifest signature does not verify,
  every check that needs the manifest is `unknown`, never `pass`.
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

The quick check cannot afford this, so it does not look at package files at
all, and it compares the kernel image only with dpkg's MD5 record. A quick pass
of `kernel.image` or `kernel.config` therefore says much less than a full one;
`details.method` records which was used (`dpkg-md5`, `provenance-sha256` or
`signed-sha256`).

## JSON result (schema `desos-verify-v1`)

Stable contract: fields listed here keep their name, type and meaning within
`desos-verify-v1`. New fields and new check ids may be added; consumers must
ignore what they do not know. `details` is diagnostic and may change, except
for the keys named below.

```json
{
  "schema": "desos-verify-v1",
  "tool_version": 1,
  "mode": "quick",
  "overall": "pass",
  "summary": "DesOS consistent with its signed evidence (14 of 14 quick checks passed)",
  "counts": {"pass": 14, "fail": 0, "unknown": 0},
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
                "counts": {"pass": 16, "fail": 0, "unknown": 0},
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
| `checks[].details.method` | string | On `kernel.image` and `kernel.config`: `signed-sha256`, `provenance-sha256` or `dpkg-md5`. |
| `checks[].details.value` | string | On the three `state.*` checks: the same value as in `state`. |
| `skipped` | array of string | Check ids not run in this mode. They do not appear in `checks`. |
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
to be present.

## What a pass does and does not prove

The chain is: shipped key → signature on `manifest.json` → SHA-256 of each
`.deb` → SHA-256 of each installed file, kernel image and kernel config.

**It proves**, when the full check passes, that the installed DesOS packages,
kernel image file, kernel configuration file and release identity are
byte-identical to what the holder of that key signed, and that the unsigned
build evidence in `/usr/share/doc/desos/build` does not contradict it.

**It does not prove any of the following.**

1. *That the key is the DesOS key.* The key is read from the system being
   checked. Someone with root can install their own key, re-sign a modified
   manifest and pass every check; `tests/test_verify.py` demonstrates exactly
   this. The fingerprint is printed so a person can compare it with one
   obtained elsewhere, and `--expect-fingerprint` makes that comparison
   mechanical, but a tool on the same disk can be replaced too.
2. *That this tool is the real tool.* `desos-verify`, its units and Python
   itself are files on the same disk. The verifier is currently copied onto
   the image outside any signed package, so it is not even covered by its own
   check.
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
7. *Much, in quick mode.* See above: no package file hashes, and the kernel
   image against an MD5 that the same attacker can rewrite.

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
two throwaway keys in temporary GnuPG homes, signs a repository with the real
`image/component_repo.py`, installs it into a temporary root and runs the tool
against it. Every path is resolved beneath an injectable root
(`Config(root=…)`), absolute symlinks included, and commands go through an
injectable `Runner`, so no test needs root or reads the user's keyring. The
tests need `gpg`, `gpgv`, `dpkg-deb` and `apt-ftparchive` or
`dpkg-scanpackages`; without them the fixture tests are skipped.
