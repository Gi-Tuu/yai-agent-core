// shell/desktop/qml/NebulaOrbCanvas.qml
// Drop-in Qt Quick 2D Canvas implementation of the 4-state Orange Nebula & Clockwise Planet header
// States (phase):
//   "idle"     -> 未工作：星云不断向右飘（左深右浅）
//   "working"  -> 工作中：俯视同心圆行星凝聚，顺时针旋转 + 轨道轨迹（内密外疏、左深右浅）
//   "complete" -> 完成时：超新星冲击波爆发并平滑恢复为向右飘动的星云
//   "frozen"   -> 待确认：HITL 审批拦截，时间平滑减速至完全静止
import QtQuick

Item {
    id: root
    width: 440
    height: 52

    property string phase: "idle"     // "idle" | "working" | "complete" | "frozen"
    property bool isWide: true        // false in Ball (52x52), true in Capsule/Panel (440x52)

    readonly property real coreX: 26
    readonly property real coreY: 26

    property real nebulaTime: 0.0
    property real timeScale: 1.0
    property real accretionBlend: 0.0
    property real planetCoreRadius: 0.0
    property real supernovaFlash: 0.0
    property string lastPhase: "idle"
    property var particles: []
    property var orbitRadii: [6.2, 9.8, 14.2, 19.5, 25.5]

    Component.onCompleted: {
        var arr = [];
        // 常驻桌面：空闲只保留下沉漂移，粒子数压低；工作态再提高帧率。
        var count = 120;
        for (var i = 0; i < count; i++) {
            var u = (i + 0.5) / count;
            var driftX = 4 + Math.pow(Math.random(), 1.55) * 410;
            var driftY = coreY + (Math.random() - 0.5) * 28;
            var isRider = (i % 4 === 0);
            var workingR = isRider
                ? orbitRadii[i % orbitRadii.length]
                : (4.6 + Math.pow(u, 2.35) * 24.0);
            arr.push({
                u: u,
                driftX: driftX,
                driftY: driftY,
                driftVx: 0.28 + Math.random() * 0.48,
                driftPhase: Math.random() * 6.28,
                angle: u * 44.0,
                workingR: workingR,
                currentR: workingR,
                cwSpeed: 0.014 + 0.062 / Math.pow(Math.max(4.4, workingR), 0.68),
                isTrackRider: isRider,
                size: (u < 0.22) ? 0.55 : 0.36,
                baseAlpha: (u < 0.3) ? (0.65 + (1 - u) * 0.3) : (0.22 + (1 - u) * 0.45),
                x: driftX,
                y: driftY
            });
        }
        particles = arr;
    }

    Timer {
        // 工作中 60fps；空闲/完成 20fps；冻结减速到 0 后彻底停画。
        interval: root.phase === "working" ? 16 : 50
        running: root.visible && !(root.phase === "frozen" && root.timeScale <= 0.002)
        repeat: true
        onTriggered: orbCanvas.requestPaint()
    }

    // 渐变交给 QML 圆角矩形：Canvas 的 fillRect 在胶囊左端会露出直角。
    Rectangle {
        anchors.fill: parent
        radius: root.isWide ? height / 2 : width / 2
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "#47FF4F00" }
            GradientStop { position: 0.4; color: "#1FFF6E23" }
            GradientStop { position: 1.0; color: "#00111112" }
        }
    }

    Canvas {
        id: orbCanvas
        anchors.fill: parent
        antialiasing: true
        renderTarget: Canvas.FramebufferObject
        renderStrategy: Canvas.Cooperative

        onPaint: {
            var ctx = getContext("2d");
            ctx.clearRect(0, 0, width, height);
            // 星云必须裁进壳层：球/面板是圆，胶囊是左右全圆角的横向药丸。
            ctx.save();
            ctx.beginPath();
            if (!root.isWide) {
                ctx.arc(root.coreX, root.coreY, 25, 0, Math.PI * 2);
            } else {
                var rr = Math.min(26, height / 2);
                ctx.moveTo(rr, 0);
                ctx.lineTo(width - rr, 0);
                ctx.quadraticCurveTo(width, 0, width, rr);
                ctx.lineTo(width, height - rr);
                ctx.quadraticCurveTo(width, height, width - rr, height);
                ctx.lineTo(rr, height);
                ctx.quadraticCurveTo(0, height, 0, height - rr);
                ctx.lineTo(0, rr);
                ctx.quadraticCurveTo(0, 0, rr, 0);
                ctx.closePath();
            }
            ctx.clip();

            if (root.phase !== root.lastPhase) {
                if (root.phase === "complete") root.supernovaFlash = 1.0;
                root.lastPhase = root.phase;
            }

            var targetTS = (root.phase === "frozen") ? 0.0 : 1.0;
            root.timeScale += (targetTS - root.timeScale) * 0.15;
            if (root.timeScale < 0.002) root.timeScale = 0;
            root.nebulaTime += 0.016 * root.timeScale;

            var targetAcc = (root.phase === "working") ? 1.0 : (root.phase === "frozen" ? Math.max(root.accretionBlend, 0.85) : 0.0);
            root.accretionBlend += (targetAcc - root.accretionBlend) * 0.06 * Math.max(0.15, root.timeScale);

            // 渐变由外层 QML Rectangle 负责；这里只画轨道、粒子和行星核心。

            // 2. Top-Down Orbital Tracks
            if (root.accretionBlend > 0.04) {
                for (var t = 0; t < root.orbitRadii.length; t++) {
                    var tr = root.orbitRadii[t];
                    var ta = (1 - t / root.orbitRadii.length) * 0.25 * root.accretionBlend;
                    ctx.strokeStyle = "rgba(255, 130, 60, " + ta + ")";
                    ctx.lineWidth = 0.55;
                    ctx.beginPath();
                    ctx.arc(root.coreX, root.coreY, tr, 0, Math.PI * 2);
                    ctx.stroke();
                }
            }

            // 3. Particles (Rightward Drift in Idle, Clockwise Rotation in Working)
            var limitX = root.isWide ? 310 : 52;
            for (var i = 0; i < root.particles.length; i++) {
                var p = root.particles[i];
                if (root.timeScale > 0) {
                    p.driftX += p.driftVx * root.timeScale;
                    if (p.driftX > limitX) {
                        p.driftX = 4 + Math.random() * 14;
                        p.driftY = root.coreY + (Math.random() - 0.5) * 26;
                    }
                    p.angle += p.cwSpeed * root.timeScale; // Clockwise
                }
                var ox = root.coreX + Math.cos(p.angle) * p.currentR;
                var oy = root.coreY + Math.sin(p.angle) * p.currentR;
                p.x = p.driftX * (1 - root.accretionBlend) + ox * root.accretionBlend;
                p.y = p.driftY * (1 - root.accretionBlend) + oy * root.accretionBlend;

                var xNorm = Math.max(0, Math.min(1, (p.x - 6) / (root.isWide ? 250 : 44)));
                var alpha = Math.max(0.05, p.baseAlpha * Math.pow(1 - xNorm, 1.35));
                ctx.fillStyle = "rgba(255, 112, 36, " + alpha + ")";
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                ctx.fill();
            }

            // 4. Central Planet Core
            var targetR = (root.accretionBlend > 0.08) ? (3.6 * root.accretionBlend) : 0;
            root.planetCoreRadius += (targetR - root.planetCoreRadius) * 0.12;
            if (root.planetCoreRadius > 0.25) {
                ctx.fillStyle = "#FF4F00";
                ctx.beginPath();
                ctx.arc(root.coreX, root.coreY, root.planetCoreRadius, 0, Math.PI * 2);
                ctx.fill();
            }

            // 完成态：一次向外扩散的橙色冲击波，然后回到向右飘散的星云。
            if (root.supernovaFlash > 0.01) {
                var ringR = 6 + (1 - root.supernovaFlash) * 30;
                ctx.strokeStyle = "rgba(255, 122, 52, " + (root.supernovaFlash * 0.55) + ")";
                ctx.lineWidth = 1.2;
                ctx.beginPath();
                ctx.arc(root.coreX, root.coreY, ringR, 0, Math.PI * 2);
                ctx.stroke();
                root.supernovaFlash *= 0.92;
            }
            ctx.restore();
        }
    }
}
