"""Writing a plan to a disk, and what the later Calamares jobs are told about it.

The commands are built as lists first (commands()), so tests read exactly what
would run without running it; run() then executes them through a runner the
Calamares job passes in. Nothing here decides the layout: that is plan.py.
"""
from plan import PlanError

# Calamares' file system names (plan.py) to the tool that makes each.
MAKE = {
    "fat32": lambda dev, label: ["mkfs.fat", "-F", "32", "-n", label[:11].upper(), dev],
    "btrfs": lambda dev, label: ["mkfs.btrfs", "-f", "-L", label, dev],
    "ext4": lambda dev, label: ["mkfs.ext4", "-F", "-q", "-L", label, dev],
    "linuxswap": lambda dev, label: ["mkswap", "-L", label, dev],
}


def partition_path(disk, number):
    """/dev/sda + 1 -> /dev/sda1; /dev/nvme0n1 + 1 -> /dev/nvme0n1p1 (the kernel's names)."""
    return f"{disk}{'p' if disk[-1].isdigit() else ''}{number}"


def refuse_unsafe(disk, probed):
    """The disk must be one the probe offered as usable: never the live USB,
    never a read-only or too-small disk, never a name typed elsewhere."""
    match = [d for d in probed if d["path"] == disk]
    if not match:
        raise PlanError(f"{disk} is not a disk this installer found")
    if not match[0]["usable"]:
        raise PlanError(f"{disk} cannot be used: {match[0]['why_not']}")
    return match[0]


def commands(disk, parts, script, existing_partitions):
    """Everything that is run, in order, to put `parts` on `disk`.

    `existing_partitions` are the disk's partitions now (from the probe): their
    swap is switched off and their mounts released, then their signatures are
    wiped so nothing old is found again on the new partitions."""
    steps = []
    for old in existing_partitions:
        steps.append((["swapoff", old], True))      # may not be swap: failure is fine
        steps.append((["umount", "--recursive", old], True))
    for old in existing_partitions:
        steps.append((["wipefs", "--all", "--force", old], True))
    steps.append((["wipefs", "--all", "--force", disk], False))
    steps.append((["sfdisk", "--wipe", "always", "--wipe-partitions", "always", disk], False, script))
    steps.append((["udevadm", "settle"], False))
    for number, part in enumerate(parts, start=1):
        if part.filesystem:
            steps.append((MAKE[part.filesystem](partition_path(disk, number), part.label), False))
    steps.append((["udevadm", "settle"], False))
    return steps


def run(steps, runner):
    """Runs each step; `runner(argv, stdin)` raises on failure. Steps marked
    as allowed to fail (switching off swap that is not swap) are not checked."""
    for step in steps:
        argv, may_fail, *stdin = step
        try:
            runner(argv, stdin[0] if stdin else None)
        except Exception:
            if not may_fail:
                raise


def global_partitions(disk, parts, uuid_of):
    """The partitions as Calamares' partition module would describe them, for
    the mount, fstab, bootloader and grubcfg jobs. `uuid_of(device)` reads a
    file system UUID (blkid)."""
    described = []
    for number, part in enumerate(parts, start=1):
        if not part.filesystem:
            continue  # the BIOS boot partition has nothing to mount
        device = partition_path(disk, number)
        described.append({
            "device": device,
            "fs": part.filesystem,
            "fsName": part.filesystem,
            "mountPoint": part.mount_point,
            "uuid": uuid_of(device),
            "partlabel": part.label,
            "parttype": part.type_guid,
            "claimed": True,     # ours: fstab lists it, grubcfg uses swap for resume=
            "features": {},
        })
    return described


def boot_loader(disk, firmware):
    """Calamares' bootLoader entry: where GRUB goes on a BIOS machine.
    On UEFI the bootloader job installs to efiSystemPartition instead."""
    return {"installPath": disk if firmware == "bios" else None}
