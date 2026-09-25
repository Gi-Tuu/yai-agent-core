"""为上海开源赛《作品介绍》生成学术风格架构图（PNG）。

运行：
  .venv/Scripts/python.exe docs/competitions/shanghai/draw_figs.py

产物：docs/competitions/shanghai/figs/fig_*.png
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
            fontweight="bold" if bold else "normal", linespacing=1.4)
    return p


def arrow(ax, p1, p2, color=LINE, lw=1.6, style="-|>", ls="-", rad=0.0, ms=14):
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


def caption(ax, n, title):
    # 图注统一由排版系统（Word alt 图注）输出，图片本身不再内嵌，避免重复
    return


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
        ("托管平台", "Dify · Coze", "应用进入平台运行\n数据 / UI / 发布在平台侧\n存量软件无法内嵌", TINT2, GREY),
        ("拼装框架", "LangChain · CrewAI · AutoGen", "提供积木与范式\n谁是 Agent、有哪些工具\n仍需开发者逐一定义装配", TINT2, GREY),
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
    caption(ax, 1, "应用 AI 化的三类形态对比")
    save(fig, "fig1_position.png")


# ================================================================ 图2 分层架构
def fig_architecture():
    fig, ax = new_fig(11, 8.2)
    # 宿主层
    box(ax, 8, 86, 84, 10,
        "宿主软件　host_a 笔记 · host_c 陪伴 · host_e CRM · host_g 沙箱 · host_i Mealie · AMBRACE",
        fc=TINT2, ec=GREY, fs=12.5, bold=True)
    ax.text(50, 82.5, "声明 capabilities（普通函数 + 类型注解 + 一行 docstring）　|　选择 model / memory / policy",
            ha="center", fontsize=10.5, color=GREY)

    # Core 外框
    outer = FancyBboxPatch((6, 20), 88, 58, boxstyle="round,pad=0.01,rounding_size=0.02",
                           fc="none", ec=INK, lw=2.2, zorder=2)
    ax.add_patch(outer)
    ax.text(10, 75, "YAI Agent Core（进程内库 · 内核零第三方硬依赖 dependencies = []）",
            fontsize=12.5, color=INK, fontweight="bold")

    groups = [
        ("接入与发现", ["内省 → JSON Schema", "ToolDiscovery 目录", "词法 + 语义双通道"]),
        ("自适应决策", ["Router 四策略", "Context 预算压缩", "Bandit 在线学习"]),
        ("执行与工具", ["Agent Loop / ReAct", "ToolRegistry + Executor", "组合工具 composer"]),
        ("支撑与契约", ["Memory（含 SQLite）", "事件流 / 权限三档", "SPI 八契约"]),
    ]
    gx, gw, ggap = 9, 19.5, 2
    for i, (head, items) in enumerate(groups):
        x = gx + i * (gw + ggap)
        box(ax, x, 62, gw, 8, head, fc=INK, ec=INK, fs=11.5, bold=True, tc="white")
        for j, it in enumerate(items):
            box(ax, x, 53 - j * 8.5, gw, 7, it, fc=TINT, ec=LINE, fs=10)

    # 外部可选
    box(ax, 8, 6, 26, 9, "模型后端\nOpenAI 兼容（DeepSeek 等）", fc=PAPER, ec=GREY, fs=10.5, tc=GREY)
    box(ax, 37, 6, 26, 9, "MCP Server\n远程 / stdio / 进程内", fc=PAPER, ec=GREY, fs=10.5, tc=GREY)
    box(ax, 66, 6, 26, 9, "OpenAPI 服务\nREST → 工具自动桥接", fc=PAPER, ec=GREY, fs=10.5, tc=GREY)
    arrow(ax, (90, 86), (90, 78.6), color=INK, lw=2)
    for x in (21, 50, 79):
        arrow(ax, (x, 20), (x, 15.2), color=GREY, lw=1.4, ls=(0, (4, 3)))
    caption(ax, 2, "YAI Agent Core 分层架构与模块分组")
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
        ("plan", "先产出有序计划\n再逐步执行", TINT),
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
    caption(ax, 3, "自适应路由的四策略与决策路径")
    save(fig, "fig3_router.png")


# ================================================================ 图4 能力缺口闭环
def fig_discovery():
    fig, ax = new_fig(11, 5.6)
    steps = [
        ("感知缺口", "capability_missing\n（缺口 + 工具清单）", "#FBF3E8", ACCENT),
        ("请求发现", "ToolDiscovery\n按缺口要候选", TINT, LINE),
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
    ax.text(50, 12,
            "召回为词法 + 语义双通道；发现源异常只发错误事件、不中断主流程；“能被发现”不等于“被授权执行”。",
            ha="center", fontsize=10.6, color=GREY)
    caption(ax, 4, "能力缺口的最小闭环：感知 → 发现 → 注册 → 本轮可用")
    save(fig, "fig4_discovery.png")


# ================================================================ 图5 工具总线与权限
def fig_toolbus():
    fig, ax = new_fig(11, 6.6)
    # 三类来源
    srcs = ["Native 函数", "MCP Server", "OpenAPI 服务"]
    for i, s in enumerate(srcs):
        box(ax, 6, 72 - i * 13, 20, 9, s, fc=TINT2, ec=GREY, fs=11.5)
        arrow(ax, (26, 76.5 - i * 13), (37, 56), color=GREY, lw=1.3,
              rad={0: -0.1, 1: 0, 2: 0.1}[i])
    box(ax, 37, 48, 24, 16, "统一 ToolSpec\n同一张工具表\n执行侧对来源零感知",
        fc=INK, ec=INK, fs=11.5, bold=True, tc="white")
    box(ax, 70, 66, 24, 9, "Schema 参数校验", fc=TINT, ec=LINE, fs=10.5)
    box(ax, 70, 54, 24, 9, "权限三档授权", fc="#FBF3E8", ec=ACCENT, fs=10.5)
    box(ax, 70, 42, 24, 9, "sync / async 执行", fc=TINT, ec=LINE, fs=10.5)
    arrow(ax, (61, 58), (70, 70.5), color=LINE, lw=1.4)
    arrow(ax, (61, 56), (70, 58.5), color=ACCENT, lw=1.4)
    arrow(ax, (61, 54), (70, 46.5), color=LINE, lw=1.4)
    # 权限三档
    modes = [("全部审批", "任何调用前确认"), ("部分审批", "读放行 · 写询问（默认）"),
             ("无需审批", "仅本地演示")]
    for i, (m, d) in enumerate(modes):
        box(ax, 6 + i * 30, 22, 26, 12, f"{m}\n{d}", fc="#FBF3E8", ec=ACCENT, fs=10.8)
    ax.text(50, 12, "失败原因回灌模型做每轮反思；组合工具只编排已注册工具，每步仍走权限，能力物理上不越界。",
            ha="center", fontsize=10.6, color=GREY)
    caption(ax, 5, "工具总线、统一执行管线与权限三档")
    save(fig, "fig5_toolbus.png")


# ================================================================ 图6 事件流出口
def fig_eventflow():
    fig, ax = new_fig(11, 6.4)
    comps = ["路由决策", "计划生成", "工具调用 / 结果", "权限确认", "能力缺口", "错误 / 完成"]
    xs = (4, 37, 70)
    for i, c in enumerate(comps):
        x = xs[i % 3]
        y = 72 if i < 3 else 54
        box(ax, x, y, 26, 11, c, fc=TINT2, ec=GREY, fs=12)
        # 汇聚到 AgentLoop 顶部
        arrow(ax, (x + 13, y if i < 3 else y), (50, 47),
              color=GREY, lw=1.1,
              rad={(0, 0): 0.12, (0, 1): 0.04, (0, 2): -0.04,
                   (1, 0): 0.16, (1, 1): -0.02, (1, 2): -0.12}[(i // 3, i % 3)])
    box(ax, 37, 36, 26, 11, "AgentLoop\n统一收集", fc=INK, ec=INK, fs=12.5, bold=True,
        tc="white")
    box(ax, 35, 15, 30, 12, "AgentCore.astream\n唯一 emit 出口", fc=TINT, ec=INK,
        fs=12, bold=True)
    box(ax, 70, 15, 26, 12, "UI 消费\n终端 · 网页 · Flutter", fc=PAPER, ec=GREY,
        fs=11, tc=GREY)
    arrow(ax, (50, 36), (50, 27), color=INK, lw=2)
    arrow(ax, (65, 21), (70, 21), color=GREY, lw=1.6)
    ax.text(18, 21, "不存在\n静默决策", ha="center", va="center", fontsize=11.5,
            color=ACCENT, fontweight="bold", linespacing=1.3)
    caption(ax, 6, "事件流唯一出口与 UI 消费方式")
    save(fig, "fig6_eventflow.png")


# ================================================================ 图7 三级造工具路线
def fig_evolution():
    fig, ax = new_fig(11, 6.2)
    levels = [
        ("组合工具", "编排已注册工具 · 不生成代码\n能力上限 = 被组合工具的并集", "已落地", GREEN, GREEN),
        ("沙箱代码工具", "宿主子进程沙箱：白名单 / 超时\n输出截断 / 内存上限 · TTL 回收", "教学演示（host_g）", ACCENT, ACCENT),
        ("生产级强隔离", "容器 / gVisor / Firecracker\n持久沙箱服务", "路线图", GREY, GREY),
    ]
    x, w, gap = 6, 26, 6
    for i, (head, body, tag, ec, tagc) in enumerate(levels):
        px = x + i * (w + gap)
        box(ax, px, 61, w, 12, head, fc=ec, ec=ec, fs=13.5, bold=True, tc="white")
        box(ax, px, 38, w, 21, body, fc=TINT2 if i else TINT, ec=ec, fs=10.8)
        box(ax, px + 3, 27, w - 6, 7, tag, fc=PAPER, ec=tagc, fs=10, tc=tagc, lw=1.3)
        if i < 2:
            arrow(ax, (px + w, 67), (px + w + gap, 67), color=INK, lw=2)
    ax.text(50, 18,
            "内核侧 ToolSandbox 只定义契约（隔离 / 超时 / 无网络 / 授权后执行），沙箱由宿主按部署形态提供，内核不内置执行器。",
            ha="center", fontsize=10.4, color=GREY)
    caption(ax, 7, "“发现—组合—受限代码”三级造工具演进路线")
    save(fig, "fig7_evolution.png")


def main():
    fig_position()
    fig_architecture()
    fig_router()
    fig_discovery()
    fig_toolbus()
    fig_eventflow()
    fig_evolution()
    print("all figures in", OUT)


if __name__ == "__main__":
    main()
