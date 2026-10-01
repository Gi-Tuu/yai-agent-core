// YAI 灵动岛 · 原生桌面端（Qt Quick 渲染，非浏览器内容）
//
// 形态机：球（待命）→ 指针停留 → 胶囊（向右展开）→ 点击 → 面板（向下展开）；
// 指针离开窗口回到球。左上角不动，整岛移动交给系统 startSystemMove()。
// 纪律：动效只在小元素上做 x/y/opacity/color/height 属性动画，
// 不开全窗 alpha 模糊、不做满屏重绘（避免拖慢整个桌面）。
import QtQuick
import QtQuick.Controls
import QtQuick.Window
import YaiTheme

Window {
    id: root

    property string shape: "ball"        // ball | capsule | panel
    property bool hovering: false
    property bool dragging: false
    property bool typing: false      // 面板输入框持有焦点（正在打字），不自动收起
    property string capText: "待命"
    property string notice: ""
    property bool delegating: false

    readonly property bool expanded: shape === "panel"
    readonly property var pending: workbench
                                   ? JSON.parse(workbench.activePendingPayload || "{}") : ({})

    // 不用 Qt.Tool：无 owner 的透明 frameless Tool 窗口在 Windows 上会走到
    // QWindowToolSaveBits 遗留合成路径，内容全透明、鼠标穿透；改用 Qt.Window 走
    // 正常 DWM 合成，任务栏图标由 app.py 事后加 WS_EX_TOOLWINDOW 隐藏。
    flags: Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    color: "transparent"
    visible: true
    // 左上角不动：球→胶囊只向右长，胶囊→面板只向下长
    width: shape === "panel" ? Theme.panelW
           : (shape === "capsule" ? Theme.capW : Theme.ballSize)
    height: shape === "panel" ? Theme.panelH
            : (shape === "capsule" ? Theme.capH : Theme.ballSize)

    Behavior on width { PropertyAnimation { duration: Theme.msNormal; easing.type: Easing.OutCubic } }
    Behavior on height { PropertyAnimation { duration: Theme.msSlow; easing.type: Easing.OutCubic } }

    function collapse() {
        if (workbench.activePendingKind !== "") return;   // 有决策要用户看到，不收
        if (root.typing) return;                         // 正在打字，不收
        root.shape = "ball";
    }

    // 悬停驻留 110ms 才展开成胶囊，路过不触发
    Timer {
        id: dwell
        interval: 110
        onTriggered: if (root.hovering && root.shape === "ball") root.shape = "capsule"
    }

    // 离开留一点宽限：指针跨子项时 hover 可能瞬时抖动，别立刻收
    Timer {
        id: leaveGrace
        interval: 320
        onTriggered: {
            if (root.hovering || root.dragging) return;
            if (root.shape === "capsule") root.shape = "ball";
            else if (root.shape === "panel") root.collapse();
        }
    }

    onHoveringChanged: {
        if (hovering) {
            leaveGrace.stop();
            if (shape === "ball") dwell.restart();
            return;
        }
        dwell.stop();
        if (dragging) return;
        leaveGrace.restart();
    }

    // ---------- 事件 → 对话主体 + 折叠过程 ----------

    function shortArgs(args) {
        var keys = Object.keys(args || {});
        var parts = [];
        for (var i = 0; i < keys.length && i < 3; i++) {
            var value = args[keys[i]];
            var text = (value !== null && typeof value === "object")
                       ? JSON.stringify(value) : String(value);
            parts.push(keys[i] + "=" + text.slice(0, 48));
        }
        return parts.join(" · ");
    }

    // ListModel 的角色表由第一条 append 决定，之后缺的角色会被静默丢弃：
    // 所以每一行都写全同一组角色。步骤数组以 JSON 串存（读回来不是 JS 数组）。
    function blank() {
        return { "fKind": "", "fText": "", "fTone": Theme.text, "fWho": "agent",
                 "fSteps": "[]", "fCount": 0, "fOpen": false };
    }

    function addStep(model, text) {
        if (model === undefined) return;
        var last = model.count - 1;
        if (last >= 0 && model.get(last).fKind === "chain") {
            var row = model.get(last);
            var steps = JSON.parse(row.fSteps);
            steps.push({ "t": text });
            var updated = blank();
            updated.fKind = "chain";
            updated.fSteps = JSON.stringify(steps);
            updated.fCount = steps.length;
            updated.fOpen = row.fOpen;
            model.set(last, updated);
        } else {
            var fresh = blank();
            fresh.fKind = "chain";
            fresh.fSteps = JSON.stringify([{ "t": text }]);
            fresh.fCount = 1;
            model.append(fresh);
        }
    }

    function addSay(model, text, tone, who) {
        if (model === undefined || text === "") return;
        var row = blank();
        row.fKind = "say";
        row.fText = text;
        row.fTone = tone;
        row.fWho = who || "agent";
        model.append(row);
    }

    // done 的 final_text 可能与刚显示的 model_message 同源（live 内核会重复），
    // 也可能是独立结论（demo 脚本）：仅当与最近一条 AI 说的话完全相同时才跳过。
    function lastAgentSayMatches(model, text) {
        if (model === undefined) return false;
        for (var i = model.count - 1; i >= 0; i--) {
            var r = model.get(i);
            if (r.fKind === "say")
                return r.fWho !== "user" && r.fText === text;
        }
        return false;
    }

    // 面板发问时用：对话以"你说 / 它答"为主体
    function pushUser(sid, text) {
        addSay(feeds[sid], text, Theme.text, "user");
    }

    function onSpecialistEvent(sid, type, payloadJson) {
        var d = {};
        try { d = payloadJson ? JSON.parse(payloadJson) : {}; } catch (e) { d = {}; }
        var model = feeds[sid];

        // 非当前专员：只提醒，不改当前画面
        if (sid !== workbench.activeSpecialistId) {
            if (type === "done" || type === "error"
                    || type === "permission_asked" || type === "clarify_requested")
                root.capText = workbench.displayName(sid) + "有事找你";
            return;
        }

        if (type === "strategy_selected") {
            root.capText = "路由 " + (d.strategy || "?") + "（" + (d.source || "?") + "）";
            ripple.restart();
            addStep(model, "路由 " + (d.strategy || "?") + " · " + (d.source || "?"));
        } else if (type === "model_message") {
            addSay(model, String(d.text || ""), Theme.text);
        } else if (type === "capability_missing") {
            root.capText = "能力缺口";
            amberFlash.restart();
            addStep(model, "缺能力 · " + String(d.missing || ""));
        } else if (type === "tool_discovered") {
            root.capText = "发现新工具 " + (d.registered || []).join("、");
            chipFly.restart();
            addStep(model, "发现工具 · " + (d.registered || []).join("、"));
        } else if (type === "permission_asked" || type === "clarify_requested") {
            root.shape = "panel";
            root.capText = type === "permission_asked" ? "等待你的授权" : "需要你澄清";
            addStep(model, (type === "permission_asked" ? "请求授权 · " : "反问 · ")
                           + String(d.tool || d.question || ""));
        } else if (type === "tool_call") {
            if (d.tool === "delegate") root.delegating = true;
            root.capText = "调用 " + (d.tool || "?");
            addStep(model, "调用 " + (d.tool || "?") + " " + shortArgs(d.arguments));
        } else if (type === "tool_result") {
            if (d.tool === "delegate") root.delegating = false;
            var ok = d.ok !== false;
            addStep(model, (ok ? "完成 " : "失败 ") + (d.tool || "?") + " "
                             + String(d.preview || d.error || "").slice(0, 80));
        } else if (type === "tool_composed") {
            root.capText = "组合出新工具";
            chipFly.restart();
            addStep(model, "组合工具 " + (d.tool || "") + " · " + (d.steps || []).join("→"));
        } else if (type === "code_tool_created") {
            root.capText = "沙箱里造出新工具";
            chipFly.restart();
            addStep(model, "新代码工具 " + (d.tool || ""));
        } else if (type === "worker_delegated") {
            root.delegating = true;
            addStep(model, "派出子员工 · " + String(d.goal || "").slice(0, 60));
        } else if (type === "done") {
            root.delegating = false;
            root.capText = "已完成";
            // live 内核的 final_text 与刚发的 model_message 同源，去重；
            // demo 的 final_text 是独立结论，仍显示。
            var ft = String(d.final_text || "");
            if (ft !== "" && !lastAgentSayMatches(model, ft))
                addSay(model, ft, Theme.accent);
            collapseTimer.restart();
        } else if (type === "cancelled") {
            root.delegating = false;
            root.capText = "已终止";
            addSay(model, "已终止：" + String(d.reason || ""), Theme.bad);
        } else if (type === "error") {
            root.delegating = false;
            root.notice = String(d.error || "出错");
            root.capText = "出错";
            addSay(model, "出错：" + root.notice, Theme.bad);
        }
    }

    // 每个专员一份独立对话（切走再切回不丢）
    property var feeds: ({})

    Component.onCompleted: {
        var ids = workbench.specialistIds;
        for (var i = 0; i < ids.length; i++) {
            feeds[ids[i]] = Qt.createQmlObject(
                'import QtQuick; ListModel { }', root, "feed" + i);
        }
        workbench.specialistEvent.connect(root.onSpecialistEvent);
        workbench.noticeRaised.connect(function (text) { root.notice = text; });
        root.capText = workbench.activeName;
    }

    Connections {
        target: workbench
        function onStateChanged() {
            if (workbench.activePendingKind !== "") root.shape = "panel";
        }
    }

    // 任务完成后延迟自动收起；但指针还停在窗口内看结果时不收，
    // 等指针真正离开后由 leaveGrace 自然收回。
    Timer {
        id: collapseTimer
        interval: 1600
        onTriggered: if (!root.hovering) root.collapse()
    }

    // ---------- 唯一内容根：hover 挂在这里才覆盖全部子树 ----------
    Item {
        id: surface
        anchors.fill: parent

        HoverHandler {
            id: hoverProbe
            onHoveredChanged: root.hovering = hovered
        }

        Rectangle {
            id: shell
            anchors.fill: parent
            // 每态各自的小圆角（都不超过短边一半）：形变中间帧不会再割出扇形/椭圆
            radius: root.shape === "panel" ? 28
                    : (root.shape === "capsule" ? 26 : height / 2)
            color: Theme.shell
            border.width: 1
            border.color: Theme.edge
            Behavior on radius { PropertyAnimation { duration: Theme.msNormal } }

            Rectangle {
                id: amberOverlay
                anchors.fill: parent
                radius: shell.radius
                color: "transparent"
                border.width: 2
                border.color: "transparent"
                SequentialAnimation {
                    id: amberFlash
                    ColorAnimation { target: amberOverlay; property: "border.color"; to: Theme.warn; duration: 150 }
                    ColorAnimation { target: amberOverlay; property: "border.color"; to: "transparent"; duration: 240 }
                    ColorAnimation { target: amberOverlay; property: "border.color"; to: Theme.warn; duration: 150 }
                    ColorAnimation { target: amberOverlay; property: "border.color"; to: "transparent"; duration: 280 }
                }
            }
        }

        // ---------- 球 / 胶囊 ----------
        Item {
            id: chrome
            anchors.fill: parent
            visible: !root.expanded
            opacity: root.expanded ? 0 : 1
            Behavior on opacity { NumberAnimation { duration: Theme.msFast } }

            MouseArea {
                id: chromeDrag
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                property point grabAt
                property bool moved: false

                onPressed: (mouse) => {
                    grabAt = Qt.point(mouse.x, mouse.y);
                    moved = false;
                    root.dragging = true;
                }
                onPositionChanged: (mouse) => {
                    if (!pressed || moved) return;
                    if (Math.abs(mouse.x - grabAt.x) <= Theme.dragSlop
                            && Math.abs(mouse.y - grabAt.y) <= Theme.dragSlop) return;
                    moved = true;
                    if (!root.startSystemMove()) root.dragging = false;
                }
                onReleased: {
                    root.dragging = false;
                    if (!moved) root.shape = "panel";
                }
                onCanceled: root.dragging = false
            }

            // 动态波形：空闲静止，执行中起伏（5 根 3px 小条）
            Row {
                id: waveform
                spacing: 3
                y: (parent.height - 22) / 2
                x: root.shape === "capsule" ? 18 : (parent.width - implicitWidth) / 2
                Behavior on x { PropertyAnimation { duration: Theme.msNormal; easing.type: Easing.OutCubic } }
                Repeater {
                    model: 5
                    Rectangle {
                        required property int index
                        readonly property var peaks: [12, 18, 22, 16, 10]
                        width: 3
                        radius: 1.5
                        color: Theme.accent
                        height: 7
                        SequentialAnimation on height {
                            loops: Animation.Infinite
                            running: workbench.activeBusy || root.delegating
                            NumberAnimation { to: peaks[index]; duration: 190 + index * 45; easing.type: Easing.OutQuad }
                            NumberAnimation { to: 6; duration: 210 + index * 35; easing.type: Easing.InQuad }
                        }
                    }
                }
            }

            Text {
                visible: root.shape === "capsule"
                x: waveform.x + waveform.implicitWidth + 14
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - x - 46
                color: Theme.text
                font.pixelSize: 13
                elide: Text.ElideRight
                text: root.capText
            }

            // 状态点：空闲灰、执行中紫、待授权琥珀
            Rectangle {
                visible: root.shape === "capsule"
                anchors.right: parent.right
                anchors.rightMargin: 18
                anchors.verticalCenter: parent.verticalCenter
                width: 9
                height: 9
                radius: 4.5
                color: workbench.activePendingKind !== "" ? Theme.warn
                     : (workbench.activeBusy ? Theme.accent : Theme.idleDot)
                Behavior on color { ColorAnimation { duration: 200 } }
                SequentialAnimation on opacity {
                    loops: Animation.Infinite
                    running: workbench.activeBusy
                    NumberAnimation { to: 0.35; duration: 650 }
                    NumberAnimation { to: 1; duration: 650 }
                }
            }

            // 其他专员有事找你：右上角一粒琥珀点
            Rectangle {
                visible: workbench.unreadTotal > 0 && !workbench.activeBusy
                width: 12
                height: 12
                radius: 6
                color: Theme.warn
                border.width: 2
                border.color: Theme.shell
                anchors.right: parent.right
                anchors.rightMargin: root.shape === "capsule" ? 30 : 5
                anchors.top: parent.top
                anchors.topMargin: 5
            }

            // 思考涟漪：胶囊态一条高光横穿底边
            Rectangle {
                id: rippleBar
                visible: root.shape === "capsule"
                width: 60
                height: 2
                radius: 1
                y: chrome.height - 4
                x: -width
                color: Theme.accent
                opacity: 0
                SequentialAnimation {
                    id: ripple
                    PropertyAction { target: rippleBar; property: "x"; value: -60 }
                    ParallelAnimation {
                        NumberAnimation { target: rippleBar; property: "opacity"; to: 0.85; duration: 120 }
                        NumberAnimation { target: rippleBar; property: "x"; to: chrome.width;
                                          duration: 900; easing.type: Easing.InOutSine }
                    }
                    NumberAnimation { target: rippleBar; property: "opacity"; to: 0; duration: 160 }
                }
            }

            // 子员工"出芽"：委派中三粒光点从胶囊下缘分出
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.top: parent.bottom
                anchors.topMargin: 3
                spacing: 12
                opacity: root.delegating ? 1 : 0
                visible: opacity > 0.01
                Behavior on opacity { NumberAnimation { duration: Theme.msSlow } }
                Repeater {
                    model: 3
                    Rectangle {
                        width: 7
                        height: 7
                        radius: 3.5
                        color: Theme.ok
                        SequentialAnimation on y {
                            loops: Animation.Infinite
                            running: root.delegating
                            NumberAnimation { to: -7; duration: Theme.msSlow; easing.type: Easing.OutQuad }
                            NumberAnimation { to: 0; duration: Theme.msSlow; easing.type: Easing.InQuad }
                        }
                    }
                }
            }

            // 新能力"飞入"提示条
            Rectangle {
                id: chip
                anchors.top: parent.bottom
                anchors.horizontalCenter: parent.horizontalCenter
                width: chipLabel.implicitWidth + 22
                height: 24
                radius: 12
                color: "#223ECF8E"
                border.width: 1
                border.color: Theme.ok
                opacity: 0
                Text {
                    id: chipLabel
                    anchors.centerIn: parent
                    color: Theme.ok
                    font.pixelSize: 11
                    text: "新能力已就位"
                }
                SequentialAnimation {
                    id: chipFly
                    PropertyAction { target: chip; property: "y"; value: -10 }
                    ParallelAnimation {
                        NumberAnimation { target: chip; property: "y"; to: 8; duration: 240; easing.type: Easing.OutBack }
                        NumberAnimation { target: chip; property: "opacity"; to: 1; duration: 200 }
                    }
                    PauseAnimation { duration: 1100 }
                    NumberAnimation { target: chip; property: "opacity"; to: 0; duration: 320 }
                }
            }
        }

        // ---------- 员工面板 ----------
        Loader {
            id: panelLoader
            anchors.fill: parent
            anchors.margins: 8
            source: "Panel.qml"
            visible: root.expanded
            opacity: root.expanded ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Theme.msNormal } }
            onLoaded: item.collapseRequested.connect(
                function () { root.shape = "ball"; })
        }

        Binding {
            target: panelLoader.item
            property: "feedModel"
            value: root.feeds[workbench.activeSpecialistId]
            when: panelLoader.item !== null
        }

        // 一次性提示：贴窗口底部，球态也看得见
        Rectangle {
            id: toast
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 10
            anchors.horizontalCenter: parent.horizontalCenter
            width: toastText.implicitWidth + 24
            height: 26
            radius: 13
            color: "#33F1666A"
            border.width: 1
            border.color: Theme.bad
            visible: root.notice !== ""
            Text {
                id: toastText
                anchors.centerIn: parent
                color: Theme.bad
                font.pixelSize: 11
                text: root.notice
            }
            Timer { id: toastTimer; interval: 4200; onTriggered: root.notice = "" }
            onVisibleChanged: if (visible) toastTimer.restart()
        }
    }

    Shortcut {
        sequences: ["Escape"]
        onActivated: root.collapse()
    }
}
