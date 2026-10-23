"""为《YAI Agent Core 技术白皮书》生成学术风格架构图（PNG）。

运行：
  .venv/Scripts/python.exe docs/competitions/shanghai/draw_figs.py

产物：docs/competitions/shanghai/figs/fig*.png
仅用于文档构建，不进入内核依赖；matplotlib 为文档工具依赖。
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon

# ---------------------------------------------------------------- 全局样式
for cand in ("Microsoft YaHei", "SimHei", "SimSun"):
    if any(cand in f.name for f in fm.fontManager.ttflist):
        plt.rcParams["font.family"] = cand
        break
plt.rcParams["axes.unicode_minus"] = False

# 学术配色
INK = "#1F3A5F"      # 深蓝（主/轮廓）
PAPER = "#FFFFFF"    # 底
TINT = "#E9EFF6"     # 浅蓝填充
TINT2 = "#F3F6FA"    # 更浅
ACCENT = "#C26A1E"   # 赭橙（强调/缺口）
GREEN = "#3E6B4F"    # 墨绿（成功/落地）
GREY = "#66707C"     # 中灰
LINE = "#33506E"

OUT = Path(__file__).resolve().parent / "figs"
OUT.mkdir(parents=True, exist_ok=True)


def box(ax, x, y, w, h, text, fc=TINT, ec=INK, fs=12, bold=False, tc="#16263C",
        rounded=0.02, lw=1.4, ls="-"):
    p = FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0.005,rounding_size={rounded}",
        fc=fc, ec=ec, lw=lw, linestyle=ls, zorder=2,
    )
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=tc, zorder=3,
            fontweight="bold" if bold else "normal", linespacing=1.35)
    return p


def arrow(ax, p1, p2, color=LINE, lw=1.6, style="-|>", ls="-", rad=0.0, ms=13):
    a = FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=ms, lw=lw,
                        color=color, linestyle=ls,
                        connectionstyle=f"arc3,rad={rad}", zorder=1)
    ax.add_patch(a)
    return a


def diamond(ax, cx, cy, w, h, text, fc="#FBF3E8", ec=ACCENT, fs=11):
    pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
    ax.add_patch(Polygon(pts, closed=True, fc=fc, ec=ec, lw=1.5, zorder=2))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, color="#5A3413",
            zorder=3, linespacing=1.3)


def band(ax, x, y, w, h, label, fc, ec, fs=11.5):
    """左侧竖向标签的横向分层带。"""
    p = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.005,rounding_size=0.015",
                       fc=fc, ec=ec, lw=1.3, zorder=1, alpha=0.55)
    ax.add_patch(p)
    ax.text(x + 1.2, y + h - 2.4, label, ha="left", va="top", fontsize=fs,
            color=ec, fontweight="bold", zorder=3)


def new_fig(w=11, h=6.4):
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    fig.patch.set_facecolor(PAPER)
    return fig, ax


def save(fig, name):
    fig.savefig(OUT / name, bbox_inches="tight", facecolor=PAPER, pad_inches=0.15)
    plt.close(fig)
    print("saved:", name)


# ================================================================ 图1 定位对比
def fig_position():
    fig, ax = new_fig(11, 6.6)
    cols = [
        ("托管平台", "可视化编排 · 托管运行", "应用进入平台运行\n数据 / UI / 发布在平台侧\n存量软件无法内嵌", TINT2, GREY),
        ("拼装框架", "通用编排 · 图运行时 · 多智能体", "提供积木与范式\n谁是 Agent、有哪些工具\n仍需开发者逐一定义装配", TINT2, GREY),
        ("YAI 内核", "进程内嵌入式（类 SQLite）", "内核进入应用进程\n宿主声明能力\n其余由内核自适应完成", TINT, INK),
    ]
    x0, w, gap = 4, 28, 4
    for i, (head, sub, body, fc, ec) in enumerate(cols):
        x = x0 + i * (w + gap)
        bold = i == 2
        box(ax, x, 72, w, 14, head, fc=fc, ec=ec, fs=15, bold=True, lw=2.2 if bold else 1.4)
        box(ax, x, 60, w, 10, sub, fc=PAPER, ec=ec, fs=11, tc=GREY)
        box(ax, x, 30, w, 28, body, fc=fc, ec=ec, fs=12.5, bold=bold)
    ax.text(50, 20, "差别不在功能多少，而在交付形态：是“应用迁就 AI”，还是“AI 进入应用”。",
            ha="center", fontsize=12.5, color=ACCENT, fontweight="bold")
    ax.text(50, 13, "YAI 对标数据库世界里的 SQLite：一个可被直接 import 的引擎，而非 ORM，也非云数据库控制台。",
            ha="center", fontsize=11, color=GREY)
    save(fig, "fig1_position.png")


# ================================================================ 图2 分层架构（v0.9）
def fig_architecture():
    fig, ax = new_fig(11.4, 10.2)

    # 宿主层
    box(ax, 6, 89.5, 88, 7.5,
        "宿主软件与产品壳　host_a 笔记 · host_c 陪伴 · host_d MCP · host_e CRM · host_f 发现\n"
        "host_g 沙箱 · host_i Mealie · host_j Actual 记账 · 灵动岛桌面壳 · AMBRACE（规划回流）",
        fc=TINT2, ec=GREY, fs=10.6, bold=True)
    ax.text(50, 86.4,
            "只声明 capabilities（普通函数 + 类型注解 + 一行 docstring），选择 model / policy / memory；内核是唯一嵌入点，可一键开关对比",
            ha="center", fontsize=9.2, color=GREY)

    # Core 外框
    outer = FancyBboxPatch((6, 21), 88, 63, boxstyle="round,pad=0.01,rounding_size=0.018",
                           fc="none", ec=INK, lw=2.2, zorder=2)
    ax.add_patch(outer)
    ax.text(8, 81.6, "YAI Agent Core（进程内库 · 内核零第三方硬依赖 dependencies = []）",
            fontsize=12, color=INK, fontweight="bold")

    groups = [
        ("自适应决策", ["Router 四策略", "Bandit 在线自校准", "缺口 · replan · 选档"]),
        ("执行与工具", ["AgentLoop / ReAct", "两层目录 · 结果收口", "组合 composer · 代码工具"]),
        ("发现与接入", ["函数内省注册", "MCP · OpenAPI 总线", "远程发现 · need 筛选"]),
        ("记忆与上下文", ["SQLite 会话记忆", "Vault 长期记忆 · 主动召回", "上下文压缩 · 小模型"]),
        ("运行时保障", ["Budget 预算熔断", "并发串行锁 · 超时取消", "诊断 SPI · 在线热备份"]),
        ("可观测性", ["19 类事件唯一出口", "Metrics · /readyz", "OTel tracing（可选）"]),
    ]
    gx, gw, ggap = 8, 27, 2.5
    xs = [gx, gx + gw + ggap, gx + 2 * (gw + ggap)]
    head_y = [72.5, 50.5]
    for i, (head, items) in enumerate(groups):
        col, row = i % 3, i // 3
        x = xs[col]
        hy = head_y[row]
        box(ax, x, hy, gw, 4.6, head, fc=INK, ec=INK, fs=11, bold=True, tc="white")
        for j, it in enumerate(items):
            iy = hy - 4.9 - j * 4.7
            box(ax, x, iy, gw, 4.2, it, fc=TINT, ec=LINE, fs=9.6)

    # SPI 十契约横条
    box(ax, 8, 23.2, 84, 6.4,
        "SPI 十契约（全部可替换）　model · channel · memory · policy · discovery · sandbox\n"
        "learning（路由自校准）· embedding（语义）· cipher（落盘加密）· resource（文件资源）",
        fc="#FBF3E8", ec=ACCENT, fs=10.2, bold=True, tc="#5A3413")

    # 外部可选后端
    backs = ["模型后端\nOpenAI 兼容", "MCP\nServer", "OpenAPI\n服务",
             "Embedding\n本地 bge / 云", "沙箱\n子进程 · Docker", "OTLP\nPrometheus"]
    bx, bw, bg = 8, 12.8, 1.6
    for i, b in enumerate(backs):
        x = bx + i * (bw + bg)
        box(ax, x, 10.5, bw, 7.2, b, fc=PAPER, ec=GREY, fs=9.4, tc=GREY)
        arrow(ax, (x + bw / 2, 21), (x + bw / 2, 17.8), color=GREY, lw=1.1, ls=(0, (3, 3)))
    ax.text(50, 6.6, "外部能力全部为可选 extra 且懒加载：不装不引、不配置不启用，内核本体始终零硬依赖。",
            ha="center", fontsize=9.4, color=GREY)
    save(fig, "fig2_architecture.png")


# ================================================================ 图3 路由四策略
def fig_router():
    fig, ax = new_fig(11, 6.8)
    box(ax, 6, 70, 20, 12, "用户任务", fc=TINT2, ec=GREY, fs=13, bold=True)
    diamond(ax, 42, 76, 26, 20, "自适应路由\nLLM → 规则兜底\n（bandit 在线学习）")
    arrow(ax, (26, 76), (29, 76), color=INK, lw=1.8)

    strats = [
        ("direct", "无需工具\n模型直接回答", TINT2),
        ("react", "ReAct 循环\n边推理边调工具", TINT),
        ("plan", "先产出有序计划\n可在执行中 replan", TINT),
        ("clarify", "意图不清则反问\n轮数上限不循环", "#FBF3E8"),
    ]
    sx, sw, gap = 6, 20, 2.7
    for i, (name, desc, fc) in enumerate(strats):
        x = sx + i * (sw + gap)
        box(ax, x, 40, sw, 10, name, fc=INK, ec=INK, fs=13, bold=True, tc="white")
        box(ax, x, 20, sw, 18, desc, fc=fc, ec=LINE, fs=11)
        arrow(ax, (42, 66), (x + sw / 2, 50.5), color=LINE, lw=1.4,
              rad={0: 0.15, 1: 0.05, 2: -0.05, 3: -0.15}[i])
    ax.text(50, 11,
            "每次决策都在事件流中标明 source = llm | rules | bandit，可审计、可复现；模型不可用时规则路径保证下限。",
            ha="center", fontsize=10.8, color=GREY)
    save(fig, "fig3_router.png")


# ================================================================ 图4 能力缺口闭环
def fig_discovery():
    fig, ax = new_fig(11, 5.8)
    steps = [
        ("感知缺口", "capability_missing\n（缺口 + 工具清单）", "#FBF3E8", ACCENT),
        ("请求发现", "ToolDiscovery\n按 need 语义筛选候选", TINT, LINE),
        ("去重注册", "tool_discovered\n注册进工具表", TINT, LINE),
        ("本轮调用", "同一轮交给模型\n新工具立即使用", TINT, GREEN),
    ]
    x, w, gap = 5, 20, 6.7
    for i, (head, body, fc, ec) in enumerate(steps):
        px = x + i * (w + gap)
        box(ax, px, 52, w, 11, head, fc=ec, ec=ec, fs=13, bold=True, tc="white")
        box(ax, px, 30, w, 19, body, fc=fc, ec=ec, fs=11)
        if i < 3:
            arrow(ax, (px + w, 40), (px + w + gap, 40), color=INK, lw=2)
    ax.text(50, 20, "不需要等下一次任务：缺口在“同一轮”被补上并使用。",
            ha="center", fontsize=12, color=GREEN, fontweight="bold")
    ax.text(50, 12.5,
            "召回为词法 + 语义双通道（远程源按 need 保守收窄、不中回退全量）；发现源异常只发诊断、不中断主流程；“能被发现”不等于“被授权执行”。",
            ha="center", fontsize=10.2, color=GREY)
    save(fig, "fig4_discovery.png")


# ================================================================ 图5 工具总线与权限
def fig_toolbus():
    fig, ax = new_fig(11, 6.6)
    srcs = ["Native 函数", "MCP Server", "OpenAPI 服务"]
    for i, s in enumerate(srcs):
        box(ax, 6, 72 - i * 13, 20, 9, s, fc=TINT2, ec=GREY, fs=11.5)
        arrow(ax, (26, 76.5 - i * 13), (37, 56), color=GREY, lw=1.3,
              rad={0: -0.1, 1: 0, 2: 0.1}[i])
    box(ax, 37, 48, 24, 16, "统一 ToolSpec\n同一张工具表\n执行侧对来源零感知",
        fc=INK, ec=INK, fs=11.5, bold=True, tc="white")
    box(ax, 70, 66, 24, 9, "Schema 参数校验", fc=TINT, ec=LINE, fs=10.5)
    box(ax, 70, 54, 24, 9, "权限三档授权", fc="#FBF3E8", ec=ACCENT, fs=10.5)
    box(ax, 70, 42, 24, 9, "sync / async 执行 · 结果收口", fc=TINT, ec=LINE, fs=10.5)
    arrow(ax, (61, 58), (70, 70.5), color=LINE, lw=1.4)
    arrow(ax, (61, 56), (70, 58.5), color=ACCENT, lw=1.4)
    arrow(ax, (61, 54), (70, 46.5), color=LINE, lw=1.4)
    modes = [("全部审批", "任何调用前确认"), ("部分审批", "读放行 · 写询问（默认）"),
             ("无需审批", "仅本地演示")]
    for i, (m, d) in enumerate(modes):
        box(ax, 6 + i * 30, 22, 26, 12, f"{m}\n{d}", fc="#FBF3E8", ec=ACCENT, fs=10.8)
    ax.text(50, 12, "失败原因回灌模型做每轮反思；组合工具只编排已注册工具、每步仍走权限，能力物理上不越界。",
            ha="center", fontsize=10.6, color=GREY)
    save(fig, "fig5_toolbus.png")


# ================================================================ 图6 一次 run 自适应时序（v0.9）
def fig_run_lifecycle():
    fig, ax = new_fig(12, 7.4)

    # 主干
    trunk = [
        (3, "① 任务进入", "实例串行锁\n一个实例一条会话"),
        (22.5, "② 主动召回", "长期记忆按任务检索\nMEMORIES_RECALLED"),
        (42, "③ 自适应路由", "STRATEGY_SELECTED\nsource 可审计"),
        (61.5, "④ 规划与执行", "plan / react\n选档 · 重规划"),
        (81, "⑤ 完成", "DONE\n成本·档位汇总"),
    ]
    tw = 16.5
    for x, head, body in trunk:
        box(ax, x, 74, tw, 7.2, head, fc=INK, ec=INK, fs=11.5, bold=True, tc="white")
        box(ax, x, 60.5, tw, 11.5, body, fc=TINT, ec=LINE, fs=9.4)
    for x in (3, 22.5, 42, 61.5):
        arrow(ax, (x + tw, 70), (x + tw + 3, 70), color=INK, lw=1.8)

    # 工具子流程（执行框下方）
    ax.text(50, 55.5, "执行中每一轮：观察 → 决策 → 行动", ha="center", fontsize=10.5,
            color=GREY, fontweight="bold")
    sub = [
        (3, "能力缺口", "CAPABILITY_MISSING", ACCENT),
        (27, "补能力", "发现 / 组合 / 创建工具", LINE),
        (51, "授权", "PERMISSION_ASKED\n（创建 + 执行双授权）", ACCENT),
        (75, "调用", "TOOL_CALL / TOOL_RESULT", GREEN),
    ]
    sw = 22
    for x, head, body, ec in sub:
        box(ax, x, 40, sw, 6, head, fc=ec, ec=ec, fs=10.5, bold=True, tc="white")
        box(ax, x, 28.5, sw, 10, body, fc=TINT2 if ec != ACCENT else "#FBF3E8", ec=ec, fs=9.2)
    for x in (3, 27, 51):
        arrow(ax, (x + sw, 43), (x + sw + 2, 43), color=INK, lw=1.6)
    arrow(ax, (86, 43), (90, 43), color=GREEN, lw=1.6)
    arrow(ax, (69.5, 60.5), (69.5, 46.2), color=GREY, lw=1.2, ls=(0, (3, 3)))
    arrow(ax, (86, 38.5), (86, 60.5), color=GREEN, lw=1.2, ls=(0, (3, 3)), rad=-0.25)

    # 护栏
    box(ax, 3, 16.5, 30, 8.4,
        "上下文护栏：超水位 → 压缩（CONTEXT_COMPACTED）\n可注入小模型，失败回退硬裁剪",
        fc=TINT2, ec=LINE, fs=9.2)
    box(ax, 35, 16.5, 30, 8.4,
        "预算护栏：超 model / tool / token 上限\n→ BUDGET_EXCEEDED 体面熔断",
        fc="#FBF3E8", ec=ACCENT, fs=9.2)
    box(ax, 67, 16.5, 30, 8.4,
        "韧性护栏：模型硬超时 · 取消传播\n并发锁 · 8 类静默兜底诊断",
        fc=TINT2, ec=LINE, fs=9.2)

    # 可观测出口
    box(ax, 3, 4, 94, 8.6,
        "全部环节 → AgentCore.astream 事件流唯一出口（19 类事件）\n"
        "UI（灵动岛 / 网页 / 终端）　·　零依赖 Metrics　·　OpenTelemetry tracing",
        fc=INK, ec=INK, fs=10, bold=True, tc="white")
    for x in (18, 50, 82):
        arrow(ax, (x, 16.5), (x, 12.7), color=GREY, lw=1.0, ls=(0, (2, 2)))
    save(fig, "fig6_run_lifecycle.png")


# ================================================================ 图7 记忆与上下文分层（v0.9，新）
def fig_memory():
    fig, ax = new_fig(11.6, 7.4)

    # 上层：上下文窗口
    band(ax, 4, 66, 92, 27, "上下文窗口（喂给模型）", TINT, INK)
    win = [
        "工具结果有界收口\n默认 16000 字符\n头部 70% + 尾部 30%",
        "工具目录确定性排序\n无状态前缀字节稳定\n利于 prompt cache",
        "上下文压缩 Compaction\n旧轮摘要 + 近轮完整\n可注入小模型 · 水位工厂",
    ]
    for i, t in enumerate(win):
        box(ax, 7 + i * 30.5, 70, 28, 16.5, t, fc=PAPER, ec=LINE, fs=9.6)

    # 中层：长期记忆
    band(ax, 4, 38, 92, 25, "结构化长期记忆 MemoryVault（opt-in）", "#FBF3E8", ACCENT)
    lt = [
        "remember / recall\n模型主动存取事实画像",
        "词法倒排索引\nmax_notes 容量上限",
        "语义双通道\nembedding 可选 · 不中降级",
        "任务起始主动召回\nMEMORIES_RECALLED",
        "落盘静态加密\nStorageCipher（第九 SPI）",
    ]
    for i, t in enumerate(lt):
        box(ax, 6 + i * 18.7, 41.5, 17.4, 15, t, fc=PAPER, ec=ACCENT, fs=8.8)

    # 下层：会话记忆
    band(ax, 4, 10, 92, 25, "会话记忆 SQLite（默认启用）", TINT2, GREEN)
    sh = [
        "History + KV\n工具调用 / 消息持久化",
        "TTL 过期\n按轮裁剪 · 工具对不拆",
        "prune 先 COUNT 短路\n大规模历史低开销",
        "WAL + 原子写\n崩溃恢复不丢数据",
        "在线热备份\nbackup_to 不停服快照",
    ]
    for i, t in enumerate(sh):
        box(ax, 6 + i * 18.7, 13.5, 17.4, 15, t, fc=PAPER, ec=GREEN, fs=8.8)

    ax.text(50, 5.6,
            "默认只开会话记忆；长期记忆 / 语义召回 / 落盘加密均显式 opt-in，内核不内置向量库、不强制加密依赖。",
            ha="center", fontsize=9.4, color=GREY)
    save(fig, "fig7_memory.png")


# ================================================================ 图8 造工具三级（更新 Docker 落地）
def fig_evolution():
    fig, ax = new_fig(11.4, 6.6)
    levels = [
        ("一级 · 组合工具",
         "编排已注册工具 · 不生成代码\n能力上限 = 被组合工具的并集\n每步递归鉴权、禁止嵌套",
         "已落地", GREEN, GREEN),
        ("二级 · 沙箱代码工具",
         "子进程教学沙箱：AST 闸门 / 白名单\n超时 · 输出截断 · TTL 回收 · 续期\nDocker 生产沙箱：--network none /\n--read-only / --cap-drop ALL / pids-limit",
         "均已落地（host_g）", GREEN, GREEN),
        ("三级 · 规模化（路线图）",
         "持久沙箱服务 · gVisor / Firecracker\n多语言工具 conformance 套件\n宿主市场动态下发与审计",
         "路线图", GREY, GREY),
    ]
    x, w, gap = 5, 28, 4.5
    for i, (head, body, tag, ec, tagc) in enumerate(levels):
        px = x + i * (w + gap)
        box(ax, px, 70, w, 11, head, fc=ec, ec=ec, fs=12.5, bold=True, tc="white")
        box(ax, px, 42, w, 24, body, fc=TINT if i < 2 else TINT2, ec=ec, fs=9.8)
        box(ax, px + 4, 32.5, w - 8, 6.5, tag, fc=PAPER, ec=tagc, fs=9.8, tc=tagc, lw=1.3)
        if i < 2:
            arrow(ax, (px + w, 75.5), (px + w + gap, 75.5), color=INK, lw=2)
    ax.text(50, 22,
            "内核侧 ToolSandbox 只定义契约（隔离 / 超时 / 无网络 / 授权后执行）；子进程用于教学与单机，Docker 才是 OS 级安全边界，沙箱由宿主按部署形态提供。",
            ha="center", fontsize=9.8, color=GREY)
    ax.text(50, 15,
            "代码工具创建与执行双重授权；除“无需审批”档外一律先请求允许，默认 48h TTL，多次调用续期，后台可设永久保留。",
            ha="center", fontsize=9.8, color=ACCENT)
    save(fig, "fig8_evolution.png")


# ================================================================ 图9 部署形态与多租户（v0.9，新）
def fig_deployment():
    fig, ax = new_fig(12, 6.8)
    cols = [
        ("形态 A · 进程内嵌入（默认）",
         ["import yai_core，随宿主进程运行", "单租户、零部署、零额外端口",
          "离线 ScriptedModel 30 秒验证嵌入", "桌面 / 移动端 / 受限网络均可"]),
        ("形态 B · 可选服务端 Battery",
         ["FastAPI + Docker / Compose", "HTTPS · API Key（HMAC 常量时间）",
          "限流置于最外层（含鉴权失败）", "/health · /readyz · /metrics"]),
        ("形态 C · 多租户 / 水平扩展",
         ["TenantCoreFactory 装配层编排", "SQLite-per-tenant + 每租户 DEK",
          "KMS 信封加密 · 空闲 LRU 换出", "无状态模型/发现/沙箱共享 · RLS 路线"]),
    ]
    x, w, gap = 4, 29.3, 3
    for i, (head, items) in enumerate(cols):
        px = x + i * (w + gap)
        box(ax, px, 72, w, 11, head, fc=INK, ec=INK, fs=11.5, bold=True, tc="white")
        for j, it in enumerate(items):
            box(ax, px, 58 - j * 11.5, w, 9.6, it,
                fc=TINT if i == 0 else (TINT2 if i == 1 else "#FBF3E8"),
                ec=LINE if i < 2 else ACCENT, fs=9.8)
    arrow(ax, (33.3, 50), (36.3, 50), color=GREY, lw=1.4, ls=(0, (4, 3)))
    arrow(ax, (65.6, 50), (68.6, 50), color=GREY, lw=1.4, ls=(0, (4, 3)))
    box(ax, 4, 12, 92, 12,
        "内核立场：tenant_id 不进入 kernel 签名，会话 / 专员 / 租户边界都在装配层；\n"
        "同一份零依赖内核在三种形态下零改动，隔离强度与运维成本由宿主按需选择。",
        fc=PAPER, ec=INK, fs=11, bold=True, tc=INK)
    ax.text(50, 6.5, "跨不可信企业的硬隔离 = 独立库文件 + 独立密钥 + 进程级配额；规模化时平滑升级到 Postgres 行级安全（RLS）。",
            ha="center", fontsize=9.4, color=GREY)
    save(fig, "fig9_deployment.png")


def main():
    fig_position()
    fig_architecture()
    fig_router()
    fig_discovery()
    fig_toolbus()
    fig_run_lifecycle()
    fig_memory()
    fig_evolution()
    fig_deployment()
    print("all figures in", OUT)


if __name__ == "__main__":
    main()
