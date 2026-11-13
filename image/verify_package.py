#!/usr/bin/env python3
"""Build the desos-verify .deb from verify/rootfs without root or signing keys.

The verifier used to be copied onto the image outside any package, so the
signed manifest it checks did not cover the verifier itself. Packaged, it is
one more entry of the signed component repository: its files are in the
signed per-file list and are re-hashed like every other DesOS package.

  python3 image/verify_package.py build --output /abs/desos-packages

The package has no maintainer scripts; the image build enables its units.
VERSION must be raised whenever anything under verify/rootfs changes: the
repository builder's ledger refuses the same version with different bytes.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

try:
    from . import debpackage
except ImportError:
    import debpackage

PACKAGE = 'desos-verify'
VERSION = '2.1'
EPOCH = 1794441600
TREE = Path(__file__).resolve().parent.parent / 'verify/rootfs'


def collect(tree=TREE):
    """{relative path: (bytes, mode)}; anything but plain files and directories refuses."""
    tree = Path(tree)
    if tree.is_symlink() or not tree.is_dir():
        raise ValueError('verify/rootfs must be a real directory')
    payload = {}
    for current, directories, files in os.walk(tree, followlinks=False):
        directories[:] = sorted(name for name in directories if name != '__pycache__')
        for name in sorted(directories + files):
            path = Path(current) / name
            mode = path.lstat().st_mode
            if name.endswith('.pyc'):
                continue
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode) or path.stat().st_nlink != 1:
                raise ValueError('Only plain files may be packaged: %s' % path.relative_to(tree))
            payload[path.relative_to(tree).as_posix()] = (path.read_bytes(), 0o755 if mode & 0o111 else 0o644)
    if 'usr/bin/desos-verify' not in payload:
        raise ValueError('verify/rootfs holds no usr/bin/desos-verify')
    return payload


def build(output, tree=TREE):
    payload = collect(tree)
    fields = [
        ('Package', PACKAGE), ('Version', VERSION), ('Architecture', 'all'),
        ('Section', 'admin'), ('Priority', 'optional'), ('Maintainer', debpackage.MAINTAINER),
        ('Depends', 'python3, gpgv, dpkg'),
        ('Description', 'check a DesOS system against its signed build evidence\n'
                        ' desos-verify compares the running system with the signed component\n'
                        ' repository manifest and build evidence shipped in every DesOS image.'),
    ]
    data = debpackage.build_deb(fields, payload, EPOCH)
    stem = '%s_%s_all' % (PACKAGE, VERSION)
    record = {
        'schema': 1, 'package': PACKAGE, 'version': VERSION, 'architecture': 'all',
        'filename': stem + '.deb', 'sha256': debpackage.sha256(data), 'size_bytes': len(data),
        'source_date_epoch': EPOCH, 'source': 'image/verify_package.py and verify/rootfs/',
        'payload': debpackage.payload_record(payload),
    }
    output = debpackage.write_artifacts(output, [
        (stem + '.deb', data), (stem + '.json', (json.dumps(record, indent=2, sort_keys=True) + '\n').encode())])
    return {'package_path': str(output / (stem + '.deb')), 'record_path': str(output / (stem + '.json')),
            'sha256': record['sha256']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('build', help='build the package and its public JSON record')
    command.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.output), sort_keys=True))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print('desos-verify package build: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
