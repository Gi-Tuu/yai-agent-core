"""YAI Agent Core 的核心数据类型。

刻意只依赖标准库：内核本体零第三方依赖，第三方能力全部通过 SPI 注入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

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
    CLARIFY_REQUESTED = "clarify_requested"
    CAPABILITY_MISSING = "capability_missing"
    TOOL_DISCOVERED = "tool_discovered"
    TOOL_COMPOSED = "tool_composed"
    CODE_TOOL_CREATED = "code_tool_created"
    CODE_TOOL_RETIRED = "code_tool_retired"
    CONTEXT_COMPACTED = "context_compacted"
    MEMORIES_RECALLED = "memories_recalled"
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
class GateDecision:
    """工具自带准入判定（``ToolGovernance.gate``）的返回值：内核的中性语言。

    内核不知道这个结论是谁算的（外部能力契约、宿主策略引擎、都行），只按三条话行动：

    - ``reject`` 非空：这次调用不该到达业务操作，直接失败并把理由回灌给模型；
    - ``require_approval``：要人工确认——**只能把闸门往上抬**，权限策略的 DENY 永远优先；
    - 两者皆假：这一层对这次调用没有意见。

    ``risk`` / ``scope`` / ``target`` 是附带信息（进诊断、审批话术与审计事件，
    见 :func:`yai_core.policy.authorize.authorize_tool_call`），不参与内核决策。
    其中 ``target`` 专门用来把"这次调用**实际会碰哪个资源**"说清楚：审批人看到的
    应当是解析后的落点，而不是模型原样写来的字符串。
    """

    reject: str = ""
    require_approval: bool = False
    reason: str = ""
    prompt: str = ""
    risk: str = ""
    scope: str = ""
    target: str = ""


#: 参数级审批支持的算子（严格 JSON 值语义，见 :mod:`yai_core.policy.approval`）。
APPROVAL_OPS = (">", ">=", "<", "<=", "==", "!=", "in", "contains", "exists")


@dataclass(frozen=True)
class ApprovalRule:
    """一条参数级审批条件：命中就把这次调用抬成"要人确认"。

    这是 **YAI 原生**的声明单元，不来自任何外部协议：``param`` 是点号路径，
    ``op`` 必须落在 :data:`APPROVAL_OPS` 里，比较一律走严格 JSON 值语义
    （``"100" != 100``、``true != 1``），判不出来就失败关闭为拒绝。

    ``has_value`` 说清"声明里到底带没带值"：``exists`` 本来没有值，而 ``== null`` 的值是
    货真价实的 JSON null，两者不能靠 ``value is None`` 区分。手写规则时最容易忘 ——
    所以 ``__post_init__`` 见到非 None 的值就把它补上：忘了写只是省事，不是想改语义。
    """

    param: str
    op: str
    value: Any = None
    has_value: bool = False
    label: str = ""

    def __post_init__(self) -> None:
        if self.value is not None and not self.has_value:
            object.__setattr__(self, "has_value", True)


@dataclass(frozen=True)
class ApprovalPolicy:
    """宿主给一个工具写的审批要求。

    与 :attr:`ToolGovernance.gate` 的区别：``gate`` 是**函数**（宿主自己算，不可序列化），
    ``approval`` 是**数据**（可随能力清单走，导入端无需任何外部协议就能恢复强制）。
    两者都只能把闸门往上抬——``required`` 与命中的 ``when`` 只会把"允许"变成"要问人"，
    永远压不过权限策略的 DENY。
    """

    required: bool = False
    prompt: str = ""
    when: tuple[ApprovalRule, ...] = ()


@dataclass(frozen=True)
class ToolGovernance:
    """宿主声明的工具治理元数据。

    只由宿主/集成层填写，**模型输出永远不能改它**；缺省即"无声明"，
    此时内核保持原有行为（不猜、不假装保护）。

    - ``sensitive``：需要脱敏的**参数路径**（dot-notation，如
      ``api_key``、``headers.authorization``、``rows.0.token``）。声明了才会被
      脱敏——内核不按字段名关键词猜"这像不像密钥"，因为猜出来的东西不能当安全承诺。
    - ``audit_sensitive``：**宽口径**审计敏感提示（外部能力契约的 ``audit.sensitive``）。
      只把"进审计/日志的那一份"整体收口，**审批人要看的副本仍按路径脱敏**——
      把所有值都糊掉会让人工批准失去判断依据，而规范的义务范围是 logs。
    - ``acc``：外部能力契约声明的**原样快照**（已校验过的 mapping）。内核只透传不解释，
      它本身**不产生任何执行效果**；要让声明参与准入，得配上下面的 ``gate``。
    - ``gate``：工具自带的**准入判定函数** ``(arguments, subject) -> GateDecision``，
      由宿主或集成层提供（``integrations/acc`` 用已校验的声明造一个）。每次调用现场算，
      用当时的参数与当时的主体；内核不 import 任何协议实现，只按 :class:`GateDecision` 行动。
    - ``approval``：**YAI 原生**的参数级审批要求（数据，不是函数）。宿主不接任何外部
      协议也能表达"超过这个条件就要人确认"；它是数据，所以能随能力清单走、导入端
      无需外部协议即可恢复强制。判定语义见 :mod:`yai_core.policy.approval`。
    """

    sensitive: tuple[str, ...] = ()
    audit_sensitive: bool = False
    acc: Mapping[str, Any] | None = None
    # 放在最后（而不是紧跟 acc）：`gate` 一直是第 4 个位置参数，新字段插在它前面
    # 会让既有位置写法的 ToolGovernance(..., acc, my_gate) 把闸门关到 approval 上——
    # 静默改变绑定对象，比多一个换行贵得多。
    gate: Callable[[dict[str, Any], Any], GateDecision] | None = None
    approval: ApprovalPolicy | None = None


#: 清单里可携带的治理字段白名单；不在表内的键**拒绝加载**（见
#: :func:`governance_from_manifest` 的理由说明）。
_GOVERNANCE_KEYS = ("sensitive", "audit_sensitive", "acc", "approval")

_APPROVAL_KEYS = ("required", "prompt", "when")
_APPROVAL_RULE_KEYS = ("param", "op", "value", "label")


def _approval_to_manifest(policy: ApprovalPolicy) -> dict[str, Any]:
    """审批策略降成纯数据；``value`` 只在"声明里确实带了值"时才出键。

    ``exists`` 不需要比较值，而 ``== null`` 的值是货真价实的 JSON null。用键的**存在与否**
    区分这两者，比多写一个 ``has_value`` 布尔更接近手写清单的样子，也更不容易自相矛盾
    （``{"op": "exists", "value": 3}`` 这种组合根本表达不出来）。
    """
    rules: list[dict[str, Any]] = []
    for rule in policy.when:
        item: dict[str, Any] = {"param": rule.param, "op": rule.op}
        if rule.has_value:
            item["value"] = rule.value
        if rule.label:
            item["label"] = rule.label
        rules.append(item)
    out: dict[str, Any] = {}
    if policy.required:
        out["required"] = True
    if policy.prompt:
        out["prompt"] = policy.prompt
    if rules:
        out["when"] = rules
    return out


def _approval_from_manifest(name: str, raw: Any) -> ApprovalPolicy | None:
    """校验入站清单的 ``governance.approval``（外来数据，逐字段严校验）。

    入站校验必须比出站更严：这份数据来自别的进程/别的语言，而它**会真的改变执行行为**
    （抬闸门、拒调用）。特别是算子必须落在 :data:`APPROVAL_OPS` 里——放一个拼错的算子
    进来，判定会失败关闭成"每次都拒"，看起来像安全，其实只是把工具搞瘫。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"工具 {name!r} 的 governance.approval 必须是 object，得到 {type(raw)!r}"
        )
    unknown = sorted(set(raw) - set(_APPROVAL_KEYS))
    if unknown:
        raise ValueError(
            f"工具 {name!r} 的 governance.approval 含内核不认识的字段 {unknown}，"
            f"合法字段只有 {list(_APPROVAL_KEYS)}；静默忽略会让审批要求落空，故拒绝加载"
        )

    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise ValueError(
            f"工具 {name!r} 的 governance.approval.required 必须是布尔值，得到 {required!r}"
        )
    prompt = raw.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError(
            f"工具 {name!r} 的 governance.approval.prompt 必须是字符串，得到 {prompt!r}"
        )
    raw_when = raw.get("when", [])
    if not isinstance(raw_when, (list, tuple)):
        raise ValueError(
            f"工具 {name!r} 的 governance.approval.when 必须是数组，得到 {type(raw_when)!r}"
        )
    rules: list[ApprovalRule] = []
    for index, item in enumerate(raw_when):
        if not isinstance(item, dict):
            raise ValueError(
                f"工具 {name!r} 的 approval.when 第 {index + 1} 条必须是 object"
            )
        bad = sorted(set(item) - set(_APPROVAL_RULE_KEYS))
        if bad:
            raise ValueError(
                f"工具 {name!r} 的 approval.when 第 {index + 1} 条含未知字段 {bad}，"
                f"合法字段只有 {list(_APPROVAL_RULE_KEYS)}"
            )
        param = item.get("param")
        if not isinstance(param, str) or not param.strip():
            raise ValueError(
                f"工具 {name!r} 的 approval.when 第 {index + 1} 条缺少合法 param"
            )
        op = item.get("op")
        if op not in APPROVAL_OPS:
            raise ValueError(
                f"工具 {name!r} 的 approval.when 第 {index + 1} 条算子非法：{op!r}，"
                f"合法算子 {list(APPROVAL_OPS)}"
            )
        label = item.get("label", "")
        if not isinstance(label, str):
            raise ValueError(
                f"工具 {name!r} 的 approval.when 第 {index + 1} 条 label 必须是字符串"
            )
        rules.append(
            ApprovalRule(
                param=param.strip(),
                op=op,
                value=item.get("value"),
                has_value="value" in item,
                label=label,
            )
        )
    return ApprovalPolicy(required=required, prompt=prompt, when=tuple(rules))


def governance_to_manifest(governance: ToolGovernance) -> dict[str, Any]:
    """把治理声明降成纯 JSON 数据；``gate`` 是函数，**不出现在输出里**。

    空声明返回 ``{}``，调用方据此省略整个键——不给未治理的工具制造噪声字段。
    """
    out: dict[str, Any] = {}
    if governance.sensitive:
        out["sensitive"] = list(governance.sensitive)
    if governance.audit_sensitive:
        out["audit_sensitive"] = True
    if governance.acc is not None:
        out["acc"] = dict(governance.acc)
    if governance.approval is not None:
        approval = _approval_to_manifest(governance.approval)
        if approval:
            out["approval"] = approval
    return out


def governance_from_manifest(name: str, raw: Any) -> ToolGovernance:
    """校验入站清单的治理块，返回 :class:`ToolGovernance`（``gate`` 恒为 None）。

    三条硬规矩：

    1. **不认识的键直接拒绝**。清单是数据交换格式，静默忽略一个写错的
       ``sensitives``（本意要脱敏）就等于"声明了保护但内核没做"，比报错更危险；
       真要加字段就升 ``MANIFEST_VERSION``，别靠老读者猜。
    2. **``acc`` 只校验形状，不解释内容**；``approval`` 反过来，逐字段严校验，
       因为它**会真的参与准入判定**（见 :func:`_approval_from_manifest`）。
    3. **绝不凭清单造 ``gate``**。外来清单只是"别人说这个工具有这些约束"，
       函数级的准入逻辑必须由本地宿主显式绑定（``integrations/acc.build_gate``）；
       否则一份外来 JSON 就能给本机工具加上或摘掉闸门。
       清单里的 ``approval`` 确实会生效，但它只能**抬闸门**（要多问人、要拒这次调用），
       永远不能替宿主放行——所以"外来数据带上来的强制"在这个方向上是安全的。
    """
    if raw is None:
        return ToolGovernance()
    if not isinstance(raw, dict):
        raise ValueError(
            f"工具 {name!r} 的 governance 必须是 object，得到 {type(raw)!r}"
        )
    unknown = sorted(set(raw) - set(_GOVERNANCE_KEYS))
    if unknown:
        raise ValueError(
            f"工具 {name!r} 的 governance 含内核不认识的字段 {unknown}，"
            f"合法字段只有 {list(_GOVERNANCE_KEYS)}；"
            "静默忽略会让人以为脱敏/审计收口已生效，故拒绝加载"
        )

    sensitive: tuple[str, ...] = ()
    if raw.get("sensitive") is not None:
        items = raw["sensitive"]
        if not isinstance(items, (list, tuple)):
            raise ValueError(
                f"工具 {name!r} 的 governance.sensitive 必须是字符串数组"
            )
        paths: list[str] = []
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"工具 {name!r} 的 governance.sensitive 含空或非法路径: {item!r}"
                )
            paths.append(item.strip())
        sensitive = tuple(paths)

    audit_sensitive = raw.get("audit_sensitive", False)
    if not isinstance(audit_sensitive, bool):
        raise ValueError(
            f"工具 {name!r} 的 governance.audit_sensitive 必须是布尔值，"
            f"得到 {audit_sensitive!r}"
        )

    acc = raw.get("acc")
    if acc is not None and not isinstance(acc, dict):
        raise ValueError(
            f"工具 {name!r} 的 governance.acc 必须是 object 或 null，"
            f"得到 {type(acc)!r}"
        )

    return ToolGovernance(
        sensitive=sensitive,
        audit_sensitive=audit_sensitive,
        acc=acc,
        approval=_approval_from_manifest(name, raw.get("approval")),
        gate=None,
    )


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
        return cls(
            name=name,
            description=str(data.get("description", "")),
            input_schema=dict(data.get("input_schema") or {}),
            handler=None,
            source=source,
            steps=steps,
            code=code,
            governance=governance_from_manifest(name, data.get("governance")),
        )


@dataclass
class ChatMessage:
    role: Role
    content: str = ""
    # assistant 发起的工具调用：[{"id","name","arguments"}]
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_llm_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
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
