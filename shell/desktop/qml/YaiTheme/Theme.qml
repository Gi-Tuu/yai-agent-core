pragma Singleton
import QtQuick

// 桌面端唯一配色 / 字体 / 动效口径来源：Terminal Craft（终端工艺）。
// 近黑底 + 单一琥珀 + JetBrains Mono，与网页宿主 :root 的 --term-* token 对齐。
// 换主题只改这里。
QtObject {
    // 尺寸
    readonly property int ballSize: 56
    readonly property int capW: 340
    readonly property int capH: 52
    readonly property int panelW: 470
    readonly property int panelH: 620
    readonly property int topMargin: 10
    // 按下后移动超过这个距离算拖动，否则算点击
    readonly property int dragSlop: 4

    // 字体：拉丁 / 数字用 JetBrains Mono（app.py 注册随包 ttf，离线可用）；
    // 中文缺字由 Qt 自动回退到系统中文 sans，无需另配。
    readonly property string fontMono: "JetBrains Mono"

    // 圆角
    readonly property int rInner: 4
    readonly property int rCard: 6
    readonly property int rPanel: 8

    // 配色（深色终端，贴近工业仪表观感）
    // 壳层不透明：桌面上开着别的窗口时，半透明会让对话"看起来是空的"
    readonly property color shell: "#0A0B0D"         // term-base 近黑
    readonly property color raised: "#121419"        // term-surface
    readonly property color raised2: "#181B21"       // term-surface-2
    readonly property color edge: "#14FFFFFF"        // term-hairline（白 8%）
    readonly property color edgeStrong: "#24FFFFFF"  // term-hairline-strong（白 14%）
    readonly property color text: "#E7EAF0"          // term-text
    readonly property color dim: "#98A0AE"           // term-text-2
    readonly property color mute: "#5E6673"          // term-text-muted
    readonly property color accent: "#E5A23B"        // 琥珀：唯一强调色
    readonly property color accentBright: "#F5B54D"
    readonly property color accentInk: "#1A1205"     // 琥珀底上的深棕黑文字
    readonly property color accentTint: "#1FE5A23B"  // 琥珀 12%（发现 / 创造 chip）
    readonly property color ok: "#6FCF8B"            // 终端绿（降饱和）
    readonly property color warn: "#E0B341"          // 终端黄（全审 / 警告）
    readonly property color warnTint: "#24E0B341"    // 黄 14%（授权卡 / 离线提示条）
    readonly property color bad: "#EE6E6E"           // 终端红
    readonly property color badTint: "#2EEE6E6E"     // 红 18%（出错 toast）
    readonly property color idleDot: "#5E6673"       // 空闲状态点（= mute）
    readonly property color knob: "#E7EAF0"          // 开关旋钮（近白）

    // 动效时长
    readonly property int msFast: 160
    readonly property int msNormal: 240
    readonly property int msSlow: 420
}
