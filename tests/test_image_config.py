import json
import os
import subprocess
import sys
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
        for name in ('des-ui', 'des-ui-greeter', 'des-ui-music', 'des-ui-plymouth', 'desos-release', 'desos-verify',
                     'linux-image-6.18.62-desos', 'linux-headers-6.18.62-desos',
                     'linux-image-desos', 'linux-headers-desos'):
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

    def select(self, kernels, entries=None, top=None):
        """Run the shipped hook against a fixture /boot with stand-in GRUB tools."""
        if not (self.root / 'usr/lib/desos/select-upstream-kernel').exists():
            write_configuration(self.root, self.parent)
        boot, tools = self.parent / 'boot', self.parent / 'tools'
        (boot / 'grub').mkdir(parents=True, exist_ok=True)
        tools.mkdir(exist_ok=True)
        for old in boot.glob('vmlinuz-*'):
            old.unlink()
        for name in kernels:
            (boot / ('vmlinuz-' + name)).write_text('image')
        listed = kernels if entries is None else entries
        simple = '' if top is None else ("menuentry 'DesOS GNU/Linux' --class desos $menuentry_id_option 'gnulinux-simple-UUID' {\n"
                                         "\tlinux\t/boot/vmlinuz-%s root=UUID=UUID ro quiet splash\n}\n" % top)
        (boot / 'grub/grub.cfg').write_text(simple + "submenu 'Advanced' $menuentry_id_option 'gnulinux-advanced-UUID' {\n" + ''.join(
            "menuentry 'x' $menuentry_id_option 'gnulinux-%s-advanced-UUID' {\n}\n" % name for name in listed) + '}\n')
        (tools / 'grub-probe').write_text('#!/bin/sh\necho UUID\n')
        (tools / 'grub-set-default').write_text('#!/bin/sh\nprintf "%s" "$1" > "' + str(self.parent / 'default') + '"\n')
        for tool in tools.iterdir():
            tool.chmod(0o755)
        script = (self.root / 'usr/lib/desos/select-upstream-kernel').read_text()
        self.assertEqual(script.count("pathlib.Path('/boot')"), 1)
        script = script.replace("pathlib.Path('/boot')", 'pathlib.Path(%r)' % str(boot))
        done = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                              env={'PATH': '%s:/usr/bin:/bin' % tools})
        chosen = self.parent / 'default'
        return done, chosen.read_text() if chosen.exists() else None

    def test_kernel_hook_selects_the_newest_desos_kernel_after_an_upgrade(self):
        # One DesOS kernel beside Ubuntu's newer-numbered compatibility kernel.
        done, chosen = self.select(['6.18.62-desos', '7.0.0-14-generic'])
        self.assertEqual((done.returncode, chosen), (0, 'gnulinux-advanced-UUID>gnulinux-6.18.62-desos-advanced-UUID'))
        # linux-image-desos brought in a second one: version order, not text order.
        for newer in ('6.18.70-desos', '6.18.100-desos', '6.19.2-desos'):
            with self.subTest(newer=newer):
                done, chosen = self.select(['6.18.62-desos', newer, '6.18.9-desos', '7.0.0-14-generic', '7.0.0-15-generic'])
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(chosen, 'gnulinux-advanced-UUID>gnulinux-%s-advanced-UUID' % newer)
        # Removing the newer kernel (postrm.d) falls back to the one that is left.
        done, chosen = self.select(['6.18.62-desos', '7.0.0-15-generic'])
        self.assertEqual(chosen, 'gnulinux-advanced-UUID>gnulinux-6.18.62-desos-advanced-UUID')
        # Both hook directories run it, after update-grub has rewritten the menu.
        for stage in ('postinst.d', 'postrm.d'):
            hook = self.root / 'etc/kernel' / stage / 'zzzz-desos-select-kernel'
            self.assertIn('exec /usr/lib/desos/select-upstream-kernel', hook.read_text())
            self.assertGreater(hook.name, 'zz-update-grub')

    def test_kernel_hook_prefers_the_top_entry_when_it_starts_the_desos_kernel(self):
        # GRUB_TOP_LEVEL put the DesOS kernel on the first line: boot that line.
        done, chosen = self.select(['6.18.62-desos', '7.0.0-43-generic'], top='6.18.62-desos')
        self.assertEqual((done.returncode, chosen), (0, 'gnulinux-simple-UUID'), done.stderr)
        # The top entry starts Ubuntu's kernel (or an older DesOS one): keep the exact entry.
        for other in ('7.0.0-43-generic', '6.18.6-desos'):
            with self.subTest(top=other):
                done, chosen = self.select(['6.18.62-desos', '6.18.6-desos', '7.0.0-43-generic'], top=other)
                self.assertEqual(chosen, 'gnulinux-advanced-UUID>gnulinux-6.18.62-desos-advanced-UUID')

    def test_grub_defaults_put_the_newest_desos_kernel_on_top(self):
        write_configuration(self.root, self.parent)
        cfg = (self.root / 'etc/default/grub.d/10-desos.cfg').read_text()
        self.assertIn('GRUB_DEFAULT=saved', cfg)
        boot = self.parent / 'topboot'
        boot.mkdir()
        for name in ('6.18.9-desos', '6.18.62-desos', '7.0.0-43-generic'):
            (boot / ('vmlinuz-' + name)).write_text('image')
        shown = subprocess.run(['sh', '-c', cfg.replace('/boot/', str(boot) + '/') + '\nprintf %s "$GRUB_TOP_LEVEL"'],
                               capture_output=True, text=True, check=True).stdout
        self.assertEqual(shown, str(boot / 'vmlinuz-6.18.62-desos'))
        empty = self.parent / 'noboot'
        empty.mkdir()
        shown = subprocess.run(['sh', '-c', cfg.replace('/boot/', str(empty) + '/') + '\nprintf %s "${GRUB_TOP_LEVEL-unset}"'],
                               capture_output=True, text=True, check=True).stdout
        self.assertEqual(shown, 'unset')

    def test_grub_defaults_add_splash_without_dropping_resume(self):
        write_configuration(self.root, self.parent)
        cfg = (self.root / 'etc/default/grub.d/10-desos.cfg').read_text()
        self.assertNotIn('GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"', cfg)
        for start, want in (('', 'quiet splash'), ('quiet resume=UUID=abc', 'quiet resume=UUID=abc splash'),
                            ('quiet splash resume=UUID=abc', 'quiet splash resume=UUID=abc')):
            shown = subprocess.run(['sh', '-c', 'GRUB_CMDLINE_LINUX_DEFAULT="%s"\n%s\nprintf %%s "$GRUB_CMDLINE_LINUX_DEFAULT"' % (start, cfg)],
                                   capture_output=True, text=True, check=True).stdout
            self.assertEqual(shown, want)

    def test_kernel_hook_refuses_a_menu_without_the_new_kernel(self):
        done, chosen = self.select(['6.18.62-desos', '6.18.70-desos'], entries=['6.18.62-desos'])
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('menu ID missing', done.stderr)

    def test_image_installs_the_kernel_through_its_metapackages(self):
        finalize = (REPO / 'image/finalize-rootfs.sh').read_text()
        self.assertIn('kernel_metapackage.py" names --pin "$recipes/kernel/source-pin.json"', finalize)
        self.assertIn('"${desos_packages[@]}"', finalize)
        # No versioned kernel package is named by hand any more.
        self.assertNotRegex(finalize, r'linux-(image|headers)-[0-9][^ ]*-desos=')
        pin = json.loads((REPO / 'kernel/source-pin.json').read_text())
        done = subprocess.run([sys.executable, str(REPO / 'image/kernel_metapackage.py'), 'names'],
                              capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.split(), ['linux-image-desos=' + pin['package_version'],
                                               'linux-headers-desos=' + pin['package_version']])

    def test_build_evidence_is_installed_from_the_signed_repository_and_the_image_is_verified(self):
        finalize = (REPO / 'image/finalize-rootfs.sh').read_text()
        self.assertIn('evidence=$repo/build-evidence', finalize)
        self.assertIn('sha256sum --check --strict --quiet SHA256SUMS', finalize)
        self.assertIn('"$evidence/$name" "$root/usr/share/doc/desos/build/$name"', finalize)
        self.assertNotIn('cp "$provenance/', finalize)
        gate = finalize.index('"$root/usr/bin/desos-verify" --root "$root" --quick --json')
        self.assertLess(finalize.index('components.py" install'), gate)
        self.assertLess(gate, finalize.index('seal.py'))
        # The worker verifies; it never signs.
        for script in ('finalize-rootfs.sh', 'build-rootfs.sh', 'build-iso.sh'):
            text = (REPO / 'image' / script).read_text()
            self.assertNotRegex(text, r'(?m)(^|[\s"(])gpg\s|--detach-sign|--clearsign|--local-user|\.private')
        self.assertEqual(subprocess.run(['bash', '-n', str(REPO / 'image/finalize-rootfs.sh')]).returncode, 0)
        self.assertEqual(subprocess.run(['bash', '-n', str(REPO / 'image/build-rootfs.sh')]).returncode, 0)

    def test_active_development_credentials_are_rejected(self):
        write_configuration(self.root, self.parent)
        audit_credentials(self.root, self.parent)
        secret = self.root / 'home/des/.ssh/authorized_keys'
        secret.parent.mkdir(parents=True)
        secret.write_text('development key must not ship')
        with self.assertRaises(ValueError):
            audit_credentials(self.root, self.parent)
