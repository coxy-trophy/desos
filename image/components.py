#!/usr/bin/env python3
"""Install the reviewed DesOS component trees into a clean image rootfs.

A component is a top-level recipe directory holding ``component.json`` and an
optional ``rootfs/`` tree. Only the fixed names in COMPONENTS are considered;
absent ones are skipped with a message, never guessed. Every manifest and
every destination is checked before the first write, so a refusal leaves the
image untouched.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

try:
    from .paths import validate_rootfs
except ImportError:
    from paths import validate_rootfs

BASE = Path('/srv/desos-build')
# Installation order. `system` is first so later components may rely on it.
COMPONENTS = ('system', 'undo', 'verify', 'boot-theme', 'phonekey')
MANIFEST_KEYS = {'name', 'packages', 'enable_units', 'notes'}
PACKAGE = re.compile(r'[a-z0-9][a-z0-9+.-]{1,127}')
UNIT = re.compile(r'[A-Za-z0-9:_.\\-]+(@[A-Za-z0-9:_.\\-]*)?\.(service|socket|timer|path|target|mount|automount|swap)')
# Image state that no component may supply: account databases, privilege
# rules, user homes, runtime/pseudo filesystems and the build stage markers.
FORBIDDEN_EXACT = {
    'etc/passwd', 'etc/shadow', 'etc/group', 'etc/gshadow', 'etc/subuid',
    'etc/subgid', 'etc/sudoers', 'etc/machine-id', 'etc/hostname',
    'etc/os-release', 'usr/lib/os-release', 'etc/lsb-release',
}
FORBIDDEN_PREFIX = (
    'etc/sudoers.d', 'etc/ssh', 'home', 'root', 'dev', 'proc', 'sys', 'run',
    'tmp', 'var/tmp', 'mnt', 'media', 'srv',
)


class Component:
    def __init__(self, name, directory, packages, units, notes):
        self.name, self.directory = name, directory
        self.packages, self.units, self.notes = packages, units, notes
        self.tree = directory / 'rootfs'


def load_manifest(directory, name):
    """Strictly validate one component.json; unknown or missing keys refuse."""
    path = directory / 'component.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Component {name}: component.json must be a regular file')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Component {name}: invalid component.json: {error}') from None
    if not isinstance(data, dict) or set(data) != MANIFEST_KEYS:
        raise ValueError(f'Component {name}: component.json needs exactly the keys {sorted(MANIFEST_KEYS)}')
    if data['name'] != name:
        raise ValueError(f'Component {name}: manifest name does not match its directory')
    if not isinstance(data['notes'], str):
        raise ValueError(f'Component {name}: notes must be a string')
    for key, pattern in (('packages', PACKAGE), ('enable_units', UNIT)):
        values = data[key]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f'Component {name}: {key} must be a list of strings')
        if len(set(values)) != len(values):
            raise ValueError(f'Component {name}: duplicate entry in {key}')
        for value in values:
            if not pattern.fullmatch(value):
                raise ValueError(f'Component {name}: invalid {key} entry {value!r}')
    return Component(name, directory, list(data['packages']), list(data['enable_units']), data['notes'])


def discover(recipes, log=None):
    """Return the present, valid components in installation order."""
    recipes = Path(recipes)
    found = []
    for name in COMPONENTS:
        directory = recipes / name
        if directory.is_symlink():
            raise ValueError(f'Component {name}: directory must not be a symlink')
        if not directory.exists():
            if log:
                log(f'Component {name}: not present in this recipe tree; skipped')
            continue
        if not directory.is_dir():
            raise ValueError(f'Component {name}: expected a directory')
        component = load_manifest(directory, name)
        if component.tree.is_symlink() or (component.tree.exists() and not component.tree.is_dir()):
            raise ValueError(f'Component {name}: rootfs must be a real directory')
        found.append(component)
    return found


def union(components, attribute):
    """Ordered union across components, without duplicates."""
    return list(dict.fromkeys(value for component in components for value in getattr(component, attribute)))


def source_entries(component):
    """Every directory and file of a component tree; anything else refuses."""
    directories, files = [], []
    if not component.tree.is_dir():
        return directories, files
    for current, names, filenames in os.walk(component.tree, followlinks=False):
        current = Path(current)
        for name in sorted(names + filenames):
            path = current / name
            relative = path.relative_to(component.tree)
            mode = path.lstat().st_mode
            if name == '__pycache__' or name.endswith('.pyc'):
                # Interpreter residue from running the tests is never shipped.
                continue
            if stat.S_ISLNK(mode):
                raise ValueError(f'Component {component.name}: symlinks are refused: {relative}')
            if stat.S_ISDIR(mode):
                directories.append(relative)
            elif stat.S_ISREG(mode):
                if path.stat().st_nlink != 1:
                    raise ValueError(f'Component {component.name}: hard-linked source refused: {relative}')
                files.append((relative, 0o755 if mode & 0o111 else 0o644))
            else:
                raise ValueError(f'Component {component.name}: special file refused: {relative}')
        names[:] = sorted(name for name in names if name != '__pycache__')
    return directories, files


def destination(root, relative, name):
    """Resolve one image path, refusing escapes, symlinks and reserved state."""
    relative = Path(relative)
    text = relative.as_posix()
    if relative.is_absolute() or '..' in relative.parts or text in ('', '.'):
        raise ValueError(f'Component {name}: invalid image path {text!r}')
    if (text in FORBIDDEN_EXACT or relative.name.startswith('.desos-')
            or any(text == prefix or text.startswith(prefix + '/') for prefix in FORBIDDEN_PREFIX)):
        raise ValueError(f'Component {name}: reserved image path refused: {text}')
    target = root / relative
    for part in [target, *target.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError(f'Component {name}: image destination contains a symlink: {text}')
    if not target.resolve().is_relative_to(root):
        raise ValueError(f'Component {name}: image destination escapes the rootfs: {text}')
    return target


def plan(root, components):
    """Preflight every destination of every component. Performs no writes."""
    directories, copies, owners = {}, [], {}
    for component in components:
        tree_directories, files = source_entries(component)
        for relative in tree_directories:
            target = destination(root, relative, component.name)
            if target.exists() and not target.is_dir():
                raise ValueError(f'Component {component.name}: image path is not a directory: {relative}')
            directories.setdefault(target, component.name)
        for relative, mode in files:
            target = destination(root, relative, component.name)
            if target in owners:
                raise ValueError(f'Components {owners[target]} and {component.name} both ship {relative}')
            if target.exists() and not target.is_file():
                raise ValueError(f'Component {component.name}: image path is not a regular file: {relative}')
            owners[target] = component.name
            copies.append((component.tree / relative, target, mode))
    for target, name in directories.items():
        if target in owners:
            raise ValueError(f'Components {owners[target]} and {name} disagree about {target.relative_to(root)}')
    return list(directories), copies


def install(root, components, parent=BASE):
    """Copy all component trees; root:root when run as root in the worker."""
    original = Path(root)
    root = validate_rootfs(original, Path(parent))
    if original != root or not root.is_dir():
        raise ValueError('Existing rootfs without symlink components required')
    directories, copies = plan(root, components)
    # Everything below was path-checked before the first write.
    privileged = os.geteuid() == 0
    for target in sorted(directories):
        if not target.exists():
            # Only directories created here are owned/moded; existing image
            # directories (for example a 0700 or sticky one) keep their policy.
            target.mkdir()
            target.chmod(0o755)
            if privileged:
                os.chown(target, 0, 0)
    for source, target, mode in copies:
        staging = target.with_name(target.name + '.desos-new')
        if staging.is_symlink() or staging.exists():
            staging.unlink()
        # A fresh inode replaces the destination; never write through a link.
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(source.read_bytes())
        if privileged:
            os.chown(staging, 0, 0)
        staging.chmod(mode)
        os.replace(staging, target)
    return {'components': [component.name for component in components],
            'files': len(copies), 'packages': union(components, 'packages'),
            'enable_units': union(components, 'units')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('packages', 'units', 'install'))
    parser.add_argument('--recipes', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    components = discover(args.recipes, log=lambda message: print(message, file=sys.stderr))
    if args.command in ('packages', 'units'):
        # One name per line; no output at all when there is nothing to add.
        for value in union(components, args.command):
            print(value)
    else:
        if args.root is None:
            parser.error('install requires --root')
        if os.geteuid() != 0:
            parser.error('root inside the isolated image worker required')
        print(json.dumps(install(args.root, components), sort_keys=True), file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('DesOS components refused: ' + str(error), file=sys.stderr)
        sys.exit(1)
