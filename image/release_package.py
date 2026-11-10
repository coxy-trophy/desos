#!/usr/bin/env python3
"""Build the local DesOS release-identity .deb without root or signing keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


PACKAGE = "desos-release"
VERSION = "0.1.0~preview1"
EPOCH = 1794268800
OS_RELEASE = '''NAME="DesOS"
VERSION="0.1.0-preview1"
ID=desos
ID_LIKE="ubuntu debian"
PRETTY_NAME="DesOS 0.1 Preview"
VERSION_ID="0.1.0"
VERSION_CODENAME=preview1
UBUNTU_CODENAME=resolute
ANSI_COLOR="0;36"
'''
LSB_RELEASE = '''DISTRIB_ID=DesOS
DISTRIB_RELEASE=0.1.0-preview1
DISTRIB_CODENAME=resolute
DISTRIB_DESCRIPTION="DesOS 0.1 Preview"
'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_output(output):
    output = Path(output)
    if not output.is_absolute() or output.resolve() != output or len(output.parts) < 3:
        raise ValueError("Output must be an absolute, non-root directory without symlinks")
    if output.parts[1] in {"bin", "boot", "dev", "etc", "lib", "lib64", "proc", "root", "run", "sbin", "sys", "usr"} or output.is_relative_to(Path("/var/lib/dpkg")):
        raise ValueError("Refusing a system-directory output destination")
    if output.exists() and not output.is_dir():
        raise ValueError("Output is not a directory")
    if any(parent.is_symlink() for parent in (output, *output.parents)):
        raise ValueError("Output contains a symlink")
    return output


def build(output):
    output = validate_output(output)
    resources = Path(__file__).resolve().parent / "release"
    payload = {
        "usr/lib/os-release": OS_RELEASE.encode(),
        "etc/lsb-release": LSB_RELEASE.encode(),
        "usr/share/doc/desos-release/README": (resources / "README").read_bytes(),
    }
    scripts = {"preinst": (resources / "preinst").read_text(), "postrm": (resources / "postrm.in").read_text()}
    scripts["postrm"] = scripts["postrm"].replace("@OS_RELEASE_SHA256@", sha256(payload["usr/lib/os-release"]))
    scripts["postrm"] = scripts["postrm"].replace("@LSB_RELEASE_SHA256@", sha256(payload["etc/lsb-release"]))
    filename = f"{PACKAGE}_{VERSION}_all.deb"
    recordname = f"{PACKAGE}_{VERSION}_all.json"
    with tempfile.TemporaryDirectory(prefix="desos-release-build-") as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir(mode=0o755)
        for name, data in payload.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o644)
        control = stage / "DEBIAN"
        control.mkdir(mode=0o755)
        size = (sum(len(data) for data in payload.values()) + 1023) // 1024
        (control / "control").write_text(f'''Package: {PACKAGE}
Version: {VERSION}
Architecture: all
Section: misc
Priority: optional
Maintainer: DesOS Preview Project <desos@localhost>
Depends: base-files, dpkg
Installed-Size: {size}
Description: DesOS preview identity with preserved Ubuntu vendor metadata
 Provides the DesOS release identity for an Ubuntu-derived native desktop.
 Preserves the Ubuntu base-files identity through package-owned diversions.
''')
        (control / "control").chmod(0o644)
        for name, content in scripts.items():
            (control / name).write_text(content)
            (control / name).chmod(0o755)
        # md5sums are conventional dpkg payload verification metadata; the public
        # provenance record below uses SHA-256 for the archive and every payload.
        (control / "md5sums").write_text("".join(f"{hashlib.md5(data).hexdigest()}  {name}\n" for name, data in sorted(payload.items())))
        (control / "md5sums").chmod(0o644)
        for path in [stage, *stage.rglob("*")]:
            if path.is_dir():
                path.chmod(0o755)
            os.utime(path, (EPOCH, EPOCH))
        package = Path(temporary) / filename
        subprocess.run([
            "/usr/bin/dpkg-deb", "--root-owner-group", "--uniform-compression", "-Zxz", "-z9",
            "--build", str(stage), str(package),
        ], check=True, capture_output=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "SOURCE_DATE_EPOCH": str(EPOCH)})
        data = package.read_bytes()
    record = {
        "schema": 1,
        "package": PACKAGE, "version": VERSION, "architecture": "all",
        "filename": filename, "sha256": sha256(data), "size_bytes": len(data),
        "source_date_epoch": EPOCH,
        "source": "image/release_package.py and image/release/",
        "upstream": {"distribution": "Ubuntu 26.04", "codename": "resolute", "base_package": "base-files", "package_tools": "dpkg"},
        "payload": [{"path": "/" + name, "sha256": sha256(contents), "size_bytes": len(contents), "mode": "0644", "uid": 0, "gid": 0} for name, contents in sorted(payload.items())],
    }
    record_data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    # Check both output names before creating either artifact. Repeated identical
    # builds are idempotent; conflicting owner files are never overwritten.
    artifacts = [(output / filename, data), (output / recordname, record_data)]
    for path, contents in artifacts:
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes() != contents)):
            raise ValueError(f"Refusing to overwrite a conflicting artifact: {path}")
    output.mkdir(parents=True, exist_ok=True)
    for path, contents in artifacts:
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(contents)
    return {"package_path": str(output / filename), "record_path": str(output / recordname), "sha256": record["sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build", help="Build the identity package and public JSON record")
    command.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output), sort_keys=True))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"desos-release build: {error}\n")


if __name__ == "__main__":
    main()
