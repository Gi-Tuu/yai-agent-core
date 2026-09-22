"""自校准路由离线 benchmark：用确定性环境复现"学习确实发生"。

不调用任何真实 LLM、不需要网络或 API Key。做法：

1. 60 条人工标注 ground-truth 的任务（见 ``router_bench_tasks.py``）；
2. 五条对比线各自顺序跑任务：
   - rules：纯规则路由，固定不学习（基线）；
   - bandit：上下文 Thompson bandit，在线从结果学习（本方案主角）；
   - oracle：始终按 ground-truth 选（完美路由天花板，非产品能力）；
   - ablation_no_prior：bandit 关掉规则先验（α=β=1 均匀冷启动）；
   - ablation_no_context：bandit 退化为单上下文 4-arm（验证上下文特征价值）；
3. 环境模型按 (gt, 实际选中策略) 构造**与真实 AgentLoop 一致的事件序列**，
   再交给 M1 的纯函数 ``extract_route_outcome`` 计算 reward——奖励不是 benchmark
   手编的，而是真实反馈抽取器在仿真事件上的输出；
4. 多随机种子平均，输出滑动窗口成功率、成本代理，标准库拼 SVG 学习曲线。

硬校验：bandit 末段成功率必须显著高于 rules，否则以非零退出码结束
（提醒奖励信号 / 超参有问题，防止"看起来在学习"的假曲线）。

用法：
    python scripts/benchmark_router.py            # 跑默认 8 seeds × 3 epochs
    python scripts/benchmark_router.py --seeds 4 --epochs 2
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

# 脚本可直接运行：把 src/ 与 scripts/ 加入导入路径。
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from router_bench_tasks import TASKS, BenchTask  # noqa: E402

from yai_core.discovery import build_spec  # noqa: E402
from yai_core.kernel.router import AdaptiveRouter  # noqa: E402
from yai_core.learning.bandit import ContextualBanditSelector  # noqa: E402
from yai_core.learning.outcomes import extract_route_outcome  # noqa: E402
from yai_core.tools.registry import ToolRegistry  # noqa: E402
from yai_core.types import AgentEvent, EventType, Strategy  # noqa: E402

# ---------------------------------------------------------------------------
# 环境模型：固定的演示宿主工具集（5 个，落在 few 桶）。
# ---------------------------------------------------------------------------

_TOOL = "search_notes"
_ANSWERS = {
    Strategy.DIRECT: "这是可以直接回答的内容。",
    Strategy.REACT: "已为你查到结果，相关数据如下。",
    Strategy.PLAN: "已按计划完成全部步骤，结果汇总如下。",
}
# direct 误判工具型任务时，模型手里没有工具，只能承认无能（命中 unable_hint）。
_UNABLE = "我无法查询相关数据，当前没有访问权限，也缺少必要的工具。"
# 信息不全却硬答：模型只能承认缺少必要信息（同样命中 unable_hint）。
_MISSING_INFO = "我缺少必要信息，无法确定你具体要做什么。"
_CLARIFIED = "明白了，已根据你补充的信息完成处理。"
_ABORT = "任务信息不足，已停止。请换个说法重新描述。"


def build_registry() -> ToolRegistry:
    """构造固定的 5 工具宿主（搜索/订单/天气/记忆/客户）。"""

    def search_notes(keyword: str = "") -> list:
        """搜索笔记。"""
        return []

    def list_orders() -> list:
        """列出订单。"""
        return []

    def get_weather(city: str = "") -> dict:
        """查询天气。"""
        return {}

    def save_memory(content: str = "") -> dict:
        """保存记忆。"""
        return {}

    def get_customer(name: str = "") -> dict:
        """获取客户资料。"""
        return {}

    reg = ToolRegistry()
    for fn in (search_notes, list_orders, get_weather, save_memory, get_customer):
        reg.register(build_spec(fn))
    return reg


def _tool_events(ok: bool) -> list[AgentEvent]:
    result = (
        {"tool": _TOOL, "ok": True, "preview": "查到 1 条结果"}
        if ok
        else {"tool": _TOOL, "ok": False, "error": "参数不足，无法执行"}
    )
    return [
        AgentEvent(EventType.TOOL_CALL, {"tool": _TOOL, "arguments": {}}),
        AgentEvent(EventType.TOOL_RESULT, result),
    ]


def simulate(gt: Strategy, chosen: Strategy) -> tuple[list[AgentEvent], int]:
    """环境模型：按 (ground-truth, 实际选中策略) 构造事件轨迹。

    返回 (事件列表, 模型调用数代理)。事件构造与真实 ``AgentLoop`` 的四类策略
    事件流一一对应；判定逻辑完全交给 ``extract_route_outcome``。
    """
    ev: list[AgentEvent] = []

    # --- 选中 clarify：要么澄清后递归成功，要么空回答放弃（abort）---
    if chosen == Strategy.CLARIFY:
        ev.append(AgentEvent(EventType.CLARIFY_REQUESTED, {"question": "请补充", "round": 1}))
        if gt == Strategy.CLARIFY:
            # 正确澄清：补充后递归成功，最终 DONE 是递归内的真实策略（react）。
            ev.append(AgentEvent(EventType.MODEL_MESSAGE, {"text": _CLARIFIED}))
            ev.append(
                AgentEvent(
                    EventType.DONE,
                    {"strategy": Strategy.REACT.value, "final_text": _CLARIFIED},
                )
            )
            return ev, 1
        # 清晰任务却被澄清：用户放弃，以 clarify 收尾（abort）。
        ev.append(AgentEvent(EventType.MODEL_MESSAGE, {"text": _ABORT}))
        ev.append(
            AgentEvent(
                EventType.DONE,
                {"strategy": Strategy.CLARIFY.value, "final_text": _ABORT},
            )
        )
        return ev, 0

    if chosen == Strategy.PLAN:
        ev.append(
            AgentEvent(EventType.PLAN_CREATED, {"steps": ["步骤一", "步骤二", "步骤三"]})
        )

    # --- 该 plan 却只 react：缺乏规划，工具循环无法收敛，撞满迭代上限 ---
    if gt == Strategy.PLAN and chosen == Strategy.REACT:
        for _ in range(6):
            ev.extend(_tool_events(True))
        final = "已达到工具调用上限，多步骤任务未能全部完成。"
        ev.append(AgentEvent(EventType.MODEL_MESSAGE, {"text": final}))
        ev.append(AgentEvent(EventType.DONE, {"strategy": chosen.value, "final_text": final}))
        return ev, 7

    # --- 该 clarify 却硬执行：信息不全，工具失败 ---
    if gt == Strategy.CLARIFY and chosen in (Strategy.REACT, Strategy.PLAN):
        ev.extend(_tool_events(False))
        final = "信息不足，工具调用失败，无法完成。"
        ev.append(AgentEvent(EventType.MODEL_MESSAGE, {"text": final}))
        ev.append(AgentEvent(EventType.DONE, {"strategy": chosen.value, "final_text": final}))
        return ev, (2 if chosen == Strategy.PLAN else 1)

    # --- 该用工具却 direct：手里没工具，只能说无能 ---
    if chosen == Strategy.DIRECT and gt in (Strategy.REACT, Strategy.PLAN, Strategy.CLARIFY):
        text = _MISSING_INFO if gt == Strategy.CLARIFY else _UNABLE
        ev.append(AgentEvent(EventType.MODEL_MESSAGE, {"text": text}))
        ev.append(
            AgentEvent(
                EventType.DONE,
                {"strategy": Strategy.DIRECT.value, "final_text": text},
            )
        )
        return ev, 1

    # --- 以下为成功路径 ---
    if chosen == Strategy.REACT:
        n_tools = 1 if gt == Strategy.REACT else 0  # direct 任务误入 react：模型直接答、0 工具
        for _ in range(n_tools):
            ev.extend(_tool_events(True))
        model_calls = n_tools + 1
    elif chosen == Strategy.PLAN:
        n_tools = 2 if gt == Strategy.PLAN else 1  # react 任务误入 plan：1 工具 + 计划开销
        for _ in range(n_tools):
            ev.extend(_tool_events(True))
        model_calls = 1 + n_tools + 1
    else:  # direct 正确
        model_calls = 1

    text = _ANSWERS[gt] if chosen == gt else _ANSWERS[gt]
    ev.append(AgentEvent(EventType.MODEL_MESSAGE, {"text": text}))
    ev.append(AgentEvent(EventType.DONE, {"strategy": chosen.value, "final_text": text}))
    return ev, model_calls


# ---------------------------------------------------------------------------
# 对比线
# ---------------------------------------------------------------------------

def _ordered_tasks(seed: int, epochs: int) -> list[BenchTask]:
    """固定种子的可复现任务序列：每 epoch 打乱一次，模拟长期陆续到来的任务。"""
    ordered: list[BenchTask] = []
    for epoch in range(epochs):
        batch = list(TASKS)
        random.Random(seed * 10_000 + epoch).shuffle(batch)
        ordered.extend(batch)
    return ordered


def _build_router(line: str, seed: int) -> AdaptiveRouter | None:
    if line == "oracle":
        return None
    if line == "rules":
        return AdaptiveRouter()
    if line == "bandit":
        return AdaptiveRouter(selector=ContextualBanditSelector(seed=seed))
    if line == "ablation_no_prior":
        return AdaptiveRouter(
            selector=ContextualBanditSelector(seed=seed, prior_strength=0.0)
        )
    if line == "ablation_no_context":
        return AdaptiveRouter(
            selector=ContextualBanditSelector(seed=seed, contextual=False)
        )
    raise ValueError(f"未知对比线：{line}")  # pragma: no cover


def run_line(line: str, seed: int, epochs: int, registry: ToolRegistry) -> list[dict]:
    """跑一条对比线，返回逐步记录。"""
    tasks = _ordered_tasks(seed, epochs)
    router = _build_router(line, seed)

    records: list[dict] = []
    for item in tasks:
        gt = Strategy(item.gt)
        if line == "oracle":
            chosen, source = gt, "oracle"
        elif line == "rules":
            chosen, source = router.classify(item.task, registry), "rules"
        else:
            # 复用真实路由的同步路径：selector 前置、不表态则规则兜底。
            decision = router._learned_decision(item.task, registry)
            if decision is None:
                decision = router._rules(item.task, registry)
            chosen, source = decision.strategy, decision.source

        events, model_calls = simulate(gt, chosen)
        outcome = extract_route_outcome(
            events, chosen, max_iters=6, clarify_budget=2
        )
        if router is not None and router.selector is not None:
            router.selector.record(item.task, registry, chosen, outcome)

        records.append(
            {
                "task": item.task,
                "gt": gt.value,
                "chosen": chosen.value,
                "source": source,
                "success": bool(outcome.success),
                "reward": round(outcome.reward, 4),
                "tool_calls": outcome.tool_calls,
                "model_calls": model_calls,
                "false_clarify": chosen == Strategy.CLARIFY and gt != Strategy.CLARIFY,
            }
        )
    return records


# ---------------------------------------------------------------------------
# 聚合 + 输出
# ---------------------------------------------------------------------------

LINES = ("rules", "bandit", "oracle", "ablation_no_prior", "ablation_no_context")
_LINE_LABELS = {
    "rules": "规则路由（基线，不学习）",
    "bandit": "自校准 bandit（本方案）",
    "oracle": "Oracle（完美路由天花板）",
    "ablation_no_prior": "消融 A：无规则先验",
    "ablation_no_context": "消融 B：无上下文",
}
_LINE_COLORS = {
    "rules": "#9aa3ad",
    "bandit": "#1a7f37",
    "oracle": "#2563eb",
    "ablation_no_prior": "#d97706",
    "ablation_no_context": "#9333ea",
}


def rolling(seq: list[float], window: int) -> list[float]:
    out: list[float] = []
    for i in range(len(seq)):
        lo = max(0, i - window + 1)
        out.append(sum(seq[lo : i + 1]) / (i - lo + 1))
    return out


def aggregate(seeds: int, epochs: int, registry: ToolRegistry, window: int) -> dict:
    per_line_records: dict[str, list[list[dict]]] = {line: [] for line in LINES}
    for seed in range(seeds):
        for line in LINES:
            per_line_records[line].append(run_line(line, seed, epochs, registry))

    n_steps = len(per_line_records["rules"][0])
    curves: dict[str, list[float]] = {}
    summary: dict[str, dict] = {}
    tail = max(1, n_steps // 5)

    for line in LINES:
        # 每个 seed 内做滑动窗口，再跨 seed 平均。
        per_seed_roll = [
            rolling([1.0 if r["success"] else 0.0 for r in recs], window)
            for recs in per_line_records[line]
        ]
        curves[line] = [
            sum(run[step] for run in per_seed_roll) / seeds for step in range(n_steps)
        ]
        tail_records = [r for recs in per_line_records[line] for r in recs[-tail:]]
        all_records = [r for recs in per_line_records[line] for r in recs]
        summary[line] = {
            "final_success": round(
                sum(r["success"] for r in tail_records) / len(tail_records), 4
            ),
            "overall_success": round(
                sum(r["success"] for r in all_records) / len(all_records), 4
            ),
            "avg_tool_calls": round(
                sum(r["tool_calls"] for r in all_records) / len(all_records), 3
            ),
            "avg_model_calls": round(
                sum(r["model_calls"] for r in all_records) / len(all_records), 3
            ),
            "false_clarify_rate": round(
                sum(r["false_clarify"] for r in all_records) / len(all_records), 4
            ),
        }

    return {
        "meta": {
            "n_tasks": len(TASKS),
            "epochs": epochs,
            "seeds": seeds,
            "steps": n_steps,
            "window": window,
            "gt_distribution": {
                s: sum(1 for t in TASKS if t.gt == s)
                for s in ("direct", "react", "plan", "clarify")
            },
        },
        "summary": summary,
        "curves": curves,
    }


def render_svg(curves: dict[str, list[float]], path: Path, epochs: int) -> None:
    """标准库拼折线图：x=任务步数，y=滑动窗口成功率，五线 + 图例。"""
    width, height = 860, 500
    ml, mr, mt, mb = 70, 240, 40, 56
    plot_w, plot_h = width - ml - mr, height - mt - mb
    n = len(next(iter(curves.values())))

    def x(i: int) -> float:
        return ml + plot_w * i / max(1, n - 1)

    def y(v: float) -> float:
        return mt + plot_h * (1 - v)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="Segoe UI, Microsoft YaHei, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{ml}" y="24" font-size="17" font-weight="700" fill="#1f2937">'
        "自校准路由学习曲线（离线确定性仿真）</text>",
    ]
    # y 网格 + 刻度（0/0.25/0.5/0.75/1）
    for v in (0.0, 0.25, 0.5, 0.75, 1.0):
        yy = y(v)
        parts.append(
            f'<line x1="{ml}" y1="{yy:.1f}" x2="{ml + plot_w}" y2="{yy:.1f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{ml - 8}" y="{yy + 4:.1f}" font-size="11" fill="#6b7280" '
            f'text-anchor="end">{int(v * 100)}%</text>'
        )
    # x 轴刻度（epoch 边界）
    steps_per_epoch = n // max(1, epochs)
    for e in range(epochs + 1):
        i = min(n - 1, e * steps_per_epoch)
        xx = x(i)
        parts.append(
            f'<line x1="{xx:.1f}" y1="{mt + plot_h}" x2="{xx:.1f}" y2="{mt + plot_h + 5}" '
            'stroke="#9ca3af"/>'
        )
        if e < epochs:
            parts.append(
                f'<text x="{xx + steps_per_epoch / 2:.1f}" y="{mt + plot_h + 22}" '
                f'font-size="11" fill="#6b7280" text-anchor="middle">第 {e + 1} 轮</text>'
            )
    parts.append(
        f'<text x="{ml + plot_w / 2:.1f}" y="{height - 12}" font-size="12" '
        'fill="#374151" text-anchor="middle">已处理任务数（滑动窗口成功率）</text>'
    )
    parts.append(
        f'<text x="18" y="{mt + plot_h / 2:.1f}" font-size="12" fill="#374151" '
        'text-anchor="middle" transform="rotate(-90 18 '
        f'{mt + plot_h / 2:.1f})">成功率</text>'
    )

    # 折线（oracle/rules 用虚线，bandit 加粗主线）
    for line in LINES:
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(curves[line]))
        dash = "" if line == "bandit" else ' stroke-dasharray="5 4"'
        width_ = "2.6" if line == "bandit" else "1.6"
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{_LINE_COLORS[line]}" '
            f'stroke-width="{width_}"{dash}/>'
        )

    # 图例
    ly = mt + 6
    for line in LINES:
        parts.append(
            f'<line x1="{ml + plot_w + 18}" y1="{ly}" x2="{ml + plot_w + 46}" y2="{ly}" '
            f'stroke="{_LINE_COLORS[line]}" stroke-width="3"/>'
        )
        parts.append(
            f'<text x="{ml + plot_w + 52}" y="{ly + 4}" font-size="12" '
            f'fill="#374151">{_LINE_LABELS[line]}</text>'
        )
        ly += 30
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def print_table(result: dict) -> None:
    print("\n=== 路由 benchmark 汇总（末段=最后 20% 任务）===")
    header = (
        f"{'对比线':<22}{'末段成功率':>10}{'整体成功率':>10}"
        f"{'均工具数':>9}{'均模型调用':>10}{'误澄清率':>9}"
    )
    print(header)
    print("-" * len(header))
    for line in LINES:
        s = result["summary"][line]
        print(
            f"{_LINE_LABELS[line]:<20}"
            f"{s['final_success'] * 100:>9.1f}%"
            f"{s['overall_success'] * 100:>9.1f}%"
            f"{s['avg_tool_calls']:>9.2f}"
            f"{s['avg_model_calls']:>10.2f}"
            f"{s['false_clarify_rate'] * 100:>8.1f}%"
        )


def main(seeds: int = 8, epochs: int = 3, window: int = 15, out_dir: Path | None = None) -> dict:
    registry = build_registry()
    result = aggregate(seeds, epochs, registry, window)

    out_dir = out_dir or (_ROOT / "benchmark_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "router_bench.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    render_svg(result["curves"], out_dir / "router_learning_curve.svg", epochs)
    print_table(result)

    bandit_final = result["summary"]["bandit"]["final_success"]
    rules_final = result["summary"]["rules"]["final_success"]
    print(f"\n硬校验：bandit 末段 {bandit_final:.1%} vs rules 末段 {rules_final:.1%}")
    if not bandit_final > rules_final + 0.05:
        print("FAIL：自校准未显著超过规则基线，奖励信号或超参需排查。")
        raise SystemExit(1)
    print("PASS：自校准路由显著优于规则基线，学习确实发生。")
    print(f"产物：{out_dir / 'router_bench.json'}")
    print(f"产物：{out_dir / 'router_learning_curve.svg'}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="自校准路由离线 benchmark")
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--window", type=int, default=15)
    args = parser.parse_args()
    main(seeds=args.seeds, epochs=args.epochs, window=args.window)
