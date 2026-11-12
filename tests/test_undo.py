"""desos-undo: snapshots, transactions, diff, rollback, prune and degradation.

Host-safe: every btrfs/mount/findmnt call is answered by the fake machine in
test_undo_support; no real filesystem, package manager or boot loader is used.
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest

from test_undo_support import Machine, REPO, ROOTFS, UNDO, desos_undo

APT_V3 = '''VERSION 3
APT::Architecture=amd64
DPkg::Options::=--force-confold

htop - - none < 3.3.0-4 amd64 none /var/cache/apt/archives/htop_3.3.0-4_amd64.deb
nano 7.2-1 amd64 none < 8.0-1 amd64 none /var/cache/apt/archives/nano_8.0-1_amd64.deb
bash 5.2-1 amd64 none > - - none **REMOVE**
htop - - none < 3.3.0-4 amd64 none **CONFIGURE**
'''
STATUS_AFTER = '''Package: nano
Status: install ok installed
Architecture: amd64
Version: 8.0-1

Package: htop
Status: install ok installed
Architecture: amd64
Version: 3.3.0-4
'''


class Base(unittest.TestCase):
    firmware, fstype, live = 'efi', 'btrfs', False

    def setUp(self):
        self.m = Machine(self.firmware, self.fstype, self.live)
        self.addCleanup(self.m.close)

    def info(self, identifier):
        return json.loads((self.m.top / '@snapshots' / str(identifier) / 'info.json').read_text())


class DegradeTests(unittest.TestCase):
    def check(self, machine, code):
        self.addCleanup(machine.close)
        exit_code, status = machine.json('status')
        self.assertEqual((exit_code, status['ok'], status['supported'], status['code']), (0, True, False, code))
        self.assertTrue(status['message'])
        for command in (['list'], ['snapshot', '--reason', 'x'], ['diff', '1'], ['rollback', '1'], ['prune'], ['notices'],
                        ['boot-start'], ['boot-good', '--now'], ['grub-refresh']):
            exit_code, result = machine.json(*command)
            self.assertEqual((exit_code, result['supported'], result['code']), (0, False, code), command)
            self.assertEqual(machine.cli(*command, '--strict')[0], 0 if command[0].startswith('boot-') else 3, command)
            self.assertIn(result['message'], machine.cli(*command)[1])
        # Package-manager hooks say nothing and never fail a transaction.
        for hook in ('apt-pre', 'apt-post', 'dpkg-pre', 'dpkg-post'):
            self.assertEqual(machine.cli(hook, stdin=APT_V3), (0, ''))
        # A wrapped command still runs and its exit status is passed through.
        machine.execute_result = 7
        exit_code, result = machine.json('run', '--reason', 'tune', '--', 'true', '--flag')
        self.assertEqual((exit_code, result['data']['exit_code'], result['data']['pre_id']), (7, 7, None))
        self.assertEqual(machine.executed, [['true', '--flag']])
        self.assertFalse(any(call[0] in ('btrfs', 'mount') for call in machine.calls))
        self.assertEqual(list((machine.top / '@snapshots').iterdir()), [])

    def test_live_session_reports_and_changes_nothing(self):
        self.check(Machine(live=True), 'live-session')

    def test_ext4_root_reports_and_changes_nothing(self):
        self.check(Machine(fstype='ext4'), 'not-btrfs')

    def test_foreign_btrfs_layout_and_missing_tool_are_unsupported(self):
        machine = Machine()
        self.addCleanup(machine.close)
        machine.mounts['/'].update(fsroot='/', source='/dev/vda2')
        self.assertEqual(machine.json('status')[1]['code'], 'layout-unsupported')
        machine.mounts['/'].update(fsroot='/@')
        del machine.mounts['/.snapshots']
        self.assertEqual(machine.json('snapshot', '--reason', 'x')[1]['code'], 'layout-unsupported')
        other = Machine()
        self.addCleanup(other.close)
        host = other.new_host()
        host.which = lambda name: None
        self.assertEqual(desos_undo.probe(host, desos_undo.load_config(host)).code, 'tools-missing')

    def test_mutating_commands_need_root(self):
        machine = Machine()
        self.addCleanup(machine.close)
        machine.euid = 1000
        exit_code, result = machine.json('snapshot', '--reason', 'x')
        self.assertEqual((exit_code, result['ok'], result['code']), (1, False, 'not-root'))
        self.assertEqual(machine.json('list')[0], 0)
        self.assertEqual(machine.cli('apt-pre', stdin=APT_V3), (0, ''))
        self.assertEqual(machine.snapshot_ids(), [])


class SnapshotTests(Base):
    def test_snapshot_list_and_metadata(self):
        exit_code, result = self.m.json('snapshot', '--reason', "before I try 'things'")
        self.assertEqual((exit_code, result['ok'], result['command'], result['schema']), (0, True, 'snapshot', 1))
        info = result['data']['snapshot']
        self.assertEqual((info['id'], info['kind'], info['kernel']), (1, 'manual', '6.18.62-desos'))
        self.assertEqual(info['boot']['kernels'], ['6.18.62-desos', '7.0.0-12-generic'])
        self.assertIn(['btrfs', 'subvolume', 'snapshot', '-r', str(self.m.root), str(self.m.root / '.snapshots/1/snapshot')], self.m.calls)
        self.m.json('snapshot', '--reason', 'second')
        listed = self.m.json('list')[1]['data']['snapshots']
        self.assertEqual([item['id'] for item in listed], [1, 2])
        self.assertEqual(listed[0], self.info(1))
        self.assertIn('before I try', self.m.cli('list')[1])
        # Unprivileged callers (the desktop) can read list and status.
        self.assertEqual(stat.S_IMODE((self.m.top / '@snapshots/1/info.json').stat().st_mode), 0o644)
        self.m.euid = 1000
        self.assertEqual(len(self.m.json('list')[1]['data']['snapshots']), 2)
        self.assertEqual(self.m.json('status')[1]['data']['snapshots'], {'count': 2, 'newest': 2})

    def test_ids_are_never_reused_and_failed_snapshot_leaves_nothing(self):
        self.m.json('snapshot', '--reason', 'a')
        self.m.json('snapshot', '--reason', 'b')
        self.m.host.run(['btrfs', 'subvolume', 'delete', self.m.top / '@snapshots/2/snapshot'])
        (self.m.top / '@snapshots/2/info.json').unlink()
        (self.m.top / '@snapshots/2').rmdir()
        self.m.fail.add('btrfs subvolume snapshot')
        exit_code, result = self.m.json('snapshot', '--reason', 'c')
        self.assertEqual((exit_code, result['ok'], result['code']), (1, False, 'command-failed'))
        self.m.fail.clear()
        self.assertEqual(self.m.json('snapshot', '--reason', 'd')[1]['data']['snapshot']['id'], 4)
        self.assertEqual(self.m.snapshot_ids(), [1, 4])

    def test_unknown_snapshot_is_a_clear_error(self):
        for command in (['diff', '9'], ['rollback', '9']):
            exit_code, result = self.m.json(*command)
            self.assertEqual((exit_code, result['code']), (1, 'no-such-snapshot'))
        self.assertFalse((self.m.top / '@.desos-undo-new').exists())


class TransactionTests(Base):
    def test_apt_transaction_gets_pre_and_post_with_package_summary(self):
        self.assertEqual(self.m.cli('apt-pre', stdin=APT_V3), (0, ''))
        pre = self.info(1)
        self.assertEqual(pre['kind'], 'pre')
        self.assertEqual(pre['planned']['installed'], [{'name': 'htop', 'version': '3.3.0-4'}])
        self.assertEqual(pre['planned']['upgraded'], [{'name': 'nano', 'from': '7.2-1', 'to': '8.0-1'}])
        self.assertEqual(pre['planned']['removed'], [{'name': 'bash', 'version': '5.2-1'}])
        self.assertIn('installed htop', pre['reason'])
        # apt drives dpkg several times; those hooks must not add snapshots.
        self.assertEqual(self.m.cli('dpkg-pre'), (0, ''))
        self.assertEqual(self.m.cli('dpkg-post'), (0, ''))
        self.assertEqual(self.m.snapshot_ids(), [1])
        self.m.write('var/lib/dpkg/status', STATUS_AFTER)
        self.assertEqual(self.m.cli('apt-post'), (0, ''))
        post = self.info(2)
        self.assertEqual((post['kind'], post['pre_id']), ('post', 1))
        self.assertEqual(post['packages'], {
            'installed': [{'name': 'htop', 'version': '3.3.0-4'}], 'removed': [{'name': 'bash', 'version': '5.2-1'}],
            'upgraded': [{'name': 'nano', 'from': '7.2-1', 'to': '8.0-1'}], 'downgraded': []})
        self.assertEqual(self.info(1)['post_id'], 2)
        self.assertEqual(self.info(1)['packages'], post['packages'])
        self.assertIsNone(self.m.json('status')[1]['data']['transaction'])
        self.assertEqual(self.m.cli('apt-post'), (0, ''))
        self.assertEqual(self.m.snapshot_ids(), [1, 2])

    def test_apt_protocol_version_2_and_garbage(self):
        plan = desos_undo.parse_apt_plan('VERSION 2\nA=b\n\nvim - < 9.1-1 /x/vim.deb\nold 1.0 > - **REMOVE**\njunk\n')
        self.assertEqual(plan['installed'], [{'name': 'vim', 'version': '9.1-1'}])
        self.assertEqual(plan['removed'], [{'name': 'old', 'version': '1.0'}])
        self.assertEqual(desos_undo.parse_apt_plan('/var/cache/apt/archives/a.deb\n'), {'installed': [], 'upgraded': [], 'downgraded': [], 'removed': []})

    def test_direct_dpkg_is_bracketed_and_noop_invocations_leave_nothing(self):
        self.m.cli('dpkg-pre')
        self.assertEqual(self.m.snapshot_ids(), [1])
        self.m.cli('dpkg-post')
        self.assertEqual(self.m.snapshot_ids(), [])
        self.m.cli('dpkg-pre')
        self.m.write('var/lib/dpkg/status', STATUS_AFTER)
        self.m.cli('dpkg-post')
        ids = self.m.snapshot_ids()
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.info(ids[1])['packages']['installed'][0]['name'], 'htop')

    def test_hook_failure_never_fails_the_package_manager(self):
        self.m.fail.add('btrfs subvolume snapshot')
        self.assertEqual(self.m.cli('apt-pre', stdin=APT_V3)[0], 0)
        self.assertEqual(self.m.cli('apt-post')[0], 0)
        self.assertEqual(self.m.cli('dpkg-pre')[0], 0)
        self.m.environ['DESOS_UNDO_DISABLE'] = '1'
        self.m.fail.clear()
        self.m.cli('apt-pre', stdin=APT_V3)
        self.assertEqual(self.m.snapshot_ids(), [])

    def test_run_wraps_a_command_and_covers_nested_package_hooks(self):
        (self.m.root / 'proc' / str(os.getpid())).mkdir()

        def command_body():
            # apt started by the wrapped command: its hooks must defer to run.
            self.m.cli('apt-pre', stdin=APT_V3)
            self.m.write('var/lib/dpkg/status', STATUS_AFTER)
            self.m.cli('apt-post')

        self.m.execute_hook = command_body
        self.m.execute_result = 3
        result_file = self.m.base / 'result.json'
        exit_code, result = self.m.json('run', '--reason', 'install htop for the user', '--result-file', str(result_file),
                                        '--', 'apt-get', 'install', '-y', 'htop')
        self.assertEqual(exit_code, 3)
        self.assertEqual(self.m.executed, [['apt-get', 'install', '-y', 'htop']])
        self.assertEqual((result['data']['pre_id'], result['data']['post_id'], result['data']['exit_code']), (1, 2, 3))
        self.assertEqual(self.m.snapshot_ids(), [1, 2])
        self.assertEqual(self.info(1)['command'], ['apt-get', 'install', '-y', 'htop'])
        self.assertEqual(self.info(1)['reason'], 'before: install htop for the user')
        self.assertEqual((self.info(2)['exit_code'], self.info(2)['reason']), (3, 'after: install htop for the user'))
        self.assertEqual(self.info(2)['packages']['installed'][0]['name'], 'htop')
        self.assertEqual(json.loads(result_file.read_text()), result)

    def test_crashed_run_does_not_silence_later_hooks(self):
        desos_undo.write_json(self.m.host.path('/.snapshots/.desos-undo/pending.json'),
                              {'source': 'run', 'pre_id': 99, 'pid': 4000000, 'boot_id': 'boot-0'})
        self.m.cli('apt-pre', stdin=APT_V3)
        self.assertEqual(self.m.snapshot_ids(), [1])

    def test_run_still_runs_when_the_snapshot_fails(self):
        self.m.fail.add('btrfs subvolume snapshot')
        exit_code, result = self.m.json('run', '--reason', 'x', '--', 'true')
        self.assertEqual((exit_code, result['data']['pre_id']), (0, None))
        self.assertEqual(self.m.executed, [['true']])
        self.assertEqual(self.m.cli('run', '--reason', 'x')[0], 1)


class DiffTests(Base):
    def test_diff_reports_package_and_file_changes(self):
        self.m.write('etc/keep.conf', 'same')
        self.m.write('etc/app.conf', 'old value')
        self.m.write('etc/removed.conf', 'bye')
        self.m.write('usr/bin/tool', 'v1')
        self.m.json('snapshot', '--reason', 'base')
        self.m.write('etc/app.conf', 'new value')
        self.m.write('etc/added.conf', 'hello')
        (self.m.root / 'etc/removed.conf').unlink()
        os.chmod(self.m.root / 'etc/hostname', 0o600)
        self.m.write('usr/bin/tool', 'v2')
        self.m.write('var/lib/dpkg/status', STATUS_AFTER)
        exit_code, result = self.m.json('diff', '1')
        data = result['data']
        self.assertEqual((exit_code, data['from'], data['to'], data['paths']), (0, 1, 'current', ['/etc']))
        self.assertEqual(data['files'], {'added': ['/etc/added.conf'], 'removed': ['/etc/removed.conf'],
                                         'modified': ['/etc/app.conf', '/etc/hostname'], 'truncated': False})
        self.assertEqual([item['name'] for item in data['packages']['installed']], ['htop'])
        self.assertEqual(data['packages']['upgraded'], [{'name': 'nano', 'from': '7.2-1', 'to': '8.0-1'}])
        full = self.m.json('diff', '1', '--full')[1]['data']['files']
        self.assertIn('/usr/bin/tool', full['modified'])
        self.assertNotIn('/.snapshots', full['modified'] + full['added'])
        self.m.json('snapshot', '--reason', 'later')
        self.assertEqual(self.m.json('diff', '1', '--to', '2')[1]['data']['files']['added'], ['/etc/added.conf'])
        self.assertEqual(self.m.json('diff', '2')[1]['data']['files']['modified'], [])
        text = self.m.cli('diff', '1', '--path', '/usr')[1]
        self.assertIn('~ /usr/bin/tool', text)
        self.assertIn('package installed  htop 3.3.0-4', text)

    def test_debian_version_order(self):
        for left, right, expected in (('1.0', '1.0', 0), ('1.0-1', '1.0-2', -1), ('1:0.1', '2.0', 1), ('1.0~rc1', '1.0', -1),
                                      ('2.10', '2.9', 1), ('1.0a', '1.0', 1), ('7.0.0-12.12', '7.0.0-9.9', 1), ('1.0', '1.0+b1', -1)):
            self.assertEqual(desos_undo.compare_versions(left, right), expected, (left, right))
            if subprocess.run(['which', 'dpkg'], capture_output=True).returncode == 0:
                relation = {0: 'eq', -1: 'lt', 1: 'gt'}[expected]
                self.assertEqual(subprocess.run(['dpkg', '--compare-versions', left, relation, right]).returncode, 0, (left, right))


class RollbackTests(Base):
    def test_rollback_swaps_the_system_keeps_home_and_is_undoable(self):
        self.m.write('etc/app.conf', 'working')
        self.m.json('snapshot', '--reason', 'known working')
        self.m.write('etc/app.conf', 'broken')
        self.m.write('boot/grub/i386-pc/normal.mod', 'modules after a boot loader upgrade')
        (self.m.top / '@home/alex/thesis.txt').write_text('chapter two')
        exit_code, result = self.m.json('rollback', '1', '--reason', 'app stopped working')
        record = result['data']['rollback']
        self.assertEqual((exit_code, record['target'], record['undo'], result['data']['restart_required']), (0, 1, 2, True))
        # The next boot's @ is the snapshot; the running system was only renamed.
        self.assertEqual((self.m.top / '@/etc/app.conf').read_text(), 'working')
        self.assertEqual((self.m.top / '@.desos-undo-old-2/etc/app.conf').read_text(), 'broken')
        self.assertFalse((self.m.top / '@.desos-undo-new').exists())
        self.assertFalse((self.m.base / 'run/top').is_symlink())
        # The snapshot itself is untouched and the undo point holds the broken state.
        self.assertEqual((self.m.top / '@snapshots/1/snapshot/etc/app.conf').read_text(), 'working')
        self.assertEqual((self.m.top / '@snapshots/2/snapshot/etc/app.conf').read_text(), 'broken')
        self.assertEqual((self.info(2)['kind'], self.info(2)['rolled_back_to']), ('rollback', 1))
        # Home directories are not part of any of this.
        self.assertEqual((self.m.top / '@home/alex/thesis.txt').read_text(), 'chapter two')
        self.assertFalse((self.m.top / '@snapshots/1/snapshot/home').exists())
        # GRUB modules follow the installed boot code, not the snapshot.
        self.assertEqual((self.m.top / '@/boot/grub/i386-pc/normal.mod').read_text(), 'modules after a boot loader upgrade')
        self.assertEqual(record['grub_modules'], ['i386-pc'])
        notices = self.m.json('notices')[1]['data']['notices']
        self.assertEqual((notices[0]['kind'], notices[0]['snapshot'], notices[0]['undo']), ('rollback', 1, 2))
        self.assertIn('rollback 2', notices[0]['message'])
        # Before the restart the old root is still running: say so, snapshot nothing.
        self.m.boot_from('/@.desos-undo-old-2')
        status = self.m.json('status')[1]
        self.assertTrue(status['data']['root']['restart_pending'])
        self.assertEqual(status['data']['rollback']['target'], 1)
        self.assertEqual(self.m.json('snapshot', '--reason', 'x')[1]['code'], 'restart-pending')
        self.assertEqual(self.m.cli('apt-pre', stdin=APT_V3), (0, ''))
        # After the restart: undo the rollback.
        self.m.boot_from('/@')
        self.assertEqual(self.m.json('rollback', '2')[0], 0)
        self.assertEqual((self.m.top / '@/etc/app.conf').read_text(), 'broken')
        self.assertEqual((self.m.top / '@home/alex/thesis.txt').read_text(), 'chapter two')

    def test_rollback_without_atomic_exchange_and_with_failures(self):
        self.m.write('etc/app.conf', 'working')
        self.m.json('snapshot', '--reason', 'base')
        self.m.write('etc/app.conf', 'broken')
        original = desos_undo.exchange
        desos_undo.exchange = lambda left, right: False
        self.addCleanup(setattr, desos_undo, 'exchange', original)
        self.assertEqual(self.m.json('rollback', '1')[0], 0)
        self.assertEqual((self.m.top / '@/etc/app.conf').read_text(), 'working')
        self.assertEqual((self.m.top / '@.desos-undo-old-2/etc/app.conf').read_text(), 'broken')
        # A failing btrfs leaves the bootable system exactly where it was.
        self.m.fail.add('btrfs subvolume snapshot')
        exit_code, result = self.m.json('rollback', '2')
        self.assertEqual((exit_code, result['ok']), (1, False))
        self.assertEqual((self.m.top / '@/etc/app.conf').read_text(), 'working')
        self.assertFalse((self.m.base / 'run/top').is_symlink())

    def test_atomic_exchange_swaps_two_directories(self):
        left, right = self.m.base / 'left', self.m.base / 'right'
        left.mkdir()
        right.mkdir()
        (left / 'was-left').touch()
        if not desos_undo.exchange(left, right):
            self.skipTest('renameat2(RENAME_EXCHANGE) is unavailable on this filesystem')
        self.assertTrue((right / 'was-left').exists())
        self.assertFalse((left / 'was-left').exists())
        self.assertFalse(desos_undo.exchange(left, self.m.base / 'missing'))


class PruneTests(Base):
    def make(self, kind, **extra):
        return desos_undo.create_snapshot(self.m.host, desos_undo.load_config(self.m.host), kind, kind + ' test', **extra)['id']

    def test_retention_policy_keeps_recent_pairs_and_protected_snapshots(self):
        self.m.write('etc/desos-undo.conf', '[retention]\nkeep_pairs = 2\nkeep_manual = 1\nkeep_boot_good = 1\nkeep_rollback = 1\n')
        pairs = []
        for _ in range(4):
            pre = self.make('pre')
            pairs.append((pre, self.make('post', pre_id=pre)))
        manual = [self.make('manual'), self.make('manual', protected=True), self.make('manual')]
        good = [self.make('boot-good'), self.make('boot-good')]
        rollbacks = [self.make('rollback'), self.make('rollback')]
        desos_undo.write_json(self.m.host.path('/.snapshots/.desos-undo/good.json'), {'id': good[0]})
        exit_code, preview = self.m.json('prune', '--dry-run')
        self.assertEqual(exit_code, 0)
        expected = sorted([*pairs[0], *pairs[1], manual[0], rollbacks[0]])
        self.assertEqual(preview['data']['deleted'], expected)
        self.assertEqual(len(self.m.snapshot_ids()), 15)
        result = self.m.json('prune')[1]['data']
        self.assertEqual(result['deleted'], expected)
        self.assertIn('transactions', result['reasons'][str(pairs[0][0])])
        # Kept: two newest pairs, the protected and newest manual, the recorded
        # known-good and the newest boot-good, the newest rollback point.
        self.assertEqual(self.m.snapshot_ids(), sorted([*pairs[2], *pairs[3], manual[1], manual[2], *good, rollbacks[1]]))
        self.assertEqual(self.m.json('prune')[1]['data']['deleted'], [])

    def test_low_space_frees_gradually_and_never_the_known_good(self):
        ids = [self.make('manual') for _ in range(5)]
        good = self.make('boot-good')
        self.m.free = (100, 5)
        result = self.m.json('prune')[1]['data']
        self.assertEqual(result['deleted'], ids[:3])
        self.assertIn(good, self.m.snapshot_ids())
        self.m.json('prune')
        self.assertEqual(self.m.snapshot_ids(), [good])

    def test_age_limit_and_failed_deletion_are_reported(self):
        self.m.write('etc/desos-undo.conf', '[retention]\nmax_age_days = 7\n')
        old = self.make('manual')
        self.m.clock += 8 * 86400
        new = self.make('manual')
        self.m.fail.add('btrfs subvolume delete')
        result = self.m.json('prune')[1]['data']
        self.assertEqual((result['deleted'], result['failed'][0]['id']), ([], old))
        self.m.fail.clear()
        self.assertEqual(self.m.json('prune')[1]['data']['deleted'], [old])
        self.assertEqual(self.m.snapshot_ids(), [new])


class ComponentTests(unittest.TestCase):
    def test_manifest_follows_the_component_convention(self):
        manifest = json.loads((UNDO / 'component.json').read_text())
        self.assertEqual(set(manifest), {'name', 'packages', 'enable_units', 'notes'})
        self.assertEqual(manifest['name'], 'undo')
        self.assertIn('btrfs-progs', manifest['packages'])
        for unit in manifest['enable_units']:
            text = (ROOTFS / 'usr/lib/systemd/system' / unit).read_text()
            self.assertIn('[Install]', text, unit)
            self.assertIn('ConditionKernelCommandLine=!boot=casper', text, unit)

    def test_rootfs_tree_is_installable_as_shipped(self):
        executables = set()
        for path in ROOTFS.rglob('*'):
            self.assertFalse(path.is_symlink(), path)
            self.assertNotIn('__pycache__', path.parts)
            self.assertFalse(path.name.endswith('.pyc'), path)
            if path.is_file() and path.stat().st_mode & 0o111:
                executables.add(path.relative_to(ROOTFS).as_posix())
        self.assertEqual(executables, {'usr/sbin/desos-undo', 'etc/grub.d/42_desos-undo'})
        for script in ('etc/grub.d/42_desos-undo', 'etc/profile.d/desos-undo-notice.sh'):
            self.assertEqual(subprocess.run(['/bin/sh', '-n', str(ROOTFS / script)]).returncode, 0, script)
        for unit in (ROOTFS / 'usr/lib/systemd/system').glob('*.service'):
            self.assertIn('ExecStart=/usr/sbin/desos-undo ', unit.read_text(), unit)

    def test_package_manager_hooks_cannot_fail_a_transaction(self):
        apt = (ROOTFS / 'etc/apt/apt.conf.d/80desos-undo').read_text()
        self.assertIn('DPkg::Pre-Install-Pkgs { "/usr/sbin/desos-undo apt-pre || true"; };', apt)
        self.assertIn('DPkg::Post-Invoke { "/usr/sbin/desos-undo apt-post || true"; };', apt)
        # apt looks the protocol version up under the command's first word.
        self.assertIn('DPkg::Tools::Options::/usr/sbin/desos-undo::Version "3";', apt)
        dpkg = [line for line in (ROOTFS / 'etc/dpkg/dpkg.cfg.d/desos-undo').read_text().splitlines() if not line.startswith('#')]
        self.assertEqual(len(dpkg), 2)
        for line, option in zip(dpkg, ('pre-invoke', 'post-invoke')):
            name, _, command = line.partition('=')
            self.assertEqual(name, option)
            self.assertIn('|| true', command)
            self.assertEqual(subprocess.run(['/bin/sh', '-n', '-c', command]).returncode, 0)

    def test_launcher_runs_the_shipped_module(self):
        launcher = (ROOTFS / 'usr/sbin/desos-undo').read_text().replace('/usr/lib/desos-undo', str(ROOTFS / 'usr/lib/desos-undo'))
        result = subprocess.run(['python3', '-B', '-c', launcher, '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('list', 'snapshot', 'diff', 'rollback', 'status', 'prune', 'run'):
            self.assertIn(command, result.stdout)

    def test_schema_file_describes_real_output(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema is not installed')
        schema = json.loads((ROOTFS / 'usr/share/desos-undo/schema-v1.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        machine = Machine()
        self.addCleanup(machine.close)
        outputs = [machine.json('status'), machine.json('snapshot', '--reason', 'one'), machine.json('apt-pre', stdin=APT_V3),
                   machine.json('apt-post'), machine.json('list'), machine.json('diff', '1'), machine.json('rollback', '1'),
                   machine.json('status'), machine.json('notices'), machine.json('prune', '--dry-run'),
                   machine.json('run', '--reason', 'r', '--', 'true'), machine.json('diff', '99'),
                   machine.json('boot-start'), machine.json('grub-fragment')]
        live = Machine(live=True)
        self.addCleanup(live.close)
        outputs += [live.json('status'), live.json('list'), live.json('run', '--reason', 'r', '--', 'true')]
        for _, envelope in outputs:
            jsonschema.validate(envelope, schema)
        self.assertEqual(schema['properties']['schema']['const'], desos_undo.SCHEMA)
        self.assertIn('schema-v1.json', (UNDO / 'README.md').read_text())


if __name__ == '__main__':
    unittest.main()
