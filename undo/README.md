# DesOS undo

Every system change on an installed DesOS can be taken back, and a system that
stops starting goes back to its last working state by itself.

- The installer puts the system on btrfs, in subvolumes.
- `desos-undo` takes read-only snapshots of the system subvolume: before and
  after every apt/dpkg transaction, around any command run through
  `desos-undo run`, on request, and whenever a start reaches the sign-in screen.
- `desos-undo rollback <id>` makes a snapshot the system for the next start.
  The state it replaces is kept as a snapshot, so a rollback can be undone.
- GRUB counts starts that never reached the sign-in screen. After three in a
  row it starts the last known-good snapshot, which makes itself permanent,
  restarts, and tells the user what happened.
- Home directories are never part of a snapshot or a rollback.

**Status: nothing here has been booted.** The logic is covered by host-safe
unit tests (fake btrfs, real GRUB syntax checker, a GRUB-script interpreter for
the counter logic). No image has been built with this component and no btrfs
filesystem, GRUB or systemd has run it. See [Unverified](#unverified).

## On-disk layout

Created by Calamares from `installer/calamares/modules/mount.conf`
(`btrfsSubvolumes`) when the root filesystem is btrfs, which
`partition.conf` makes the default (`defaultFileSystemType: btrfs`). ext4
remains selectable in the Erase-disk drop-down and in manual partitioning.

| Subvolume    | Mounted at    | Rolled back? | Why |
|--------------|---------------|--------------|-----|
| `@`          | `/`           | yes          | The system: `/usr`, `/etc`, `/boot`, `/var/lib` (dpkg database). |
| `@home`      | `/home`       | never        | Personal files. |
| `@root`      | `/root`       | never        | The administrator's personal files. |
| `@snapshots` | `/.snapshots` | never        | Snapshots and undo state must survive every rollback. |
| `@log`       | `/var/log`    | never        | The log of a failed start must survive the recovery from it. |
| `@cache`     | `/var/cache`  | never        | Bulky, reproducible; would bloat every snapshot. |
| `@tmp`       | `/var/tmp`    | never        | Same. |

`/boot` is deliberately inside `@`: each snapshot carries the kernel, initrd
and modules that belong together, and GRUB can start a snapshot's own kernel.
There is no separate `/boot` partition in the automatic layout. The EFI system
partition (`/boot/efi`, FAT) is outside all of this.

Mount options for btrfs: `noatime` (reads must not rewrite metadata shared
with snapshots). No compression: GRUB 2.14 could not read the zstd-compressed
kernel in the v4 image test ("premature end of file"), and `/boot` lives in
`@` and in every snapshot, so GRUB must be able to read it.

Inside `@snapshots`:

```
/.snapshots/<id>/snapshot      read-only btrfs snapshot of @
/.snapshots/<id>/info.json     metadata (schema below), mode 0644
/.snapshots/.desos-undo/
    next-id                    ids are never reused
    lock                       flock for all mutating commands
    pending.json               open transaction (pre snapshot taken, post not yet)
    good.json                  id of the last known-good snapshot
    rollback.json              rollback made, restart not yet confirmed good
    notices.json               messages for the user (auto-recovery, rollback)
    grub/snapshots.cfg         GRUB menu of snapshots, sourced by grub.cfg
    grub/env                   boot counter, only when GRUB cannot write (BIOS)
/boot/efi/desos-undo/env       boot counter on UEFI (GRUB environment block)
```

During a rollback the top level of the filesystem briefly also holds
`@.desos-undo-new` (staged) and afterwards `@.desos-undo-old-<id>` (the
replaced system, still running until the restart; deleted at the next good
start, its content is kept as snapshot `<id>`).

### Why not snapper

`desos-undo` drives `btrfs subvolume` directly.

- snapper's own rollback only works with the openSUSE layout: the system
  lives in `/.snapshots/N/snapshot` and is selected with `btrfs subvolume
  set-default`. Calamares writes `subvol=/@` into fstab and Ubuntu's
  `10_linux` adds `rootflags=subvol=@`, both of which pin the root subvolume by
  name and defeat set-default. On an `@` layout snapper can create and list
  snapshots but cannot roll back; that part would have to be written here
  anyway, against snapper's metadata format.
- The operations needed are four btrfs commands (`snapshot`, `snapshot -r`,
  `delete`, `filesystem sync`) plus two renames. Owning them keeps the tool
  testable without btrfs (every command goes through one injected runner) and
  removes a D-Bus daemon and an XML metadata format from the boot-recovery
  path, where the fewest moving parts matter most.
- Metadata is plain JSON the desktop and its AI companion can read without
  root; snapper's is root-only XML plus a D-Bus API.

The cost: no `snapper` CLI compatibility and no `snapper undochange`
(file-level partial undo). `desos-undo diff` covers inspection; undo is whole
system only.

## Commands

`desos-undo [--json] [--strict] COMMAND`. Mutating commands need root.
Reading (`list`, `status`, `diff`, `notices`) works for any user.

| Command | Effect |
|---------|--------|
| `list` | All snapshots, oldest first. |
| `snapshot --reason TEXT [--keep]` | Snapshot now. `--keep` exempts it from pruning. |
| `diff ID [--to ID2] [--path DIR]... [--full]` | Package changes (from the dpkg databases) and file changes between snapshot `ID` and the running system or `ID2`. Files are compared under `/etc` unless `--path`/`--full`; nested filesystems are not entered. |
| `rollback ID [--reason TEXT]` | Snapshot `ID` becomes the system at the next restart. Prints the id of the undo point. |
| `status` | Whether undo is active, boot counter, known-good snapshot, pending rollback, unread notices. |
| `prune [--dry-run]` | Apply the retention policy. Also runs daily and after each transaction. |
| `run --reason TEXT [--result-file F] -- CMD...` | Snapshot, run `CMD`, snapshot. Exit status is `CMD`'s. apt/dpkg hooks inside `CMD` defer to it, so there is one pair. If snapshots are unavailable `CMD` still runs. |
| `notices [--ack]` | Messages about automatic recovery and rollbacks. As root `--ack` clears them for everyone; as a user, for that user (`~/.local/state/desos-undo/acknowledged.json`). |
| `boot-good [--wait] [--now]` | Mark this start good. `--now` skips the greeter check (text-mode machines, administrators). |

Internal (units, hooks, grub-mkconfig): `apt-pre`, `apt-post`, `dpkg-pre`,
`dpkg-post`, `boot-start`, `grub-fragment`, `grub-refresh`.

Exit status: `0` success; `1` failed; `2` usage; `3` undo unavailable here and
`--strict` given. Without `--strict` an unavailable system exits `0` with a
clear message: the live session and ext4 installs are not errors. `run` exits
with the wrapped command's status. Package and boot hooks always exit `0`.

Configuration: `/etc/desos-undo.conf` (shipped with every default spelled
out). Changing `[boot] failure_threshold` needs `sudo update-grub`.

### Retention

Defaults: the newest 10 transactions (a pre/post pair counts once), 10 manual,
3 boot-good and 5 rollback snapshots. Never deleted automatically: the
recorded known-good snapshot, the newest boot-good, the pre snapshot of an
open transaction, both ends of a rollback awaiting its first good start, and
`--keep` snapshots. Below 10 % free space each prune additionally removes up
to three of the oldest unprotected snapshots (btrfs returns space
asynchronously, so it is done gradually). `max_age_days` is off by default.

## JSON output (stable, schema 1)

The machine-readable schema is shipped at
`/usr/share/desos-undo/schema-v1.json` (source:
`undo/rootfs/usr/share/desos-undo/schema-v1.json`, JSON Schema 2020-12). The
tests validate real output of every public command against it.

With `--json` (placed before the command name when using `run`) each command
prints exactly one object as the last line of standard output:

```json
{"schema": 1, "command": "snapshot", "ok": true, "supported": true,
 "code": null, "message": "Created snapshot 14.", "data": {"snapshot": {...}}}
```

- `ok`: the command did what was asked.
- `supported`: `false` in the live session (`code: "live-session"`), on a
  non-btrfs root (`"not-btrfs"`), on a btrfs layout that is not the one above
  (`"layout-unsupported"`) or without btrfs-progs (`"tools-missing"`). `status`
  then still succeeds (`ok: true`); other commands report `ok: false`.
- `code`: stable identifier when something was not done. Besides the above:
  `snapshot-boot` (running read-only from a snapshot), `restart-pending`,
  `not-root`, `no-such-snapshot`, `command-failed`, `not-ready`, `no-snapshot`,
  `usage`, `error`.
- `message`: plain language, safe to show to the user as is.
- `data`: per command; see the schema file. Snapshot objects are the content
  of `info.json`:

```json
{"schema": 1, "id": 14, "kind": "post", "reason": "after: apt upgraded nano",
 "created": "2026-11-12T10:03:22Z", "kernel": "6.18.62-desos",
 "boot": {"kernels": ["6.18.62-desos", "7.0.0-12-generic"]},
 "pre_id": 13, "post_id": null, "command": null, "exit_code": null,
 "packages": {"installed": [], "removed": [],
              "upgraded": [{"name": "nano", "from": "7.2-1", "to": "8.0-1"}],
              "downgraded": []},
 "planned": null, "protected": false}
```

`kind` is `pre`, `post`, `manual`, `boot-good` or `rollback`. `packages` on a
post snapshot (and copied to its pre snapshot) is the actual difference of the
dpkg database, not what was requested; `planned` on an apt pre snapshot is
what apt announced.

Compatibility rule: within schema 1 fields are only added. Consumers must
ignore unknown fields and unknown `code` values.

For the desktop and the AI companion:

- Before changing the system: `desos-undo --json run --reason "..." -- CMD`.
- At session start: `desos-undo --json notices`; show each `message`; then
  `desos-undo notices --ack`.
- "Undo that": `desos-undo --json list`, pick the `pre` snapshot of the
  transaction, `sudo desos-undo --json rollback ID`, then ask to restart.

## Automatic snapshots

`/etc/apt/apt.conf.d/80desos-undo`: `DPkg::Pre-Install-Pkgs` (protocol
version 3; the package list apt writes on standard input becomes `planned`
and the reason text) and `DPkg::Post-Invoke`.

`/etc/dpkg/dpkg.cfg.d/desos-undo`: `pre-invoke`/`post-invoke`, for dpkg used
without apt. While an apt transaction is open they do nothing. A dpkg run that
changed no package leaves no snapshots.

All four end in `|| true`, print nothing where undo is unavailable, and can
be switched off for one command with `DESOS_UNDO_DISABLE=1`.

## Self-healing boot

Pieces:

- `/etc/grub.d/42_desos-undo` adds one section to `grub.cfg`
  (`desos-undo grub-fragment`). It is static per installation: it only
  contains filesystem UUIDs and the threshold. Everything that changes
  (the snapshot menu, the recovery entry) is in
  `/.snapshots/.desos-undo/grub/snapshots.cfg`, which the section `source`s
  and which `desos-undo` rewrites itself. No `update-grub` runs when a
  snapshot is taken, and a rolled-back system (with its older `grub.cfg`)
  still shows the current snapshots.
- A dedicated GRUB environment block with three variables: `undo_tries`,
  `undo_good`, `undo_healed`.
- `desos-undo-boot-start.service` (early, before `sysinit.target`).
- `desos-undo-boot-good.service` (after `greetd.service`).

Where the counter lives, decided when `grub.cfg` is generated and recorded in
it as `# desos-undo: mode=... env=... threshold=N` (the tool reads that line,
so tool and boot loader cannot disagree):

- **UEFI (`grub-counted`)**: `/boot/efi/desos-undo/env`. GRUB cannot write to
  btrfs (Ubuntu's own `recordfail` is disabled on btrfs for that reason) but
  can write a preallocated file on FAT. GRUB increments `undo_tries` on every
  start.
- **BIOS (`userspace-counted`)**: there is no GRUB-writable filesystem.
  GRUB only reads `/.snapshots/.desos-undo/grub/env`; `boot-start` increments
  it. Starts that die before early userspace (kernel panic, unusable initrd)
  are therefore **not counted on BIOS**; the snapshot menu is the manual way
  out.

One start:

1. GRUB loads the three variables. `undo_tries < N`: count and start
   normally; `default` is untouched.
2. `boot-good --wait` polls until `greetd.service` is active/running and a
   logind session exists, waits `good_stable_seconds` (10) and checks that the
   greeter was not restarted meanwhile. Then: `undo_tries=0`, `undo_healed`
   cleared, and if anything was snapshotted since the last known-good
   snapshot (or there is none, or a rollback just happened) a new `boot-good`
   snapshot is taken and recorded in `undo_good` and `good.json`. On machines
   whose default target is not graphical or where greetd is not enabled, the
   criterion is `systemctl is-system-running` = running/degraded.
3. If the greeter never comes up, nothing resets the counter.

After N (default 3) such starts in a row:

4. GRUB sets `default=desos-undo-heal`, shows the menu for 10 s, and (UEFI)
   stores `undo_healed`. The entry starts the known-good snapshot **read-only,
   with its own kernel and initrd**, with `desos.undo.snapshot=<id>
   desos.undo.heal=1` on the kernel command line.
5. `boot-start` sees that, runs the same code as `desos-undo rollback <id>`,
   resets the counter, writes a notice and restarts.
6. The machine starts the recovered `@`. `boot-good` confirms it. The user
   sees the notice at the next login (`desos-undo notices`, the desktop, or
   `/etc/profile.d/desos-undo-notice.sh` on a terminal): what happened, which
   snapshot holds the state that failed, how to inspect it (`diff`) and how to
   go back to it (`rollback`).
7. If the recovered system also fails N times, GRUB does not recover again
   (`undo_healed` is still set): it selects the "DesOS snapshots" submenu and
   waits without a timeout.

Starts from the snapshot menu and rescue starts (`single`, `recovery`,
`systemd.unit=...`) are not counted as failures.

### Manual snapshot entries

The submenu "DesOS snapshots (start an earlier system state)" lists the newest
15 snapshots. Such a start is read-only: `status` and the apt hooks say so,
nothing is snapshotted, and `sudo desos-undo rollback <id>` followed by a
restart keeps it.

### Coexistence with the kernel default (`image/configure.py`)

`select-upstream-kernel` keeps `GRUB_DEFAULT=saved` pointing at the `-desos`
kernel through `grub-set-default`, i.e. `saved_entry` in
`/boot/grub/grubenv`. `desos-undo`:

- never reads or writes `/boot/grub/grubenv`, `saved_entry` or `next_entry`,
  never calls `grub-set-default`, `grub-reboot`, `grub-editenv` or
  `update-grub`; its variables live in its own file;
- only overrides the runtime variable `default`, and only in the two recovery
  situations above. In every normal start `default` is whatever `00_header`
  computed from `saved_entry`;
- is numbered 42, after `10_linux`, so the IDs the selector looks for
  (`gnulinux-advanced-<uuid>`, `gnulinux-<version>-advanced-<uuid>`) and their
  position are unchanged.

`/boot/grub/grubenv` is inside `@`, so a rollback also restores the
`saved_entry` that matches the kernels of that snapshot.

## Failure modes

| Situation | Behaviour |
|-----------|-----------|
| Live session, ext4 root, foreign btrfs layout, no btrfs-progs | Every command explains and exits 0 (3 with `--strict`); hooks are silent; `run` still runs the command; no GRUB section beyond a comment; units are skipped by their conditions. |
| Snapshot fails (disk full, btrfs error) inside an apt/dpkg hook | One line on stderr, the transaction continues. |
| Rollback fails before the swap | `@` is untouched; error reported; a stray `@.desos-undo-new` is removed by the next rollback. |
| Power loss during the swap | The swap is one `renameat2(RENAME_EXCHANGE)`; where the kernel refuses it, two renames in quick succession (same btrfs transaction in practice, not guaranteed). |
| Changes made after `rollback` but before the restart | Lost: they go to the retired system. The command says so. |
| Rollback across a GRUB package upgrade | The boot code in the MBR/ESP is not in any snapshot. Rollback copies the GRUB module directories (`i386-pc`, `x86_64-efi`) of the replaced system into the new one so they keep matching the installed core image. |
| Rollback across account changes | `/etc/passwd` and `/etc/shadow` go back; `/home` does not. A user created after the snapshot keeps a home directory but loses the account until the rollback is undone. |
| Automatic recovery itself fails | The machine stays up read-only on the snapshot, reports on the console/journal, does not restart, and the next start stops at the snapshot menu. |
| Greeter up but session broken after login | Counts as a good start. The criterion is "sign-in screen reachable", not "desktop usable". |
| Three deliberate power-offs before the greeter | Indistinguishable from failures: recovery starts. It is undoable and loses nothing in `/home`. |
| Separate `/boot` (manual partitioning) | Snapshots contain no kernel: no menu entries, no automatic recovery, and a rollback does not roll back kernels. Not recommended. |
| `/boot/efi` not mounted at start | The counter cannot be reset; after N starts recovery triggers although the system is fine. |
| Text-mode default target | Good = system reached running/degraded; or `desos-undo boot-good --now`. |
| Docker/VM images under `/var/lib` | They are in `@` and get snapshotted and rolled back. Put them on their own subvolume. |

## Files

```
undo/component.json
undo/rootfs/usr/sbin/desos-undo                       launcher
undo/rootfs/usr/lib/desos-undo/desos_undo.py          all logic
undo/rootfs/usr/share/desos-undo/schema-v1.json       JSON schema
undo/rootfs/etc/desos-undo.conf
undo/rootfs/etc/apt/apt.conf.d/80desos-undo
undo/rootfs/etc/dpkg/dpkg.cfg.d/desos-undo
undo/rootfs/etc/grub.d/42_desos-undo                  must be executable
undo/rootfs/etc/profile.d/desos-undo-notice.sh
undo/rootfs/usr/lib/systemd/system/desos-undo-boot-start.service
undo/rootfs/usr/lib/systemd/system/desos-undo-boot-good.service
undo/rootfs/usr/lib/systemd/system/desos-undo-prune.{service,timer}
```

Tests: `python3 -m unittest discover -s tests -p 'test_undo*.py'`.

## Unverified

Nothing below can be checked until an image is built and installed on btrfs.
Most likely to be wrong first:

1. **GRUB `save_env -f` to the EFI system partition** from Ubuntu's GRUB 2.14,
   and `load_env -f FILE var...`. The syntax passes `grub-script-check`; the
   behaviour is modelled on Fedora's boot counter but has not run here.
2. **Starting a read-only snapshot far enough for `boot-start` to run**
   (`rootflags=subvol=@snapshots/N/snapshot ro`, fstab still naming
   `subvol=/@` for `/`, `systemd-remount-fs` on a read-only subvolume).
3. **Calamares 3.3.14 `partitionLayout` with `filesystem: unknown`** following
   `defaultFileSystemType`/the drop-down. The option names and
   `PartitionLayout::setDefaultFsType` are present in the packaged plugin; the
   behaviour is from upstream documentation, not from a run.
4. **GRUB reading `/@snapshots/N/snapshot/boot/...` and `source`-ing a file
   from `@snapshots`** (uncompressed btrfs since the v4 image test).
5. **The greeter criterion**: that greetd's greeter shows up in
   `loginctl list-sessions` on DesOS. If not, set `[boot] require_session = no`.
6. Renaming the mounted root subvolume from the top-level mount, and
   `renameat2(RENAME_EXCHANGE)` on two btrfs subvolumes.
7. apt's hook protocol details on Ubuntu 26.04 (the parser tolerates versions
   2 and 3 and unknown lines; the actual package change is computed from the
   dpkg database regardless).
