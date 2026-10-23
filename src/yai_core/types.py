"""YAI Agent Core 的核心数据类型。

刻意只依赖标准库：内核本体零第三方依赖，第三方能力全部通过 SPI 注入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from yai_core.paths import schema_at_path

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


ReceiptStatus = Literal["not_started", "succeeded", "failed", "unknown", "partial"]


@dataclass(frozen=True)
class ExecutionReceipt:
    """一次工具执行的机器可读收据，用于判断"能不能重试"。

    - ``not_started``：参数/权限/频率/预览等在 handler 之前失败，没有副作用；
    - ``succeeded``：handler 正常返回；
    - ``failed``：执行器明确知道失败（例如沙箱返回 ``ok=False``）；
    - ``unknown``：超时、同步 handler 被放弃、handler 抛错或沙箱调用异常——
      副作用可能已经发生，**不能盲目重试**；
    - ``partial``：组合工具前几步成功、后续步骤失败。

    ``idempotency_key`` 只来自宿主声明的生成器；内核的进程内缓存只在这个 key
    存在时生效，跨进程/多实例去重仍由宿主负责。
    """

    call_id: str
    tool: str
    status: ReceiptStatus
    retryable: bool = False
    idempotency_key: str = ""
    error: str = ""
    duration_ms: float = 0.0
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.status not in (
            "not_started",
            "succeeded",
            "failed",
            "unknown",
            "partial",
        ):
            raise ValueError(f"未知执行状态：{self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "status": self.status,
            "retryable": self.retryable,
            "idempotency_key": self.idempotency_key,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "replayed": self.replayed,
        }


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
class RateLimitHint:
    """单个工具的调用频率上限（进程内执行器强制）。

    - ``count``：窗口内最多调用次数；
    - ``window``：窗口长度（``30s`` / ``1m`` / ``2h`` / ``1d``）。

    这是"外部契约声明的频率上限真正变成约束"的那一层：ACC ``execution.rate_limit``
    声明的频率，在这里变成执行器里的一条真实闸门——超限的调用**不会到达 handler**。
    它是**进程内**的（多实例部署要跨实例总闸，那需要状态共享，不在这一层）。
    """

    count: int
    window: str

    def window_seconds(self) -> int | None:
        """把窗口串解析成秒；格式不认识时返回 None（= 这条提示无法执行）。"""
        return parse_window_seconds(self.window)

    def to_reference(self) -> dict[str, Any]:
        return {"count": self.count, "window": self.window}

    @classmethod
    def from_reference(cls, data: Any) -> RateLimitHint:
        if not isinstance(data, dict):
            raise ValueError(f"rate_limit 必须是 object，得到 {type(data)!r}")
        count = data.get("count")
        window = data.get("window")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError(f"rate_limit.count 必须是正整数，得到 {count!r}")
        if not isinstance(window, str) or not window.strip():
            raise ValueError(f"rate_limit.window 必须是非空字符串，得到 {window!r}")
        return cls(count=count, window=window.strip())


def parse_window_seconds(window: str) -> int | None:
    """解析 ``30s`` / ``1m`` / ``2h`` / ``1d`` 这类窗口串；不认识返回 None。"""
    text = (window or "").strip().lower()
    if not text:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    unit = text[-1]
    if unit not in units:
        return None
    try:
        value = int(text[:-1])
    except ValueError:
        return None
    if value <= 0:
        return None
    return value * units[unit]


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
    # 执行策略提示（同样放最后，别动既有位置写法的绑定关系）：
    # - timeout_ms：这个工具的调用硬超时（与全局 handler_timeout 取更严的一档）；
    # - rate_limit：这个工具的调用频率上限（进程内滑动窗口，超限不执行）。
    # 两者都是**数据**，随能力清单跨进程走；执行器读它们并真的强制。
    timeout_ms: int | None = None
    rate_limit: RateLimitHint | None = None
    #: 宿主/集成层的**变更预览生成器** ``(arguments, subject) -> ChangePreview``。
    #: 它是函数，和 ``gate`` 一样不随清单导出；只有审批真正发生时才会展示，
    #: 审批通过后执行前会再生成一次并比较 token。模型参数无法设置它。
    preview: Callable[[dict[str, Any], Any], Any] | None = None
    #: 宿主提供的幂等键生成器 ``(arguments, subject) -> str``。返回空串表示
    #: 本工具不做幂等去重；非空时执行器会在进程内缓存结果，重试同一 key 不再
    #: 进入 handler。跨进程/多实例去重必须由宿主用同一个 key 自己实现。
    idempotency_key: Callable[[dict[str, Any], Any], Any] | None = None


#: 清单里可携带的治理字段白名单；不在表内的键**拒绝加载**（见
#: :func:`governance_from_manifest` 的理由说明）。
_GOVERNANCE_KEYS = (
    "sensitive",
    "audit_sensitive",
    "acc",
    "approval",
    "timeout_ms",
    "rate_limit",
)

_APPROVAL_KEYS = ("required", "prompt", "when")
_APPROVAL_RULE_KEYS = ("param", "op", "value", "label")


def _check_manifest_param_path(
    name: str, param: str, input_schema: dict[str, Any] | None, index: int
) -> None:
    """交叉校验审批条件的参数路径：清单声明的路径必须在同一份 ``input_schema`` 里存在。

    为什么要在**导入期**就拒：路径拼错（如 ``totl`` 其实是 ``amount``）在运行期表现为
    "每一次调用都被判参数不合法"，工具 100% 不可用——而错误发生在**别的进程**里，
    排查成本极高（本地看起来"工具坏了"，远端日志里只是一行拒绝理由）。导入期拒掉，
    错在数据进入的那一刻就被看见。

    只在 schema **带 properties** 时才校验：没有可校验的 schema（例如 native 工具
    还没绑定后端）不代表路径写错了，交给运行期失败关闭即可，不在导入期误杀。
    """
    if not isinstance(input_schema, dict):
        return
    properties = input_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return
    if schema_at_path(input_schema, param) is None:
        raise ValueError(
            f"工具 {name!r} 的 approval.when 第 {index} 条参数路径未在 input_schema 中声明："
            f"{param!r}；已声明的参数：{', '.join(sorted(properties))}。"
            "路径写错会让该工具在每次调用时都被判参数不合法，故拒绝加载"
        )


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


def _approval_from_manifest(
    name: str, raw: Any, input_schema: dict[str, Any] | None = None
) -> ApprovalPolicy | None:
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
        _check_manifest_param_path(name, param.strip(), input_schema, index + 1)
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
    if governance.timeout_ms is not None:
        out["timeout_ms"] = int(governance.timeout_ms)
    if governance.rate_limit is not None:
        out["rate_limit"] = governance.rate_limit.to_reference()
    return out


def governance_from_manifest(
    name: str, raw: Any, input_schema: dict[str, Any] | None = None
) -> ToolGovernance:
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

    timeout_ms = raw.get("timeout_ms")
    if timeout_ms is not None:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
            raise ValueError(
                f"工具 {name!r} 的 governance.timeout_ms 必须是正整数，"
                f"得到 {timeout_ms!r}"
            )

    rate_limit = raw.get("rate_limit")
    if rate_limit is not None:
        try:
            rate_limit = RateLimitHint.from_reference(rate_limit)
        except ValueError as exc:
            raise ValueError(f"工具 {name!r} 的 governance.rate_limit 非法：{exc}") from exc

    return ToolGovernance(
        sensitive=sensitive,
        audit_sensitive=audit_sensitive,
        acc=acc,
        approval=_approval_from_manifest(name, raw.get("approval"), input_schema),
        gate=None,
        timeout_ms=timeout_ms,
        rate_limit=rate_limit,
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
