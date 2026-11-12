"""Installer contract for desos-undo: btrfs by default, the subvolume layout
the tool expects, ext4 still selectable. Reads configuration only."""
from pathlib import Path
import unittest
import warnings

import yaml

from test_undo_support import REPO, desos_undo

MODULES = REPO / 'installer/calamares/modules'
# The real Calamares 3.3.14 Ubuntu package contents, when the ignored build
# state is present on this machine.
PACKAGED = REPO / 'work/package-examples/calamares/usr/lib/x86_64-linux-gnu/calamares/modules'


def conf(name):
    return yaml.safe_load((MODULES / (name + '.conf')).read_text())


def calamares_mount_options(filesystem, mount_options):
    """get_mount_options() of the packaged mount module, without disk probing."""
    chosen = next((item for item in mount_options if item['filesystem'] == filesystem), None)
    chosen = chosen or next(item for item in mount_options if item['filesystem'] == 'default')
    return ','.join(chosen.get('options', [])) or 'defaults'


class PartitionTests(unittest.TestCase):
    def test_btrfs_is_the_default_and_ext4_remains_selectable(self):
        partition = conf('partition')
        self.assertEqual(partition['defaultFileSystemType'], 'btrfs')
        self.assertEqual(partition['availableFileSystemTypes'], ['btrfs', 'ext4'])
        self.assertTrue(partition['allowManualPartitioning'])
        # Nothing is selected or erased without the user's explicit choice.
        self.assertEqual(partition['initialPartitioningChoice'], 'none')
        self.assertFalse(partition['enableLuksAutomatedPartitioning'])
        self.assertFalse(partition['lvm']['enable'])
        self.assertEqual(partition['userSwapChoices'], ['none'])

    def test_layout_follows_the_chosen_filesystem_and_keeps_boot_inside_root(self):
        layout = conf('partition')['partitionLayout']
        self.assertEqual([entry['mountPoint'] for entry in layout], ['/'])
        # "unknown" defers to defaultFileSystemType / the Erase-disk drop-down;
        # a fixed type here would silently override the user's ext4 choice.
        self.assertEqual(layout[0]['filesystem'], 'unknown')
        self.assertEqual(layout[0]['size'], '100%')


class MountTests(unittest.TestCase):
    def setUp(self):
        self.mount = conf('mount')
        self.subvolumes = {item['mountPoint']: item['subvolume'] for item in self.mount['btrfsSubvolumes']}

    def test_subvolumes_match_what_desos_undo_expects(self):
        layout = desos_undo.DEFAULTS['layout']
        shipped = (REPO / 'undo/rootfs/etc/desos-undo.conf').read_text()
        self.assertIn('root_subvolume = ' + layout['root_subvolume'], shipped)
        self.assertIn('snapshots_subvolume = ' + layout['snapshots_subvolume'], shipped)
        self.assertEqual(self.subvolumes['/'], '/' + layout['root_subvolume'])
        self.assertEqual(self.subvolumes[desos_undo.SNAPSHOTS], '/' + layout['snapshots_subvolume'])
        self.assertEqual(len(self.subvolumes), len(self.mount['btrfsSubvolumes']))
        self.assertEqual(self.mount['btrfsSubvolumes'][0]['mountPoint'], '/')

    def test_personal_files_logs_and_caches_are_outside_the_rolled_back_subvolume(self):
        for mount_point in ('/home', '/root', '/var/log', '/var/cache', '/var/tmp', '/.snapshots'):
            self.assertIn(mount_point, self.subvolumes)
        # Kernel/modules and the package database must roll back with the system.
        for inside in ('/boot', '/usr', '/etc', '/var', '/var/lib', '/opt'):
            self.assertNotIn(inside, self.subvolumes)
        names = list(self.subvolumes.values())
        self.assertEqual(len(set(names)), len(names))
        for name in names:
            self.assertRegex(name, r'^/@[a-z]*$')

    def test_fstab_entries_would_satisfy_the_runtime_probe(self):
        # The packaged fstab module writes "subvol=<subvolume>,<options>" per
        # entry; findmnt then reports FSROOT=<subvolume>, which probe() checks.
        options = calamares_mount_options('btrfs', self.mount['mountOptions'])
        self.assertEqual(options, 'defaults,noatime,compress=zstd:1')
        self.assertEqual(calamares_mount_options('ext4', self.mount['mountOptions']), 'defaults')
        self.assertEqual(calamares_mount_options('efi', self.mount['mountOptions']), 'defaults,umask=0077')
        self.assertIn('tmpOptions', conf('fstab'))

    def test_target_pseudo_filesystems_are_still_mounted(self):
        points = [item['mountPoint'] for item in self.mount['extraMounts']]
        for needed in ('/proc', '/sys', '/dev', '/run', '/run/udev', '/sys/firmware/efi/efivars'):
            self.assertIn(needed, points)

    @unittest.skipUnless((PACKAGED / 'mount/mount.schema.yaml').is_file(), 'extracted Calamares package not present')
    def test_configs_validate_against_the_packaged_3_3_14_schemas(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema is not installed')
        for name in ('mount', 'fstab'):
            schema = yaml.safe_load((PACKAGED / name / (name + '.schema.yaml')).read_text())
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)  # the packaged schemas name no draft
                jsonschema.validate(conf(name), schema)
        # The packaged module reads exactly these keys.
        source = (PACKAGED / 'mount/main.py').read_text()
        for key in ('"btrfsSubvolumes"', '"mountOptions"', 's["subvolume"]', "s['mountPoint']"):
            self.assertIn(key, source)
        plugin = (PACKAGED / 'partition/libcalamares_viewmodule_partition.so').read_bytes()
        for key in (b'defaultFileSystemType', b'availableFileSystemTypes', b'partitionLayout', b'setDefaultFsType'):
            self.assertIn(key, plugin)


if __name__ == '__main__':
    unittest.main()
