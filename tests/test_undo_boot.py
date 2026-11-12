"""desos-undo self-healing boot: GRUB fragments, counter, recovery, notices.

The generated GRUB script is syntax-checked with the real grub-script-check
and executed by a small interpreter (test_undo_support.Grub) against real GRUB
environment-block files, so the boot logic itself is tested. No boot loader is
installed or started.
"""
import shutil
import subprocess
import unittest

from test_undo_support import ESP_UUID, Grub, Machine, REPO, ROOTFS, ROOT_UUID, desos_undo

CHECKER = shutil.which('grub-script-check')
EDITENV = shutil.which('grub-editenv')
SAVED = 'gnulinux-advanced>gnulinux-6.18.62-desos'


class Base(unittest.TestCase):
    firmware = 'efi'

    def setUp(self):
        self.m = Machine(self.firmware)
        self.addCleanup(self.m.close)
        self.cfg = desos_undo.load_config(self.m.host)

    def script_check(self, text):
        if not CHECKER:
            self.skipTest('grub-script-check is not installed')
        path = self.m.base / 'check.cfg'
        # A lone comment is valid inside grub.cfg; the checker only rejects an empty file.
        path.write_text('set surrounding_grub_cfg=1\n' + text)
        result = subprocess.run([CHECKER, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + text)

    def env(self):
        return desos_undo.read_env(self.m.host.path(desos_undo.boot_arming(self.m.host)['env']))

    def good_start(self):
        code, result = self.m.json('boot-good', '--wait')
        self.assertEqual((code, result['ok']), (0, True), result)
        return result['data']

    def snapshots_cfg(self):
        return (self.m.top / '@snapshots/.desos-undo/grub/snapshots.cfg').read_text()


class FragmentTests(Base):
    def test_uefi_fragment_counts_in_grub_on_the_esp(self):
        text = self.m.install_grub_cfg()
        self.script_check(text)
        self.assertIn('# desos-undo: mode=grub-counted env=/boot/efi/desos-undo/env threshold=3', text)
        self.assertIn('search --no-floppy --fs-uuid --set=desos_undo_dev ' + ROOT_UUID, text)
        self.assertIn('search --no-floppy --fs-uuid --set=desos_undo_envdev ' + ESP_UUID, text)
        self.assertIn('save_env -f "${desos_undo_env}" undo_tries', text)
        self.assertEqual(desos_undo.boot_arming(self.m.host),
                         {'mode': 'grub-counted', 'env': '/boot/efi/desos-undo/env', 'threshold': 3})

    def test_fragment_leaves_the_kernel_default_mechanism_alone(self):
        text = self.m.install_grub_cfg()
        # select-upstream-kernel owns saved_entry (GRUB_DEFAULT=saved, grub-set-default).
        for forbidden in ('saved_entry', 'next_entry', 'grubenv', 'savedefault', 'GRUB_DEFAULT'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(text.count('set default='), 2)
        source = (ROOTFS / 'usr/lib/desos-undo/desos_undo.py').read_text()
        for forbidden in ('grub-set-default', 'grub-reboot', 'grub-editenv', 'update-grub', 'grub-mkconfig', '/etc/default/grub'):
            self.assertNotIn("'%s'" % forbidden, source)

    def test_threshold_is_configurable(self):
        self.m.write('etc/desos-undo.conf', '[boot]\nfailure_threshold = 5\n')
        text = self.m.install_grub_cfg()
        self.script_check(text)
        self.assertIn('-ge 5 ]', text)
        self.assertIn('set undo_tries=5', text)
        self.assertEqual(desos_undo.boot_arming(self.m.host)['threshold'], 5)

    def test_non_btrfs_and_foreign_layouts_emit_only_a_comment(self):
        for machine in (Machine(fstype='ext4'), Machine()):
            self.addCleanup(machine.close)
            if machine.mounts['/']['fstype'] == 'btrfs':
                del machine.mounts['/.snapshots']
            text = desos_undo.grub_fragment(machine.host, desos_undo.load_config(machine.host))
            self.assertTrue(text.startswith('# desos-undo: self-healing boot and the snapshot menu are inactive'))
            self.assertEqual(len(text.splitlines()), 1)
            self.script_check(text)
            machine.write('boot/grub/grub.cfg', text)
            self.assertEqual(desos_undo.boot_arming(machine.host)['mode'], 'unarmed')

    def test_fragment_is_generated_inside_the_installer_chroot(self):
        # Calamares runs grub-mkconfig in the target while the live kernel
        # command line still says boot=casper; the section must still appear.
        (self.m.root / 'proc/cmdline').write_text('boot=casper quiet\n')
        code, text = self.m.cli('grub-fragment')
        self.assertEqual(code, 0)
        self.assertIn('mode=grub-counted', text)
        self.assertEqual(self.m.json('status')[1]['code'], 'live-session')

    def test_grub_d_script_prints_the_fragment_and_never_fails(self):
        script = (ROOTFS / 'etc/grub.d/42_desos-undo').read_text()
        tool = self.m.base / 'desos-undo'
        tool.write_text('#!/bin/sh\n[ "$1" = grub-fragment ] || exit 9\nprintf "set a=1\\nset b=2\\n"\n')
        tool.chmod(0o755)
        relocated = script.replace('/usr/sbin/desos-undo', str(tool))
        done = subprocess.run(['/bin/sh', '-c', relocated], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, 'set a=1\nset b=2\n'))
        tool.write_text('#!/bin/sh\necho partial\nexit 1\n')
        done = subprocess.run(['/bin/sh', '-c', relocated], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        self.assertNotIn('partial', done.stdout)
        self.assertTrue(done.stdout.startswith('# desos-undo'))
        self.script_check(done.stdout)
        tool.unlink()
        done = subprocess.run(['/bin/sh', '-c', relocated], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, ''))


class SnapshotMenuTests(Base):
    def test_menu_lists_bootable_snapshots_newest_first(self):
        self.m.install_grub_cfg()
        self.m.json('snapshot', '--reason', "it's $(reboot) `x` \\ {} time")
        self.m.json('snapshot', '--reason', 'second')
        text = self.snapshots_cfg()
        self.script_check(text)
        self.assertLess(text.index('--id desos-undo-snapshot-2 {'), text.index('--id desos-undo-snapshot-1 {'))
        self.assertIn("submenu 'DesOS snapshots (start an earlier system state)' --class desos-snapshots --id desos-undo-snapshots {", text)
        # The kernel that was running is started from inside the snapshot, read-only.
        self.assertIn('linux /@snapshots/2/snapshot/boot/vmlinuz-6.18.62-desos root=UUID=%s ro '
                      'rootflags=subvol=@snapshots/2/snapshot quiet splash desos.undo.snapshot=2' % ROOT_UUID, text)
        self.assertIn('initrd /@snapshots/2/snapshot/boot/initrd.img-6.18.62-desos', text)
        self.assertNotIn("it's", text)
        self.assertNotIn('\\', text)
        self.assertNotIn('desos-undo-heal', text)
        self.assertNotIn('boot=casper', text)

    def test_menu_follows_deletions_and_skips_snapshots_without_a_kernel(self):
        self.m.json('snapshot', '--reason', 'one')
        for path in (self.m.root / 'boot').glob('vmlinuz-*'):
            path.unlink()
        self.m.json('snapshot', '--reason', 'separate boot partition')
        text = self.snapshots_cfg()
        self.assertIn('desos-undo-snapshot-1 ', text)
        self.assertNotIn('desos-undo-snapshot-2 ', text)
        self.m.write('etc/desos-undo.conf', '[retention]\nkeep_manual = 0\n')
        self.m.json('prune')
        text = self.snapshots_cfg()
        self.assertNotIn('menuentry', text)
        self.script_check(text)

    def test_menu_is_limited_and_prefers_the_desos_kernel(self):
        self.m.write('etc/desos-undo.conf', '[boot]\nmenu_entries = 2\n')
        host = self.m.new_host()
        host.release = '9.9.9-not-installed'
        for index in range(4):
            desos_undo.create_snapshot(host, desos_undo.load_config(host), 'manual', 'n%d' % index)
        text = self.snapshots_cfg()
        self.assertEqual(text.count('menuentry '), 2)
        self.assertIn('vmlinuz-6.18.62-desos', text)
        self.assertNotIn('generic', text)


class EnvBlockTests(Base):
    def test_block_is_a_valid_fixed_size_grub_environment(self):
        path = self.m.base / 'env'
        desos_undo.write_env(path, {'undo_tries': '2', 'undo_good': '14', 'undo_healed': ''})
        data = path.read_bytes()
        self.assertEqual(len(data), 1024)
        self.assertTrue(data.startswith(b'# GRUB Environment Block\n'))
        self.assertEqual(desos_undo.read_env(path), {'undo_tries': '2', 'undo_good': '14'})
        inode = path.stat().st_ino
        desos_undo.write_env(path, {'undo_tries': '0'})
        self.assertEqual((path.stat().st_ino, path.stat().st_size), (inode, 1024))
        if not EDITENV:
            self.skipTest('grub-editenv is not installed')
        listed = subprocess.run([EDITENV, str(path), 'list'], capture_output=True, text=True)
        self.assertEqual((listed.returncode, listed.stdout), (0, 'undo_tries=0\n'))
        # ... and a block written by GRUB's own tool is read back correctly.
        subprocess.run([EDITENV, str(path), 'set', 'undo_tries=3', 'undo_good=7'], check=True)
        self.assertEqual(desos_undo.read_env(path), {'undo_tries': '3', 'undo_good': '7'})


class SelfHealingTests(Base):
    """Whole-life scenario on UEFI: GRUB counts on the EFI system partition."""

    def start(self, grub):
        return grub.boot((self.m.root / 'boot/grub/grub.cfg').read_text(), SAVED)

    def test_first_boot_arms_and_good_boots_never_trigger_recovery(self):
        self.m.install_grub_cfg()
        grub = Grub(self.m)
        # Before the first good start there is no environment file: GRUB does nothing.
        env = self.start(grub)
        self.assertEqual((env['desos_undo_action'], env['default'], grub.saves), ('none', SAVED, []))
        data = self.good_start()
        self.assertEqual((data['good_id'], data['new_snapshot'], data['mode']), (1, True, 'grub-counted'))
        self.assertEqual(self.env(), {'undo_tries': '0', 'undo_good': '1'})
        self.assertTrue((self.m.esp / 'desos-undo/env').is_file())
        for _ in range(10):
            env = self.start(grub)
            self.assertEqual((env['desos_undo_action'], env['default'], env['timeout']), ('count', SAVED, '5'))
            self.assertEqual(self.env()['undo_tries'], '1')
            self.assertTrue(env.get('desos_undo_dev'))
            self.good_start()
            self.assertEqual(self.env()['undo_tries'], '0')
        # Nothing changed between starts, so the known-good snapshot is reused.
        self.assertEqual(self.m.snapshot_ids(), [1])
        self.assertTrue(self.m.json('status')[1]['data']['boot']['marked_good'])
        self.assertEqual(grub.sourced[0], self.m.top / '@snapshots/.desos-undo/grub/snapshots.cfg')

    def test_three_failed_starts_recover_the_last_good_snapshot_and_tell_the_user(self):
        self.m.install_grub_cfg()
        self.m.write('etc/app.conf', 'working')
        self.good_start()
        # A package transaction breaks the greeter.
        self.m.cli('apt-pre', stdin='VERSION 3\n\ngreeter 1.0 amd64 none < 2.0 amd64 none /x.deb\n')
        self.m.write('etc/app.conf', 'broken')
        self.m.cli('apt-post')
        self.assertEqual(self.m.snapshot_ids(), [1, 2, 3])
        grub = Grub(self.m)
        for attempt in (1, 2, 3):
            env = self.start(grub)
            self.assertEqual((env['desos_undo_action'], env['default']), ('count', SAVED))
            self.assertEqual(self.env()['undo_tries'], str(attempt))
            self.m.systemd['active'] = 'failed'
            code, result = self.m.json('boot-good')
            self.assertEqual((code, result['code']), (1, 'not-ready'))
        env = self.start(grub)
        self.assertEqual((env['desos_undo_action'], env['default'], env['timeout'], env['timeout_style']),
                         ('heal', 'desos-undo-heal', '10', 'menu'))
        self.assertEqual(self.env(), {'undo_tries': '3', 'undo_good': '1', 'undo_healed': '1'})
        heal = self.snapshots_cfg()
        self.script_check(heal)
        self.assertIn('--class desos-known-good --id desos-undo-heal {', heal)
        self.assertIn('rootflags=subvol=@snapshots/1/snapshot quiet splash desos.undo.snapshot=1 desos.undo.heal=1', heal)
        self.assertLess(heal.index('if [ "${desos_undo_action}" = "heal" ]; then'), heal.index('desos-undo-heal'))
        # GRUB started snapshot 1 read-only; the early unit makes it permanent.
        self.m.boot_from('/@snapshots/1/snapshot', 'desos.undo.snapshot=1 desos.undo.heal=1')
        code, result = self.m.json('boot-start')
        self.assertEqual((code, result['data']['action']), (0, 'healed'))
        self.assertIn(['systemctl', '--no-block', 'reboot'], self.m.calls)
        self.assertEqual((self.m.top / '@/etc/app.conf').read_text(), 'working')
        undo = result['data']['rollback']['undo']
        self.assertEqual((self.m.top / '@snapshots' / str(undo) / 'snapshot/etc/app.conf').read_text(), 'broken')
        self.assertEqual((self.m.top / '@home/alex/thesis.txt').read_text(), 'personal work')
        self.assertEqual(self.env(), {'undo_tries': '0', 'undo_good': '1', 'undo_healed': '1'})
        # Restart into the recovered system.
        self.m.boot_from('/@')
        self.m.systemd['active'] = 'active'
        env = self.start(grub)
        self.assertEqual((env['desos_undo_action'], env['default']), ('count', SAVED))
        self.assertEqual(self.m.json('boot-start')[1]['data']['action'], 'none')
        data = self.good_start()
        self.assertEqual(self.env(), {'undo_tries': '0', 'undo_good': str(data['good_id'])})
        self.assertEqual(data['removed_roots'], ['@.desos-undo-old-%d' % undo])
        self.assertFalse(list(self.m.top.glob('@.desos-undo-*')))
        # The user is told at the next login, once.
        self.m.euid = 1000
        status = self.m.json('status')[1]['data']
        self.assertEqual(len(status['notices']), 1)
        notice = status['notices'][0]
        self.assertEqual((notice['kind'], notice['snapshot'], notice['undo']), ('auto-recovery', 1, undo))
        self.assertIn('did not start 3 times in a row', notice['message'])
        self.assertIn('rollback %d' % undo, notice['message'])
        self.assertIn('did not start 3 times', self.m.cli('notices', '--ack')[1])
        self.assertEqual(self.m.json('notices')[1]['data']['notices'], [])
        self.m.euid = 0
        self.assertEqual(len(self.m.json('notices')[1]['data']['notices']), 1)
        self.m.json('notices', '--ack')
        self.assertEqual(self.m.json('notices')[1]['data']['notices'], [])

    def test_recovery_that_does_not_help_stops_at_the_menu_instead_of_looping(self):
        self.m.install_grub_cfg()
        self.good_start()
        desos_undo.write_env(self.m.esp / 'desos-undo/env', {'undo_tries': '3', 'undo_good': '1', 'undo_healed': '1'})
        grub = Grub(self.m)
        env = self.start(grub)
        self.assertEqual((env['desos_undo_action'], env['default'], env['timeout']), ('menu', 'desos-undo-snapshots', '-1'))
        self.assertEqual(grub.saves, [])
        self.assertIn('--id desos-undo-snapshots {', self.snapshots_cfg())

    def test_failed_recovery_reports_and_does_not_restart(self):
        self.m.install_grub_cfg()
        self.good_start()
        self.m.boot_from('/@snapshots/1/snapshot', 'desos.undo.snapshot=1 desos.undo.heal=1')
        self.m.fail.add('btrfs subvolume snapshot')
        code, result = self.m.json('boot-start')
        self.assertEqual((code, result['ok'], result['code']), (0, False, 'command-failed'))
        self.assertNotIn(['systemctl', '--no-block', 'reboot'], self.m.calls)
        self.assertEqual(self.env()['undo_healed'], '1')

    def test_deliberate_snapshot_and_rescue_starts_are_not_failures(self):
        self.m.install_grub_cfg()
        self.good_start()
        grub = Grub(self.m)
        for _ in range(6):
            self.start(grub)
            self.m.boot_from('/@snapshots/1/snapshot', 'desos.undo.snapshot=1')
            self.assertEqual(self.m.json('boot-start')[1]['data']['action'], 'not-counted')
            code, result = self.m.json('boot-good', '--wait')
            self.assertEqual((code, result['code']), (0, 'snapshot-boot'))
            self.assertEqual(self.env()['undo_tries'], '0')
        status = self.m.json('status')[1]
        self.assertEqual(status['data']['root']['booted_snapshot'], 1)
        self.assertIn('rollback 1', status['message'])
        self.assertEqual(self.m.cli('apt-pre', stdin='VERSION 3\n\n'), (0, ''))
        self.assertEqual(self.m.snapshot_ids(), [1])
        self.m.boot_from('/@', 'systemd.unit=rescue.target')
        self.start(grub)
        self.assertEqual(self.m.json('boot-start')[1]['data']['action'], 'not-counted')
        self.assertEqual(self.env()['undo_tries'], '0')

    def test_new_known_good_snapshot_only_after_changes(self):
        self.m.install_grub_cfg()
        self.good_start()
        self.m.json('snapshot', '--reason', 'manual')
        self.assertEqual(self.good_start()['good_id'], 3)
        self.assertEqual(self.good_start()['good_id'], 3)
        self.assertEqual(self.m.snapshot_ids(), [1, 2, 3])
        self.assertIn('snapshot 3', self.snapshots_cfg())
        # A full disk must not stop the counter from being reset.
        self.m.json('snapshot', '--reason', 'again')
        self.m.fail.add('btrfs subvolume snapshot')
        desos_undo.write_env(self.m.esp / 'desos-undo/env', {'undo_tries': '2', 'undo_good': '3'})
        data = self.good_start()
        self.assertEqual((data['good_id'], bool(data['warnings'])), (3, True))
        self.assertEqual(self.env(), {'undo_tries': '0', 'undo_good': '3'})


class GreeterTests(Base):
    def test_start_is_good_only_when_the_greeter_stays_up(self):
        self.m.install_grub_cfg()
        self.m.systemd['sessions'] = ''
        self.assertEqual(self.m.json('boot-good')[1]['code'], 'not-ready')
        self.m.systemd['sessions'] = '1 110 _greetd seat0 tty7\n'
        # A crash-looping greeter is briefly "active" but its restart count moves.
        original = self.m.sleep

        def restart(seconds):
            original(seconds)
            self.m.systemd['restarts'] = str(int(self.m.systemd['restarts']) + 1)

        self.m.sleep = restart
        started = self.m.clock
        self.assertEqual(self.m.json('boot-good', '--wait')[1]['code'], 'not-ready')
        self.assertGreaterEqual(self.m.clock - started, 600)
        self.assertFalse((self.m.esp / 'desos-undo/env').exists())
        self.assertEqual(self.m.snapshot_ids(), [])
        self.m.sleep = original
        started = self.m.clock
        self.assertEqual(self.m.json('boot-good', '--wait')[0], 0)
        self.assertEqual(self.m.clock - started, 10)

    def test_waits_for_a_slow_greeter(self):
        self.m.install_grub_cfg()
        self.m.systemd['active'] = 'activating'
        original = self.m.sleep

        def slow(seconds):
            original(seconds)
            if self.m.clock >= 1794000000.0 + 60:
                self.m.systemd['active'] = 'active'

        self.m.sleep = slow
        self.assertEqual(self.m.json('boot-good', '--wait')[0], 0)

    def test_text_mode_systems_use_system_state_and_admin_can_force(self):
        self.m.install_grub_cfg()
        self.m.systemd.update(default='multi-user.target', active='inactive', system='starting')
        self.assertEqual(self.m.json('boot-good')[1]['code'], 'not-ready')
        self.m.systemd['system'] = 'degraded'
        self.assertEqual(self.m.json('boot-good')[0], 0)
        self.m.systemd['system'] = 'starting'
        self.assertEqual(self.m.json('boot-good', '--now')[0], 0)


class BiosTests(Base):
    """No EFI system partition: GRUB cannot write, userspace counts instead."""
    firmware = 'bios'

    def test_bios_counts_in_userspace_and_grub_never_writes(self):
        text = self.m.install_grub_cfg()
        self.script_check(text)
        self.assertIn('# desos-undo: mode=userspace-counted env=/.snapshots/.desos-undo/grub/env threshold=3', text)
        self.assertNotIn('save_env', text)
        self.assertNotIn(ESP_UUID, text)
        grub = Grub(self.m, writable_devices=())
        text = (self.m.root / 'boot/grub/grub.cfg').read_text()
        self.good_start()
        env_file = self.m.top / '@snapshots/.desos-undo/grub/env'
        self.assertEqual(desos_undo.read_env(env_file), {'undo_tries': '0', 'undo_good': '1'})
        for attempt in (1, 2, 3):
            env = grub.boot(text, SAVED)
            self.assertEqual((env['desos_undo_action'], env['default']), ('count', SAVED))
            self.assertEqual(self.m.json('boot-start')[1]['data']['action'], 'counted')
            self.assertEqual(desos_undo.read_env(env_file)['undo_tries'], str(attempt))
        env = grub.boot(text, SAVED)
        self.assertEqual((env['desos_undo_action'], env['default']), ('heal', 'desos-undo-heal'))
        self.m.boot_from('/@snapshots/1/snapshot', 'desos.undo.snapshot=1 desos.undo.heal=1')
        self.assertEqual(self.m.json('boot-start')[1]['data']['action'], 'healed')
        self.assertEqual(desos_undo.read_env(env_file), {'undo_tries': '0', 'undo_good': '1', 'undo_healed': '1'})
        self.m.boot_from('/@')
        self.assertEqual(grub.boot(text, SAVED)['desos_undo_action'], 'count')
        self.m.json('boot-start')
        self.good_start()
        self.assertEqual(desos_undo.read_env(env_file)['undo_tries'], '0')
        self.assertNotIn('undo_healed', desos_undo.read_env(env_file))
        self.assertEqual(grub.saves, [])
        self.assertEqual(self.m.json('status')[1]['data']['boot']['mode'], 'userspace-counted')


class UnitTests(unittest.TestCase):
    def test_units_are_ordered_for_early_counting_and_late_confirmation(self):
        units = ROOTFS / 'usr/lib/systemd/system'
        early = (units / 'desos-undo-boot-start.service').read_text()
        for line in ('DefaultDependencies=no', 'After=local-fs.target', 'Before=sysinit.target shutdown.target',
                     'WantedBy=sysinit.target', 'ExecStart=/usr/sbin/desos-undo boot-start', 'ConditionPathIsMountPoint=/.snapshots'):
            self.assertIn(line, early.splitlines())
        late = (units / 'desos-undo-boot-good.service').read_text()
        for line in ('After=greetd.service systemd-user-sessions.service desos-undo-boot-start.service', 'Type=exec',
                     'ExecStart=/usr/sbin/desos-undo boot-good --wait', 'WantedBy=multi-user.target'):
            self.assertIn(line, late.splitlines())
        timer = (units / 'desos-undo-prune.timer').read_text()
        self.assertIn('WantedBy=timers.target', timer)
        self.assertTrue((units / 'desos-undo-prune.service').is_file())
        analyze = shutil.which('systemd-analyze')
        if analyze:
            for unit in sorted(units.iterdir()):
                done = subprocess.run([analyze, 'verify', '--recursive-errors=no', str(unit)], capture_output=True, text=True)
                problems = [line for line in done.stderr.splitlines()
                            if unit.name in line and 'is not executable' not in line and 'No such file' not in line]
                self.assertEqual(problems, [], done.stderr)


if __name__ == '__main__':
    unittest.main()
