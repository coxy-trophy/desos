# DesOS boot menu

The GRUB menu of the live medium and of the installed system, in the DEs-UI
instrument language: the splash ground `#06080b`, the cyan accent `#4fd8ce`,
the DesOS bird and wordmark (branding/bird.svg), Chakra Petch for interface text and JetBrains
Mono for readouts. It is a keyboard-only menu with a clear selected row, a
countdown readout and meter, key hints, and a plain text menu behind every
step that can fail.

**Status.** Rendered and operated in QEMU with BIOS GRUB 2.14 at 1920x1080,
1360x768, 1024x768 and 800x600 (see [What was checked](#what-was-checked)).
Not run under UEFI, on real hardware, from a real ISO, or through
`grub-mkconfig` on an installed system.

## Files

```
component.json                                   manifest for image/components.py
generate.py                                      writes everything under themes/desos
tools/preview.py                                 boots the menu in QEMU, saves screenshots
rootfs/boot/grub/themes/desos/                   theme.txt, *.pf2, *.png, icons/, font licences
rootfs/etc/default/grub.d/20-desos-theme.cfg     GRUB_THEME, GRUB_GFXMODE, GRUB_FONT
rootfs/etc/grub.d/06_desos_theme                 text-menu colours, splash ground (executable)
```

Packages: `grub-common` and `grub2-common` (already in the image). The
`gfxmenu`, `png` and `gfxterm_background` modules come with `grub-pc-bin` and
`grub-efi-amd64-bin`, which the image also already has.

## Changing the theme

Nothing under `rootfs/boot/grub/themes/desos` is edited by hand.

```sh
python3 boot-theme/generate.py            # rewrite the theme
python3 boot-theme/generate.py --check    # compare a fresh build with the committed one
python3 boot-theme/tools/preview.py --config live --size 1920x1080 --out /tmp/shots
```

`generate.py` needs Pillow and `grub-mkfont`; it reads the fonts in
`installer/fonts`. Output is byte-identical for one Pillow/FreeType/GRUB
version. A different FreeType can round a glyph edge differently, so `--check`
is a maintainer tool and the unit tests assert structure, not bytes.

`tools/preview.py` needs `grub-mkimage` (i386-pc), `qemu-system-x86_64` and
ImageMagick. It starts one QEMU with 256 MB, no network and no display, and
kills it. Configurations: `live` (the real `iso_recipe.grub_menu()`),
`installed` (the header grub-mkconfig writes for the fragment, the real output
of `06_desos_theme`, and sample entries with desos-undo's titles),
`installed-classes` (the same with the icon classes below), or a path.

## Where the theme lives, and why

`/boot/grub/themes/desos`, beside `grub.cfg`. GRUB reads the theme with its
own filesystem drivers before any Linux filesystem is mounted, so the theme
must be wherever GRUB can already read its configuration:

- **btrfs with subvolumes** (the DesOS default, `/boot` inside `@`):
  `grub-mkconfig` writes `set theme=($root)/@/boot/grub/themes/desos/theme.txt`
  through `make_system_path_relative_to_its_root`, the same way it addresses
  the kernel.
- **separate `/boot`**: the files are on that partition, like the kernel.
- **encrypted root with unencrypted `/boot`**: a theme under `/usr/share`
  would be unreadable and silently dropped; this one is not.
- **rollback**: the theme is in the same snapshot as the `grub.cfg` that
  names it.

`00_header` of grub-mkconfig does the rest for `GRUB_THEME`: it makes the
device accessible, loads every `*.pf2` in the theme directory, loads `png` and
exports `theme`. If the file is missing or unreadable it skips the theme and
the text menu is used.

`GRUB_TERMINAL_OUTPUT` must be unset or contain `gfxterm`. The fragment does
not set it, so `GRUB_TERMINAL=console` (or `serial`) in `/etc/default/grub`
still switches the graphical menu off. `installer/calamares/modules/grubcfg.conf`
used to write `GRUB_TERMINAL_OUTPUT: console`; that line is gone and a test
keeps it gone.

To opt out on one machine: a later fragment such as
`/etc/default/grub.d/99-local.cfg` with `GRUB_THEME=` and `GRUB_FONT=`, then
`sudo update-grub`.

## Layout

- Header: the bird and the wordmark (image, antialiased), a status readout, a hairline.
- Section row: `SYSTEMS`, the countdown readout on the right, and a rule that
  is also the countdown meter. Pressing any key removes the readout and the
  meter; the hairline stays.
- List: a fixed 760 px column centred on the screen. Selected row: a quiet
  tint, a solid accent bar on its left edge and bright text; other rows are
  secondary grey. A long list scrolls and shows a thin thumb on the right.
- Footer: the keys.
- Editor (`e`), command line (`c`) and boot messages open in a frame card over
  the same screen, in JetBrains Mono.

Everything except the list column is placed in percentages, so one theme
serves 1920x1080, 1366x768 and 1024x768. At 800x600, which GRUB uses on a BIOS
that reports no display size, the column has 20 px margins. Below that the
column is cut off at both sides.

GRUB fonts are 1-bit bitmaps: there is no smoothing. Sizes were chosen where
the faces hint cleanly (Chakra Petch 20 and 14, JetBrains Mono 14; at 12 and
13 px JetBrains Mono loses the dot on "i").

Things GRUB's theme engine cannot do, and what is done instead:

- **No per-entry description line.** The description is part of the entry
  title: `Title - what it does`, plain ASCII so the text and serial menus show
  the same thing.
- **No submenu heading.** A submenu replaces the list; `ESC back` is in the
  footer. Entry titles inside a submenu must stand on their own.
- **No icon without a class.** See below.
- **No HiDPI scaling.** On a 4K panel at its native mode the menu is correct
  but small. `generate.py` has the sizes in one place if a 2x variant is
  wanted; choosing between variants at boot is not implemented because it
  could not be tested here.

## Classes and icons for menu entries

GRUB draws `icons/<class>.png` for the first class of an entry that has a
file. Entries without any class get an empty icon slot. Put the specific class
first.

| Entry | `--class` | Icon |
|-------|-----------|------|
| The system (grub-mkconfig does this already) | `desos` (also `gnu-linux`, `gnu`, `os`, `linux`) | the bird |
| Recovery mode | `recovery`, `desos-recovery` | cross |
| One snapshot | `desos-snapshot`, `snapshot` | clock |
| The snapshots submenu | `desos-snapshots`, `snapshots` | stacked states |
| Automatic start of the last working system | `desos-known-good`, `desos-rollback`, `known-good`, `rollback` | return arrow with a tick |
| Any other submenu | `submenu`, `desos-submenu`, `advanced` | list with a chevron |
| Ubuntu compatibility kernel | `compat`, `desos-compat`, `ubuntu` | stacked squares |
| Verbose/diagnostic start | `debug`, `desos-debug` | prompt |
| Installer | `install`, `desos-install` | arrow into a tray |
| Another disk | `disk`, `desos-disk`, `harddisk` | drive |
| Firmware setup | `firmware`, `desos-firmware`, `efi`, `uefi` | chip |
| Memory test | `memtest`, `memtest86`, `memtest86+` | memory module |
| Power off, restart | `power`, `shutdown`, `reboot`, `desos-power` | power mark |

Guidance for generated entries (snapshots, rollback):

- Keep titles under about 70 characters; longer ones are cut at the right edge
  of the column. Lead with what distinguishes the entry (`Snapshot 41
  2026-11-10 19:10  pre: apt full-upgrade`).
- ASCII only if the title should read the same on the serial and text menus.
  Other scripts render on the installed system through GRUB's unicode font,
  which `06_desos_theme` keeps loaded as a fallback.
- `echo` lines printed while an entry starts appear in the frame card on the
  splash ground, so a message such as "Starting DesOS snapshot 41 ..." is
  visible and does not flash a black screen.
- An automatic path that shortens the timeout still shows the countdown
  readout and meter; `timeout=-1` hides both.

`undo/` currently emits its entries (`desos-undo-heal`, the
`desos-undo-snapshots` submenu and its `desos-undo-snapshot-N` entries)
without classes, so they render correctly but without icons. Ubuntu's
`Advanced options` submenu and `UEFI Firmware Settings` entry also carry no
class.

## The live medium

`image/iso_recipe.py` copies this theme to `/boot/grub/themes/desos` on the
ISO (a missing theme fails the build) and writes the menu. At boot:

1. If `theme.txt` is there, the fonts load and `terminal_output gfxterm`
   succeeds, the theme is used. If any of those fails, the menu is the plain
   text menu on the firmware console, in light grey with a cyan highlight.
2. GRUB probes the first UART. Only if one answers does it add the serial
   terminal and set `desos_serial=console=ttyS0,115200n8` for the kernel.
3. Every kernel entry ends its consoles with `console=tty0`, and the splash
   entries add `plymouth.ignore-serial-consoles`. See `image/ISO.md`.

## What was checked

With `tools/preview.py` (QEMU, SeaBIOS, GRUB 2.14-4 i386-pc, software
emulation), looking at the screenshots:

- live menu at 1920x1080, 1360x768, 1024x768, 800x600; selection moves with
  the arrow keys; the countdown readout counts and the meter fills; a key
  press removes both;
- installed-style menu, entering the snapshots submenu, scrolling a 24-entry
  list with the thumb visible, with and without icon classes;
- editor and command line inside the frame card; an entry whose kernel is
  missing reports its error in the card;
- no UART: `desos_serial` empty, the empty variable adds no argument, only
  gfxterm active. With a UART: `serial gfxterm` active and the text menu also
  drawn on the serial line;
- fallbacks: no theme on the medium, a font missing (text menu on the
  console); a syntactically broken `theme.txt` (GRUB prints the error, waits
  for a key or about ten seconds, then shows the text menu in gfxterm);
- "Boot from first hard disk" on BIOS with the medium as `hd0`: the second
  disk's boot sector ran and saw itself as BIOS drive `0x80`.

Not checked: UEFI (no OVMF or x86_64-efi GRUB on the development host), so
the firmware-setup entry, `exit 1` and GOP mode selection are unverified;
`gfxmode=auto` with real display data (SeaBIOS's VGA BIOS has none, modes were
forced for the screenshots); 1366x768 exactly (QEMU offers 1360x768); the
hand-over to Plymouth, since no kernel was booted; a real `grub-mkrescue` ISO;
`update-grub` with the fragment on Ubuntu 26.04; Secure Boot.
