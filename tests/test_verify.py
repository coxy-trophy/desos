"""desos-verify against a disposable, really signed fixture system.

A throwaway key is generated in a temporary GNUPGHOME, the real repository
builder signs a tiny repository with it, and the packages are "installed" into
a temp-dir root. Nothing here needs root or touches the user's keyring.
"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).parents[1]
TOOL = REPO_ROOT / 'verify/rootfs/usr/bin/desos-verify'
COMPONENT = REPO_ROOT / 'verify'


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ may appear inside verify/rootfs: that tree is copied onto the image.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


dv = load('desos_verify', TOOL)
component_repo = load('component_repo_for_verify', REPO_ROOT / 'image/component_repo.py')

KERNEL = '6.18.62-desos'
COMPAT = '7.0.0-43-generic'
NEEDED = ('gpg', 'gpgv', 'dpkg-deb')
OS_RELEASE = 'NAME="DesOS"\nVERSION="0.0-test"\nID=desos\nID_LIKE="ubuntu debian"\nPRETTY_NAME="DesOS Test"\nVERSION_ID="0.0"\n'
SECURE_BOOT = 'sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c'
UBUNTU_SOURCES = '''Types: deb
URIs: http://archive.ubuntu.com/ubuntu
Suites: resolute resolute-updates
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu
Suites: resolute-security
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
'''
DESOS_SOURCES = '''Types: deb
URIs: file:/usr/share/desos/repository
Suites: desos
Components: main
Signed-By: /usr/share/keyrings/desos-archive-keyring.asc
'''
PACKAGES = {
    'desos-release': ('0.0~test1', 'all', {'usr/lib/os-release': (OS_RELEASE, 0o644),
                                           'etc/lsb-release': ('DISTRIB_ID=DesOS\n', 0o644)}, []),
    'linux-image-' + KERNEL: ('6.18.62-1desos1', 'amd64', {
        'boot/vmlinuz-' + KERNEL: ('fixture kernel image\n' * 50, 0o644),
        'boot/config-' + KERNEL: ('CONFIG_FIXTURE=y\n', 0o644),
        'usr/lib/modules/%s/kernel/fixture.ko' % KERNEL: ('module\n', 0o644)}, []),
    'des-ui': ('0.1.1', 'amd64', {'usr/bin/des-ui': ('#!/bin/sh\necho desktop\n', 0o755),
                                  'usr/share/des-ui/.hidden': ('dot file\n', 0o644),
                                  'usr/share/doc/des-ui/changelog': ('docs\n', 0o644),
                                  'etc/des-ui/config.toml': ('theme = "default"\n', 0o644)},
               ['/etc/des-ui/config.toml']),
}


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def gpg(home, *arguments):
    return subprocess.run(['gpg', '--no-options', '--homedir', str(home), '--batch', '--no-tty', *arguments],
                          check=True, capture_output=True, text=True).stdout


def make_key(home):
    home.mkdir(mode=0o700)
    gpg(home, '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key',
        'DesOS Verify Test <test@example.invalid>', 'ed25519', 'sign', 'never')
    listing = gpg(home, '--with-colons', '--list-secret-keys')
    return next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))


def build_deb(work, name, version, arch, files, conffiles):
    tree = work / (name + '-tree')
    (tree / 'DEBIAN').mkdir(parents=True)
    (tree / 'DEBIAN/control').write_text(
        'Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: DesOS Test <test@example.invalid>\n'
        'Description: verification fixture\n' % (name, version, arch))
    if conffiles:
        (tree / 'DEBIAN/conffiles').write_text(''.join(c + '\n' for c in conffiles))
    for relative, (content, mode) in files.items():
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
    if name == 'des-ui':
        os.symlink('des-ui', tree / 'usr/bin/des-ui-session')
    output = work / ('%s.deb' % name)
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(tree), str(output)], check=True, capture_output=True)
    return output


def evidence_files(debs):
    """The build evidence as the kernel build and image recipe would supply it."""
    by_name = {deb.stem: deb for deb in debs}
    image = by_name['linux-image-' + KERNEL]
    signer = 'F' * 40
    def pool_name(name):
        version, arch = PACKAGES[name][0], PACKAGES[name][1]
        return '%s_%s_%s.deb' % (name, version, arch)
    return {
        'kernel-provenance.json': json.dumps({
            'kernel_release': KERNEL, 'package_version': PACKAGES['linux-image-' + KERNEL][0], 'version': '6.18.62',
            'config_sha256': sha('CONFIG_FIXTURE=y\n'), 'source_archive_sha256': 'a' * 64, 'source_modified': False,
            'signer_primary_fingerprint': signer,
            'packages': {pool_name('linux-image-' + KERNEL): sha(image.read_bytes())}}),
        'source-pin.json': json.dumps({'archive_sha256': 'a' * 64, 'version': '6.18.62',
                                       'maintainers': [{'fingerprint': signer}]}),
        'signature-status.txt': '[GNUPG:] GOODSIG X test\n[GNUPG:] VALIDSIG %s 2026-11-06 1 0 4 0 1 10 00 %s\n' % (signer, signer),
        'desktop-pin.json': json.dumps({'packages': [{'file': pool_name('des-ui'), 'sha256': sha(by_name['des-ui'].read_bytes())}]}),
        'config-' + KERNEL: 'CONFIG_FIXTURE=y\n',
    }


def resign(home, repository, change):
    """Rewrite a repository's manifest and sign it again, as the key holder could."""
    path = repository / 'manifest.json'
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    (repository / 'manifest.json.asc').unlink()
    gpg(home, '--armor', '--output', str(repository / 'manifest.json.asc'), '--detach-sign', str(path))


def strip_new_evidence(manifest):
    for entry in manifest['packages']:
        del entry['file_list']
    del manifest['build_evidence']
    manifest['artifacts'] = {name: record for name, record in manifest['artifacts'].items()
                             if not name.startswith(('file-lists/', 'build-evidence/'))}


class Fixture:
    """Built once: two keys, the debs, a repository signed by each key, and the
    first repository as an image before 0.2 had it: no per-file lists, no signed
    build evidence."""
    ready = None

    @classmethod
    def get(cls):
        if cls.ready is None:
            cls.ready = cls()
        return cls.ready

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix='desos-verify-fixture-')
        work = Path(self.tmp)
        self.homes = [work / 'gpg-a', work / 'gpg-b']
        self.fingerprints = [make_key(home) for home in self.homes]
        self.debs = [build_deb(work, name, *spec) for name, spec in PACKAGES.items()]
        self.evidence = evidence_files(self.debs)
        (work / 'evidence').mkdir()
        for name, content in self.evidence.items():
            (work / 'evidence' / name).write_text(content)
        self.repos = []
        for index, home in enumerate(self.homes):
            output = work / ('repository-%d' % index)
            component_repo.build_repository([str(d) for d in self.debs], output, home, self.fingerprints[index],
                                            epoch=1794268800, evidence=sorted((work / 'evidence').iterdir()))
            self.repos.append(output)
        self.legacy = work / 'repository-legacy'
        shutil.copytree(self.repos[0], self.legacy)
        shutil.rmtree(self.legacy / 'file-lists')
        shutil.rmtree(self.legacy / 'build-evidence')
        resign(self.homes[0], self.legacy, strip_new_evidence)

    def close(self):
        for home in self.homes:
            subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'all'], capture_output=True, check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)


def tearDownModule():
    if Fixture.ready is not None:
        Fixture.ready.close()


@unittest.skipUnless(all(shutil.which(tool) for tool in NEEDED) and
                     (shutil.which('apt-ftparchive') or shutil.which('dpkg-scanpackages')),
                     'gpg, gpgv, dpkg-deb and an apt index tool are needed for the signed fixture')
class VerifyCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture.get()
        self.tmp = tempfile.TemporaryDirectory(prefix='desos-verify-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.scratch = Path(self.tmp.name) / 'scratch'
        self.scratch.mkdir()
        self.install(self.repository())

    def repository(self):
        return self.fixture.repos[0]

    # --- fixture system -----------------------------------------------------

    def put(self, relative, content, mode=0o644):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        path.chmod(mode)
        return path

    def install(self, repository):
        root = self.root
        shutil.copytree(repository, root / 'usr/share/desos/repository')
        self.put('usr/share/keyrings/desos-archive-keyring.asc', (repository / 'desos-archive-keyring.asc').read_text())
        manifest = json.loads((repository / 'manifest.json').read_text())
        status = []
        for entry in manifest['packages']:
            deb = repository / entry['pool_path']
            subprocess.run(['dpkg-deb', '-x', str(deb), str(root)], check=True, capture_output=True)
            name = entry['package']
            files, conffiles = PACKAGES[name][2], PACKAGES[name][3]
            stanza = 'Package: %s\nStatus: install ok installed\nArchitecture: %s\nVersion: %s\n' % (
                name, entry['architecture'], entry['version'])
            if conffiles:
                stanza += 'Conffiles:\n' + ''.join(' %s %s\n' % (c, hashlib.md5(
                    files[c[1:]][0].encode()).hexdigest()) for c in conffiles)
            status.append(stanza)
            self.put('var/lib/dpkg/info/%s.md5sums' % name, ''.join(
                '%s  %s\n' % (hashlib.md5(content.encode()).hexdigest(), relative)
                for relative, (content, _) in sorted(files.items())))
        status.append('Package: linux-image-%s\nStatus: install ok installed\nArchitecture: amd64\nVersion: 7.0.0-43.43\n' % COMPAT)
        self.put('boot/vmlinuz-' + COMPAT, 'ubuntu kernel\n')
        self.put('var/lib/dpkg/info/linux-image-%s.md5sums' % COMPAT,
                 '%s  boot/vmlinuz-%s\n' % (hashlib.md5(b'ubuntu kernel\n').hexdigest(), COMPAT))
        self.put('var/lib/dpkg/status', '\n'.join(status))
        self.put('var/lib/dpkg/diversions', '/usr/lib/os-release\n/usr/lib/os-release.ubuntu\ndesos-release\n')
        (root / 'etc').mkdir(exist_ok=True)
        os.symlink('../usr/lib/os-release', root / 'etc/os-release')
        self.install_evidence(repository)
        self.put('etc/apt/sources.list', '')
        self.put('etc/apt/sources.list.d/ubuntu.sources', UBUNTU_SOURCES)
        self.put('etc/apt/sources.list.d/desos.sources', DESOS_SOURCES)
        self.put('proc/sys/kernel/osrelease', KERNEL + '\n')
        self.put('proc/sys/kernel/random/boot_id', '11111111-2222-3333-4444-555555555555\n')
        self.put('proc/cmdline', 'BOOT_IMAGE=/boot/vmlinuz-%s root=UUID=x ro quiet\n' % KERNEL)
        self.put('proc/mounts', 'proc /proc proc rw 0 0\n/dev/vda2 / ext4 rw,relatime 0 0\n')
        self.put(SECURE_BOOT, b'\x06\x00\x00\x00\x00')

    def install_evidence(self, repository):
        """As image/finalize-rootfs.sh: the signed copies when the repository has them."""
        signed = repository / 'build-evidence'
        if signed.is_dir():
            for path in signed.iterdir():
                self.put('usr/share/doc/desos/build/' + path.name, path.read_bytes())
        else:
            for name, content in self.fixture.evidence.items():
                self.put('usr/share/doc/desos/build/' + name, content)

    def resign_installed(self, change):
        resign(self.fixture.homes[0], self.root / 'usr/share/desos/repository', change)

    def config(self, **options):
        options.setdefault('offline', False)
        return dv.Config(root=self.root, tmpdir=self.scratch, **options)

    def run_check(self, full=True, **options):
        result = dv.verify(self.config(**options), full=full)
        self.by_id = {c['id']: c for c in result['checks']}
        return result

    def status(self, identifier):
        return self.by_id[identifier]['status']

    def reason(self, identifier):
        return self.by_id[identifier]['reason']

    def resign_with_second_key(self):
        shutil.rmtree(self.root / 'usr/share/desos/repository')
        (self.root / 'usr/share/keyrings/desos-archive-keyring.asc').unlink()
        repository = self.fixture.repos[1]
        shutil.copytree(repository, self.root / 'usr/share/desos/repository')
        self.put('usr/share/keyrings/desos-archive-keyring.asc', (repository / 'desos-archive-keyring.asc').read_text())
        self.install_evidence(repository)


class PristineSystemTests(VerifyCase):
    def test_full_check_passes_every_check_and_reports_the_key_fingerprint(self):
        result = self.run_check(full=True)
        self.assertEqual({c['id']: c['status'] for c in result['checks'] if c['status'] != 'pass'}, {})
        self.assertEqual(len(result['checks']), 17)
        self.assertEqual(result['overall'], 'pass')
        self.assertEqual(result['skipped'], ['packages.key-files'])
        self.assertEqual(result['tool_version'], 2)
        self.assertEqual(self.by_id['kernel.image']['details']['evidence'], 'package')
        self.assertTrue(self.by_id['kernel.provenance']['details']['evidence_signed'])
        self.assertEqual(result['key_fingerprint'], self.fixture.fingerprints[0])
        self.assertEqual(result['schema'], 'desos-verify-v1')
        self.assertEqual(result['state'], {'kernel': 'desos', 'kernel_release': KERNEL, 'secure_boot': 'disabled',
                                           'root_filesystem': 'ext4', 'session': 'installed'})
        self.assertEqual(self.by_id['kernel.image']['details']['method'], 'signed-sha256')
        self.assertEqual(self.by_id['kernel.config']['details']['method'], 'signed-sha256')
        files = self.by_id['packages.files']['details']
        self.assertEqual(files['files'], 10)  # nine regular files and one symlink
        self.assertEqual(list(self.scratch.iterdir()), [], 'temporary keyring must be removed')

    def test_quick_check_skips_the_expensive_checks_and_says_so(self):
        result = self.run_check(full=False)
        self.assertEqual(result['overall'], 'pass')
        self.assertEqual(result['skipped'], ['repository.pool', 'packages.files'])
        self.assertEqual(len(result['checks']), 16)
        self.assertNotIn('packages.files', self.by_id)
        # With the signed per-file lists the quick check no longer leans on dpkg's MD5.
        for identifier in ('kernel.image', 'kernel.config'):
            self.assertEqual(self.by_id[identifier]['details']['method'], 'signed-sha256')
            self.assertEqual(self.by_id[identifier]['details']['evidence'], 'file-list')
        keys = self.by_id['packages.key-files']['details']
        self.assertEqual((keys['files'], keys['packages_without_list'], keys['not_hashed_over_budget']), (1, [], 0))
        self.assertEqual(self.by_id['signatures.evidence']['details']['files'], 5)

    def test_quick_check_runs_no_dpkg_deb(self):
        class NoDeb(dv.Runner):
            def stream(inner, argv):
                raise AssertionError('quick check unpacked a package: %r' % argv)
        self.assertEqual(self.run_check(full=False, runner=NoDeb())['overall'], 'pass')

    def test_every_check_has_the_documented_shape(self):
        result = self.run_check(full=True)
        for key in ('schema', 'tool_version', 'mode', 'overall', 'counts', 'started', 'finished', 'duration_seconds',
                    'root', 'offline', 'boot_id', 'key_fingerprint', 'key_pinned', 'manifest_sha256', 'state',
                    'checks', 'skipped', 'last_full', 'assurance', 'summary'):
            self.assertIn(key, result)
        for item in result['checks']:
            self.assertEqual(sorted(item), ['details', 'group', 'id', 'reason', 'status'])
            self.assertIn(item['status'], ('pass', 'fail', 'unknown'))
            self.assertNotIn('\n', item['reason'])
            self.assertEqual(item['group'], item['id'].split('.')[0])
        self.assertEqual(sum(result['counts'].values()), len(result['checks']))
        json.dumps(result)


class SignatureTests(VerifyCase):
    def test_only_an_isolated_temporary_keyring_is_used(self):
        calls = []

        class Spy(dv.Runner):
            def run(inner, argv, env=None, timeout=120):
                calls.append((list(argv), dict(env or {})))
                return super().run(argv, env=env, timeout=timeout)
        home = Path(self.tmp.name) / 'user-home'
        home.mkdir()
        old = os.environ.get('HOME'), os.environ.get('GNUPGHOME')
        os.environ['HOME'] = str(home)
        os.environ.pop('GNUPGHOME', None)
        try:
            result = self.run_check(full=False, runner=Spy())
        finally:
            for name, value in zip(('HOME', 'GNUPGHOME'), old):
                if value is not None:
                    os.environ[name] = value
        self.assertEqual(result['overall'], 'pass')
        self.assertEqual(len(calls), 4)   # manifest, Release.gpg, InRelease, SHA256SUMS
        for argv, env in calls:
            self.assertEqual(argv[0], 'gpgv')
            homedir, keyring = Path(argv[argv.index('--homedir') + 1]), Path(argv[argv.index('--keyring') + 1])
            self.assertEqual(homedir.parent, self.scratch)
            self.assertEqual(keyring.parent, homedir)
            self.assertEqual(env['GNUPGHOME'], str(homedir))
            self.assertEqual(env['HOME'], str(homedir))
        self.assertEqual(list(home.iterdir()), [], 'the user keyring location must stay untouched')

    def test_tampered_manifest_fails_and_nothing_is_derived_from_it(self):
        manifest = self.root / 'usr/share/desos/repository/manifest.json'
        manifest.write_text(manifest.read_text().replace('0.1.1', '0.1.2'))
        result = self.run_check(full=True)
        self.assertEqual(self.status('signatures.manifest'), 'fail')
        self.assertEqual(result['overall'], 'fail')
        self.assertIsNone(result['manifest_sha256'])
        for dependent in ('repository.index', 'repository.pool', 'packages.installed', 'packages.files',
                          'kernel.provenance', 'kernel.image', 'kernel.config', 'identity.os-release'):
            self.assertEqual(self.status(dependent), 'unknown', dependent)

    def test_foreign_key_does_not_verify_the_shipped_signatures(self):
        self.put('usr/share/keyrings/desos-archive-keyring.asc',
                 (self.fixture.repos[1] / 'desos-archive-keyring.asc').read_text())
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.key'), 'pass')
        self.assertEqual(self.status('signatures.manifest'), 'fail')
        self.assertEqual(self.status('signatures.release'), 'fail')

    def test_keyring_with_a_second_key_is_refused(self):
        key = self.root / 'usr/share/keyrings/desos-archive-keyring.asc'
        both = dv.dearmor(key.read_text()) + dv.dearmor((self.fixture.repos[1] / 'desos-archive-keyring.asc').read_text())
        self.assertEqual(len(dv.primary_fingerprints(both)), 2)
        key.write_text(key.read_text() + (self.fixture.repos[1] / 'desos-archive-keyring.asc').read_text())
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.key'), 'fail')
        self.assertEqual(self.status('signatures.manifest'), 'unknown')

    def test_resigned_system_passes_unpinned_but_fails_against_an_outside_fingerprint(self):
        """The documented gap: whoever can replace the key and re-sign passes the self-check."""
        self.resign_with_second_key()
        self.assertEqual(self.run_check(full=True)['overall'], 'pass')
        result = self.run_check(full=True, expect_fingerprint=self.fixture.fingerprints[0])
        self.assertEqual(self.status('signatures.key'), 'fail')
        self.assertIn(self.fixture.fingerprints[1], self.reason('signatures.key'))
        self.assertEqual(result['overall'], 'fail')
        self.assertTrue(result['key_pinned'])
        self.assertNotIn('pass', [self.status(i) for i in ('signatures.manifest', 'packages.files', 'kernel.image')])

    def test_outside_key_file_is_used_instead_of_the_shipped_one(self):
        outside = Path(self.tmp.name) / 'trusted.asc'
        outside.write_text((self.fixture.repos[0] / 'desos-archive-keyring.asc').read_text())
        self.resign_with_second_key()
        self.run_check(full=False, key_file=outside)
        self.assertEqual(self.status('signatures.manifest'), 'fail')

    def test_modified_release_fails(self):
        release = self.root / 'usr/share/desos/repository/dists/desos/Release'
        release.write_text(release.read_text().replace('Suite: desos', 'Suite: other'))
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.release'), 'fail')
        self.assertEqual(self.status('repository.index'), 'fail')

    def test_inrelease_signing_other_text_than_release_fails(self):
        dists = self.root / 'usr/share/desos/repository/dists/desos'
        other = Path(self.tmp.name) / 'other-release'
        other.write_text((dists / 'Release').read_text().replace('Label: DesOS', 'Label: Else'))
        (dists / 'InRelease').unlink()
        gpg(self.fixture.homes[0], '--armor', '--output', str(dists / 'InRelease'), '--clearsign', str(other))
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.release'), 'fail')
        self.assertIn('differs from Release', self.reason('signatures.release'))

    def test_missing_gpgv_is_unknown_not_a_failure(self):
        class NoGpgv(dv.Runner):
            def run(inner, argv, env=None, timeout=120):
                return 127, b'', b'command not found'
        result = self.run_check(full=False, runner=NoGpgv())
        self.assertEqual(self.status('signatures.manifest'), 'unknown')
        self.assertEqual(self.status('signatures.release'), 'unknown')
        self.assertEqual(result['overall'], 'unknown')

    def test_incomplete_repository_marker_fails(self):
        self.put('usr/share/desos/repository/.incomplete', 'in progress\n')
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.manifest'), 'fail')


class PackageTests(VerifyCase):
    def test_modified_installed_file_fails_only_the_full_check(self):
        self.put('usr/bin/des-ui', '#!/bin/sh\necho backdoor\n', 0o755)
        result = self.run_check(full=True)
        self.assertEqual(self.status('packages.files'), 'fail')
        self.assertIn('/usr/bin/des-ui', self.reason('packages.files'))
        self.assertEqual(self.by_id['packages.files']['details']['packages']['des-ui']['modified'], ['/usr/bin/des-ui'])
        self.assertEqual(result['overall'], 'fail')
        self.assertNotIn('packages.key-files', self.by_id)
        # A program is among the files the quick check re-hashes from the signed list.
        self.run_check(full=False)
        self.assertEqual(self.status('packages.key-files'), 'fail')
        self.assertIn('des-ui: modified /usr/bin/des-ui', self.reason('packages.key-files'))

    def test_quick_check_does_not_hash_files_that_are_not_programs_or_units(self):
        self.put('usr/share/des-ui/.hidden', 'changed\n')
        self.assertEqual(self.run_check(full=False)['overall'], 'pass', 'documented: only the full check hashes every file')
        self.assertEqual(self.run_check(full=True)['overall'], 'fail')

    def test_missing_program_and_altered_file_list_fail_the_quick_check(self):
        (self.root / 'usr/bin/des-ui').unlink()
        self.run_check(full=False)
        self.assertIn('missing /usr/bin/des-ui', self.reason('packages.key-files'))
        self.put('usr/bin/des-ui', '#!/bin/sh\necho backdoor\n', 0o755)
        listing = next((self.root / 'usr/share/desos/repository/file-lists').glob('des-ui_*.sha256'))
        listing.write_text(listing.read_text().replace(sha('#!/bin/sh\necho desktop\n'), sha('#!/bin/sh\necho backdoor\n')))
        self.run_check(full=False)
        self.assertEqual(self.status('packages.key-files'), 'fail')
        self.assertIn('differs from the signed manifest', self.reason('packages.key-files'))
        self.assertEqual(self.status('repository.index'), 'fail')

    def test_key_file_check_leaves_other_versions_to_the_installed_check(self):
        status = self.root / 'var/lib/dpkg/status'
        status.write_text(status.read_text().replace('Version: 0.1.1', 'Version: 0.1.0'))
        self.put('usr/bin/des-ui', 'the 0.1.0 program\n', 0o755)
        self.run_check(full=False)
        self.assertEqual(self.status('packages.installed'), 'fail')
        self.assertEqual(self.by_id['packages.key-files']['details']['other_version_installed'], ['des-ui'])
        self.assertNotEqual(self.status('packages.key-files'), 'fail')

    def test_key_files_beyond_the_quick_budget_are_unknown_not_passed(self):
        old, dv.QUICK_FILE_BUDGET = dv.QUICK_FILE_BUDGET, 4
        self.addCleanup(setattr, dv, 'QUICK_FILE_BUDGET', old)
        self.run_check(full=False)
        self.assertEqual(self.status('packages.key-files'), 'unknown')
        self.assertEqual(self.by_id['packages.key-files']['details']['not_hashed_over_budget'], 1)

    def test_full_check_fails_a_signed_file_list_that_does_not_describe_its_package(self):
        """Both are signed, so this is a builder defect rather than tampering; it must not pass."""
        repository = self.root / 'usr/share/desos/repository'
        listing = next((repository / 'file-lists').glob('des-ui_*.sha256'))
        listing.write_text(listing.read_text().replace(sha('dot file\n'), 'c' * 64))

        def change(manifest):
            relative = listing.relative_to(repository).as_posix()
            entry = next(e for e in manifest['packages'] if e['package'] == 'des-ui')
            entry['file_list']['sha256'] = manifest['artifacts'][relative]['sha256'] = sha(listing.read_bytes())
        self.resign_installed(change)
        self.run_check(full=True)
        self.assertEqual(self.status('repository.index'), 'pass')
        self.assertEqual(self.status('packages.files'), 'fail')
        self.assertIn('does not describe the signed package', self.reason('packages.files'))

    def test_same_size_change_is_caught_by_content_hash(self):
        self.put('usr/share/des-ui/.hidden', 'dot fild\n')
        self.run_check(full=True)
        self.assertIn('/usr/share/des-ui/.hidden', self.reason('packages.files'))

    def test_setuid_bit_missing_file_and_replaced_symlink_fail(self):
        (self.root / 'usr/bin/des-ui').chmod(0o4755)
        (self.root / 'usr/share/doc/des-ui/changelog').unlink()
        (self.root / 'usr/bin/des-ui-session').unlink()
        os.symlink('/bin/sh', self.root / 'usr/bin/des-ui-session')
        self.run_check(full=True)
        report = self.by_id['packages.files']['details']['packages']['des-ui']
        self.assertEqual(self.status('packages.files'), 'fail')
        self.assertEqual(len(report['mode_changed']), 1)
        self.assertEqual(report['missing'], ['/usr/share/doc/des-ui/changelog'])
        self.assertEqual(report['modified'], ['/usr/bin/des-ui-session'])

    def test_file_replaced_by_symlink_to_identical_content_fails(self):
        target = self.root / 'usr/bin/des-ui'
        self.put('opt/copy', target.read_text(), 0o755)
        target.unlink()
        os.symlink('/opt/copy', target)
        self.run_check(full=True)
        self.assertEqual(self.status('packages.files'), 'fail')

    def test_edited_conffile_is_reported_but_not_a_failure(self):
        self.put('etc/des-ui/config.toml', 'theme = "mine"\n')
        self.run_check(full=True)
        self.assertEqual(self.status('packages.files'), 'pass')
        self.assertIn('1 locally changed configuration file', self.reason('packages.files'))
        self.assertEqual(self.by_id['packages.files']['details']['packages']['des-ui']['conffiles_changed'],
                         ['/etc/des-ui/config.toml'])

    def test_dpkg_path_exclude_explains_absent_files(self):
        (self.root / 'usr/share/doc/des-ui/changelog').unlink()
        self.put('etc/dpkg/dpkg.cfg.d/excludes', 'path-exclude=/usr/share/doc/*\n')
        self.run_check(full=True)
        self.assertEqual(self.status('packages.files'), 'pass')
        self.assertEqual(self.by_id['packages.files']['details']['packages']['des-ui']['excluded'], 1)

    def test_tampered_pool_package_fails_pool_and_files(self):
        deb = next((self.root / 'usr/share/desos/repository/pool').rglob('des-ui_*.deb'))
        deb.write_bytes(deb.read_bytes() + b'\n')
        self.run_check(full=True)
        self.assertEqual(self.status('repository.pool'), 'fail')
        self.assertEqual(self.status('packages.files'), 'fail')
        self.assertNotIn('des-ui', self.reason('repository.index'))

    def test_wrong_version_and_removed_package_fail(self):
        status = self.root / 'var/lib/dpkg/status'
        text = status.read_text().replace('Version: 0.1.1', 'Version: 0.1.0')
        text = text.replace('Package: desos-release\nStatus: install ok installed', 'Package: desos-release\nStatus: deinstall ok config-files')
        status.write_text(text)
        self.run_check(full=False)
        self.assertEqual(self.status('packages.installed'), 'fail')
        problems = ' '.join(self.by_id['packages.installed']['details']['problems'])
        self.assertIn('des-ui is at 0.1.0, manifest has 0.1.1', problems)
        self.assertIn('desos-release is not installed', problems)

    def upgrade(self, package, version, files):
        """As apt would: a newer version unpacked, its dpkg status and md5sums rewritten."""
        status = self.root / 'var/lib/dpkg/status'
        old = PACKAGES[package][0]
        status.write_text(status.read_text().replace('Package: %s\nStatus: install ok installed\nArchitecture: %s\nVersion: %s\n'
                                                     % (package, PACKAGES[package][1], old),
                                                     'Package: %s\nStatus: install ok installed\nArchitecture: %s\nVersion: %s\n'
                                                     % (package, PACKAGES[package][1], version)))
        for relative, (content, mode) in files.items():
            self.put(relative, content, mode)
        self.put('var/lib/dpkg/info/%s.md5sums' % package, ''.join(
            '%s  %s\n' % (hashlib.md5(content.encode()).hexdigest(), relative)
            for relative, (content, _) in sorted(files.items())))

    def test_newer_package_from_an_update_passes_and_is_listed(self):
        files = dict(PACKAGES['des-ui'][2])
        files['usr/bin/des-ui'] = ('#!/bin/sh\necho desktop 0.2\n', 0o755)
        self.upgrade('des-ui', '0.2.0+dev20261114.1', files)
        for full in (False, True):
            result = self.run_check(full=full)
            self.assertEqual(self.status('packages.installed'), 'pass', full)
            self.assertEqual(self.by_id['packages.installed']['details']['upgraded'],
                             [{'package': 'des-ui', 'shipped': '0.1.1', 'installed': '0.2.0+dev20261114.1'}])
            self.assertIn('upgraded', self.reason('packages.installed'))
            files_check = 'packages.files' if full else 'packages.key-files'
            self.assertEqual(self.status(files_check), 'pass', (full, self.reason(files_check)))
            self.assertEqual(result['overall'], 'pass', (full, {c['id']: c['reason'] for c in result['checks']
                                                                if c['status'] != 'pass'}))
        self.assertEqual(self.by_id['packages.files']['details']['packages']['des-ui']['upgraded_to'], '0.2.0+dev20261114.1')

    def test_damage_to_an_upgraded_package_still_fails(self):
        files = dict(PACKAGES['des-ui'][2])
        files['usr/bin/des-ui'] = ('#!/bin/sh\necho desktop 0.2\n', 0o755)
        self.upgrade('des-ui', '0.2.0', files)
        self.put('usr/bin/des-ui', '#!/bin/sh\necho broken\n', 0o755)
        self.run_check(full=False)
        self.assertEqual(self.status('packages.installed'), 'pass')
        self.assertEqual(self.status('packages.key-files'), 'fail')
        self.assertIn('des-ui 0.2.0: modified /usr/bin/des-ui', self.reason('packages.key-files'))
        self.run_check(full=True)
        self.assertEqual(self.status('packages.files'), 'fail')

    def test_damage_to_a_package_that_was_not_upgraded_still_fails(self):
        files = dict(PACKAGES['des-ui'][2])
        self.upgrade('des-ui', '0.2.0', files)
        image = 'linux-image-' + KERNEL
        self.put('boot/config-' + KERNEL, 'CONFIG_FIXTURE=n\n')
        self.run_check(full=True)
        self.assertEqual(self.status('packages.installed'), 'pass')
        self.assertEqual(self.status('packages.files'), 'fail')
        self.assertIn(image, self.reason('packages.files'))

    def test_older_package_still_fails(self):
        self.upgrade('des-ui', '0.1.0', dict(PACKAGES['des-ui'][2]))
        self.run_check(full=False)
        self.assertEqual(self.status('packages.installed'), 'fail')
        self.assertIn('des-ui is at 0.1.0', self.reason('packages.installed'))

    def test_upgraded_identity_is_checked_against_dpkg(self):
        new = OS_RELEASE.replace('PRETTY_NAME="DesOS Test"', 'PRETTY_NAME="DesOS Test 2"') + 'LOGO=des-ui\n'
        files = dict(PACKAGES['desos-release'][2])
        files['usr/lib/os-release'] = (new, 0o644)
        self.upgrade('desos-release', '0.0~test1+1', files)
        self.run_check(full=False)
        self.assertEqual(self.status('identity.os-release'), 'pass', self.reason('identity.os-release'))
        self.assertIn('upgraded from 0.0~test1', self.reason('identity.os-release'))
        self.put('usr/lib/os-release', new + 'HOME_URL="https://example.invalid"\n')
        self.run_check(full=False)
        self.assertEqual(self.status('identity.os-release'), 'fail')

    def test_debian_version_order(self):
        cmp = dv.compare_versions
        for older, newer in [('0.1.1', '0.2.0'), ('0.2.0', '0.2.0+dev1'), ('0.2.0~rc1', '0.2.0'), ('0.1.0~preview1', '0.1.0~preview1+1'),
                             ('1.0', '1:0.1'), ('6.18.62-1desos1', '6.18.62-1desos2'), ('1.9', '1.10'), ('1.0a', '1.0b'),
                             ('1.0', '1.0a'), ('2.1', '2.2'), ('0.2.0+dev20261114.0930', '0.2.0+dev20261114.1015')]:
            self.assertEqual(cmp(older, newer), -1, (older, newer))
            self.assertEqual(cmp(newer, older), 1, (older, newer))
        self.assertEqual(cmp('1.0-1', '1.0-1'), 0)
        self.assertEqual(cmp('0:1.0', '1.0'), 0)

    def test_removing_an_optional_package_is_not_an_integrity_failure(self):
        status = self.root / 'var/lib/dpkg/status'
        names = [entry['package'] for entry in json.loads(
            (self.root / 'usr/share/desos/repository/manifest.json').read_text())['packages']]
        optional = [name for name in names if name not in ('des-ui', 'desos-release')]
        self.assertTrue(optional, 'fixture needs an optional package')
        text = status.read_text().replace('Package: %s\nStatus: install ok installed' % optional[0],
                                          'Package: %s\nStatus: deinstall ok config-files' % optional[0])
        status.write_text(text)
        self.run_check(full=True)
        self.assertEqual(self.status('packages.installed'), 'pass')
        self.assertEqual(self.by_id['packages.installed']['details']['removed'], [optional[0]])
        self.assertEqual(self.status('packages.files'), 'pass')

    def test_unreadable_file_is_unknown(self):
        if os.geteuid() == 0:
            self.skipTest('root reads everything')
        (self.root / 'usr/bin/des-ui').chmod(0)
        self.addCleanup((self.root / 'usr/bin/des-ui').chmod, 0o755)
        self.run_check(full=True)
        self.assertEqual(self.status('packages.files'), 'unknown')


class KernelTests(VerifyCase):
    def boot_compat(self):
        self.put('proc/sys/kernel/osrelease', COMPAT + '\n')
        self.put('proc/cmdline', 'BOOT_IMAGE=/boot/vmlinuz-%s root=UUID=x ro\n' % COMPAT)

    def test_compatibility_kernel_is_reported_clearly_and_is_not_a_failure(self):
        self.boot_compat()
        result = self.run_check(full=True)
        self.assertEqual(self.status('kernel.release'), 'unknown')
        self.assertIn('Ubuntu compatibility kernel ' + COMPAT, self.reason('kernel.release'))
        self.assertEqual(self.by_id['kernel.release']['details']['flavour'], 'ubuntu-compatibility')
        self.assertEqual(self.status('kernel.image'), 'unknown')
        self.assertEqual(self.status('kernel.config'), 'unknown')
        self.assertEqual(self.status('kernel.provenance'), 'pass')
        self.assertEqual(result['counts']['fail'], 0)
        self.put('proc/mounts', '/dev/vda2 / btrfs rw,subvol=/@ 0 0\n')
        self.run_check(full=False)
        self.assertEqual(self.status('state.root-filesystem'), 'pass')
        self.assertEqual(result['overall'], 'unknown')
        self.assertEqual(result['state']['kernel'], 'ubuntu-compatibility')
        self.assertIn('compatibility kernel', result['summary'])
        self.assertNotIn('FAILED', result['summary'])
        self.assertEqual(dv.main(['--quick', '--quiet'], config=self.config()), 0)
        self.assertEqual(dv.main(['--quick', '--quiet', '--strict'], config=self.config()), 3)

    def test_modified_compatibility_kernel_image_fails(self):
        self.boot_compat()
        self.put('boot/vmlinuz-' + COMPAT, 'other kernel\n')
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.image'), 'fail')

    def test_unpackaged_running_kernel_fails(self):
        self.put('proc/sys/kernel/osrelease', '6.99.0-custom\n')
        result = self.run_check(full=False)
        self.assertEqual(self.status('kernel.release'), 'fail')
        self.assertEqual(result['state']['kernel'], 'other')

    def test_replaced_boot_image_fails_quick_and_full(self):
        self.put('boot/vmlinuz-' + KERNEL, 'evil kernel\n')
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.image'), 'fail')
        self.run_check(full=True)
        self.assertEqual(self.status('kernel.image'), 'fail')
        self.assertIn('signed kernel package', self.reason('kernel.image'))

    def test_boot_image_matching_a_forged_dpkg_record_still_fails_the_full_check(self):
        self.put('boot/vmlinuz-' + KERNEL, 'evil kernel\n')
        record = self.root / ('var/lib/dpkg/info/linux-image-%s.md5sums' % KERNEL)
        record.write_text('%s  boot/vmlinuz-%s\n' % (hashlib.md5(b'evil kernel\n').hexdigest(), KERNEL))
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.image'), 'fail', 'the signed per-file list, not dpkg, is what counts')
        self.assertIn('signed per-file list', self.reason('kernel.image'))
        self.run_check(full=True)
        self.assertEqual(self.status('kernel.image'), 'fail')

    def test_newer_desos_kernel_from_the_update_channel_is_not_called_an_ubuntu_kernel(self):
        status = self.root / 'var/lib/dpkg/status'
        status.write_text(status.read_text() + '\nPackage: linux-image-6.18.70-desos\nStatus: install ok installed\n'
                          'Architecture: amd64\nVersion: 6.18.70-1desos1\n')
        self.put('proc/sys/kernel/osrelease', '6.18.70-desos\n')
        result = self.run_check(full=False)
        self.assertEqual(self.status('kernel.release'), 'unknown')
        self.assertEqual(self.by_id['kernel.release']['details']['flavour'], 'desos-update')
        self.assertEqual(result['state']['kernel'], 'other')
        self.assertNotIn('Ubuntu', self.reason('kernel.release'))
        self.assertEqual(self.status('kernel.image'), 'unknown')

    def test_live_session_also_checks_the_image_on_the_boot_medium(self):
        self.put('proc/cmdline', 'BOOT_IMAGE=/casper/vmlinuz-%s boot=casper quiet\n' % KERNEL)
        self.put('proc/mounts', '/cow / overlay rw 0 0\n')
        self.put('cdrom/casper/vmlinuz-' + KERNEL, 'fixture kernel image\n' * 50)
        result = self.run_check(full=True)
        self.assertEqual(result['overall'], 'pass')
        self.assertEqual(len(self.by_id['kernel.image']['details']['sha256']), 2)
        self.assertEqual(result['state']['session'], 'live')
        self.put('cdrom/casper/vmlinuz-' + KERNEL, 'swapped on the stick\n')
        self.run_check(full=True)
        self.assertEqual(self.status('kernel.image'), 'fail')
        self.assertIn('live medium', self.reason('kernel.image'))

    def test_changed_config_fails(self):
        self.put('boot/config-' + KERNEL, 'CONFIG_FIXTURE=n\n')
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.config'), 'fail')

    def test_builtin_config_is_compared_when_the_kernel_exposes_it(self):
        import gzip
        self.put('proc/config.gz', gzip.compress(b'CONFIG_FIXTURE=y\n'))
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.config'), 'pass')
        self.assertIn('/proc/config.gz', self.by_id['kernel.config']['details']['sha256'])
        self.put('proc/config.gz', gzip.compress(b'CONFIG_FIXTURE=m\n'))
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.config'), 'fail')

    def test_provenance_not_bound_to_the_signed_manifest_fails(self):
        path = self.root / 'usr/share/doc/desos/build/kernel-provenance.json'
        data = json.loads(path.read_text())
        data['packages'] = {name: 'b' * 64 for name in data['packages']}
        data['config_sha256'] = sha('CONFIG_FIXTURE=n\n')
        path.write_text(json.dumps(data))
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.provenance'), 'fail')
        self.assertEqual(self.status('kernel.config'), 'unknown')

    def test_rewritten_provenance_config_hash_is_caught_by_the_quick_check_too(self):
        """The provenance record is covered by the signed SHA256SUMS and the config by the signed file list."""
        path = self.root / 'usr/share/doc/desos/build/kernel-provenance.json'
        data = json.loads(path.read_text())
        data['config_sha256'] = sha('CONFIG_FIXTURE=n\n')
        path.write_text(json.dumps(data))
        for name in ('boot/config-' + KERNEL, 'usr/share/doc/desos/build/config-' + KERNEL):
            self.put(name, 'CONFIG_FIXTURE=n\n')
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.evidence'), 'fail')
        self.assertEqual(self.status('kernel.config'), 'fail')
        self.assertIn('signed per-file list', self.reason('kernel.config'))
        self.run_check(full=True)
        self.assertEqual(self.status('kernel.config'), 'fail')

    def test_inconsistent_desktop_pin_fails(self):
        path = self.root / 'usr/share/doc/desos/build/desktop-pin.json'
        path.write_text(path.read_text().replace('"sha256": "', '"sha256": "0'))
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.provenance'), 'fail')

    def test_offline_check_of_a_mounted_system(self):
        result = self.run_check(full=True, offline=True)
        self.assertTrue(result['offline'])
        self.assertIsNone(result['boot_id'])
        for identifier in ('kernel.release', 'state.secure-boot', 'state.live-session', 'state.root-filesystem'):
            self.assertEqual(self.status(identifier), 'unknown', identifier)
        self.assertEqual(self.status('kernel.image'), 'pass')
        self.assertEqual(self.status('packages.files'), 'pass')
        self.assertEqual(result['counts']['fail'], 0)


class EvidenceTests(VerifyCase):
    BUILD = 'usr/share/doc/desos/build/'

    def test_edited_or_added_or_missing_evidence_fails(self):
        self.put(self.BUILD + 'source-pin.json', '{"version": "6.18.62"}')
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.evidence'), 'fail')
        self.assertIn('source-pin.json differs', self.reason('signatures.evidence'))
        self.install_evidence(self.repository())
        self.put(self.BUILD + 'extra-note.txt', 'not part of the build\n')
        self.run_check(full=False)
        self.assertIn('extra-note.txt is not covered', self.reason('signatures.evidence'))
        (self.root / self.BUILD / 'extra-note.txt').unlink()
        (self.root / self.BUILD / 'signature-status.txt').unlink()
        self.run_check(full=False)
        self.assertIn('signature-status.txt is missing', self.reason('signatures.evidence'))

    def test_removing_the_signed_sums_does_not_downgrade_to_unknown(self):
        for name in ('SHA256SUMS.asc', 'SHA256SUMS'):
            (self.root / self.BUILD / name).unlink()
            self.run_check(full=False)
            self.assertEqual(self.status('signatures.evidence'), 'fail')
            self.assertIn('signed manifest says', self.reason('signatures.evidence'))

    def test_validly_signed_sums_of_another_build_are_refused(self):
        build = self.root / self.BUILD
        (build / 'source-pin.json').write_text('{"archive_sha256": "%s", "version": "6.18.62", "maintainers": [{"fingerprint": "%s"}], "pinned_on": "another day"}'
                                               % ('a' * 64, 'F' * 40))
        lines = (build / 'SHA256SUMS').read_text().splitlines()
        (build / 'SHA256SUMS').write_text(''.join(
            ('%s  source-pin.json' % sha((build / 'source-pin.json').read_bytes()) if line.endswith('  source-pin.json') else line) + '\n'
            for line in lines))
        (build / 'SHA256SUMS.asc').unlink()
        gpg(self.fixture.homes[0], '--armor', '--output', str(build / 'SHA256SUMS.asc'), '--detach-sign', str(build / 'SHA256SUMS'))
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.evidence'), 'fail')
        self.assertIn('another build', self.reason('signatures.evidence'))

    def test_sums_signed_by_another_key_fail(self):
        build = self.root / self.BUILD
        (build / 'SHA256SUMS.asc').write_bytes((self.fixture.repos[1] / 'build-evidence/SHA256SUMS.asc').read_bytes())
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.evidence'), 'fail')


class ImageWithoutTheNewEvidenceTests(VerifyCase):
    """A 0.1 image: no per-file lists in the manifest, no signed SHA256SUMS."""
    def repository(self):
        return self.fixture.legacy

    def test_missing_evidence_is_unknown_and_never_a_failure(self):
        result = self.run_check(full=False)
        self.assertEqual(result['counts']['fail'], 0)
        self.assertEqual(result['overall'], 'unknown')
        self.assertEqual({c['id'] for c in result['checks'] if c['status'] == 'unknown'},
                         {'signatures.evidence', 'packages.key-files'})
        self.assertIn('before 0.2', self.reason('signatures.evidence'))
        self.assertIn('before 0.2', self.reason('packages.key-files'))
        self.assertEqual(dv.main(['--quick', '--quiet'], config=self.config()), 0)
        result = self.run_check(full=True)
        self.assertEqual({c['id']: c['status'] for c in result['checks'] if c['status'] != 'pass'},
                         {'signatures.evidence': 'unknown'})
        self.assertFalse(self.by_id['kernel.provenance']['details']['evidence_signed'])
        self.assertIn('itself unsigned', self.reason('kernel.provenance'))
        # The kernel build's own, unsigned SHA256SUMS lying there changes nothing.
        self.put('usr/share/doc/desos/build/SHA256SUMS', 'not signed\n')
        self.run_check(full=False)
        self.assertEqual(self.status('signatures.evidence'), 'unknown')

    def test_quick_kernel_checks_fall_back_to_the_weaker_comparisons(self):
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.image'), 'pass')
        self.assertEqual(self.by_id['kernel.image']['details']['method'], 'dpkg-md5')
        self.assertIn('weak', self.reason('kernel.image'))
        self.assertEqual(self.by_id['kernel.config']['details']['method'], 'provenance-sha256')
        self.assertEqual(self.by_id['kernel.config']['details']['evidence'], 'unsigned-provenance')
        self.assertEqual(self.status('identity.os-release'), 'pass')

    def test_the_documented_weaknesses_of_the_old_quick_check_remain(self):
        self.put('boot/vmlinuz-' + KERNEL, 'evil kernel\n')
        record = self.root / ('var/lib/dpkg/info/linux-image-%s.md5sums' % KERNEL)
        record.write_text('%s  boot/vmlinuz-%s\n' % (hashlib.md5(b'evil kernel\n').hexdigest(), KERNEL))
        self.run_check(full=False)
        self.assertEqual(self.status('kernel.image'), 'pass')
        self.run_check(full=True)
        self.assertEqual(self.status('kernel.image'), 'fail')


class IdentityTests(VerifyCase):
    def sources(self, name, text):
        self.put('etc/apt/sources.list.d/' + name, text)
        self.run_check(full=False)
        return self.status('identity.apt-sources')

    def test_foreign_os_release_fails(self):
        self.put('usr/lib/os-release', 'NAME="Ubuntu"\nID=ubuntu\n')
        self.run_check(full=False)
        self.assertEqual(self.status('identity.os-release'), 'fail')
        self.assertIn("'ubuntu'", self.reason('identity.os-release'))

    def test_edited_desos_os_release_fails_even_in_the_quick_check(self):
        self.put('usr/lib/os-release', OS_RELEASE + 'HOME_URL="https://example.invalid"\n')
        self.run_check(full=False)
        self.assertEqual(self.status('identity.os-release'), 'fail')

    def test_third_party_source_is_unknown(self):
        status = self.sources('ppa.sources', 'Types: deb\nURIs: https://ppa.example.invalid/ubuntu\nSuites: resolute\n'
                              'Components: main\nSigned-By: /etc/apt/keyrings/ppa.gpg\n')
        self.assertEqual(status, 'unknown')
        self.assertIn('ppa.example.invalid', self.reason('identity.apt-sources'))

    def test_disabled_and_source_only_entries_are_ignored(self):
        status = self.sources('extra.sources', 'Types: deb\nURIs: https://x.example.invalid\nSuites: s\nEnabled: no\n\n'
                              'Types: deb-src\nURIs: https://y.example.invalid\nSuites: s\n')
        self.assertEqual(status, 'pass')

    def test_one_line_format_is_parsed(self):
        self.assertEqual(self.sources('old.list', '# deb http://commented.invalid x main\n'
                                      'deb [arch=amd64 signed-by=/etc/k.gpg] http://mirror.example.invalid/u resolute main\n'), 'unknown')
        self.assertEqual(self.sources('old.list', 'deb [trusted=yes] http://mirror.example.invalid/u resolute main\n'), 'fail')

    def test_ubuntu_host_with_another_key_is_not_expected(self):
        self.assertEqual(self.sources('ubuntu.sources', UBUNTU_SOURCES.replace('ubuntu-archive-keyring.gpg', 'other.gpg')), 'unknown')

    def test_desos_key_trusted_for_another_location_fails(self):
        self.assertEqual(self.sources('evil.sources', DESOS_SOURCES.replace('file:/usr/share/desos/repository',
                                                                            'https://evil.example.invalid')), 'fail')

    def test_sources_desos_configures_pass_only_with_their_own_keyrings(self):
        vendor = Path(__file__).resolve().parents[1] / 'image/vendor'
        for name in ('brave-browser.sources', 'vscode.sources'):
            self.assertEqual(self.sources(name, (vendor / name).read_text()), 'pass', name)
        self.assertIn('vscode.sources', self.by_id['identity.apt-sources']['details']['known'])
        dev = ('Types: deb\nURIs: file:/var/lib/desos-dev/channel/\nSuites: ./\n'
               'Signed-By: /usr/share/keyrings/desos-dev.gpg\n')
        self.assertEqual(self.sources('desos-dev.sources', dev), 'pass')
        published = ('Types: deb\nURIs: https://coxy.example.invalid/desos-dev/\nSuites: ./\n'
                     'Signed-By: /usr/share/keyrings/desos-dev-published.gpg\n')
        self.assertEqual(self.sources('desos-dev-published.sources', published), 'pass')
        # The same file names with another keyring or another place are not recognised.
        self.assertEqual(self.sources('vscode.sources', (vendor / 'vscode.sources').read_text().replace(
            'microsoft.gpg', 'other.gpg')), 'unknown')
        self.assertEqual(self.sources('vscode.sources', (vendor / 'vscode.sources').read_text().replace(
            'packages.microsoft.com', 'evil.example.invalid')), 'unknown')
        self.assertEqual(self.sources('desos-dev.sources', dev.replace('/var/lib/desos-dev/channel', '/tmp/x')), 'unknown')

    def test_enabled_update_channel_passes_but_only_under_its_own_name(self):
        channel = DESOS_SOURCES.replace('file:/usr/share/desos/repository', 'https://updates.example.invalid')
        self.assertEqual(self.sources('desos-updates.sources', channel), 'pass')
        (self.root / 'etc/apt/sources.list.d/desos-updates.sources').unlink()
        self.assertEqual(self.sources('other.sources', channel), 'fail')

    def test_altered_missing_or_duplicated_desos_source_fails(self):
        self.assertEqual(self.sources('desos.sources', DESOS_SOURCES.replace(
            'Signed-By: /usr/share/keyrings/desos-archive-keyring.asc', 'Trusted: yes')), 'fail')
        self.assertEqual(self.sources('desos.sources', DESOS_SOURCES), 'pass')
        self.assertEqual(self.sources('again.list', 'deb [signed-by=/usr/share/keyrings/desos-archive-keyring.asc] '
                                      'file:///usr/share/desos/repository desos main\n'), 'fail')
        (self.root / 'etc/apt/sources.list.d/again.list').unlink()
        (self.root / 'etc/apt/sources.list.d/desos.sources').unlink()
        self.run_check(full=False)
        self.assertEqual(self.status('identity.apt-sources'), 'fail')


class StateTests(VerifyCase):
    def test_secure_boot_states(self):
        self.put(SECURE_BOOT, b'\x06\x00\x00\x00\x01')
        self.assertEqual(self.run_check(full=False)['state']['secure_boot'], 'enabled')
        (self.root / SECURE_BOOT).unlink()
        self.assertEqual(self.run_check(full=False)['state']['secure_boot'], 'disabled')
        shutil.rmtree(self.root / 'sys/firmware/efi')
        self.assertEqual(self.run_check(full=False)['state']['secure_boot'], 'unsupported')
        self.assertIn('BIOS', self.reason('state.secure-boot'))

    def test_live_session_expects_an_overlay_root(self):
        self.put('proc/cmdline', 'BOOT_IMAGE=/casper/vmlinuz-%s boot=casper\n' % KERNEL)
        self.run_check(full=False)
        self.assertEqual(self.by_id['state.live-session']['details']['value'], 'live')
        self.assertEqual(self.status('state.root-filesystem'), 'unknown')
        self.put('proc/mounts', 'rootfs / rootfs rw 0 0\n/cow / overlay rw 0 0\n')
        self.run_check(full=False)
        self.assertEqual(self.status('state.root-filesystem'), 'pass')

    def test_running_release_read_stays_below_the_kernel_sysctl_limit(self):
        # Linux returns ENOMEM for a /proc/sys read of 4 MiB or more; the
        # 0.2 image reported the running kernel as unreadable on every boot.
        real_open = open
        osrelease = str(self.root / 'proc/sys/kernel/osrelease')

        class SysctlFile:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.stream.close()
            def read(self, size=-1):
                if size < 0 or size >= 4 * 1024 * 1024:
                    raise OSError(12, 'Cannot allocate memory')
                return self.stream.read(size)

        def kernel_like_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return SysctlFile(stream) if str(path) == osrelease else stream

        with mock.patch('builtins.open', kernel_like_open):
            self.run_check(full=False)
        self.assertNotEqual(self.reason('kernel.release'), 'the running kernel release could not be read')
        self.assertEqual(self.status('kernel.release'), 'pass')

    def test_unvalidated_root_filesystem_is_unknown_not_failed(self):
        self.put('proc/mounts', '/dev/vda2 / xfs rw 0 0\n')
        result = self.run_check(full=False)
        self.assertEqual(self.status('state.root-filesystem'), 'unknown')
        self.assertEqual(result['state']['root_filesystem'], 'xfs')
        self.assertEqual(result['counts']['fail'], 0)


class OutputTests(VerifyCase):
    def test_results_are_written_atomically_and_world_readable(self):
        config = self.config()
        self.assertEqual(dv.main(['--write', '--quiet'], config=config), 0)
        run, state = self.root / 'run/desos', self.root / 'var/lib/desos'
        self.assertEqual(sorted(p.name for p in run.iterdir()), ['verify-status', 'verify.json'])
        self.assertEqual([p.name for p in state.iterdir()], ['verify-full.json'])
        for path in (run / 'verify.json', run / 'verify-status', state / 'verify-full.json'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        full = json.loads((state / 'verify-full.json').read_text())
        self.assertEqual(full['mode'], 'full')
        self.assertEqual(json.loads((run / 'verify.json').read_text()), full)
        line = (run / 'verify-status').read_text()
        self.assertEqual(line.count('\n'), 1)
        self.assertEqual(line.strip(), full['summary'])
        self.assertIn('17 of 17 full checks passed', line)

    def test_quick_result_carries_the_last_full_outcome_into_the_status_line(self):
        self.put('usr/share/des-ui/.hidden', 'changed\n')
        self.assertEqual(dv.main(['--write', '--quiet'], config=self.config()), 1)
        self.assertEqual(dv.main(['--quick', '--write', '--quiet'], config=self.config()), 0)
        quick = json.loads((self.root / 'run/desos/verify.json').read_text())
        self.assertEqual(quick['mode'], 'quick')
        self.assertEqual(quick['last_full']['overall'], 'fail')
        self.assertEqual(quick['last_full']['failed'], ['packages.files'])
        self.assertTrue(quick['last_full']['same_manifest'])
        self.assertIn('last full check FAILED', (self.root / 'run/desos/verify-status').read_text())
        self.assertEqual(json.loads((self.root / 'var/lib/desos/verify-full.json').read_text())['mode'], 'full')

    def test_status_line_for_a_failure_names_the_check(self):
        self.put('usr/lib/os-release', 'ID=ubuntu\n')
        dv.main(['--quick', '--write', '--quiet'], config=self.config())
        line = (self.root / 'run/desos/verify-status').read_text()
        self.assertTrue(line.startswith('DesOS integrity check FAILED'))
        self.assertIn('identity.os-release', line)

    def test_untrusted_text_cannot_inject_lines_into_the_status(self):
        self.put('etc/apt/sources.list.d/x.list', 'deb http://a.invalid/\x1b[31m suite main\n')
        self.put('proc/sys/kernel/osrelease', '9.9\x1b]0;evil\x07\n')
        result = self.run_check(full=False)
        text = json.dumps([c['reason'] for c in result['checks']]) + result['summary']
        self.assertNotIn('\\u001b', text)
        self.assertNotIn('\x1b', text)
        self.assertEqual(dv.clean('a\nb\tc'), 'a b?c')

    def test_cli_human_and_json_output_and_exit_status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(dv.main(['--quick'], config=self.config()), 0)
        self.assertIn('[PASS   ] signatures.manifest', out.getvalue())
        self.assertIn(self.fixture.fingerprints[0], out.getvalue())
        self.assertIn('not run in a quick check: repository.pool, packages.files', out.getvalue())
        self.put('boot/config-' + KERNEL, 'CONFIG_FIXTURE=n\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(dv.main(['--quick', '--json'], config=self.config()), 1)
        self.assertEqual(json.loads(out.getvalue())['overall'], 'fail')

    def test_tool_runs_as_a_program_against_an_explicit_root(self):
        done = subprocess.run([str(TOOL), '--quick', '--json', '--root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertTrue(result['offline'])
        self.assertEqual(result['counts']['fail'], 0)

    def test_a_crashing_check_becomes_unknown(self):
        self.put('usr/share/doc/desos/build/kernel-provenance.json', '{"kernel_release": "6.18.62-desos", "packages": 5}')
        result = self.run_check(full=False)
        self.assertEqual(self.status('kernel.provenance'), 'unknown')
        self.assertEqual(len(result['checks']), 16)


class PathTests(unittest.TestCase):
    def test_symlinks_resolve_inside_the_injected_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'usr/lib').mkdir(parents=True)
            (root / 'usr/lib/target').write_text('inside\n')
            os.symlink('/usr/lib', root / 'lib')
            os.symlink('../../../../etc/passwd', root / 'usr/lib/escape')
            config = dv.Config(root=root)
            self.assertEqual(config.inroot('/lib/target'), root / 'usr/lib/target')
            self.assertEqual(config.inroot('lib/escape'), root / 'etc/passwd')
            self.assertEqual(config.inroot('/lib', follow=False), root / 'lib')
            self.assertTrue(config.offline)
            self.assertFalse(dv.Config(root='/').offline)

    def test_payload_names_keep_leading_dots_of_file_names(self):
        self.assertEqual(dv.payload_name('./usr/share/.hidden'), 'usr/share/.hidden')
        self.assertEqual(dv.payload_name('./'), '')


class ComponentConventionTests(unittest.TestCase):
    def test_component_metadata_and_tree_follow_the_convention(self):
        data = json.loads((COMPONENT / 'component.json').read_text())
        self.assertEqual(sorted(data), ['enable_units', 'name', 'notes', 'package', 'packages'])
        self.assertEqual(data['name'], 'verify')
        # Delivered inside the signed desos-verify package, not copied in from rootfs/.
        self.assertEqual(data['package'], 'desos-verify')
        self.assertIn('gpgv', data['packages'])
        self.assertTrue((COMPONENT / 'README.md').is_file())
        units = COMPONENT / 'rootfs/usr/lib/systemd/system'
        for unit in data['enable_units']:
            self.assertTrue((units / unit).is_file(), unit)
            self.assertIn('[Install]', (units / unit).read_text())
        for path in (COMPONENT / 'rootfs').rglob('*'):
            self.assertFalse(path.is_symlink(), path)
            self.assertNotIn('__pycache__', path.parts, path)
            if path.is_file():
                executable = bool(path.stat().st_mode & 0o111)
                self.assertEqual(executable, path == TOOL, path)

    def test_units_run_the_shipped_tool_with_the_documented_modes(self):
        units = COMPONENT / 'rootfs/usr/lib/systemd/system'
        quick = (units / 'desos-verify-quick.service').read_text()
        full = (units / 'desos-verify-full.service').read_text()
        self.assertIn('ExecStart=/usr/bin/desos-verify --quick --write --quiet', quick)
        self.assertIn('ExecStart=/usr/bin/desos-verify --write --quiet', full)
        self.assertNotIn('--quick', full)
        for text in (quick, full):
            self.assertIn('ReadWritePaths=/run/desos /var/lib/desos', text)
            self.assertIn('PrivateNetwork=yes', text)
        self.assertIn('IOSchedulingClass=idle', full)
        self.assertIn('CPUSchedulingPolicy=idle', full)
        self.assertIn('Unit=desos-verify-full.service', (units / 'desos-verify-full.timer').read_text())
        tmpfiles = (COMPONENT / 'rootfs/usr/lib/tmpfiles.d/desos-verify.conf').read_text()
        self.assertIn('d /run/desos 0755 root root', tmpfiles)
        self.assertIn('d /var/lib/desos 0755 root root', tmpfiles)

    def test_tool_uses_only_the_standard_library(self):
        import ast
        allowed = set(sys.stdlib_module_names)
        tree = ast.parse(TOOL.read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module.split('.')[0])
        self.assertEqual(names - allowed, set())


if __name__ == '__main__':
    unittest.main()
