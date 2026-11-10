import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'image'))


class ImageSealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('image_seal', BASE / 'image/seal.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / 'rootfs'
        self.root.mkdir()
        for directory in ('etc/ssh', 'usr/lib', 'usr/sbin', 'home', 'root', 'var/lib/dbus', 'var/lib/systemd', 'var/lib/urandom', 'var/log', 'var/tmp', 'tmp', 'var/cache/apt/archives'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.put('usr/lib/os-release', 'ID=desos\nNAME=DesOS\n')
        (self.root / 'etc/os-release').symlink_to('../usr/lib/os-release')
        self.put('etc/passwd', 'root:x:0:0:root:/root:/bin/bash\n_greetd:x:998:998:greeter:/var/lib/greetd:/usr/sbin/nologin\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n')
        self.put('etc/shadow', 'root:!:1:0:99999:7:::\n')
        self.put('.desos-packages-complete', '')
        self.put('.desos-bootstrap-complete', '')
        self.put('root/.bashrc', 'normal package defaults\n')
        self.put('root/.profile', 'normal login defaults\n')
        self.put('etc/machine-id', '0123456789abcdef0123456789abcdef\n')
        self.put('var/lib/dbus/machine-id', 'old identity\n')
        self.put('var/lib/systemd/random-seed', 'seed')
        self.put('var/lib/urandom/random-seed', 'seed')
        self.put('usr/sbin/policy-rc.d', '#!/bin/sh\nexit 101\n')

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def seal(self, mountinfo=''):
        return self.module.seal_rootfs(self.root, self.parent, mountinfo=mountinfo)

    def test_seal_clears_transient_state_and_preserves_package_defaults(self):
        for relative in ('root/.bash_history', 'root/.python_history', 'root/.cache/item', 'root/.ssh/known_hosts', 'tmp/transient', 'var/tmp/item', 'var/cache/apt/archives/kernel.deb', 'var/log/installer/build.log', 'etc/ssh/ssh_host_ed25519_key', 'etc/ssh/ssh_host_ed25519_key.pub'):
            self.put(relative, 'transient')
        self.put('var/log/README', 'normal package documentation')
        self.seal()
        for relative in ('root/.bash_history', 'root/.python_history', 'root/.cache', 'root/.ssh', 'tmp/transient', 'var/tmp/item', 'var/cache/apt/archives/kernel.deb', 'usr/sbin/policy-rc.d', 'var/lib/systemd/random-seed', 'var/lib/urandom/random-seed', 'etc/ssh/ssh_host_ed25519_key', '.desos-packages-complete', '.desos-bootstrap-complete'):
            self.assertFalse((self.root / relative).exists(), relative)
        self.assertEqual((self.root / 'etc/machine-id').read_bytes(), b'')
        self.assertEqual(os.readlink(self.root / 'var/lib/dbus/machine-id'), '../../../etc/machine-id')
        self.assertEqual(os.readlink(self.root / 'etc/resolv.conf'), '../run/systemd/resolve/stub-resolv.conf')
        self.assertEqual((self.root / 'root/.bashrc').read_text(), 'normal package defaults\n')
        self.assertEqual((self.root / 'root/.profile').read_text(), 'normal login defaults\n')
        self.assertEqual((self.root / 'var/log/README').read_text(), 'normal package documentation')
        self.assertEqual((self.root / 'var/log/installer/build.log').read_bytes(), b'')
        self.assertIn('ID=desos', (self.root / 'etc/os-release').read_text())

    def test_credentials_are_refused_before_any_cleanup(self):
        for credential in ('root/.ssh/id_ed25519', 'root/.ssh/authorized_keys', 'root/subdir/private/id_rsa'):
            with self.subTest(credential=credential):
                path = self.put(credential, 'credential')
                with self.assertRaisesRegex(ValueError, 'credential'):
                    self.seal()
                self.assertTrue((self.root / 'var/lib/systemd/random-seed').exists())
                self.assertTrue((self.root / '.desos-packages-complete').exists())
                path.unlink()

    def test_child_mount_refuses_before_mutation(self):
        mountinfo = f'26 22 0:9 / {self.root}/proc rw - proc proc rw\n'
        with self.assertRaisesRegex(ValueError, 'mount'):
            self.seal(mountinfo)
        self.assertNotEqual((self.root / 'etc/machine-id').read_text(), '')
        self.assertTrue((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_user_accounts_or_nonempty_home_are_refused(self):
        original = (self.root / 'etc/passwd').read_text()
        for account in ('des:x:1000:1000:dev:/home/des:/bin/bash\n', 'desos:x:999:999:live:/home/desos:/bin/bash\n', 'buildbot:x:999:999:build:/var/lib/build:/bin/bash\n', 'alice:x:1001:1001:person:/home/alice:/bin/bash\n'):
            with self.subTest(account=account):
                self.put('etc/passwd', original + account)
                with self.assertRaisesRegex(ValueError, 'account'):
                    self.seal()
                self.assertTrue((self.root / 'var/lib/systemd/random-seed').exists())
        self.put('etc/passwd', original)
        (self.root / 'home/des').mkdir()
        with self.assertRaisesRegex(ValueError, 'home'):
            self.seal()

    def test_unlocked_root_and_missing_marker_refuse_before_mutation(self):
        self.put('etc/shadow', 'root:$6$enabled:1:0:99999:7:::\n')
        with self.assertRaisesRegex(ValueError, 'locked'):
            self.seal()
        self.put('etc/shadow', 'root:*:1:0:99999:7:::\n')
        (self.root / '.desos-packages-complete').unlink()
        with self.assertRaisesRegex(ValueError, 'packages'):
            self.seal()
        self.assertTrue((self.root / 'var/lib/systemd/random-seed').exists())

    def test_symlink_parent_escape_is_refused_before_mutation(self):
        (self.root / 'var/log').rmdir()
        external = self.parent / 'outside'
        external.mkdir()
        (external / 'important.log').write_text('do not change')
        (self.root / 'var/log').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.seal()
        self.assertEqual((external / 'important.log').read_text(), 'do not change')
        self.assertTrue((self.root / 'var/lib/systemd/random-seed').exists())

    def test_logs_do_not_follow_symlinks_or_mutate_external_hardlinks(self):
        external = self.parent / 'external.log'
        external.write_text('keep outside contents')
        (self.root / 'var/log/link.log').symlink_to(external)
        os.link(external, self.root / 'var/log/hard.log')
        self.seal()
        self.assertEqual(external.read_text(), 'keep outside contents')
        self.assertFalse((self.root / 'var/log/link.log').exists())
        self.assertEqual((self.root / 'var/log/hard.log').read_bytes(), b'')

    def test_generated_snakeoil_is_removed_but_public_trust_store_is_preserved(self):
        self.put('etc/ssl/private/ssl-cert-snakeoil.key', 'generated TLS private key')
        self.put('etc/ssl/certs/ssl-cert-snakeoil.pem', 'generated public matching certificate')
        self.put('etc/ssl/certs/ca-certificates.crt', 'normal public trust store')
        self.seal()
        self.assertFalse((self.root / 'etc/ssl/private/ssl-cert-snakeoil.key').exists())
        self.assertFalse((self.root / 'etc/ssl/certs/ssl-cert-snakeoil.pem').exists())
        self.assertEqual((self.root / 'etc/ssl/certs/ca-certificates.crt').read_text(), 'normal public trust store')

    def test_root_gnupg_private_key_is_refused_before_mutation(self):
        key = self.put('root/.gnupg/private-keys-v1.d/key.key', '(private-key secret)')
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.seal()
        self.assertTrue((self.root / 'var/lib/systemd/random-seed').exists())
        key.unlink()
        key.symlink_to('/some/unread/private-key')
        with self.assertRaisesRegex(ValueError, 'credential'):
            self.seal()
        self.assertTrue((self.root / '.desos-packages-complete').exists())

    def test_transient_special_nodes_are_unlinked_without_reading(self):
        fifo = self.root / 'tmp/stale-fifo'
        os.mkfifo(fifo)
        self.seal()
        self.assertFalse(fifo.exists())

    def test_absolute_root_and_identity_guards_precede_mutation(self):
        for root in (Path('/'), self.parent, Path('rootfs')):
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.module.seal_rootfs(root, self.parent, mountinfo='')
        self.put('usr/lib/os-release', 'ID=ubuntu\n')
        with self.assertRaisesRegex(ValueError, 'DesOS'):
            self.seal()
        self.assertTrue((self.root / 'var/lib/systemd/random-seed').exists())


if __name__ == '__main__':
    unittest.main()
