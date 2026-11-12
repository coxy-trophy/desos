#!/usr/bin/env python3
"""DesOS preview integration for a clean filesystem, never a cloned home."""
import argparse
import json
from pathlib import Path

try:
    from .paths import validate_rootfs
except ImportError:
    from paths import validate_rootfs

# The release identity (/usr/lib/os-release, /etc/lsb-release) is owned solely
# by the desos-release package built from image/release_package.py.

# The single definition of the DesOS network update repository. Nothing is
# published yet: the reserved .invalid name can never resolve, the source is
# shipped disabled, and desos-update-channel refuses to enable a placeholder.
UPDATE_CHANNEL_URL = 'https://updates.desos.invalid/apt'
ARCHIVE_KEYRING = '/usr/share/keyrings/desos-archive-keyring.asc'
UPDATE_SOURCE = f'''# DesOS network update channel. Managed by: desos-update-channel enable|disable|status
Enabled: no
Types: deb
URIs: {UPDATE_CHANNEL_URL}
Suites: desos
Components: main
Signed-By: {ARCHIVE_KEYRING}
'''
# Package names DesOS itself supplies (desktop, release identity, upstream kernel).
DESOS_PACKAGE_PATTERNS = 'des-ui des-ui-* desos-* linux-image-*-desos linux-headers-*-desos'
APT_PREFERENCES = f'''Explanation: DesOS supplies these packages; its signed origin outranks every
Explanation: other archive (990 beats the default 500 without forcing downgrades).
Package: {DESOS_PACKAGE_PATTERNS}
Pin: release o=DesOS
Pin-Priority: 990

Explanation: Never take a same-named package from the Ubuntu base archive.
Package: {DESOS_PACKAGE_PATTERNS}
Pin: release o=Ubuntu
Pin-Priority: -1
'''

LIVE_HOOK = '''#!/bin/sh
PREREQ="25adduser"
case "$1" in prereqs) echo "$PREREQ"; exit 0;; esac
set -e
# This is the documented PUBLIC live-media password, not an installed credential.
printf 'desos:desos\\n' | chroot /root chpasswd
for group in adm sudo audio video render netdev plugdev bluetooth; do
    if chroot /root getent group "$group" >/dev/null; then
        chroot /root usermod -a -G "$group" desos
    fi
done
mkdir -p /root/etc/sudoers.d
chmod 755 /root/etc/sudoers.d
printf 'desos ALL=(ALL) NOPASSWD: ALL\\n' > /root/etc/sudoers.d/90-desos-live
chmod 440 /root/etc/sudoers.d/90-desos-live
'''

LIVE_SESSION = '''#!/bin/bash
set -e
if grep -qw 'desos.install=1' /proc/cmdline; then
    (
        for attempt in $(seq 1 120); do
            if /usr/bin/des-ui-ctl ping >/dev/null 2>&1; then break; fi
            sleep 1
        done
        # Take only the display coordinates published by our compositor.
        while IFS='=' read -r key value; do
            case "$key" in WAYLAND_DISPLAY|DISPLAY|XAUTHORITY) export "$key=$value";; esac
        done < <(systemctl --user show-environment)
        exec /usr/bin/desos-installer
    ) &
fi
exec /usr/bin/des-ui-session "$@"
'''

SELECT_KERNEL = '''#!/usr/bin/python3
import pathlib,subprocess,re
boot=pathlib.Path('/boot');cfg=boot/'grub/grub.cfg'
if not cfg.exists():raise SystemExit(0)
versions=[p.name.removeprefix('vmlinuz-') for p in boot.glob('vmlinuz-*-desos')]
if not versions:raise SystemExit('No DesOS upstream kernel installed')
version=subprocess.run(['sort','-V'],input='\\n'.join(versions)+'\\n',capture_output=True,text=True,check=True).stdout.splitlines()[-1]
uuid=subprocess.run(['grub-probe','--target=fs_uuid','/'],capture_output=True,text=True,check=True).stdout.strip()
submenu='gnulinux-advanced-'+uuid
entry='gnulinux-'+version+'-advanced-'+uuid
text=cfg.read_text()
if entry not in text or submenu not in text:raise SystemExit('DesOS kernel menu ID missing from generated GRUB config')
subprocess.run(['grub-set-default',submenu+'>'+entry],check=True)
print('Selected DesOS upstream kernel '+version)
'''


def write_configuration(root: Path, parent: Path = Path('/srv/desos-build')) -> None:
    root = validate_rootfs(root, parent)
    if not (root / 'etc/debian_version').is_file():
        raise ValueError('Clean Debian-compatible rootfs required')

    def put(name, content, mode=0o644):
        target = root / name
        if target.is_symlink():
            raise ValueError('Refusing configuration symlink: '+name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(mode)

    put('etc/apt/sources.list.d/desos-updates.sources', UPDATE_SOURCE)
    put('etc/apt/preferences.d/desos', APT_PREFERENCES)
    put('usr/share/doc/desos/README', '''DesOS 0.1 Preview
Linux desktop distribution using unmodified upstream Linux, DEs-UI and Ubuntu
26.04-compatible packages. This live preview uses public account desos/password
desos and passwordless live administration. Installation creates your own user;
live privileges are removed from the installed system. No development keys are
included. Secure Boot and encrypted installation are not enabled in this preview.
''')
    put('etc/hostname', 'desos-live\n')
    put('etc/hosts', '127.0.0.1 localhost\n127.0.1.1 desos-live\n::1 localhost ip6-localhost ip6-loopback\n')
    put('etc/casper.conf', '''export USERNAME="desos"
export USERFULLNAME="DesOS Live Session"
export HOST="desos-live"
# Casper uses this field to select the base system's modprobe options.
export BUILD_SYSTEM="Ubuntu"
export FLAVOUR="DesOS"
''')
    put('etc/initramfs-tools/conf.d/desos-live', 'CASPER_GENERATE_UUID=1\n')
    put('etc/initramfs-tools/conf.d/desos-modules', 'MODULES=most\nCOMPRESS=zstd\n')
    put('usr/share/initramfs-tools/scripts/casper-bottom/99-desos-live', LIVE_HOOK, 0o755)
    put('etc/greetd/config.toml', '''[terminal]
vt = 7

[default_session]
command = "/usr/bin/des-ui-greeter"
user = "_greetd"

[initial_session]
command = "/usr/lib/desos/live-session"
user = "desos"
''')
    put('usr/lib/desos/live-session', LIVE_SESSION, 0o755)
    put('etc/sudoers.d/90-desos-live', 'desos ALL=(ALL) NOPASSWD: ALL\n', 0o440)
    put('etc/NetworkManager/conf.d/10-globally-managed-devices.conf', '# DesOS manages wired and wireless devices.\n')
    put('etc/NetworkManager/conf.d/20-desos.conf', '[main]\ndns=systemd-resolved\n[ifupdown]\nmanaged=true\n')
    put('etc/netplan/01-desos.yaml', 'network:\n  version: 2\n  renderer: NetworkManager\n', 0o600)
    put('etc/default/grub.d/10-desos.cfg', 'GRUB_DISTRIBUTOR=DesOS\nGRUB_DEFAULT=saved\nGRUB_SAVEDEFAULT=false\nGRUB_TIMEOUT=5\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\n')
    put('usr/lib/desos/select-upstream-kernel', SELECT_KERNEL, 0o755)
    put('etc/kernel/postinst.d/zzzz-desos-select-kernel', '#!/bin/sh\nset -e\nexec /usr/lib/desos/select-upstream-kernel\n', 0o755)
    put('etc/kernel/postrm.d/zzzz-desos-select-kernel', '#!/bin/sh\nset -e\nexec /usr/lib/desos/select-upstream-kernel\n', 0o755)
    put('etc/skel/.config/des-ui/desos-preview.txt', 'DesOS preview live system. See /usr/share/doc/desos/README.\n')


def audit_credentials(root: Path, parent: Path = Path('/srv/desos-build')) -> None:
    root = validate_rootfs(root, parent)
    forbidden = list((root / 'etc/ssh').glob('ssh_host_*_key'))
    for base in [root / 'home', root / 'root']:
        if base.exists():
            forbidden += list(base.rglob('authorized_keys'))
            forbidden += list(base.rglob('id_ed25519'))
            forbidden += list(base.rglob('id_rsa'))
    if forbidden:
        raise ValueError('Development/host credentials must not ship: '+', '.join(str(p.relative_to(root)) for p in forbidden))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--audit-only', action='store_true')
    args = parser.parse_args()
    if not args.audit_only:
        write_configuration(args.root)
    audit_credentials(args.root)
    print(json.dumps({'root':str(args.root),'credentials_audit':'passed'}))
