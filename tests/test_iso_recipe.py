import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class IsoRecipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('iso_recipe', ROOT / 'image/iso_recipe.py')
        cls.recipe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.recipe)

    def test_staging_paths_refuse_system_paths_symlinks_and_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root, output = base / 'rootfs', base / 'output'
            root.mkdir()
            self.assertEqual(self.recipe.validate_paths(root, output, base), (root, output))
            for badroot, badoutput in [(base, output), (pathlib.Path('/'), output), (root, root / 'out'), (root, root), (root, base.parent / 'out')]:
                with self.subTest(root=badroot, output=badoutput), self.assertRaises(ValueError):
                    self.recipe.validate_paths(badroot, badoutput, base)
            (base / 'link').symlink_to(root)
            with self.assertRaises(ValueError):
                self.recipe.validate_paths(base / 'link', output, base)

    def test_kernel_selection_requires_exact_upstream_and_matching_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / 'boot').mkdir()
            (root / 'lib/modules').mkdir(parents=True)
            versions = ['6.18.62-desos', '7.0.0-9-generic', '7.0.0-38-generic', '99.0-unrelated']
            for version in versions:
                (root / f'boot/vmlinuz-{version}').write_bytes(b'kernel')
                (root / f'lib/modules/{version}').mkdir()
            self.assertEqual(self.recipe.select_kernels(root), {'upstream': '6.18.62-desos', 'compat': '7.0.0-38-generic'})
            (root / 'lib/modules/6.18.62-desos').rmdir()
            with self.assertRaisesRegex(ValueError, 'modules'):
                self.recipe.select_kernels(root)

    def test_menu_maps_each_kernel_to_its_own_initrd_and_install_is_explicit(self):
        menu = self.recipe.grub_menu()
        self.assertIn('set default=0', menu)
        self.assertIn('menuentry "Try DesOS', menu)
        self.assertIn('menuentry "Install DesOS', menu)
        self.assertEqual(menu.count('desos.install=1'), 1)
        self.assertNotIn('ignore_uuid', menu)
        entries = menu.split('menuentry ')[1:]
        for entry in entries:
            self.assertIn('boot=casper', entry)
            self.assertIn('username=desos hostname=desos-live', entry)
            self.assertIn('console=tty0 console=ttyS0,115200n8', entry)
            kernel = 'compat' if 'vmlinuz-compat' in entry else 'upstream'
            self.assertIn(f'initrd /casper/initrd-{kernel}', entry)

    def test_boot_payload_symlinks_are_refused_before_worker_namespace_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / 'boot').mkdir()
            for version in ['6.18.62-desos', '7.0.0-38-generic']:
                (root / f'boot/vmlinuz-{version}').write_bytes(b'kernel')
                (root / f'lib/modules/{version}').mkdir(parents=True)
            image = root / 'boot/vmlinuz-6.18.62-desos'
            image.rename(root / 'boot/real-kernel')
            image.symlink_to('/boot/real-kernel')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                self.recipe.select_kernels(root)
            image.unlink()
            image.write_bytes(b'kernel')
            (root / 'boot/initrd.img-6.18.62-desos').symlink_to('/boot/worker-initrd')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                self.recipe.select_kernels(root)

    def test_uuid_is_copied_from_each_extracted_initrd(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            extraction, disk = base / 'initrd', base / '.disk'
            (extraction / 'main/conf').mkdir(parents=True)
            uuid = '12345678-abcd-4abc-8abc-123456789abc'
            (extraction / 'main/conf/uuid.conf').write_text(uuid + '\n')
            self.recipe.copy_uuid(extraction, disk, 'upstream')
            self.assertEqual((disk / 'casper-uuid-upstream').read_text(), uuid + '\n')
            (extraction / 'main/conf/uuid.conf').write_text('bad uuid\n')
            with self.assertRaises(ValueError):
                self.recipe.copy_uuid(extraction, disk, 'compat')

    def test_squash_command_excludes_pseudo_filesystems_and_uses_two_jobs(self):
        command = self.recipe.squash_command(pathlib.Path('/srv/desos-build/rootfs'), pathlib.Path('/srv/desos-build/output/iso/casper/filesystem.squashfs'))
        self.assertEqual(command[0], 'mksquashfs')
        self.assertEqual(command[command.index('-processors') + 1], '2')
        self.assertIn('-one-file-system', command)
        exclusions = command[command.index('-e') + 1:]
        for directory in ['proc', 'sys', 'dev', 'run', 'tmp']:
            self.assertIn(directory + '/*', exclusions)
        self.assertNotIn('boot', exclusions)

    def test_rescue_command_keeps_both_platforms_and_forces_bootable_mbr(self):
        command = self.recipe.rescue_command(pathlib.Path('/stage'), pathlib.Path('/output/desos.iso'))
        self.assertEqual(command[0], 'grub-mkrescue')
        self.assertNotIn('--directory', command)
        self.assertIn('--mbr-force-bootable', command)
        self.assertLess(command.index('--mbr-force-bootable'), command.index('--'))
        self.assertEqual(command[command.index('-o') + 1], '/output/desos.iso')

    def test_boot_report_rejects_missing_platform_or_hybrid_partition(self):
        report = ('El Torito boot img : 1 BIOS y none 0x0000 0x00 4 123\n'
                  'El Torito boot img : 2 UEFI y none 0x0000 0x00 5760 456\n'
                  'System area summary: MBR grub2-mbr GPT\n'
                  'MBR partition : 2 0x80 0x00 0 1\n')
        self.recipe.check_boot_report(report)
        for missing in ['BIOS', 'UEFI', 'GPT', '0x80']:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.recipe.check_boot_report(report.replace(missing, 'absent'))

    def test_host_invocation_without_explicit_paths_is_refused(self):
        result = subprocess.run(['bash', str(ROOT / 'image/build-iso.sh')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Usage:', result.stderr)


if __name__ == '__main__':
    unittest.main()
