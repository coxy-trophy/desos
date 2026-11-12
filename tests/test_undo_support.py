"""Fixtures for the desos-undo tests: a fake btrfs machine in a temp directory.

Nothing here touches a real block device, mount table or boot loader. btrfs
snapshots are emulated with directory copies and the top-level mount with a
symlink, so rollback and diff run against real files.
"""
import collections
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True  # the component tree must never gain __pycache__
REPO = Path(__file__).resolve().parents[1]
UNDO = REPO / 'undo'
ROOTFS = UNDO / 'rootfs'
sys.path.insert(0, str(ROOTFS / 'usr/lib/desos-undo'))

import desos_undo  # noqa: E402

Usage = collections.namedtuple('Usage', 'total used free')
ROOT_UUID = '3f2a8c1e-9b7d-4e55-a1c2-0d9e8f7a6b5c'
ESP_UUID = 'A1B2-C3D4'
STATUS = '''Package: bash
Status: install ok installed
Architecture: amd64
Version: 5.2-1

Package: nano
Status: install ok installed
Architecture: amd64
Version: 7.2-1

Package: gone
Status: deinstall ok config-files
Architecture: amd64
Version: 1.0
'''


class Machine:
    """A temp-dir DesOS installation plus the fake commands that act on it."""

    def __init__(self, firmware='efi', fstype='btrfs', live=False):
        self._temp = tempfile.TemporaryDirectory(prefix='desos-undo-test-')
        self.base = Path(self._temp.name)
        self.top = self.base / 'top'
        self.root = self.top / '@'
        self.esp = self.base / 'esp'
        self.calls = []
        self.errors = io.StringIO()
        self.fail = set()
        self.clock = 1794000000.0
        self.euid = 0
        self.environ = {'HOME': str(self.base / 'userhome')}
        self.executed = []
        self.execute_result = 0
        self.execute_hook = None
        self.free = (100, 50)
        self.systemd = {'default': 'graphical.target', 'greetd-enabled': 'enabled', 'active': 'active',
                        'sub': 'running', 'restarts': '0', 'sessions': '1 110 _greetd seat0 tty7\n',
                        'system': 'running'}
        for name in ('@', '@home', '@root', '@snapshots', '@log', '@cache', '@tmp'):
            (self.top / name).mkdir(parents=True)
        self.write('etc/os-release', 'ID=desos\n')
        self.write('etc/hostname', 'desos\n')
        self.write('var/lib/dpkg/status', STATUS)
        self.write('proc/cmdline', 'BOOT_IMAGE=/@/boot/vmlinuz-6.18.62-desos root=UUID=%s ro rootflags=subvol=@ quiet splash%s\n'
                   % (ROOT_UUID, ' boot=casper' if live else ''))
        self.write('proc/sys/kernel/random/boot_id', 'boot-1\n')
        for version in ('6.18.62-desos', '7.0.0-12-generic'):
            self.write('boot/vmlinuz-' + version, 'kernel ' + version)
            self.write('boot/initrd.img-' + version, 'initrd ' + version)
        self.write('boot/grub/i386-pc/normal.mod', 'modules of the installed boot loader')
        (self.top / '@home/alex').mkdir()
        (self.top / '@home/alex/thesis.txt').write_text('personal work')
        # Absolute, so every emulated snapshot still reaches the one @snapshots.
        (self.root / '.snapshots').symlink_to(self.top / '@snapshots')
        self.mounts = {'/': {'target': '/', 'source': '/dev/vda2[/@]', 'fstype': fstype, 'uuid': ROOT_UUID, 'fsroot': '/@'}}
        if fstype == 'btrfs':
            self.mounts['/.snapshots'] = {'target': '/.snapshots', 'source': '/dev/vda2[/@snapshots]', 'fstype': 'btrfs',
                                          'uuid': ROOT_UUID, 'fsroot': '/@snapshots'}
        else:
            self.mounts['/'].update(source='/dev/vda2', fsroot='/')
        if firmware == 'efi':
            self.esp.mkdir()
            (self.root / 'boot/efi').symlink_to(self.esp)
            self.mounts['/boot/efi'] = {'target': '/boot/efi', 'source': '/dev/vda1', 'fstype': 'vfat', 'uuid': ESP_UUID, 'fsroot': '/'}
        self.host = self.new_host()

    def close(self):
        self._temp.cleanup()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def new_host(self):
        return desos_undo.Host(
            root=self.root, runner=self.runner, clock=lambda: self.clock, sleeper=self.sleep, euid=self.euid,
            release='6.18.62-desos', environ=self.environ, execute=self.execute, runtime=self.base / 'run',
            which=lambda name: '/usr/bin/' + name, usage=lambda path: Usage(self.free[0], self.free[0] - self.free[1], self.free[1]))

    def sleep(self, seconds):
        self.clock += seconds

    def execute(self, argv):
        self.executed.append(list(argv))
        if self.execute_hook:
            self.execute_hook()
        return self.execute_result

    def boot_from(self, fsroot, extra=''):
        """Pretend the kernel mounted another subvolume as /."""
        self.mounts['/'].update(source='/dev/vda2[%s]' % fsroot, fsroot=fsroot)
        (self.root / 'proc/cmdline').write_text('root=UUID=%s ro rootflags=subvol=%s quiet %s\n' % (ROOT_UUID, fsroot.lstrip('/'), extra))

    def runner(self, argv, input_text=None):
        self.calls.append(argv)
        done = desos_undo.Completed
        if any(' '.join(argv).startswith(prefix) for prefix in self.fail):
            return done(1, '', 'ERROR: injected failure')
        if argv[0] == 'findmnt':
            entry = self.mounts.get(argv[-1])
            return done(0, json.dumps({'filesystems': [entry]})) if entry else done(1)
        if argv[:3] == ['btrfs', 'subvolume', 'snapshot']:
            source, target = argv[-2], argv[-1]
            shutil.copytree(source, target, symlinks=True)
            return done(0)
        if argv[:3] == ['btrfs', 'subvolume', 'delete']:
            shutil.rmtree(argv[-1])
            return done(0)
        if argv[:2] == ['btrfs', 'filesystem']:
            return done(0)
        if argv[0] == 'mount':
            os.rmdir(argv[-1])
            os.symlink(self.top, argv[-1])
            return done(0)
        if argv[0] == 'umount':
            os.unlink(argv[-1])
            return done(0)
        if argv[0] == 'systemctl':
            if argv[1] == 'get-default':
                return done(0, self.systemd['default'] + '\n')
            if argv[1] == 'is-enabled':
                return done(0, self.systemd['greetd-enabled'] + '\n')
            if argv[1] == 'is-system-running':
                return done(0, self.systemd['system'] + '\n')
            if argv[1] == 'show':
                return done(0, 'ActiveState=%s\nSubState=%s\nNRestarts=%s\nMainPID=700\n'
                            % (self.systemd['active'], self.systemd['sub'], self.systemd['restarts']))
            return done(0)
        if argv[0] == 'loginctl':
            return done(0, self.systemd['sessions'])
        return done(127, '', 'unexpected command: ' + ' '.join(argv))

    def cli(self, *argv, stdin=''):
        """Run the real command-line entry point; returns (exit, stdout text)."""
        out = io.StringIO()
        code = desos_undo.main(list(argv), host=self.new_host(), stdin=io.StringIO(stdin), stdout=out, stderr=self.errors)
        return code, out.getvalue()

    def json(self, *argv, stdin=''):
        code, text = self.cli('--json', *argv, stdin=stdin)
        return code, json.loads(text.strip().splitlines()[-1])

    def snapshot_ids(self):
        return sorted(int(path.name) for path in (self.top / '@snapshots').iterdir() if path.name.isdigit())

    def install_grub_cfg(self):
        """What update-grub would leave: only our section matters here."""
        text = desos_undo.grub_fragment(self.host, desos_undo.load_config(self.host))
        self.write('boot/grub/grub.cfg', 'set default="${saved_entry}"\n' + text)
        return text


class Grub:
    """Interpreter for the small GRUB script subset desos-undo generates, so
    the boot-counter logic is executed rather than only syntax-checked."""

    def __init__(self, machine, writable_devices=('esp',)):
        self.machine = machine
        self.devices = {ROOT_UUID: 'root', ESP_UUID: 'esp'}
        self.writable = set(writable_devices)
        self.saves = []

    def file(self, path):
        device, _, rest = path[1:].partition(')')
        base = {'root': self.machine.top, 'esp': self.machine.esp}[device]
        return device, base / rest.lstrip('/')

    def expand(self, text):
        for name, value in sorted(self.env.items(), key=lambda item: -len(item[0])):
            text = text.replace('${%s}' % name, value)
        while '${' in text:  # unset variables expand to nothing
            start = text.index('${')
            text = text[:start] + text[text.index('}', start) + 1:]
        return text

    def test(self, words):
        if words[0] == 'search':
            target = [word for word in words if word.startswith('--set=')][0][6:]
            device = self.devices.get(words[-1])
            if device and (device != 'esp' or self.machine.esp.is_dir()):
                self.env[target] = device
                return True
            return False
        assert words[0] == '[' and words[-1] == ']', words
        inner = words[1:-1]
        if inner[0] == '-n':
            return len(inner) > 1 and inner[1] != ''
        if inner[0] == '-z':
            return len(inner) == 1 or inner[1] == ''
        if inner[0] == '-s':
            path = self.file(inner[1])[1]
            return path.is_file() and path.stat().st_size > 0
        if inner[1] == '=':
            return inner[0] == inner[2]
        if inner[1] == '-ge':
            return int(inner[0] or 0) >= int(inner[2])
        raise AssertionError('unsupported test: %r' % (inner,))

    def command(self, words):
        if words[0] == 'set':
            name, _, value = words[1].partition('=')
            self.env[name] = value
        elif words[0] == 'load_env':
            values = desos_undo.read_env(self.file(words[2])[1])
            for name in words[3:]:
                if name in values:
                    self.env[name] = values[name]
        elif words[0] == 'save_env':
            device, path = self.file(words[2])
            assert device in self.writable, 'GRUB cannot write to %s' % device
            values = desos_undo.read_env(path)
            for name in words[3:]:
                values[name] = self.env.get(name, '')
            desos_undo.write_env(path, values)
            self.saves.append(words[3:])
        elif words[0] == 'source':
            self.sourced.append(self.file(words[1])[1])
        else:
            raise AssertionError('unsupported GRUB command: %r' % (words,))

    def boot(self, script, saved_entry='gnulinux-advanced>gnulinux-6.18.62-desos'):
        """Execute the script as GRUB would at power-on; returns its variables."""
        self.env = {'saved_entry': saved_entry, 'timeout': '5'}
        self.sourced = []
        statements = []
        for line in script.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            statements += [part.strip() for part in line.split('; ') if part.strip()]
        stack = []  # (taken already, currently active)
        for statement in statements:
            active = all(frame[1] for frame in stack)
            if statement.startswith('then'):
                statement = statement[4:].strip()
                if not statement:
                    continue
            head = statement.split()[0]
            if head in ('if', 'elif'):
                condition = statement[len(head):].strip()
                if head == 'if':
                    result = active and self.test(shlex.split(self.expand(condition)))
                    stack.append([result, result])
                else:
                    outer = all(frame[1] for frame in stack[:-1])
                    result = outer and not stack[-1][0] and self.test(shlex.split(self.expand(condition)))
                    stack[-1] = [stack[-1][0] or result, result]
            elif head == 'else':
                outer = all(frame[1] for frame in stack[:-1])
                stack[-1] = [True, outer and not stack[-1][0]]
            elif head == 'fi':
                stack.pop()
            elif active:
                self.command(shlex.split(self.expand(statement)))
        assert not stack
        return dict(self.env)
