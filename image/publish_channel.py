#!/usr/bin/env python3
"""Prepare the DesOS network update channel as a directory tree. Offline.

Input is a signed component repository made by image/component_repo.py, and
optionally the tree this tool published last time. Output is a new directory
that can be served as the apt source the image already carries (disabled) in
/etc/apt/sources.list.d/desos-updates.sources: same archive key,
Origin: DesOS, Suite: desos, component main.

  python3 image/publish_channel.py --repository /abs/new-component-repo \\
    [--previous /abs/last-published-tree] --output /abs/new-channel-tree \\
    --gpg-home /abs/dedicated-gnupg --key-fingerprint FULL_PRIMARY_FINGERPRINT

Every input is validated before anything is written: no .incomplete marker,
manifest.json, Release and InRelease signed by exactly the selected key, every
index and every pool package matching the signed manifest, nothing unlisted in
the pool. Unsigned, partly built or inconsistent input is refused. Packages of
the previous tree are kept, so an older version stays installable beside the
new one; a package name and version that appear in both with different bytes
are refused, as is anything the package ledger records with another hash.

The tree is assembled from the verified packages and signed again through the
same dedicated GPG home interface as component_repo.py. Nothing is written
outside --output (GnuPG itself updates files in its own home), the inputs are
not modified, and this tool has no network code: it does not upload. Copying
the tree to a web server is a separate, deliberate operator step.
"""
import argparse
from email.parser import Parser
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

try:
    from . import component_repo as repo
except ImportError:
    import component_repo as repo

RepoError, require = repo.RepoError, repo.require
REQUIRED = ('manifest.json', 'manifest.json.asc', 'desos-archive-keyring.asc', 'dists/desos/Release',
            'dists/desos/Release.gpg', 'dists/desos/InRelease', 'dists/desos/main/binary-amd64/Packages',
            'dists/desos/main/binary-amd64/Packages.gz')
DESCRIPTION = 'DesOS network update channel'


def regular(path, label):
    require(not path.is_symlink() and path.is_file(), '%s is missing or not a regular file: %s' % (label, path))
    return path


def signed_text(home, fingerprint, path):
    """The text inside a clearsigned file, once exactly the selected key's signature on it is valid."""
    done = subprocess.run(['gpg', '--no-options', '--homedir', str(home), '--batch', '--no-tty', '--pinentry-mode', 'error',
                           '--status-fd', '2', '--output', '-', '--decrypt', str(path)],
                          env=dict(os.environ, LC_ALL='C', TZ='UTC'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    status = [line.split() for line in done.stderr.decode('utf-8', 'replace').splitlines() if line.startswith('[GNUPG:] ')]
    valid = [line[2] for line in status if len(line) >= 3 and line[1] == 'VALIDSIG']
    require(done.returncode == 0 and valid == [fingerprint], '%s is not signed by exactly the selected key' % path.name)
    return done.stdout.decode('utf-8', 'replace')


def stanzas(text):
    found = []
    for block in re.split(r'\n\s*\n', text):
        if block.strip():
            found.append({key.lower(): value for key, value in Parser().parsestr(block).items()})
    return found


def verify_repository(directory, home, fingerprint, label='repository'):
    """Validate a signed repository tree completely; return its manifest and
    [(identity, verified .deb path, manifest entry)]. Raises RepoError."""
    directory = Path(directory).absolute()
    require(not directory.is_symlink() and directory.is_dir(), '%s must be an existing directory without symlink: %s' % (label, directory))
    directory = directory.resolve(strict=True)
    require(not os.path.lexists(directory / '.incomplete'), '%s carries an .incomplete marker; it was never finished' % label)
    for name in REQUIRED:
        regular(directory / name, label + ' file ' + name)
    dists = directory / 'dists/desos'
    # Signatures first: nothing below is believed before these hold.
    repo.verify_signature(home, fingerprint, directory / 'manifest.json.asc', directory / 'manifest.json')
    repo.verify_signature(home, fingerprint, dists / 'Release.gpg', dists / 'Release')
    release = (dists / 'Release').read_text()
    inline = signed_text(home, fingerprint, dists / 'InRelease')
    require([line.rstrip() for line in inline.strip().splitlines()] == [line.rstrip() for line in release.strip().splitlines()],
            label + ': the text signed inside InRelease differs from Release')
    shown = repo.gpg_run(home, ['--with-colons', '--show-keys', str(directory / 'desos-archive-keyring.asc')])
    primaries, pending = [], False
    for line in shown.splitlines():
        fields = line.split(':')
        if fields[0] == 'pub':
            pending = True
        elif fields[0] == 'fpr' and pending and len(fields) > 9:
            primaries.append(fields[9])
            pending = False
    require(primaries == [fingerprint], label + ': the exported archive key is not exactly the selected signing key')
    try:
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        require(manifest['schema'] == 'desos-component-repository-v1', label + ': unsupported manifest schema')
        require((manifest['origin'], manifest['suite'], manifest['component']) == ('DesOS', 'desos', 'main'),
                label + ': not a DesOS desos/main repository')
        require(manifest['signing_fingerprint'].upper() == fingerprint, label + ': manifest names another signing key')
        packages, artifacts = manifest['packages'], manifest['artifacts']
        require(isinstance(packages, list) and packages and isinstance(artifacts, dict), label + ': manifest lists no packages')
        for name in ('dists/desos/Release', 'dists/desos/InRelease', 'dists/desos/Release.gpg',
                     'dists/desos/main/binary-amd64/Packages', 'dists/desos/main/binary-amd64/Packages.gz',
                     'desos-archive-keyring.asc'):
            require(name in artifacts, label + ': manifest does not cover ' + name)
        for name, record in artifacts.items():
            require(isinstance(name, str) and not name.startswith('/') and '..' not in name.split('/'),
                    label + ': unsafe artifact path in manifest')
            require(repo.sha256(regular(directory / name, label + ' artifact')) == record['sha256'],
                    '%s: %s differs from the signed manifest' % (label, name))
        index = dists / 'main/binary-amd64/Packages'
        require(gzip.decompress(index.with_name('Packages.gz').read_bytes()) == index.read_bytes(),
                label + ': Packages.gz is not the compressed Packages index')
        for path in (index, index.with_name('Packages.gz')):
            line = ' %s %16d %s' % (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size,
                                    path.relative_to(dists).as_posix())
            require(line in release.splitlines(), '%s: signed Release does not list %s' % (label, path.name))
        listed = {}
        for stanza in stanzas(index.read_text()):
            listed[(stanza.get('package'), stanza.get('version'), stanza.get('architecture'))] = stanza
        verified, seen = [], set()
        for entry in packages:
            identity = (entry['package'], entry['version'], entry['architecture'])
            require(all(isinstance(value, str) for value in identity) and identity not in seen,
                    label + ': manifest package entries are malformed or repeated')
            seen.add(identity)
            pool = 'pool/main/%s/%s/%s_%s_%s.deb' % ((identity[0][:1], identity[0]) + identity)
            require(entry['pool_path'] == pool and re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) is not None,
                    '%s: unexpected pool path or hash for %s' % (label, identity[0]))
            deb = regular(directory / pool, label + ' package')
            require(repo.sha256(deb) == entry['sha256'], '%s: %s differs from the signed manifest' % (label, pool))
            metadata = repo.package_metadata(deb)
            require((metadata['package'], metadata['version'], metadata['architecture']) == identity,
                    '%s: %s is not the package the manifest names' % (label, pool))
            stanza = listed.get(identity)
            require(stanza is not None and stanza.get('filename') == pool and stanza.get('sha256') == entry['sha256'],
                    '%s: Packages index and manifest disagree about %s' % (label, identity[0]))
            verified.append((identity, deb, entry))
        require(set(listed) == seen, label + ': Packages index lists packages the manifest does not')
        present = {path.relative_to(directory).as_posix() for path in (directory / 'pool').rglob('*') if not path.is_dir()}
        require(present == {entry['pool_path'] for entry in packages}, label + ': pool holds files the signed manifest does not list')
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as error:
        raise RepoError('%s is not a complete signed repository: %s: %s' % (label, type(error).__name__, error))
    return manifest, verified


def newer(left, right):
    """True when Debian version `left` is strictly greater than `right`."""
    return subprocess.run(['dpkg', '--compare-versions', left, 'gt', right], check=False).returncode == 0


def publish(repository, output, gpg_home, fingerprint, previous=None, epoch=None, ledger=None):
    home, fingerprint = repo.validate_home(gpg_home, fingerprint)
    require(all(shutil.which(tool) for tool in ('gpg', 'dpkg-deb', 'dpkg')), 'gpg and dpkg are required')
    output = Path(output).absolute()
    require(output.name not in ('', '.', '..') and output.parent.is_dir(), 'explicit new output directory with an existing parent is required')
    output = output.parent.resolve(strict=True) / output.name
    require(not os.path.lexists(output), 'output already exists; refusing overwrite')
    sources = [('repository', Path(repository).absolute())] + ([('previous', Path(previous).absolute())] if previous else [])
    for label, path in sources:
        resolved = path.resolve()
        require(resolved != output and output not in resolved.parents and resolved not in output.parents,
                'output must be separate from the %s tree' % label)
    require(len({path.resolve() for _, path in sources}) == len(sources), 'previous tree and new repository are the same directory')
    merged, origin, manifests = {}, {}, {}
    # The previous tree first, so its packages are the ones "kept".
    for label, path in reversed(sources):
        manifests[label], verified = verify_repository(path, home, fingerprint, label)
        for identity, deb, entry in verified:
            if identity in merged:
                require(merged[identity][1]['sha256'] == entry['sha256'],
                        '%s %s (%s) is in the previous tree with other bytes; a published version is never replaced: '
                        'raise the package version' % identity)
                continue
            merged[identity], origin[identity] = (deb, entry), label
    added = sorted(identity for identity, label in origin.items() if label == 'repository')
    warnings = []
    for identity in added:
        older = [other[1] for other in origin if other[0] == identity[0] and origin[other] == 'previous']
        if any(not newer(identity[1], version) for version in older):
            warnings.append('%s %s is not newer than every published version (%s); apt will not upgrade to it'
                            % (identity[0], identity[1], ', '.join(sorted(older))))
    channel = {'purpose': 'network update channel', 'added': ['%s %s %s' % identity for identity in added],
               'kept': ['%s %s %s' % identity for identity in sorted(origin) if origin[identity] == 'previous'],
               'sources': {label: hashlib.sha256((path / 'manifest.json').read_bytes()).hexdigest() for label, path in sources}}
    result = repo.build_repository([str(deb) for deb, _ in merged.values()], output, home, fingerprint, epoch,
                                   ledger=ledger, record_ledger_entries=False, description=DESCRIPTION,
                                   extra={'channel': channel})
    # The tree just written must itself be a complete signed repository holding
    # exactly the verified packages (and the same per-file lists, where given).
    try:
        _, verified = verify_repository(output, home, fingerprint, 'published tree')
        require({identity: entry['sha256'] for identity, _, entry in verified}
                == {identity: entry['sha256'] for identity, (_, entry) in merged.items()},
                'published tree does not hold exactly the verified packages')
        for identity, _, entry in verified:
            signed_list = merged[identity][1].get('file_list')
            require(signed_list is None or signed_list.get('sha256') == entry['file_list']['sha256'],
                    'per-file list of %s %s differs from the one signed in its source repository' % identity[:2])
    except RepoError:
        (output / '.incomplete').write_text('Channel tree failed re-verification; do not serve.\n')
        raise
    return {'status': 'published-locally', 'output': str(output), 'package_count': result['package_count'],
            'signing_fingerprint': fingerprint, 'manifest': result['manifest'], 'added': channel['added'],
            'kept': channel['kept'], 'warnings': warnings, 'uploaded': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repository', required=True, help='signed component repository built by component_repo.py')
    parser.add_argument('--previous', help='the tree this tool published last time; its packages are kept')
    parser.add_argument('--output', required=True, help='new output directory; parent must exist')
    parser.add_argument('--gpg-home', required=True, help='dedicated existing GPG homedir, mode 0700, owned by current user')
    parser.add_argument('--key-fingerprint', required=True, help='full primary signing key fingerprint; exact key forced')
    parser.add_argument('--source-date-epoch', type=int, help='optional explicit Release timestamp')
    parser.add_argument('--ledger', default=str(repo.LEDGER), help='package ledger to check against (never written here)')
    args = parser.parse_args(argv)
    try:
        result = publish(args.repository, args.output, args.gpg_home, args.key_fingerprint, args.previous,
                         args.source_date_epoch, args.ledger)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RepoError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'failed', 'error': str(error), 'output': str(Path(args.output).absolute())}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
