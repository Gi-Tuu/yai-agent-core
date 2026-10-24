pragma Singleton
import QtQuick

// 桌面端唯一配色 / 字体 / 动效口径来源：Optical Obsidian + Braun Signal Orange。
// 保留原 Terminal Craft 的运行时 token（字体、时长、状态色），只替换视觉语言，
// 避免其他 QML 文件因缺 token 编译失败。
QtObject {
    // 三态正交尺寸：球 52 → 胶囊 440×52（向右）→ 面板 440×536（向下）
    readonly property int ballSize: 52
    readonly property int capW: 440
    readonly property int capH: 52
    readonly property int panelW: 440
    readonly property int panelH: 536
    readonly property int topMargin: 10
    // 按下后移动超过这个距离算拖动，否则算点击
    readonly property int dragSlop: 4

    // 字体：拉丁 / 数字用随包 JetBrains Mono（离线可用）；中文回退系统 sans。
    readonly property string fontMono: "JetBrains Mono"

    // 圆角：面板 26 / 卡片 14 / 内元素 10
    readonly property int rInner: 10
    readonly property int rCard: 14
    readonly property int rPanel: 26

    // 光学哑光黑曜石表面：必须不透明，否则桌面上其他窗口的文字会透进对话。
    readonly property color shell: "#111112"
    readonly property color raised: "#1A1A1C"
    readonly property color raised2: "#222225"

    // 暖编辑体文字 + 单一信号橙强调
    readonly property color text: "#F4F2ED"
    readonly property color dim: "#8E8D8A"
    readonly property color mute: "#5E5D59"
    readonly property color accent: "#FF4F00"
    readonly property color accentBright: "#FF6A2B"
    readonly property color accentInk: "#FFFFFF"
    readonly property color accentTint: "#1FFF4F00"

    // 语义状态
    readonly property color ok: "#10B981"
    readonly property color warn: "#FF4F00"
    readonly property color bad: "#EF4444"
    readonly property color warnTint: "#1FFF4F00"
    readonly property color badTint: "#2EEF4444"
    readonly property color idleDot: "#5E5D59"
    readonly property color knob: "#F4F2ED"

    // 1px 内外高光边
    readonly property color edge: "#1CFFFFFF"
    readonly property color edgeStrong: "#2EFFFFFF"

    // 动效时长（保留原节奏）
    readonly property int msFast: 160
    readonly property int msNormal: 240
    readonly property int msSlow: 420
}
