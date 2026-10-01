pragma Singleton
import QtQuick

// 桌面端唯一配色与动效口径来源：换主题只改这里。
QtObject {
    // 尺寸
    readonly property int ballSize: 56
    readonly property int capW: 340
    readonly property int capH: 52
    readonly property int panelW: 420
    readonly property int panelH: 620
    readonly property int topMargin: 10
    // 按下后移动超过这个距离算拖动，否则算点击
    readonly property int dragSlop: 4

    // 配色（深色胶囊，贴近系统灵动岛观感）
    // 壳层不透明：桌面上开着别的窗口时，半透明会让对话"看起来是空的"
    readonly property color shell: "#FF12141A"
    readonly property color raised: "#242A38"
    readonly property color edge: "#3350607A"
    readonly property color text: "#E9ECF4"
    readonly property color dim: "#8A93A6"
    readonly property color accent: "#7C5CFF"
    readonly property color ok: "#3ECF8E"
    readonly property color warn: "#F5A524"
    readonly property color bad: "#F1666A"
    readonly property color idleDot: "#4A5164"

    // 动效时长
    readonly property int msFast: 160
    readonly property int msNormal: 240
    readonly property int msSlow: 420
}
