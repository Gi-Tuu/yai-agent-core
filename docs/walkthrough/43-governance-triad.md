# 逐行讲解 43 · 治理三件套：令牌鉴权、参数快照、声明式脱敏（＋原生参数级审批）

> 第 22/23 篇把员工做成了原生桌面端、一座岛管多个专员。本篇补的是**跨进程之后
> 才暴露出来的安全缺口**：当"审批在灵动岛侧、执行在宿主侧"变成两个进程，
> 服务端那道口若谁都能调，前面所有权限设计都会被绕过。
> 三件事各自独立，但共同回答一个问题：**审批看到的东西，凭什么等于真正执行的东西？**

## 0. 三个缺口是怎么被发现的

本轮通用能力与兼容性架构审查逐文件取证时发现：

| 缺口 | 证据 | 后果 |
|---|---|---|
| 跨进程工具口无鉴权 | `web_app.py:322` 直接执行，`tool_service.py:13` 自陈"决策在调用方" | 同机任意进程可绕过审批执行宿主写操作 |
| 审批与执行共用一个 dict | `authorize.py` 把 `arguments` 同时交给 `confirm()` 与后续执行 | 通道在确认期间改参数，改的就是实际执行值 |
| 事件原样携带参数 | `authorize.py` 的 `PERMISSION_ASKED`/`TOOL_CALL`、`otel.py:116` 的 span | 密钥作参数即进日志/追踪/界面 |

## 1. 令牌鉴权（`examples/host_e_sales_crm/tool_auth.py`）

解析优先级：**显式参数 → 环境变量 `YAI_TOOL_TOKEN` → 本地令牌文件**
（`data/host_e/tool_token`，首次启动 `secrets.token_urlsafe(32)` 生成并 `chmod 600`）。

```python
token_auth.token_matches(header, expected)   # 只认 Bearer，hmac.compare_digest 常量时间
token_auth.startup_guard(host, explicit)     # 非环回 + 无显式令牌 → 拒绝启动
```

三条刻意的设计：

1. **白名单清单端点也要令牌**：`GET /api/agent/tools` 返回的白名单本身就是攻击面信息；
2. **纯 CRM 自己的页面接口不受影响**：`/api/state` 等照旧无令牌——它是普通软件，
   不是 Agent 口，鉴权只加在"能驱动 Agent 能力"的边界上；
3. **边界写进 docstring**：能读到令牌文件的进程即被视为同一用户身份。这一层挡的是
   同机其他用户/服务与误接客户端，**不防同一用户的恶意本地进程**（那要 OS 级隔离）。

服务端与客户端各持一份令牌文件路径常量（跨进程不能共享代码），由
`tests/test_host_e_tool_auth.py::test_client_and_server_share_token_convention`
断言二者一致，防止哪天改一边。

## 2. 参数快照：三份副本，一份权威

`snapshot_arguments()` 深拷贝模型参数（模型侧本就是 JSON 值，拷贝代价低；
拷不动的极端情况**退回原对象**——快照是加固手段，不该变成新的崩溃源）。

关键在于**谁拿哪一份**：

```
模型给的 dict
   └─ executor 入口 snapshot → arguments（权威：策略判定 + 实际执行）
        ├─ redact_arguments(...) → shown   → PERMISSION_ASKED 事件 + channel.confirm()
        └─ redact_arguments(...) → audit   → TOOL_CALL 事件
```

于是：通道在 `confirm()` 里 `arguments.clear()` 也只清掉自己那份副本；宿主改审计事件
也改不动下一份。**"用户批准的是 A、执行的是 B"这条路径被结构上堵死**，
而不是靠"约定通道别乱改"。

`replan` 那条路径**故意不快照**：它不过授权闸（只重排内部计划、无外部副作用），
给它加快照会留下一句错误的注释——注释与语义不符比没有注释更糟。

## 3. 声明式脱敏：`ToolSpec.governance.sensitive`

```python
spec.governance = ToolGovernance(sensitive=("api_key", "headers.Authorization"))
# 事件与 confirm 里 → "[redacted:len=22]"；handler 收到的仍是真值
```

为什么按**声明**而不是按字段名猜：

- 猜会漏（改叫 `tok_v2` 就绕过），也会误伤（把业务字段抹掉，审批人反而看不懂）；
- 任务书与 ACC 规范同一条纪律：安全语义必须来自可信声明，不能来自启发式；
- 所以"未声明即不脱敏"是**有意的边界**，测试
  `test_undeclared_params_stay_visible_by_design` 把它钉成契约而不是留成惊喜。

占位符保留长度（`len=N`）：审批人需要判断"这是不是一把我认得的长令牌"，
全抹成一个 `***` 反而丢信息。

脱敏只发生在**对外副本**上，因此追踪 span、metrics 标签、事件流自动继承——
不需要在每个出口各写一遍。

### 3.1 组合工具：声明只覆盖"自己这一层"的副本

组合分支不经过 `authorize_tool_call`（权限精确落到每一步，不让用户看一个黑盒名字），
但 `TOOL_COMPOSED` 事件里的 arguments 副本**仍按组合工具自己的 `sensitive` 脱敏**——
声明写了就不能因为这条路走的是组合而失效；handler 收到的还是真值。

反过来说清边界：脱敏是**逐层声明**，不是自动污点追踪。密钥经占位符流进内部步骤后
参数名已经换了（`api_key` → `token`），那一步的副本按**那一步自己的**声明处理。
宿主想让那一步也收口，就给那一步也声明；内核不会替业务字段发明"这看起来像密钥"。
测试 `test_redaction_is_per_layer_not_taint_tracking` 把这条边界钉成契约而不是留成惊喜。

### 3.2 声明会跟着能力清单走

`governance` 的**数据部分**（`sensitive` / `audit_sensitive` / `acc` 快照 / `approval` 审批要求 /
`timeout_ms` 硬超时 / `rate_limit` 频控）随 manifest 一起导出、导入后继续生效；`gate` / `preview` /
`idempotency_key` 是函数，与 `handler` 同命——不导出、也不从清单重建，跨进程后由宿主本地重新绑定
（第 21 篇 §5 有完整字段表）。

## 4. 原生参数级审批：`governance.approval`

第 44 篇的 ACC 声明能写出"退款超过 1000 要人确认"。但宿主不接 ACC 时，以前只有一条路：
自己写一个 `gate` 函数。函数进不了清单、跨不了语言，而且每家都要把"超过"这个词重新实现一遍
——判定语义散在宿主代码里，写法不同结论就不同。现在这套语义住进内核
（`src/yai_core/policy/approval.py`，纯函数、零依赖），宿主只写**数据**：

```python
spec.governance = ToolGovernance(
    approval=ApprovalPolicy(
        prompt="退款 {amount} 元，确认吗？",
        when=(ApprovalRule("amount", ">", 1000, label="金额超限"),),
    )
)
```

判定顺序本身就是语义的一部分，换一步就换结论：

1. **入参先于条件**：带条件时先按绑定 input schema 校验调用值，不兼容直接**拒**，
   handler 不许跑——拿一个类型都不对的值去比大小，结论没有意义；
2. **条件路径必须已声明**：`param` 不在绑定 schema 里 → **拒**；压根没绑 schema 也 → **拒**。
   放过去的后果不是"松一点"，是拼错的路径在 `==` 下永远不命中、条件静默失效；
3. **`required` 占优**：为真时每次都要问，命中的条件不得削弱它；
4. **`when` 取 ANY**：命中一条就要问，全部不中才是"这层没意见"。

比较一律严格 JSON 值：`"1000" != 1000`、`True != 1`；数值比较遇到 NaN/Infinity、
`contains` 遇到标量、算子不认识 —— **判不出来就拒**，绝不"当没命中"放行。

三条与三件套同源的纪律：

- **只抬不放**。`allow` 只代表"这层不要求问人"，放行与否仍归 `PermissionPolicy`；
  策略 DENY 时连问都不问（`test_permission_deny_still_beats_the_approval_requirement`）。
- **判定只有一份**。ACC 层现在只做翻译（`to_approval_policy`），不再自带一套比较逻辑——
  两份"看起来一样"的判定迟早漂移，而漂移的那份没人测。
- **两个来源取更严**。同一个工具既挂 `gate`（函数）又写 `approval`（数据）时合并而不是覆盖，
  两边理由都进审批事件、只问一次（`test_gate_and_approval_are_merged_not_overwritten`）。
  "有 gate 就当 approval 不存在"等于声明了的保护内核没做。
- **组合工具不例外**。组合分支不过 `authorize_tool_call`（权限落到每一步），但组合工具
  **自己声明**的审批要求会真的问一次：只走 `Channel.confirm`、不替黑盒名字跑策略，
  人不批就一步都不跑（`test_composite_can_declare_its_own_approval`）。

还有一条踩出来的坑值得记：`from __future__ import annotations` 会让函数注解变成字符串，
而内省曾经把解不开的字符串一律当成 `string`。审批要按绑定 schema 校验入参，于是
"期望 string、实到 number" 把**每次合法调用**都判成非法。现在 `build_spec` 先走
`get_type_hints`，解不开才退回原兜底（第 3 篇，测试
`test_future_annotations_do_not_poison_the_bound_schema`）。

### 4.1 这道闸防的是谁，不防谁

限流那一层（第 14 篇）常被读成"保护模型额度"，得纠正：**它的威胁模型是防误刷**——手滑重试、
爬虫、失控循环。客户端身份取 `X-Forwarded-For` **最左一项**，这意味着任何伪造转发头的
调用方都能领一份新的 30 次/分钟。2026-10-16 对线上实例的实测正好把这两面都拍下来了：

| 实测 | 结果 | 说明什么 |
|---|---|---|
| 并发 40 次、同一伪造 XFF | 恰好 30×200 + 10×429，`Retry-After: 59/60`、`scope:"ip"` | 每 IP 配额与 `Retry-After` 形状成立 |
| 换**另一个**伪造 XFF 并发 34 | 30×200 + 4×429 | 身份**就是**那个可以随便写的头——这是设计，不是漏洞，但也**不是防线** |
| 并发 34 次**空 body** | 30×422 + 4×429 | 限流在参数校验与鉴权之前：被拒的请求一个 token 都不消耗 |
| 期间 GET `/health`、`/v1/tools` | 全 200 | 探活硬门槛不受限 |

要挡"蓄意刷免费额度"，得靠鉴权（`YAI_SERVER_TOKEN`，或 host_e 那套本地令牌），
不是这一层。文档与对外帖子都不许把这条闸写成额度保护。

## 5. 审批人到底看见了什么：上下文随 `confirm()` 一起递

第 4 节让内核算出了 `reason / prompt / risk / scope / target`，第 21 节把它们写进了事件。
但**事件到不了正在弹出的那张卡片**：`authorize_tool_call` 必须先把"批不批"拿到手才返回，
而事件是它返回之后才由 `AgentLoop` 统一 yield 的——卡片弹出来的那一刻，那条
`PERMISSION_ASKED` 还在半路上。所以这些字段得**随通道调用递出去**：

```python
class QtChannel:
    yai_approval_context_aware = True          # 通道自己声明愿意收

    async def confirm(self, tool_name, arguments, context=None):
        ...                                     # context 只有描述性字段
```

三条口径：

- **能力旗标，不是探签名**。没声明的通道仍只收到两个参数，宿主自写的 CLI / HTTP / Flutter
  通道一字不改照样跑；用 `getattr` 探参数形状会被宿主的 `**kwargs` 猜错，猜错那一次的代价
  是审批人少看一个字段——与策略侧的 `yai_subject_aware` 同一个约定。类型层面也拆成两层：
  基础 `Channel.confirm(name, arguments)` 是稳定下限，第三个参数属于可选的
  `ApprovalContextChannel`，不再用 `cast` 把两参数实现"伪装"成三参数契约。
- **上下文里没有未脱敏的参数值**。它只带 `tool` 与算出来的描述性字段；`arguments`
  那一份仍是按声明脱敏的对外副本。唯一的例外是宿主自己写的 `prompt` 模板：模板里
  本来就写了 `{amount}`，填进去就是宿主的选择，内核不替它扩散别的字段。
- **有值才带**。策略自己 ASK（没有任何审批意图）时，上下文就只有 `{"tool": ...}`；
  界面按"有什么显示什么"渲染，标题靠 `approval_required` 与 `risk` 分档：
  参数级审批 / 高影响写操作待确认 / 新能力授权请求。

对应测试：`tests/test_approval_context_channel.py`——含一条**时序探针**（`confirm` 被调用时
通道收到的事件数是 0）和一条老通道断言（只收到两个位置参数）。桌面端的落点在
`shell/desktop/channel.py`（把 `approval_required` 与描述性字段并进卡片负载）与
`qml/Panel.qml` 的 `pendingSummary`：先说为什么问，再说动哪个资源，最后才是参数串；
对象/数组参数用 `JSON.stringify` 渲染，不再显示 `[object Object]`。高风险但非参数级审批
的标题改为“高影响操作待确认”——宿主没有提供动作类型，不能把读操作写成写操作。
对应桌面测试 `tests/test_desktop_pending_summary.py` 用 QJSEngine 直接执行这些绑定。

## 5.5 被拒绝也要留痕：`PERMISSION_DENIED`

以前只有"要不要问人"会发事件（`PERMISSION_ASKED`），真正**拒绝**的那一步只在返回值里
说明原因，事件流看不见——宿主没法审计"这次调用为什么没发生"。现在五条拒绝路径各发一条
`PERMISSION_DENIED`：

| `source` | 触发点 | 典型原因 |
|---|---|---|
| `gate` | 工具自带的能力闸 | 作用域外 / 声明不满足 |
| `rate_limit` | 频率上限闸 | 窗口内次数用尽 |
| `policy` | 权限策略 | 不在白名单 / 显式 DENY |
| `approval` | 人工审批 | 审批人点了拒绝 |
| `egress` | 外发 URL 来源闸 | URL 只在不可信工具输出出现、任务原文没有 |

事件带 `tool` / `source` / `reason` 与**按声明脱敏后**的 `arguments`——拒绝事件同样会进
宿主日志，不能因为"这次没执行"就漏掉脱敏。它也让注入回归集能把"拒绝可见"当成断言。

## 5.6 外发 URL 来源闸：只信任务原文

模型可以在正文里写“这是注入、我忽略了”，但这句话不是安全边界。对**明示会对外发送文本**
的工具，内核在策略闸里做一次确定性比对：

1. 工具用 `ToolGovernance.egress_text=("body",)` 声明哪个参数是要外发的文本；
2. 当前任务原文里的 URL 是可信用；工具输出里的 URL 是不可信用；
3. 待发送文本里出现一个**只在不可信工具输出出现、任务原文没有**的 URL，直接拒绝
   （`PERMISSION_DENIED`，`source="egress"`），业务 handler 不执行。

这解决的是 slack 那一类案例的形状：工具输出里藏着 `www.secure-systems-252.com`，
模型把它原样写进给 Alice 的私信；旧行为只看“模型有没有加警示”，现在直接拦发送。
对应测试 `tests/test_egress_guard.py` 断言：发送函数零调用、模型收到拒绝原因、
事件流里有 `permission_denied`；另有一条正例覆盖“URL 本来就在任务原文里”不误拦。

边界（有意保留）：

- 只对声明了 `egress_text` 的工具生效；不按参数名猜“这个字段像不像外发文本”；
- 只认 `http(s)://` 与 `www.` 开头的 URL，不解析完整 URL 语法；
- 只比较任务原文与工具输出，不做跨任务/跨进程污点追踪；
- 命中即拒绝，不把决定权交回模型；要放宽只能由宿主改声明或走人工审批设计。

## 6. 自检

1. 为什么权限决策在调用方（灵动岛）完成时，服务端反而**必须**鉴权？
2. 三份参数副本各自的作用是什么？少了"审批展示副本"会出什么问题？
3. 为什么 `replan` 不做快照？
4. 为什么内核不按字段名关键词自动脱敏？这条边界的代价是什么？
5. 组合工具的 `sensitive` 声明管到哪一层为止？为什么不做污点追踪？
6. 令牌文件方案挡住了谁、挡不住谁？要挡后者该换什么机制？
7. 参数级审批为什么做成**数据**（`approval`）而不是只留一个 `gate` 函数？
8. 同一个工具既挂了 `gate` 又写了 `approval`，为什么合并要取更严的那个，而不是 gate 优先？
9. "条件路径没在绑定 schema 里声明"为什么判**拒**而不是判"不命中"？
10. 界面为什么不能从 `PERMISSION_ASKED` 事件里读落点来渲染审批卡？
11. 审批上下文为什么用能力旗标决定传不传，而不是探测通道签名？
