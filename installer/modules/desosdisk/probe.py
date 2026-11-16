"""What the disk page shows: the disks DesOS may go on, the memory, the
firmware, and the systems already on each disk. The parsing is pure and tested
without hardware (tests/test_desosdisk.py). collect() runs the commands; the
installer launcher runs this file before Calamares starts and writes the result
to /run/desos-installer/disks.json, which the disk page reads. (A Calamares job
before the first page would put two extra steps in the rail.)
"""
import json
import os
import subprocess
import sys

from plan import MIN_ROOT, EFI_SIZE

LSBLK = ["lsblk", "--json", "--bytes", "--output",
         "NAME,PATH,TYPE,SIZE,RO,RM,TRAN,MODEL,LOG-SEC,PTTYPE,FSTYPE,LABEL,MOUNTPOINTS"]
# Where the live system is mounted from: the disk holding these is never offered.
LIVE_MOUNTS = ("/cdrom", "/run/live/medium", "/run/initramfs/live")


def systems(os_prober_output):
    """os-prober's lines, "device:Long name:Short:type", by partition."""
    found = {}
    for line in os_prober_output.splitlines():
        fields = line.split(":")
        if len(fields) >= 3 and fields[0].startswith("/dev/"):
            device = fields[0].split("@")[0]  # EFI entries: /dev/sda1@/EFI/...
            found.setdefault(device, fields[1] or fields[2])
    return found


def _mounts(node):
    return [m for m in (node.get("mountpoints") or []) if m]


def _walk(node):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def disks(lsblk, found_systems=None):
    """The disks to offer, biggest first, each with what is on it now.

    `lsblk` is lsblk's --json output as loaded; `found_systems` comes from
    systems() above."""
    found_systems = found_systems or {}
    offered = []
    for node in lsblk.get("blockdevices", []):
        if node.get("type") != "disk" or node.get("name", "").startswith(("zram", "loop", "ram")):
            continue
        tree = list(_walk(node))
        mounts = [m for n in tree for m in _mounts(n)]
        reason = None
        if any(m.startswith(LIVE_MOUNTS) for m in mounts):
            reason = "the DesOS USB, in use"
        elif node.get("ro"):
            reason = "read-only"
        elif int(node.get("size") or 0) < MIN_ROOT + EFI_SIZE + 2 * 1024 ** 2:
            reason = f"smaller than {(MIN_ROOT + EFI_SIZE) / 1e9:.0f} GB"
        partitions = []
        for child in tree[1:]:
            if child.get("type") != "part":
                continue
            partitions.append({
                "path": child["path"],
                "size": int(child.get("size") or 0),
                "filesystem": child.get("fstype") or "",
                "label": child.get("label") or "",
                "system": found_systems.get(child["path"], ""),
            })
        names = []
        for part in partitions:
            if part["system"] and part["system"] not in names:
                names.append(part["system"])
        offered.append({
            "path": node["path"],
            "size": int(node.get("size") or 0),
            "model": (node.get("model") or "").strip(),
            "transport": node.get("tran") or "",
            "removable": bool(node.get("rm")),
            "sector": int(node.get("log-sec") or 512),
            "table": node.get("pttype") or "",
            "partitions": partitions,
            "systems": names,
            "empty": not partitions,
            "usable": reason is None,
            "why_not": reason or "",
        })
    offered.sort(key=lambda disk: (not disk["usable"], disk["removable"], -disk["size"]))
    return offered


def memory_bytes(meminfo):
    """MemTotal from /proc/meminfo, in bytes."""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise ValueError("no MemTotal in /proc/meminfo")


def describe(disk):
    """The disk's line on the page: "sda · 238.5 GB · HFS256G39TND · Kali Linux found"."""
    name = disk["path"].rsplit("/", 1)[-1]
    parts = [name, f"{disk['size'] / 1e9:.1f} GB"]
    if disk["model"]:
        parts.append(disk["model"])
    if not disk["usable"]:
        parts.append(disk["why_not"])
    elif disk["systems"]:
        parts.append(" and ".join(disk["systems"]) + " found")
    elif disk["empty"]:
        parts.append("empty")
    return " · ".join(parts)


def firmware():
    return "efi" if os.path.isdir("/sys/firmware/efi") else "bios"


def collect():
    """Reads the machine: disks (with os-prober's findings), memory, firmware."""
    lsblk = json.loads(subprocess.run(LSBLK, capture_output=True, text=True, check=True).stdout)
    try:
        found = subprocess.run(["os-prober"], capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        found = ""
    with open("/proc/meminfo") as meminfo:
        memory = memory_bytes(meminfo.read())
    offered = disks(lsblk, systems(found))
    for disk in offered:
        disk["line"] = describe(disk)
    return {"disks": offered, "memory": memory, "firmware": firmware()}


if __name__ == "__main__":
    json.dump(collect(), sys.stdout)
