"""零依赖本地诊断事件：给宿主一条"旁路排障"通道。

它与另外两套可观测机制分工不同、互不替代：

- ``AgentEvent`` 事件流：面向**业务与产品**，记录每一次自适应决策（选了什么
  策略、调了什么工具、缺口、压缩……），是宿主 UI 与回放的主数据；
- ``DiagnosticEvent``（本模块）：面向**运维与排障**，专门承接那些"内核已经
  静默兜底、主流程不会失败、但出问题时必须看得见"的瞬间——模型硬超时、LLM
  路由失败回退规则、工具发现失败、上下文压缩失败回退硬裁剪、多模型降级等；
- OpenTelemetry（可选 extra，见 ``integrations/tracing``）：面向**分布式链路
  追踪**，回答跨进程/跨服务的耗时与因果。单机嵌入式排障用本模块即可，不必引
  OTel。

设计约束（与内核红线一致）：

- **零第三方硬依赖、不引入 logging**：内核不决定日志写到哪、用什么框架；
  宿主传一个普通同步回调（``DiagnosticSink``）即可，想接 logging、structlog、
  Sentry、文件或 UI 通知都由宿主决定。
- **回调是旁路，永不反噬主流程**：回调自身抛任何异常都会被 ``emit`` 吞掉，
  诊断不能反过来把一次本来可以兜底成功的任务弄崩。
- **回调应快速、非阻塞**：它在事件循环线程里被同步调用，不要在里面做网络
  请求或重计算；需要的话宿主自行把事件丢进队列异步处理。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DiagnosticLevel(StrEnum):
    """诊断级别。沿用 logging 习惯，但不依赖 logging。"""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticKind(StrEnum):
    """内核内置的诊断事件种类（机器可识别，宿主可按 kind 过滤/告警）。"""

    #: 模型调用抛出非超时异常（规划/收尾/react 等路径），已发 ERROR 事件兜底。
    MODEL_ERROR = "model_error"
    #: 模型调用触发硬超时上限（AgentLoop.model_timeout），已终止并体面收尾。
    MODEL_TIMEOUT = "model_timeout"
    #: FallbackModelProvider 中某个模型可重试失败、降级到下一个后端。
    MODEL_FALLBACK = "model_fallback"
    #: LLM 路由分类异常/超时/非法输出，已回退到关键词规则。
    ROUTER_LLM_FALLBACK = "router_llm_fallback"
    #: 能力缺口出现后，工具发现源 discover() 抛错（增强失败，本次不注册新工具）。
    DISCOVERY_FAILED = "discovery_failed"
    #: 拉取"可发现能力目录"失败（静默退化为不展示目录）。
    DISCOVERABLE_LIST_FAILED = "discoverable_list_failed"
    #: 上下文摘要压缩失败/超时，已回退到硬裁剪（不破坏结构、不阻断任务）。
    COMPACTION_FALLBACK = "compaction_fallback"
    #: 工具 schema 两层聚焦失败，已回退注入全量 schema（宁多勿漏）。
    SCHEMA_FOCUS_FALLBACK = "schema_focus_fallback"


@dataclass
class DiagnosticEvent:
    """一次本地诊断信号。

    - ``kind``：机器可识别的种类（见 :class:`DiagnosticKind``，宿主也可自定义
      新字符串）；
    - ``message``：给人看的一句话说明（中文，可直接进日志）；
    - ``level``：严重级别；
    - ``data``：结构化上下文（模型名、异常类型、工具名等），不得放密钥/PII。
    """

    kind: str
    message: str
    level: DiagnosticLevel = DiagnosticLevel.WARNING
    data: dict[str, Any] = field(default_factory=dict)


#: 宿主提供的同步诊断回调；None 表示不接收诊断（内核默认安静运行）。
DiagnosticSink = Callable[[DiagnosticEvent], None]


def emit(sink: DiagnosticSink | None, event: DiagnosticEvent) -> None:
    """把诊断事件交给宿主回调；任何情况下都不允许影响内核主流程。

    - ``sink`` 为 None 时直接返回（零开销的默认形态）；
    - 回调抛异常（含它内部再触发的错误）一律吞掉——诊断是旁路，不能反噬。
    """
    if sink is None:
        return
    try:
        sink(event)
    except Exception:  # noqa: BLE001 - 诊断旁路不得反噬主流程
        pass
