import tempfile
import unittest
from pathlib import Path

from image.configure import write_configuration, audit_credentials


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
        identity = (self.root / 'usr/share/desos/os-release').read_text()
        casper = (self.root / 'etc/casper.conf').read_text()
        self.assertIn('ID=desos\n', identity)
        self.assertIn('ID_LIKE="ubuntu debian"', identity)
        self.assertIn('USERNAME="desos"', casper)
        self.assertIn('FLAVOUR="DesOS"', casper)
        self.assertIn('BUILD_SYSTEM="Ubuntu"', casper)
        self.assertIn('initial_session', (self.root / 'etc/greetd/config.toml').read_text())

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
