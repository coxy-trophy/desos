# DesOS installer design

The installer is the stock Ubuntu Calamares 3.3.14 (Qt 6) with DesOS branding.
Its one job is to turn an explicitly chosen disk into someone's own desktop.
Calamares owns every form, validation, the disk summary and the final
confirmation. Branding paints them and adds three things of its own: a step
rail, a slideshow to read while files copy, and a welcome picture.

**Status: unverified.** Calamares, Qt and qmllint are not on the development
host, so nothing below has been rendered. The tests check what can be checked
without rendering. The first image built with this branding needs the review
in [Review in a built image](#review-in-a-built-image).

## What the 0.1 screenshots showed

The redesign started from the 0.1 validation screenshots
(`output/live-v3/validation`, `work/qa`), which showed four faults:

- The welcome picture was missing. Calamares loads it with `QPixmap`, and the
  live image has no Qt SVG image-format plugin (only gif, ico and jpeg), so an
  SVG there decodes to nothing. The sidebar logo worked only because that one
  goes through Calamares's own SVG renderer.
- "Restart now" on the last page had no visible box. Check and radio
  indicators were dark on dark, so their state could not be seen. The same
  hid the selected state of "Erase disk" and "Manual partitioning".
- Partition names sat on a white slab, because Calamares paints them in fixed
  black.
- The current step was a full accent slab, which DEs-UI reserves for nothing.

## Structure

| Part | Mechanism | Why |
| --- | --- | --- |
| Pages | Calamares widget modules, `stylesheet.qss` | Behaviour, validation and translations stay upstream's |
| Back, Next, Cancel | `navigation: widget` | A QML error must never leave someone without a Next button |
| Step rail | `sidebar: qml`, `calamares-sidebar.qml` | The widget rail paints itself in C++ and cannot show done and to-do |
| Install progress | `show.qml`, `slideshowAPI: 2` | Something worth reading while files copy |
| Welcome picture | `welcome.png` | The only place on that page DesOS can speak |

If `calamares-sidebar.qml` fails to load, the rail is an empty panel and every
page and button still works. Setting `sidebar: widget` in `branding.desc`
brings back Calamares's own list; the `style:` colours are chosen for it.

## Colour

DEs-UI's matte palette, from `des-ui/shell/src/ui.rs`, plus the few values a
form needs that a terminal desktop does not.

| Token | Value | Use |
| --- | --- | --- |
| ground | `#06080b` | Page |
| panel | `#0a0e12` | Rail, inset fields, lists |
| track | `#141b22` | Resting buttons and choosers |
| line / strong | `#1a232b` / `#2a3742` | Separators, frames, grid lines |
| edge | `#5b7385` | Outline of every control: 3:1 on ground, panel and track |
| hover / tint / pressed | `#1b252e` / `#0f2a29` / `#254c4d` | Pointer over, selected, held |
| text / secondary | `#dce7ec` / `#8fa3b0` | Body, supporting |
| label | `#6f8492` | Steps still to do, on the rail's panel only |
| quiet | `#7d909c` | Disabled text; still 4.5:1 |
| accent | `#4fd8ce` | Focus, the current step, the lit primary action |
| chunk | `#29706b` | Progress fill and the resting primary action |
| caution | `#f5b85a` | Field errors and warnings on the Users page |

Rules, each enforced by `tests/test_installer_branding.py`:

- Every text and background pair is at least 4.5:1, disabled and error states
  included. Nothing is exempted.
- Every control outline, focus ring and state mark is at least 3:1 against the
  surfaces it can sit on.
- Only these tokens appear in the style sheet, the QML and the SVG.
- State never rests on hue alone. A ticked box gains a tick, a chosen radio
  becomes a thick ring, the current step is a ring with a centre and bolder
  text, and position is also written out as "STEP 3 OF 8".

Selections are a quiet tint with bright text. Accent is spent on three things:
where the keyboard is, where you are, and the action that moves forward.

## Type

Chakra Petch (Medium and SemiBold) for interface text at 14 px; JetBrains Mono
for short upper-case labels, key caps, the percentage and the log. Both are
bundled in `installer/fonts` under the SIL Open Font License and installed
into the image. Nothing smaller than 10 px, and 10 to 11 px only for tracked
mono labels.

## Focus

Every focusable control gets a 2 px accent ring; padding shrinks by the same
amount so nothing moves. The primary action (Next, Install, and the default
button of a dialog) rests on the chunk teal with light text and lights to full
accent with a white ring when it has the pointer or the keyboard, so focus
there is a 3:1 change of the whole button, not only of its edge. The About
control in the rail is reachable with Tab and activates with Enter or Space.

## The step rail

`calamares-sidebar.qml` is one column: the DesOS wordmark, the steps joined by
a line, then position, then About.

- Done: filled dot, line above and below filled.
- Current: ring with a centre, a tint behind the row, SemiBold text.
- To do: hollow ring, dimmer text.
- Below: "STEP n OF m" and a thin bar.
- On the last step only: what to do when the page says All done. Restart,
  take out the USB when the screen goes dark, sign in with the name and
  password just chosen.

Rows shrink from 36 px to 24 px so eight steps and that note fit a 520 px
window. The rail draws no images, so it does not depend on an SVG plugin.

## The pages

`stylesheet.qss` is organised by kind of control, then by page. It covers
buttons, tool buttons, line and text edits, spin boxes with their arrows,
choosers and their pop-up lists, check boxes, radio buttons, check marks
inside lists, lists, tables, trees and their headers, group boxes, scroll
areas and both scroll bars, menus, tooltips, the progress bar and the log.

Page by page:

- **Welcome.** The language chooser gets a globe (`language.svg`, named in
  `welcome.conf`) in place of Calamares's black-and-white box, for someone who
  cannot read the language the page starts in.
- **Location, Keyboard.** The choosers, lists and buttons around the map and
  the keyboard preview are styled. The map and the preview paint themselves.
- **Partitions.** Radio marks are now visible. The disk bars lose the generic
  list frame. Partition names keep a light plate (see below).
- **Users.** Field messages are caution amber. Automatic login stays visibly
  unavailable; that block is unchanged from 0.1 and is the only place the
  style sheet touches a Calamares property.
- **Summary.** Section titles are quiet labels; each body is an inset panel.
- **Install.** Dark trough, teal fill, mono percentage readable on both.
- **Finished.** The "Restart now" box is visible.

Style-sheet images (`img/*.png`: arrows, tick, dash) use the absolute path
`/usr/share/calamares/branding/desos/`. Calamares applies the sheet as a
string, so relative paths would resolve against its working directory. A test
ties the paths to the destination in `install_into_rootfs.py`.

## The slideshow

`show.qml` is six cards, each a mono tag, a headline, one paragraph and one
quieter paragraph:

1. What you are installing.
2. Keys for the first boot, from the "Keys" table in the DEs-UI README. A
   test compares them with that table.
3. Undo anything: with the default Btrfs layout the system is snapshotted
   before a system change and can be returned to. Not a backup of your files.
4. Proves what it is: the system checks itself against its signed build
   record. It cannot catch everything.
5. Phone as the key: a paired phone can lock, unlock and approve. Nothing is
   paired during installation.
6. When this finishes: restart, take out the USB, what sign-in looks like.

Cards 3 to 5 describe features that are still being built. They say only what
those features are for, in plain words, and a test rejects promise words.

A click or the right arrow goes forward, a right click or the left arrow goes
back, and the marks along the bottom jump to a card. One timer advances the
card every sixteen seconds and stops when installation ends. The only motion
is a 300 ms cross-fade. The card shrinks as a whole if the window is too small
to hold it. It imports QtQuick and nothing else, and loads no images.

## Wording that DesOS cannot change

The welcome heading, its one line of body text and the whole of the last page
are Calamares's translated strings. The widget modules have no setting for
them, and overriding them from the style sheet would replace Calamares's
translations with English and could mask the "Installation Failed" text. So:

- The welcome page speaks through `welcome.png`: nothing is written until you
  confirm, everything installs from the USB, keep the power connected.
- The last page is left as upstream wrote it. What to do next is told on the
  last slideshow card and in the rail beside the page.

The words in the picture, the rail and the slideshow are English only and the
words in the picture are not available to a screen reader. Both are costs of
staying on the widget modules.

## Artwork

`installer/branding/build_art.py` draws each picture once and writes it twice:

- SVG with all lettering converted to outlines, so it looks the same with or
  without the fonts installed: `welcome.svg`, `icon.svg`, `language.svg`.
- PNG for everything Qt itself decodes: `welcome.png` (2x, fitted to the page
  by `welcomeExpandingLogo: true`), `icon.png` (the window icon) and
  `img/*.png` with `@2x` versions.

`icon.svg` is also installed as the launcher icon. The welcome picture reads
the release name from `branding.desc`; a test fails if the two drift apart.
Run the script after changing either. It needs python3-fonttools and
python3-cairo on the build host only.

## What the style sheet cannot reach

- **Partition names and sizes.** `PartitionLabelsView` paints names in fixed
  black and sizes in fixed grey (`#a0a0a4`). No single background gives both
  4.5:1. The plate is light so names are 18:1; the grey size line is about
  2.3:1. That line fails AA and only an upstream change can fix it.
- **Links** in rich-text labels (the requirements "Details" link, the About
  box) use Qt's palette link colour, which a widget style sheet cannot set.
  Expect default blue on dark there.
- **The timezone map and the keyboard preview** paint themselves with their
  own colours.
- **Pop-up list rows** in choosers are drawn by Qt's combo delegate, so only
  their selection colours are set, not their row padding.

## Safety properties, unchanged

- No disk operation is preselected; the summary and Calamares's final
  confirmation stay.
- Automatic login stays unavailable.
- "Restart now" stays unticked.
- No remote content, analytics, GeoIP or log upload. The QML imports no
  network or image elements and a test enforces that.
- Pages and navigation are widgets. No page depends on a QML file.

## Calamares contracts relied on

All confirmed against the 3.3.14 package in `work/package-examples` and the
0.1 image, not from memory of the documentation.

| Contract | Where confirmed |
| --- | --- |
| `sidebar: qml` loads `calamares-sidebar.qml` from the branding directory | Comments in the packaged default `branding.desc` |
| `ViewManager` as model with role `display`, `ViewManager.currentStepIndex`, `Branding.styleString`, `debug.about()`, `debug.toggle()`, `debug.enabled`, imports `io.calamares.ui` and `io.calamares.core` | The default sidebar QML embedded in `/usr/bin/calamares` |
| `Branding.string(entry)` and the `ShortVersion` entry are callable from QML | Meta-object strings in `libcalamaresui.so.3.3.14` |
| `slideshowAPI: 2` calls `onActivate()` and `onLeave()` and sets `activatedInCalamares` | Packaged default `show.qml` and `libcalamaresui` strings |
| Object names `view-button-next`, `aboutButton`, `sidebarApp`, `logoApp`, `mainApp`, `checkBoxDoAutoLogin`, `summaryItemTitle`, `summaryItemBody`, `label…Error`, classes `PartitionBarsView`, `PartitionLabelsView` | Strings in the binary and module plugins; a test repeats the search |
| Welcome picture goes through `QPixmap(QString)`; language icon through `Branding::image(name)` | Imported symbols of the welcome plugin |
| Style sheet applied with `QWidget::setStyleSheet`; window icon with `QIcon(QString)` | Imported symbols of `/usr/bin/calamares` |
| No Qt SVG image-format plugin; QtQuick, Controls and Layouts present | File list of the 0.1 `filesystem.squashfs` |

## Review in a built image

Everything visual is unverified until this is done. In order of how likely
each is to be wrong:

1. The rail loads at all (run `desos-installer` from the live terminal and
   read the log for QML errors), fits at 520 px height, and Tab reaches About.
2. Spin-box arrows, chooser arrows and ticks appear (absolute image paths).
3. Radio marks on the Partitions page: thin ring off, thick ring on.
4. The welcome picture is sharp and does not force the window wider.
5. The slideshow shows, advances, and its text fits.
6. The primary action reads as primary at rest and lights on focus.
7. Lists and tables do not shift by a pixel when they take focus.
8. The globe replaces the language icon.
9. Each page at 1280x800, where the DEs-UI workspace is 976x528 and
   Calamares's own minimum window is 1024x520.
