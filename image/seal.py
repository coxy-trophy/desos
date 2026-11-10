#!/usr/bin/env python3
"""Seal a completed clean DesOS rootfs in the isolated Ubuntu image worker.

All account, credential, mount and path checks precede every write. This does
not make a development-VM clone safe: its user accounts/homes cause refusal.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

try:
    from .paths import validate_rootfs
    from .iso_recipe import mounts_within, rooted
except ImportError:
    from paths import validate_rootfs
    from iso_recipe import mounts_within, rooted

BASE = Path('/srv/desos-build')
DIRECTORIES = (
    'etc', 'etc/ssh', 'etc/ssl/private', 'etc/ssl/certs', 'home', 'root', 'tmp', 'var/tmp', 'var/log',
    'var/cache/apt/archives', 'var/lib/dbus', 'var/lib/systemd',
    'var/lib/urandom', 'usr/sbin',
)
TRANSIENT = (
    'root/.bash_history', 'root/.zsh_history', 'root/.python_history',
    'root/.sqlite_history', 'root/.node_repl_history', 'root/.lesshst',
    'root/.wget-hsts', 'root/.viminfo', 'root/.ssh', 'root/.cache',
    'root/.gnupg', 'root/.local/share/fish/fish_history',
    'var/lib/systemd/random-seed', 'var/lib/urandom/random-seed',
    'usr/sbin/policy-rc.d', 'etc/ssl/private/ssl-cert-snakeoil.key',
    'etc/ssl/certs/ssl-cert-snakeoil.pem',
)
CREDENTIAL_NAMES = {
    'authorized_keys', 'authorized_keys2', 'id_rsa', 'id_dsa',
    'id_ecdsa', 'id_ed25519', 'id_ecdsa_sk', 'id_ed25519_sk',
}
PRIVATE_PEM = re.compile(rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')


def confined(root, relative, *, allow_leaf_symlink=False):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Invalid rootfs-relative seal path')
    target = root / relative
    components = list(target.parents)
    if not allow_leaf_symlink:
        components.insert(0, target)
    for component in components:
        if component == root:
            break
        if component.is_symlink():
            raise ValueError('Refusing symlink parent/path during seal: ' + str(relative))
    return target


def walk_nofollow(directory):
    if not directory.exists():
        return []
    paths = []
    for current, directories, files in os.walk(directory, followlinks=False):
        current = Path(current)
        for name in sorted(directories + files):
            paths.append(current / name)
        directories[:] = [name for name in directories if not (current / name).is_symlink()]
    return paths


def audit_accounts(root):
    databases = {}
    for relative in ('etc/passwd', 'etc/shadow'):
        path = confined(root, relative)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('Account database must be a private regular file: ' + relative)
        databases[relative] = path.read_text().splitlines()
    accounts = {}
    for line in databases['etc/passwd']:
        fields = line.split(':')
        if len(fields) != 7 or not fields[2].isdigit() or not fields[3].isdigit() or fields[0] in accounts:
            raise ValueError('Malformed or duplicate account in clean rootfs')
        name, uid = fields[0], int(fields[2])
        if (name in {'des', 'desos', 'build', 'builder'} or name.startswith('build')
                or (1000 <= uid <= 59999)
                or (uid == 0 and name != 'root')):
            raise ValueError('Non-system/development account present: ' + name)
        accounts[name] = fields
    if 'root' not in accounts or accounts['root'][2] != '0':
        raise ValueError('Normal root account is required')
    root_shadow = [line.split(':') for line in databases['etc/shadow'] if line.split(':', 1)[0] == 'root']
    if len(root_shadow) != 1 or len(root_shadow[0]) != 9 or not root_shadow[0][1].startswith(('!', '*')):
        raise ValueError('Root shadow password must be locked before sealing')
    return len(accounts)


def audit_credentials(root):
    for directory in ('home', 'root'):
        for path in walk_nofollow(root / directory):
            relative = path.relative_to(root)
            if path.name in CREDENTIAL_NAMES:
                raise ValueError('Refusing copied SSH credential: ' + str(relative))
            if path.name == 'secring.gpg' or ('private-keys-v1.d' in relative.parts and (path.is_symlink() or not path.is_dir())):
                raise ValueError('Refusing copied signing credential: ' + str(relative))
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_nlink != 1:
                raise ValueError('Refusing hard-linked root/home state: ' + str(relative))
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                if PRIVATE_PEM.search(stream.read(8192)):
                    raise ValueError('Refusing copied private credential: ' + str(relative))


def removal(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        # unlink never opens regular files, symlinks, sockets, FIFOs or devices.
        path.unlink()


def replace_empty(path, *, mode=0o644):
    # Replace the inode, rather than truncate a possible hard link to a host file.
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise ValueError('Expected a file at seal destination: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    os.close(descriptor)
    path.chmod(mode)


def preflight(root, parent, mountinfo):
    original = Path(root)
    root = validate_rootfs(original, Path(parent))
    if original != root or not root.is_dir():
        raise ValueError('Existing clean rootfs without symlink components required')
    mounted = mounts_within(root, mountinfo)
    if mounted:
        raise ValueError('Unmount every rootfs filesystem before seal: ' + ', '.join(mounted))
    for relative in DIRECTORIES:
        path = confined(root, relative)
        if path.exists() and not path.is_dir():
            raise ValueError('Expected rootfs directory: ' + relative)
    marker = confined(root, '.desos-packages-complete')
    if not marker.is_file():
        raise ValueError('Completed packages marker .desos-packages-complete required')
    identity = rooted(root, 'etc/os-release')
    confined(root, identity.relative_to(root))
    if not re.search(r'^ID=[\"\']?desos[\"\']?\s*$', identity.read_text(), re.MULTILINE):
        raise ValueError('Only a clean DesOS rootfs may be sealed')
    account_count = audit_accounts(root)
    audit_credentials(root)
    home = root / 'home'
    if home.exists() and any(home.iterdir()):
        raise ValueError('Clean rootfs /home must be empty; never seal development homes')
    remove_paths = [confined(root, relative, allow_leaf_symlink=True) for relative in TRANSIENT]
    for directory in ('tmp', 'var/tmp'):
        path = root / directory
        if path.exists():
            remove_paths.extend(path.iterdir())
    remove_paths.extend((root / 'etc/ssh').glob('ssh_host_*'))
    remove_paths.extend((root / 'var/cache/apt/archives').rglob('*.deb'))
    remove_paths.extend(root.glob('.desos-*-complete'))
    # Preflight every existing recursive entry before mutation; never follow links.
    for path in remove_paths:
        confined(root, path.relative_to(root), allow_leaf_symlink=True)
    logs = []
    for path in walk_nofollow(root / 'var/log'):
        if path.name.upper().startswith('README'):
            continue
        confined(root, path.relative_to(root), allow_leaf_symlink=True)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
            logs.append((path, info))
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError('Unexpected special log file: ' + str(path.relative_to(root)))
    for relative in ('etc/machine-id', 'var/lib/dbus/machine-id', 'etc/resolv.conf'):
        path = confined(root, relative, allow_leaf_symlink=True)
        if path.exists() and not (path.is_file() or path.is_symlink()):
            raise ValueError('Unexpected seal destination type: ' + relative)
    return root, remove_paths, logs, account_count


def seal_rootfs(root, parent=BASE, *, mountinfo=None):
    if mountinfo is None:
        mountinfo = Path('/proc/self/mountinfo').read_text()
    root, remove_paths, logs, account_count = preflight(root, parent, mountinfo)
    # Everything below was path/credential/mount-checked before the first write.
    removed = 0
    for path in dict.fromkeys(remove_paths):
        if path.exists() or path.is_symlink():
            removal(path)
            removed += 1
    for path, info in logs:
        if stat.S_ISLNK(info.st_mode):
            path.unlink()
        else:
            replace_empty(path, mode=stat.S_IMODE(info.st_mode))
            if os.geteuid() == 0:
                os.chown(path, info.st_uid, info.st_gid, follow_symlinks=False)
    replace_empty(root / 'etc/machine-id')
    for relative, target in (
        ('var/lib/dbus/machine-id', '../../../etc/machine-id'),
        ('etc/resolv.conf', '../run/systemd/resolve/stub-resolv.conf'),
    ):
        path = root / relative
        removal(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    # Keep the filesystem mountpoints required during early boot.
    for relative, mode in (('home', 0o755), ('root', 0o700), ('tmp', 0o1777), ('var/tmp', 0o1777)):
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
    return {'status': 'sealed', 'system_account_count': account_count,
            'removed_transient_entries': removed, 'cleared_log_entries': len(logs),
            'machine_id': 'empty', 'home': 'empty', 'root_password': 'locked'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    # Path validation precedes even the worker checks, and all writes follow the
    # independent full preflight in seal_rootfs(). No arbitrary root option.
    root = validate_rootfs(args.root)
    if root != args.root or not root.is_dir():
        parser.error('explicit existing clean rootfs without symlink components required')
    if os.geteuid() != 0:
        parser.error('root inside the isolated image worker required')
    if subprocess.run(['systemd-detect-virt', '--vm', '--quiet']).returncode != 0:
        parser.error('isolated VM worker required')
    if not Path('/etc/desos-build-worker').is_file():
        parser.error('missing isolated worker marker')
    if not re.search(r'^ID=[\"\']?ubuntu[\"\']?$', Path('/etc/os-release').read_text(), re.MULTILINE):
        parser.error('Ubuntu image worker required')
    print(json.dumps(seal_rootfs(root), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('DesOS rootfs seal refused: ' + str(error), file=sys.stderr)
        sys.exit(1)
