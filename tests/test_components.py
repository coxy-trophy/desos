"""Component installer contracts, exercised only on temp-dir fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from image import components
from image.configure import write_configuration

REPO = Path(__file__).resolve().parents[1]


class ComponentFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name)
        self.recipes = self.parent / 'recipes'
        self.root = self.parent / 'rootfs'
        (self.root / 'etc').mkdir(parents=True)
        (self.root / 'usr/lib').mkdir(parents=True)
        self.recipes.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def component(self, name, files=None, **manifest):
        directory = self.recipes / name
        directory.mkdir()
        data = {'name': name, 'packages': [], 'enable_units': [], 'notes': ''}
        data.update(manifest)
        (directory / 'component.json').write_text(json.dumps(data))
        for relative, (content, mode) in (files or {}).items():
            path = directory / 'rootfs' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(mode)
        return directory

    def install(self):
        return components.install(self.root, components.discover(self.recipes), self.parent)

    def image_files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*') if not p.is_dir())


class DiscoveryTests(ComponentFixture):
    def test_only_allow_listed_names_in_fixed_order_and_missing_ones_are_reported(self):
        self.component('phonekey', packages=['libpam-modules'])
        self.component('system', packages=['ufw'])
        (self.recipes / 'unreviewed').mkdir()
        (self.recipes / 'unreviewed/component.json').write_text('not even json')
        messages = []
        found = components.discover(self.recipes, log=messages.append)
        self.assertEqual([c.name for c in found], ['system', 'phonekey'])
        self.assertEqual(len(messages), 3)
        for name in ('undo', 'verify', 'boot-theme'):
            self.assertTrue(any(name in message and 'skipped' in message for message in messages))
        self.assertEqual(components.COMPONENTS[0], 'system')

    def test_union_keeps_order_and_drops_duplicates(self):
        self.component('system', packages=['ufw', 'apparmor'], enable_units=['ufw.service'])
        self.component('undo', packages=['apparmor', 'btrfs-progs'], enable_units=['undo.timer', 'ufw.service'])
        found = components.discover(self.recipes)
        self.assertEqual(components.union(found, 'packages'), ['ufw', 'apparmor', 'btrfs-progs'])
        self.assertEqual(components.union(found, 'units'), ['ufw.service', 'undo.timer'])

    def test_manifest_is_validated_strictly(self):
        bad = {
            'missing key': {'name': 'system', 'packages': [], 'enable_units': []},
            'extra key': {'name': 'system', 'packages': [], 'enable_units': [], 'notes': '', 'postinst': 'rm -rf /'},
            'wrong name': {'name': 'undo', 'packages': [], 'enable_units': [], 'notes': ''},
            'package option': {'name': 'system', 'packages': ['-o', 'ufw'], 'enable_units': [], 'notes': ''},
            'package version': {'name': 'system', 'packages': ['ufw=1.0'], 'enable_units': [], 'notes': ''},
            'package path': {'name': 'system', 'packages': ['/tmp/evil.deb'], 'enable_units': [], 'notes': ''},
            'package type': {'name': 'system', 'packages': 'ufw', 'enable_units': [], 'notes': ''},
            'duplicate': {'name': 'system', 'packages': ['ufw', 'ufw'], 'enable_units': [], 'notes': ''},
            'unit suffix': {'name': 'system', 'packages': [], 'enable_units': ['ufw'], 'notes': ''},
            'unit option': {'name': 'system', 'packages': [], 'enable_units': ['--now', 'ufw.service'], 'notes': ''},
            'unit path': {'name': 'system', 'packages': [], 'enable_units': ['/etc/x.service'], 'notes': ''},
            'notes type': {'name': 'system', 'packages': [], 'enable_units': [], 'notes': None},
            'not an object': ['system'],
        }
        directory = self.recipes / 'system'
        directory.mkdir()
        for label, data in bad.items():
            with self.subTest(label):
                (directory / 'component.json').write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    components.discover(self.recipes)
        (directory / 'component.json').write_text('{broken')
        with self.assertRaises(ValueError):
            components.discover(self.recipes)
        (directory / 'component.json').unlink()
        with self.assertRaises(ValueError):
            components.discover(self.recipes)

    def test_template_units_are_accepted(self):
        self.component('system', enable_units=['getty@tty1.service', 'a-b_c.timer', 'x.socket'])
        self.assertEqual(len(components.discover(self.recipes)[0].units), 3)

    def test_symlinked_component_directory_manifest_or_tree_is_refused(self):
        real = self.component('system')
        (self.recipes / 'undo').symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            components.discover(self.recipes)
        (self.recipes / 'undo').unlink()
        (real / 'rootfs').symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            components.discover(self.recipes)
        (real / 'rootfs').unlink()
        (real / 'real.json').write_text((real / 'component.json').read_text())
        (real / 'component.json').unlink()
        (real / 'component.json').symlink_to(real / 'real.json')
        with self.assertRaises(ValueError):
            components.discover(self.recipes)


class InstallTests(ComponentFixture):
    def test_copies_tree_with_convention_modes_and_returns_union(self):
        self.component('system', {'usr/sbin/tool': ('#!/bin/sh\n', 0o700), 'etc/tool.conf': ('a=1\n', 0o600)},
                       packages=['ufw'], enable_units=['ufw.service'])
        self.component('verify', {'usr/lib/desos/verify': ('#!/bin/sh\n', 0o755)}, packages=['debsums'])
        result = self.install()
        self.assertEqual((self.root / 'usr/sbin/tool').stat().st_mode & 0o7777, 0o755)
        self.assertEqual((self.root / 'etc/tool.conf').stat().st_mode & 0o7777, 0o644)
        self.assertEqual((self.root / 'usr/sbin').stat().st_mode & 0o7777, 0o755)
        self.assertEqual((self.root / 'etc/tool.conf').read_text(), 'a=1\n')
        self.assertEqual(result['packages'], ['ufw', 'debsums'])
        self.assertEqual(result['enable_units'], ['ufw.service'])
        self.assertEqual(result['components'], ['system', 'verify'])
        self.assertEqual(result['files'], 3)
        self.assertFalse(list(self.root.rglob('*.desos-new')))

    def test_reapply_is_idempotent_and_existing_directory_modes_are_kept(self):
        private = self.root / 'etc/private.d'
        private.mkdir(mode=0o700)
        private.chmod(0o700)
        self.component('system', {'etc/private.d/rule': ('x\n', 0o644)})
        self.install()
        self.install()
        self.assertEqual(private.stat().st_mode & 0o7777, 0o700)
        self.assertEqual((private / 'rule').read_text(), 'x\n')

    def test_existing_file_is_replaced_by_a_new_inode_not_written_through_a_hard_link(self):
        outside = self.parent / 'host-file'
        outside.write_text('host content')
        os.link(outside, self.root / 'etc/tool.conf')
        self.component('system', {'etc/tool.conf': ('image content', 0o644)})
        self.install()
        self.assertEqual(outside.read_text(), 'host content')
        self.assertEqual((self.root / 'etc/tool.conf').read_text(), 'image content')

    def test_nothing_is_written_when_any_component_fails_preflight(self):
        self.component('system', {'etc/good.conf': ('ok', 0o644)})
        self.component('undo', {'etc/shadow': ('root::0:0:::::', 0o644)})
        before = self.image_files()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.image_files(), before)
        self.assertFalse((self.root / 'etc/good.conf').exists())

    def test_symlinks_and_special_entries_in_a_component_tree_are_refused(self):
        directory = self.component('system', {'etc/good.conf': ('ok', 0o644)})
        (directory / 'rootfs/etc/link').symlink_to('/etc/shadow')
        with self.assertRaises(ValueError):
            self.install()
        (directory / 'rootfs/etc/link').unlink()
        (directory / 'rootfs/etc/dirlink').symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        (directory / 'rootfs/etc/dirlink').unlink()
        os.mkfifo(directory / 'rootfs/etc/pipe')
        with self.assertRaises(ValueError):
            self.install()
        (directory / 'rootfs/etc/pipe').unlink()
        os.link(directory / 'rootfs/etc/good.conf', directory / 'rootfs/etc/twin.conf')
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.root / 'etc/good.conf').exists())

    def test_symlinked_image_parents_and_leaves_are_refused(self):
        outside = self.parent / 'outside'
        outside.mkdir()
        # A merged-/usr image has /lib -> usr/lib; components must use usr/lib.
        (self.root / 'lib').symlink_to('usr/lib', target_is_directory=True)
        self.component('system', {'lib/systemd/system/x.service': ('[Unit]\n', 0o644)})
        with self.assertRaises(ValueError) as caught:
            self.install()
        self.assertIn('symlink', str(caught.exception))
        self.assertFalse((self.root / 'usr/lib/systemd').exists())

    def test_symlinked_destination_directory_and_file_cannot_escape(self):
        outside = self.parent / 'outside'
        outside.mkdir()
        (self.root / 'etc/escape').symlink_to(outside, target_is_directory=True)
        (self.root / 'etc/leaf.conf').symlink_to(outside / 'leaf.conf')
        self.component('system', {'etc/escape/file': ('x', 0o644)})
        self.component('undo', {'etc/leaf.conf': ('x', 0o644)})
        for name in ('system', 'undo'):
            with self.subTest(name), self.assertRaises(ValueError):
                found = [c for c in components.discover(self.recipes) if c.name == name]
                components.install(self.root, found, self.parent)
        self.assertEqual(list(outside.iterdir()), [])

    def test_reserved_image_state_is_refused(self):
        for index, relative in enumerate((
                'etc/passwd', 'etc/shadow', 'etc/sudoers', 'etc/sudoers.d/90-extra', 'etc/os-release',
                'usr/lib/os-release', 'etc/ssh/ssh_host_ed25519_key', 'root/.ssh/authorized_keys',
                'home/desos/.profile', 'tmp/file', 'run/file', 'dev/null', '.desos-packages-complete')):
            with self.subTest(relative):
                recipes = self.parent / f'recipes{index}'
                directory = recipes / 'system'
                (directory / 'rootfs' / relative).parent.mkdir(parents=True)
                (directory / 'rootfs' / relative).write_text('x')
                (directory / 'component.json').write_text(json.dumps(
                    {'name': 'system', 'packages': [], 'enable_units': [], 'notes': ''}))
                with self.assertRaises(ValueError) as caught:
                    components.install(self.root, components.discover(recipes), self.parent)
                self.assertIn('reserved', str(caught.exception))

    def test_destination_helper_refuses_traversal(self):
        for relative in ('../outside', '/etc/passwd', 'etc/../../outside', '.', ''):
            with self.subTest(relative), self.assertRaises(ValueError):
                components.destination(self.root, relative, 'system')

    def test_two_components_may_not_ship_the_same_path_or_disagree_about_its_type(self):
        self.component('system', {'etc/shared.conf': ('one', 0o644)})
        self.component('undo', {'etc/shared.conf': ('two', 0o644)})
        with self.assertRaises(ValueError) as caught:
            self.install()
        self.assertIn('both ship', str(caught.exception))
        self.component('verify', {'etc/shared.conf/inner': ('three', 0o644)})
        (self.recipes / 'undo/rootfs/etc/shared.conf').unlink()
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.root / 'etc/shared.conf').exists())

    def test_file_and_directory_type_conflicts_with_the_image_are_refused(self):
        (self.root / 'etc/is-dir').mkdir()
        (self.root / 'etc/is-file').write_text('x')
        self.component('system', {'etc/is-dir': ('x', 0o644)})
        self.component('undo', {'etc/is-file/inner': ('x', 0o644)})
        for name in ('system', 'undo'):
            with self.subTest(name), self.assertRaises(ValueError):
                found = [c for c in components.discover(self.recipes) if c.name == name]
                components.install(self.root, found, self.parent)

    def test_interpreter_residue_is_never_shipped(self):
        self.component('system', {'usr/lib/desos/__pycache__/x.cpython-313.pyc': ('x', 0o644),
                                  'usr/lib/desos/stray.pyc': ('x', 0o644),
                                  'usr/lib/desos/tool': ('#!/bin/sh\n', 0o755)})
        self.assertEqual(self.install()['files'], 1)
        self.assertEqual(self.image_files(), ['usr/lib/desos/tool'])

    def test_rootfs_must_be_a_real_child_of_the_build_directory(self):
        self.component('system', {'etc/good.conf': ('ok', 0o644)})
        found = components.discover(self.recipes)
        (self.parent / 'link').symlink_to(self.root, target_is_directory=True)
        for root in (self.parent, Path('/'), self.parent / 'link', self.parent / 'missing', self.root / '..' / 'rootfs'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                components.install(root, found, self.parent)
        with self.assertRaises(ValueError):
            components.install(self.root, found, Path('/srv/desos-build'))

    def test_component_without_a_tree_only_contributes_packages(self):
        self.component('system', packages=['ufw'])
        result = self.install()
        self.assertEqual((result['files'], result['packages']), (0, ['ufw']))


class RepositoryIntegrationTests(unittest.TestCase):
    def image(self, parent):
        root = parent / 'rootfs'
        (root / 'etc').mkdir(parents=True)
        (root / 'etc/debian_version').write_text('forky/sid\n')
        write_configuration(root, parent)
        return root

    def test_system_component_installs_over_the_image_configuration(self):
        system = components.load_manifest(REPO / 'system', 'system')
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = self.image(parent)
            configured = {p for p in root.rglob('*') if p.is_file()}
            result = components.install(root, [system], parent)
            self.assertGreater(result['files'], 0)
            # The component adds files; it does not replace image configuration.
            _, copies = components.plan(root, [system])
            self.assertEqual({target for _, target, _ in copies} & configured, set())

    def test_every_component_directory_present_validates_and_installs_together(self):
        # Strict on purpose: a component directory without a valid manifest, or
        # two components shipping one path, would also stop the image build.
        found = components.discover(REPO)
        self.assertEqual(found[0].name, 'system')
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = self.image(parent)
            components.install(root, found, parent)
            for path in root.rglob('*'):
                self.assertFalse(path.is_symlink(), path)

    def test_rootfs_scripts_install_component_packages_then_files_then_units(self):
        for name in ('build-rootfs.sh', 'finalize-rootfs.sh'):
            with self.subTest(name):
                text = (REPO / 'image' / name).read_text()
                packages = text.index('components.py" packages')
                units = text.index('components.py" units')
                apt = text.index('install -y --no-install-recommends')
                configure = text.index('configure.py" --root "$root"\n')
                files = text.index('components.py" install')
                enable = text.index('systemctl enable "${units[@]}"')
                self.assertLess(packages, apt)
                self.assertLess(units, apt)
                self.assertLess(apt, configure)
                self.assertLess(configure, files)
                self.assertLess(files, enable)
                # Component packages join the one reviewed package transaction.
                self.assertIn('mapfile -t -O "${#packages[@]}" packages <<< "$component_packages"', text)
                # A component conffile must not stall a non-interactive build.
                self.assertIn('--force-confold "${packages[@]}"', text)
                if 'seal.py' in text:
                    self.assertLess(enable, text.index('seal.py'))


if __name__ == '__main__':
    unittest.main()
