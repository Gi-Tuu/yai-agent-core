"""工具治理契约：GateDecision / ApprovalPolicy / ToolGovernance 与清单编解码。

从 ``types.py`` 拆出；``types`` 继续 re-export 这些名字，旧导入路径保持可用。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from yai_core.paths import schema_at_path


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
    #: 后置条件检查 ``(arguments) -> bool | Awaitable[bool]``。只在该次调用结果为
    #: ``unknown``（超时 / 同步 handler 被放弃 / handler 抛错 / 沙箱异常）时运行：
    #: 确认副作用已发生 → 收据改判 ``succeeded`` 并标 ``verified``；确认没发生或检查
    #: 自身出错 → 保持 ``unknown``，只记录 ``postcondition`` 的结论。函数，不随清单导出。
    postcondition: Callable[[dict[str, Any]], Any] | None = None
    #: 即将对外发送的**文本参数路径**（dot-notation，如 ``body`` / ``message.text``）。
    #: 声明后内核在策略闸里做确定性比对：这些字段里的 URL 若只出现在不可信工具输出、
    #: 用户任务原文里没有，就拒绝本次外发（``PERMISSION_DENIED``，source=egress）。
    #: 声明路径在导入/注册时按 schema 校验；直接执行必须由宿主建立 egress_scope。
    #: 这是数据，随能力清单走；不声明 = 不做这项保护（不按字段名猜）。
    egress_text: tuple[str, ...] = ()


#: 清单里可携带的治理字段白名单；不在表内的键**拒绝加载**（见
#: :func:`governance_from_manifest` 的理由说明）。
_GOVERNANCE_KEYS = (
    "sensitive",
    "audit_sensitive",
    "acc",
    "approval",
    "timeout_ms",
    "rate_limit",
    "egress_text",
)

_APPROVAL_KEYS = ("required", "prompt", "when")
_APPROVAL_RULE_KEYS = ("param", "op", "value", "label")


def validate_egress_paths(
    name: str, paths: Any, input_schema: dict[str, Any] | None
) -> None:
    if not isinstance(paths, (list, tuple)):
        raise ValueError(f"工具 {name!r} 的 governance.egress_text 必须是字符串数组")
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or any(not part or part != part.strip() for part in path.split("."))
        ):
            raise ValueError(f"工具 {name!r} 的 governance.egress_text 含非法路径: {path!r}")
        target = schema_at_path(input_schema, path)
        if target is None:
            raise ValueError(
                f"工具 {name!r} 的 egress_text 路径未在 input_schema 中声明: {path!r}"
            )
        if target.get("type") in ("integer", "number", "boolean", "null"):
            raise ValueError(f"工具 {name!r} 的 egress_text 路径不包含文本: {path!r}")


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
    if governance.egress_text:
        out["egress_text"] = list(governance.egress_text)
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

    egress_text: tuple[str, ...] = ()
    if raw.get("egress_text") is not None:
        items = raw["egress_text"]
        if not isinstance(items, (list, tuple)):
            raise ValueError(
                f"工具 {name!r} 的 governance.egress_text 必须是字符串数组"
            )
        paths = []
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"工具 {name!r} 的 governance.egress_text 含空或非法路径: {item!r}"
                )
            paths.append(item.strip())
        egress_text = tuple(paths)
    validate_egress_paths(name, egress_text, input_schema)

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
        egress_text=egress_text,
    )
