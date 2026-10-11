// 标题栏齿轮下拉：把 Core 开关、审批档位、代码工具、路由学习、自动收回、退出
// 收拢到一处，标题栏保持干净。非模态：不压暗背景，点卡片外任意处即关。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import YaiTheme

Rectangle {
    id: menu

    visible: false
    color: "transparent"
    opacity: visible ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: Theme.msFast } }

    // 请求打开当前专员的代码工具 / 路由学习弹层（由 Panel 接线，菜单自身先关）
    signal openTools()
    signal openLearning()

    function open() { visible = true; }
    function close() { visible = false; }

    // 切专员时关掉，避免在别的专员上误操作上一个专员的开关
    property string boundSid: workbench.activeSpecialistId
    onBoundSidChanged: menu.visible = false

    // 点卡片外任意处关闭，并吞掉滚轮，不让后面的聊天列表跟着滚
    MouseArea {
        anchors.fill: parent
        enabled: menu.visible
        onClicked: menu.close()
        onWheel: (wheel) => { wheel.accepted = true; }
    }

    Rectangle {
        id: card
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 30
        anchors.rightMargin: 2
        width: 248
        implicitHeight: cardCol.implicitHeight + 20
        radius: 12
        color: Theme.shell
        border.width: 1
        border.color: Theme.edgeStrong
        z: 2

        // 卡片内点击不被外层遮罩关掉，并吞掉滚轮
        MouseArea {
            anchors.fill: parent
            onWheel: (wheel) => { wheel.accepted = true; }
        }

        ColumnLayout {
            id: cardCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 12
            spacing: 8

            // 标题
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: "设置"
                    color: Theme.text
                    font.pixelSize: 13
                    font.bold: true
                }
                Text {
                    text: workbench.activeName
                    color: Theme.dim
                    font.pixelSize: 10
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: "×"
                    color: Theme.dim
                    font.pixelSize: 15
                    MouseArea {
                        anchors.fill: parent
                        anchors.margins: -8
                        cursorShape: Qt.PointingHandCursor
                        onClicked: menu.close()
                    }
                }
            }

            Hairline { }

            // Core 开关
            RowLayout {
                Layout.fillWidth: true
                Text { text: "Core 启用"; color: Theme.text; font.pixelSize: 12 }
                Item { Layout.fillWidth: true }
                Toggle {
                    checked: workbench.activeCoreEnabled
                    onToggled: workbench.setCoreEnabled(checked)
                }
            }

            // 审批档位：三段分段选择
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5
                Text {
                    text: "审批模式"
                    color: Theme.text
                    font.pixelSize: 12
                }
                Row {
                    spacing: 6
                    Repeater {
                        model: [
                            { "k": "manual", "t": "全审", "c": Theme.warn },
                            { "k": "partial", "t": "部审", "c": Theme.accent },
                            { "k": "auto", "t": "免审", "c": Theme.bad }
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            width: 66
                            height: 26
                            radius: 8
                            readonly property bool on:
                                workbench.activePermissionMode === modelData.k
                            color: on ? Qt.rgba(modelData.c.r, modelData.c.g,
                                                modelData.c.b, 0.18)
                                      : Theme.raised
                            border.width: 1
                            border.color: on ? modelData.c : Theme.edge
                            Text {
                                anchors.centerIn: parent
                                text: modelData.t
                                color: on ? modelData.c : Theme.dim
                                font.pixelSize: 11
                            }
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: workbench.setPermissionMode(modelData.k)
                            }
                        }
                    }
                }
                Text {
                    text: workbench.activePermissionMode === "auto"
                          ? "免审：工具调用不再逐次询问，注意风险"
                          : (workbench.activePermissionMode === "manual"
                             ? "全审：每次工具调用都先问你"
                             : "部审：只读放行，写入 / 高风险先问你")
                    color: Theme.dim
                    font.pixelSize: 10
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }

            Hairline { }

            // 代码工具
            MenuRow {
                label: "代码工具"
                available: workbench.codeVaultAvailable
                onChosen: { menu.close(); menu.openTools(); }
            }

            // 路由学习
            MenuRow {
                label: "路由学习"
                available: workbench.learningAvailable
                onChosen: { menu.close(); menu.openLearning(); }
            }

            // 离开自动收回
            RowLayout {
                Layout.fillWidth: true
                ColumnLayout {
                    spacing: 1
                    Text { text: "离开自动收回"; color: Theme.text; font.pixelSize: 12 }
                    Text {
                        text: "指针移出后自动收成球"
                        color: Theme.dim
                        font.pixelSize: 10
                    }
                }
                Item { Layout.fillWidth: true }
                Toggle {
                    checked: workbench.autoCollapse
                    onToggled: workbench.setAutoCollapse(checked)
                }
            }

            Hairline { }

            // 退出
            Text {
                text: "退出灵动岛"
                color: Theme.bad
                font.pixelSize: 12
                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -6
                    cursorShape: Qt.PointingHandCursor
                    onClicked: workbench.requestQuit()
                }
            }
        }
    }

    // 细分隔线
    component Hairline: Rectangle {
        Layout.fillWidth: true
        height: 1
        color: Theme.edge
    }

    // 可点的跳转行（工具 / 学习）
    component MenuRow: Rectangle {
        id: row
        property string label: ""
        property bool available: true
        signal chosen

        Layout.fillWidth: true
        height: 26
        radius: 8
        color: "transparent"
        opacity: available ? 1 : 0.4
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 4
            anchors.rightMargin: 4
            Text { text: row.label; color: Theme.text; font.pixelSize: 12 }
            Item { Layout.fillWidth: true }
            Text {
                text: available ? "查看 ›" : "不可用"
                color: Theme.dim
                font.pixelSize: 11
            }
        }
        MouseArea {
            anchors.fill: parent
            enabled: row.available
            cursorShape: Qt.PointingHandCursor
            onClicked: row.chosen()
        }
    }

    // 深色主题小开关（与 ToolVaultPanel 同款，内联组件不能跨文件引用）
    component Toggle: Rectangle {
        id: toggle
        property bool checked: false
        signal toggled

        width: 34
        height: 18
        radius: 9
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
            color: Theme.knob
            Behavior on x { NumberAnimation { duration: Theme.msFast; easing.type: Easing.OutCubic } }
        }

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
                toggle.checked = !toggle.checked;
                toggle.toggled();
            }
        }
    }
}
