import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    title: "Mind Piano"
    width: 420
    height: 560
    visible: true
    color: palette.window

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        // ── Transport bar ───────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            // BPM
            Text {
                text: appState ? "♩ " + Math.round(appState.bpm) + " BPM" : ""
                font.pixelSize: 18
                font.bold: true
                color: palette.text
            }

            // Metronome indicator
            Rectangle {
                width: 14; height: 14; radius: 7
                color: appState && appState.metronomeOn ? "#4CAF50" : palette.mid
                border.color: palette.dark
                border.width: 1

                ToolTip.visible: metroMa.containsMouse
                ToolTip.text: "Metronome: " + (appState && appState.metronomeOn ? "ON" : "OFF")
                MouseArea { id: metroMa; anchors.fill: parent; hoverEnabled: true }
            }

            Item { Layout.fillWidth: true }

            // Playing / recording status
            Text {
                text: {
                    if (!appState) return ""
                    if (appState.recording) return "⏺ REC"
                    if (appState.playing) return "▶ PLAY"
                    return "⏹ STOP"
                }
                font.pixelSize: 14
                font.bold: true
                color: {
                    if (!appState) return palette.text
                    if (appState.recording) return "#F44336"
                    if (appState.playing) return "#4CAF50"
                    return palette.mid
                }
            }

            // Gain
            Text {
                text: appState ? "🔊 " + Math.round(appState.masterGain * 20) + "%" : ""
                font.pixelSize: 12
                color: palette.text
                opacity: 0.7
            }
        }

        // ── Playback phase bar ──────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            height: 6
            radius: 3
            color: palette.mid
            clip: true

            Rectangle {
                id: phaseBar
                height: parent.height
                radius: 3
                color: appState && appState.playing ? "#64B5F6" : palette.mid
                width: 0

                // Re-derive phase from loopEpoch + loopDuration
                Timer {
                    interval: 33  // ~30 fps
                    repeat: true
                    running: appState ? appState.playing : false
                    onTriggered: {
                        if (!appState || appState.loopDuration <= 0) {
                            phaseBar.width = 0
                            return
                        }
                        let elapsed = Date.now() - appState.loopEpoch
                        let phase = (elapsed / (appState.loopDuration * 1000)) % 1.0
                        if (phase < 0) phase = 0
                        phaseBar.width = phaseBar.parent.width * phase
                    }
                }
            }
        }

        // ── Separator ───────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            height: 1
            color: palette.mid
        }

        // ── Strip list ──────────────────────────────────────────
        Repeater {
            model: appState ? appState.stripCount : 0

            delegate: Rectangle {
                required property int index
                Layout.fillWidth: true
                height: 32
                radius: 4
                color: {
                    let s = appState ? appState.getStrip(index) : null
                    if (!s) return "transparent"
                    if (s.is_current) return Qt.rgba(palette.highlight.r, palette.highlight.g, palette.highlight.b, 0.15)
                    return "transparent"
                }
                border.color: {
                    let s = appState ? appState.getStrip(index) : null
                    if (s && s.recording) return "#F44336"
                    if (s && s.is_current) return palette.highlight
                    return "transparent"
                }
                border.width: {
                    let s = appState ? appState.getStrip(index) : null
                    return (s && (s.is_current || s.recording)) ? 1 : 0
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 8
                    anchors.rightMargin: 8
                    spacing: 8

                    // Strip number
                    Text {
                        text: (index + 1).toString()
                        font.pixelSize: 13
                        font.bold: true
                        color: palette.text
                        opacity: 0.5
                        Layout.preferredWidth: 16
                    }

                    // Instrument name
                    Text {
                        property var s: appState ? appState.getStrip(index) : null
                        text: s ? s.instrument : ""
                        font.pixelSize: 13
                        color: palette.text
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }

                    // Content indicator
                    Rectangle {
                        width: 8; height: 8; radius: 4
                        visible: {
                            let s = appState ? appState.getStrip(index) : null
                            return s ? s.has_content : false
                        }
                        color: "#64B5F6"
                    }

                    // Mute indicator
                    Text {
                        property var s: appState ? appState.getStrip(index) : null
                        text: s && s.muted ? "M" : ""
                        font.pixelSize: 11
                        font.bold: true
                        color: "#FF9800"
                        Layout.preferredWidth: 14
                    }

                    // Recording indicator
                    Text {
                        property var s: appState ? appState.getStrip(index) : null
                        text: s && s.recording ? "●" : ""
                        font.pixelSize: 14
                        color: "#F44336"
                        Layout.preferredWidth: 14
                    }
                }
            }
        }

        // ── Separator ───────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            height: 1
            color: palette.mid
        }

        // ── Help panel ──────────────────────────────────────────
        Text {
            Layout.fillWidth: true
            Layout.fillHeight: true
            text: "<b>Controls</b><br>" +
                  "<b>Strip buttons</b> — mute/unmute<br>" +
                  "<b>Modifier + strip button</b> — select strip<br>" +
                  "<b>Modifier + piano key</b> — change instrument<br>" +
                  "<b>Modifier + mod wheel</b> — set tempo<br>" +
                  "<b>Master strip button</b> — toggle metronome<br>" +
                  "<b>Master fader</b> — master volume<br>" +
                  "<b>Record</b> — start/stop recording<br>" +
                  "<b>Play / Stop</b> — transport"
            font.pixelSize: 11
            color: palette.text
            opacity: 0.6
            wrapMode: Text.WordWrap
            verticalAlignment: Text.AlignTop
        }
    }
}
