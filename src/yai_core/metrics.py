"""零依赖运行指标收集器：给嵌入式内核一条"成本 / 健康"自观测通道。

它与另外三套机制分工不同、互不替代：

- ``AgentEvent`` 事件流：面向**业务与产品**，记录每一次自适应决策，是宿主 UI 与回放主数据；
- :mod:`yai_core.diagnostics`：面向**排障**，承接"内核已静默兜底、主流程没失败"的瞬间；
- :mod:`yai_core.integrations.tracing.otel`：面向**分布式链路追踪**，回答跨进程耗时与因果；
- 本模块（``MetricsRecorder``）：面向**运维与成本**，输出可被 Prometheus 拉取的聚合计数
  （跑了多少 run、成功 / 失败 / 熔断 / 压缩多少次、工具与模型调用量、run 时延分布）。

设计约束（与内核红线一致）：

- **零第三方硬依赖、纯标准库**：计数器 / 直方图 / Prometheus 文本全部手写，``dependencies``
  仍为空；要推 OTLP / 后端可视化请用可选 ``otel`` extra 的 meter 桥接。
- **旁路、永不反噬主流程**：收集器只做整数累加与常数时间直方图更新，方法内部对脏数据
  容错；调用方（``AgentCore.astream``）仍在外层再包一层 try/except，双保险。
- **实例串行、跨线程可读**：一个 core 的任务串行，但 ``/metrics`` 可能在另一处被读取，
  所有计数与快照在一把 :class:`threading.Lock` 下访问。
- **成本可观测是第一目的**：模型调用按档位（standard / strong）计数、run 时延入直方图、
  预算熔断 / 降级 / 压缩等事件都有 counter，便于宿主做配额、告警与"花了多少钱"的归因。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from yai_core.types import AgentEvent, EventType

#: run 时延直方图桶上界（秒）；+Inf 桶在快照/渲染时补，累计桶语义（Prometheus 惯例）。
_DURATION_BUCKETS: tuple[float, ...] = (0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0)
_INF_LABEL = "+Inf"

#: 穿透出事件流即视为"取消"的异常（用户终止 / aclose / 任务取消）。
_CANCELLATION_TYPES: tuple[type, ...] = (
    asyncio.CancelledError,
    GeneratorExit,
    KeyboardInterrupt,
)


class MetricsRecorder:
    """进程内、零依赖的运行指标收集器。

    由 :class:`~yai_core.core.AgentCore` 在 ``astream`` 唯一出口里喂事件；宿主也可自行
    实例化后注入 core（多个 core 共享一个 recorder 即可做进程级聚合），或替换为自定义
    实现（只需提供同名方法，鸭子类型）。

    计数口径：

    - **runs**：``started`` 在进入实例锁时 +1；``completed`` 在见到 ``done`` 事件时 +1
      （模型异常体面收尾、预算熔断也都会发 ``done``，故计入 completed，其严重度另由
      ``error`` / ``budget_exceeded`` 事件计数体现）；``failed`` 为非取消异常穿透；
      ``cancelled`` 为用户终止 / ``aclose`` / 任务取消。
    - **model_calls**：从 ``done`` 事件的 ``standard_calls`` / ``strong_calls`` 累加，
      是成本归因最准的口径（按真实模型调用次数，而非路由猜测）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        # 单次 run 的瞬态（实例串行，无需每任务一个对象）。
        self._active = False
        self._saw_done = False
        self._run_start_mono = 0.0
        # 聚合计数。
        self._runs = {"started": 0, "completed": 0, "failed": 0, "cancelled": 0}
        self._runs_by_strategy: dict[str, int] = {}
        # 工具：name -> {"total": 发起数, "errors": 失败数}。
        self._tools: dict[str, dict[str, int]] = {}
        self._model_calls = {"standard": 0, "strong": 0}
        self._events: dict[str, int] = {}
        # run 时延直方图（累计桶）。
        self._hist_count = 0
        self._hist_sum = 0.0
        self._hist_buckets: dict[float, int] = {b: 0 for b in _DURATION_BUCKETS}

    # ---------- 由 AgentCore.astream 调用 ----------

    def on_run_start(self) -> None:
        """一次 run 开始（在实例锁内、产出首个事件前）。"""
        with self._lock:
            # 防御重入：实例串行下不会发生，出现则只重置瞬态、不重复计数。
            if not self._active:
                self._runs["started"] += 1
            self._active = True
            self._saw_done = False
            self._run_start_mono = time.monotonic()

    def record_event(self, event: AgentEvent) -> None:
        """逐事件累计；脏数据容错，不抛异常。"""
        with self._lock:
            etype = getattr(event, "type", None)
            key = etype.value if isinstance(etype, EventType) else str(etype)
            self._events[key] = self._events.get(key, 0) + 1
            data = event.data if isinstance(getattr(event, "data", None), dict) else {}
            if etype == EventType.STRATEGY_SELECTED:
                strategy = str(data.get("strategy", "unknown"))
                self._runs_by_strategy[strategy] = (
                    self._runs_by_strategy.get(strategy, 0) + 1
                )
            elif etype == EventType.TOOL_CALL:
                slot = self._tools.setdefault(str(data.get("tool", "unknown")),
                                              {"total": 0, "errors": 0})
                slot["total"] += 1
            elif etype == EventType.TOOL_RESULT:
                slot = self._tools.setdefault(str(data.get("tool", "unknown")),
                                              {"total": 0, "errors": 0})
                if not bool(data.get("ok", True)):
                    slot["errors"] += 1
            elif etype == EventType.DONE:
                self._accumulate_done(data)
                if not self._saw_done:
                    self._saw_done = True
                    self._settle_run_locked("completed")

    def on_run_finish(self, exc: BaseException | None = None) -> None:
        """run 结束时在 ``finally`` 调用；已见 done 的正常 run 不重复计数。

        ``exc`` 为穿透出事件流的异常：``asyncio.CancelledError`` /
        ``GeneratorExit`` / ``KeyboardInterrupt`` 计为 cancelled，其余计 failed。
        """
        with self._lock:
            if not self._active:
                return
            if self._saw_done:
                # 正常 / 体面收尾已在 DONE 结算，这里只复位瞬态。
                self._active = False
                return
            outcome = "cancelled" if self._is_cancellation(exc) else "failed"
            self._settle_run_locked(outcome)

    # ---------- 快照与渲染 ----------

    def snapshot(self) -> dict[str, Any]:
        """返回指标的深拷贝快照（调用方修改不影响收集器）。"""
        with self._lock:
            tools = {
                name: {"total": v["total"], "errors": v["errors"]}
                for name, v in sorted(self._tools.items())
            }
            buckets = {self._bucket_label(b): self._hist_buckets[b] for b in _DURATION_BUCKETS}
            buckets[_INF_LABEL] = self._hist_count
            return {
                "started_at": self._started_at,
                "uptime_seconds": round(time.time() - self._started_at, 3),
                "runs": dict(self._runs),
                "runs_by_strategy": dict(sorted(self._runs_by_strategy.items())),
                "tool_calls": tools,
                "model_calls": dict(self._model_calls),
                "events": dict(sorted(self._events.items())),
                "run_duration_seconds": {
                    "count": self._hist_count,
                    "sum": round(self._hist_sum, 6),
                    "buckets": buckets,
                },
            }

    def render_prometheus(self) -> str:
        """渲染 Prometheus 文本 exposition 格式（纯标准库，末尾带换行）。"""
        snap = self.snapshot()
        lines: list[str] = []

        def add_metric(
            metric: str, mtype: str, help_text: str, samples: list[tuple[str, float]]
        ) -> None:
            lines.append(f"# HELP {metric} {help_text}")
            lines.append(f"# TYPE {metric} {mtype}")
            for labels, value in samples:
                suffix = f"{{{labels}}}" if labels else ""
                lines.append(f"{metric}{suffix} {self._fmt_number(value)}")

        runs = snap["runs"]
        add_metric(
            "yai_agent_runs_total",
            "counter",
            "Agent runs by outcome (started/completed/failed/cancelled).",
            [
                (f'outcome="{outcome}"', float(runs[outcome]))
                for outcome in ("started", "completed", "failed", "cancelled")
            ],
        )

        add_metric(
            "yai_agent_runs_by_strategy_total",
            "counter",
            "Runs by selected routing strategy.",
            [
                (f'strategy="{self._escape(strategy)}"', float(count))
                for strategy, count in snap["runs_by_strategy"].items()
            ],
        )

        tool_samples: list[tuple[str, float]] = []
        for name, counts in snap["tool_calls"].items():
            ok = max(counts["total"] - counts["errors"], 0)
            tool_samples.append((f'tool="{self._escape(name)}",result="ok"', float(ok)))
            tool_samples.append(
                (f'tool="{self._escape(name)}",result="error"', float(counts["errors"]))
            )
        add_metric(
            "yai_agent_tool_calls_total",
            "counter",
            "Tool invocations by tool name and result.",
            tool_samples,
        )

        add_metric(
            "yai_agent_model_calls_total",
            "counter",
            "Model calls by cost tier (accumulated from done events).",
            [
                (f'tier="{tier}"', float(count))
                for tier, count in snap["model_calls"].items()
            ],
        )

        add_metric(
            "yai_agent_events_total",
            "counter",
            "Adaptive lifecycle events by type.",
            [
                (f'type="{self._escape(etype)}"', float(count))
                for etype, count in snap["events"].items()
            ],
        )

        # histogram：# TYPE 用基础名，数据行按 Prometheus 规范加 _bucket/_sum/_count。
        hist = snap["run_duration_seconds"]
        lines.append("# HELP yai_run_duration_seconds Agent run wall-clock duration in seconds.")
        lines.append("# TYPE yai_run_duration_seconds histogram")
        for label, count in hist["buckets"].items():
            lines.append(
                f'yai_run_duration_seconds_bucket{{le="{label}"}} '
                f"{self._fmt_number(float(count))}"
            )
        lines.append(f"yai_run_duration_seconds_sum {self._fmt_number(hist['sum'])}")
        lines.append(f"yai_run_duration_seconds_count {self._fmt_number(hist['count'])}")

        add_metric(
            "yai_uptime_seconds",
            "gauge",
            "Process uptime since the recorder was created.",
            [("", float(snap["uptime_seconds"]))],
        )
        return "\n".join(lines) + "\n"

    # ---------- 内部 ----------

    def _accumulate_done(self, data: dict[str, Any]) -> None:
        self._model_calls["standard"] += self._as_int(data.get("standard_calls"))
        self._model_calls["strong"] += self._as_int(data.get("strong_calls"))

    def _settle_run_locked(self, outcome: str) -> None:
        """结算一次 run：累加结果计数与时延直方图，并复位瞬态（调用方持锁）。"""
        self._runs[outcome] += 1
        elapsed = max(time.monotonic() - self._run_start_mono, 0.0)
        self._hist_count += 1
        self._hist_sum += elapsed
        for bound in _DURATION_BUCKETS:
            if elapsed <= bound:
                self._hist_buckets[bound] += 1
        self._active = False

    @staticmethod
    def _is_cancellation(exc: BaseException | None) -> bool:
        return exc is not None and isinstance(exc, _CANCELLATION_TYPES)

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(number, 0)

    @staticmethod
    def _bucket_label(bound: float) -> str:
        return str(int(bound)) if float(bound).is_integer() else str(bound)

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    @staticmethod
    def _fmt_number(value: float | int) -> str:
        if isinstance(value, int) or float(value).is_integer():
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")
