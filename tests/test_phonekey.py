"""Host-safe tests of the phonekey component.

Everything runs against temporary directories: no real PAM file, account
database, DES Connect daemon or system log is read or written. The helper's
decision is exercised with a stand-in phone that signs with OpenSSL.
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import pwd
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
COMPONENT = HERE / 'phonekey'
TOOL = COMPONENT / 'rootfs/usr/sbin/desos-phonekey'
OPENSSL = shutil.which('openssl')


def load(name, path):
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loader = SourceFileLoader(name, str(path))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


tool = load('desos_phonekey', TOOL)
ME = pwd.getpwuid(os.getuid())
USABLE = OPENSSL is not None and os.getuid() != 0 and tool.USER.fullmatch(ME.pw_name) is not None

SUDO = b'''#%PAM-1.0

# Set up user limits from /etc/security/limits.conf.
session    required   pam_limits.so

session    required   pam_env.so readenv=1 user_readenv=0
session    required   pam_env.so readenv=1 envfile=/etc/default/locale user_readenv=0

@include common-auth
@include common-account
@include common-session-noninteractive
'''
POLKIT = b'''#%PAM-1.0

@include common-auth
@include common-account
@include common-password
session       required   pam_env.so readenv=1 user_readenv=0
@include common-session-noninteractive
'''
DESKTOP = '5f3a9c0e7b1d4a2688c4e1f0a9b7d6c3'
PHONE = 'a1b2c3d4_e5f6_4789_abcd_ef0123456789'
SECOND = 'b1b2c3d4_e5f6_4789_abcd_ef0123456789'


def run(*command, data=None):
    return subprocess.run(command, input=data, capture_output=True, check=True).stdout


class Keys:
    """A desktop certificate, a phone certificate and phone signing keys, made once."""
    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix='phonekey-keys-')
        self.certs = {}
        for name in ('desktop', 'phone'):
            key, cert = os.path.join(self.directory, name + '.key'), os.path.join(self.directory, name + '.crt')
            run(OPENSSL, 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-keyout', key, '-out', cert,
                '-subj', '/CN=' + name, '-days', '2')
            self.certs[name] = Path(cert).read_text()
        self.signers = {}
        for name in ('enrolled', 'other'):
            key = os.path.join(self.directory, name + '.pem')
            run(OPENSSL, 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', key)
            self.signers[name] = (key, run(OPENSSL, 'pkey', '-in', key, '-pubout', '-outform', 'DER'))

    def digest(self, name):
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.certs[name])).digest()

    def spki(self, name='enrolled'):
        return self.signers[name][1]

    def sign(self, data, name='enrolled'):
        return run(OPENSSL, 'dgst', '-sha256', '-sign', self.signers[name][0], data=data)


class Quiet:
    """Stands in for the syslog module so tests never write to the host's log."""
    LOG_PID = LOG_AUTHPRIV = LOG_NOTICE = 0

    def __init__(self):
        self.lines = []

    def openlog(self, *args):
        pass

    def syslog(self, priority, line):
        self.lines.append(line)


@unittest.skipUnless(USABLE, 'needs the openssl command and an ordinary user')
class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = Keys()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keys.directory, ignore_errors=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='phonekey-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'root'
        shutil.copytree(COMPONENT / 'rootfs', self.root, ignore=shutil.ignore_patterns('__pycache__'))
        for path in [self.root, *self.root.rglob('*')]:
            os.chmod(path, 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644)
        (self.root / 'etc/pam.d').mkdir(parents=True)
        (self.root / 'usr/lib/pam.d').mkdir(parents=True)
        (self.root / 'etc/pam.d/sudo').write_bytes(SUDO)
        (self.root / 'usr/lib/pam.d/polkit-1').write_bytes(POLKIT)
        self.store = Path(self.temp.name) / 'store'
        self.store.mkdir()
        self.write_store({PHONE: True})
        self.paths = tool.Paths(str(self.root), owner=os.getuid())
        self.syslog = Quiet()
        patcher = patch.dict(sys.modules, {'syslog': self.syslog})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1_794_480_000.0
        self.asked = []
        self.proc = Path(self.temp.name) / 'proc'
        self.parent(['sudo', 'apt', 'upgrade'])
        # By default the request comes from a graphical session on the local seat.
        self.cgroup('0::/user.slice/user-1000.slice/session-3.scope\n')
        self.session = {'Remote': 'no', 'Seat': 'seat0', 'Class': 'user', 'Type': 'wayland', 'Service': 'greetd'}
        self.sessions_asked = []

    # -- fixtures --
    def write_store(self, phones, key='enrolled'):
        (self.store / 'identity.json').write_text(json.dumps({'id': DESKTOP, 'key_pem': '', 'cert_pem': self.keys.certs['desktop']}))
        certificate = ssl.PEM_cert_to_DER_cert(self.keys.certs['phone']).hex()
        peers = {device: {'id': device, 'name': 'Fixture phone', 'kind': 'phone', 'certificate': certificate,
                          'phonekey': {'public_key': base64.b64encode(self.keys.spki(key)).decode(), 'enrolled_at': 1, 'unlock': False, 'approve': approve}}
                 for device, approve in phones.items()}
        (self.store / 'peers.json').write_text(json.dumps(peers))

    def parent(self, argv, exe='/usr/bin/sudo', pid=4242):
        directory = self.proc / str(pid)
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        os.symlink(exe, directory / 'exe')
        (directory / 'cmdline').write_bytes(b'\0'.join(word if isinstance(word, bytes) else word.encode() for word in argv) + b'\0')

    def cgroup(self, text):
        (self.proc / 'self').mkdir(parents=True, exist_ok=True)
        (self.proc / 'self/cgroup').write_text(text)

    def logind(self, session_id):
        self.sessions_asked.append(session_id)
        if isinstance(self.session, Exception):
            raise self.session
        return dict(self.session)

    def tool(self, *argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'stderr', err), patch.object(sys, 'stdin', io.StringIO(stdin)):
            code = tool.main(['--root', str(self.root), *argv], out=out)
        return code, out.getvalue() + err.getvalue()

    def enable(self, *extra):
        return self.tool('enable', '--user', ME.pw_name, '--store', str(self.store), '--yes', *extra)

    def conf(self, **values):
        (self.root / 'etc/desos/phonekey/phonekey.conf').write_text(''.join(f'{name} = {value}\n' for name, value in values.items()))

    def phone(self, decision='approved', change=None, signer='enrolled', **shown):
        """An honest daemon and phone: the phone signs what it was sent and would
        show, bound to its own view of the pairing. `shown` overrides one of those."""
        def send(paths, account, request, patience):
            self.asked.append(request)
            seen = dict(purpose=request['purpose'], user=request['user'], action=request['action'], desktop_cert=self.keys.digest('desktop'))
            seen.update(shown)
            data = tool.message(seen['purpose'], DESKTOP, seen['desktop_cert'], PHONE, self.keys.digest('phone'), hashlib.sha256(self.keys.spki(signer)).digest(),
                                bytes.fromhex(request['request_id']), base64.b64decode(request['nonce']), seen['user'], seen['action'],
                                request['issued_at'], request['expires_at'])
            answer = {'ok': True, 'decision': decision, 'request_id': request['request_id']}
            if decision == 'approved':
                answer['signature'] = base64.b64encode(self.keys.sign(data, signer)).decode()
            if change:
                answer = change(answer, request)
            return answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return send

    def auth(self, send=None, service='sudo', user=None, pam_type='auth', paths=None, **pam):
        self.out = io.StringIO()
        env = {'PAM_TYPE': pam_type, 'PAM_SERVICE': service, 'PAM_USER': ME.pw_name if user is None else user,
               'PAM_TTY': '/dev/pts/2', 'PAM_RHOST': ''}
        env.update(pam)
        return tool.pam_auth(paths or self.paths, env, send=send or self.phone(), out=self.out, clock=lambda: self.now,
                             parent=4242, proc=str(self.proc), session=self.logind)

    def log(self):
        path = self.root / 'var/log/desos-phonekey.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def decisions(self):
        return [entry.get('decision', entry.get('reason')) for entry in self.log() if entry['event'] in ('decision', 'skipped')]


class ComponentShape(unittest.TestCase):
    def files(self):
        return sorted(str(path.relative_to(COMPONENT / 'rootfs')) for path in (COMPONENT / 'rootfs').rglob('*')
                      if not path.is_dir() and '__pycache__' not in path.parts)

    def test_manifest_follows_the_component_convention(self):
        manifest = json.loads((COMPONENT / 'component.json').read_text())
        self.assertEqual(set(manifest), {'name', 'packages', 'enable_units', 'notes'})
        self.assertEqual(manifest['name'], 'phonekey')
        self.assertEqual(manifest['enable_units'], [], 'nothing is started or enabled')
        self.assertTrue({'python3', 'openssl', 'libpam-modules'} <= set(manifest['packages']))
        self.assertIsInstance(manifest['notes'], str)

    def test_the_image_builder_accepts_the_component(self):
        path = HERE / 'image/components.py'
        if not path.exists():
            self.skipTest('image/components.py is not in this tree')
        sys.path.insert(0, str(path.parent))
        try:
            components = load('desos_components', path)
        finally:
            sys.path.remove(str(path.parent))
        component = components.load_manifest(COMPONENT, 'phonekey')
        self.assertIn('phonekey', components.COMPONENTS)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directories, copies = components.plan(root, [component])
            modes = {str(target.relative_to(root)): mode for _, target, mode in copies}
        self.assertEqual(sorted(modes), self.files())
        self.assertEqual({name for name, mode in modes.items() if mode == 0o755}, {'usr/sbin/desos-phonekey'})

    def test_it_ships_no_pam_configuration_and_no_symlinks(self):
        self.assertEqual(self.files(), ['etc/desos/phonekey/phonekey.conf', 'usr/sbin/desos-phonekey', 'usr/share/desos/phonekey/README',
                                        'usr/share/desos/phonekey/pam/polkit-1.example', 'usr/share/desos/phonekey/pam/sudo.example'])
        for path in (COMPONENT / 'rootfs').rglob('*'):
            self.assertFalse(path.is_symlink(), path)
            relative = str(path.relative_to(COMPONENT / 'rootfs'))
            for reserved in ('etc/pam.d', 'usr/lib/pam.d', 'usr/share/pam-configs', 'etc/security', 'etc/sudoers', 'etc/polkit-1', 'usr/share/polkit-1',
                             'etc/systemd', 'usr/lib/systemd', 'etc/desos/phonekey/users'):
                self.assertFalse(relative == reserved or relative.startswith(reserved + '/'), relative)
        self.assertTrue(os.access(TOOL, os.X_OK))
        self.assertEqual(TOOL.read_text().splitlines()[0], '#!/usr/bin/python3 -I', 'isolated mode: no PYTHON* variables, no user site')

    def test_the_examples_show_exactly_what_enable_writes(self):
        for name, stock in (('sudo', SUDO), ('polkit-1', POLKIT)):
            example = (COMPONENT / f'rootfs/usr/share/desos/phonekey/pam/{name}.example').read_text()
            self.assertIn('EXAMPLE ONLY', example.splitlines()[0])
            written = tool.insert_block(stock.decode()).splitlines()
            block = written[written.index(tool.BEGIN):written.index(tool.END) + 2]
            self.assertEqual(example.splitlines()[example.splitlines().index(tool.BEGIN):][:len(block)], block)

    def test_the_shipped_settings_file_only_restates_the_defaults(self):
        text = (COMPONENT / 'rootfs/etc/desos/phonekey/phonekey.conf').read_text()
        active = dict(line.split('=') for line in text.splitlines() if line and not line.startswith('#'))
        self.assertEqual({name.strip(): int(value) for name, value in active.items()}, {name: default for name, (default, _, _) in tool.DEFAULTS.items()})


class PamEditing(unittest.TestCase):
    SAMPLES = [SUDO.decode(), POLKIT.decode(), 'auth required pam_unix.so', 'auth\trequired\tpam_unix.so\n', '#auth is a comment\n-auth optional pam_x.so\naccount required pam_unix.so\n',
               '#%PAM-1.0\r\n@include common-auth\r\n@include common-account\r\n', '\n\n  auth  sufficient pam_rootok.so\nauth required pam_unix.so\n\n\n',
               'session required pam_limits.so\n@include common-auth', 'caf\xe9 \xff\n@include common-auth\n']

    def test_the_block_goes_before_the_first_auth_rule_and_comes_out_byte_for_byte(self):
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                edited = tool.insert_block(sample)
                lines = edited.split('\n')
                at = lines.index(tool.BEGIN)
                self.assertEqual(lines[at:at + 3], [tool.BEGIN, tool.RULE, tool.END])
                self.assertTrue(tool.AUTH_LINE.match(lines[at + 3]))
                self.assertFalse(any(tool.AUTH_LINE.match(line) for line in lines[:at]), 'nothing authenticates before the helper is tried')
                self.assertEqual(lines[:at] + lines[at + 3:], sample.split('\n'), 'no other line changed')
                self.assertEqual(tool.remove_block(edited), sample)
                self.assertEqual(tool.remove_block(sample), sample, 'removing from an untouched file is a no-op')

    def test_the_rule_can_only_ever_add_a_way_in_never_take_the_password_away(self):
        control, module, *arguments = tool.RULE.split()[1:]
        self.assertEqual(tool.RULE.split()[0], 'auth')
        self.assertEqual(control, 'sufficient', 'a failing sufficient module is ignored and the stack goes on')
        self.assertEqual(module, 'pam_exec.so')
        self.assertNotIn('expose_authtok', arguments, 'the helper is never handed a password')
        self.assertEqual(arguments[-2:], ['/usr/sbin/desos-phonekey', 'pam-auth'])
        self.assertIn('seteuid', arguments)

    def test_files_it_cannot_edit_safely_are_refused(self):
        for text in ('', 'session required pam_limits.so\n', '# auth required pam_unix.so\n', tool.insert_block(SUDO.decode()),
                     SUDO.decode() + '# /usr/sbin/desos-phonekey mentioned by hand\n'):
            with self.subTest(text=text), self.assertRaises(tool.Refused):
                tool.insert_block(text)

    def test_a_damaged_block_is_reported_not_guessed_at(self):
        good = tool.insert_block(SUDO.decode())
        damaged = [good.replace(tool.RULE, tool.RULE.replace('sufficient', 'required')), good.replace(tool.END + '\n', ''),
                   good.replace(tool.BEGIN + '\n', ''), good + tool.BEGIN + '\n' + tool.RULE + '\n' + tool.END + '\n',
                   good.replace(tool.RULE, tool.RULE + '\nauth sufficient pam_permit.so')]
        for text in damaged:
            with self.subTest(text=text), self.assertRaises(tool.Refused):
                tool.remove_block(text)


class EnableDisable(Fixture):
    def pam(self, name='sudo'):
        return (self.root / 'etc/pam.d' / name).read_bytes()

    def test_a_fresh_image_is_off_and_its_pam_files_are_untouched(self):
        code, text = self.tool('status', '--json')
        report = json.loads(text)
        self.assertEqual((code, report['active'], report['users']), (0, False, {}))
        self.assertEqual(report['pam'], {'sudo': 'clean', 'sudo-i': 'absent', 'polkit-1': 'absent'})
        self.assertEqual(self.pam(), SUDO)
        self.assertFalse((self.root / 'etc/desos/phonekey/users').exists())
        # The helper on a fresh image: no prompt, no request, no log.
        self.assertEqual(self.auth(), 1)
        self.assertEqual((self.asked, self.out.getvalue(), self.log(), self.syslog.lines), ([], '', [], []))

    def test_enable_then_disable_restores_the_pam_file_byte_for_byte(self):
        os.chmod(self.root / 'etc/pam.d/sudo', 0o640)
        code, text = self.enable()
        self.assertEqual(code, 0, text)
        self.assertIn(tool.fingerprint(hashlib.sha256(self.keys.spki()).digest()), text)
        self.assertEqual(self.pam().decode(), tool.insert_block(SUDO.decode()))
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'etc/pam.d/sudo').st_mode), 0o640)
        self.assertEqual((self.root / 'var/lib/desos-phonekey/pam/sudo.orig').read_bytes(), SUDO)
        self.assertFalse((self.root / 'etc/pam.d/polkit-1').exists(), 'polkit is not touched unless asked for by name')
        record = json.loads((self.root / f'etc/desos/phonekey/users/{ME.pw_name}.json').read_text())
        self.assertEqual((record['services'], record['device_id'], record['uid']), (['sudo'], PHONE, os.getuid()))
        self.assertEqual(record['desktop_cert'], self.keys.digest('desktop').hex())
        self.assertEqual(record['phone_cert'], self.keys.digest('phone').hex())
        self.assertEqual(json.loads(self.tool('status', '--json')[1])['active'], True)
        self.assertEqual(self.enable()[0], 0)
        self.assertEqual(self.pam().count(tool.BEGIN.encode()), 1, 'enabling twice adds one block')

        code, text = self.tool('disable')
        self.assertEqual(code, 0, text)
        self.assertIn('byte for byte', text)
        self.assertEqual(self.pam(), SUDO)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / 'etc/pam.d/sudo').st_mode), 0o640)
        self.assertEqual(list((self.root / 'etc/desos/phonekey/users').iterdir()), [])
        self.assertEqual(list((self.root / 'var/lib/desos-phonekey/pam').iterdir()), [])
        self.assertEqual(json.loads(self.tool('status', '--json')[1])['active'], False)
        self.assertEqual(self.tool('disable')[0], 0, 'disabling twice is harmless')
        self.assertEqual(self.pam(), SUDO)

    def test_polkit_is_a_separate_choice_made_as_an_override_and_removed_again(self):
        self.assertEqual(self.enable('--service', 'sudo', '--service', 'polkit-1')[0], 0)
        self.assertEqual(self.pam('polkit-1').decode(), tool.insert_block(POLKIT.decode()))
        self.assertEqual((self.root / 'usr/lib/pam.d/polkit-1').read_bytes(), POLKIT, 'the vendor file is never edited')
        self.assertEqual(self.tool('disable')[0], 0)
        self.assertFalse((self.root / 'etc/pam.d/polkit-1').exists(), 'the override this tool created is gone')
        self.assertEqual((self.root / 'usr/lib/pam.d/polkit-1').read_bytes(), POLKIT)
        self.assertEqual(self.pam(), SUDO)

    def test_disable_keeps_changes_someone_else_made_meanwhile(self):
        self.assertEqual(self.enable()[0], 0)
        with open(self.root / 'etc/pam.d/sudo', 'ab') as handle:
            handle.write(b'session optional pam_lastlog.so\n')
        code, text = self.tool('disable')
        self.assertEqual(code, 0)
        self.assertIn('other changes', text)
        self.assertEqual(self.pam(), SUDO + b'session optional pam_lastlog.so\n')

    def test_a_refused_enable_changes_nothing(self):
        (self.root / 'usr/lib/pam.d/polkit-1').write_bytes(b'session required pam_env.so\n')
        code, text = self.enable('--service', 'sudo', '--service', 'polkit-1')
        self.assertEqual(code, 1)
        self.assertIn('no auth rule', text)
        self.assertEqual(self.pam(), SUDO)
        self.assertFalse((self.root / 'etc/desos/phonekey/users').exists())
        (self.root / 'etc/pam.d/sudo').write_bytes(tool.insert_block(SUDO.decode()).replace('sufficient', 'required').encode())
        self.assertEqual(self.enable()[0], 1, 'a hand-edited block is not built upon')

    def test_enable_needs_an_enrolled_phone_the_user_switched_on(self):
        self.write_store({PHONE: False})
        code, text = self.enable()
        self.assertEqual(code, 1)
        self.assertIn('phonekey-set', text)
        self.write_store({PHONE: True, SECOND: True})
        code, text = self.enable()
        self.assertEqual(code, 1)
        self.assertIn(SECOND, text)
        self.assertEqual(self.enable('--device', SECOND)[0], 0)
        self.assertEqual(json.loads((self.root / f'etc/desos/phonekey/users/{ME.pw_name}.json').read_text())['device_id'], SECOND)
        self.assertEqual(self.tool('disable')[0], 0)
        (self.store / 'peers.json').write_text('{"' + PHONE + '": {"certificate": "zz", "phonekey": {"approve": true, "public_key": "AAAA"}}}')
        self.assertEqual(self.enable()[0], 1, 'a record that is not a P-256 key is not a phone')
        self.assertEqual(self.pam(), SUDO)

    def test_enable_asks_before_changing_anything(self):
        code, text = self.tool('enable', '--user', ME.pw_name, '--store', str(self.store), stdin='no\n')
        self.assertEqual(code, 1)
        self.assertIn('nothing was changed', text)
        self.assertEqual(self.pam(), SUDO)
        self.assertEqual(self.tool('enable', '--user', ME.pw_name, '--store', str(self.store), stdin='yes\n')[0], 0)

    def test_root_and_unknown_users_are_refused(self):
        for user in ('root', 'no-such-user-here', '../etc'):
            self.assertEqual(self.tool('enable', '--user', user, '--store', str(self.store), '--yes')[0], 1, user)
        self.assertEqual(self.pam(), SUDO)

    def test_disabling_one_user_leaves_the_other_working(self):
        self.assertEqual(self.enable()[0], 0)
        users = self.root / 'etc/desos/phonekey/users'
        record = json.loads((users / f'{ME.pw_name}.json').read_text())
        (users / 'someone.json').write_text(json.dumps(dict(record, user='someone', uid=os.getuid() + 1)))
        self.assertEqual(self.tool('disable', '--user', ME.pw_name)[0], 0)
        self.assertEqual(self.pam().decode(), tool.insert_block(SUDO.decode()), 'someone else still uses the block')
        self.assertEqual([path.name for path in users.iterdir()], ['someone.json'])
        self.assertEqual(self.tool('disable')[0], 0)
        self.assertEqual(self.pam(), SUDO)

    def test_the_real_system_is_never_changed_without_root(self):
        if os.geteuid() == 0:
            self.skipTest('running as root')
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            self.assertEqual(tool.main(['enable', '--user', ME.pw_name, '--yes']), 2)
            self.assertEqual(tool.main(['disable']), 2)
            self.assertEqual(tool.main(['pam-auth']), 1, 'the helper only ever runs as root, from PAM')
            self.assertEqual(tool.main(['pam-auth', '--root', str(self.root)]), 1, 'and takes no options')


class Approval(Fixture):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.enable()[0], 0)

    def test_a_signed_answer_over_this_request_is_the_only_thing_that_approves(self):
        self.assertEqual(self.auth(), 0)
        request = self.asked[0]
        self.assertEqual((request['op'], request['purpose'], request['user'], request['action'], request['device_id']),
                         ('phonekey_request', 'approve', ME.pw_name, 'sudo apt upgrade', PHONE))
        self.assertEqual((len(bytes.fromhex(request['request_id'])), len(base64.b64decode(request['nonce']))), (16, 32))
        self.assertEqual(request['expires_at'] - request['issued_at'], 20)
        self.assertIn('Approve on Fixture phone: sudo apt upgrade', self.out.getvalue())
        self.assertIn('password', self.out.getvalue())
        events = [(entry['event'], entry.get('decision')) for entry in self.log() if entry['event'] != 'enabled']
        self.assertEqual(events, [('request', None), ('decision', 'approved')])
        self.assertEqual(self.log()[-1]['action'], 'sudo apt upgrade')
        self.assertEqual(len(self.syslog.lines), len(self.log()), 'the same trail goes to the auth log')

    def test_every_request_has_a_new_nonce_and_an_old_answer_is_worthless(self):
        kept = {}

        def remember(answer, request):
            kept.setdefault('answer', answer)
            return answer
        self.assertEqual(self.auth(self.phone(change=remember)), 0)
        self.now += 5
        self.assertEqual(self.auth(self.phone(change=lambda answer, request: kept['answer'])), 1, 'a replayed answer names the old request')
        self.now += 100
        self.assertEqual(self.auth(self.phone(change=lambda answer, request: dict(kept['answer'], request_id=request['request_id']))), 1,
                         'and its signature does not cover the new nonce')
        self.assertEqual(self.decisions(), ['approved', 'malformed', 'bad_signature'])
        first, second, third = self.asked
        self.assertEqual(len({first['nonce'], second['nonce'], third['nonce']}), 3)
        self.assertEqual(len({first['request_id'], second['request_id'], third['request_id']}), 3)

    def test_anything_but_a_valid_signature_falls_back_to_the_password(self):
        other = hashlib.sha256(b'another desktop').digest()
        cases = {
            'declined on the phone': (self.phone('denied'), 'denied'),
            'timed out': (self.phone('timeout'), 'timeout'),
            'approved but unsigned': (self.phone(change=lambda a, r: {k: v for k, v in a.items() if k != 'signature'}), 'malformed'),
            'signature is not base64': (self.phone(change=lambda a, r: dict(a, signature='!!!')), 'bad_signature'),
            'signature is empty': (self.phone(change=lambda a, r: dict(a, signature='')), 'bad_signature'),
            'signature is enormous': (self.phone(change=lambda a, r: dict(a, signature='A' * 5000)), 'malformed'),
            'signature is a number': (self.phone(change=lambda a, r: dict(a, signature=7)), 'malformed'),
            'signed by another key': (self.phone(signer='other'), 'bad_signature'),
            'phone showed other text': (self.phone(action='sudo apt update'), 'bad_signature'),
            'phone showed another user': (self.phone(user='mallory'), 'bad_signature'),
            'an unlock signature': (self.phone(purpose='unlock'), 'bad_signature'),
            'an enrolment signature': (self.phone(purpose='enrol'), 'bad_signature'),
            'signed for another desktop': (self.phone(desktop_cert=other), 'bad_signature'),
            'unknown decision word': (self.phone(change=lambda a, r: dict(a, decision='APPROVED')), 'malformed'),
            'answer for another request': (self.phone(change=lambda a, r: dict(a, request_id='00' * 16)), 'malformed'),
        }
        for name, (send, expected) in cases.items():
            with self.subTest(name=name):
                (self.root / f'var/lib/desos-phonekey/{ME.pw_name}.state').unlink(missing_ok=True)
                self.assertEqual(self.auth(send), 1)
                self.assertEqual(self.decisions()[-1], expected)
                self.assertNotIn('Approved', self.out.getvalue())

    def test_a_daemon_that_cannot_ask_is_not_a_failure_and_never_an_approval(self):
        def broken(error):
            def send(paths, account, request, patience):
                raise error
            return send
        answers = [self.phone(change=lambda a, r: {'ok': False, 'error': 'the phone is not connected'}), self.phone(change=lambda a, r: b'not json'),
                   self.phone(change=lambda a, r: b'[1, 2]'), self.phone(change=lambda a, r: b'\xff\xfe'), self.phone(change=lambda a, r: b''),
                   self.phone(change=lambda a, r: {'ok': 'true', 'decision': 'approved'}),
                   broken(FileNotFoundError('no socket')), broken(tool.Refused('timed out waiting for the user side')), broken(ConnectionRefusedError())]
        for send in answers:
            self.assertEqual(self.auth(send), 1)
            self.assertTrue(self.decisions()[-1].startswith('unavailable'), self.decisions()[-1])
        self.assertEqual(self.auth(), 0, 'none of that locked the phone out')

    def test_the_helper_never_raises_whatever_goes_wrong(self):
        for error in (KeyboardInterrupt(), SystemExit(0), MemoryError(), RuntimeError('boom')):
            def send(paths, account, request, patience, error=error):
                raise error
            self.assertEqual(self.auth(send), 1)

    def test_an_answer_that_arrives_after_the_expiry_is_refused(self):
        def late(answer, request):
            self.now = request['expires_at']
            return answer
        self.assertEqual(self.auth(self.phone(change=late)), 1)
        self.assertEqual(self.decisions()[-1], 'expired')

    def test_it_is_inert_for_anyone_and_anything_not_enabled(self):
        quiet = [dict(user='someone-else'), dict(service='polkit-1'), dict(service='login'), dict(service='sshd'), dict(pam_type='account'),
                 dict(pam_type='open_session'), dict(user='../' + ME.pw_name), dict(user=''), dict(user='root')]
        before = self.log()
        for arguments in quiet:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.auth(**arguments), 1)
                self.assertEqual((self.asked, self.out.getvalue()), ([], ''))
        self.assertEqual(self.log(), before, 'not enabled means not even logged')
        self.assertEqual(self.tool('disable')[0], 0)
        self.assertEqual(self.auth(), 1)
        self.assertEqual(self.asked, [])

    def test_the_record_must_be_roots_own_and_match_the_account(self):
        path = self.root / f'etc/desos/phonekey/users/{ME.pw_name}.json'
        original = path.read_text()
        os.chmod(path, 0o666)
        self.assertEqual(self.auth(), 1, 'a record others can write is not believed')
        os.chmod(path, 0o644)
        os.chmod(path.parent, 0o777)
        self.assertEqual(self.auth(), 1, 'nor one in a directory others can write')
        os.chmod(path.parent, 0o755)
        self.assertEqual(self.auth(paths=tool.Paths(str(self.root), owner=os.getuid() + 1)), 1, 'nor one owned by the wrong user')
        for change in (dict(uid=os.getuid() + 1), dict(user='someone'), dict(version=2), dict(public_key='AAAA'), dict(desktop_cert='xyz'), dict(services=['login'])):
            path.write_text(json.dumps(dict(json.loads(original), **change)))
            self.assertEqual(self.auth(), 1, change)
        path.write_text('{')
        self.assertEqual(self.auth(), 1)
        os.unlink(path)
        os.symlink(self.temp.name + '/elsewhere.json', path)
        Path(self.temp.name, 'elsewhere.json').write_text(original)
        self.assertEqual(self.auth(), 1, 'a symlink is not a record')
        self.assertEqual(self.asked, [], 'and the phone was never asked')
        os.unlink(path)
        path.write_text(original)
        self.assertEqual(self.auth(), 0)

    def test_failures_cool_down_then_lock_the_phone_out_and_the_password_still_works(self):
        self.assertEqual(self.auth(self.phone('denied')), 1)
        self.now += 59
        self.assertEqual(self.auth(), 1, 'no second prompt while a wrong password is being retried')
        self.assertEqual((len(self.asked), self.decisions()[-1], self.out.getvalue()), (1, 'cooling_down', ''))
        self.now += 1
        self.assertEqual(self.auth(), 0, 'after the cool-down the phone is asked again, and success clears the count')

        self.conf(cooldown_seconds=0, max_failures=3, lockout_seconds=600)
        for _ in range(3):
            self.now += 1
            self.assertEqual(self.auth(self.phone('denied')), 1)
        asked = len(self.asked)
        self.now += 1
        self.assertEqual(self.auth(), 1)
        self.assertEqual((len(self.asked), self.decisions()[-1]), (asked, 'locked_out'))
        self.now += 598
        self.assertEqual(self.auth(), 1)
        self.now += 2
        self.assertEqual(self.auth(), 0, 'the lockout ends on its own')

    def test_prompts_are_rate_limited(self):
        self.conf(max_requests_per_10_minutes=2, cooldown_seconds=0)
        self.assertEqual([self.auth(), self.auth(), self.auth()], [0, 0, 1])
        self.assertEqual((len(self.asked), self.decisions()[-1]), (2, 'rate_limited'))
        self.now += 600
        self.assertEqual(self.auth(), 0)

    def test_settings_are_clamped_and_a_bad_file_means_the_defaults(self):
        defaults = {name: default for name, (default, _, _) in tool.DEFAULTS.items()}
        self.assertEqual(tool.settings(self.paths), defaults)
        self.conf(timeout_seconds=100000, max_failures=0, lockout_seconds=1, cooldown_seconds='soon', unknown=5)
        self.assertEqual(tool.settings(self.paths), dict(defaults, timeout_seconds=60, max_failures=1, lockout_seconds=60))
        os.chmod(self.root / 'etc/desos/phonekey/phonekey.conf', 0o666)
        self.assertEqual(tool.settings(self.paths), defaults, 'a settings file others can write is ignored')

    def test_the_text_on_the_phone_is_worked_out_by_root_not_by_the_session(self):
        describe = lambda service='sudo': tool.describe(service, 'coxy', 4242, str(self.proc))
        self.parent(['sudo', 'rm', '-rf', '/var/lib/some dir', 'it\'s'])
        self.assertEqual(describe(), "sudo rm -rf '/var/lib/some dir' 'it'\"'\"'s'")
        self.parent(['/usr/bin/sudo', '-u', 'postgres', 'psql'])
        self.assertEqual(describe('sudo-i'), 'sudo -u postgres psql')
        self.parent(['sudo', 'echo', 'safe‮gnp.exe', 'two\nlines', b'\xff\x00'.rstrip(b'\0')])
        text = describe()
        self.assertTrue(tool.valid_action(text), text)
        self.assertNotIn('‮', text)
        self.assertNotIn('\n', text)
        self.parent(['sudo', 'sh', '-c', 'x' * 5000])
        text = describe()
        self.assertTrue(tool.valid_action(text) and len(text.encode()) <= 512, text)
        self.assertIn('[cut; ', text)
        self.assertIn('sha256 ', text)
        self.parent(['sudo', 'true'], exe='/usr/bin/python3')
        self.assertEqual(describe(), 'sudo as coxy (the command could not be read)')
        self.assertEqual(tool.describe('sudo', 'coxy', 99999, str(self.proc)), 'sudo as coxy (the command could not be read)')
        self.parent(['sudo', 'true'])
        self.assertIn('polkit', describe('polkit-1'))
        self.assertNotIn('true', describe('polkit-1'), 'PAM is not told the polkit action, so none is claimed')
        self.assertTrue(tool.valid_action(describe('polkit-1')))


class Locality(Fixture):
    """Phone approval is declined for remote sessions and whenever locality cannot be shown."""
    def setUp(self):
        super().setUp()
        self.assertEqual(self.enable()[0], 0)

    def declined(self, reason):
        self.assertEqual(self.auth(), 1)
        self.assertEqual(self.asked, [], 'the phone must not even be asked')
        entry = self.log()[-1]
        self.assertEqual((entry['event'], entry['reason']), ('skipped', reason))
        self.assertIn('use your password', self.out.getvalue())
        self.assertNotIn('Approve on', self.out.getvalue())
        return entry

    def test_a_local_session_on_a_seat_is_asked_and_the_session_comes_from_the_cgroup(self):
        self.assertEqual(self.auth(), 0)
        self.assertEqual(self.sessions_asked, ['3'])
        self.assertEqual(self.log()[-1]['tty'], '/dev/pts/2')
        self.session.update(Type='tty', Service='login')
        self.now += 100
        self.assertEqual(self.auth(PAM_TTY='/dev/tty2'), 0, 'a text console on the seat is local too')

    def test_sudo_in_an_ssh_session_falls_through_to_the_password(self):
        self.cgroup('0::/user.slice/user-1000.slice/session-7.scope\n')
        self.session = {'Remote': 'yes', 'Seat': '', 'Class': 'user', 'Type': 'tty', 'Service': 'sshd'}
        entry = self.declined('remote_session')
        self.assertEqual(self.sessions_asked, ['7'])
        self.assertIn('session 7', entry['detail'])
        # Nothing was counted against the phone: no cooldown, no failure.
        self.assertFalse((self.root / f'var/lib/desos-phonekey/{ME.pw_name}.state').exists())
        self.session = {'Remote': 'no', 'Seat': 'seat0', 'Class': 'user', 'Type': 'tty', 'Service': 'sshd'}
        self.declined('remote_session')

    def test_a_remote_host_named_by_pam_is_refused_without_asking_logind(self):
        self.assertEqual(self.auth(PAM_RHOST='203.0.113.9'), 1)
        self.assertEqual((self.asked, self.sessions_asked), ([], []))
        self.assertEqual(self.log()[-1]['reason'], 'remote_session')
        self.assertEqual(self.log()[-1]['rhost'], '203.0.113.9')
        # sudo's pam_rhost option reports this machine's own name: not evidence of a remote user.
        self.assertEqual(self.auth(PAM_RHOST=socket.gethostname()), 0)

    def test_it_fails_closed_when_it_cannot_tell(self):
        cases = {
            'a user-manager scope (systemd-run --user), not a login session': '0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-u12.scope\n',
            'a system service': '0::/system.slice/polkit-agent-helper@3.service\n',
            'no usable line': 'garbage\n',
            'two different sessions': '1:name=systemd:/user.slice/user-1000.slice/session-3.scope\n0::/user.slice/user-1000.slice/session-4.scope\n',
            'a name that only looks like a scope': '0::/user.slice/xsession-3.scope.d\n',
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.cgroup(text)
                self.declined('locality_unknown')
        self.assertEqual(self.sessions_asked, [])
        (self.proc / 'self/cgroup').unlink()
        self.declined('locality_unknown')
        self.cgroup('0::/user.slice/user-1000.slice/session-3.scope\n')
        for session in (tool.Refused('loginctl could not describe the session'), OSError('no loginctl'),
                        subprocess.TimeoutExpired('loginctl', 5), {}, {'Remote': 'no', 'Seat': '', 'Class': 'user'},
                        {'Remote': 'no', 'Seat': 'seat0', 'Class': 'greeter'}, {'Seat': 'seat0', 'Class': 'user'}):
            with self.subTest(session=session):
                self.session = session
                self.declined('locality_unknown')

    def test_the_callers_environment_is_not_evidence(self):
        self.session = {'Remote': 'yes', 'Seat': '', 'Class': 'user', 'Type': 'tty', 'Service': 'sshd'}
        self.assertEqual(self.auth(XDG_SESSION_ID='3', XDG_SEAT='seat0', PAM_TTY='/dev/tty1'), 1)
        self.assertEqual(self.asked, [])

    def test_someone_not_enrolled_is_still_told_and_logged_nothing(self):
        self.session = {'Remote': 'yes', 'Seat': '', 'Class': 'user'}
        before = self.log()
        self.assertEqual(self.auth(user='someone-else'), 1)
        self.assertEqual((self.out.getvalue(), self.log()), ('', before))

    def test_the_real_query_asks_loginctl_for_exactly_that_session(self):
        seen = {}

        def fake(argv, **options):
            seen['argv'], seen['env'] = argv, options['env']
            return types.SimpleNamespace(returncode=0, stdout=b'Remote=no\nSeat=seat0\nClass=user\nType=wayland\nService=greetd\n')
        with patch.object(tool.subprocess, 'run', side_effect=fake):
            self.assertEqual(tool.locality({'PAM_RHOST': ''}, str(self.proc)), (True, 'session 3 on seat0'))
        self.assertEqual(seen['argv'][:3], ['/usr/bin/loginctl', 'show-session', '3'])
        self.assertNotIn('XDG_SESSION_ID', seen['env'])
        with patch.object(tool.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1, stdout=b'')):
            self.assertFalse(tool.locality({'PAM_RHOST': ''}, str(self.proc))[0])
        source = TOOL.read_text()
        self.assertIn("'PAM_RHOST', 'PAM_TTY'", source)


class Wire(unittest.TestCase):
    def test_the_signed_message_matches_the_shared_vectors(self):
        vectors = json.loads((COMPONENT / 'testvectors.json').read_text())
        binding, key = vectors['binding'], base64.b64decode(vectors['public_key'])
        self.assertEqual(hashlib.sha256(key).hexdigest(), vectors['key_id'])
        self.assertEqual(tool.fingerprint(bytes.fromhex(vectors['key_id'])), vectors['fingerprint'])
        self.assertEqual(tool.spki(vectors['public_key']), key)
        self.assertEqual({vector['purpose'] for vector in vectors['vectors']}, {'enrol', 'unlock', 'approve'})
        for vector in vectors['vectors']:
            data = tool.message(vector['purpose'], binding['desktop_id'], bytes.fromhex(binding['desktop_cert']), binding['phone_id'], bytes.fromhex(binding['phone_cert']),
                                bytes.fromhex(vector['key_id'] if 'key_id' in vector else vectors['key_id']), bytes.fromhex(vector['request_id']),
                                base64.b64decode(vector['nonce']), vector['user'], vector['action'], vector['issued_at'], vector['expires_at'])
            self.assertEqual(data.hex(), vector['message'])
            self.assertTrue(data.startswith(b'DES-PHONEKEY-v1\x00'))
            if OPENSSL:
                signature = base64.b64decode(vector['signature'])
                self.assertTrue(tool.verify(key, data, signature))
                self.assertFalse(tool.verify(key, data + b'x', signature))
                self.assertFalse(tool.verify(key, data, signature[:-1] + bytes([signature[-1] ^ 1])))
                self.assertFalse(tool.verify(key, data, b''))
                self.assertFalse(tool.verify(key, data, signature + b'\0' * 80))

    def test_only_a_p256_key_is_a_phone_key(self):
        good = json.loads((COMPONENT / 'testvectors.json').read_text())['public_key']
        for bad in ('', 'AAAA', good[:-4], '!' + good[1:], base64.b64encode(b'\0' * 91).decode(), None, 5):
            with self.subTest(bad=bad), self.assertRaises(tool.Refused):
                tool.spki(bad)

    def test_action_text_rules(self):
        for good in ('sudo apt upgrade', "sudo apt install 'café tools' --yes", 'x' * 512):
            self.assertTrue(tool.valid_action(good), good)
        for bad in ('', ' padded', 'two\nlines', 'tab\there', 'bell\x07', 'rtl‮override', 'isolate⁦', 'x' * 513, 'nbsp x'):
            self.assertFalse(tool.valid_action(bad), bad)

    def test_the_daemon_socket_must_be_the_users_own_and_its_answer_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, 'control.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(2)
            replies = [b'{"ok": true, "decision": "denied"}\n', b'x' * 400000]
            seen = []

            def serve():
                for reply in replies:
                    link, _ = server.accept()
                    with link:
                        seen.append(link.makefile('rb').readline())
                        try:
                            link.sendall(reply)
                        except OSError:
                            pass
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            self.assertEqual(json.loads(tool.daemon_request(path, os.getuid(), {'op': 'phonekey_request'}, 5)), {'ok': True, 'decision': 'denied'})
            self.assertEqual(json.loads(seen[0]), {'op': 'phonekey_request'})
            with self.assertRaises(tool.Refused):
                tool.daemon_request(path, os.getuid(), {'op': 'phonekey_request'}, 5)
            thread.join(5)
            server.close()
            with self.assertRaises(tool.Refused):
                tool.daemon_request(path, os.getuid() + 1, {}, 1)
            plain = os.path.join(temp, 'plain')
            Path(plain).write_text('')
            with self.assertRaises(tool.Refused):
                tool.daemon_request(plain, os.getuid(), {}, 1)
            with self.assertRaises(OSError):
                tool.daemon_request(os.path.join(temp, 'missing'), os.getuid(), {}, 1)


if __name__ == '__main__':
    unittest.main()
