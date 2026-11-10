import QtQuick 2.0

Rectangle {
    color: "#06080b"
    function onActivate() {}
    function onLeave() {}
    Column {
        anchors.centerIn: parent
        width: Math.max(240, parent.width - 64)
        spacing: 20
        Text {
            text: qsTr("Make room for your work.")
            color: "#dce7ec"
            width: parent.width
            wrapMode: Text.WordWrap
            font.family: "Chakra Petch"
            font.pixelSize: 32
            font.weight: Font.DemiBold
        }
        Rectangle { width: 56; height: 3; color: "#4fd8ce" }
        Text {
            text: qsTr("DesOS is installing the DEs-UI desktop and your chosen settings. Keep this computer connected to power until installation finishes.")
            width: parent.width
            wrapMode: Text.WordWrap
            color: "#dce7ec"
            font.family: "Chakra Petch"
            font.pixelSize: 18
            lineHeight: 1.3
        }
        Text {
            text: qsTr("After restarting, remove the live USB and sign in with the account you created.")
            width: parent.width
            wrapMode: Text.WordWrap
            color: "#8fa3b0"
            font.family: "Chakra Petch"
            font.pixelSize: 16
            lineHeight: 1.3
        }
        Text {
            text: qsTr("DesOS 0.1 Preview · Linux · Ubuntu 26.04 · Calamares")
            width: parent.width
            wrapMode: Text.WordWrap
            color: "#8fa3b0"
            font.family: "JetBrains Mono"
            font.pixelSize: 11
        }
    }
}
