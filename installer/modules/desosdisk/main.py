"""Calamares 3.3 job: puts the disk plan chosen on the disk page on the disk.

Runs first in the install phase, where Calamares' partition job would. It
re-reads the disks (the page's list may be minutes old), refuses anything the
probe would not offer, writes the plan, and tells the mount, fstab, bootloader
and grubcfg jobs about the result in the partition module's own format."""
import json
import subprocess
import sys

import libcalamares

sys.path.insert(0, "/usr/lib/desos/calamares/modules/desosdisk")
import apply  # noqa: E402 (the folder above, as installed)
import plan  # noqa: E402
import probe  # noqa: E402


def choice():
    return libcalamares.globalstorage.value("desosDiskChoice") or {}


def pretty_name():
    picked = choice()
    if not picked.get("disk"):
        return "Prepare the disk"
    return f"Erase {picked['disk']} and set it up for DesOS"


def runner(argv, stdin):
    libcalamares.utils.debug("desosdisk: " + " ".join(argv))
    subprocess.run(argv, input=stdin, text=True, check=True, capture_output=True)


def uuid_of(device):
    return subprocess.run(["blkid", "-s", "UUID", "-o", "value", device],
                          capture_output=True, text=True, check=True).stdout.strip()


def run():
    try:
        picked = choice()
        if picked.get("mode", "erase") != "erase":
            return ("Not supported yet", "Only erasing a whole disk is set up automatically in this preview.")
        gs = libcalamares.globalstorage
        lsblk = json.loads(subprocess.run(probe.LSBLK, capture_output=True, text=True, check=True).stdout)
        disk = apply.refuse_unsafe(picked.get("disk", ""), probe.disks(lsblk))
        firmware = gs.value("firmwareType")
        choices = plan.Choices(filesystem=picked.get("filesystem", "btrfs"), swap=picked.get("swap", "none"),
                               home_bytes=int(picked.get("homeBytes", 0)))
        parts = plan.plan(disk["size"], gs.value("desosMemory"), firmware, choices, disk["sector"])
        script = plan.sfdisk_script(parts, disk["sector"])
        libcalamares.utils.debug("desosdisk plan:\n" + script)
        steps = apply.commands(disk["path"], parts, script, [p["path"] for p in disk["partitions"]])
        for done, step in enumerate(steps, start=1):
            apply.run([step], runner)
            libcalamares.job.setprogress(done / len(steps))
        gs.insert("partitions", apply.global_partitions(disk["path"], parts, uuid_of))
        gs.insert("bootLoader", apply.boot_loader(disk["path"], firmware))
        gs.insert("efiSystemPartition", "/boot/efi")
    except plan.PlanError as error:
        return ("The disk could not be set up", str(error))
    except subprocess.CalledProcessError as error:
        libcalamares.utils.warning(f"desosdisk: {error.cmd} failed: {error.stderr}")
        return ("The disk could not be set up",
                f"{error.cmd[0]} failed on {error.cmd[-1]}. Nothing else was changed after it. "
                "See the installer log for details.")
    except Exception as error:
        libcalamares.utils.warning(f"desosdisk failed: {error}")
        return ("The disk could not be set up", "See the installer log for details.")
    return None
