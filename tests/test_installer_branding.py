"""Contracts for the DesOS installer branding (installer/branding/desos).

Nothing here renders a page: Calamares and Qt are not available to the test
host. What can be checked without them is checked hard: every colour pair in
the style sheet and in both QML files against WCAG AA, that keyboard focus is
visible on every focusable control, that the QML stays inside the Calamares
3.3.14 contract and cannot stop an installation, and that every referenced
asset exists in a format the live image can decode.
"""
from pathlib import Path
import re
import struct
import unittest
import xml.dom.minidom

import yaml

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "installer"
BRANDING = INSTALLER / "branding/desos"
# Real Calamares 3.3.14 binaries, when the build tree has them (not in CI).
CALAMARES_FILES = REPO / "work/package-examples/calamares"
DES_UI_README = REPO.parent / "des-ui/README.md"

GROUND, PANEL, TRACK = "#06080b", "#0a0e12", "#141b22"
# DEs-UI's own palette (des-ui/shell/src/ui.rs) plus the installer's additions.
TOKENS = {
    "#06080b", "#0a0e12", "#141b22", "#1a232b", "#2a3742", "#6f8492", "#8fa3b0",
    "#dce7ec", "#f5b85a", "#4fd8ce",
    "#5b7385", "#1b252e", "#0f2a29", "#254c4d", "#7d909c", "#29706b",
    "#eef3f6", "#000000", "#ffffff",
}
TEXT_AA, NON_TEXT_AA = 4.5, 3.0


def luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first, second):
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


class Stylesheet:
    """Just enough CSS to resolve the colours a Qt style sheet rule ends up with."""

    # Sub-controls that never carry text; they are judged as shapes instead.
    SHAPES = {
        "handle", "add-line", "sub-line", "add-page", "sub-page", "indicator", "up-button",
        "down-button", "up-arrow", "down-arrow", "drop-down", "branch", "separator", "corner",
    }

    def __init__(self, text):
        self.text = text
        body = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        self.rules = []
        for selectors, block in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            declarations = {}
            for declaration in block.split(";"):
                if declaration.strip():
                    name, _, value = declaration.partition(":")
                    declarations[name.strip()] = value.strip()
            for selector in selectors.split(","):
                self.rules.append((" ".join(selector.split()), declarations))
        self.leftover = re.sub(r"([^{}]+)\{([^{}]*)\}", "", body).strip()

    def declared(self, selector, name):
        value = None
        for candidate, declarations in self.rules:
            if candidate == selector and name in declarations:
                value = declarations[name]
        return value

    @staticmethod
    def fallbacks(selector):
        """The selector, then progressively plainer ones it inherits colours from."""
        seen = []

        def add(value):
            if value and value not in seen:
                seen.append(value)

        add(selector)
        resting = re.sub(r"(?<!:):(?!:)[a-z-]+", "", selector)
        add(resting)
        without_part = resting.split("::")[0]
        add(without_part)
        add(without_part.split("#")[0])
        add(without_part.split(" ")[-1].split(">")[-1].strip())
        add("QWidget")
        return seen

    def colour(self, selector, name):
        for candidate in self.fallbacks(selector):
            value = self.declared(candidate, name)
            if value and HEX.fullmatch(value):
                return value.lower()
        return None

    def border_colour(self, selector):
        for candidate, declarations in reversed(self.rules):
            if candidate != selector:
                continue
            for name in ("border-color", "border"):
                found = HEX.findall(declarations.get(name, ""))
                if found:
                    return found[-1].lower()
        return None

    @classmethod
    def is_shape(cls, selector):
        return any(part.split(":")[0] in cls.SHAPES for part in selector.split("::")[1:])


def strip_qml(text):
    """QML with comments and string contents blanked, for structural checks."""
    out, index, strings = [], 0, []
    while index < len(text):
        if text.startswith("//", index):
            index = text.index("\n", index) if "\n" in text[index:] else len(text)
        elif text.startswith("/*", index):
            index = text.index("*/", index) + 2
        elif text[index] == '"':
            end = index + 1
            while text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            strings.append(text[index + 1:end])
            out.append('""')
            index = end + 1
        else:
            out.append(text[index])
            index += 1
    return "".join(out), strings


def qml_palette(text):
    block = re.search(r"QtObject\s*\{\s*id:\s*pal\b(.*?)\n    \}", text, flags=re.DOTALL).group(1)
    return dict(re.findall(r'readonly property color (\w+): "(#[0-9a-f]{6})"', block)), block


class BrandingDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.desc = yaml.safe_load((BRANDING / "branding.desc").read_text())

    def test_navigation_is_native_and_the_qml_rail_is_optional(self):
        # A QML error must never be able to stop someone installing.
        self.assertEqual(self.desc["navigation"], "widget")
        self.assertIn(self.desc["sidebar"], ("qml", "widget"))
        if self.desc["sidebar"] == "qml":
            self.assertTrue((BRANDING / "calamares-sidebar.qml").is_file())
        settings = yaml.safe_load((INSTALLER / "calamares/settings.conf").read_text())
        shown = [name for phase in settings["sequence"] for name in phase.get("show", [])]
        self.assertFalse([name for name in shown if name.endswith("q")], "pages must stay widget modules")

    def test_assets_exist_in_formats_the_live_image_can_decode(self):
        images = self.desc["images"]
        for filename in [*images.values(), self.desc["slideshow"]]:
            self.assertTrue((BRANDING / filename).is_file(), filename)
        # QPixmap and QIcon load these two; without a Qt SVG plugin only PNG works.
        self.assertTrue(images["productWelcome"].endswith(".png"))
        self.assertTrue(images["productIcon"].endswith(".png"))
        # install_into_rootfs.py publishes this one as the launcher icon.
        self.assertTrue((BRANDING / "icon.svg").is_file())

    def test_no_remote_endpoints_and_slideshow_contract(self):
        self.assertEqual(self.desc["uploadServer"], {"type": "none"})
        self.assertEqual(self.desc["slideshowAPI"], 2)
        self.assertEqual(self.desc["componentName"], "desos")
        for name, value in self.desc["strings"].items():
            self.assertNotIn("://", str(value), name)

    def test_widget_rail_fallback_colours_are_readable(self):
        style = self.desc["style"]
        self.assertGreaterEqual(contrast(style["SidebarText"], style["SidebarBackground"]), TEXT_AA)
        self.assertGreaterEqual(contrast(style["SidebarTextCurrent"], style["SidebarBackgroundCurrent"]), TEXT_AA)
        self.assertTrue({value.lower() for value in style.values()} <= TOKENS)


class StylesheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Stylesheet((BRANDING / "stylesheet.qss").read_text())

    def test_parses_completely(self):
        self.assertEqual(self.sheet.leftover, "")
        self.assertGreater(len(self.sheet.rules), 120)
        for selector, declarations in self.sheet.rules:
            self.assertTrue(selector, declarations)
            for name, value in declarations.items():
                self.assertRegex(name, r"^[a-zA-Z-]+$", selector)
                self.assertTrue(value, f"{selector} {name}")

    def test_only_design_tokens_are_used(self):
        used = {colour.lower() for colour in HEX.findall(re.sub(r"/\*.*?\*/", "", self.sheet.text, flags=re.DOTALL))}
        self.assertEqual(used - TOKENS, set())

    def test_every_text_colour_pair_meets_wcag_aa(self):
        checked = 0
        for selector, declarations in self.sheet.rules:
            if self.sheet.is_shape(selector):
                continue
            pairs = []
            if "color" in declarations or "background-color" in declarations:
                pairs.append((self.sheet.colour(selector, "color"), self.sheet.colour(selector, "background-color")))
            if "alternate-background-color" in declarations:
                pairs.append((self.sheet.colour(selector, "color"), declarations["alternate-background-color"]))
            if "selection-color" in declarations or "selection-background-color" in declarations:
                pairs.append((self.sheet.colour(selector, "selection-color"),
                              self.sheet.colour(selector, "selection-background-color")))
            if "placeholder-text-color" in declarations:
                pairs.append((declarations["placeholder-text-color"], self.sheet.colour(selector, "background-color")))
            for foreground, background in pairs:
                self.assertIsNotNone(foreground, selector)
                self.assertIsNotNone(background, selector)
                ratio = contrast(foreground, background)
                self.assertGreaterEqual(ratio, TEXT_AA, f"{selector}: {foreground} on {background} is {ratio:.2f}:1")
                checked += 1
        # Disabled and error states are included above, not exempted.
        self.assertGreater(checked, 60)
        self.assertGreaterEqual(contrast(self.sheet.colour("QPushButton:disabled", "color"),
                                         self.sheet.colour("QPushButton:disabled", "background-color")), TEXT_AA)

    def test_keyboard_focus_is_visible_on_every_focusable_control(self):
        focusable = [
            "QPushButton", "QPushButton#view-button-next", "QPushButton:default", "QPushButton#aboutButton",
            "QToolButton", "QLineEdit", "QTextEdit", "QPlainTextEdit", "QAbstractSpinBox", "QComboBox",
            "QCheckBox", "QRadioButton", "QAbstractItemView", "QGroupBox", "PartitionBarsView",
            "PartitionLabelsView",
        ]
        for control in focusable:
            with self.subTest(control=control):
                declared = self.sheet.declared(f"{control}:focus", "border")
                self.assertIsNotNone(declared, "needs its own :focus border")
                self.assertRegex(declared, r"^2px solid #[0-9a-f]{6}$")
                ring = self.sheet.border_colour(f"{control}:focus")
                resting = self.sheet.colour(control, "background-color")
                if control == "PartitionLabelsView":
                    surfaces = [GROUND, PANEL]  # sits on the page; its plate is light on purpose
                else:
                    surfaces = [GROUND, PANEL, resting]
                for surface in surfaces:
                    self.assertGreaterEqual(contrast(ring, surface), NON_TEXT_AA, f"{ring} on {surface}")
        # Every :focus rule in the sheet draws a ring; none merely recolours text.
        for selector, declarations in self.sheet.rules:
            if ":focus" in selector:
                self.assertTrue("border" in declarations, selector)
        # The filled primary button changes its fill by 3:1 as well as its ring.
        resting = self.sheet.colour("QPushButton#view-button-next", "background-color")
        focused = self.sheet.colour("QPushButton#view-button-next:focus", "background-color")
        self.assertGreaterEqual(contrast(resting, focused), NON_TEXT_AA)
        # Qt's dotted item focus rectangle is switched off only where a ring replaces it.
        self.assertEqual(self.sheet.declared("QAbstractItemView", "outline"), "0")

    def test_control_outlines_and_state_marks_meet_three_to_one(self):
        for control in ("QPushButton", "QToolButton", "QLineEdit", "QTextEdit", "QPlainTextEdit",
                        "QAbstractSpinBox", "QComboBox", "QAbstractItemView", "QProgressBar", "QMenu",
                        "QToolTip", "QPushButton#aboutButton", "QComboBox QAbstractItemView"):
            with self.subTest(control=control):
                edge = self.sheet.border_colour(control)
                self.assertIsNotNone(edge)
                for surface in (GROUND, PANEL):
                    self.assertGreaterEqual(contrast(edge, surface), NON_TEXT_AA, f"{edge} on {surface}")
        for indicator in ("QCheckBox::indicator", "QRadioButton::indicator"):
            with self.subTest(indicator=indicator):
                self.assertIsNotNone(self.sheet.declared(indicator, "width"))
                self.assertIsNotNone(self.sheet.declared(indicator, "height"))
                off_edge = self.sheet.border_colour(indicator)
                off_fill = self.sheet.declared(indicator, "background-color")
                on_edge = self.sheet.border_colour(f"{indicator}:checked")
                for surface in (GROUND, PANEL, off_fill):
                    self.assertGreaterEqual(contrast(off_edge, surface), NON_TEXT_AA)
                    self.assertGreaterEqual(contrast(on_edge, surface), NON_TEXT_AA)
                # Checked differs from unchecked in shape, not only in hue.
                self.assertNotEqual(self.sheet.declared(indicator, "border"),
                                    self.sheet.declared(f"{indicator}:checked", "border"))
        # A ticked box is an accent fill with a dark tick; it must also work if the image is missing.
        fill = self.sheet.declared("QCheckBox::indicator:checked", "background-color")
        self.assertGreaterEqual(contrast(fill, self.sheet.declared("QCheckBox::indicator", "background-color")),
                                NON_TEXT_AA)
        self.assertGreaterEqual(contrast(self.sheet.declared("QScrollBar::handle:vertical", "background-color"),
                                         self.sheet.declared("QScrollBar:vertical", "background-color")), NON_TEXT_AA)
        self.assertGreaterEqual(contrast(self.sheet.declared("QProgressBar::chunk", "background-color"),
                                         self.sheet.declared("QProgressBar", "background-color")), NON_TEXT_AA)

    def test_every_widget_calamares_shows_is_styled(self):
        selectors = {selector for selector, _ in self.sheet.rules}
        for needed in (
            "QWidget", "QLabel", "QToolTip", "QPushButton", "QPushButton:hover", "QPushButton:pressed",
            "QPushButton:disabled", "QPushButton:default", "QPushButton#view-button-next", "QToolButton",
            "QLineEdit", "QLineEdit:disabled", "QLineEdit:read-only", "QPlainTextEdit",
            "QAbstractSpinBox", "QAbstractSpinBox::up-button", "QAbstractSpinBox::down-button",
            "QAbstractSpinBox::up-arrow", "QAbstractSpinBox::down-arrow",
            "QComboBox", "QComboBox:disabled", "QComboBox::drop-down", "QComboBox::down-arrow",
            "QComboBox QAbstractItemView",
            "QCheckBox", "QCheckBox:disabled", "QCheckBox::indicator", "QCheckBox::indicator:checked",
            "QCheckBox::indicator:disabled", "QCheckBox::indicator:checked:disabled",
            "QRadioButton", "QRadioButton::indicator", "QRadioButton::indicator:checked",
            "QRadioButton::indicator:disabled",
            "QAbstractItemView", "QAbstractItemView:disabled", "QAbstractItemView::item:selected",
            "QAbstractItemView::indicator", "QHeaderView::section", "QTableCornerButton::section",
            "QGroupBox", "QGroupBox::title",
            "QScrollArea", "QScrollBar:vertical", "QScrollBar:horizontal", "QScrollBar::handle:vertical",
            "QScrollBar::handle:horizontal", "QAbstractScrollArea::corner",
            "QMenu", "QMenu::item:selected", "QProgressBar", "QProgressBar::chunk",
            "PartitionBarsView", "PartitionLabelsView",
            "#labelUsernameError", "#labelUserPasswordError", "#summaryItemTitle", "#summaryItemBody",
            "#sidebarApp", "#mainApp",
        ):
            self.assertIn(needed, selectors)

    def test_tree_expand_arrows_are_not_styled_away(self):
        # A ::branch rule without images makes Qt stop drawing the arrows.
        self.assertFalse([selector for selector, _ in self.sheet.rules if "::branch" in selector])

    def test_automatic_login_stays_unavailable(self):
        block = {name: value for selector, declarations in self.sheet.rules
                 if selector == "QCheckBox#checkBoxDoAutoLogin" for name, value in declarations.items()}
        self.assertEqual(block["qproperty-enabled"], "false")
        self.assertIn("unavailable", block["qproperty-text"])
        # Nothing else rewrites Calamares's translated wording or state.
        others = [(selector, name) for selector, declarations in self.sheet.rules for name in declarations
                  if name.startswith("qproperty-") and selector != "QCheckBox#checkBoxDoAutoLogin"]
        self.assertEqual(others, [])

    def test_images_are_local_png_at_the_installed_path(self):
        destination = "usr/share/calamares/branding/desos"
        self.assertIn(f'"{destination}"', (INSTALLER / "install_into_rootfs.py").read_text())
        urls = re.findall(r"url\(([^)]*)\)", self.sheet.text)
        self.assertGreater(len(urls), 6)
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(url.startswith(f"/{destination}/"), "style sheet paths must be absolute")
                relative = url[len(destination) + 2:]
                self.assertTrue(relative.endswith(".png"))
                source = BRANDING / relative
                self.assertEqual(source.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
                self.assertTrue(source.with_name(source.stem + "@2x.png").is_file())
        self.assertNotRegex(self.sheet.text, r"https?:|qrc:|@import")

    def test_partition_names_painted_in_fixed_black_stay_legible(self):
        # Calamares draws partition names with Qt::black and sizes with
        # Qt::gray regardless of palette, so the view needs a light plate.
        plate = self.sheet.declared("PartitionLabelsView", "background-color")
        self.assertGreaterEqual(contrast("#000000", plate), 7.0)
        # No plate can also carry the grey size line (#a0a0a4) at 4.5:1; the
        # design document must say so rather than leave it undiscovered.
        self.assertLess(contrast("#a0a0a4", plate), TEXT_AA)
        self.assertIn("PartitionLabelsView", (INSTALLER / "DESIGN.md").read_text())

    @unittest.skipUnless(CALAMARES_FILES.is_dir(), "extracted Calamares 3.3.14 package not present")
    def test_named_widgets_exist_in_the_real_calamares_3_3_14(self):
        names = set()
        for selector, _ in self.sheet.rules:
            names.update(re.findall(r"#([A-Za-z][\w-]*)", selector))
            names.update(part for part in re.findall(r"\b([A-Z]\w+)", selector) if not part.startswith("Q"))
        binaries = b"".join(path.read_bytes() for path in sorted(CALAMARES_FILES.rglob("*"))
                            if path.is_file() and (path.suffix == ".so" or path.name == "calamares"))
        self.assertGreater(len(names), 15)
        for name in sorted(names):
            self.assertIn(name.encode(), binaries, f"{name} is not a Calamares 3.3.14 object or class name")


class QmlTests(unittest.TestCase):
    FILES = ("show.qml", "calamares-sidebar.qml")
    TEXT_ROLES = ("text", "secondary", "accent")
    SURFACES = ("ground", "panel", "track", "tint")

    def setUp(self):
        self.source = {name: (BRANDING / name).read_text() for name in self.FILES}
        self.code = {name: strip_qml(text) for name, text in self.source.items()}

    def test_brackets_balance_and_one_root_object(self):
        pairs = {")": "(", "]": "[", "}": "{"}
        for name, (code, _) in self.code.items():
            with self.subTest(name=name):
                stack = []
                for char in code:
                    if char in "([{":
                        stack.append(char)
                    elif char in pairs:
                        self.assertTrue(stack and stack.pop() == pairs[char], "mismatched bracket")
                self.assertEqual(stack, [])
                body = "\n".join(line for line in code.splitlines() if not line.startswith("import"))
                self.assertRegex(body.strip(), r"^Rectangle \{")
                depth, roots = 0, 0
                for char in body:
                    if char == "{":
                        roots += depth == 0
                        depth += 1
                    elif char == "}":
                        depth -= 1
                self.assertEqual(roots, 1)
                self.assertNotIn("\t", self.source[name])

    def test_imports_stay_inside_what_the_image_ships(self):
        imports = {name: re.findall(r"^import (.+)$", code, flags=re.MULTILINE) for name, (code, _) in self.code.items()}
        self.assertEqual(imports["show.qml"], ["QtQuick 2.15"])
        # Exactly the modules the sidebar embedded in the 3.3.14 binary imports.
        self.assertEqual(imports["calamares-sidebar.qml"],
                         ["io.calamares.ui 1.0", "io.calamares.core 1.0", "QtQuick 2.15"])

    def test_nothing_remote_no_images_and_no_busy_motion(self):
        for name, (code, strings) in self.code.items():
            with self.subTest(name=name):
                for forbidden in ("Image", "AnimatedImage", "Loader", "XMLHttpRequest", "WebSocket", "openUrlExternally",
                                  "FontLoader", "Canvas", "ShaderEffect", "ParticleSystem", "Animation.Infinite",
                                  "RotationAnimation", "PathAnimation"):
                    self.assertNotRegex(code, rf"\b{re.escape(forbidden)}\b")
                for string in strings:
                    self.assertNotRegex(string, r"://|\.svg|\.png|www\.")
                for interval in re.findall(r"interval:\s*(\d+)", code):
                    self.assertGreaterEqual(int(interval), 10000, "timers must be slow")
                for duration in re.findall(r"duration:\s*(\d+)", code):
                    self.assertLessEqual(int(duration), 300, "motion must be brief")
                self.assertNotRegex(code, r"running:\s*true")
                self.assertNotRegex(code, r"\bBehavior\b|loops:")
        self.assertNotIn("Timer", self.code["calamares-sidebar.qml"][0])
        self.assertEqual(self.code["show.qml"][0].count("Timer {"), 1)

    def test_palette_matches_the_design_tokens_and_passes_contrast(self):
        for name, text in self.source.items():
            with self.subTest(name=name):
                palette, block = qml_palette(text)
                self.assertTrue(set(palette.values()) <= TOKENS)
                # Colour literals live only in the palette object.
                self.assertEqual(HEX.findall(text.replace(block, "")), [])
                for role in self.TEXT_ROLES:
                    for surface in self.SURFACES:
                        if surface in palette:
                            ratio = contrast(palette[role], palette[surface])
                            self.assertGreaterEqual(ratio, TEXT_AA, f"{role} on {surface}: {ratio:.2f}")
                for outline in ("edge", "accent"):
                    for surface in ("ground", "panel"):
                        self.assertGreaterEqual(contrast(palette[outline], palette[surface]), NON_TEXT_AA)
                code, _ = self.code[name]
                # Every colour in use is a palette role, and text uses only text roles.
                used = set(re.findall(r"pal\.(\w+)", code))
                self.assertTrue(used <= set(palette), used - set(palette))
                for element in re.findall(r"\bText \{[^{}]*\}", code):
                    roles = set(re.findall(r"pal\.(\w+)", element))
                    self.assertTrue(roles, "Text without an explicit palette colour")
                    self.assertTrue(roles <= {*self.TEXT_ROLES, "label"}, roles)
                self.assertNotRegex(code, r'color:\s*""')
        rail, _ = qml_palette(self.source["calamares-sidebar.qml"])
        # "label" marks steps still to do; it is only ever drawn on the rail's panel.
        self.assertGreaterEqual(contrast(rail["label"], rail["panel"]), TEXT_AA)
        self.assertRegex(self.code["calamares-sidebar.qml"][0], r"Rectangle \{\s*id: rail\s*color: pal\.panel")
        self.assertNotIn("label", qml_palette(self.source["show.qml"])[0])

    def test_text_is_readable_and_names_its_fonts(self):
        for name, (code, _) in self.code.items():
            with self.subTest(name=name):
                for element in re.findall(r"\bText \{[^{}]*\}", code):
                    self.assertIn("font.family:", element)
                    self.assertIn("font.pixelSize:", element)
                    size = re.search(r"font\.pixelSize:\s*(?:Math\.round\()?(\d+)", element)
                    self.assertGreaterEqual(int(size.group(1)), 10)
                    if int(size.group(1)) < 12:
                        # The smallest size is reserved for short upper-case mono labels.
                        self.assertIn("font.letterSpacing", element)
        for family in set(re.findall(r'font\.family: "([^"]+)"', "".join(self.source.values()))):
            self.assertIn(family, ("Chakra Petch", "JetBrains Mono"))
        fonts = {path.name for path in (INSTALLER / "fonts").iterdir()}
        self.assertTrue({"ChakraPetch-Medium.ttf", "ChakraPetch-SemiBold.ttf", "JetBrainsMono.ttf"} <= fonts)

    def test_slideshow_follows_api_2_and_covers_what_was_asked(self):
        code, strings = self.code["show.qml"]
        self.assertRegex(code, r"function onActivate\(\)")
        self.assertRegex(code, r"function onLeave\(\)")
        self.assertRegex(code, r"property bool activatedInCalamares: false")
        self.assertRegex(code, r"function onLeave\(\) \{\s*advance\.stop\(\)")
        text = " ".join(strings)
        for topic in ("What you are installing", "Keys for the first boot", "Undo anything",
                      "Proves what it is", "Phone as the key", "When this finishes"):
            self.assertIn(topic, text)
        self.assertEqual(code.count("tag: qsTr"), 6)
        for needed in ("snapshot", "signed record", "pair", "lock", "unlock", "approve", "USB", "password"):
            self.assertIn(needed, text)
        # Plain description only: no promises the features have not made.
        for overreach in ("guarantee", "always", "never", "100%", "cannot be", "impossible", "unbreakable",
                          "instantly", "any change", "every change", "everything you"):
            self.assertNotIn(overreach, text.lower())
        self.assertIn("not a backup", text)
        self.assertIn("cannot catch everything", text)
        # Release numbers come from branding.desc, never from a second copy here.
        for name, (_, literals) in self.code.items():
            for literal in literals:
                self.assertNotRegex(literal.replace("26.04", ""), r"\d+\.\d+", f"{name}: {literal}")

    @unittest.skipUnless(DES_UI_README.is_file(), "DEs-UI README not present beside this repository")
    def test_slideshow_keys_are_the_ones_in_the_des_ui_readme(self):
        readme = DES_UI_README.read_text()
        section = readme[readme.index("## Keys"):]
        section = section[:section.index("\n## ", 5)]
        rows = [re.findall(r"<kbd>(.*?)</kbd>", line.split("|")[1]) for line in section.splitlines()
                if line.startswith("| <kbd>")]
        self.assertGreater(len(rows), 15)
        self.assertIn("<kbd>Super</kbd>", section)
        keys = re.findall(r'\{ key: "([^"]+)", what: qsTr\("([^"]+)"\) \}', self.source["show.qml"])
        self.assertGreaterEqual(len(keys), 8)
        for key, _ in keys:
            wanted = [part.strip() for part in key.split(" - ")]
            self.assertTrue(any(row[:len(wanted)] == wanted for row in rows), f"Super+{key} is not in the README")
        self.assertIn("Print", " ".join(self.code["show.qml"][1]))
        self.assertIn("<kbd>Print</kbd>", section)

    def test_rail_uses_only_the_confirmed_calamares_contract(self):
        code, strings = self.code["calamares-sidebar.qml"]
        self.assertIn("model: ViewManager", code)
        self.assertIn("ViewManager.currentStepIndex", code)
        self.assertRegex(code, r"\btext: display\b")
        self.assertEqual(set(re.findall(r"ViewManager\.(\w+)", code)), {"currentStepIndex"})
        self.assertEqual(set(re.findall(r"Branding\.(\w+)", code)), {"string", "ShortVersion"})
        # Branding and debug are optional at run time: guarded, with a fallback.
        self.assertRegex(code, r"try \{\s*var value = Branding\.string\(entry\)")
        self.assertEqual(set(re.findall(r"\bdebug\.(\w+)", code)), {"about", "toggle", "enabled"})
        self.assertEqual(code.count("debug."), code.count('typeof debug !== "" && debug'))
        # Done, current and to-do differ in shape and in words, not only colour.
        self.assertIn("border.width: step.done ? 0 : 2", code)
        self.assertIn("width: step.now ? 14 : 10", code)
        text = " ".join(strings)
        for needed in ("Step %1 of %2", ", current step", ", done", ", to do", "About",
                       "take out the USB", "Sign in"):
            self.assertIn(needed, text)
        # The one control in the rail is reachable and visible from the keyboard.
        self.assertIn("activeFocusOnTab: true", code)
        self.assertIn("Keys.onReturnPressed: rail.showAbout()", code)
        self.assertIn("border.color: activeFocus ? pal.accent : pal.edge", code)
        self.assertIn("Accessible.role: Accessible.Button", code)


class ArtworkTests(unittest.TestCase):
    def test_svg_sources_are_self_contained_outlines(self):
        desc = yaml.safe_load((BRANDING / "branding.desc").read_text())
        for name in ("welcome.svg", "icon.svg", "language.svg"):
            with self.subTest(name=name):
                text = (BRANDING / name).read_text()
                document = xml.dom.minidom.parseString(text)
                self.assertEqual(document.documentElement.tagName, "svg")
                for tag in ("text", "image", "script", "style", "use", "foreignObject", "filter"):
                    self.assertEqual(document.getElementsByTagName(tag), [], f"<{tag}> in {name}")
                self.assertNotRegex(text, r"href=|url\(|font-family")
                self.assertTrue({colour.lower() for colour in HEX.findall(text)} <= TOKENS)
                self.assertTrue(document.getElementsByTagName("title")[0].firstChild.data.strip())
        # The picture carries the release name; it must be regenerated with it.
        title = xml.dom.minidom.parse(str(BRANDING / "welcome.svg")).getElementsByTagName("title")[0].firstChild.data
        self.assertIn(desc["strings"]["versionedName"], title)

    def test_png_renderings_have_the_expected_shape(self):
        def size(path):
            data = path.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", path.name)
            return struct.unpack(">II", data[16:24])

        self.assertEqual(size(BRANDING / "welcome.png"), (1440, 440))
        self.assertEqual(size(BRANDING / "icon.png"), (256, 256))
        images = sorted(path for path in (BRANDING / "img").iterdir())
        self.assertGreaterEqual(len(images), 14)
        for path in images:
            if "@2x" not in path.name:
                width, height = size(path)
                self.assertEqual(size(path.with_name(path.stem + "@2x.png")), (width * 2, height * 2))

    def test_only_installable_assets_live_in_the_branding_directory(self):
        # install_into_rootfs.py copies this directory wholesale into the image.
        allowed = {".desc", ".qss", ".qml", ".svg", ".png"}
        for path in BRANDING.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertIn(path.suffix, allowed, path)
                self.assertFalse(path.is_symlink())
                self.assertLess(path.stat().st_size, 200_000, path)


class PageConfigurationTests(unittest.TestCase):
    def test_welcome_page_is_offline_and_its_language_icon_exists(self):
        welcome = yaml.safe_load((INSTALLER / "calamares/modules/welcome.conf").read_text())
        for key in ("showSupportUrl", "showKnownIssuesUrl", "showReleaseNotesUrl", "showDonateUrl"):
            self.assertFalse(welcome[key])
        self.assertEqual(welcome["geoip"], {"style": "none", "url": "", "selector": ""})
        self.assertTrue((BRANDING / welcome["languageIcon"]).is_file())
        self.assertTrue({"storage", "ram", "root"} <= set(welcome["requirements"]["required"]))
        self.assertNotIn("internet", welcome["requirements"]["check"])

    def test_finished_page_never_restarts_by_surprise(self):
        finished = yaml.safe_load((INSTALLER / "calamares/modules/finished.conf").read_text())
        self.assertEqual(finished["restartNowMode"], "user-unchecked")
        self.assertFalse(finished["notifyOnFinished"])

    def test_design_document_describes_what_ships(self):
        design = (INSTALLER / "DESIGN.md").read_text()
        for path in BRANDING.iterdir():
            if path.is_file():
                self.assertIn(path.name, design)
        for needed in ("sidebar: qml", "navigation: widget", "4.5:1", "3:1", "unverified"):
            self.assertIn(needed, design)


if __name__ == "__main__":
    unittest.main()
