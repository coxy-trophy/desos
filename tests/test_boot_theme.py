"""The boot-theme component: theme contents, GRUB integration files and generator."""
import importlib.util
import os
import pathlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

from image import components

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'boot-theme'
TREE = COMPONENT / 'rootfs'
THEME = TREE / 'boot/grub/themes/desos'
FRAGMENT = TREE / 'etc/default/grub.d/20-desos-theme.cfg'
SCRIPT = TREE / 'etc/grub.d/06_desos_theme'
# Classes other components and grub-mkconfig put on entries; each needs artwork.
CLASSES = ['desos', 'gnu-linux', 'install', 'compat', 'debug', 'recovery', 'disk', 'firmware',
           'desos-snapshot', 'desos-snapshots', 'desos-known-good', 'desos-rollback', 'submenu', 'memtest']


def png_size(path):
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise AssertionError(f'{path.name} is not a PNG')
    return struct.unpack('>II', data[16:24])


class BootThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (THEME / 'theme.txt').read_text()
        spec = importlib.util.spec_from_file_location('boot_theme_generate', COMPONENT / 'generate.py')
        cls.generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.generator)

    def block(self, kind, identifier=None):
        """Properties of the first theme component of a kind (and id)."""
        for match in re.finditer(r'^\+ (\w+) \{\n(.*?)^\}', self.text, re.M | re.S):
            properties = dict(re.findall(r'^\s+(\w+) = "?([^"\n]*)"?$', match[2], re.M))
            if match[1] == kind and (identifier is None or properties.get('id') == identifier):
                return properties
        self.fail(f'theme has no {kind} {identifier or ""}')

    def test_component_manifest_and_tree_are_accepted_by_the_installer(self):
        component = components.load_manifest(COMPONENT, 'boot-theme')
        self.assertEqual(component.units, [])
        self.assertIn('grub-common', component.packages)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            _, copies = components.plan(root, [component])
            modes = {target.relative_to(root).as_posix(): mode for _, target, mode in copies}
        self.assertEqual(modes['etc/grub.d/06_desos_theme'], 0o755)
        self.assertEqual(modes['etc/default/grub.d/20-desos-theme.cfg'], 0o644)
        self.assertEqual(modes['boot/grub/themes/desos/theme.txt'], 0o644)
        self.assertTrue(all(path.startswith(('boot/grub/themes/desos/', 'etc/')) for path in modes))
        self.assertFalse(any(path.is_symlink() for path in TREE.rglob('*')))

    def test_theme_uses_the_instrument_palette_and_only_bundled_fonts(self):
        self.assertIn('desktop-color: "#06080b"', self.text)
        for colour in ('#4fd8ce', '#dce7ec', '#8fa3b0'):
            self.assertIn(colour, self.text)
        named = set(re.findall(r'(?:font|terminal-font|message-font)(?: =|:) "([^"]+)"', self.text))
        self.assertTrue(any(name.startswith('Chakra Petch') for name in named))
        self.assertTrue(any(name.startswith('JetBrains Mono') for name in named))
        fonts = sorted(THEME.glob('*.pf2'))
        loaded = [font.read_bytes()[:512] for font in fonts]
        for name in named:
            with self.subTest(font=name):
                self.assertTrue(any(name.encode() + b'\0' in data for data in loaded))
        for font in fonts:
            self.assertEqual(font.read_bytes()[:12], b'FILE\0\0\0\x04PFF2')
        for licence in ('ChakraPetch-OFL.txt', 'JetBrainsMono-OFL.txt'):
            self.assertEqual((THEME / licence).read_bytes(), (ROOT / 'installer/fonts' / licence).read_bytes())

    def test_every_image_the_theme_names_is_shipped(self):
        for name in re.findall(r'file = "([^"]+)"', self.text):
            self.assertTrue((THEME / name).is_file(), name)
        patterns = re.findall(r'"([a-z_]+)_\*\.png"', self.text)
        self.assertEqual(sorted(patterns), ['bar', 'bar_fill', 'scroll_frame', 'scroll_thumb', 'select', 'terminal'])
        for pattern in patterns:
            self.assertTrue((THEME / f'{pattern}_c.png').is_file(), pattern)
        for stem in ('select', 'terminal'):
            for part in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
                self.assertTrue((THEME / f'{stem}_{part}.png').is_file(), f'{stem}_{part}')
        shipped = {path.name for path in THEME.glob('*.png')}
        used = {name for name in shipped if name == 'wordmark.png' or name == 'line.png'
                or any(name.startswith(pattern + '_') for pattern in patterns)}
        self.assertEqual(shipped, used)

    def test_menu_is_keyboard_only_with_selection_countdown_and_scrolling(self):
        menu = self.block('boot_menu')
        self.assertEqual(menu['selected_item_pixmap_style'], 'select_*.png')
        self.assertNotEqual(menu['item_color'], menu['selected_item_color'])
        self.assertEqual(menu['scrollbar'], 'true')
        self.assertEqual(menu['scrollbar_slice'], 'center')
        self.assertEqual(int(menu['icon_width']), int(menu['icon_height']))
        self.assertGreaterEqual(int(menu['item_height']), int(menu['icon_height']) + 8)
        # A fixed column no wider than the 800x600 a BIOS without display data uses.
        self.assertLessEqual(int(menu['width']), 800)
        self.assertEqual(menu['left'], '50%%-%d' % (int(menu['width']) // 2))
        readout = self.block('label', '__timeout__')
        self.assertIn('%d', readout['text'])
        meter = self.block('progress_bar', '__timeout__')
        self.assertEqual(meter['bar_style'], 'bar_*.png')
        self.assertEqual(meter['left'], menu['left'])
        legend = ' '.join(re.findall(r'text = "([^"]*)"', self.text))
        for key in ('ENTER', 'E edit', 'C console', 'ESC back'):
            self.assertIn(key, legend)
        self.assertIn('terminal-box: "terminal_*.png"', self.text)

    def test_documented_entry_classes_have_icons_of_the_menu_size(self):
        size = int(self.block('boot_menu')['icon_width'])
        readme = (COMPONENT / 'README.md').read_text()
        for name in CLASSES:
            with self.subTest(icon=name):
                self.assertEqual(png_size(THEME / 'icons' / f'{name}.png'), (size, size))
                self.assertIn(f'`{name}`', readme)
        for icon in (THEME / 'icons').iterdir():
            self.assertEqual(png_size(icon), (size, size))

    def test_grub_default_fragment_points_at_files_the_component_ships(self):
        text = FRAGMENT.read_text()
        settings = dict(re.findall(r'^([A-Z_]+)=(.*)$', text, re.M))
        self.assertEqual(sorted(settings), ['GRUB_FONT', 'GRUB_GFXMODE', 'GRUB_THEME'])
        self.assertEqual(settings['GRUB_THEME'], '/boot/grub/themes/desos/theme.txt')
        self.assertEqual(settings['GRUB_GFXMODE'], 'auto')
        for key in ('GRUB_THEME', 'GRUB_FONT'):
            # Beside grub.cfg, so GRUB can read it before any root filesystem is mounted.
            self.assertTrue(settings[key].startswith('/boot/grub/'))
            self.assertTrue((TREE / settings[key].lstrip('/')).is_file())
        self.assertEqual(subprocess.run(['sh', '-n', str(FRAGMENT)]).returncode, 0)

    def test_installer_no_longer_forces_the_console_terminal(self):
        conf = (ROOT / 'installer/calamares/modules/grubcfg.conf').read_text()
        self.assertFalse(re.search(r'^\s*GRUB_TERMINAL', conf, re.M))
        self.assertFalse(re.search(r'^\s*GRUB_THEME', conf, re.M))
        self.assertIn('always_use_defaults: true', conf)

    def run_script(self, **environment):
        result = subprocess.run(['sh', str(SCRIPT)], capture_output=True, text=True,
                                env={'PATH': os.environ['PATH'], **environment})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_grub_d_script_sets_fallback_colours_and_the_splash_ground(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertEqual(subprocess.run(['sh', '-n', str(SCRIPT)]).returncode, 0)
        graphical = self.run_script()
        self.assertIn('set menu_color_highlight=black/cyan', graphical)
        self.assertIn('background_color 6,8,11', graphical)
        self.assertIn('if [ -s $prefix/fonts/unicode.pf2 ]; then', graphical)
        self.assertEqual(graphical, self.run_script(GRUB_TERMINAL_OUTPUT='gfxterm serial'))
        text = self.run_script(GRUB_TERMINAL_OUTPUT='console')
        self.assertIn('set menu_color_normal=light-gray/black', text)
        self.assertNotIn('background_color', text)
        self.assertNotIn('loadfont', text)

    @unittest.skipUnless(shutil.which('grub-script-check'), 'grub-script-check not installed')
    def test_grub_d_script_output_is_valid_grub_script(self):
        for environment in ({}, {'GRUB_TERMINAL_OUTPUT': 'console'}):
            with tempfile.TemporaryDirectory() as temporary:
                path = pathlib.Path(temporary) / 'section.cfg'
                path.write_text(self.run_script(**environment))
                result = subprocess.run(['grub-script-check', str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_committed_theme_text_and_file_list_match_the_generator(self):
        generator = self.generator
        self.assertEqual(self.text, generator.theme_text())
        expected = {'theme.txt', 'wordmark.png', 'line.png', 'ChakraPetch-OFL.txt', 'JetBrainsMono-OFL.txt'}
        expected |= {f'{family} {size}'.lower().replace(' ', '_') + '.pf2' for _, size, family in generator.FONT_FILES.values()}
        expected |= {f'icons/{name}.png' for icon, aliases in generator.ICON_ALIASES.items() for name in [icon, *aliases]}
        shipped = {path.relative_to(THEME).as_posix() for path in THEME.rglob('*') if path.is_file()}
        self.assertLessEqual(expected, shipped)
        self.assertEqual({name for name in shipped - expected if not re.fullmatch(r'[a-z_]+_(c|[ns]?[ew]?)\.png', name)}, set())
        self.assertEqual(generator.hexa(generator.GROUND), '#06080b')
        self.assertEqual(generator.hexa(generator.ACCENT), '#4fd8ce')


if __name__ == '__main__':
    unittest.main()
