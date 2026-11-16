#!/usr/bin/python3 -I
"""Fixed-command live installer elevation; never a general root wrapper."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys


def validate_live_media(cmdline=Path("/proc/cmdline"), source=Path("/cdrom/casper/filesystem.squashfs")):
    if "boot=casper" not in cmdline.read_text().split():
        raise ValueError("The installer is available only in a DesOS live session")
    if not source.is_file() or source.is_symlink():
        raise ValueError("DesOS installation media is missing from /cdrom/casper")
    with source.open("rb") as image:
        if image.read(4) != b"hsqs":
            raise ValueError("Installation source is not a SquashFS image")


def base_environment():
    return {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root", "USER": "root", "LOGNAME": "root",
        "LANG": "C.UTF-8", "QT_AUTO_SCREEN_SCALE_FACTOR": "1",
    }


def owned_socket(path, uid):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid not in {0, uid}:
        raise ValueError("Display connection is not an owned local socket")


def display_environment(uid, display, wayland, xauthority):
    if display and not re.fullmatch(r":[0-9]{1,5}(?:\.[0-9]{1,2})?", display):
        raise ValueError("Only a local X display is allowed")
    if wayland and not re.fullmatch(r"wayland-[0-9]{1,5}", wayland):
        raise ValueError("Invalid Wayland display name")
    runtime = Path(f"/run/user/{uid}")
    if not display and not wayland:
        raise ValueError("Launch Install DesOS from the active graphical desktop")
    env = {}
    if wayland:
        info = runtime.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError("Unsafe graphical session runtime directory")
        owned_socket(runtime / wayland, uid)
        env.update({"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": wayland, "QT_QPA_PLATFORM": "wayland"})
    elif display:
        number = display[1:].split(".")[0]
        owned_socket(Path(f"/tmp/.X11-unix/X{number}"), uid)
        env.update({"DISPLAY": display, "QT_QPA_PLATFORM": "xcb"})
        if xauthority:
            auth = Path(xauthority)
            allowed = (runtime, Path("/home/desos"))
            if not auth.is_absolute() or auth.is_symlink() or not any(auth.resolve().is_relative_to(base) for base in allowed):
                raise ValueError("Xauthority must be a regular file inside the live user's session")
            info = auth.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022 or info.st_size > 65536:
                raise ValueError("Unsafe Xauthority file")
            env["XAUTHORITY"] = str(auth)
        else:
            raise ValueError("The active X display has no Xauthority file")
    return env


MANUAL_CONFIG = "/usr/share/desos/calamares-manual"
PROBE = "/usr/lib/desos/calamares/modules/desosdisk/probe.py"
PROBED = Path("/run/desos-installer/disks.json")
CHOICE = Path("/run/desos-installer/choice.json")


def probe_disks(env):
    """What the disk page shows, read now as root (lsblk, os-prober), into a
    root-owned file the page reads. Nothing is written to any disk."""
    PROBED.parent.mkdir(mode=0o755, exist_ok=True)
    os.chmod(PROBED.parent, 0o755)
    # A choice from an earlier run must never be used: the page writes a new one.
    CHOICE.unlink(missing_ok=True)
    found = subprocess.run(["/usr/bin/python3", "-E", "-s", PROBE], env=env, capture_output=True,
                           text=True, timeout=180, check=True).stdout
    json.loads(found)  # refuse anything that is not the probe's JSON
    temporary = PROBED.with_suffix(".new")
    temporary.write_text(found)
    os.chmod(temporary, 0o644)
    os.replace(temporary, PROBED)


def main():
    parser = argparse.ArgumentParser(description="Launch the DesOS live installer")
    parser.add_argument("--display", required=True)
    parser.add_argument("--wayland-display", required=True)
    parser.add_argument("--xauthority", required=True)
    parser.add_argument("--manual", action="store_true",
                        help="Calamares' partition page instead of the DesOS disk page")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("The installer helper must be launched through pkexec")
    caller = os.environ.get("PKEXEC_UID", "")
    if not caller.isdecimal() or int(caller) < 1000:
        raise ValueError("A live desktop user must launch the installer")
    uid = int(caller)
    if pwd.getpwuid(uid).pw_name != "desos":
        raise ValueError("Only the live desktop account can launch the installer")
    state = subprocess.check_output(
        ["/usr/bin/loginctl", "show-user", str(uid), "--property=State", "--value"],
        env=base_environment(), text=True, timeout=10,
    ).strip()
    if state != "active":
        raise ValueError("The live graphical session is not active")
    validate_live_media()
    env = base_environment()
    env.update(display_environment(uid, args.display, args.wayland_display, args.xauthority))
    os.chdir("/")
    os.umask(0o077)
    # The standard search uses /etc/calamares and /usr/share/calamares.
    # Do not use -c: that overrides the data directory, hiding the branding.
    # Neither arguments nor configuration paths are caller-controlled: --manual
    # only switches to the fixed, root-owned MANUAL_CONFIG settings, found first
    # through XDG_CONFIG_DIRS (module configuration then comes from /etc).
    if args.manual:
        env["XDG_CONFIG_DIRS"] = f"{MANUAL_CONFIG}:/etc"
        os.execve("/usr/bin/calamares", ["calamares", "--xdg-config"], env)
    else:
        probe_disks(base_environment())
        # The disk page reads PROBED and writes CHOICE with XMLHttpRequest,
        # which Qt allows for local files only with these set. Calamares loads
        # only root-owned QML (the DesOS branding), so this reaches no other code.
        env["QML_XHR_ALLOW_FILE_READ"] = "1"
        env["QML_XHR_ALLOW_FILE_WRITE"] = "1"
        os.execve("/usr/bin/calamares", ["calamares"], env)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Install DesOS: {error}", file=sys.stderr)
        sys.exit(1)
