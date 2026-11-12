/* DesOS install slideshow for Calamares 3.3.14 (slideshowAPI: 2).
 *
 * Six short cards to read while files copy. Calamares calls onActivate() when
 * installation starts and onLeave() when it ends, and sets
 * activatedInCalamares if the root object declares it.
 *
 * Motion is one 300 ms cross-fade per card. The only timer fires once every
 * sixteen seconds and stops when installation ends. Nothing is fetched, no
 * Image is loaded, and only QtQuick itself is imported. Colours come only
 * from the palette below; the tests check every text colour against every
 * surface it can sit on.
 */
import QtQuick 2.15

Rectangle {
    id: show
    color: pal.ground
    clip: true

    QtObject {
        id: pal
        // Surfaces
        readonly property color ground: "#06080b"
        readonly property color panel: "#0a0e12"
        readonly property color track: "#141b22"
        // Text
        readonly property color text: "#dce7ec"
        readonly property color secondary: "#8fa3b0"
        readonly property color accent: "#4fd8ce"
        // Lines and outlines
        readonly property color strong: "#2a3742"
        readonly property color edge: "#5b7385"
    }

    property bool activatedInCalamares: false
    property int current: 0
    property int shown: 0

    readonly property real unit: Math.max(0.8, Math.min(1.25, Math.min(width / 760, height / 400)))
    readonly property int margin: Math.round(36 * unit)
    readonly property bool wide: width >= 600

    readonly property var slides: [
        {
            tag: qsTr("What you are installing"),
            title: qsTr("A desktop built around the terminal."),
            lead: qsTr("DesOS is the DEs-UI desktop on Linux and Ubuntu 26.04. The terminal sits in the centre, system readings sit around it, and applications open in the workspace beside it."),
            more: qsTr("The installer is copying it to your disk now. Keep the power connected and leave the USB stick in until it finishes."),
            keys: false
        },
        {
            tag: qsTr("Keys for the first boot"),
            title: qsTr("Hold Super, then press:"),
            lead: "",
            more: qsTr("Without Super: Print takes a screenshot or starts a recording."),
            keys: true
        },
        {
            tag: qsTr("Undo anything"),
            title: qsTr("System changes can be taken back."),
            lead: qsTr("With the default Btrfs disk layout, DesOS takes a snapshot of the system before a system change. If the change turns out to be wrong, you can return to the snapshot."),
            more: qsTr("This covers the system itself. It is not a backup of your own files, so keep copies of what matters."),
            keys: false
        },
        {
            tag: qsTr("Proves what it is"),
            title: qsTr("The system can check itself."),
            lead: qsTr("Every DesOS build has a signed record of what it contains. The installed system checks itself against that record."),
            more: qsTr("It tells you when the system no longer matches that record. It cannot catch everything: someone with full control of the machine can get around it."),
            keys: false
        },
        {
            tag: qsTr("Phone as the key"),
            title: qsTr("A paired phone can stand in for you."),
            lead: qsTr("Once you pair a phone with this computer, the phone can lock it, unlock it, and approve requests that need you."),
            more: qsTr("Nothing is paired during installation."),
            keys: false
        },
        {
            tag: qsTr("When this finishes"),
            title: qsTr("Restart, then take out the USB."),
            lead: qsTr("Choose Restart now on the last page, or restart later from the power menu. When the screen goes dark, pull out the USB stick so the computer starts from its own disk."),
            more: qsTr("The first thing you will see is a clock, your name and a password field. Type the password you chose and press Enter. The terminal is waiting behind it."),
            keys: false
        }
    ]

    // From the "Keys" table in the DEs-UI README; the tests compare them.
    readonly property var keys: [
        { key: "Space", what: qsTr("Launcher: type to search, Enter to open") },
        { key: ",", what: qsTr("Settings") },
        { key: "L", what: qsTr("Lock the screen") },
        { key: "Tab", what: qsTr("Switch between the terminal and applications") },
        { key: "1 - 4", what: qsTr("Bring that workspace into view") },
        { key: "S", what: qsTr("Split the workspace between two windows") },
        { key: "W", what: qsTr("Overview of every workspace") },
        { key: "F", what: qsTr("Full screen") },
        { key: "Q", what: qsTr("Close the current application") },
        { key: "Esc", what: qsTr("Power menu") }
    ]

    readonly property var slide: slides[Math.max(0, Math.min(shown, slides.length - 1))]

    function pad(number) {
        return number < 10 ? "0" + number : "" + number
    }

    function go(to) {
        current = (to + slides.length) % slides.length
        if (advance.running) {
            advance.restart()
        }
    }

    function onActivate() {
        current = 0
        advance.restart()
    }

    function onLeave() {
        advance.stop()
    }

    onActivatedInCalamaresChanged: {
        if (activatedInCalamares) {
            advance.restart()
        } else {
            advance.stop()
        }
    }

    onCurrentChanged: swap.restart()

    Timer {
        id: advance
        interval: 16000
        repeat: true
        running: false
        onTriggered: show.current = (show.current + 1) % show.slides.length
    }

    SequentialAnimation {
        id: swap
        NumberAnimation { target: card; property: "opacity"; to: 0; duration: 120 }
        ScriptAction { script: show.shown = show.current }
        NumberAnimation { target: card; property: "opacity"; to: 1; duration: 180 }
    }

    focus: true
    Keys.onRightPressed: show.go(show.current + 1)
    Keys.onSpacePressed: show.go(show.current + 1)
    Keys.onLeftPressed: show.go(show.current - 1)

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        onClicked: function (mouse) {
            show.go(show.current + (mouse.button === Qt.RightButton ? -1 : 1))
        }
    }

    // Shrinks as a whole if a small window cannot hold the card at full size.
    Column {
        id: card
        x: show.margin
        y: show.margin
        width: Math.min(show.width - 2 * show.margin, Math.round(660 * show.unit))
        spacing: Math.round(12 * show.unit)
        transformOrigin: Item.TopLeft
        scale: Math.max(0.5, Math.min(1, (show.height - 2 * show.margin - 28) / Math.max(1, height)))

        Accessible.role: Accessible.StaticText
        Accessible.name: show.slide.title + " " + show.slide.lead + " " + show.slide.more

        Text {
            width: parent.width
            text: (show.pad(show.shown + 1) + " / " + show.pad(show.slides.length) + "   " + show.slide.tag).toUpperCase()
            color: pal.accent
            elide: Text.ElideRight
            font.family: "JetBrains Mono"
            font.pixelSize: Math.round(11 * show.unit)
            font.letterSpacing: 1.4
        }
        Text {
            width: parent.width
            text: show.slide.title
            color: pal.text
            wrapMode: Text.WordWrap
            font.family: "Chakra Petch"
            font.pixelSize: Math.round(28 * show.unit)
            font.weight: Font.DemiBold
        }
        Rectangle {
            width: Math.round(48 * show.unit)
            height: 3
            color: pal.accent
        }
        Text {
            visible: show.slide.lead !== ""
            width: parent.width
            text: show.slide.lead
            color: pal.text
            wrapMode: Text.WordWrap
            lineHeight: 1.3
            font.family: "Chakra Petch"
            font.pixelSize: Math.round(17 * show.unit)
            font.weight: Font.Medium
        }
        Grid {
            id: keyGrid
            visible: show.slide.keys
            width: parent.width
            columns: show.wide ? 2 : 1
            columnSpacing: Math.round(24 * show.unit)
            rowSpacing: Math.round(8 * show.unit)

            readonly property real cell: (width - (columns - 1) * columnSpacing) / columns
            readonly property real capWidth: Math.round(58 * show.unit)

            Repeater {
                model: show.keys

                Item {
                    width: keyGrid.cell
                    height: Math.max(cap.height, meaning.height)

                    Rectangle {
                        id: cap
                        width: keyGrid.capWidth
                        height: Math.round(26 * show.unit)
                        radius: 4
                        color: pal.track
                        border.width: 1
                        border.color: pal.edge

                        Text {
                            anchors.centerIn: parent
                            text: modelData.key
                            color: pal.text
                            font.family: "JetBrains Mono"
                            font.pixelSize: Math.round(12 * show.unit)
                        }
                    }
                    Text {
                        id: meaning
                        x: keyGrid.capWidth + Math.round(12 * show.unit)
                        y: Math.max(0, (cap.height - height) / 2)
                        width: parent.width - x
                        text: modelData.what
                        color: pal.text
                        wrapMode: Text.WordWrap
                        font.family: "Chakra Petch"
                        font.pixelSize: Math.round(14 * show.unit)
                        font.weight: Font.Medium
                    }
                }
            }
        }
        Text {
            width: parent.width
            text: show.slide.more
            color: pal.secondary
            wrapMode: Text.WordWrap
            lineHeight: 1.3
            font.family: "Chakra Petch"
            font.pixelSize: Math.round(15 * show.unit)
            font.weight: Font.Medium
        }
    }

    // Position: the current card's mark is wider as well as brighter.
    Row {
        id: marks
        x: show.margin
        y: show.height - Math.round(show.margin * 0.6) - height
        spacing: 6

        Repeater {
            model: show.slides.length

            Item {
                width: mark.width
                height: 18

                Rectangle {
                    id: mark
                    width: index === show.current ? 28 : 14
                    height: 3
                    anchors.verticalCenter: parent.verticalCenter
                    color: index === show.current ? pal.accent : pal.edge
                }
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: show.go(index)
                }
            }
        }
    }
    Text {
        visible: show.wide
        anchors.right: parent.right
        anchors.rightMargin: show.margin
        anchors.verticalCenter: marks.verticalCenter
        text: qsTr("Click for the next card").toUpperCase()
        color: pal.secondary
        font.family: "JetBrains Mono"
        font.pixelSize: 10
        font.letterSpacing: 1.2
    }
}
