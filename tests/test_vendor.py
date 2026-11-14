"""Vendor repositories (Brave, Visual Studio Code): pinned keys, wired in before apt runs."""
import pathlib
import re
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / 'image/vendor'
PINNED = {
    'brave-browser-archive-keyring.gpg': {'DBF1A116C220B8C7164F98230686B78420038257',
                                          '47D32A74E9A9E013A4B4926C68D513D36A73CD96'},
    'microsoft.gpg': {'BC528686B50D79E339D3721CEB3E94ADBE1229CF'},
}


class VendorTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('gpg'), 'gpg not installed')
    def test_keyrings_hold_exactly_the_pinned_keys(self):
        for name, fingerprints in PINNED.items():
            with self.subTest(keyring=name), tempfile.TemporaryDirectory() as home:
                out = subprocess.run(['gpg', '--no-options', '--show-keys', '--with-colons', str(VENDOR / 'keyrings' / name)],
                                     capture_output=True, text=True, check=True, env=dict(os.environ, GNUPGHOME=home)).stdout
                primary = {line.split(':')[9] for line in out.splitlines() if line.startswith('fpr:')}
                self.assertTrue(fingerprints <= primary, primary)

    def test_every_source_is_https_and_signed_by_a_shipped_keyring(self):
        sources = sorted(VENDOR.glob('*.sources'))
        self.assertEqual([s.name for s in sources], ['brave-browser.sources', 'vscode.sources'])
        for source in sources:
            with self.subTest(source=source.name):
                fields = dict(re.findall(r'^([A-Za-z-]+): *(.*)$', source.read_text(), re.M))
                self.assertTrue(fields['URIs'].startswith('https://'))
                keyring = pathlib.PurePosixPath(fields['Signed-By'])
                self.assertEqual(str(keyring.parent), '/usr/share/keyrings')
                self.assertIn(keyring.name, PINNED)

    def test_rootfs_build_installs_vendor_sources_before_the_first_update(self):
        script = (ROOT / 'image/build-rootfs.sh').read_text()
        update = script.index('apt-get -o DPkg::Lock::Timeout=300 update')
        self.assertLess(script.index('vendor/keyrings/*.gpg'), update)
        self.assertLess(script.index('vendor/*.sources'), update)
        self.assertIn("code code/add-microsoft-repo boolean false", script)

    def test_the_replaced_applications_are_gone_and_the_new_ones_listed(self):
        packages = {line.strip() for line in (ROOT / 'image/packages.list').read_text().splitlines()
                    if line.strip() and not line.startswith('#')}
        for gone in ('thunar', 'mousepad', 'epiphany-browser', 'ristretto'):
            self.assertNotIn(gone, packages)
        for wanted in ('brave-browser', 'code', 'vlc', 'cups', 'zip', 'unzip', '7zip', 'git', 'rustup'):
            self.assertIn(wanted, packages)

    def test_defaults_point_at_the_chosen_applications(self):
        text = (ROOT / 'system/rootfs/etc/xdg/mimeapps.list').read_text()
        for line in ('x-scheme-handler/https=brave-browser.desktop', 'inode/directory=des-files.desktop',
                     'video/mp4=vlc.desktop', 'text/plain=code.desktop'):
            self.assertIn(line + '\n', text)


if __name__ == '__main__':
    unittest.main()
