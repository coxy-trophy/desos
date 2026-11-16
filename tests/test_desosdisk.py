"""The DesOS disk plan (installer/modules/desosdisk/plan.py)."""
import importlib
from pathlib import Path
import sys
import unittest

# The modules import each other by name, as Calamares runs them from their folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer/modules/desosdisk"))
plan = importlib.import_module("plan")
probe = importlib.import_module("probe")
apply = importlib.import_module("apply")
GiB, MiB = plan.GiB, plan.MiB

# The first real install: a 238.5 GiB SATA SSD, 12 GiB of RAM, UEFI.
LAPTOP = 256060514304
RAM = 12 * GiB - 517 * MiB


def roles(parts):
    return [part.role for part in parts]


class PlanTests(unittest.TestCase):
    def check_contiguous(self, parts, disk):
        first, end = plan.usable(disk)
        self.assertEqual(parts[0].start, first)
        for a, b in zip(parts, parts[1:]):
            self.assertEqual(a.start + a.size, b.start)
        self.assertLessEqual(parts[-1].start + parts[-1].size, end)
        self.assertEqual(parts[-1].start + parts[-1].size, end)  # nothing wasted
        for part in parts:
            self.assertEqual(part.start % MiB, 0)
            self.assertEqual(part.size % MiB, 0)

    def test_default_is_efi_and_one_btrfs_root(self):
        parts = plan.plan(LAPTOP, RAM, "efi", plan.Choices())
        self.assertEqual(roles(parts), ["efi", "root"])
        efi, root = parts
        self.assertEqual((efi.size, efi.filesystem, efi.mount_point), (512 * MiB, "fat32", "/boot/efi"))
        self.assertEqual((root.filesystem, root.mount_point), ("btrfs", "/"))
        self.check_contiguous(parts, LAPTOP)

    def test_bios_gets_a_bios_boot_partition_instead_of_efi(self):
        parts = plan.plan(LAPTOP, RAM, "bios", plan.Choices(filesystem="ext4"))
        self.assertEqual(roles(parts), ["bios", "root"])
        self.assertEqual((parts[0].size, parts[0].filesystem, parts[0].mount_point), (MiB, "", ""))
        self.check_contiguous(parts, LAPTOP)

    def test_swap_sizes(self):
        self.assertEqual(plan.swap_size("none", RAM), 0)
        self.assertEqual(plan.swap_size("small", RAM), 4 * GiB)
        self.assertEqual(plan.swap_size("small", 2 * GiB), 2 * GiB)
        # Hibernation: RAM, rounded up to whole GiB.
        self.assertEqual(plan.swap_size("suspend", RAM), 12 * GiB)
        parts = plan.plan(LAPTOP, RAM, "efi", plan.Choices(swap="suspend"))
        self.assertEqual(roles(parts), ["efi", "swap", "root"])
        self.assertEqual((parts[1].size, parts[1].filesystem, parts[1].mount_point), (12 * GiB, "linuxswap", ""))
        self.check_contiguous(parts, LAPTOP)

    def test_separate_home_takes_the_chosen_size_at_the_end(self):
        parts = plan.plan(LAPTOP, RAM, "efi", plan.Choices(filesystem="ext4", swap="small", home_bytes=140 * GiB))
        self.assertEqual(roles(parts), ["efi", "swap", "root", "home"])
        home = parts[-1]
        self.assertEqual((home.size, home.filesystem, home.mount_point), (140 * GiB, "ext4", "/home"))
        self.check_contiguous(parts, LAPTOP)

    def test_the_slider_maximum_leaves_desos_its_minimum(self):
        biggest = plan.largest_home(LAPTOP, RAM, "efi", "suspend")
        parts = plan.plan(LAPTOP, RAM, "efi", plan.Choices(swap="suspend", home_bytes=biggest))
        self.assertGreaterEqual(parts[2].size, plan.MIN_ROOT)
        with self.assertRaises(plan.PlanError):
            plan.plan(LAPTOP, RAM, "efi", plan.Choices(swap="suspend", home_bytes=biggest + 2 * GiB))

    def test_too_small_disks_and_homes_are_refused_with_a_reason(self):
        with self.assertRaisesRegex(plan.PlanError, "at least"):
            plan.plan(12 * GiB, RAM, "efi", plan.Choices())
        with self.assertRaisesRegex(plan.PlanError, "/home needs at least 20 GB"):
            plan.plan(LAPTOP, RAM, "efi", plan.Choices(home_bytes=10 * GiB))
        # 16 GiB of root plus EFI fits a 20 GiB disk; with RAM-sized swap it no longer does.
        plan.plan(20 * GiB, RAM, "efi", plan.Choices())
        with self.assertRaises(plan.PlanError):
            plan.plan(20 * GiB, RAM, "efi", plan.Choices(swap="suspend"))

    def test_unknown_choices_are_refused(self):
        for bad in (plan.Choices(filesystem="xfs"), plan.Choices(swap="file"), plan.Choices(home_bytes=GiB + 1)):
            with self.assertRaises(plan.PlanError):
                plan.plan(LAPTOP, RAM, "efi", bad)
        with self.assertRaises(plan.PlanError):
            plan.plan(LAPTOP, RAM, "uboot", plan.Choices())

    def test_sfdisk_script_is_a_new_gpt_in_sectors(self):
        parts = plan.plan(LAPTOP, RAM, "efi", plan.Choices(swap="small"))
        script = plan.sfdisk_script(parts)
        lines = script.splitlines()
        self.assertEqual(lines[:2], ["label: gpt", "unit: sectors"])
        self.assertEqual(lines[3], 'start=2048, size=1048576, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI"')
        self.assertIn('type=0657FD6D-A4AB-43C4-84E5-0933C84B4F4F, name="swap"', lines[4])
        self.assertIn('type=4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709, name="DesOS"', lines[5])
        # 4096-byte sectors: same layout, counted in bigger sectors.
        big = plan.sfdisk_script(plan.plan(LAPTOP, RAM, "efi", plan.Choices(), sector=4096), sector=4096)
        self.assertIn("start=256, size=131072,", big)


def part(name, size, fstype="", label="", mounts=()):
    return {"name": name, "path": "/dev/" + name, "type": "part", "size": size, "ro": False, "rm": False,
            "fstype": fstype or None, "label": label or None, "mountpoints": list(mounts) or [None]}


# The laptop as Kali left it (msdos table, 3 partitions), the DesOS USB stick, zram.
LSBLK = {"blockdevices": [
    {"name": "sda", "path": "/dev/sda", "type": "disk", "size": LAPTOP, "ro": False, "rm": False,
     "tran": "sata", "model": "HFS256G39TND-N210A", "log-sec": 512, "pttype": "dos", "mountpoints": [None],
     "children": [part("sda1", 73873596416, "ext4"), part("sda5", 2898264064, "swap"),
                  part("sda6", 179307872256, "ext4")]},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": 125 * 10 ** 9, "ro": False, "rm": True,
     "tran": "usb", "model": "ProductCode", "log-sec": 512, "pttype": "gpt", "mountpoints": [None],
     "children": [part("sdb1", 2960074752, "iso9660", "DesOS 0.1", ["/cdrom"])]},
    {"name": "zram0", "path": "/dev/zram0", "type": "disk", "size": 4 * GiB, "ro": False, "rm": False,
     "mountpoints": ["[SWAP]"]},
    {"name": "sdc", "path": "/dev/sdc", "type": "disk", "size": 8 * GiB, "ro": False, "rm": True,
     "tran": "usb", "model": "Cruzer", "log-sec": 512, "pttype": None, "mountpoints": [None]},
]}
OS_PROBER = "/dev/sda1:Kali GNU/Linux Rolling:Kali:linux\n"


class ProbeTests(unittest.TestCase):
    def test_disks_are_offered_with_what_is_on_them(self):
        found = probe.disks(LSBLK, probe.systems(OS_PROBER))
        self.assertEqual([d["path"] for d in found], ["/dev/sda", "/dev/sdb", "/dev/sdc"])
        laptop, stick, small = found
        self.assertTrue(laptop["usable"])
        self.assertEqual(laptop["systems"], ["Kali GNU/Linux Rolling"])
        self.assertEqual([p["path"] for p in laptop["partitions"]], ["/dev/sda1", "/dev/sda5", "/dev/sda6"])
        self.assertEqual(probe.describe(laptop), "sda · 256.1 GB · HFS256G39TND-N210A · Kali GNU/Linux Rolling found")
        # The stick the live system runs from is shown, never offered.
        self.assertFalse(stick["usable"])
        self.assertEqual(stick["why_not"], "the DesOS USB, in use")
        self.assertFalse(small["usable"])
        self.assertIn("smaller than", probe.describe(small))
        self.assertTrue(small["empty"])

    def test_os_prober_lines_and_efi_entries(self):
        found = probe.systems("/dev/nvme0n1p1@/EFI/Microsoft/Boot/bootmgfw.efi:Windows Boot Manager:Windows:efi\n"
                              "garbage\n/dev/sda2::Debian:linux\n")
        self.assertEqual(found, {"/dev/nvme0n1p1": "Windows Boot Manager", "/dev/sda2": "Debian"})

    def test_memory_from_meminfo(self):
        self.assertEqual(probe.memory_bytes("MemFree: 1 kB\nMemTotal:  11829508 kB\n"), 11829508 * 1024)
        with self.assertRaises(ValueError):
            probe.memory_bytes("MemFree: 1 kB\n")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.probed = probe.disks(LSBLK, probe.systems(OS_PROBER))
        self.parts = plan.plan(LAPTOP, RAM, "efi", plan.Choices(filesystem="ext4", swap="small", home_bytes=100 * GiB))

    def test_partition_names_follow_the_kernel(self):
        self.assertEqual(apply.partition_path("/dev/sda", 3), "/dev/sda3")
        self.assertEqual(apply.partition_path("/dev/nvme0n1", 1), "/dev/nvme0n1p1")
        self.assertEqual(apply.partition_path("/dev/mmcblk0", 2), "/dev/mmcblk0p2")

    def test_only_a_usable_probed_disk_is_written(self):
        self.assertEqual(apply.refuse_unsafe("/dev/sda", self.probed)["path"], "/dev/sda")
        with self.assertRaisesRegex(plan.PlanError, "DesOS USB"):
            apply.refuse_unsafe("/dev/sdb", self.probed)
        with self.assertRaisesRegex(plan.PlanError, "not a disk this installer found"):
            apply.refuse_unsafe("/dev/sdz", self.probed)

    def test_commands_release_and_wipe_the_old_then_write_the_new(self):
        old = [p["path"] for p in self.probed[0]["partitions"]]
        script = plan.sfdisk_script(self.parts)
        steps = apply.commands("/dev/sda", self.parts, script, old)
        argv = [step[0] for step in steps]
        # Old swap off and mounts released before anything is wiped.
        self.assertEqual(argv[0], ["swapoff", "/dev/sda1"])
        first_wipe = argv.index(["wipefs", "--all", "--force", "/dev/sda1"])
        self.assertTrue(all(a[0] in ("swapoff", "umount") for a in argv[:first_wipe]))
        sfdisk = [s for s in steps if s[0][0] == "sfdisk"][0]
        self.assertEqual(sfdisk[2], script)
        self.assertFalse(sfdisk[1])
        made = [a for a in argv if a[0].startswith(("mkfs", "mkswap"))]
        self.assertEqual(made, [
            ["mkfs.fat", "-F", "32", "-n", "EFI", "/dev/sda1"],
            ["mkswap", "-L", "swap", "/dev/sda2"],
            ["mkfs.ext4", "-F", "-q", "-L", "DesOS", "/dev/sda3"],
            ["mkfs.ext4", "-F", "-q", "-L", "home", "/dev/sda4"],
        ])
        # Only releasing old partitions may fail; every write must succeed.
        self.assertTrue(all(not s[1] for s in steps if s[0][0] in ("sfdisk", "mkfs.fat", "mkswap", "mkfs.ext4")))

    def test_run_stops_at_the_first_real_failure(self):
        ran = []
        def runner(argv, stdin):
            ran.append(argv[0])
            if argv[0] in ("swapoff", "sfdisk"):
                raise RuntimeError(argv[0])
        steps = [(["swapoff", "x"], True), (["sfdisk", "/dev/sda"], False, "s"), (["mkswap", "y"], False)]
        with self.assertRaisesRegex(RuntimeError, "sfdisk"):
            apply.run(steps, runner)
        self.assertEqual(ran, ["swapoff", "sfdisk"])

    def test_calamares_is_told_what_its_partition_module_would_say(self):
        described = apply.global_partitions("/dev/sda", self.parts, lambda device: "UUID-" + device[-1])
        self.assertEqual([(d["device"], d["fs"], d["mountPoint"]) for d in described], [
            ("/dev/sda1", "fat32", "/boot/efi"), ("/dev/sda2", "linuxswap", ""),
            ("/dev/sda3", "ext4", "/"), ("/dev/sda4", "ext4", "/home")])
        for d in described:
            self.assertTrue(d["claimed"])  # fstab lists it; grubcfg takes swap for resume=
            self.assertEqual(d["fsName"], d["fs"])
            self.assertEqual(d["uuid"], "UUID-" + d["device"][-1])
        bios = plan.plan(LAPTOP, RAM, "bios", plan.Choices())
        self.assertEqual([d["device"] for d in apply.global_partitions("/dev/sda", bios, str)], ["/dev/sda2"])
        self.assertEqual(apply.boot_loader("/dev/sda", "bios"), {"installPath": "/dev/sda"})
        self.assertEqual(apply.boot_loader("/dev/sda", "efi"), {"installPath": None})


if __name__ == "__main__":
    unittest.main()
