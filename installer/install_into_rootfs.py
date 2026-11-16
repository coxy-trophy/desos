#!/usr/bin/python3 -I
"""Copy installer integration into an explicitly selected clean DesOS rootfs."""
import argparse
import os
from pathlib import Path
import re
import shutil
import sys


def safe_destination(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid rootfs-relative path")
    target = root / relative
    for component in [target, *target.parents]:
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f"Rootfs destination contains a symlink: {relative}")
    return target


def validate_root(root):
    root = Path(root)
    if not root.is_absolute() or root == Path("/") or root.resolve() != root or not root.is_dir():
        raise ValueError("Supply an absolute, existing, non-root path without symlink components")
    if len(root.parts) < 3 or root in {Path("/usr"), Path("/etc"), Path("/var"), Path("/home"), Path("/root"), Path("/run"), Path("/tmp"), Path("/mnt"), Path("/media")}:
        raise ValueError("Refusing unsafe rootfs destination")
    release = root / "etc/os-release"
    if not release.resolve().is_relative_to(root) or not re.search(r'^ID=[\"\']?desos[\"\']?\s*$', release.read_text(), re.MULTILINE):
        raise ValueError("Rootfs must identify itself as DesOS before installing integration")
    return root


def install(root, source):
    root = validate_root(root)
    if os.geteuid() != 0:
        raise ValueError("Root privileges are required to install root-owned integration")
    if not safe_destination(root, "usr/bin/calamares").is_file():
        raise ValueError("Install the Ubuntu Calamares package into the rootfs first")
    # Preflight every destination before making any changes.
    copies = []
    for source_dir, dest_dir in (
        (source / "calamares", "etc/calamares"),
        (source / "branding/desos", "usr/share/calamares/branding/desos"),
        (source / "modules/desoscleanup", "usr/lib/desos/calamares/modules/desoscleanup"),
        (source / "modules/desosdisk", "usr/lib/desos/calamares/modules/desosdisk"),
        (source / "modules/desosbootloader", "usr/lib/desos/calamares/modules/desosbootloader"),
        (source / "calamares-manual", "usr/share/desos/calamares-manual"),
    ):
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                copies.append((path, safe_destination(root, str(Path(dest_dir) / path.relative_to(source_dir))), 0o644))
    for font in sorted((source / "fonts").iterdir()):
        directory = "usr/share/fonts/truetype/desos" if font.suffix == ".ttf" else "usr/share/doc/desos-installer/fonts"
        copies.append((font, safe_destination(root, f"{directory}/{font.name}"), 0o644))
    for src, dest, mode in (
        ("bin/desos-installer", "usr/bin/desos-installer", 0o755),
        ("bin/desos-installer-helper.py", "usr/libexec/desos-installer-helper", 0o755),
        ("polkit/org.desos.installer.policy", "usr/share/polkit-1/actions/org.desos.installer.policy", 0o644),
        ("polkit/49-desos-live-installer.rules", "etc/polkit-1/rules.d/49-desos-live-installer.rules", 0o644),
        ("desktop/desos-installer.desktop", "usr/share/applications/desos-installer.desktop", 0o644),
        ("desktop/desos-installer-manual.desktop", "usr/share/applications/desos-installer-manual.desktop", 0o644),
        ("branding/desos/icon.svg", "usr/share/icons/hicolor/scalable/apps/desos-installer.svg", 0o644),
    ):
        copies.append((source / src, safe_destination(root, dest), mode))
    for src, target, mode in copies:
        if not src.is_file() or src.is_symlink():
            raise ValueError(f"Missing or unsafe installer source: {src}")
    for src, target, mode in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        os.chown(target, 0, 0)
        target.chmod(mode)
    print(f"Installed DesOS Calamares integration into {root}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rootfs", type=Path, help="Absolute path to a clean DesOS root filesystem")
    args = parser.parse_args()
    install(args.rootfs, Path(__file__).resolve().parent)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Installer integration: {error}", file=sys.stderr)
        sys.exit(1)
