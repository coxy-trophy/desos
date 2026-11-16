"""The DesOS disk plan: from the disk, the memory, the firmware and the user's
choices to an exact GPT layout. Pure: nothing here touches a disk, so every
layout the installer can write is covered by tests (tests/test_desosdisk.py).

Layout, front to back (installer/DESIGN.md, "Disk"):
  BIOS boot 1 MiB   only on BIOS machines (GRUB's core image on GPT)
  EFI 512 MiB       only on UEFI machines, FAT32 at /boot/efi
  swap              when chosen: "small" (up to 4 GiB) or "suspend" (RAM-sized)
  DesOS             the rest, btrfs (undo snapshots) or ext4
  /home             when a separate /home partition is chosen, of the chosen size

On btrfs /home is already a subvolume of its own (mount.conf), so a separate
/home partition is an option, not the default. Kernels stay inside the root
filesystem: there is no /boot partition (undo/README.md).
"""
from dataclasses import dataclass

MiB = 1024 ** 2
GiB = 1024 ** 3
ALIGN = MiB
EFI_SIZE = 512 * MiB
BIOS_BOOT_SIZE = 1 * MiB
SMALL_SWAP_MAX = 4 * GiB
MIN_ROOT = 16 * GiB
MIN_HOME = 20 * GiB
# The backup GPT at the end of the disk: header plus 32 sectors of entries.
GPT_TAIL_SECTORS = 33

FILESYSTEMS = ("btrfs", "ext4")
SWAP_CHOICES = ("none", "small", "suspend")

TYPE = {
    "bios": "21686148-6449-6E6F-744E-656564454649",
    "efi": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
    "swap": "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F",
    "root": "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709",  # Linux root (x86-64)
    "home": "933AC7E1-2EB4-4F13-B844-0E14E2AEF915",
}


class PlanError(ValueError):
    """A plan that cannot be made; the message is shown to the user as it is."""


@dataclass(frozen=True)
class Choices:
    filesystem: str = "btrfs"
    swap: str = "none"
    home_bytes: int = 0  # 0: no separate /home partition

    def check(self):
        if self.filesystem not in FILESYSTEMS:
            raise PlanError(f"Unknown file system {self.filesystem!r}")
        if self.swap not in SWAP_CHOICES:
            raise PlanError(f"Unknown swap choice {self.swap!r}")
        if self.home_bytes < 0 or self.home_bytes % ALIGN:
            raise PlanError("The /home size must be a whole number of MiB")


@dataclass(frozen=True)
class Part:
    role: str          # bios, efi, swap, root, home
    start: int         # bytes from the start of the disk, MiB-aligned
    size: int          # bytes, MiB-aligned
    filesystem: str    # Calamares' names: fat32, linuxswap, btrfs, ext4; "" for bios
    mount_point: str   # "" for bios and swap
    label: str         # the GPT partition name

    @property
    def type_guid(self):
        return TYPE[self.role]


def down(value, unit=ALIGN):
    return value - value % unit


def up(value, unit=ALIGN):
    return -(-value // unit) * unit


def swap_size(choice, ram_bytes):
    """Swap on the disk. zram is always on as well (system/README.md)."""
    if choice == "none":
        return 0
    if choice == "small":
        return min(SMALL_SWAP_MAX, up(ram_bytes, GiB))
    # Hibernation writes memory to swap: as much swap as RAM, in whole GiB.
    return up(ram_bytes, GiB)


def usable(disk_bytes, sector=512):
    """The first and the end (exclusive) of the MiB-aligned space GPT leaves."""
    return ALIGN, down(disk_bytes - GPT_TAIL_SECTORS * sector)


def plan(disk_bytes, ram_bytes, firmware, choices, sector=512):
    """The partitions to make on an emptied disk, front to back."""
    choices.check()
    if firmware not in ("efi", "bios"):
        raise PlanError(f"Unknown firmware {firmware!r}")
    first, end = usable(disk_bytes, sector)
    fixed = []
    if firmware == "bios":
        fixed.append(("bios", BIOS_BOOT_SIZE, "", "", "BIOS boot"))
    else:
        fixed.append(("efi", EFI_SIZE, "fat32", "/boot/efi", "EFI"))
    swap = swap_size(choices.swap, ram_bytes)
    if swap:
        fixed.append(("swap", swap, "linuxswap", "", "swap"))
    taken = sum(size for _, size, *_ in fixed)
    root = end - first - taken - choices.home_bytes
    if choices.home_bytes and choices.home_bytes < MIN_HOME:
        raise PlanError(f"A separate /home needs at least {MIN_HOME // GiB} GB")
    if root < MIN_ROOT:
        need = (MIN_ROOT + taken + choices.home_bytes + first + (disk_bytes - end)) / 1e9
        raise PlanError(f"DesOS needs a disk of at least {need:.1f} GB with these choices")
    layout = fixed + [("root", root, choices.filesystem, "/", "DesOS")]
    if choices.home_bytes:
        layout.append(("home", choices.home_bytes, choices.filesystem, "/home", "home"))
    parts, at = [], first
    for role, size, filesystem, mount_point, label in layout:
        parts.append(Part(role, at, size, filesystem, mount_point, label))
        at += size
    return parts


def largest_home(disk_bytes, ram_bytes, firmware, swap, sector=512):
    """The biggest separate /home that still leaves DesOS its minimum, for the slider."""
    first, end = usable(disk_bytes, sector)
    fixed = (BIOS_BOOT_SIZE if firmware == "bios" else EFI_SIZE) + swap_size(swap, ram_bytes)
    return max(0, down(end - first - fixed - MIN_ROOT, GiB))


def sfdisk_script(parts, sector=512):
    """The layout as an sfdisk script: a new GPT, sizes in sectors."""
    lines = ["label: gpt", "unit: sectors", ""]
    for part in parts:
        if part.start % sector or part.size % sector:
            raise PlanError("A partition is not a whole number of sectors")
        lines.append(f'start={part.start // sector}, size={part.size // sector}, '
                     f'type={part.type_guid}, name="{part.label}"')
    return "\n".join(lines) + "\n"
