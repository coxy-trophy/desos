"""Calamares 3.3 job, run before the first page: what the disk page shows.

Puts in global storage: desosDisks (probe.disks), desosMemory (bytes),
firmwareType ("efi" or "bios", as Calamares' partition module would) and
efiSystemPartition. Reads only; nothing is written to any disk."""
import json
import os
import subprocess
import sys

import libcalamares

sys.path.insert(0, "/usr/lib/desos/calamares/modules/desosdisk")


def pretty_name():
    return "Looking at the disks"


def run():
    try:
        import probe
        lsblk = json.loads(subprocess.run(probe.LSBLK, capture_output=True, text=True, check=True).stdout)
        try:
            found = subprocess.run(["os-prober"], capture_output=True, text=True, timeout=120).stdout
        except (OSError, subprocess.TimeoutExpired):
            found = ""
        with open("/proc/meminfo") as meminfo:
            memory = probe.memory_bytes(meminfo.read())
        disks = probe.disks(lsblk, probe.systems(found))
        for disk in disks:
            disk["line"] = probe.describe(disk)
        gs = libcalamares.globalstorage
        gs.insert("desosDisks", disks)
        gs.insert("desosMemory", memory)
        gs.insert("firmwareType", "efi" if os.path.isdir("/sys/firmware/efi") else "bios")
        gs.insert("efiSystemPartition", "/boot/efi")
    except Exception as error:
        libcalamares.utils.warning(f"DesOS disk probe failed: {error}")
        return ("The disks could not be read",
                "The installer could not list this computer's disks. See the installer log for details.")
    return None
