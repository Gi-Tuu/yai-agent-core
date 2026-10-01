// 代码工具仓库弹层：查看 / 永久保留 / 删除当前专员自建的代码工具。
//
// 数据来自 workbench.codeVault —— 读写都在常驻 worker loop 上做（Core 每任务临时，
// 空闲时没有 Core 实例，真实状态在持久化 JSON 里），刷新完成经 codeVaultUpdated 回传。
// busy 时只允许查看：正在跑的 Core 是装配时的快照，改了文件下次任务才生效。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import YaiTheme

Rectangle {
    id: overlay

    visible: false
    color: Qt.rgba(0, 0, 0, 0.66)
    opacity: visible ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: Theme.msFast } }

    readonly property var payload: JSON.parse(workbench.codeVault || "{}")
    readonly property var tools: payload.tools || []

    function open() {
        workbench.openCodeVault();      // 触发 worker 读取并回传
        visible = true;
    }

    // 切专员时关掉：列的是上一个专员的仓库，留着会误导
    property string boundSid: workbench.activeSpecialistId
    onBoundSidChanged: overlay.visible = false

    // 点卡片外的遮罩区域关闭（卡片在其上，会吃掉卡片内点击）
    MouseArea {
        anchors.fill: parent
        onClicked: overlay.visible = false
    }

    ColumnLayout {
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 380)

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: cardCol.implicitHeight + 28
            radius: 14
            color: Theme.shell
            border.width: 1
            border.color: Theme.edge

            MouseArea { anchors.fill: parent }   // 挡住遮罩，卡片内点击不误关

            ColumnLayout {
                id: cardCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 14
                spacing: 10

                RowLayout {
                    spacing: 8
                    Text {
                        text: "代码工具"
                        color: Theme.text
                        font.pixelSize: 13
                        font.bold: true
                    }
                    Text {
                        text: workbench.activeName
                        color: Theme.dim
                        font.pixelSize: 11
                    }
                    Item { Layout.fillWidth: true }
                    Pill {
                        label: "关闭"
                        tone: Theme.dim
                        onClicked: overlay.visible = false
                    }
                }

                ListView {
                    id: vaultList
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(264, Math.max(88, contentHeight))
                    // 保底 88px：0 高度时委托不会被创建，contentHeight 就永远长不出来
                    spacing: 8
                    clip: true
                    interactive: count > 4
                    model: overlay.tools

                    delegate: Rectangle {
                        required property var modelData
                        required property int index

                        width: vaultList.width
                        height: rowCol.implicitHeight + 20
                        radius: 10
                        color: Theme.raised
                        border.width: 1
                        border.color: Theme.edge

                        ColumnLayout {
                            id: rowCol
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            spacing: 4

                            RowLayout {
                                spacing: 8
                                Text {
                                    text: modelData.name
                                    color: Theme.accent
                                    font.pixelSize: 12
                                    font.bold: true
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: "调用 " + modelData.call_count + " 次 · "
                                          + (modelData.permanent ? "已固定" : "48h")
                                          + (modelData.live ? "" : " · 已过期")
                                    color: Theme.dim
                                    font.pixelSize: 10
                                }
                            }

                            Text {
                                visible: (modelData.description || "") !== ""
                                text: modelData.description
                                color: Theme.text
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }

                            RowLayout {
                                spacing: 8
                                Text {
                                    text: "永久保留"
                                    color: Theme.dim
                                    font.pixelSize: 11
                                }
                                Toggle {
                                    checked: modelData.permanent === true
                                    enabled: !workbench.activeBusy
                                    onToggled: workbench.setCodeToolPermanent(
                                        modelData.name, checked)
                                }
                                Item { Layout.fillWidth: true }
                                Pill {
                                    label: "删除"
                                    tone: Theme.bad
                                    enabled: !workbench.activeBusy
                                    onClicked: workbench.removeCodeTool(modelData.name)
                                }
                            }
                        }
                    }
                }

                Text {
                    visible: vaultList.count === 0
                    Layout.fillWidth: true
                    color: Theme.dim
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    text: "还没有自建代码工具。让专员处理一个需要计算 / 统计的任务，"
                          + "它会在授权后于沙箱里创建一个。"
                }

                Text {
                    visible: workbench.activeBusy
                    Layout.fillWidth: true
                    color: Theme.warn
                    font.pixelSize: 10
                    wrapMode: Text.WordWrap
                    text: "任务执行中：可查看。永久保留 / 删除将在下次任务生效。"
                }
            }
        }
    }

    // 深色主题下的小开关（默认 Switch 太大、指示器会压住描述行）
    component Toggle: Rectangle {
        id: toggle
        property bool checked: false
        signal toggled

        width: 34
        height: 18
        radius: 9
        opacity: enabled ? 1 : 0.45
        color: checked ? Theme.ok : Theme.idleDot
        border.width: 1
        border.color: checked ? Theme.ok : Theme.edge
        Behavior on color { ColorAnimation { duration: Theme.msFast } }

        Rectangle {
            x: toggle.checked ? toggle.width - width - 2 : 2
            anchors.verticalCenter: parent.verticalCenter
            width: 14
            height: 14
            radius: 7
            color: "#F4F6FB"
            Behavior on x { NumberAnimation { duration: Theme.msFast; easing.type: Easing.OutCubic } }
        }

        MouseArea {
            anchors.fill: parent
            enabled: toggle.enabled
            cursorShape: Qt.PointingHandCursor
            onClicked: {
                toggle.checked = !toggle.checked;
                toggle.toggled();
            }
        }
    }

    // 与 Panel.qml 同款小按钮（内联 component 不能跨文件引用，故此处再一份）
    component Pill: Rectangle {
        property string label: ""
        property color tone: Theme.accent
        property bool filled: false
        signal clicked

        width: pillText.implicitWidth + 22
        height: 26
        radius: 13
        opacity: enabled ? 1 : 0.45
        color: filled ? Qt.rgba(tone.r, tone.g, tone.b, 0.92) : "transparent"
        border.width: 1
        border.color: tone
        Text {
            id: pillText
            anchors.centerIn: parent
            text: label
            color: filled ? "#12141A" : tone
            font.pixelSize: 11
        }
        MouseArea {
            anchors.fill: parent
            enabled: parent.enabled
            cursorShape: Qt.PointingHandCursor
            onClicked: parent.clicked()
        }
    }
}
