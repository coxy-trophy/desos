import tempfile
import unittest
from pathlib import Path

from image import configure
from image.configure import write_configuration, audit_credentials

REPO = Path(__file__).resolve().parents[1]


class ImageConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name)
        self.root = self.parent / 'rootfs'
        (self.root / 'etc').mkdir(parents=True)
        (self.root / 'etc/debian_version').write_text('forky/sid\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_desos_identity_uses_ubuntu_casper_compatibility(self):
        write_configuration(self.root, self.parent)
        casper = (self.root / 'etc/casper.conf').read_text()
        self.assertIn('USERNAME="desos"', casper)
        self.assertIn('FLAVOUR="DesOS"', casper)
        self.assertIn('BUILD_SYSTEM="Ubuntu"', casper)
        self.assertIn('initial_session', (self.root / 'etc/greetd/config.toml').read_text())

    def test_release_identity_has_one_owner(self):
        # desos-release (image/release_package.py) alone supplies os-release.
        write_configuration(self.root, self.parent)
        self.assertFalse(hasattr(configure, 'IDENTITY'))
        for path in self.root.rglob('*'):
            self.assertNotIn(path.name, ('os-release', 'lsb-release'))

    def test_network_update_channel_ships_disabled_and_signed_by_the_desos_keyring(self):
        write_configuration(self.root, self.parent)
        source = (self.root / 'etc/apt/sources.list.d/desos-updates.sources').read_text()
        fields = dict(line.split(': ', 1) for line in source.splitlines() if line and not line.startswith('#'))
        self.assertEqual(fields, {
            'Enabled': 'no', 'Types': 'deb', 'URIs': configure.UPDATE_CHANNEL_URL,
            'Suites': 'desos', 'Components': 'main',
            'Signed-By': '/usr/share/keyrings/desos-archive-keyring.asc',
        })
        self.assertTrue(configure.UPDATE_CHANNEL_URL.startswith('https://'))
        # Nothing is published: the placeholder must be unresolvable by design.
        self.assertIn('.invalid/', configure.UPDATE_CHANNEL_URL)
        # Same suite and keyring as the offline source written by finalize.
        finalize = (REPO / 'image/finalize-rootfs.sh').read_text()
        self.assertIn('Signed-By: ' + configure.ARCHIVE_KEYRING, finalize)
        self.assertIn('Suites: desos', finalize)
        self.assertIn(configure.ARCHIVE_KEYRING.rsplit('/', 1)[1], finalize)

    def test_update_channel_url_is_defined_exactly_once(self):
        host = configure.UPDATE_CHANNEL_URL.split('/')[2]
        hits = []
        for base in ('image', 'system', 'installer'):
            for path in (REPO / base).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts:
                    hits += [path] * path.read_bytes().count(host.encode())
        self.assertEqual(hits, [REPO / 'image/configure.py'])

    def test_desos_packages_are_pinned_above_the_ubuntu_base(self):
        write_configuration(self.root, self.parent)
        stanzas = [dict(line.split(': ', 1) for line in block.splitlines() if not line.startswith('Explanation:'))
                   for block in (self.root / 'etc/apt/preferences.d/desos').read_text().strip().split('\n\n')]
        self.assertEqual([s['Pin'] for s in stanzas], ['release o=DesOS', 'release o=Ubuntu'])
        self.assertEqual([s['Pin-Priority'] for s in stanzas], ['990', '-1'])
        self.assertEqual(stanzas[0]['Package'], stanzas[1]['Package'])
        patterns = stanzas[0]['Package'].split()
        import fnmatch
        for name in ('des-ui', 'des-ui-greeter', 'des-ui-music', 'des-ui-plymouth', 'desos-release',
                     'linux-image-6.18.62-desos', 'linux-headers-6.18.62-desos'):
            self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns), name)
        for name in ('linux-image-7.0.0-14-generic', 'linux-generic', 'ufw', 'calamares', 'desktop-base'):
            self.assertFalse(any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns), name)
        # The origin name is the one the signed repository metadata declares.
        self.assertIn("'Origin: DesOS'", (REPO / 'image/component_repo.py').read_text())

    def test_package_list_has_platform_packages_and_no_duplicates(self):
        names = [line.strip() for line in (REPO / 'image/packages.list').read_text().splitlines()
                 if line.strip() and not line.startswith('#')]
        self.assertEqual(len(names), len(set(names)))
        for name in ('fwupd', 'power-profiles-daemon', 'mesa-vulkan-drivers'):
            self.assertIn(name, names)
        for name in names:
            self.assertRegex(name, r'^[a-z0-9][a-z0-9+.-]+$')

    def test_live_hooks_and_installed_kernel_preference_are_explicit(self):
        write_configuration(self.root, self.parent)
        hook = self.root / 'usr/share/initramfs-tools/scripts/casper-bottom/99-desos-live'
        self.assertEqual(hook.stat().st_mode & 0o777, 0o755)
        self.assertIn('chpasswd', hook.read_text())
        self.assertEqual((self.root / 'etc/sudoers.d/90-desos-live').stat().st_mode & 0o777, 0o440)
        selection = (self.root / 'usr/lib/desos/select-upstream-kernel').read_text()
        self.assertIn('grub-set-default', selection)
        self.assertIn('-desos', selection)

    def test_active_development_credentials_are_rejected(self):
        write_configuration(self.root, self.parent)
        audit_credentials(self.root, self.parent)
        secret = self.root / 'home/des/.ssh/authorized_keys'
        secret.parent.mkdir(parents=True)
        secret.write_text('development key must not ship')
        with self.assertRaises(ValueError):
            audit_credentials(self.root, self.parent)
