/* DesOS step rail for Calamares 3.3.14 ("sidebar: qml" in branding.desc).
 *
 * Shows where you are, what is done and what is left. It never drives the
 * installer: Back, Next and Cancel are native widgets, so if this file fails
 * to load the rail is empty and installation still works.
 *
 * Contract, confirmed against the sidebar embedded in the 3.3.14 binary:
 *   ViewManager            list model; role "display" is the step name
 *   ViewManager.currentStepIndex
 *   Branding.string(entry) strings from branding.desc
 *   debug                  context object: about(), toggle(), enabled
 *
 * No Image elements: the live image cannot decode SVG outside Calamares's
 * own renderer. Colours come only from the palette below; the tests check
 * every text colour against every surface it can sit on.
 */
import io.calamares.ui 1.0
import io.calamares.core 1.0

import QtQuick 2.15

Rectangle {
    id: rail
    color: pal.panel
    anchors.fill: parent

    QtObject {
        id: pal
        // Surfaces
        readonly property color ground: "#06080b"
        readonly property color panel: "#0a0e12"
        readonly property color track: "#141b22"
        readonly property color tint: "#0f2a29"
        // Text
        readonly property color text: "#dce7ec"
        readonly property color secondary: "#8fa3b0"
        readonly property color accent: "#4fd8ce"
        // Text on panel only
        readonly property color label: "#6f8492"
        // Lines and outlines
        readonly property color strong: "#2a3742"
        readonly property color edge: "#5b7385"
        readonly property color done: "#29706b"
    }

    readonly property int current: ViewManager.currentStepIndex
    readonly property int total: steps.count
    readonly property int shown: Math.min(current + 1, total)
    readonly property bool finishing: total > 1 && current === total - 1
    readonly property int gutter: width < 150 ? 10 : 20
    readonly property real rowHeight: Math.max(24, Math.min(36,
        (height - header.height - footer.height - 12) / Math.max(1, total)))
    readonly property bool debugEnabled: (typeof debug !== "undefined" && debug) ? debug.enabled : false

    function brand(entry, fallback) {
        try {
            var value = Branding.string(entry)
            return value ? value : fallback
        } catch (error) {
            return fallback
        }
    }

    function showAbout() {
        if (typeof debug !== "undefined" && debug) {
            debug.about()
        }
    }

    function toggleDebug() {
        if (typeof debug !== "undefined" && debug) {
            debug.toggle()
        }
    }

    Item {
        id: header
        width: parent.width
        height: 78

        Row {
            x: rail.gutter
            y: 18
            Text {
                text: "Des"
                color: pal.text
                font.family: "Chakra Petch"
                font.pixelSize: 26
                font.weight: Font.DemiBold
            }
            Text {
                text: "OS"
                color: pal.accent
                font.family: "Chakra Petch"
                font.pixelSize: 26
                font.weight: Font.DemiBold
            }
        }
        Text {
            x: rail.gutter
            y: 54
            width: parent.width - rail.gutter - 8
            text: (qsTr("Installer") + "  " + rail.brand(Branding.ShortVersion, "")).toUpperCase()
            color: pal.secondary
            elide: Text.ElideRight
            font.family: "JetBrains Mono"
            font.pixelSize: 10
            font.letterSpacing: 1.2
        }
    }

    Column {
        id: stepColumn
        anchors.top: header.bottom
        anchors.topMargin: 4
        width: parent.width

        Repeater {
            id: steps
            model: ViewManager

            Item {
                id: step
                width: stepColumn.width
                height: rail.rowHeight

                readonly property bool done: index < rail.current
                readonly property bool now: index === rail.current
                readonly property real nodeCenter: rail.gutter + 7

                Accessible.role: Accessible.ListItem
                Accessible.name: display + (now ? qsTr(", current step") : (done ? qsTr(", done") : qsTr(", to do")))

                // Current step: a quiet tint, not an accent slab.
                Rectangle {
                    visible: step.now
                    x: 6
                    y: 2
                    width: parent.width - 12
                    height: parent.height - 4
                    radius: 6
                    color: pal.tint
                }
                // The line into this step and the line out of it.
                Rectangle {
                    visible: index > 0
                    x: step.nodeCenter - 1
                    y: 0
                    width: 2
                    height: parent.height / 2
                    color: index <= rail.current ? pal.done : pal.strong
                }
                Rectangle {
                    visible: index < rail.total - 1
                    x: step.nodeCenter - 1
                    y: parent.height / 2
                    width: 2
                    height: parent.height / 2
                    color: index < rail.current ? pal.done : pal.strong
                }
                // Done is a filled dot, current a ring with a centre, to do a hollow ring.
                Rectangle {
                    id: node
                    width: step.now ? 14 : 10
                    height: width
                    radius: width / 2
                    x: step.nodeCenter - width / 2
                    anchors.verticalCenter: parent.verticalCenter
                    color: step.done ? pal.accent : pal.ground
                    border.width: step.done ? 0 : 2
                    border.color: step.now ? pal.accent : pal.edge
                }
                Rectangle {
                    visible: step.now
                    width: 4
                    height: 4
                    radius: 2
                    anchors.centerIn: node
                    color: pal.accent
                }
                Text {
                    x: step.nodeCenter + 18
                    width: parent.width - x - 12
                    anchors.verticalCenter: parent.verticalCenter
                    text: display
                    elide: Text.ElideRight
                    color: step.now ? pal.text : (step.done ? pal.secondary : pal.label)
                    font.family: "Chakra Petch"
                    font.pixelSize: 14
                    font.weight: step.now ? Font.DemiBold : Font.Medium
                }
            }
        }
    }

    Column {
        id: footer
        anchors.bottom: parent.bottom
        width: parent.width

        // On the last step: what to do once the page says "All done".
        Item {
            visible: rail.finishing
            width: parent.width
            height: afterInstall.height + 18

            Rectangle {
                x: rail.gutter
                width: parent.width - 2 * rail.gutter
                height: 1
                color: pal.strong
            }
            Column {
                id: afterInstall
                x: rail.gutter
                y: 10
                width: parent.width - 2 * rail.gutter
                spacing: 6

                Text {
                    width: parent.width
                    text: qsTr("When it says All done").toUpperCase()
                    color: pal.accent
                    wrapMode: Text.WordWrap
                    font.family: "JetBrains Mono"
                    font.pixelSize: 10
                    font.letterSpacing: 1.2
                }
                Text {
                    width: parent.width
                    text: qsTr("Restart. When the screen goes dark, take out the USB stick.")
                    color: pal.text
                    wrapMode: Text.WordWrap
                    font.family: "Chakra Petch"
                    font.pixelSize: 12
                    font.weight: Font.Medium
                }
                Text {
                    width: parent.width
                    text: qsTr("DesOS starts from your disk. Sign in with the name and password you just chose.")
                    color: pal.secondary
                    wrapMode: Text.WordWrap
                    font.family: "Chakra Petch"
                    font.pixelSize: 12
                    font.weight: Font.Medium
                }
            }
        }

        // Position as words and as a bar, so it does not rest on colour alone.
        Item {
            width: parent.width
            height: 38

            Text {
                x: rail.gutter
                y: 6
                width: parent.width - rail.gutter - 8
                text: qsTr("Step %1 of %2").arg(rail.shown).arg(rail.total).toUpperCase()
                color: pal.secondary
                elide: Text.ElideRight
                font.family: "JetBrains Mono"
                font.pixelSize: 10
                font.letterSpacing: 1.2
            }
            Rectangle {
                x: rail.gutter
                y: 26
                width: parent.width - 2 * rail.gutter
                height: 2
                color: pal.strong

                Rectangle {
                    height: parent.height
                    width: rail.total > 0 ? parent.width * rail.shown / rail.total : 0
                    color: pal.accent
                }
            }
        }

        Item {
            width: parent.width
            height: 44

            Row {
                x: rail.gutter
                y: 6
                spacing: 8

                Rectangle {
                    id: aboutButton
                    width: aboutLabel.implicitWidth + 24
                    height: 28
                    radius: 6
                    color: aboutArea.containsMouse ? pal.track : pal.panel
                    border.width: activeFocus ? 2 : 1
                    border.color: activeFocus ? pal.accent : pal.edge
                    activeFocusOnTab: true

                    Accessible.role: Accessible.Button
                    Accessible.name: aboutLabel.text
                    Accessible.onPressAction: rail.showAbout()
                    Keys.onReturnPressed: rail.showAbout()
                    Keys.onEnterPressed: rail.showAbout()
                    Keys.onSpacePressed: rail.showAbout()

                    Text {
                        id: aboutLabel
                        anchors.centerIn: parent
                        text: qsTr("About")
                        color: pal.secondary
                        font.family: "Chakra Petch"
                        font.pixelSize: 12
                        font.weight: Font.Medium
                    }
                    MouseArea {
                        id: aboutArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            aboutButton.forceActiveFocus()
                            rail.showAbout()
                        }
                    }
                }

                // Only when Calamares was started with -d.
                Rectangle {
                    id: debugButton
                    visible: rail.debugEnabled
                    width: debugLabel.implicitWidth + 24
                    height: 28
                    radius: 6
                    color: debugArea.containsMouse ? pal.track : pal.panel
                    border.width: activeFocus ? 2 : 1
                    border.color: activeFocus ? pal.accent : pal.edge
                    activeFocusOnTab: true

                    Accessible.role: Accessible.Button
                    Accessible.name: debugLabel.text
                    Accessible.onPressAction: rail.toggleDebug()
                    Keys.onReturnPressed: rail.toggleDebug()
                    Keys.onEnterPressed: rail.toggleDebug()
                    Keys.onSpacePressed: rail.toggleDebug()

                    Text {
                        id: debugLabel
                        anchors.centerIn: parent
                        text: qsTr("Debug")
                        color: pal.secondary
                        font.family: "Chakra Petch"
                        font.pixelSize: 12
                        font.weight: Font.Medium
                    }
                    MouseArea {
                        id: debugArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            debugButton.forceActiveFocus()
                            rail.toggleDebug()
                        }
                    }
                }
            }
        }
    }
}
