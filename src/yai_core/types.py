"""YAI Agent Core 的核心数据类型。

刻意只依赖标准库：内核本体零第三方依赖，第三方能力全部通过 SPI 注入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from yai_core.governance import (  # noqa: F401 - re-exported for compatibility
    APPROVAL_OPS,
    ApprovalPolicy,
    ApprovalRule,
    GateDecision,
    RateLimitHint,
    ToolGovernance,
    governance_from_manifest,
    governance_to_manifest,
    parse_window_seconds,
    validate_egress_paths,
)
from yai_core.receipts import (  # noqa: F401 - re-exported for compatibility
    ExecutionReceipt,
    ReceiptStatus,
    verify_receipt_chain,
)

Role = Literal["system", "user", "assistant", "tool"]


class Strategy(StrEnum):
    """Adaptive Router 可选的执行策略。"""

    DIRECT = "direct"      # 无需工具，模型直接回答
    REACT = "react"        # 工具循环：推理 -> 调用 -> 观察 -> 再推理
    PLAN = "plan"          # 先拆解计划，再按计划执行工具循环
    CLARIFY = "clarify"    # 意图不清，先向宿主/用户反问


class EventType(StrEnum):
    """Observer 事件流类型——每一次"自适应决策"都必须可观测。"""

    STRATEGY_SELECTED = "strategy_selected"
    PLAN_CREATED = "plan_created"
    MODEL_MESSAGE = "model_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION_ASKED = "permission_asked"
    #: 调用在**执行前**被拒（策略 / 闸门 / 审批 / 频率），是一次真正的治理决策。
    PERMISSION_DENIED = "permission_denied"
    CLARIFY_REQUESTED = "clarify_requested"
    CAPABILITY_MISSING = "capability_missing"
    TOOL_DISCOVERED = "tool_discovered"
    TOOL_COMPOSED = "tool_composed"
    CODE_TOOL_CREATED = "code_tool_created"
    CODE_TOOL_RETIRED = "code_tool_retired"
    CONTEXT_COMPACTED = "context_compacted"
    MEMORIES_RECALLED = "memories_recalled"
    HOST_STATE = "host_state"
    PLAN_REVISED = "plan_revised"
    TIER_CHANGED = "tier_changed"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"
    DONE = "done"


@dataclass(frozen=True)
class Subject:
    """宿主在当前运行上下文里注入的**可信操作主体**。

    只有宿主能构造它：模型输出、工具参数、上游消息里抄来的"我是某某"都不算
    ——接收方必须通过自己的可信上下文或显式委托绑定来判定主体可信性。
    内核用它做能力准入（如外部契约的 ``subject.required``）并交给声明了
    ``yai_subject_aware`` 的权限策略；身份到底能不能做这件事，仍由宿主业务层裁决。
    """

    subject_id: str
    display_name: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.subject_id).strip():
            raise ValueError("Subject.subject_id 必须是非空字符串")


@dataclass
class CompositeStep:
    """组合工具中的一步：调用一个已注册的内部工具，参数可引用前序结果。

    - tool：被组合的内部工具名（必须是宿主已注册的工具，组合不产生新原始能力）；
    - args：传给该工具的参数，支持占位符 ``{{$input.x}}``（组合工具入参）、
      ``{{$steps.0}}``（第 0 步完整结果）、``{{$steps.0.field}}``（字典字段）。
    """

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChangePreview:
    """宿主生成的**实际变更预览**，用于审批与执行前复核。

    ``token`` 是宿主自己计算的版本/哈希（例如目标文件内容 + 参数），内核只比较
    两次生成的结果是否相同，不解释它。审批人看到的是宿主渲染的 ``diff``，不是
    模型编造的描述；执行前 token 变了就说明审批所依据的计划已经失效。
    """

    summary: str
    token: str
    diff: str = ""
    target: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("ChangePreview.summary 必须是非空字符串")
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("ChangePreview.token 必须是非空字符串")
        for name in ("diff", "target"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"ChangePreview.{name} 必须是字符串")

    @classmethod
    def from_value(cls, value: Any) -> ChangePreview:
        """接受 ChangePreview 或普通 mapping；其他形状直接拒绝。"""
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                summary=value.get("summary", ""),
                token=value.get("token", ""),
                diff=value.get("diff", ""),
                target=value.get("target", ""),
            )
        raise TypeError("变更预览必须是 ChangePreview 或 mapping")

    def to_dict(self) -> dict[str, str]:
        return {
            "summary": self.summary,
            "token": self.token,
            "diff": self.diff,
            "target": self.target,
        }


@dataclass
class ToolSpec:
    """统一工具规格。Native / OpenAPI / MCP / 组合工具在 Registry 中同构。"""

    name: str
    description: str
    input_schema: dict[str, Any]          # JSON Schema（与 MCP tools 形状一致）
    handler: Callable[..., Any] | None    # 同步函数；异步函数同样支持；组合/代码工具为 None
    source: Literal["native", "openapi", "mcp", "composite", "code"] = "native"
    # 仅 source == "composite" 时使用：按顺序编排的内部工具步骤。
    steps: list[CompositeStep] | None = None
    # 仅 source == "code" 时使用：模型生成、需在宿主沙箱里执行的 Python 代码。
    # 内核不内置任何代码执行器（见 ToolSandbox SPI），handler 保持 None。
    code: str | None = None
    #: 版本标记（可选，纯数据）。组合工具用它标出"这套流程是 v1 / v2"；
    #: 随能力清单跨进程走，老读者不认识这个键就当没有（向后兼容）。
    version: str = ""
    # 宿主声明的治理元数据（脱敏、风险等）；默认空声明 = 行为与从前一致。
    governance: ToolGovernance = field(default_factory=ToolGovernance)

    def llm_schema(self) -> dict[str, Any]:
        """转换成 OpenAI 兼容的 function-calling 工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    #: 合法的工具来源（manifest 反序列化时校验，防止脏数据进入注册表）。
    _SOURCES = ("native", "openapi", "mcp", "composite", "code")

    def to_manifest_dict(self) -> dict[str, Any]:
        """导出语言中立、可 JSON 序列化的能力清单（**不含运行时 handler**）。

        - composite：随清单携带 ``steps``（每步 {tool, args}），工作流即数据；
        - code：随清单携带 ``code`` 字符串（执行仍需宿主沙箱）；
        - native/openapi/mcp：handler 是运行时对象、不可移植，只导出声明；
        - governance：导出 ``sensitive`` / ``audit_sensitive`` / ``acc`` 快照 /
          ``approval`` 审批策略（都是数据）；``gate`` 是**函数**，与 handler 同命——
          不可序列化，导出即丢弃。

        无治理声明时整个 ``governance`` 键都不写，保持清单与旧格式逐字节一致。
        """
        manifest: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "source": self.source,
        }
        if self.source == "composite" and self.steps:
            manifest["steps"] = [
                {"tool": step.tool, "args": step.args} for step in self.steps
            ]
        if self.source == "code":
            manifest["code"] = self.code
        if self.version:
            manifest["version"] = self.version
        governance = governance_to_manifest(self.governance)
        if governance:
            manifest["governance"] = governance
        return manifest

    @classmethod
    def from_manifest_dict(cls, data: dict[str, Any]) -> ToolSpec:
        """从语言中立清单重建工具规格；``handler`` 始终为 ``None``。

        composite / code 的可执行信息（steps / code）随清单携带，注册后即可
        （在宿主沙箱内）执行；native/openapi/mcp 仅为能力声明，需另行绑定
        执行后端（跨语言接入请直接走 MCP / OpenAPI 集成，它们自带 handler）。

        治理块同样随清单走（脱敏路径、宽口径审计、契约声明快照、原生审批要求），但
        ``governance.gate`` 与 handler 一样是运行时函数，**不可能来自清单**：
        跨进程 / 跨语言导入后函数级准入判定为空，要恢复必须由宿主显式重新绑定::

            from dataclasses import replace
            from yai_core.integrations.acc import build_gate, parse_declaration

            parsed = parse_declaration(spec.governance.acc)
            if parsed.valid and parsed.declaration is not None:
                spec.governance = replace(
                    spec.governance,
                    gate=build_gate(parsed.declaration, spec.input_schema),
                )

        内核不会拿清单里的 ``acc`` 快照自动造 gate——外来清单不能给本机工具
        加上或摘掉**函数级**闸门。清单里的 ``approval`` 则导入即生效：它是数据、
        且只会把闸门往上抬（要多问人 / 要拒这次调用），不存在"外来 JSON 替宿主放行"
        这条路。
        """
        if not isinstance(data, dict):
            raise TypeError(f"manifest 工具必须是 object，得到 {type(data)!r}")
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("manifest 工具缺少非空 name")
        source = data.get("source", "native")
        if source not in cls._SOURCES:
            raise ValueError(
                f"manifest 工具 {name!r} 的 source 非法: {source!r}，"
                f"合法值 {cls._SOURCES}"
            )

        steps: list[CompositeStep] | None = None
        if source == "composite":
            raw_steps = data.get("steps")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise ValueError(f"组合工具 {name!r} 的 manifest 缺少非空 steps")
            steps = []
            for index, raw in enumerate(raw_steps):
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"组合工具 {name!r} 第 {index + 1} 步必须是 object"
                    )
                tool = str(raw.get("tool", "")).strip()
                if not tool:
                    raise ValueError(
                        f"组合工具 {name!r} 第 {index + 1} 步缺少 tool"
                    )
                steps.append(
                    CompositeStep(tool=tool, args=dict(raw.get("args", {})))
                )

        code = data.get("code") if source == "code" else None
        version = str(data.get("version", "") or "").strip()
        input_schema = dict(data.get("input_schema") or {})
        return cls(
            name=name,
            description=str(data.get("description", "")),
            input_schema=input_schema,
            handler=None,
            source=source,
            steps=steps,
            code=code,
            version=version,
            governance=governance_from_manifest(
                name, data.get("governance"), input_schema
            ),
        )


@dataclass(frozen=True)
class TextPart:
    """纯文本片段（多模态消息里的文本块）。"""

    text: str

    def to_openai_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}

    def to_text(self) -> str:
        return self.text

    def to_reference(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


#: 内联图片的**字节上限**（按 base64 载荷估）。不是"建议"——超了直接拒：
#: 图片会整块进模型请求，超大载荷既烧钱又可能把请求撑爆。
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024


def _estimate_base64_bytes(payload: str) -> int:
    """估算 base64 载荷的字节数（去掉 data: 前缀与换行，按 4 字符 → 3 字节）。"""
    text = payload.split(",", 1)[1] if payload.startswith("data:") and "," in payload else payload
    text = "".join(text.split())
    return (len(text) * 3) // 4


@dataclass(frozen=True)
class ImagePart:
    """图片片段。

    两条路径，**互斥择一**：

    - ``data``：宿主在信任边界上解析好的内联 base64（可带 ``data:`` 前缀直接用）；
    - ``ref``：宿主签发的引用（``ref`` 由宿主自己解析，内核**不替它取**任何外部 URL）。

    只有内联的那条能被模型真正看到；只有引用的那条会渲染成**明说"模型看不到"的文字
    占位**——绝不静默丢图，否则模型会在缺资料的情况下照常回答，看起来成功、其实答错。

    内联载荷有**硬上限**（:data:`MAX_INLINE_IMAGE_BYTES`）：图片整块进模型请求，
    超大载荷既烧钱又可能把请求撑爆，所以超限在构造时就拒，而不是等请求失败。
    """

    media_type: str = "image/png"
    data: str = ""
    ref: str = ""

    def __post_init__(self) -> None:
        if self.data:
            size = _estimate_base64_bytes(self.data)
            if size > MAX_INLINE_IMAGE_BYTES:
                raise ValueError(
                    f"内联图片约 {size} 字节，超过上限 {MAX_INLINE_IMAGE_BYTES} 字节；"
                    "请改用宿主签发的引用（ref）由宿主在授权边界内解析"
                )

    def to_openai_dict(self) -> dict[str, Any]:
        if self.data:
            url = self.data
            if not url.startswith("data:"):
                url = f"data:{self.media_type};base64,{self.data}"
            return {"type": "image_url", "image_url": {"url": url}}
        return {"type": "text", "text": self.to_text()}

    def to_text(self) -> str:
        if self.data:
            return f"[图片：{self.media_type}]"
        return f"[图片引用：{self.ref or '（无引用）'} —— 未内联，模型看不到这张图]"

    def to_reference(self) -> dict[str, Any]:
        """持久化形态：**只留元数据与引用，不落图片字节**。"""
        return {"type": "image", "media_type": self.media_type, "ref": self.ref}


@dataclass(frozen=True)
class ResourcePart:
    """资源引用片段（宿主签发的文件 / 对象引用）。

    内核**不解析、不抓取**任何外部资源：引用只是"告诉模型有这么个东西存在"，
    真正的读取由宿主在授权边界内完成。落盘时只保留引用与元数据。

    四个字段把"引用"这件事的语义钉死（都是**声明**，不是执行）：

    - ``uri``：引用本身（宿主内部可解析的句柄，如 ``res://host/42``）；
    - ``media_type``：资源类型（提示用）；
    - ``scope``：**授权范围提示**——这条引用允许被谁用（宿主自己的判据，内核不解释）；
    - ``expires_at``：**失效时刻**（ISO 8601）。过期后引用不再有效；内核只把它
      如实写进渲染文本，**是否已过期由宿主判定**（内核没有时钟真相）。

    为什么把这些都做成字段而不是注释：引用的"生命周期、授权范围、访问方式、失效
    行为"如果只存在于文档里，宿主各写各的，跨进程传递时更没人知道它们。
    """

    uri: str
    media_type: str = "application/octet-stream"
    name: str = ""
    scope: str = ""
    expires_at: str = ""

    def to_openai_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.to_text()}

    def to_text(self) -> str:
        label = self.name or self.uri
        extra = []
        if self.scope:
            extra.append(f"作用域 {self.scope}")
        if self.expires_at:
            extra.append(f"{self.expires_at} 失效")
        suffix = ("，" + "，".join(extra)) if extra else ""
        return (
            f"[资源引用：{label}（{self.media_type}）{suffix}"
            "—— 内容由宿主提供，内核未内联]"
        )

    def to_reference(self) -> dict[str, Any]:
        return {
            "type": "resource",
            "uri": self.uri,
            "media_type": self.media_type,
            "name": self.name,
            "scope": self.scope,
            "expires_at": self.expires_at,
        }


#: 多模态消息片段的联合类型。
ContentPart = TextPart | ImagePart | ResourcePart


def flatten_content_blocks(blocks: Any) -> str:
    """把 OpenAI 兼容的内容数组压成纯文本。

    给不支持多模态的后端与故障切换包装用；图片与未知块都显式说明"看不到"，
    不做静默丢弃——模型宁可知道自己缺资料，也不该把缺图当成没发生过。
    """
    if not isinstance(blocks, list):
        return "" if blocks is None else str(blocks)
    chunks: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text", ""))
            if text:
                chunks.append(text)
            continue
        if block.get("type") == "image_url":
            chunks.append("[图片：当前后端不支持多模态，模型看不到这张图]")
            continue
        chunks.append("[未知片段：当前后端不支持多模态]")
    return "\n".join(chunks)


def parts_to_text(parts: Any) -> str:
    """把消息片段渲染成纯文本（给不支持多模态的模型、上下文摘要、降级路径用）。

    渲染**逐字保留信息量**：图片与资源会带上"模型看不到它"的明说，不做静默丢弃。
    """
    if not parts:
        return ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, (TextPart, ImagePart, ResourcePart)):
            chunks.append(part.to_text())
        else:  # 宿主自造的部分：能取文本就取，取不到就明说
            text = getattr(part, "text", None)
            chunks.append(str(text) if text is not None else "[未知片段]")
    return "\n".join(chunk for chunk in chunks if chunk)


def parts_from_references(raw: Any) -> list[ContentPart] | None:
    """从持久化形态（``to_reference`` 的产物）还原消息片段。

    还原出来的图片**没有内联数据**（字节不落盘），因此它们在下游会渲染成
    "模型看不到"的占位——这是刻意的：历史里的图片字节不长期驻留。
    """
    if not isinstance(raw, list) or not raw:
        return None
    parts: list[ContentPart] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(TextPart(text=str(item.get("text", ""))))
        elif kind == "image":
            parts.append(
                ImagePart(
                    media_type=str(item.get("media_type", "image/png")),
                    ref=str(item.get("ref", "")),
                )
            )
        elif kind == "resource":
            parts.append(
                ResourcePart(
                    uri=str(item.get("uri", "")),
                    media_type=str(item.get("media_type", "application/octet-stream")),
                    name=str(item.get("name", "")),
                    scope=str(item.get("scope", "")),
                    expires_at=str(item.get("expires_at", "")),
                )
            )
    return parts or None


@dataclass
class ChatMessage:
    role: Role
    content: str = ""
    # assistant 发起的工具调用：[{"id","name","arguments"}]
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    #: 多模态片段（图片 / 资源引用等）。**默认 None = 纯文本消息**，与旧行为逐字一致；
    #: 有片段时 ``to_llm_dict()`` 输出 OpenAI 兼容的内容数组形态。
    parts: list[ContentPart] | None = None

    def to_llm_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role}
        if self.parts:
            blocks: list[dict[str, Any]] = []
            if self.content:
                blocks.append({"type": "text", "text": self.content})
            blocks.extend(part.to_openai_dict() for part in self.parts)
            d["content"] = blocks
        else:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    """单次模型调用的 token 用量（OpenAI 兼容口径）。

    由模型后端在 :class:`ModelResponse` 中可选返回；拿不到用量的后端留 None，
    内核的 token 预算据此"能算才算"，绝不臆造数字。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ModelResponse:
    """ModelProvider 的统一返回，屏蔽各家 SDK 差异。"""

    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    raw: Any = None
    # 本次调用的 token 用量；后端不提供时为 None（token 预算此时不累计）。
    usage: TokenUsage | None = None


@dataclass
class AgentEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    strategy: Strategy
    events: list[AgentEvent]
    final_text: str
    #: 任务级验收结果（v0.26 Outcome Verification）；未配置 verifier 时为 None。
    #: 注意它回答的是"目标是否达成"，不是"工具是否调用成功"——后者看收据。
    verification: VerificationResult | None = None
    #: 任务终态（加性字段，默认 ``"ok"``）：``"ok"`` / ``"error"``。
    #: 宿主不必再扫描事件流才能判断这次 run 是否失败；失败原因见 :attr:`error`。
    status: str = "ok"
    #: 失败原因（``status == "error"`` 时的可展示文本；成功时为 None）。
    error: str | None = None


#: 任务级验收状态（与执行收据的 ReceiptStatus 刻意分开）：
#: - ``verified_success``：有确定性证据表明目标已达成；
#: - ``verified_failure``：有确定性证据表明目标未达成（或明确失败）；
#: - ``partial``：部分子目标达成、部分未达成 / 无法确认；
#: - ``unknown``：动作可能已发生，但当前无法确认结果（不得盲目重试）；
#: - ``not_verifiable``：宿主没有提供可判定证据（不代表成功，也不代表失败）。
VERIFICATION_STATUSES = (
    "verified_success",
    "verified_failure",
    "partial",
    "unknown",
    "not_verifiable",
)

#: 证据摘要的字符上限：证据会进事件与宿主日志，不允许无限长。
EVIDENCE_SUMMARY_MAX = 500


@dataclass(frozen=True)
class Evidence:
    """一条验收证据。

    ``kind`` 由宿主 / 适配器定义（如 ``receipt``、``file``、``api_state``、
    ``page``、``navigation``），内核只负责透传、截断与序列化，不解释语义。

    - ``ok``：``True`` 证明达成 / ``False`` 证明未达成 / ``None`` 仅作信息；
    - ``summary``：给人看的一句话（上限 :data:`EVIDENCE_SUMMARY_MAX`）；
    - ``detail``：结构化上下文（只允许 JSON 可序列化的浅层值）。
    """

    kind: str
    summary: str
    ok: bool | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.kind).strip():
            raise ValueError("Evidence.kind 必须是非空字符串")
        if not isinstance(self.ok, (bool, type(None))):
            raise ValueError("Evidence.ok 必须是 True / False / None")
        if len(self.summary) > EVIDENCE_SUMMARY_MAX:
            object.__setattr__(self, "summary", self.summary[:EVIDENCE_SUMMARY_MAX])

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "summary": self.summary}
        if self.ok is not None:
            out["ok"] = self.ok
        if self.detail:
            out["detail"] = dict(self.detail)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Evidence:
        return cls(
            kind=str(data.get("kind", "")),
            summary=str(data.get("summary", "")),
            ok=data.get("ok"),
            detail=dict(data.get("detail") or {}),
        )


@dataclass(frozen=True)
class VerificationResult:
    """任务级验收结论：状态 + 置信度 + 证据。

    与 :class:`ExecutionReceipt` 的分工：收据记录"每个工具调用发生了什么"，
    验收结果记录"用户目标是否达成"。二者可以不一致——例如所有工具都成功，
    但页面仍显示"支付失败"（工具调用成功 ≠ 任务成功）。
    """

    status: str
    goal: str = ""
    confidence: float = 0.0
    reason: str = ""
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in VERIFICATION_STATUSES:
            raise ValueError(
                f"VerificationResult.status 必须是 {VERIFICATION_STATUSES} 之一，"
                f"收到 {self.status!r}"
            )
        confidence = float(self.confidence)
        if confidence != confidence:  # NaN
            raise ValueError("VerificationResult.confidence 不能是 NaN")
        object.__setattr__(self, "confidence", max(0.0, min(1.0, confidence)))

    @property
    def is_success(self) -> bool:
        return self.status == "verified_success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "goal": self.goal,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationResult:
        evidence = tuple(
            Evidence.from_dict(item)
            for item in (data.get("evidence") or [])
            if isinstance(item, Mapping)
        )
        return cls(
            status=str(data.get("status", "")),
            goal=str(data.get("goal", "")),
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "")),
            evidence=evidence,
        )
