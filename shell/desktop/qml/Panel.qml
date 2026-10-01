// 员工面板：以"你说 / 它答"的对话为主体，思考链与调用链默认折叠
//
// 时间线由 main.qml 按专员分模型喂进来（切走再切回不丢）；
// 过程行折叠成一条"过程 · N 步"，点开才展开细节——干净优先。
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import YaiTheme

Item {
    id: panel

    // Loader 不会替有尺寸的项改大小：显式跟随窗口，否则右侧留下吃点击的透明死区
    width: parent ? parent.width : 420
    height: parent ? parent.height : 620

    signal collapseRequested

    readonly property var host: Window.window
    readonly property string sid: workbench.activeSpecialistId
    property var feedModel: null
    property var pending: workbench
                          ? JSON.parse(workbench.activePendingPayload || "{}") : ({})

    readonly property var chipHints: ({
        "sales": ["算这笔订单的含税金额并归档", "两个独立只读任务，并行委派"],
        "warehouse": ["哪种物料快断货，帮我补齐"],
        "notes": ["找出上次那个复盘的要点"],
        "companion": ["记下今天的心情，看趋势"]
    })

    readonly property string pendingSummary: {
        if (workbench.activePendingKind === "ask") return String(panel.pending.question || "");
        var args = panel.pending.arguments || {};
        var keys = Object.keys(args);
        var parts = [];
        for (var i = 0; i < keys.length; i++) parts.push(keys[i] + "=" + args[keys[i]]);
        return String(panel.pending.tool || "") +
               (parts.length ? "（" + parts.join(" · ") + "）" : "");
    }

    // 权限三挡：全审（琥珀，每次都问）/ 部审（紫，读放行、写询问）/ 免审（红，风险最高）。
    // 点标题栏小 Pill 循环切换；经 channel.permission_mode 在"下一个任务"装配策略时生效。
    readonly property var permLabels: ({
        "manual": "全审",
        "partial": "部审",
        "auto": "免审"
    })
    readonly property var permOrder: ["manual", "partial", "auto"]
    readonly property string permLabel:
        panel.permLabels[workbench.activePermissionMode] || "部审"
    readonly property color permTone: {
        var mode = workbench.activePermissionMode;
        if (mode === "manual") return Theme.warn;
        if (mode === "auto") return Theme.bad;
        return Theme.accent;
    }

    function cyclePermission() {
        var modes = panel.permOrder;
        var idx = modes.indexOf(workbench.activePermissionMode);
        workbench.setPermissionMode(modes[(idx + 1) % modes.length]);
    }

    // 用户主动点开面板时聚焦输入框，展开即可直接打字（由 main.qml 在动画后调用）。
    function focusInput() {
        inputField.forceActiveFocus();
    }

    function send() {
        var text = inputField.text.trim();
        if (text === "") return;
        host.pushUser(sid, text);
        if (workbench.startTask(text)) inputField.text = "";
    }

    function toggleChain(index, open) {
        if (panel.feedModel) panel.feedModel.set(index, { "fOpen": open });
    }

    // 背景拖动手势：空白处按住即由系统拖动整座岛；控件在其之上自吃点击
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        property point grabAt
        property bool moved: false
        onPressed: (mouse) => {
            grabAt = Qt.point(mouse.x, mouse.y);
            moved = false;
        }
        onPositionChanged: (mouse) => {
            if (!pressed || moved) return;
            if (Math.abs(mouse.x - grabAt.x) <= Theme.dragSlop
                    && Math.abs(mouse.y - grabAt.y) <= Theme.dragSlop) return;
            moved = true;
            host.startSystemMove();
        }
    }

    ColumnLayout {
        objectName: "panelLayout"
        anchors.fill: parent
        spacing: 10

        // ---------- 标题 ----------
        RowLayout {
            spacing: 6
            Text {
                text: "YAI 灵动岛"
                color: Theme.text
                font.pixelSize: 14
                font.bold: true
            }
            Rectangle {
                width: modeText.implicitWidth + 14
                height: 19
                radius: 9.5
                color: "transparent"
                border.width: 1
                border.color: workbench.activeRunMode === "demo" ? Theme.dim : Theme.accent
                Text {
                    id: modeText
                    anchors.centerIn: parent
                    font.pixelSize: 10
                    color: workbench.activeRunMode === "demo" ? Theme.dim : Theme.accent
                    text: workbench.activeRunMode === "demo" ? "离线演示" : "真实模型"
                }
            }
            Item { Layout.fillWidth: true }
            Pill {
                label: workbench.activeCoreEnabled ? "Core 开" : "Core 关"
                filled: workbench.activeCoreEnabled
                tone: workbench.activeCoreEnabled ? Theme.ok : Theme.dim
                onClicked: workbench.setCoreEnabled(!workbench.activeCoreEnabled)
            }
            Pill {
                label: panel.permLabel
                tone: panel.permTone
                onClicked: panel.cyclePermission()
            }
            Pill {
                label: "工具"
                tone: workbench.codeVaultAvailable ? Theme.accent : Theme.dim
                onClicked: {
                    if (!workbench.codeVaultAvailable) return;
                    vaultLayer.open();
                }
            }
            Pill {
                label: "学习"
                tone: workbench.learningAvailable ? Theme.accent : Theme.dim
                onClicked: {
                    if (!workbench.learningAvailable) return;
                    learningLayer.open();
                }
            }
            Pill { label: "收"; tone: Theme.dim; onClicked: panel.collapseRequested() }
            Pill { label: "退"; tone: Theme.dim; onClicked: workbench.requestQuit() }
        }

        // 整体以离线模式启动时，明确告知这是脚本，引导评委走"真实员工"入口
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 28 : 0
            visible: !workbench.launchLive
            radius: 8
            color: "#22F5A524"
            border.width: 1
            border.color: Theme.warn
            Text {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                text: "离线演示：回复为预设脚本；真实能力请用桌面「真实员工」入口启动"
                color: Theme.warn
                font.pixelSize: 10
                elide: Text.ElideRight
            }
        }

        // ---------- 专员切换条 ----------
        Row {
            spacing: 6
            Repeater {
                model: workbench.specialistModel
                delegate: SpecialistChip { }
            }
        }

        // ---------- 授权 / 澄清卡 ----------
        Rectangle {
            id: pendingCard
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? pendingCol.implicitHeight + 18 : 0
            visible: workbench.activePendingKind !== ""
            opacity: visible ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Theme.msNormal } }
            radius: 12
            color: "#22F5A524"
            border.width: 1
            border.color: Theme.warn

            ColumnLayout {
                id: pendingCol
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.right: parent.right
                anchors.rightMargin: 12
                anchors.top: parent.top
                anchors.topMargin: 10
                spacing: 8

                RowLayout {
                    spacing: 8
                    Rectangle {
                        width: 22
                        height: 22
                        radius: 6
                        color: Theme.warn
                        Text {
                            anchors.centerIn: parent
                            text: workbench.activePendingKind === "ask" ? "?" : "⛨"
                            color: "#12141A"
                            font.pixelSize: 13
                            font.bold: true
                        }
                    }
                    Text {
                        text: workbench.activePendingKind === "ask"
                              ? "数字员工要澄清" : "新能力授权请求"
                        color: Theme.warn
                        font.pixelSize: 12
                        font.bold: true
                    }
                    Item { Layout.fillWidth: true }
                }

                // 待授权代码可能很长：限高 + 内部滚动，保证下方"允许/拒绝"始终可点
                ScrollView {
                    id: pendingScroll
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(220, codeText.implicitHeight + 2)
                    clip: true
                    Text {
                        id: codeText
                        width: pendingScroll.width - 12
                        text: panel.pendingSummary
                        color: Theme.text
                        font.pixelSize: 12
                        font.family: workbench.activePendingKind === "confirm"
                                     ? "Consolas, 'Courier New', monospace"
                                     : Qt.application.font.family
                        wrapMode: Text.WordWrap
                    }
                }

                RowLayout {
                    spacing: 8
                    visible: workbench.activePendingKind === "ask"
                    Rectangle {
                        Layout.fillWidth: true
                        height: 32
                        radius: 16
                        color: Theme.raised
                        border.width: 1
                        border.color: Theme.edge
                        TextField {
                            id: answerField
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 8
                            background: Item { }
                            color: Theme.text
                            font.pixelSize: 12
                            placeholderText: "回答一句…"
                            placeholderTextColor: Theme.dim
                            onActiveFocusChanged: host.typing =
                                inputField.activeFocus || answerField.activeFocus
                            onAccepted: workbench.answer(text)
                        }
                    }
                    Pill {
                        label: "提交"
                        filled: true
                        tone: Theme.accent
                        onClicked: workbench.answer(answerField.text)
                    }
                }

                RowLayout {
                    spacing: 8
                    visible: workbench.activePendingKind === "confirm"
                    Item { Layout.fillWidth: true }
                    Pill { label: "拒绝"; tone: Theme.bad; onClicked: workbench.decide(false) }
                    Pill {
                        label: "允许"
                        filled: true
                        tone: Theme.ok
                        onClicked: workbench.decide(true)
                    }
                }
            }
        }

        // ---------- 对话（过程默认折叠） ----------
        ListView {
            id: feedView
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 80
            spacing: 8
            model: panel.feedModel
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            onCountChanged: positionViewAtEnd()
            ScrollIndicator.vertical: ScrollIndicator { }

            delegate: Item {
                id: row
                required property var model
                required property int index
                width: feedView.width
                height: model.fKind === "say" ? bubble.height : chain.implicitHeight
                opacity: 0

                SayBubble {
                    id: bubble
                    visible: row.model.fKind === "say"
                    anchors.top: parent.top
                    text: row.model.fText || ""
                    tone: row.model.fTone || Theme.text
                    fromUser: row.model.fWho === "user"
                    maxWidth: row.width
                }

                ChainBlock {
                    id: chain
                    visible: row.model.fKind === "chain"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    count: row.model.fCount
                    steps: JSON.parse(row.model.fSteps || "[]")
                    open: row.model.fOpen === true
                    onToggled: panel.toggleChain(row.index, !row.model.fOpen)
                }

                NumberAnimation on opacity {
                    from: 0
                    to: 1
                    duration: 180
                    easing.type: Easing.OutQuad
                }
            }

            Text {
                anchors.centerIn: parent
                visible: feedView.count === 0
                color: Theme.dim
                font.pixelSize: 12
                text: "交代一件事，看 " + workbench.activeName + " 自己想办法"
            }
        }

        // ---------- 开场任务 ----------
        Row {
            spacing: 6
            visible: workbench.activeCoreEnabled && !workbench.activeBusy
            Repeater {
                model: panel.chipHints[panel.sid] || []
                delegate: Pill {
                    required property var modelData
                    label: modelData.length > 14 ? modelData.slice(0, 14) + "…" : modelData
                    tone: Theme.dim
                    onClicked: {
                        inputField.text = modelData;
                        panel.send();
                    }
                }
            }
        }

        // ---------- 输入 ----------
        RowLayout {
            spacing: 8
            Rectangle {
                Layout.fillWidth: true
                height: 38
                radius: 19
                color: Theme.raised
                border.width: 1
                border.color: inputField.activeFocus ? Theme.accent : Theme.edge
                Behavior on border.color { ColorAnimation { duration: Theme.msFast } }
                TextField {
                    id: inputField
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: 70
                    verticalAlignment: TextInput.AlignVCenter
                    background: Item { }
                    color: Theme.text
                    font.pixelSize: 12
                    placeholderText: workbench.activeCoreEnabled
                                     ? "交给" + workbench.activeName + "…" : "先启用该专员"
                    placeholderTextColor: Theme.dim
                    onActiveFocusChanged: host.typing =
                        inputField.activeFocus || answerField.activeFocus
                    onAccepted: panel.send()
                }
                Text {
                    anchors.right: parent.right
                    anchors.rightMargin: 16
                    anchors.verticalCenter: parent.verticalCenter
                    color: Theme.dim
                    font.pixelSize: 10
                    text: workbench.activeBusy ? "执行中" : "Enter 发送"
                }
            }
            Pill {
                label: workbench.activeBusy ? "终止" : "发送"
                filled: true
                tone: workbench.activeBusy ? Theme.bad : Theme.accent
                onClicked: {
                    if (workbench.activeBusy) workbench.cancel();
                    else panel.send();
                }
            }
        }
    }

    // 代码工具仓库弹层：放在最后，保证盖在所有内容之上
    ToolVaultPanel {
        id: vaultLayer
        anchors.fill: parent
    }

    // 路由学习状态弹层：放在最后，保证盖在所有内容之上
    LearningPanel {
        id: learningLayer
        anchors.fill: parent
    }

    // ---------- 对话气泡 ----------
    component SayBubble: Rectangle {
        property string text: ""
        property color tone: Theme.text
        property bool fromUser: false
        property real maxWidth: 400
        readonly property real avail: Math.max(40, maxWidth - 24)

        // 气泡按文字自然宽度收缩、长文本才 wrap 到 avail 上限。
        // MarkdownText 的 paintedWidth 会错误返回 Text.width（实测短文本也全宽）；
        // 用内部 Text 的 implicitWidth：它等于"不换行自然宽度"、不受 Text.width 影响，
        // 且 label 是子项、引用它不会像引用气泡自身 implicitWidth 那样形成绑定环。
        width: Math.min(avail, label.implicitWidth) + 24
        height: label.paintedHeight + 18
        radius: 14
        color: fromUser ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.22)
                        : Theme.raised
        border.width: 1
        border.color: fromUser ? Theme.accent : Theme.edge
        x: fromUser ? maxWidth - width : 0

        Text {
            id: label
            x: 12
            width: parent.avail
            anchors.verticalCenter: parent.verticalCenter
            text: parent.text
            // AI 回复是 Markdown（加粗 / 表格 / 列表 / 代码）；用户原话保持
            // 纯文本，避免把 * # 等符号误解析。MarkdownText = CommonMark + GFM 表格。
            textFormat: parent.fromUser ? Text.PlainText : Text.MarkdownText
            color: parent.tone
            font.pixelSize: 12
            wrapMode: Text.WordWrap
        }
    }

    // ---------- 折叠的过程行 ----------
    component ChainBlock: Column {
        id: chainRoot
        property int count: 0
        property var steps: []
        property bool open: false
        signal toggled

        spacing: 4

        Rectangle {
            width: head.implicitWidth + 20
            height: 22
            radius: 11
            color: "transparent"
            border.width: 1
            border.color: Theme.edge
            Row {
                id: head
                anchors.centerIn: parent
                spacing: 6
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: open ? "⌄" : "›"
                    color: Theme.dim
                    font.pixelSize: 11
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "过程 · " + count + " 步"
                    color: Theme.dim
                    font.pixelSize: 11
                }
            }
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: chainRoot.toggled()
            }
        }

        Repeater {
            model: open ? steps : []
            delegate: Text {
                required property var modelData
                width: parent ? parent.width : 200
                text: "· " + modelData.t
                color: Theme.dim
                font.pixelSize: 11
                wrapMode: Text.WordWrap
            }
        }
    }

    // ---------- 专员 chip ----------
    component SpecialistChip: Rectangle {
        required property int index
        required property string specialistId
        required property string name
        required property string glyph
        required property bool busy
        required property string pendingKind
        required property int unread
        required property bool active

        width: chipRow.implicitWidth + 20
        height: 28
        radius: 14
        color: active ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.18)
                      : "transparent"
        border.width: 1
        border.color: active ? Theme.accent : Theme.edge
        Behavior on color { ColorAnimation { duration: Theme.msFast } }

        Row {
            id: chipRow
            anchors.centerIn: parent
            spacing: 5
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: glyph
                color: active ? Theme.accent : Theme.dim
                font.pixelSize: 11
                font.bold: true
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: name.replace("专员", "")
                color: active ? Theme.text : Theme.dim
                font.pixelSize: 11
            }
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: 7
                height: 7
                radius: 3.5
                visible: busy || pendingKind !== ""
                color: pendingKind !== "" ? Theme.warn : Theme.accent
            }
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: badgeText.implicitWidth + 10
                height: 15
                radius: 7.5
                visible: unread > 0 && !active
                color: Theme.warn
                Text {
                    id: badgeText
                    anchors.centerIn: parent
                    text: unread
                    color: "#12141A"
                    font.pixelSize: 9
                    font.bold: true
                }
            }
        }

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: workbench.setActiveSpecialist(specialistId)
        }
    }

    // ---------- 小圆角按钮 ----------
    component Pill: Rectangle {
        property string label: ""
        property color tone: Theme.accent
        property bool filled: false
        property bool active: false
        signal clicked

        width: pillText.implicitWidth + 22
        height: 26
        radius: 13
        color: filled ? Qt.rgba(tone.r, tone.g, tone.b, 0.92)
                      : (active ? Qt.rgba(tone.r, tone.g, tone.b, 0.18) : "transparent")
        border.width: 1
        border.color: tone
        Text {
            id: pillText
            anchors.centerIn: parent
            text: label
            color: filled ? "#12141A" : tone
            font.pixelSize: 11
            font.bold: active
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: parent.clicked()
        }
    }
}
