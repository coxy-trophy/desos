#!/usr/bin/env python3
"""Boot a GRUB menu with the DesOS theme in QEMU and save screenshots.

A maintainer tool for looking at the theme; nothing in the image build uses it.
It builds a GRUB-only BIOS image (no kernel, no root filesystem) whose memdisk
has the same layout as the live medium, starts one small QEMU without network
or KVM, and takes screenshots through the QEMU monitor:

    python3 boot-theme/tools/preview.py --config live --size 1920x1080 --out /tmp/shots
    python3 boot-theme/tools/preview.py --config installed --keys down,down,ret --shot after

Needs grub-mkimage with the i386-pc platform, qemu-system-x86_64 and
ImageMagick. grub-mkrescue/xorriso are not needed.
"""
import argparse
import importlib.util
import io
import pathlib
import re
import socket
import subprocess
import tarfile
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
COMPONENT = HERE.parent
THEME = COMPONENT / 'rootfs/boot/grub/themes/desos'
PLATFORM = pathlib.Path('/usr/lib/grub/i386-pc')


def live_config():
    spec = importlib.util.spec_from_file_location('iso_recipe', REPO / 'image/iso_recipe.py')
    recipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recipe)
    return recipe.grub_menu()


def installed_config(entries=24, classes=False):
    """What grub-mkconfig writes with the component's fragment, plus sample entries.

    The header follows Ubuntu's 00_header for GRUB_FONT and GRUB_THEME and then
    includes the real output of rootfs/etc/grub.d/06_desos_theme. The entries are
    samples: the stock menu and a long snapshot submenu with the titles
    desos-undo writes (undo/rootfs/usr/lib/desos-undo/desos_undo.py). With
    classes=True the snapshot entries carry the documented icon classes.
    """
    theme = '/boot/grub/themes/desos'
    fragment = (COMPONENT / 'rootfs/etc/default/grub.d/20-desos-theme.cfg').read_text()
    font = re.search(r'^GRUB_FONT=(\S+)$', fragment, re.M)[1]
    script = subprocess.run(['sh', str(COMPONENT / 'rootfs/etc/grub.d/06_desos_theme')],
                            capture_output=True, text=True, check=True).stdout
    lines = ['set default=0', 'set timeout=5', 'set timeout_style=menu',
             'search --no-floppy --file --set=root /.disk/desos-live-id',
             f'if loadfont {font} ; then', '  set gfxmode=auto', '  insmod all_video', '  insmod gfxterm', 'fi',
             'terminal_output gfxterm', 'insmod gfxmenu']
    lines += [f'loadfont ($root){theme}/{path.name}' for path in sorted(THEME.glob('*.pf2'))]
    lines += ['insmod png', f'set theme=($root){theme}/theme.txt', 'export theme', script,
              "menuentry 'DesOS' --class desos --class gnu-linux --class gnu --class os { true }",
              "submenu 'Advanced options for DesOS' {",
              "  menuentry 'DesOS, with Linux 6.18.62-desos' --class desos --class gnu-linux { true }",
              "  menuentry 'DesOS, with Linux 6.18.62-desos (recovery mode)' --class recovery --class desos { true }",
              "  menuentry 'DesOS, with Linux 7.0.0-38-generic' --class compat --class desos { true }",
              "  menuentry 'DesOS, with Linux 7.0.0-38-generic (recovery mode)' --class recovery --class desos { true }",
              '}',
              "submenu 'DesOS snapshots (start an earlier system state)' --id desos-undo-snapshots%s {"
              % (' --class desos-snapshots' if classes else '')]
    reasons = ('pre: apt install blender', 'post: apt install blender', 'boot-good: reached the sign-in screen',
               'manual: before changing the display setup', 'pre: apt full-upgrade', 'post: apt full-upgrade')
    for index in range(entries):
        day, hour = 11 - index // 3, 21 - (index * 7) % 13
        lines.append(f"  menuentry 'Snapshot {41 - index}  2026-11-{day:02d} {hour:02d}:10  {reasons[index % 6]}'"
                     + (' --class desos-snapshot' if classes else '') + ' { true }')
    lines += ['}', "menuentry 'Memory test (memtest86+x64.bin)' --class memtest { true }",
              "menuentry 'UEFI Firmware Settings' --class firmware { true }", '']
    return '\n'.join(lines)


# QEMU sendkey names for characters typed with "type:" in --keys.
KEYS = {' ': 'spc', ';': 'semicolon', ',': 'comma', '-': 'minus', '=': 'equal', '.': 'dot', '/': 'slash', '[': 'bracket_left', ']': 'bracket_right',
        '$': 'shift-4', '_': 'shift-minus', '#': 'shift-3', '{': 'shift-bracket_left', '}': 'shift-bracket_right'}


def key_names(spec):
    for key in filter(None, spec.split(',')):
        if key.startswith('type:'):
            for character in key[5:]:
                yield KEYS.get(character) or ('shift-' + character.lower() if character.isupper() else character)
        else:
            yield key


class Monitor:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        for _ in range(100):
            try:
                self.socket.connect(str(path))
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit('QEMU monitor did not appear')
        self.read()

    def read(self):
        self.socket.settimeout(0.5)
        data = b''
        try:
            while not data.rstrip().endswith(b'(qemu)'):
                data += self.socket.recv(65536)
        except socket.timeout:
            pass
        return data

    def command(self, text):
        self.socket.sendall(text.encode() + b'\n')
        return self.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', default='live', help='live, installed, installed-classes, or a grub.cfg path')
    parser.add_argument('--theme', type=pathlib.Path, default=THEME)
    parser.add_argument('--size', default='1920x1080')
    parser.add_argument('--out', type=pathlib.Path, default=pathlib.Path('/tmp/claude-desos-grub'))
    parser.add_argument('--shot', default='menu', help='screenshot name')
    parser.add_argument('--keys', default='',
                        help='comma-separated QEMU sendkey names sent before the screenshot; '
                             '"sleep2" waits, "type:echo x" types text')
    parser.add_argument('--wait', type=float, default=20,
                        help='longest wait for the graphical menu; a text-mode menu waits this long')
    parser.add_argument('--settle', type=float, default=1.0, help='seconds between the last key and the screenshot')
    parser.add_argument('--serial', choices=['file', 'none'], default='file', help='give the machine a UART or not')
    parser.add_argument('--timeout', type=int, default=30, help='menu timeout for the preview, 0 keeps the config value')
    parser.add_argument('--auto-mode', action='store_true', help='keep gfxmode=auto instead of forcing --size')
    parser.add_argument('--second-disk', action='store_true',
                        help='add a second disk with a test boot sector, to try "Boot from first hard disk"')
    parser.add_argument('--no-theme', action='store_true', help='leave the theme off the medium (fallback test)')
    args = parser.parse_args()
    width, height = (int(part) for part in args.size.split('x'))
    config = {'live': live_config, 'installed': installed_config,
              'installed-classes': lambda: installed_config(classes=True)}.get(args.config)
    text = config() if config else pathlib.Path(args.config).read_text()
    if args.timeout:
        text = re.sub(r'^set timeout=\d+$', f'set timeout={args.timeout}', text, flags=re.M)
    if not args.auto_mode:
        # SeaBIOS's VGA BIOS has no display data channel, so "auto" would be 800x600.
        text = text.replace('set gfxmode=auto', f'set gfxmode={args.size}')
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='desos-grub-') as temporary:
        work = pathlib.Path(temporary)
        (work / 'grub.cfg').write_text(text)
        subprocess.run(['grub-script-check', str(work / 'grub.cfg')], check=True)
        # The "medium" is a tar archive on a raw disk, which GRUB reads as a
        # filesystem: the same paths as the ISO without needing xorriso.
        with tarfile.open(work / 'medium.tar', 'w', format=tarfile.USTAR_FORMAT) as medium:
            def add(name, data):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                medium.addfile(info, io.BytesIO(data))
            add('.disk/desos-live-id', b'preview\n')
            add('boot/grub/grub.cfg', text.encode())
            for module in sorted(PLATFORM.iterdir()):
                if module.suffix in ('.mod', '.lst'):
                    add('boot/grub/i386-pc/' + module.name, module.read_bytes())
            if not args.no_theme:
                for path in sorted(args.theme.rglob('*')):
                    if path.is_file():
                        add('boot/grub/themes/desos/' + path.relative_to(args.theme).as_posix(), path.read_bytes())
        subprocess.run(['grub-mkimage', '-O', 'i386-pc', '-p', '(hd0)/boot/grub', '-o', str(work / 'core.img'),
                        'biosdisk', 'tar'], check=True)
        # lnxboot.img lets QEMU load GRUB's core image as if it were a kernel.
        (work / 'grub.bin').write_bytes((PLATFORM / 'lnxboot.img').read_bytes() + (work / 'core.img').read_bytes())
        start = ['-kernel', str(work / 'grub.bin'),
                 '-drive', f'file={work / "medium.tar"},format=raw,if=ide,snapshot=on']
        if args.second_disk:
            # The medium is hd0, as a USB stick is. A second disk carries a boot
            # sector that prints a line and the low digit of the BIOS drive number
            # it was started as (0 when GRUB's drive swap worked).
            code = bytes.fromhex('31c08ed8 be227c ac 08c0 7409 b40e bb0700 cd10 ebf2 88d0 240f 0430 b40e cd10 f4 ebfd')
            sector = (code + b'DESOS TEST DISK STARTED AS BIOS DRIVE 8\0').ljust(510, b'\0') + b'\x55\xaa'
            (work / 'other.disk').write_bytes(sector.ljust(1 << 20, b'\0'))
            start += ['-drive', f'file={work / "other.disk"},format=raw,if=ide,snapshot=on']
        serial = ['-serial', f'file:{args.out / (args.shot + ".serial.txt")}'] if args.serial == 'file' else ['-serial', 'none']
        qemu = subprocess.Popen(['qemu-system-x86_64', '-m', '256', '-nic', 'none', '-display', 'none',
                                 '-vga', 'none', '-device', f'VGA,edid=on,xres={width},yres={height}',
                                 *start, *serial, '-parallel', 'none',
                                 '-monitor', f'unix:{work / "monitor"},server,nowait'])
        try:
            monitor = Monitor(work / 'monitor')
            # Emulated start-up time varies; wait until GRUB leaves the BIOS text
            # mode and draws the theme, or the full --wait for a text menu.
            deadline = time.monotonic() + args.wait
            while time.monotonic() < deadline:
                time.sleep(0.5)
                monitor.command(f'screendump {work / "probe.ppm"}')
                time.sleep(0.2)
                probe = work / 'probe.ppm'
                data = probe.read_bytes() if probe.exists() else b''
                # Graphics mode and the theme's accent on screen: the menu is drawn.
                if data[:15].split()[1:3] not in ([b'720', b'400'], []) and b'\x4f\xd8\xce' in data:
                    time.sleep(1)
                    break
            for key in key_names(args.keys):
                if key.startswith('sleep'):
                    time.sleep(float(key[5:] or 1))
                    continue
                monitor.command('sendkey ' + key)
                time.sleep(0.25)
            time.sleep(args.settle)
            monitor.command(f'screendump {work / "shot.ppm"}')
            time.sleep(0.5)
            target = args.out / f'{args.shot}.png'
            subprocess.run(['convert', str(work / 'shot.ppm'), str(target)], check=True)
            print(target)
        finally:
            qemu.kill()
            qemu.wait()


if __name__ == '__main__':
    main()
