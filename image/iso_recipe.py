#!/usr/bin/env python3
"""DesOS ISO input, menu, Casper identity and artifact evidence helpers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

UPSTREAM = '6.18.62-desos'
BASE = Path('/srv/desos-build')


def validate_paths(root, output, base=BASE):
    if not root.is_absolute() or not output.is_absolute() or not base.is_absolute():
        raise ValueError('explicit absolute rootfs/output paths required')
    if root.is_symlink() or output.is_symlink():
        raise ValueError('staging paths must not be symlinks')
    base, root, output = base.resolve(), root.resolve(), output.resolve()
    if base == Path('/') or any(path == base or not path.is_relative_to(base) for path in (root, output)):
        raise ValueError('rootfs/output must be separate children of /srv/desos-build')
    if root == output or root.is_relative_to(output) or output.is_relative_to(root):
        raise ValueError('rootfs/output must not overlap')
    if not root.is_dir():
        raise ValueError('explicit clean rootfs must already exist')
    return root, output


def rooted(root, relative):
    """Resolve chroot paths including absolute symlinks without reading host files."""
    parts = list(Path(relative).parts)
    result = root
    links = 0
    while parts:
        part = parts.pop(0)
        if part in ('', '/', '.'):
            continue
        if part == '..':
            if result == root:
                raise ValueError('rootfs path escapes root')
            result = result.parent
            continue
        candidate = result / part
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('rootfs symlink loop')
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                result = root
            parts = list(target.parts) + parts
        else:
            result = candidate
    return result


def select_kernels(root):
    if (root / 'boot').is_symlink():
        raise ValueError('boot directory must not be a symlink')
    if not rooted(root, f'boot/vmlinuz-{UPSTREAM}').is_file():
        raise ValueError('required upstream kernel image absent: ' + UPSTREAM)
    candidates = [path.name.removeprefix('vmlinuz-') for path in (root / 'boot').glob('vmlinuz-7.0.*-generic')]
    if not candidates:
        raise ValueError('Ubuntu 7.0 generic compatibility kernel absent')
    def version_key(version):
        return tuple(int(token) for token in re.findall(r'\d+', version))
    compat = max(candidates, key=version_key)
    selected = {'upstream': UPSTREAM, 'compat': compat}
    for version in selected.values():
        for name in ('vmlinuz-' + version, 'initrd.img-' + version):
            if (root / 'boot' / name).is_symlink():
                raise ValueError('boot payload must not be a symlink: ' + name)
        if not rooted(root, 'lib/modules/' + version).is_dir():
            raise ValueError('matching kernel modules absent: ' + version)
        if not rooted(root, 'boot/vmlinuz-' + version).is_file():
            raise ValueError('matching kernel image absent: ' + version)
    return selected


def grub_menu():
    common = 'boot=casper live-media-path=casper username=desos hostname=desos-live nopersistent console=tty0 console=ttyS0,115200n8'
    entries = [
        ('Try DesOS 0.1 Preview', 'upstream', ' quiet splash'),
        ('Install DesOS 0.1 Preview', 'upstream', ' quiet splash desos.install=1'),
        ('Compatibility: Ubuntu kernel (recovery choice)', 'compat', ' quiet splash'),
        ('Debug DesOS upstream (verbose boot)', 'upstream', ' debug loglevel=7 systemd.show_status=1'),
    ]
    lines = ['set default=0', 'set timeout=8', 'insmod all_video',
             'serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1',
             'terminal_input console serial', 'terminal_output console serial',
             'search --no-floppy --file --set=root /.disk/desos-live-id', '']
    for title, kernel, extra in entries:
        lines.extend([f'menuentry "{title}" {{',
                      f'    linux /casper/vmlinuz-{kernel} {common}{extra}',
                      f'    initrd /casper/initrd-{kernel}', '}', ''])
    return '\n'.join(lines)


def copy_uuid(extraction, disk, flavor):
    if flavor not in {'upstream', 'compat'}:
        raise ValueError('unknown kernel flavor')
    matches = sorted(extraction.glob('**/conf/uuid.conf'))
    values = {path.read_text().strip() for path in matches}
    if len(values) != 1:
        raise ValueError('exactly one consistent Casper initrd UUID required')
    value = values.pop()
    try:
        normalized = str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError('invalid Casper UUID') from error
    if value != normalized:
        raise ValueError('Casper UUID must use canonical spelling')
    disk.mkdir(parents=True, exist_ok=True)
    (disk / ('casper-uuid-' + flavor)).write_text(value + '\n')
    return value


def squash_command(root, destination):
    return ['mksquashfs', str(root), str(destination), '-noappend', '-comp', 'zstd',
            '-Xcompression-level', '8', '-b', '1M', '-processors', '2', '-mem', '512M',
            '-one-file-system', '-wildcards', '-e',
            'proc/*', 'sys/*', 'dev/*', 'run/*', 'tmp/*', 'var/tmp/*',
            'var/cache/apt/archives/*.deb', '.desos-bootstrap-complete']


def rescue_command(stage, iso):
    return ['grub-mkrescue', '-o', str(iso), str(stage),
            '--mbr-force-bootable', '--', '-volid', 'DESOS_01']


def check_boot_report(report):
    for platform in ('BIOS', 'UEFI'):
        if not re.search(r'El Torito boot img\s*:.*\b' + platform + r'\b', report):
            raise ValueError('ISO is missing El Torito platform: ' + platform)
    if not re.search(r'System area summary\s*:.*\bMBR\b.*\bGPT\b', report):
        raise ValueError('ISO is missing expected hybrid MBR/GPT system area')
    if not re.search(r'MBR partition\s*:\s*\d+\s+0x80\s+0x', report):
        raise ValueError('ISO is missing a bootable hybrid MBR partition')


def mounts_within(root, mountinfo):
    mounted = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6:
            raise ValueError('malformed mountinfo')
        target = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
        path = Path(target)
        if path == root or path.is_relative_to(root):
            mounted.append(target)
    return mounted


def preflight(root):
    identity = rooted(root, 'etc/os-release').read_text()
    if not re.search(r'^ID=[\"\']?desos[\"\']?$', identity, re.M):
        raise ValueError('rootfs must identify as ID=desos')
    mounted = mounts_within(root, Path('/proc/self/mountinfo').read_text())
    if mounted:
        raise ValueError('rootfs has mounted filesystems: ' + ', '.join(mounted))
    for relative in ('etc/machine-id', 'var/lib/dbus/machine-id'):
        path = rooted(root, relative)
        if path.exists() and path.read_text().strip() not in ('', 'uninitialized'):
            raise ValueError('seal rootfs machine identity first: ' + relative)
    for relative in ('var/lib/systemd/random-seed', 'var/lib/urandom/random-seed'):
        if rooted(root, relative).exists():
            raise ValueError('seal rootfs random seed first: ' + relative)
    for key in rooted(root, 'etc/ssh').glob('ssh_host_*_key'):
        raise ValueError('seal rootfs SSH host keys first: ' + key.name)
    for relative in ('usr/share/initramfs-tools/hooks/casper', 'usr/share/initramfs-tools/scripts/casper', 'usr/bin/desos-installer'):
        if not rooted(root, relative).is_file():
            raise ValueError('required live/installer integration absent: ' + relative)
    return select_kernels(root)


def prepare(root, stage):
    selected = preflight(root)
    for directory in ('casper', 'boot/grub', '.disk'):
        (stage / directory).mkdir(parents=True, exist_ok=True)
    (stage / 'boot/grub/grub.cfg').write_text(grub_menu())
    (stage / '.disk/desos-live-id').write_text('DesOS 0.1 Preview\n')
    (stage / '.disk/info').write_text('DesOS 0.1 Preview amd64\n')
    (stage / '.disk/base_installable').touch()
    (stage / '.disk/cd_type').write_text('full_cd/single\n')
    return selected


def digest(path, algorithm='sha256'):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def stage_manifest(root, stage):
    # This is installed package state, not a copied worker package inventory.
    result = subprocess.run(['chroot', str(root), 'dpkg-query', '-W', '-f=${Package} ${Version}\n'], capture_output=True, text=True, check=True)
    manifest = ''.join(sorted(result.stdout.splitlines(keepends=True)))
    (stage / 'casper/filesystem.manifest').write_text(manifest)
    # Calamares uses the squashfs; keep an explicit installed-package manifest too.
    (stage / 'casper/filesystem.manifest-desktop').write_text(manifest)
    size = subprocess.run(['du', '-sx', '--block-size=1', str(root)], capture_output=True, text=True, check=True).stdout.split()[0]
    (stage / 'casper/filesystem.size').write_text(size + '\n')
    lines = []
    for path in sorted(stage.rglob('*')):
        if path.is_file() and path.name != 'md5sum.txt':
            relative = path.relative_to(stage).as_posix()
            lines.append(digest(path, 'md5') + '  ./' + relative + '\n')
    (stage / 'md5sum.txt').write_text(''.join(lines))


def evidence(root, stage, iso, output, selected):
    iso_hash = digest(iso)
    (output / (iso.name + '.sha256')).write_text(iso_hash + '  ' + iso.name + '\n')
    report = {'distribution': 'DesOS', 'version': '0.1 Preview', 'architecture': 'amd64',
              'iso': iso.name, 'iso_sha256': iso_hash, 'iso_size_bytes': iso.stat().st_size,
              'kernels': selected, 'boot_modes_constructed': ['BIOS', 'UEFI'],
              'boot_validation': 'pending actual BIOS and UEFI VM boot and installation tests',
              'casper_uuids': {flavor: (stage / '.disk' / ('casper-uuid-' + flavor)).read_text().strip() for flavor in selected},
              'stage_artifacts': {p.relative_to(stage).as_posix(): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()},
              'recipe_sha256': digest(Path(__file__)),
              'build_script_sha256': digest(Path(__file__).with_name('build-iso.sh')),
              'tools': {tool: subprocess.run([tool, '--version'], capture_output=True, text=True).stdout.splitlines()[:1] for tool in ['mksquashfs', 'xorriso', 'grub-mkrescue']}}
    (output / 'iso-provenance.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    shutil.copyfile(stage / 'casper/filesystem.manifest', output / 'filesystem.manifest')
    shutil.copyfile(stage / 'casper/filesystem.size', output / 'filesystem.size')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'preflight', 'uuid', 'squash', 'manifest', 'evidence', 'rescue', 'check-boot-report'])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--extraction', type=Path)
    parser.add_argument('--flavor', choices=['upstream', 'compat'])
    parser.add_argument('--iso', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.action == 'check-boot-report':
        if not args.report:
            parser.error('--report required')
        check_boot_report(args.report.read_text())
        return
    if args.action == 'uuid':
        if not all((args.extraction, args.stage, args.flavor)):
            parser.error('uuid requires --extraction --stage --flavor')
        print(copy_uuid(args.extraction, args.stage / '.disk', args.flavor))
        return
    if not args.root or not args.output:
        parser.error('explicit --root and --output required')
    root, output = validate_paths(args.root, args.output)
    if args.action == 'preflight':
        print(json.dumps(preflight(root), sort_keys=True))
        return
    if not args.stage or args.stage.resolve() != output / 'iso-staging' or args.stage.is_symlink():
        parser.error('--stage must be OUTPUT/iso-staging without symlinks')
    stage = args.stage.resolve()
    if args.action == 'prepare':
        print(json.dumps(prepare(root, stage), sort_keys=True))
    elif args.action == 'squash':
        preflight(root)
        subprocess.run(squash_command(root, stage / 'casper/filesystem.squashfs'), check=True)
    elif args.action == 'manifest':
        stage_manifest(root, stage)
    elif args.action == 'rescue':
        if not args.iso or args.iso.parent.resolve() != output:
            parser.error('--iso must be directly within OUTPUT')
        subprocess.run(rescue_command(stage, args.iso), check=True)
    elif args.action == 'evidence':
        if not args.iso or args.iso.parent.resolve() != output:
            parser.error('--iso must be directly within OUTPUT')
        evidence(root, stage, args.iso, output, select_kernels(root))


if __name__ == '__main__':
    main()
