"""The offline update-channel publisher against really signed repositories.

A throwaway key is generated in a temporary GNUPGHOME; the real repository
builder signs the inputs. Nothing leaves the temp directory and nothing is
uploaded: the tool has no network code, which is asserted too.
"""
import ast
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from image import component_repo, publish_channel

REPO = Path(__file__).resolve().parents[1]
NEEDED = ('gpg', 'dpkg-deb', 'dpkg')


def gpg(home, *arguments):
    return subprocess.run(['gpg', '--no-options', '--homedir', str(home), '--batch', '--no-tty', *arguments],
                          check=True, capture_output=True, text=True).stdout


def make_key(home):
    home.mkdir(mode=0o700)
    gpg(home, '--pinentry-mode', 'loopback', '--passphrase', '', '--quick-generate-key',
        'DesOS Channel Test <test@example.invalid>', 'ed25519', 'sign', 'never')
    listing = gpg(home, '--with-colons', '--list-secret-keys')
    return next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))


def make_deb(work, name, version, content='payload\n', depends=None):
    tree = work / ('%s-%s-tree' % (name, version))
    shutil.rmtree(tree, ignore_errors=True)
    (tree / 'DEBIAN').mkdir(parents=True)
    (tree / 'DEBIAN/control').write_text(
        'Package: %s\nVersion: %s\nArchitecture: amd64\nMaintainer: DesOS Test <test@example.invalid>\n%s'
        'Description: channel fixture\n' % (name, version, 'Depends: %s\n' % depends if depends else ''))
    (tree / 'usr/share' / name).mkdir(parents=True)
    (tree / 'usr/share' / name / 'file').write_text(content)
    output = work / ('%s_%s.deb' % (name, version))
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(tree), str(output)], check=True, capture_output=True)
    return output


def snapshot(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes() for path in sorted(directory.rglob('*')) if path.is_file()}


@unittest.skipUnless(all(shutil.which(tool) for tool in NEEDED) and
                     (shutil.which('apt-ftparchive') or shutil.which('dpkg-scanpackages')),
                     'gpg, dpkg and an apt index tool are needed for the signed fixture')
class PublishChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix='desos-channel-fixture-'))
        cls.homes = [cls.base / 'gpg-a', cls.base / 'gpg-b']
        cls.fingerprints = [make_key(home) for home in cls.homes]
        work = cls.base
        cls.debs = {
            'kernel-1': make_deb(work, 'linux-image-6.18.62-desos', '6.18.62-1desos1'),
            'meta-1': make_deb(work, 'linux-image-desos', '6.18.62-1desos1', depends='linux-image-6.18.62-desos (= 6.18.62-1desos1)'),
            'ui-1': make_deb(work, 'des-ui', '0.1.1'),
            'kernel-2': make_deb(work, 'linux-image-6.18.70-desos', '6.18.70-1desos1'),
            'meta-2': make_deb(work, 'linux-image-desos', '6.18.70-1desos1', depends='linux-image-6.18.70-desos (= 6.18.70-1desos1)'),
        }
        cls.first = cls.repository('first', ['kernel-1', 'meta-1', 'ui-1'])
        cls.second = cls.repository('second', ['kernel-2', 'meta-2', 'ui-1'])
        cls.foreign = cls.repository('foreign', ['ui-1'], key=1)

    @classmethod
    def repository(cls, name, debs, key=0, epoch=1794268800):
        output = cls.base / name
        component_repo.build_repository([str(cls.debs[d]) for d in debs], output, cls.homes[key], cls.fingerprints[key], epoch=epoch)
        return output

    @classmethod
    def tearDownClass(cls):
        for home in cls.homes:
            subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'all'], capture_output=True, check=False)
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='desos-channel-test-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def publish(self, repository, name='channel', previous=None, key=0, **options):
        return publish_channel.publish(repository, self.work / name, self.homes[key], self.fingerprints[key],
                                       previous=previous, epoch=1794355200, **options)

    def damaged(self, source=None):
        copy = self.work / 'damaged'
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(source or self.first, copy)
        return copy

    def refused(self, repository, words, **options):
        with self.assertRaises(component_repo.RepoError) as caught:
            self.publish(repository, 'refused', **options)
        self.assertIn(words, str(caught.exception))
        self.assertFalse((self.work / 'refused').exists(), 'a refusal must write nothing')

    # --- the tree ---

    def test_first_publication_is_a_complete_signed_apt_tree_for_the_shipped_source(self):
        before = snapshot(self.first)
        result = self.publish(self.first)
        tree = self.work / 'channel'
        self.assertEqual((result['status'], result['uploaded'], result['package_count']), ('published-locally', False, 3))
        self.assertEqual(snapshot(self.first), before, 'the input repository is not modified')
        release = (tree / 'dists/desos/Release').read_text()
        # What system/'s update source and unattended-upgrades pattern expect.
        from image import configure
        self.assertIn('Suites: desos\nComponents: main\n', configure.UPDATE_SOURCE)
        for line in ('Origin: DesOS', 'Suite: desos', 'Components: main', 'Architectures: amd64',
                     'Description: DesOS network update channel'):
            self.assertIn(line + '\n', release)
        packages = (tree / 'dists/desos/main/binary-amd64/Packages').read_text()
        self.assertEqual(gzip.decompress((tree / 'dists/desos/main/binary-amd64/Packages.gz').read_bytes()).decode(), packages)
        for stanza in publish_channel.stanzas(packages):
            self.assertTrue((tree / stanza['filename']).is_file())
        self.assertFalse((tree / '.incomplete').exists())
        # Same key as the image trusts: the exported keyring is byte-identical to the repository's.
        self.assertEqual((tree / 'desos-archive-keyring.asc').read_bytes(), (self.first / 'desos-archive-keyring.asc').read_bytes())
        for arguments in (['--verify', str(tree / 'dists/desos/InRelease')],
                          ['--verify', str(tree / 'dists/desos/Release.gpg'), str(tree / 'dists/desos/Release')],
                          ['--verify', str(tree / 'manifest.json.asc'), str(tree / 'manifest.json')]):
            gpg(self.homes[0], *arguments)
        manifest = json.loads((tree / 'manifest.json').read_text())
        self.assertEqual(manifest['channel']['purpose'], 'network update channel')
        self.assertEqual(len(manifest['channel']['added']), 3)
        self.assertEqual(manifest['channel']['kept'], [])
        # The published tree is itself valid input (it is the next --previous).
        publish_channel.verify_repository(tree, self.homes[0], self.fingerprints[0])
        # Nothing but the output directory appeared.
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ['channel'])

    def test_a_new_kernel_is_added_and_the_previous_one_stays_installable(self):
        self.publish(self.first, 'one')
        before = snapshot(self.work / 'one')
        result = self.publish(self.second, 'two', previous=self.work / 'one')
        self.assertEqual(snapshot(self.work / 'one'), before, 'the previous tree is not modified')
        self.assertEqual(result['package_count'], 5)
        self.assertEqual(result['added'], ['linux-image-6.18.70-desos 6.18.70-1desos1 amd64', 'linux-image-desos 6.18.70-1desos1 amd64'])
        self.assertEqual(result['kept'], ['des-ui 0.1.1 amd64', 'linux-image-6.18.62-desos 6.18.62-1desos1 amd64',
                                          'linux-image-desos 6.18.62-1desos1 amd64'])
        self.assertEqual(result['warnings'], [])
        tree = self.work / 'two'
        stanzas = publish_channel.stanzas((tree / 'dists/desos/main/binary-amd64/Packages').read_text())
        meta = sorted(s['version'] for s in stanzas if s['package'] == 'linux-image-desos')
        self.assertEqual(meta, ['6.18.62-1desos1', '6.18.70-1desos1'], 'both versions of the metapackage are offered')
        newest = next(s for s in stanzas if s['package'] == 'linux-image-desos' and s['version'] == '6.18.70-1desos1')
        self.assertEqual(newest['depends'], 'linux-image-6.18.70-desos (= 6.18.70-1desos1)')
        for name in ('linux-image-6.18.62-desos_6.18.62-1desos1_amd64.deb', 'linux-image-6.18.70-desos_6.18.70-1desos1_amd64.deb'):
            self.assertEqual(len(list((tree / 'pool').rglob(name))), 1)
        # Per-file lists are regenerated from the verified packages and equal the signed originals.
        manifest = json.loads((tree / 'manifest.json').read_text())
        source = {e['package'] + e['version']: e['file_list']['sha256'] for repository in (self.first, self.second)
                  for e in json.loads((repository / 'manifest.json').read_text())['packages']}
        for entry in manifest['packages']:
            self.assertEqual(entry['file_list']['sha256'], source[entry['package'] + entry['version']])
        # A third round keeps everything again.
        third = self.publish(self.second, 'three', previous=tree)
        self.assertEqual((third['package_count'], third['added']), (5, []))

    def test_publishing_an_older_version_is_reported(self):
        self.publish(self.second, 'one')
        result = self.publish(self.first, 'two', previous=self.work / 'one')
        self.assertEqual(len(result['warnings']), 1)
        self.assertIn('linux-image-desos 6.18.62-1desos1', result['warnings'][0])
        self.assertIn('is not newer than every published version', result['warnings'][0])

    # --- refusals ---

    def test_unsigned_or_differently_signed_input_is_refused(self):
        copy = self.damaged()
        (copy / 'manifest.json.asc').unlink()
        self.refused(copy, 'manifest.json.asc')
        copy = self.damaged()
        (copy / 'dists/desos/InRelease').unlink()
        self.refused(copy, 'InRelease')
        copy = self.damaged()
        (copy / 'dists/desos/Release.gpg').write_text('not a signature\n')
        self.refused(copy, 'gpg failed')
        # Validly signed, but by a key other than the one selected for publishing.
        self.refused(self.foreign, 'gpg failed')
        with self.assertRaises(component_repo.RepoError):
            self.publish(self.first, 'refused', key=1)
        self.assertFalse((self.work / 'refused').exists())

    def test_incomplete_or_inconsistent_input_is_refused(self):
        copy = self.damaged()
        (copy / '.incomplete').write_text('in progress\n')
        self.refused(copy, '.incomplete')
        copy = self.damaged()
        next((copy / 'pool').rglob('des-ui_*.deb')).unlink()
        self.refused(copy, 'missing')
        copy = self.damaged()
        deb = next((copy / 'pool').rglob('des-ui_*.deb'))
        deb.write_bytes(deb.read_bytes() + b'\n')
        self.refused(copy, 'differs from the signed manifest')
        copy = self.damaged()
        shutil.copyfile(self.debs['kernel-2'], copy / 'pool/main/extra.deb')
        self.refused(copy, 'pool holds files the signed manifest does not list')
        copy = self.damaged()
        index = copy / 'dists/desos/main/binary-amd64/Packages'
        index.write_text(index.read_text().replace('Version: 0.1.1', 'Version: 0.1.2'))
        self.refused(copy, 'differs from the signed manifest')
        copy = self.damaged()
        manifest = copy / 'manifest.json'
        manifest.write_text(manifest.read_text().replace('0.1.1', '0.1.2'))
        self.refused(copy, 'gpg failed')
        copy = self.damaged()
        release = copy / 'dists/desos/Release'
        release.write_text(release.read_text().replace('Suite: desos', 'Suite: other'))
        self.refused(copy, 'gpg failed')
        # InRelease that is validly signed but says something else than Release.
        copy = self.damaged()
        other = self.work / 'other-release'
        other.write_text((copy / 'dists/desos/Release').read_text().replace('Label: DesOS', 'Label: Else'))
        (copy / 'dists/desos/InRelease').unlink()
        gpg(self.homes[0], '--armor', '--output', str(copy / 'dists/desos/InRelease'), '--clearsign', str(other))
        self.refused(copy, 'differs from')
        self.refused(self.work / 'does-not-exist', 'must be an existing directory')

    def test_a_published_version_is_never_replaced_by_other_bytes(self):
        self.publish(self.first, 'one')
        changed = make_deb(self.work, 'des-ui', '0.1.1', content='rebuilt with other bytes\n')
        component_repo.build_repository([str(changed)], self.work / 'rebuilt', self.homes[0], self.fingerprints[0], epoch=1794268800)
        self.refused(self.work / 'rebuilt', 'a published version is never replaced', previous=self.work / 'one')
        # The ledger refuses the same thing even for a first publication, and is never written here.
        ledger = self.work / 'ledger.json'
        ledger.write_text(json.dumps({'schema': 'desos-package-ledger-v1', 'packages': [
            {'package': 'des-ui', 'version': '0.1.1', 'architecture': 'amd64', 'sha256': component_repo.sha256(self.debs['ui-1'])}]}))
        before = ledger.read_bytes()
        with self.assertRaises(component_repo.RepoError) as caught:
            self.publish(self.work / 'rebuilt', 'ledgered', ledger=ledger)
        self.assertIn('never be reused for different bytes', str(caught.exception))
        self.assertTrue((self.work / 'ledgered/.incomplete').exists(), 'a tree that failed while being written is marked')
        self.publish(self.first, 'recorded', ledger=ledger)
        self.assertEqual(ledger.read_bytes(), before)

    def test_output_must_be_new_and_separate_and_the_key_home_dedicated(self):
        (self.work / 'exists').mkdir()
        with self.assertRaises(component_repo.RepoError):
            self.publish(self.first, 'exists')
        self.assertEqual(list((self.work / 'exists').iterdir()), [])
        for inside in (self.first / 'channel', self.homes[0] / 'channel'):
            with self.assertRaises(component_repo.RepoError):
                publish_channel.publish(self.first, inside, self.homes[0], self.fingerprints[0])
            self.assertFalse(inside.exists())
        with self.assertRaises(component_repo.RepoError):
            self.publish(self.first, 'same', previous=self.first)
        loose = self.work / 'loose-home'
        loose.mkdir(mode=0o755)
        with self.assertRaises(component_repo.RepoError):
            publish_channel.publish(self.first, self.work / 'x', loose, self.fingerprints[0])
        with self.assertRaises(component_repo.RepoError):
            publish_channel.publish(self.first, self.work / 'x', self.homes[0], 'ABCD')
        self.assertFalse((self.work / 'x').exists())

    def test_command_line_reports_json_and_a_nonzero_status_on_refusal(self):
        arguments = ['--repository', str(self.first), '--output', str(self.work / 'cli'), '--gpg-home', str(self.homes[0]),
                     '--key-fingerprint', self.fingerprints[0], '--ledger', str(self.work / 'ledger.json')]
        (self.work / 'ledger.json').write_text('{"schema": "desos-package-ledger-v1", "packages": []}')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(publish_channel.main(arguments), 0)
        self.assertEqual(json.loads(out.getvalue())['uploaded'], False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(publish_channel.main(arguments), 1)
        self.assertEqual(json.loads(out.getvalue())['status'], 'failed')


class PublishToolShapeTests(unittest.TestCase):
    def test_tool_has_no_network_code_and_uses_only_the_standard_library(self):
        source = (REPO / 'image/publish_channel.py').read_text()
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name.split('.')[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split('.')[0])
        self.assertEqual(names - set(sys.stdlib_module_names) - {'component_repo'}, set())
        self.assertEqual(names & {'socket', 'urllib', 'http', 'ftplib', 'smtplib', 'ssl', 'requests'}, set())
        commands = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertEqual(commands & {'rsync', 'scp', 'curl', 'wget', 'ssh', 'sftp', 'aws', 'gh'}, set())
        # The key never comes from the default home or from the repository's private directory.
        self.assertNotIn('.private', source)
        self.assertIn('repo.validate_home(gpg_home, fingerprint)', source)

    def test_operator_steps_are_documented(self):
        readme = (REPO / 'README.md').read_text()
        for words in ('image/publish_channel.py', '--previous', 'UPDATE_CHANNEL_URL', 'does not upload'):
            self.assertIn(words, readme)


if __name__ == '__main__':
    unittest.main()
