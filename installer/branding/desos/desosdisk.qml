/* DesOS disk page (the "desosdisk" notesqml instance; design: the installer
 * flow canvas, "Disk, step 2").
 *
 * Reads what the launcher found before Calamares started
 * (/run/desos-installer/disks.json: disks, memory, firmware; see
 * modules/desosdisk/probe.py) and writes the user's choice to
 * /run/desos-installer/choice.json:
 *   { mode: "erase", disk: "/dev/sda", filesystem: "btrfs"|"ext4",
 *     swap: "none"|"small"|"suspend", homeBytes: <bytes, 0 = none> }
 * The desosdisk job turns that into partitions with plan.py; the numbers drawn
 * here use the same rules (EFI_MIB, SMALL_SWAP_GIB, MIN_ROOT_GIB, MIN_HOME_GIB
 * below; tests/test_desosdisk.py checks they match plan.py). Nothing is written
 * until Install on the summary page.
 *
 * Palette as calamares-sidebar.qml; no Image elements.
 */
import QtQuick 2.15

Rectangle {
    id: page
    color: pal.ground

    QtObject {
        id: pal
        readonly property color ground: "#06080b"
        readonly property color panel: "#0a0e12"
        readonly property color track: "#141b22"
        readonly property color line: "#1a232b"
        readonly property color strong: "#2a3742"
        readonly property color edge: "#5b7385"
        readonly property color tint: "#0f2a29"
        readonly property color chunk: "#29706b"
        readonly property color text: "#dce7ec"
        readonly property color secondary: "#8fa3b0"
        readonly property color accent: "#4fd8ce"
        readonly property color caution: "#f5b85a"
    }

    readonly property int efiMib: 512
    readonly property int smallSwapGib: 4
    readonly property int minRootGib: 16
    readonly property int minHomeGib: 20

    property var disks: []
    property real memory: 0
    property string firmware: "efi"
    property int picked: -1
    property string filesystem: "btrfs"
    property string swap: "none"
    property bool separateHome: false
    property int homeGib: 0

    readonly property real gib: 1024 * 1024 * 1024
    readonly property var disk: picked >= 0 && picked < disks.length ? disks[picked] : null
    readonly property real swapBytes: swap === "none" ? 0
        : swap === "small" ? Math.min(smallSwapGib * gib, Math.ceil(memory / gib) * gib)
        : Math.ceil(memory / gib) * gib
    readonly property real fixedBytes: (firmware === "bios" ? 1024 * 1024 : efiMib * 1024 * 1024) + swapBytes
    // plan.usable(): 1 MiB at the front, the backup GPT (33 sectors) at the back.
    readonly property real spaceBytes: disk ? Math.floor((disk.size - 33 * disk.sector) / 1048576) * 1048576 - 1048576 : 0
    readonly property int largestHomeGib: disk ? Math.max(0, Math.floor((spaceBytes - fixedBytes - minRootGib * gib) / gib)) : 0
    readonly property bool homeFits: largestHomeGib >= minHomeGib
    readonly property real homeBytes: separateHome && homeFits ? Math.min(Math.max(homeGib, minHomeGib), largestHomeGib) * gib : 0
    readonly property real rootBytes: spaceBytes - fixedBytes - homeBytes
    readonly property bool fits: disk !== null && disk.usable && rootBytes >= minRootGib * gib

    function gb(bytes) { return (bytes / 1e9).toFixed(bytes >= 100e9 ? 0 : 1) + " GB" }

    // Calamares 3.3.14 lets QML read global storage but not write it, so the
    // choice goes to a local file the desosdisk job reads (the launcher removes
    // any old one at start and sets QML_XHR_ALLOW_FILE_WRITE).
    // Asynchronous: Qt sends the body of a file PUT only from its event loop
    // (a synchronous PUT made the file and left it empty in the first VM run).
    // The request is kept in a property so it is not collected mid-write.
    property var writing: null
    function save() {
        var request = new XMLHttpRequest()
        request.onreadystatechange = function() {
            if (request.readyState === XMLHttpRequest.DONE)
                console.log("desosdisk: choice saved, status " + request.status)
        }
        request.open("PUT", "file:///run/desos-installer/choice.json")
        request.send(JSON.stringify({
            "mode": "erase",
            "disk": fits ? disk.path : "",
            "filesystem": filesystem,
            "swap": swap,
            "homeBytes": homeBytes
        }))
        writing = request
    }

    // The launcher's probe: a local, root-owned file (QML_XHR_ALLOW_FILE_READ).
    function readProbe() {
        var request = new XMLHttpRequest()
        request.open("GET", "file:///run/desos-installer/disks.json", false)
        request.send(null)
        try {
            var found = JSON.parse(request.responseText)
            disks = found.disks || []
            memory = found.memory || 0
            firmware = found.firmware || "efi"
        } catch (error) {
            disks = []
        }
    }

    function onActivate() {
        if (disks.length === 0) readProbe()
        if (picked < 0) {
            for (var i = 0; i < disks.length; ++i) {
                if (disks[i].usable) { picked = i; break }
            }
        }
        if (homeGib === 0) homeGib = Math.max(minHomeGib, Math.floor(largestHomeGib * 0.6))
        save()
    }

    function onLeave() { save() }

    onFilesystemChanged: save()
    onSwapChanged: save()
    onHomeBytesChanged: save()
    onPickedChanged: save()

    // One choice in a row of two or three: a pill that fills with the accent.
    component Pill: Rectangle {
        id: pill
        property string label
        property bool on
        signal chosen
        height: 36
        radius: 6
        color: on ? pal.tint : (hover.containsMouse ? pal.track : pal.ground)
        border.width: activeFocus || on ? 2 : 0
        border.color: activeFocus ? pal.text : pal.accent
        activeFocusOnTab: true
        Accessible.role: Accessible.RadioButton
        Accessible.name: label
        Accessible.checked: on
        Keys.onSpacePressed: chosen()
        Keys.onReturnPressed: chosen()
        Text {
            anchors.centerIn: parent
            text: pill.label
            color: pill.on ? pal.accent : pal.text
            font.family: "Chakra Petch"
            font.pixelSize: 12
            font.weight: Font.DemiBold
            font.letterSpacing: 0.8
        }
        MouseArea { id: hover; anchors.fill: parent; hoverEnabled: true; onClicked: pill.chosen() }
    }

    component Label: Text {
        color: pal.secondary
        font.family: "JetBrains Mono"
        font.pixelSize: 11
        font.letterSpacing: 1.3
    }

    component Note: Text {
        color: pal.secondary
        font.family: "Chakra Petch"
        font.pixelSize: 12
        wrapMode: Text.WordWrap
        lineHeight: 1.3
    }

    Flickable {
        anchors.fill: parent
        anchors.margins: 32
        contentHeight: column.height
        clip: true

        Column {
            id: column
            width: parent.width
            spacing: 16

            Text {
                text: "DesOS sets up the disk for you"
                color: pal.text
                font.family: "Chakra Petch"
                font.pixelSize: 28
                font.weight: Font.DemiBold
            }
            Note {
                width: parent.width
                text: "Pick a disk. DesOS makes every partition on it, the EFI partition included. Nothing is written until you press Install on the summary page."
                font.pixelSize: 14
            }

            // The disks.
            Column {
                width: parent.width
                spacing: 8
                Repeater {
                    model: page.disks
                    delegate: Rectangle {
                        width: column.width
                        height: 58
                        radius: 12
                        color: pal.panel
                        border.color: index === page.picked ? pal.accent : pal.line
                        border.width: index === page.picked ? 2 : 1
                        opacity: modelData.usable ? 1 : 0.55
                        activeFocusOnTab: modelData.usable
                        Accessible.role: Accessible.RadioButton
                        Accessible.name: modelData.line
                        Accessible.checked: index === page.picked
                        Keys.onSpacePressed: if (modelData.usable) page.picked = index
                        Column {
                            anchors.verticalCenter: parent.verticalCenter
                            x: 16
                            spacing: 3
                            Text {
                                text: (modelData.path || "").replace("/dev/", "") + " · " + page.gb(modelData.size)
                                color: pal.text
                                font.family: "Chakra Petch"
                                font.pixelSize: 15
                                font.weight: Font.DemiBold
                            }
                            Label { text: modelData.line.toUpperCase() }
                        }
                        MouseArea { anchors.fill: parent; enabled: modelData.usable; onClicked: page.picked = index }
                    }
                }
                Note {
                    visible: page.disks.length === 0
                    text: "No disk was found that DesOS can go on."
                    color: pal.caution
                }
            }

            // The three choices.
            Row {
                width: parent.width
                spacing: 10
                visible: page.disk !== null
                readonly property real cell: (width - 2 * spacing) / 3

                Rectangle {
                    width: parent.cell; height: 150; radius: 12; color: pal.panel; border.color: pal.line
                    Column {
                        anchors.fill: parent; anchors.margins: 12; spacing: 8
                        Label { text: "FILE SYSTEM" }
                        Row {
                            width: parent.width; spacing: 4
                            Pill { width: (parent.width - 4) / 2; label: "BTRFS · UNDO"; on: page.filesystem === "btrfs"; onChosen: page.filesystem = "btrfs" }
                            Pill { width: (parent.width - 4) / 2; label: "EXT4"; on: page.filesystem === "ext4"; onChosen: page.filesystem = "ext4" }
                        }
                        Note {
                            width: parent.width
                            text: page.filesystem === "btrfs" ? "Every update can be undone from the boot menu. Recommended." : "Simpler and familiar. No undo snapshots."
                        }
                    }
                }

                Rectangle {
                    width: parent.cell; height: 150; radius: 12; color: pal.panel; border.color: pal.line
                    Column {
                        anchors.fill: parent; anchors.margins: 12; spacing: 8
                        Label { text: "SWAP" }
                        Row {
                            width: parent.width; spacing: 4
                            Pill { width: (parent.width - 8) / 3; label: "NONE"; on: page.swap === "none"; onChosen: page.swap = "none" }
                            Pill { width: (parent.width - 8) / 3; label: "SWAP"; on: page.swap === "small"; onChosen: page.swap = "small" }
                            Pill { width: (parent.width - 8) / 3; label: "HIBERNATE"; on: page.swap === "suspend"; onChosen: page.swap = "suspend" }
                        }
                        Note {
                            width: parent.width
                            text: page.swap === "none" ? "Compressed RAM swap is always on. Enough for most."
                                : page.swap === "small" ? page.gb(page.swapBytes) + " on the disk as well, for heavy work."
                                : "As big as your RAM (" + page.gb(page.swapBytes) + "), so the computer can hibernate and resume."
                        }
                    }
                }

                Rectangle {
                    width: parent.cell; height: 150; radius: 12; color: pal.panel; border.color: pal.line
                    Column {
                        anchors.fill: parent; anchors.margins: 12; spacing: 8
                        Label { text: "YOUR FILES · /HOME" }
                        Row {
                            spacing: 10
                            Rectangle {
                                id: box
                                width: 18; height: 18; radius: 4
                                color: page.separateHome ? pal.tint : pal.panel
                                border.color: activeFocus ? pal.text : (page.separateHome ? pal.accent : pal.edge)
                                border.width: activeFocus ? 2 : 1
                                opacity: page.homeFits ? 1 : 0.5
                                activeFocusOnTab: page.homeFits
                                Accessible.role: Accessible.CheckBox
                                Accessible.name: "Separate /home partition"
                                Accessible.checked: page.separateHome
                                Keys.onSpacePressed: if (page.homeFits) page.separateHome = !page.separateHome
                                Text { anchors.centerIn: parent; text: "✓"; visible: page.separateHome; color: pal.accent; font.family: "Chakra Petch"; font.pixelSize: 12 }
                            }
                            Text { text: "Separate partition"; color: pal.text; font.family: "Chakra Petch"; font.pixelSize: 13 }
                        }
                        // The size, while a separate /home is chosen.
                        Item {
                            width: parent.width; height: 34
                            visible: page.separateHome && page.homeFits
                            Text { text: "Size"; color: pal.secondary; font.family: "Chakra Petch"; font.pixelSize: 12 }
                            Text { anchors.right: parent.right; text: page.gb(page.homeBytes); color: pal.text; font.family: "JetBrains Mono"; font.pixelSize: 12 }
                            Rectangle {
                                id: groove
                                y: 22; width: parent.width; height: 4; radius: 2; color: pal.track
                                readonly property real share: page.largestHomeGib > page.minHomeGib
                                    ? (page.homeBytes / page.gib - page.minHomeGib) / (page.largestHomeGib - page.minHomeGib) : 0
                                Rectangle { width: groove.width * groove.share; height: 4; radius: 2; color: pal.accent }
                                Rectangle {
                                    x: groove.width * groove.share - 8; y: -6; width: 16; height: 16; radius: 8
                                    color: pal.accent
                                    border.color: pal.text; border.width: groove.activeFocus ? 2 : 0
                                }
                                activeFocusOnTab: true
                                Accessible.role: Accessible.Slider
                                Accessible.name: "Size of the /home partition"
                                Keys.onLeftPressed: page.homeGib = Math.max(page.minHomeGib, Math.round(page.homeBytes / page.gib) - 5)
                                Keys.onRightPressed: page.homeGib = Math.min(page.largestHomeGib, Math.round(page.homeBytes / page.gib) + 5)
                                MouseArea {
                                    anchors.fill: parent; anchors.margins: -10
                                    function set(x) {
                                        var share = Math.max(0, Math.min(1, x / groove.width))
                                        page.homeGib = Math.round(page.minHomeGib + share * (page.largestHomeGib - page.minHomeGib))
                                    }
                                    onPressed: (mouse) => set(mouse.x - 10)
                                    onPositionChanged: (mouse) => set(mouse.x - 10)
                                }
                            }
                        }
                        Note {
                            width: parent.width
                            visible: !(page.separateHome && page.homeFits)
                            text: !page.homeFits ? "This disk is too small for a separate /home."
                                : page.filesystem === "btrfs" ? "Already kept apart: /home is its own subvolume and shares the free space. Undo never touches it."
                                : "Your files live with the system. Turn this on to keep them through a reinstall."
                        }
                    }
                    MouseArea {
                        x: 12; y: 30; width: 180; height: 24
                        enabled: page.homeFits
                        onClicked: page.separateHome = !page.separateHome
                    }
                }
            }

            // Before and after.
            Rectangle {
                width: parent.width; height: 92; radius: 12; color: pal.panel; border.color: pal.line
                visible: page.disk !== null
                Column {
                    anchors.fill: parent; anchors.margins: 12; spacing: 8
                    Row {
                        width: parent.width; spacing: 12
                        Label { width: 52; text: "NOW"; anchors.verticalCenter: parent.verticalCenter }
                        Rectangle {
                            width: parent.width - 64; height: 28; radius: 6; color: pal.track; clip: true
                            Text {
                                anchors.verticalCenter: parent.verticalCenter; x: 10
                                text: page.disk ? (page.disk.systems.length ? page.disk.systems.join(" and ").toUpperCase() + " · WILL BE ERASED"
                                    : page.disk.empty ? "EMPTY" : page.disk.partitions.length + " PARTITIONS · WILL BE ERASED") : ""
                                color: page.disk && !page.disk.empty ? pal.caution : pal.secondary
                                font.family: "JetBrains Mono"; font.pixelSize: 11; font.letterSpacing: 0.8
                            }
                        }
                    }
                    Row {
                        width: parent.width; spacing: 12
                        Label { width: 52; text: "AFTER"; anchors.verticalCenter: parent.verticalCenter }
                        Row {
                            id: bar
                            width: parent.width - 64; height: 28; spacing: 2
                            readonly property real room: width - 2 * 3 - 34
                            readonly property real total: page.swapBytes + page.rootBytes + page.homeBytes
                            Rectangle {
                                width: 34; height: 28; radius: 4; color: pal.strong
                                Text { anchors.centerIn: parent; text: page.firmware === "bios" ? "BIOS" : "EFI"; color: pal.text; font.family: "JetBrains Mono"; font.pixelSize: 10; font.letterSpacing: 0.8 }
                            }
                            Rectangle {
                                visible: page.swapBytes > 0
                                width: Math.max(96, bar.room * page.swapBytes / bar.total); height: 28; radius: 4; color: pal.track; clip: true
                                Text { anchors.verticalCenter: parent.verticalCenter; x: 8; text: "SWAP " + page.gb(page.swapBytes); color: pal.secondary; font.family: "JetBrains Mono"; font.pixelSize: 10; font.letterSpacing: 0.8 }
                            }
                            Rectangle {
                                width: Math.max(80, bar.room * page.rootBytes / bar.total); height: 28; radius: 4; color: pal.chunk; clip: true
                                Text { anchors.verticalCenter: parent.verticalCenter; x: 8; text: "DESOS · " + page.gb(page.rootBytes) + " · " + page.filesystem.toUpperCase(); color: pal.text; font.family: "JetBrains Mono"; font.pixelSize: 10; font.letterSpacing: 0.8 }
                            }
                            Rectangle {
                                visible: page.homeBytes > 0
                                width: Math.max(60, bar.room * page.homeBytes / bar.total); height: 28; radius: 4; color: pal.tint; border.color: pal.accent; clip: true
                                Text { anchors.verticalCenter: parent.verticalCenter; x: 8; text: "/HOME · " + page.gb(page.homeBytes); color: pal.text; font.family: "JetBrains Mono"; font.pixelSize: 10; font.letterSpacing: 0.8 }
                            }
                        }
                    }
                }
            }

            // The erase, in words, at the moment of choosing: Calamares's summary
            // page lists only its own pages' sections, so it says nothing of this.
            Note {
                width: parent.width
                visible: page.disk !== null && page.fits
                text: page.disk ? ("When you press Install, everything on " + page.disk.path.replace("/dev/", "")
                    + " (" + page.gb(page.disk.size) + (page.disk.systems.length ? ", " + page.disk.systems.join(" and ") : "")
                    + ") is erased. Nothing is written before that.") : ""
                color: pal.caution
                font.pixelSize: 14
            }
            Note {
                width: parent.width
                visible: page.disk !== null && !page.fits
                text: "These choices do not fit on this disk: DesOS needs at least " + page.minRootGib + " GiB. Choose less swap or a smaller /home."
                color: pal.caution
            }
            Note {
                width: parent.width
                text: "Prefer to partition by hand? Quit, and start “Install DesOS (manual partitions)” from the menu."
            }
        }
    }
}
