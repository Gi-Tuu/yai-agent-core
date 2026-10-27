// shell/desktop/qml/NebulaOrbCanvas.qml
// Qt Quick 2D Canvas implementation of the 4-state Orange Nebula & Clockwise Planet header
// States (phase):
//   "idle"     -> 未工作：星云不断向右飘（左深右浅）
//   "working"  -> 工作中：俯视同心圆行星凝聚，顺时针旋转 + 轨道轨迹（内密外疏、左深右浅）
//   "complete" -> 完成时：超新星冲击波爆发并平滑恢复为向右飘动的星云
//   "frozen"   -> 待确认：HITL 审批拦截，时间平滑减速至完全静止
//
// 渲染特征对齐 web_preview 的 nebula_engine.js：四色调色板、径向核心光晕、虚线旋转轨道、
// 顺时针拖尾弧、行星球渐变与暗环、呼吸透明度、纵向波动、超新星粒子爆发与双冲击波。
// 默认 260 粒 / 全状态 60fps；所有运动按真实 dt 积分，降帧只损失流畅度、不损失速度。
// 渲染后端是软件光栅（见 shell/desktop/app.py：RHI 会破坏置顶窗的逐像素透明），
// 所以每帧成本要自己压：预置配色串 + globalAlpha（免逐粒拼 rgba）、未吸积时跳过轨道三角函数。
import QtQuick
import QtQuick.Window

Item {
    id: root
    width: 440
    height: 52

    property string phase: "idle"     // "idle" | "working" | "complete" | "frozen"
    property bool isWide: true        // false in Ball (52x52), true in Capsule/Panel (440x52)
    // 粒子数：默认 260（与 web 演示同密度）。宿主可用 workbench.nebulaParticles 覆盖。
    property int particleCount: 260

    readonly property real coreX: 26
    readonly property real coreY: 26

    property real nebulaTime: 0.0
    property real timeScale: 1.0
    property real accretionBlend: 0.0
    property real planetCoreRadius: 0.0
    property real supernovaFlash: 0.0
    property real implosionCharge: 0.0
    property real implosionCountdown: 0.0
    property string lastPhase: "idle"
    property var shockwaves: []
    property var particles: []
    property var orbitRadii: [6.2, 9.8, 14.2, 19.5, 25.5]
    property double lastTick: 0.0

    // 左深橙 → 琥珀 → 右侧象牙白。预置成不透明色串：逐粒拼 "rgba(r,g,b,a)"
    // 每帧要拼 N 次再解析 N 次，改用 globalAlpha 表达同一透明度，绘制结果一致。
    readonly property var nebulaPalette: [
        "rgb(255, 72, 0)",
        "rgb(255, 112, 36)",
        "rgb(255, 162, 96)",
        "rgb(250, 244, 236)"
    ]

    Component.onCompleted: rebuildParticles()
    onParticleCountChanged: rebuildParticles()

    function rebuildParticles() {
        var count = Math.max(24, Math.min(1200, root.particleCount));
        var arr = [];
        // 实测：260 → 520 仍是 60fps，但单核 CPU 约 14% → 20%。粒子数会真花 CPU，
        // 只是不如帧率陡；默认值保持与 web 演示同密度。
        for (var i = 0; i < count; i++) {
            var u = (i + 0.5) / count;
            var driftX = 4 + Math.pow(Math.random(), 1.55) * 410;
            var spreadY = (Math.random() - 0.5) * 2.0;
            var isRider = (i % 4 === 0);
            var workingR = isRider
                ? (root.orbitRadii[i % root.orbitRadii.length] + (Math.random() - 0.5) * 0.7)
                : (4.6 + Math.pow(u, 2.35) * 24.0);
            arr.push({
                u: u,
                driftX: driftX,
                driftY: root.coreY + spreadY * (14 + (driftX / 440) * 8),
                driftVx: 0.28 + Math.random() * 0.48,
                driftPhase: Math.random() * 6.28,
                angle: (i / count) * Math.PI * 2 * 7,
                workingR: workingR,
                currentR: workingR,
                cwSpeed: 0.014 + 0.062 / Math.pow(Math.max(4.4, workingR), 0.68),
                isTrackRider: isRider,
                vx: 0,
                vy: 0,
                burstMix: 0,
                size: (u < 0.22) ? (0.42 + (i % 3) * 0.14) : (0.28 + (i % 4) * 0.12),
                baseAlpha: (u < 0.3) ? (0.65 + (1 - u) * 0.3) : (0.22 + (1 - u) * 0.45),
                x: driftX,
                y: root.coreY
            });
        }
        particles = arr;
    }

    function triggerSupernova() {
        root.supernovaFlash = 1.0;
        root.shockwaves = [
            { r: 3.5, maxR: 32.0, alpha: 0.92, width: 1.5 },
            { r: 2.0, maxR: 22.0, alpha: 0.60, width: 0.9 }
        ];
        for (var i = 0; i < root.particles.length; i++) {
            var p = root.particles[i];
            var angle = p.angle + (i % 5 - 2) * 0.08;
            var speed = (1.6 - p.u * 0.7) * (1.25 + (i % 6) * 0.28);
            p.vx = Math.cos(angle) * speed + 0.45;   // 略偏右，爆发后自然融回右飘星云
            p.vy = Math.sin(angle) * speed;
            p.burstMix = 1.0;
            p.driftX = Math.max(6, Math.min(420, root.coreX + Math.cos(angle) * p.workingR));
            p.driftY = Math.max(6, Math.min(46, root.coreY + Math.sin(angle) * p.workingR));
        }
    }

    Timer {
        // Item.visible 不反映所属窗口隐藏或最小化，必须同时检查窗口。
        // 空闲同样 60fps：星云一直在飘，降帧会让左深右浅的漂移出现顿挫。
        interval: 16
        running: root.visible && root.Window.window !== null
                 && root.Window.window.visible
                 && root.Window.window.visibility !== Window.Minimized
                 && !(root.phase === "frozen" && root.timeScale <= 0.002)
        repeat: true
        onTriggered: orbCanvas.requestPaint()
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

            // 真实 dt：降帧只影响流畅度，速度按墙钟时间推进。
            var now = Date.now();
            var dt = root.lastTick > 0 ? (now - root.lastTick) / 1000 : 0.016;
            root.lastTick = now;
            if (dt > 0.05) dt = 0.05;
            if (dt < 0) dt = 0;
            var f = dt * 60.0;   // 以 60fps 为 1 的帧当量

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

            // 完成态：先坍缩蓄力，再爆发冲击波。
            if (root.phase !== root.lastPhase) {
                if (root.phase === "complete") {
                    if (root.accretionBlend < 0.55) {
                        root.implosionCharge = 1.0;
                        root.implosionCountdown = 0.32;
                    } else {
                        root.triggerSupernova();
                    }
                }
                root.lastPhase = root.phase;
            }

            // 1. 时间缩放：冻结时平滑减速至完全静止
            var targetTS = (root.phase === "frozen") ? 0.0 : 1.0;
            root.timeScale += (targetTS - root.timeScale) * (1 - Math.pow(1 - 0.15, f));
            if (root.timeScale < 0.002) root.timeScale = 0;
            root.nebulaTime += dt * root.timeScale;

            // 2. 吸积混合：坍缩瞬间快速成星，完成时缓慢退回星云
            var targetAcc = 0.0;
            if (root.phase === "working" || root.implosionCharge > 0) {
                targetAcc = 1.0;
            } else if (root.phase === "frozen") {
                targetAcc = root.accretionBlend > 0.3 ? root.accretionBlend : 0.85;
            }
            var blendRate = (root.implosionCharge > 0) ? 0.18 : (root.phase === "complete" ? 0.065 : 0.05);
            root.accretionBlend += (targetAcc - root.accretionBlend)
                * (1 - Math.pow(1 - blendRate, f)) * Math.max(0.15, root.timeScale);

            if (root.implosionCharge > 0) {
                root.implosionCountdown -= dt;
                if (root.implosionCountdown <= 0) {
                    root.implosionCharge = 0.0;
                    root.triggerSupernova();
                }
            }

            // 3. 左深右浅体积辉光（呼吸）
            var breath = Math.sin(root.nebulaTime * 2.0) * 0.03;
            var leftDeepAlpha = Math.min(0.65, 0.24 + root.accretionBlend * 0.14 + root.supernovaFlash * 0.32 + breath);
            var grad = ctx.createLinearGradient(0, 0, root.isWide ? 260 : 52, 0);
            grad.addColorStop(0, "rgba(255, 79, 0, " + leftDeepAlpha + ")");
            grad.addColorStop(0.28, "rgba(255, 110, 35, " + (leftDeepAlpha * 0.55) + ")");
            grad.addColorStop(0.65, "rgba(255, 150, 80, " + (leftDeepAlpha * 0.16) + ")");
            grad.addColorStop(1, "rgba(17, 17, 18, 0)");
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, root.isWide ? 260 : 52, height);

            // 4. 核心径向光晕
            var coreGlow = ctx.createRadialGradient(root.coreX, root.coreY, 1, root.coreX, root.coreY, 25);
            coreGlow.addColorStop(0, "rgba(255, 125, 45, " + (0.22 + root.accretionBlend * 0.24 + root.supernovaFlash * 0.35) + ")");
            coreGlow.addColorStop(0.55, "rgba(255, 79, 0, " + (0.08 + root.accretionBlend * 0.06) + ")");
            coreGlow.addColorStop(1, "rgba(17, 17, 18, 0)");
            ctx.fillStyle = coreGlow;
            ctx.beginPath();
            ctx.arc(root.coreX, root.coreY, 25, 0, Math.PI * 2);
            ctx.fill();

            // 5. 俯视行星轨道（内密外疏，奇数道虚线顺时针流动）
            if (root.accretionBlend > 0.04) {
                for (var t = 0; t < root.orbitRadii.length; t++) {
                    var tr = root.orbitRadii[t];
                    var ta = (1 - t / root.orbitRadii.length) * 0.26 * root.accretionBlend;
                    ctx.save();
                    ctx.strokeStyle = "rgba(255, 130, 60, " + ta + ")";
                    ctx.lineWidth = (t === 0 || t === 1) ? 0.65 : 0.45;
                    if (t % 2 === 1) {
                        ctx.setLineDash([4, 3]);
                        ctx.lineDashOffset = -root.nebulaTime * 12 * (t + 1);
                    }
                    ctx.beginPath();
                    ctx.arc(root.coreX, root.coreY, tr, 0, Math.PI * 2);
                    ctx.stroke();
                    ctx.restore();
                }
            }

            // 6. 星尘粒子：空闲向右飘，工作顺时针公转
            var maxVisibleX = root.isWide ? 422 : 52;
            for (var i = 0; i < root.particles.length; i++) {
                var p = root.particles[i];
                if (root.timeScale > 0) {
                    p.driftX += p.driftVx * root.timeScale * f;
                    var waveY = Math.sin(root.nebulaTime * 1.6 + p.driftPhase + p.driftX * 0.04) * 0.22;
                    p.driftY += waveY * root.timeScale * f;
                    if (p.driftX > 310) {
                        p.driftX = 4 + Math.random() * 14;
                        p.driftY = root.coreY + (Math.random() - 0.5) * 26;
                    }
                    p.angle += p.cwSpeed * root.timeScale * f;
                    p.currentR += (p.workingR - p.currentR) * (1 - Math.pow(1 - 0.1, f)) * root.timeScale;
                    if (p.burstMix > 0.005) {
                        p.driftX += p.vx * root.timeScale * f;
                        p.driftY += p.vy * root.timeScale * f;
                        p.vx *= Math.pow(0.92, f);
                        p.vy *= Math.pow(0.92, f);
                        p.burstMix *= Math.pow(0.93, f);
                    } else {
                        p.burstMix = 0;
                    }
                }

                var tailStretchX = (root.isWide && p.u > 0.55) ? ((p.u - 0.55) * 105 * root.accretionBlend) : 0;
                if (root.accretionBlend > 0.001) {
                    var ox = root.coreX + Math.cos(p.angle) * p.currentR + tailStretchX;
                    var oy = root.coreY + Math.sin(p.angle) * p.currentR;
                    p.x = p.driftX * (1 - root.accretionBlend) + ox * root.accretionBlend;
                    p.y = p.driftY * (1 - root.accretionBlend) + oy * root.accretionBlend;
                } else {
                    // 未吸积时位置就是漂移位置，省掉每粒两次三角函数。
                    p.x = p.driftX;
                    p.y = p.driftY;
                }

                if (p.x > maxVisibleX) continue;

                // 左深右浅：位置与半径共同决定亮度衰减
                var xNorm = Math.max(0, Math.min(1, (p.x - 6) / (root.isWide ? 250 : 44)));
                var rNorm = Math.max(0, Math.min(1, p.currentR / 26.0));
                var fadeFactor = (1 - root.accretionBlend) * Math.pow(1 - xNorm, 1.45)
                    + root.accretionBlend * Math.pow(1 - rNorm * 0.72, 1.35) * Math.pow(1 - xNorm * 0.65, 1.2);
                var alpha = Math.min(0.95, Math.max(0.04, p.baseAlpha * fadeFactor + root.supernovaFlash * 0.25));

                // 配色：左侧深橙、中段琥珀、右侧与内圈偏象牙白
                var colIdx = 0;
                if (root.accretionBlend > 0.5 && p.u < 0.16) {
                    colIdx = 3;
                } else if (xNorm > 0.55) {
                    colIdx = 2;
                } else if (xNorm > 0.25) {
                    colIdx = 1;
                }

                // 顺时针拖尾弧
                if (root.accretionBlend > 0.25 && p.isTrackRider && p.currentR < 24) {
                    var trailArcLen = 0.28 * root.accretionBlend;
                    ctx.globalAlpha = alpha * 0.55;
                    ctx.strokeStyle = root.nebulaPalette[colIdx];
                    ctx.lineWidth = p.size * 0.85;
                    ctx.beginPath();
                    ctx.arc(root.coreX + tailStretchX, root.coreY, p.currentR, p.angle - trailArcLen, p.angle);
                    ctx.stroke();
                }

                ctx.globalAlpha = alpha;
                ctx.fillStyle = root.nebulaPalette[colIdx];
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.globalAlpha = 1;

            // 7. 俯视行星核：球面渐变 + 与内圈分离的暗环
            var targetR = (root.accretionBlend > 0.08) ? (3.6 * root.accretionBlend) : 0;
            if (root.timeScale > 0) {
                root.planetCoreRadius += (targetR - root.planetCoreRadius) * (1 - Math.pow(1 - 0.12, f)) * root.timeScale;
                root.supernovaFlash *= Math.pow(0.89, f * root.timeScale);
            }
            if (root.planetCoreRadius > 0.25 || root.supernovaFlash > 0.04) {
                var pr = Math.max(0.4, root.planetCoreRadius + root.supernovaFlash * 2.4);
                ctx.strokeStyle = "rgba(17, 17, 18, 0.65)";
                ctx.lineWidth = 1.3;
                ctx.beginPath();
                ctx.arc(root.coreX, root.coreY, pr + 1.0, 0, Math.PI * 2);
                ctx.stroke();

                var planetGrad = ctx.createLinearGradient(root.coreX - pr, root.coreY, root.coreX + pr, root.coreY);
                planetGrad.addColorStop(0, "#C83200");
                planetGrad.addColorStop(0.45, "#FF4F00");
                planetGrad.addColorStop(0.85, "#FF9E5E");
                planetGrad.addColorStop(1, "#FFF8F0");
                ctx.fillStyle = planetGrad;
                ctx.beginPath();
                ctx.arc(root.coreX, root.coreY, pr, 0, Math.PI * 2);
                ctx.fill();
            }

            // 8. 超新星冲击波
            if (root.shockwaves.length > 0) {
                var alive = [];
                for (var s = root.shockwaves.length - 1; s >= 0; s--) {
                    var sw = root.shockwaves[s];
                    if (root.timeScale > 0) {
                        sw.r += (sw.maxR - sw.r) * (1 - Math.pow(1 - 0.12, f)) * root.timeScale;
                        sw.alpha *= Math.pow(0.91, f * root.timeScale);
                    }
                    if (sw.alpha > 0.015) {
                        alive.push(sw);
                        ctx.strokeStyle = "rgba(255, 170, 105, " + sw.alpha + ")";
                        ctx.lineWidth = sw.width;
                        ctx.beginPath();
                        ctx.arc(root.coreX, root.coreY, sw.r, 0, Math.PI * 2);
                        ctx.stroke();
                    }
                }
                root.shockwaves = alive;
            }

            // 9. 待确认：静止锁定环
            if (root.timeScale < 0.35) {
                var lockAlpha = (1 - root.timeScale / 0.35) * 0.7;
                ctx.strokeStyle = "rgba(255, 79, 0, " + lockAlpha + ")";
                ctx.lineWidth = 1.0;
                ctx.setLineDash([3, 3]);
                ctx.beginPath();
                ctx.arc(root.coreX, root.coreY, 22.5, 0, Math.PI * 2);
                ctx.stroke();
                ctx.setLineDash([]);
            }

            ctx.restore();
        }
    }
}
