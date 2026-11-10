"""Convert the extracted DesOS live image into an installed system.

Called only by the Calamares job adapter, with its mounted target and command
runner. Every path is checked before any mutation; no shell commands are built
from user input. This module does not partition disks or mount filesystems.
"""
import os
from pathlib import Path
import re
import shutil


GREETD_CONFIG = '''# DesOS installed session: authenticate through the graphical greeter.
[terminal]
vt = 1

[default_session]
command = "/usr/bin/des-ui-greeter"
user = "_greetd"
'''

# Paths owned by the DesOS live-image integration, not arbitrary distro files.
REMOVE_PATHS = (
    "home/desos", "root/.ssh", "var/mail/desos",
    "etc/sudoers.d/90-desos-live",
    "etc/polkit-1/rules.d/49-desos-live-installer.rules",
    "var/lib/polkit-1/localauthority/10-vendor.d/10-live-cd.pkla",
    "etc/sudoers.d/casper",
    "usr/share/polkit-1/actions/org.desos.installer.policy",
    "usr/bin/desos-installer", "usr/libexec/desos-installer-helper",
    "usr/share/applications/desos-installer.desktop",
    "etc/xdg/autostart/desos-installer.desktop",
    "etc/casper.conf", "etc/casper-bottom", "etc/casper",
    "usr/share/initramfs-tools/scripts/casper-bottom/99-desos-live",
    "usr/share/initramfs-tools/scripts/casper-bottom/99desos-live",
    "usr/lib/desos/live-session", "etc/initramfs-tools/conf.d/desos-live",
    "var/lib/casper", "run/desos-live",
    "etc/systemd/system/getty@tty1.service.d/autologin.conf",
    "etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf",
    "etc/systemd/system/greetd.service.d/live.conf",
    "etc/systemd/system/desos-live.service",
    "etc/systemd/system/multi-user.target.wants/desos-live.service",
    "etc/systemd/system/graphical.target.wants/desos-live.service",
    "etc/default/grub.d/50-desos-live.cfg",
)


def confined_path(root, relative):
    """Allow symlink leaves to be unlinked, never traverse symlink parents."""
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid target-relative path")
    path = root / relative
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"Refusing symlink parent in installed target: {relative}")
    return path


def remove_path(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def validate_target(root, username):
    root = Path(root)
    if not root.is_absolute() or root == Path("/") or root.resolve() != root:
        raise ValueError("Cleanup requires an absolute, non-root mounted target")
    if not os.path.ismount(root):
        raise ValueError("Cleanup target is not a mounted filesystem")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username) or username in {"root", "desos", "nobody"}:
        raise ValueError("Invalid installed username; live username is reserved")
    identity = confined_path(root, "etc/os-release")
    # Ubuntu commonly makes os-release a relative symlink into /usr/lib.
    if not identity.resolve().is_relative_to(root):
        raise ValueError("Target identity escapes its filesystem")
    if not re.search(r'^ID=[\"\']?desos[\"\']?\s*$', identity.read_text(), re.MULTILINE):
        raise ValueError("Cleanup target is not DesOS")
    passwd = confined_path(root, "etc/passwd")
    shadow = confined_path(root, "etc/shadow")
    if passwd.is_symlink() or shadow.is_symlink():
        raise ValueError("Target account databases must be regular files")
    users = {line.split(":", 1)[0]: line.split(":") for line in passwd.read_text().splitlines()}
    account = users.get(username)
    if not account or len(account) != 7 or not account[2].isdigit() or int(account[2]) < 1000:
        raise ValueError("Configured installed user does not exist")
    hashes = {line.split(":", 1)[0]: line.split(":")[1] for line in shadow.read_text().splitlines() if ":" in line}
    password = hashes.get(username, "")
    if not password or password.startswith(("!", "*")):
        raise ValueError("Configured installed user does not have an enabled password")
    # Preflight every destructive/written path before apt or userdel runs.
    paths = [confined_path(root, item) for item in REMOVE_PATHS]
    greetd = confined_path(root, "etc/greetd/config.toml")
    if greetd.is_symlink():
        raise ValueError("Refusing symlink greetd configuration")
    receipt = confined_path(root, "var/lib/desos/installation.json")
    if receipt.is_symlink():
        raise ValueError("Refusing symlink installation receipt")
    ssh = confined_path(root, "etc/ssh")
    if ssh.is_symlink():
        raise ValueError("Refusing symlink SSH configuration directory")
    paths.extend(ssh.glob("ssh_host_*"))
    return root, users, paths, greetd, receipt


def cleanup_target(root, username, run_in_target):
    root, users, paths, greetd, receipt = validate_target(root, username)
    # Purging casper removes its initramfs hooks before the subsequent
    # Calamares initramfs job. This is entirely offline; no apt update/install.
    run_in_target(["/usr/bin/apt-get", "-y", "purge", "casper"])
    if "desos" in users:
        run_in_target(["/usr/sbin/userdel", "desos"])
    for path in paths:
        remove_path(path)
    greetd.parent.mkdir(parents=True, exist_ok=True)
    greetd.write_text(GREETD_CONFIG)
    greetd.chmod(0o644)
    # Installed machines require credentials, including on the first boot.
    if any(line.split(":", 1)[0] == "desos" for line in (root / "etc/passwd").read_text().splitlines()):
        raise RuntimeError("Live account removal failed")
    if "initial_session" in greetd.read_text():
        raise RuntimeError("Installed greeter still has an automatic session")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    # No account names, password hashes, or installer answers enter this receipt.
    receipt.write_text('{"installer":"Calamares","live_state_removed":true}\n')
    receipt.chmod(0o644)
