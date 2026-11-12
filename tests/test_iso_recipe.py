import importlib.util
import pathlib
import shutil
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

    def kernel_entries(self):
        """Menu entries that load a kernel, as (header, linux argument list, body)."""
        found = []
        for entry in self.recipe.grub_menu().split('menuentry ')[1:]:
            linux = [line.strip().split() for line in entry.splitlines() if line.strip().startswith('linux ')]
            if linux:
                found.append((entry.splitlines()[0], linux[0], entry))
        return found

    def test_menu_maps_each_kernel_to_its_own_initrd_and_install_is_explicit(self):
        menu = self.recipe.grub_menu()
        self.assertIn('set default=0', menu)
        self.assertIn('set timeout=8', menu)
        self.assertIn('menuentry "Try DesOS', menu)
        self.assertIn('menuentry "Install DesOS', menu)
        self.assertEqual(menu.count('desos.install=1'), 1)
        self.assertNotIn('ignore_uuid', menu)
        entries = self.kernel_entries()
        self.assertEqual([header.split('--id ')[1].split()[0] for header, _, _ in entries],
                         ['desos-try', 'desos-install', 'desos-compat', 'desos-debug'])
        for header, linux, entry in entries:
            self.assertIn('boot=casper', linux)
            self.assertIn('live-media-path=casper', linux)
            self.assertIn('nopersistent', linux)
            self.assertIn('username=desos hostname=desos-live', entry)
            kernel = 'compat' if 'vmlinuz-compat' in entry else 'upstream'
            self.assertIn(f'initrd /casper/initrd-{kernel}', entry)
            # Every entry says what it does, in plain ASCII for the text and serial menus.
            self.assertRegex(header, r'^"[^"]+ - [a-z][^"]+" --id \S+ --class \S+ \{$')
            self.assertTrue(header.isascii())
        self.assertIn('vmlinuz-compat', entries[2][2])

    def test_normal_menu_entries_enable_plymouth_and_debug_stays_verbose(self):
        arguments = [linux for _, linux, _ in self.kernel_entries()]
        self.assertEqual(len(arguments), 4)
        for normal in arguments[:3]:
            with self.subTest(entry=normal):
                self.assertIn('quiet', normal)
                self.assertIn('splash', normal)
                # Without this Plymouth uses its text renderer whenever ttyS0 is a console.
                self.assertIn('plymouth.ignore-serial-consoles', normal)
        debug = arguments[3]
        self.assertNotIn('quiet', debug)
        self.assertNotIn('splash', debug)
        for token in ['debug', 'loglevel=7', 'systemd.show_status=1']:
            self.assertIn(token, debug)
        self.assertEqual(arguments[1], arguments[0] + ['desos.install=1'])

    def test_serial_console_is_probed_and_the_display_is_always_the_last_console(self):
        menu = self.recipe.grub_menu()
        lines = [line.strip() for line in menu.splitlines()]
        probe = lines.index('if serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1; then')
        self.assertEqual(lines[probe - 1], 'set desos_serial=')
        self.assertEqual(lines[probe + 1], 'set desos_serial=console=ttyS0,115200n8')
        # The serial terminal is only ever added inside the "UART found" guard.
        guard = lines.index('if [ -n "${desos_serial}" ]; then')
        self.assertEqual(lines[guard + 1:guard + 4], ['terminal_input --append serial', 'terminal_output --append serial', 'fi'])
        self.assertEqual(sum('serial' in line and line.startswith('terminal_') for line in lines), 2)
        for _, linux, _ in self.kernel_entries():
            consoles = [token for token in linux if 'console=' in token or token == '${desos_serial}']
            self.assertEqual(consoles[:2], ['${desos_serial}', 'console=tty0'])
            self.assertNotIn('console=ttyS0,115200n8', linux)

    def test_theme_is_loaded_with_a_text_menu_fallback_at_every_step(self):
        menu = self.recipe.grub_menu()
        lines = [line.strip() for line in menu.splitlines()]
        guard = lines.index('if [ -s /boot/grub/themes/desos/theme.txt ]; then')
        fonts = next(index for index, line in enumerate(lines) if line.startswith('if loadfont '))
        terminal = lines.index('if terminal_output gfxterm; then')
        theme = lines.index('set theme=($root)/boot/grub/themes/desos/theme.txt')
        fallback = lines.index('if [ "${desos_gfx}" != 1 ]; then')
        self.assertTrue(lines.index('set desos_gfx=0') < guard < fonts < terminal < theme < fallback)
        self.assertEqual(lines[fallback + 1], 'terminal_output console')
        self.assertLess(lines.index('search --no-floppy --file --set=root /.disk/desos-live-id'), guard)
        for name in sorted(path.name for path in self.recipe.THEME_SOURCE.glob('*.pf2')):
            self.assertIn(f'/boot/grub/themes/desos/{name}', lines[fonts])
        self.assertIn('set gfxmode=auto', lines)
        self.assertIn('set gfxpayload=keep', lines)
        self.assertIn('background_color 6,8,11', lines)
        self.assertIn('set menu_color_highlight=black/cyan', lines)
        self.assertNotIn('terminal_output console serial', menu)

    def test_disk_entry_exists_on_both_platforms_and_firmware_setup_only_on_uefi(self):
        menu = self.recipe.grub_menu()
        efi, bios = menu.split('if [ "${grub_platform}" = efi ]; then')[1].split('\nelse\n')
        self.assertEqual(efi.count('menuentry "Boot from first hard disk'), 1)
        self.assertEqual(bios.count('menuentry "Boot from first hard disk'), 1)
        self.assertIn('exit 1', efi)
        self.assertIn('if fwsetup --is-supported; then', efi)
        self.assertEqual(efi.count('menuentry "UEFI firmware settings'), 1)
        self.assertEqual(menu.count(' fwsetup\n'), 1)
        self.assertNotIn('fwsetup', bios)
        # A USB stick is hd0 itself; the installed system is then on hd1.
        self.assertIn('set desos_medium="${root}"', menu)
        self.assertIn('drivemap -s (hd0) (hd1)', bios)
        self.assertLess(bios.index('terminal_output console'), bios.index('chainloader +1'))
        self.assertNotIn('linux ', efi + bios)

    @unittest.skipUnless(shutil.which('grub-script-check'), 'grub-script-check not installed')
    def test_generated_menu_is_valid_grub_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / 'grub.cfg'
            path.write_text(self.recipe.grub_menu())
            result = subprocess.run(['grub-script-check', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_theme_is_staged_beside_the_menu_and_a_missing_theme_fails_the_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            target = self.recipe.stage_theme(base / 'stage')
            self.assertEqual(target, base / 'stage/boot/grub/themes/desos')
            source = self.recipe.THEME_SOURCE
            staged = sorted(path.relative_to(target).as_posix() for path in target.rglob('*') if path.is_file())
            self.assertEqual(staged, sorted(path.relative_to(source).as_posix() for path in source.rglob('*') if path.is_file()))
            self.assertEqual((target / 'theme.txt').read_bytes(), (source / 'theme.txt').read_bytes())
            self.assertTrue((target / 'icons/desos.png').is_file())
            for broken in ('absent', 'empty'):
                (base / 'empty').mkdir(exist_ok=True)
                with self.subTest(source=broken), self.assertRaisesRegex(ValueError, 'boot theme'):
                    self.recipe.stage_theme(base / 'other', base / broken)
            fontless = base / 'fontless'
            fontless.mkdir()
            (fontless / 'theme.txt').write_text('terminal-font: "Some Font 14"\n')
            with self.assertRaisesRegex(ValueError, 'fonts'):
                self.recipe.grub_menu(fontless)
            (base / 'link').symlink_to(source)
            with self.assertRaisesRegex(ValueError, 'boot theme'):
                self.recipe.stage_theme(base / 'other', base / 'link')

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
