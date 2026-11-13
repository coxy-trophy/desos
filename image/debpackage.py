"""Deterministic, rootless construction of small DesOS .deb packages.

Shared by the builders that follow image/release_package.py's conventions:
no root, no signing keys, fixed timestamps and ownership, and a public JSON
record written beside the archive. Existing conflicting artifacts are never
overwritten; an identical rebuild is a no-op.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

PACKAGE_NAME = re.compile(r'[a-z0-9][a-z0-9+.-]+')
PACKAGE_VERSION = re.compile(r'[0-9][A-Za-z0-9.+~-]*')
MAINTAINER = 'DesOS Preview Project <desos@localhost>'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_output(output):
    output = Path(output)
    if not output.is_absolute() or output.resolve() != output or len(output.parts) < 3:
        raise ValueError('Output must be an absolute, non-root directory without symlinks')
    if output.parts[1] in {'bin', 'boot', 'dev', 'etc', 'lib', 'lib64', 'proc', 'root', 'run', 'sbin', 'sys', 'usr'} \
            or output.is_relative_to(Path('/var/lib/dpkg')):
        raise ValueError('Refusing a system-directory output destination')
    if output.exists() and not output.is_dir():
        raise ValueError('Output is not a directory')
    if any(parent.is_symlink() for parent in (output, *output.parents)):
        raise ValueError('Output contains a symlink')
    return output


def build_deb(fields, payload, epoch):
    """Return the bytes of a .deb.

    fields: ordered (name, value) control fields; Installed-Size is added.
    payload: {relative path: (bytes, mode)} of regular files, modes 0644/0755.
    """
    names = dict(fields)
    if not PACKAGE_NAME.fullmatch(names.get('Package', '')) or not PACKAGE_VERSION.fullmatch(names.get('Version', '')):
        raise ValueError('Invalid package name or version')
    for name, (data, mode) in payload.items():
        parts = Path(name).parts
        if Path(name).is_absolute() or '..' in parts or not parts or '\n' in name or '\\' in name:
            raise ValueError('Unsafe payload path: %r' % name)
        if mode not in (0o644, 0o755) or not isinstance(data, bytes):
            raise ValueError('Payload files are regular files with mode 0644 or 0755: %s' % name)
    with tempfile.TemporaryDirectory(prefix='desos-deb-build-') as temporary:
        stage = Path(temporary) / 'package'
        stage.mkdir(mode=0o755)
        for name, (data, mode) in payload.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        control = stage / 'DEBIAN'
        control.mkdir(mode=0o755)
        size = (sum(len(data) for data, _ in payload.values()) + 1023) // 1024
        lines = []
        for key, value in fields:
            if key == 'Description':
                lines.append('Installed-Size: %d' % size)
            lines.append('%s: %s' % (key, value))
        (control / 'control').write_text('\n'.join(lines) + '\n')
        (control / 'control').chmod(0o644)
        # md5sums are conventional dpkg metadata; the JSON record carries SHA-256.
        (control / 'md5sums').write_text(''.join(
            '%s  %s\n' % (hashlib.md5(data).hexdigest(), name) for name, (data, _) in sorted(payload.items())))
        (control / 'md5sums').chmod(0o644)
        for path in [stage, *stage.rglob('*')]:
            if path.is_dir():
                path.chmod(0o755)
            os.utime(path, (epoch, epoch))
        package = Path(temporary) / 'package.deb'
        subprocess.run(['/usr/bin/dpkg-deb', '--root-owner-group', '--uniform-compression', '-Zxz', '-z9',
                        '--build', str(stage), str(package)], check=True, capture_output=True,
                       env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8', 'SOURCE_DATE_EPOCH': str(epoch)})
        return package.read_bytes()


def payload_record(payload):
    return [{'path': '/' + name, 'sha256': sha256(data), 'size_bytes': len(data), 'mode': '%04o' % mode,
             'uid': 0, 'gid': 0} for name, (data, mode) in sorted(payload.items())]


def write_artifacts(output, artifacts):
    """Write [(file name, bytes)] under output; identical files are kept,
    conflicting ones refuse before anything is created."""
    output = validate_output(output)
    for name, contents in artifacts:
        path = output / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes() != contents)):
            raise ValueError('Refusing to overwrite a conflicting artifact: %s' % path)
    output.mkdir(parents=True, exist_ok=True)
    for name, contents in artifacts:
        path = output / name
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(contents)
    return output
