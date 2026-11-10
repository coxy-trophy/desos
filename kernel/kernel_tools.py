#!/usr/bin/env python3
"""Pinned upstream Linux verification and public build evidence, stdlib only."""
import argparse
import hashlib
import json
import lzma
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

HERE = Path(__file__).resolve().parent


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def check_release(document, pin):
    matches = [r for r in document['releases'] if r['version'] == pin['version']]
    if len(matches) != 1:
        raise ValueError('pinned release missing or ambiguous')
    release = matches[0]
    if release.get('iseol') is not False or release.get('moniker') != 'longterm':
        raise ValueError('pinned release is not a maintained longterm release')
    if release.get('source') != pin['source_url'] or release.get('pgp') != pin['signature_url']:
        raise ValueError('release source/signature URLs changed')
    return release


def check_signature_status(status, allowed):
    valid = []
    bad = {'BADSIG', 'ERRSIG', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG', 'KEYREVOKED'}
    for line in status.splitlines():
        if not line.startswith('[GNUPG:] '):
            continue
        fields = line.split()[1:]
        if fields[0] in bad:
            raise ValueError('unacceptable GnuPG signature status: ' + fields[0])
        if fields[0] == 'VALIDSIG':
            primary = fields[10] if len(fields) > 10 else fields[1]
            if primary not in allowed:
                raise ValueError('signature primary fingerprint not pinned: ' + primary)
            valid.append(primary)
    if len(valid) != 1:
        raise ValueError('exactly one valid pinned detached signature required')
    return valid[0]


def required_config():
    # Merge into a full Ubuntu generic config, never localmodconfig.
    builtins = '''64BIT X86_64 SMP PCI PCI_MSI ACPI EFI EFI_STUB EFI_PARTITION
    MODULES BLK_DEV_INITRD DEVTMPFS DEVTMPFS_MOUNT TMPFS TMPFS_POSIX_ACL
    PROC_FS SYSFS UNIX INET IPV6 PACKET UNIX98_PTYS FHANDLE
    CGROUPS NAMESPACES USER_NS NET_NS BPF_SYSCALL SECCOMP SECCOMP_FILTER
    SECURITY SECURITY_APPARMOR AUDIT KEYS FW_LOADER
    INPUT INPUT_EVDEV VT VT_CONSOLE DRM DRM_KMS_HELPER DRM_FBDEV_EMULATION
    USB_SUPPORT USB HID HID_GENERIC USB_HID
    NET NETDEVICES WLAN CFG80211 MAC80211 RFKILL
    VIRTIO VIRTIO_PCI VIRTIO_BLK VIRTIO_NET VIRTIO_CONSOLE
    EXT4_FS SQUASHFS SQUASHFS_XZ SQUASHFS_ZSTD OVERLAY_FS ISO9660_FS
    FAT_FS VFAT_FS NLS NLS_CODEPAGE_437 NLS_ISO8859_1
    BLK_DEV_LOOP BLK_DEV_SD SCSI ATA SATA_AHCI BLK_DEV_NVME
    USB_XHCI_HCD USB_XHCI_PCI USB_EHCI_HCD USB_EHCI_PCI USB_OHCI_HCD
    USB_UHCI_HCD USB_STORAGE USB_UAS DM_CRYPT BLK_DEV_DM
    NETFILTER BRIDGE VETH TUN NET_NS CGROUP_PIDS MEMCG CGROUP_SCHED'''.split()
    modules = '''SND SND_PCM SND_HDA_CORE DRM_I915 DRM_NOUVEAU IWLWIFI IWLMVM R8169 SND_HDA_INTEL
    SND_HDA_CODEC_REALTEK SND_HDA_CODEC_HDMI'''.split()
    values = {key: 'y' for key in builtins}
    values.update({key: 'm' for key in modules})
    values.update({'LOCALVERSION': '"-desos"', 'LOCALVERSION_AUTO': 'n',
                   'DEBUG_INFO_NONE': 'y', 'DEBUG_INFO': 'n', 'DEBUG_INFO_BTF': 'n',
                   'DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT': 'n', 'DEBUG_INFO_DWARF4': 'n', 'DEBUG_INFO_DWARF5': 'n',
                   'RUST': 'n', 'SYSTEM_TRUSTED_KEYS': '""',
                   'SYSTEM_REVOCATION_KEYS': '""', 'MODULE_SIG': 'y',
                   'MODULE_SIG_ALL': 'y', 'MODULE_SIG_FORCE': 'n',
                   'MODULE_SIG_KEY': '"certs/signing_key.pem"',
                   'MODULE_SIG_SHA256': 'y', 'MODULE_SIG_SHA384': 'n', 'MODULE_SIG_SHA512': 'n',
                   'MODULE_SIG_SHA3_256': 'n', 'MODULE_SIG_SHA3_384': 'n', 'MODULE_SIG_SHA3_512': 'n',
                   'WERROR': 'n', 'IKHEADERS': 'n'})
    return values


def parse_config(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            key, value = line[7:].split('=', 1)
            values[key] = value
        elif line.startswith('# CONFIG_') and line.endswith(' is not set'):
            values[line[9:-11]] = 'n'
    return values


def check_config(path):
    actual = parse_config(path)
    errors = []
    for key, expected in required_config().items():
        found = actual.get(key, 'n')
        # tristates requested built-in can be modules in an inherited config, but
        # the requested fragment deliberately promotes them to built-in.
        if found != expected:
            errors.append(f'{key}: expected {expected}, found {found}')
    if errors:
        raise ValueError('kernel configuration failed:\n' + '\n'.join(errors))
    return actual


def apply_config(source, output, baseline):
    output.mkdir(parents=True, exist_ok=True)
    if (output / '.config').exists():
        # Idempotent setup supports resuming an interrupted package build.
        check_config(output / '.config')
        return
    shutil.copyfile(baseline, output / '.config')
    for key, value in required_config().items():
        flag = {'y': '--enable', 'm': '--module', 'n': '--disable'}.get(value)
        if flag:
            args = [flag, key]
        else:
            args = ['--set-str', key, value.strip('"')]
        subprocess.run([str(source / 'scripts/config'), '--file', str(output / '.config'), *args], check=True)
    subprocess.run(['make', '-C', str(source), 'O=' + str(output), 'ARCH=x86_64', 'olddefconfig'], check=True)
    check_config(output / '.config')


def source_filter(member, destination):
    """Validate data paths, retaining signed symlink spelling across Python versions."""
    safe = tarfile.data_filter(member, destination)
    if safe is None:
        return None
    if member.issym():
        root = os.path.realpath(destination)
        target = os.path.realpath(os.path.join(root, os.path.dirname(member.name), member.linkname))
        if os.path.commonpath([root, target]) != root:
            raise tarfile.LinkOutsideDestinationError(member, target)
        return safe.replace(linkname=member.linkname)
    return safe


def check_source_tree(archive, source):
    """Check every source file and reject additions, without trusting a receipt."""
    seen = set()
    with tarfile.open(archive, 'r:') as tar:
        for member in tar:
            parts = Path(member.name).parts
            if not parts or parts[0] != source.name or '..' in parts:
                raise ValueError('unexpected source archive path')
            relative = Path(*parts[1:])
            if str(relative) == '.':
                continue
            seen.add(relative)
            target = source / relative
            if member.isfile():
                if not target.is_file() or target.is_symlink():
                    raise ValueError('missing/modified source file: ' + str(relative))
                original = tar.extractfile(member)
                if hashlib.file_digest(original, 'sha256').hexdigest() != digest(target):
                    raise ValueError('modified source file: ' + str(relative))
            elif member.issym():
                if not target.is_symlink() or os.readlink(target) != member.linkname:
                    raise ValueError('modified source symlink: ' + str(relative))
            elif member.isdir():
                if not target.is_dir() or target.is_symlink():
                    raise ValueError('modified source directory: ' + str(relative))
            else:
                raise ValueError('unsupported source archive member: ' + member.name)
    extras = {p.relative_to(source) for p in source.rglob('*')} - seen
    if extras:
        raise ValueError('unexpected source files: ' + ', '.join(map(str, sorted(extras)[:10])))


def check_package_tar(stream):
    """Inspect actual package payloads without extracting privileged paths."""
    private_marker = re.compile(rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')
    with tarfile.open(fileobj=stream, mode='r|*') as archive:
        for member in archive:
            name = Path(member.name).name
            if name in {'signing_key.pem', 'id_rsa', 'id_ed25519', 'id_ecdsa'}:
                raise ValueError('private signing/SSH key pathname in package: ' + member.name)
            if member.isfile():
                payload = archive.extractfile(member)
                tail = b''
                while chunk := payload.read(1024 * 1024):
                    content = tail + chunk
                    if private_marker.search(content):
                        raise ValueError('private key PEM material in package: ' + member.name)
                    tail = content[-128:]


def check_packages(packages):
    for package in sorted(packages.glob('*.deb')):
        process = subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(package)], stdout=subprocess.PIPE)
        try:
            check_package_tar(process.stdout)
        except BaseException:
            process.terminate()
            process.wait()
            raise
        finally:
            process.stdout.close()
        if process.wait() != 0:
            raise ValueError('unable to inspect package payload: ' + str(package))


def download(url, path):
    if not url.startswith('https://'):
        raise ValueError('HTTPS required')
    if path.exists():
        return
    partial = path.with_suffix(path.suffix + '.part')
    with urllib.request.urlopen(url, timeout=90) as response, partial.open('wb') as stream:
        if not response.url.startswith('https://'):
            raise ValueError('HTTPS redirect required')
        shutil.copyfileobj(response, stream)
    partial.replace(path)


def verify(work, pin):
    metadata = HERE / 'releases-2026-11-10.json'
    if digest(metadata) != pin['release_metadata_snapshot_sha256']:
        raise ValueError('release metadata snapshot hash mismatch')
    check_release(json.loads(metadata.read_text()), pin)
    archive = work / f"linux-{pin['version']}.tar.xz"
    signature = work / f"linux-{pin['version']}.tar.sign"
    if digest(archive) != pin['archive_sha256'] or digest(signature) != pin['signature_sha256']:
        raise ValueError('pinned archive/signature SHA256 mismatch')
    home = work / 'gnupg'
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    gpg = ['gpg', '--homedir', str(home), '--batch', '--no-auto-key-retrieve']
    for maintainer in pin['maintainers']:
        fingerprint = maintainer['fingerprint']
        key = HERE / maintainer['public_key_file']
        if digest(key) != maintainer['public_key_sha256']:
            raise ValueError('pinned public key hash mismatch')
        inspected = subprocess.run([*gpg, '--with-colons', '--import-options', 'show-only', '--import', str(key)], capture_output=True, text=True, check=True)
        primary = None
        want_fingerprint = False
        for line in inspected.stdout.splitlines():
            fields = line.split(':')
            if fields[0] == 'pub':
                want_fingerprint = True
            elif fields[0] == 'fpr' and want_fingerprint:
                primary = fields[9]
                break
        if primary != fingerprint:
            raise ValueError('downloaded public key fingerprint mismatch')
        subprocess.run([*gpg, '--import', str(key)], check=True, capture_output=True)
    tarpath = work / f"linux-{pin['version']}.tar"
    # Reuse only a tar matching the pinned digest; never trust stale extraction.
    if not tarpath.exists() or digest(tarpath) != pin['tar_sha256']:
        with lzma.open(archive, 'rb') as compressed, tarpath.open('wb') as stream:
            shutil.copyfileobj(compressed, stream)
    result = subprocess.run([*gpg, '--status-fd', '1', '--verify', str(signature), str(tarpath)], capture_output=True, text=True)
    (work / 'signature-status.txt').write_text(result.stdout)
    (work / 'signature-log.txt').write_text(result.stderr)
    result.check_returncode()
    signer = check_signature_status(result.stdout, [m['fingerprint'] for m in pin['maintainers']])
    if digest(tarpath) != pin['tar_sha256']:
        raise ValueError('uncompressed source SHA256 mismatch')
    evidence = {'version': pin['version'], 'source_archive_sha256': digest(archive),
                'source_tar_sha256': digest(tarpath), 'signature_sha256': digest(signature),
                'signer_primary_fingerprint': signer, 'verification': 'gpg detached signature VALID, fingerprint pinned from kernel.org/signature.html',
                'release_metadata_url': pin['release_metadata_url'], 'release_metadata_snapshot_sha256': pin['release_metadata_snapshot_sha256'],
                'source_modified': False}
    write_json(work / 'source-provenance.json', evidence)
    return tarpath


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['fetch', 'verify', 'extract', 'configure', 'check-config', 'verify-tree', 'provenance', 'check-packages'])
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--build', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--packages', type=Path)
    args = parser.parse_args()
    pin = json.loads((HERE / 'source-pin.json').read_text())
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = (args.source or work / ('linux-' + pin['version'])).resolve()
    build = (args.build or work / 'build').resolve()
    if args.action == 'fetch':
        for url, name in ((pin['source_url'], f"linux-{pin['version']}.tar.xz"), (pin['signature_url'], f"linux-{pin['version']}.tar.sign")):
            download(url, work / name)
        verify(work, pin)
    elif args.action == 'verify':
        verify(work, pin)
    elif args.action == 'extract':
        archive = verify(work, pin)
        if not source.exists():
            with tarfile.open(archive, 'r:') as tar:
                tar.extractall(source.parent, filter=source_filter)
        check_source_tree(archive, source)
    elif args.action == 'verify-tree':
        archive = verify(work, pin)
        check_source_tree(archive, source)
    elif args.action == 'configure':
        if not args.baseline or not args.baseline.is_file():
            parser.error('--baseline must be an explicit complete Ubuntu generic config')
        apply_config(source, build, args.baseline.resolve())
    elif args.action == 'check-config':
        check_config(build / '.config')
    elif args.action == 'check-packages':
        if not args.packages:
            parser.error('--packages required')
        check_packages(args.packages)
    elif args.action == 'provenance':
        if not args.baseline or not args.packages:
            parser.error('provenance requires --baseline and --packages')
        check_config(build / '.config')
        check_packages(args.packages)
        evidence = json.loads((work / 'source-provenance.json').read_text())
        evidence.update({'package_version': pin['package_version'], 'kernel_release': (build / 'include/config/kernel.release').read_text().strip(),
                         'config_sha256': digest(build / '.config'), 'baseline_config_sha256': digest(args.baseline),
                         'source_date_epoch': pin['source_date_epoch'], 'build_user': 'desos', 'build_host': 'desos-builder',
                         'build_jobs': 2, 'config_recipe_sha256': digest(HERE / 'kernel_tools.py'),
                         'build_script_sha256': digest(HERE / 'build-guest.sh'),
                         'packages': {p.name: digest(p) for p in sorted(args.packages.glob('*.deb'))},
                         'toolchain': {tool: subprocess.run([tool, '--version'], capture_output=True, text=True, check=True).stdout.splitlines()[0] for tool in ['gcc', 'ld', 'make', 'dpkg-deb']}})
        write_json(args.packages / 'kernel-provenance.json', evidence)
        shutil.copyfile(build / '.config', args.packages / 'config-6.18.62-desos')
        shutil.copyfile(args.baseline, args.packages / 'baseline.config')
        shutil.copyfile(HERE / 'source-pin.json', args.packages / 'source-pin.json')
        shutil.copyfile(work / 'signature-status.txt', args.packages / 'signature-status.txt')
        shutil.copyfile(work / 'signature-log.txt', args.packages / 'signature-log.txt')


if __name__ == '__main__':
    main()
