"""desos-dev and its root helper: versions, the channel's source and pruning."""
import datetime
import importlib.machinery
import importlib.util
from pathlib import Path
import shutil
import subprocess
import unittest

TREE = Path(__file__).resolve().parents[1] / 'system/rootfs'


def load(relative, name):
    loader = importlib.machinery.SourceFileLoader(name, str(TREE / relative))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dev = load('usr/bin/desos-dev', 'desos_dev')
channel = load('usr/sbin/desos-dev-channel', 'desos_dev_channel')
HAS_DPKG = shutil.which('dpkg') is not None


def newer(a, b):
    return subprocess.run(['dpkg', '--compare-versions', a, 'gt', b]).returncode == 0


class DevVersionTests(unittest.TestCase):
    def test_version_names_the_time_and_commit(self):
        stamp = datetime.datetime(2026, 11, 14, 9, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(dev.dev_version('0.2.0', stamp, 'abcdef1234567890'), '0.2.0+dev202611140930.gabcdef123')

    @unittest.skipUnless(HAS_DPKG, 'dpkg not installed')
    def test_dev_builds_sort_above_the_release_and_below_the_next(self):
        early = dev.dev_version('0.2.0', datetime.datetime(2026, 11, 14, 9, 30), 'a' * 40)
        later = dev.dev_version('0.2.0', datetime.datetime(2026, 11, 14, 9, 31), '0' * 40)
        self.assertTrue(newer(early, '0.2.0'))
        self.assertTrue(newer(later, early), 'the time decides, not the commit')
        self.assertTrue(newer('0.2.1', later))

    def test_cargo_version_is_read_from_the_shell_crate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'shell').mkdir()
            (Path(directory) / 'shell/Cargo.toml').write_text('[package]\nname = "des-ui"\nversion = "0.2.0"\n')
            self.assertEqual(dev.cargo_version(Path(directory)), '0.2.0')


class ChannelTests(unittest.TestCase):
    def test_source_is_the_local_flat_repository_signed_by_its_own_key(self):
        text = channel.source_text()
        self.assertIn('URIs: file:/var/lib/desos-dev/channel/\n', text)
        self.assertIn('Suites: ./\n', text)
        self.assertIn('Signed-By: /usr/share/keyrings/desos-dev.gpg\n', text)

    def test_only_des_ui_packages_are_accepted(self):
        for good in ('des-ui_0.2.0+dev202611140930.gabc_amd64.deb', 'des-ui-music_0.2.0_amd64.deb'):
            self.assertTrue(channel.PACKAGE.match(good), good)
        for bad in ('linux-image_1_amd64.deb', 'des-ui_0.2.0_amd64.deb.sig', '../des-ui_0.2.0_amd64.deb', 'sudo_1_amd64.deb'):
            self.assertFalse(channel.PACKAGE.match(bad), bad)

    def test_release_assets_get_names_github_keeps(self):
        self.assertEqual(channel.asset_name('des-ui_0.2.0+dev202611140930.gabc_amd64.deb'),
                         'des-ui_0.2.0_dev202611140930.gabc_amd64.deb')
        self.assertEqual(channel.asset_name('des-ui_1:0.2~rc1_amd64.deb'), 'des-ui_1_0.2_rc1_amd64.deb')
        self.assertEqual(channel.flat_filenames(b'Package: des-ui\nFilename: ./des-ui_0.2.0_amd64.deb\nSize: 1\n'),
                         b'Package: des-ui\nFilename: des-ui_0.2.0_amd64.deb\nSize: 1\n')

    def test_follow_defaults_to_the_desos_updates_release(self):
        self.assertEqual((dev.RELEASES, dev.TAG), ('coxy-trophy/desos', 'updates'))

    @unittest.skipUnless(HAS_DPKG, 'dpkg not installed')
    def test_prune_keeps_the_newest_three_builds_of_each_package(self):
        names = [f'des-ui_0.2.0+dev20261114093{i}.gabc_amd64.deb' for i in range(5)] + ['des-ui-music_0.2.0_amd64.deb']
        self.assertEqual(channel.prune(names), names[:2])


if __name__ == '__main__':
    unittest.main()
