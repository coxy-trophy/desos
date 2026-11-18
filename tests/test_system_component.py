"""The `system` component: firewall, upgrades, zram and the update-channel tool.

Everything runs against temp files and a fake `ufw`; nothing touches the host
firewall, apt configuration or network.
"""
import configparser
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from image import components, configure

REPO = Path(__file__).resolve().parents[1]
SYSTEM = REPO / 'system'
TREE = SYSTEM / 'rootfs'
CONNECT = REPO.parent / 'des-ui/connect/src'
LAN = ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', 'fe80::/10']


def load_tool():
    path = TREE / 'usr/sbin/desos-update-channel'
    loader = importlib.machinery.SourceFileLoader('desos_update_channel', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # Never leave a __pycache__ inside the tree that is copied onto the image.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.component = components.load_manifest(SYSTEM, 'system')

    def test_declares_security_and_swap_packages(self):
        listed = (REPO / 'image/packages.list').read_text().split()
        for name in ('apparmor', 'ufw', 'unattended-upgrades', 'systemd-zram-generator'):
            self.assertIn(name, self.component.packages)
        # One owner per package: nothing is declared in both places.
        self.assertEqual(set(self.component.packages) & set(listed), set())

    def test_enabled_units_cover_apparmor_firewall_and_upgrades(self):
        for unit in ('apparmor.service', 'ufw.service', 'desos-firewall-setup.service',
                     'unattended-upgrades.service', 'apt-daily.timer', 'apt-daily-upgrade.timer',
                     'power-profiles-daemon.service'):
            self.assertIn(unit, self.component.units)
        # Every DesOS-owned unit that is enabled is actually shipped.
        for unit in self.component.units:
            if unit.startswith('desos-'):
                self.assertTrue((TREE / 'usr/lib/systemd/system' / unit).is_file(), unit)

    def test_tree_follows_the_component_convention(self):
        self.assertTrue((SYSTEM / 'README.md').is_file())
        directories, files = components.source_entries(self.component)
        self.assertTrue(files)
        for relative, mode in files:
            first = (TREE / relative).read_bytes()[:2]
            self.assertEqual(mode == 0o755, first == b'#!', relative)
            self.assertNotEqual(relative.parts[0], 'lib', 'use usr/lib on a merged-/usr image')


class FirewallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / 'calls'
        self.stamp = self.dir / 'state/firewall-configured'
        self.script = TREE / 'usr/lib/desos/firewall-setup'

    def tearDown(self):
        self.tmp.cleanup()

    def run_setup(self, fail_on=None):
        fake = self.dir / 'ufw'
        fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{}"\n{}'.format(
            self.log, f'case "$*" in *"{fail_on}"*) exit 1;; esac\n' if fail_on else ''))
        fake.chmod(0o755)
        env = {'DESOS_UFW': str(fake), 'DESOS_FIREWALL_STAMP': str(self.stamp), 'PATH': '/usr/bin:/bin'}
        return subprocess.run(['/bin/sh', str(self.script)], env=env, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_policy_is_default_deny_with_connect_allowed_from_lan_sources_only(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [
            'default deny incoming', 'default allow outgoing',
            *[f'allow from {source} to any app des-connect' for source in LAN],
            '--force enable',
        ])
        self.assertTrue(self.stamp.is_file())

    def test_runs_once_and_then_leaves_the_administrators_policy_alone(self):
        self.run_setup()
        self.log.unlink()
        result = self.run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_failure_leaves_no_stamp_so_the_next_boot_retries(self):
        for failing in ('default deny', 'des-connect', 'enable'):
            with self.subTest(failing):
                result = self.run_setup(fail_on=failing)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.stamp.exists())
        self.assertNotIn('--force enable', [c for c in self.calls() if 'deny' in c])

    def test_script_and_unit_are_well_formed(self):
        subprocess.run(['/bin/sh', '-n', str(self.script)], check=True)
        text = self.script.read_text()
        # No blanket allowance: every allow rule names a source network.
        for line in text.splitlines():
            if '"$ufw" allow' in line:
                self.assertIn('allow from "$source"', line)
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.optionxform = str
        unit.read(TREE / 'usr/lib/systemd/system/desos-firewall-setup.service')
        self.assertEqual(unit['Service']['ExecStart'], '/usr/lib/desos/firewall-setup')
        self.assertEqual(unit['Service']['Type'], 'oneshot')
        self.assertEqual(unit['Unit']['ConditionPathExists'], '!/var/lib/desos/firewall-configured')
        self.assertIn('/var/lib/desos/firewall-configured', text)
        self.assertEqual(unit['Unit']['Before'], 'network-pre.target')
        self.assertEqual(unit['Install']['WantedBy'], 'multi-user.target')

    def test_profile_opens_exactly_the_des_connect_ports(self):
        profile = configparser.ConfigParser(interpolation=None)
        profile.read(TREE / 'etc/ufw/applications.d/des-connect')
        self.assertEqual(profile.sections(), ['des-connect'])
        self.assertEqual(profile['des-connect']['ports'], '1716/udp|1716:1764/tcp')

    @unittest.skipUnless((CONNECT / 'service.rs').is_file(), 'DES Connect source is not beside this repository')
    def test_profile_matches_the_ports_des_connect_binds(self):
        service = (CONNECT / 'service.rs').read_text()
        transfers = (CONNECT / 'transfers.rs').read_text()
        self.assertIn('(1716..=1764).find_map(|port|TcpListener::bind(("0.0.0.0",port))', service)
        self.assertIn('("0.0.0.0",1716)', service)
        self.assertIn('for port in 1739..=1764', transfers)
        # No other non-loopback listener: every remaining bind is 127.0.0.1.
        self.assertEqual(service.count('"0.0.0.0"'), 2)
        self.assertNotIn('UNSPECIFIED', service)


class UpgradeAndSwapTests(unittest.TestCase):
    def test_unattended_upgrades_name_the_ubuntu_security_pocket_explicitly(self):
        spec = importlib.util.spec_from_file_location('release_package', REPO / 'image/release_package.py')
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        codename = re.search(r'^UBUNTU_CODENAME=(\w+)$', release.OS_RELEASE, re.MULTILINE).group(1)
        text = (TREE / 'etc/apt/apt.conf.d/52desos-unattended-upgrades').read_text()
        self.assertIn(f'"origin=Ubuntu,archive={codename}-security";', text)
        self.assertIn('"origin=DesOS,archive=desos";', text)
        self.assertIn('Unattended-Upgrade::Automatic-Reboot "false";', text)
        # distro_id would expand to DesOS and silently match no Ubuntu archive.
        code = '\n'.join(line for line in text.splitlines() if not line.startswith('//'))
        self.assertNotIn('distro_id', code)
        periodic = (TREE / 'etc/apt/apt.conf.d/21desos-auto-upgrades').read_text()
        self.assertIn('APT::Periodic::Update-Package-Lists "1";', periodic)
        self.assertIn('APT::Periodic::Unattended-Upgrade "1";', periodic)
        # The suites the pattern names are the ones the image actually configures.
        self.assertIn(f'Suites: {codename}-security', (REPO / 'image/build-rootfs.sh').read_text())

    def test_ubuntu_kernels_are_never_installed_and_firmware_still_is(self):
        import fnmatch
        text = (TREE / 'etc/apt/preferences.d/desos-kernel').read_text()
        stanza = dict(line.split(': ', 1) for line in text.splitlines() if not line.startswith('Explanation:'))
        self.assertEqual(stanza['Pin'], 'release o=Ubuntu')
        self.assertEqual(stanza['Pin-Priority'], '-1')
        patterns = stanza['Package'].split()
        blocked = lambda name: any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
        for name in ('linux-generic', 'linux-image-generic', 'linux-headers-generic', 'linux-image-7.0.0-43-generic',
                     'linux-modules-7.0.0-43-generic', 'linux-modules-extra-7.0.0-43-generic', 'linux-headers-7.0.0-43',
                     'linux-signed-generic', 'linux-generic-hwe-26.04', 'linux-virtual', 'linux-oem-24.04',
                     'linux-main-modules-zfs-7.0.0-43-generic', 'linux-main-modules-v4l2loopback-7.0.0-43-generic'):
            self.assertTrue(blocked(name), name)
        for name in ('linux-firmware', 'linux-libc-dev', 'linux-base', 'linux-tools-common', 'linux-sound-base'):
            self.assertFalse(blocked(name), name)
        # The image installs no Ubuntu kernel either.
        names = (REPO / 'image/packages.list').read_text().split()
        self.assertFalse([name for name in names if blocked(name)])

    def test_no_background_upgrades_in_the_ram_backed_live_session(self):
        for unit in ('apt-daily', 'apt-daily-upgrade'):
            text = (TREE / f'etc/systemd/system/{unit}.service.d/10-desos-live.conf').read_text()
            self.assertIn('[Unit]\nConditionKernelCommandLine=!boot=casper\n', text)
        self.assertIn('boot=casper', (REPO / 'image/iso_recipe.py').read_text())

    def test_zram_swap_is_bounded_and_uses_zstd(self):
        config = configparser.ConfigParser(interpolation=None)
        config.read(TREE / 'etc/systemd/zram-generator.conf')
        self.assertEqual(config.sections(), ['zram0'])
        zram = config['zram0']
        self.assertEqual(zram['zram-size'], 'min(ram / 2, 4096)')
        self.assertEqual(zram['compression-algorithm'], 'zstd')
        self.assertEqual(zram['fs-type'], 'swap')
        # The documented expression: 1 GiB on a 2 GB machine, never above 4 GiB.
        size = lambda ram: min(ram / 2, 4096)
        self.assertEqual((size(2048), size(4096), size(32768)), (1024, 2048, 4096))
        sysctl = dict(re.findall(r'^([a-z.-]+) = (\d+)$', (TREE / 'etc/sysctl.d/60-desos-zram.conf').read_text(), re.MULTILINE))
        self.assertEqual(sysctl, {'vm.page-cluster': '0', 'vm.swappiness': '100'})


class UpdateChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'etc/apt/sources.list.d/desos-updates.sources'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(configure.UPDATE_SOURCE)
        self.keyring = self.root / configure.ARCHIVE_KEYRING.lstrip('/')

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, url='https://packages.example.org/desos'):
        self.source.write_text(configure.UPDATE_SOURCE.replace(configure.UPDATE_CHANNEL_URL, url))
        self.keyring.parent.mkdir(parents=True, exist_ok=True)
        self.keyring.write_text('public key fixture')

    def test_tool_manages_the_file_the_image_configuration_writes(self):
        self.assertEqual(str(self.tool.SOURCE), '/etc/apt/sources.list.d/desos-updates.sources')
        state = self.tool.status(self.source)
        self.assertEqual(state, {'enabled': False, 'uri': configure.UPDATE_CHANNEL_URL, 'suite': 'desos',
                                 'signed_by': configure.ARCHIVE_KEYRING, 'published': False})

    def test_shipped_placeholder_cannot_be_enabled(self):
        self.keyring.parent.mkdir(parents=True)
        self.keyring.write_text('public key fixture')
        before = self.source.read_text()
        with self.assertRaises(ValueError) as caught:
            self.tool.set_enabled(True, self.source, self.root)
        self.assertIn('published', str(caught.exception))
        self.assertEqual(self.source.read_text(), before)

    def test_enable_and_disable_change_only_the_enabled_field(self):
        self.publish()
        before = self.source.read_text()
        self.tool.set_enabled(True, self.source, self.root)
        self.assertEqual(self.source.read_text(), before.replace('Enabled: no', 'Enabled: yes'))
        self.assertTrue(self.tool.status(self.source)['enabled'])
        self.assertTrue(self.tool.status(self.source)['published'])
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o644)
        self.tool.set_enabled(True, self.source, self.root)
        self.tool.set_enabled(False, self.source, self.root)
        self.assertEqual(self.source.read_text(), before)
        self.assertEqual([p.name for p in self.source.parent.iterdir()], [self.source.name])

    def test_enabled_field_is_added_when_absent(self):
        self.publish()
        self.source.write_text(self.source.read_text().replace('Enabled: no\n', ''))
        self.assertTrue(self.tool.status(self.source)['enabled'])  # apt's own default
        self.tool.set_enabled(False, self.source, self.root)
        self.assertFalse(self.tool.status(self.source)['enabled'])
        self.assertEqual(self.source.read_text().count('Enabled:'), 1)

    def test_enable_refuses_plain_http_missing_keyring_and_malformed_sources(self):
        self.publish('http://packages.example.org/desos')
        with self.assertRaises(ValueError):
            self.tool.set_enabled(True, self.source, self.root)
        self.publish()
        self.keyring.unlink()
        with self.assertRaises(ValueError):
            self.tool.set_enabled(True, self.source, self.root)
        self.publish()
        good = self.source.read_text()
        for label, text in {
            'second stanza': good + '\nTypes: deb\nURIs: https://evil.example\nSuites: x\nComponents: main\nSigned-By: /x\n',
            'no signature': good.replace('Signed-By: ' + configure.ARCHIVE_KEYRING + '\n', ''),
            'repeated field': good + 'URIs: https://evil.example\n',
            'garbage': good + 'not a field\n',
            'two uris': good.replace('/desos\n', '/desos https://evil.example\n'),
        }.items():
            with self.subTest(label):
                self.source.write_text(text)
                with self.assertRaises(ValueError):
                    self.tool.set_enabled(True, self.source, self.root)
                self.assertEqual(self.source.read_text(), text)
        self.assertFalse('Enabled: yes' in self.source.read_text())

    def test_symlinked_or_missing_source_is_refused(self):
        self.publish()
        real = self.source.with_name('real')
        self.source.rename(real)
        with self.assertRaises(ValueError):
            self.tool.status(self.source)
        self.source.symlink_to(real)
        with self.assertRaises(ValueError):
            self.tool.set_enabled(False, self.source, self.root)
        self.assertIn('Enabled: no', real.read_text())

    @unittest.skipIf(os.geteuid() == 0, 'checks the unprivileged refusal')
    def test_changes_require_root_and_the_tool_never_runs_apt_or_the_network(self):
        for command in ('enable', 'disable'):
            output = io.StringIO()
            with redirect_stderr(output), redirect_stdout(io.StringIO()):
                self.assertEqual(self.tool.main([command]), 1)
            self.assertIn('root required', output.getvalue())
        text = (TREE / 'usr/sbin/desos-update-channel').read_text()
        for forbidden in ('subprocess', 'socket', 'urlopen', 'http.client', 'os.system'):
            self.assertNotIn(forbidden, text)
        self.assertTrue(text.startswith('#!/usr/bin/python3 -I\n'))

    def test_installed_tool_is_executable_through_the_component_installer(self):
        parent = self.root / 'build'
        image = parent / 'rootfs'
        image.mkdir(parents=True)
        result = components.install(image, [components.load_manifest(SYSTEM, 'system')], parent)
        self.assertEqual((image / 'usr/sbin/desos-update-channel').stat().st_mode & 0o777, 0o755)
        self.assertEqual((image / 'usr/lib/desos/firewall-setup').stat().st_mode & 0o777, 0o755)
        self.assertEqual((image / 'etc/systemd/zram-generator.conf').stat().st_mode & 0o777, 0o644)
        self.assertEqual(result['packages'], json.loads((SYSTEM / 'component.json').read_text())['packages'])


if __name__ == '__main__':
    unittest.main()
