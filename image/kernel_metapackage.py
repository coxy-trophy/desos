#!/usr/bin/env python3
"""Build the DesOS kernel metapackages without root or signing keys.

The upstream kernel packages are named by version (linux-image-6.18.62-desos),
so apt can never replace one with a newer one. `linux-image-desos` and
`linux-headers-desos` are empty packages that depend on the exact current
versioned packages; publishing a newer pair in the DesOS apt origin is what
makes an installed system pull the next kernel in. The kernel version comes
from kernel/source-pin.json, the same pin the kernel build uses.

  python3 image/kernel_metapackage.py build --output /abs/kernel-meta
  python3 image/kernel_metapackage.py names        # NAME=VERSION per line

A JSON record is written beside each .deb. Nothing is installed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

try:
    from . import debpackage
except ImportError:
    import debpackage

PIN = Path(__file__).resolve().parent.parent / 'kernel/source-pin.json'
# Raise only to change a metapackage's content while the kernel stays the same;
# a new kernel version resets it to 0.
REVISION = 0
KINDS = {
    'image': 'the DesOS build of the unmodified upstream Linux kernel',
    'headers': 'the headers for building modules against the DesOS kernel',
}


def kernel_identity(pin=PIN):
    """{'release', 'package_version', 'meta_version', 'epoch'} from the kernel pin."""
    data = json.loads(Path(pin).read_text(encoding='utf-8'))
    version, local, package = data['version'], data['localversion'], data['package_version']
    if not (isinstance(version, str) and re.fullmatch(r'[0-9]+(\.[0-9]+){1,2}(-rc[0-9]+)?', version)
            and isinstance(local, str) and re.fullmatch(r'-[a-z0-9]+', local)
            and isinstance(package, str) and debpackage.PACKAGE_VERSION.fullmatch(package)
            and package.startswith(version + '-')):
        raise ValueError('Kernel pin has an unexpected version, localversion or package_version')
    epoch = data['source_date_epoch']
    if type(epoch) is not int or not 0 <= epoch <= 253402300799:
        raise ValueError('Kernel pin has an invalid source_date_epoch')
    meta = package + ('+meta%d' % REVISION if REVISION else '')
    return {'release': version + local, 'package_version': package, 'meta_version': meta, 'epoch': epoch}


def names(pin=PIN):
    """The exact apt arguments the image build installs."""
    identity = kernel_identity(pin)
    return ['linux-%s-desos=%s' % (kind, identity['meta_version']) for kind in KINDS]


def build(output, pin=PIN):
    identity = kernel_identity(pin)
    artifacts, result = [], []
    for kind, summary in KINDS.items():
        package = 'linux-%s-desos' % kind
        target = 'linux-%s-%s' % (kind, identity['release'])
        readme = ('%s\n\nThis package contains no kernel. It depends on %s (= %s),\n%s, so that the DesOS '
                  'package origin can deliver a newer\nkernel by publishing a newer %s.\n\n'
                  'Source: image/kernel_metapackage.py and kernel/source-pin.json in the DesOS build source.\n'
                  % (package, target, identity['package_version'], summary, package)).encode()
        payload = {'usr/share/doc/%s/README' % package: (readme, 0o644)}
        fields = [
            ('Package', package), ('Version', identity['meta_version']), ('Architecture', 'amd64'),
            ('Section', 'kernel'), ('Priority', 'optional'), ('Maintainer', debpackage.MAINTAINER),
            ('Depends', '%s (= %s)' % (target, identity['package_version'])),
            ('Description', 'DesOS upstream kernel %s (metapackage)\n Depends on the current %s so that apt '
                            'upgrades follow the DesOS kernel.' % (kind, target)),
        ]
        data = debpackage.build_deb(fields, payload, identity['epoch'])
        stem = '%s_%s_amd64' % (package, identity['meta_version'])
        record = {
            'schema': 1, 'package': package, 'version': identity['meta_version'], 'architecture': 'amd64',
            'filename': stem + '.deb', 'sha256': debpackage.sha256(data), 'size_bytes': len(data),
            'source_date_epoch': identity['epoch'],
            'source': 'image/kernel_metapackage.py and kernel/source-pin.json',
            'depends': {'package': target, 'version': identity['package_version']},
            'kernel_release': identity['release'], 'payload': debpackage.payload_record(payload),
        }
        artifacts += [(stem + '.deb', data), (stem + '.json', (json.dumps(record, indent=2, sort_keys=True) + '\n').encode())]
        result.append({'package': package, 'version': identity['meta_version'], 'sha256': record['sha256']})
    output = debpackage.write_artifacts(output, artifacts)
    return {'output': str(output), 'packages': result,
            'package_paths': [str(output / name) for name, _ in artifacts if name.endswith('.deb')]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('build', help='build both metapackages and their public JSON records')
    command.add_argument('--output', required=True, type=Path)
    commands.add_parser('names', help='print NAME=VERSION for the image package transaction')
    for sub in commands.choices.values():
        sub.add_argument('--pin', type=Path, default=PIN, help='kernel source pin (default: kernel/source-pin.json)')
    args = parser.parse_args(argv)
    try:
        if args.command == 'names':
            print('\n'.join(names(args.pin)))
        else:
            print(json.dumps(build(args.output, args.pin), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print('kernel metapackage build: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
