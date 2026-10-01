// 路由学习状态弹层：只读展示当前专员"越用越准"的自校准结果。
//
// 数据来自 workbench.learningVault —— 读取在常驻 worker loop 上做（Core 每任务临时，
// 空闲时没有 Core 实例，真实状态在持久化 JSON 里），刷新完成经 learningUpdated 回传。
// 每个上下文桶列出四个策略臂的 Beta 后验均值（成功率期望），偏好臂高亮。
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

    readonly property var payload: JSON.parse(workbench.learningVault || "{}")
    readonly property var buckets: payload.buckets || []
    readonly property bool learned: payload.enabled === true

    function open() {
        workbench.openLearning();      // 触发 worker 读取并回传
        visible = true;
    }

    // 切专员时关掉：列的是上一个专员的学习状态，留着会误导
    property string boundSid: workbench.activeSpecialistId
    onBoundSidChanged: overlay.visible = false

    // 点卡片外的遮罩区域关闭（卡片在其上，会吃掉卡片内点击）
    MouseArea {
        anchors.fill: parent
        onClicked: overlay.visible = false
    }

    ColumnLayout {
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 400)

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
                        text: "路由学习"
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

                // 顶部摘要：已学习任务数 + 场景桶数
                RowLayout {
                    spacing: 8
                    visible: overlay.learned
                    Text {
                        text: "已学习 " + (payload.learned_tasks || 0) + " 个任务 · "
                              + (payload.context_buckets || 0) + " 类场景"
                        color: Theme.ok
                        font.pixelSize: 11
                    }
                    Item { Layout.fillWidth: true }
                }

                ListView {
                    id: learnList
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(300, Math.max(96, contentHeight))
                    // 保底 96px：0 高度时委托不会被创建，contentHeight 就永远长不出来
                    spacing: 8
                    clip: true
                    interactive: contentHeight > 300
                    model: overlay.buckets

                    delegate: Rectangle {
                        required property var modelData
                        id: bucketCard

                        width: learnList.width
                        height: bucketCol.implicitHeight + 20
                        radius: 10
                        color: Theme.raised
                        border.width: 1
                        border.color: Theme.edge

                        ColumnLayout {
                            id: bucketCol
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            spacing: 6

                            RowLayout {
                                spacing: 8
                                Text {
                                    text: bucketCard.modelData.context
                                    color: Theme.accent
                                    font.pixelSize: 11
                                    font.bold: true
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: "观测 " + bucketCard.modelData.observed + " 次"
                                    color: Theme.dim
                                    font.pixelSize: 10
                                }
                            }

                            Text {
                                text: "偏好：" + bucketCard.modelData.preferred_label
                                      + "（置信 " + Number(bucketCard.modelData.confidence).toFixed(2) + "）"
                                color: Theme.text
                                font.pixelSize: 10
                                Layout.fillWidth: true
                            }

                            // 四个策略臂的 Beta 后验均值条
                            Column {
                                spacing: 3
                                Repeater {
                                    model: ["direct", "react", "plan", "clarify"]
                                    delegate: Row {
                                        required property string modelData
                                        property var info: (bucketCard.modelData.means
                                                           && bucketCard.modelData.means[modelData])
                                                           || {"label": modelData, "mean": 0}

                                        spacing: 6
                                        Text {
                                            width: 46
                                            text: parent.info.label
                                            color: Theme.dim
                                            font.pixelSize: 9
                                            elide: Text.ElideRight
                                        }
                                        Rectangle {
                                            width: 118
                                            height: 6
                                            radius: 3
                                            anchors.verticalCenter: parent.verticalCenter
                                            color: Theme.idleDot
                                            Rectangle {
                                                height: 6
                                                radius: 3
                                                anchors.verticalCenter: parent.verticalCenter
                                                width: 118 * Number(parent.parent.info.mean || 0)
                                                color: modelData === bucketCard.modelData.preferred
                                                       ? Theme.ok : Theme.accent
                                            }
                                        }
                                        Text {
                                            text: Number(parent.info.mean || 0).toFixed(2)
                                            color: Theme.dim
                                            font.pixelSize: 9
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Text {
                    visible: !overlay.learned
                    Layout.fillWidth: true
                    color: Theme.dim
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    text: "还没有学习记录。让这个专员跑几个不同类型的任务，"
                          + "它会从每类任务的成败里校准该走哪条路，越用越准。"
                }
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
