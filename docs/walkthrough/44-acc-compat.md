# 逐行讲解 44 · ACC v1 兼容层：零依赖声明校验 + 严格 JSON 审批原语

> 这一篇讲的是 `src/yai_core/integrations/acc/`。**先说清楚它不是什么**：
> 它不把 YAI 变成"ACC 应用"，不引入任何第三方依赖，不接管事件流，也不在运行时强制执行什么。
> 它是**可选兼容层**：外部生态用 ACC（Agent Capability Contract v1，一个能力声明契约规范）描述一个
> 操作的风险、审批意图与审计敏感性，YAI 负责把那份声明**读懂、判准**，再决定要不要接进执行器。
> 规范原文与语料都是 Apache-2.0，出处与逐字节锁定的哈希见 `tests/fixtures/acc/SOURCE.md`。

## 0. 为什么要接一个外部规范

YAI 自己的工具治理是 `ToolSpec` + `PermissionPolicy` + `Channel.confirm`：**宿主声明能力，内核决定怎么用**。
问题是宿主之外的世界也在给自己的 API 贴治理标签。如果一个 CRM 厂商已经在 OpenAPI 描述里写了
"这个操作要人工确认、那个字段是敏感的"，YAI 要么视而不见（自己重贴一遍标签），要么读得懂它。
后者才谈得上生态，所以有了这个包。三条硬约束来自项目红线：

| 约束 | 落地方式 |
|---|---|
| 内核零第三方硬依赖 | 手写 schema 子集校验器，不装 `jsonschema`；整个包只用 `re` / `dataclasses` / `math` |
| ACC 是可选兼容层 | 只有宿主主动去解析声明时才会 import 这个包；`ToolSpec` 内核字段不因它改变语义 |
| 不做业务授权 | ACC 只决定**能力可达（reach）**；"这个主体到底能不能做这件事"永远归宿主业务层 |

## 1. 两层分工：校验归校验，决策归决策

```
一份声明 dict
   └─ declaration.parse_declaration()          【校验层：只回答"合不合法"】
        ├─ valid=False → diagnostic + errors    （绝不抛异常，绝不"尽力解释"）
        └─ AccDeclaration（强类型、可选字段留 None）
              └─ evaluate.is_exposed / effective_risk / audit_sensitive / approval_outcome
                 【决策层：纯函数，给结论 + 人类可读 reason，不做 IO】
```

分开是有原因的：**校验失败与决策拒绝是两种性质不同的事件**。声明格式错 = 这条能力根本不该被暴露；
调用值与 schema 不兼容 = 这次调用要挡下但声明本身没问题。混在一起就会做出"解析失败就当低风险放行"这种事。

## 2. 校验层：三条判定规则

`ACC_MAJOR_VERSION = 1`，必填三件套 `version` / `enabled` / `scope`（`scope` 还带正则与 1..160 长度）。

**诊断只有两类，而且分得很细**：

```python
parse_declaration({"version": 2, "enabled": True, "scope": "order.read"}).diagnostic
# → "unsupported_version"        整数主版本不认识
parse_declaration({"enabled": True, "scope": "order.read"}).diagnostic
# → "invalid_declaration"        缺 version 不算"版本不支持"
```

这条区分是官方向量 `T-PARSE-05` 专门钉的：缺字段是**写错了**，`version: 2` 才是**没见过**。
把两者混成一个诊断，上游规范将来加 v2 时就没法从日志里分清"该升级"还是该修 spec。

第二条规则：**未知字段一律忽略**（`additionalProperties: true`），根层未知键进
`AccDeclaration.extensions` 扩展袋。规范明确要求扩展字段**不得隐式改变** allowlist、风险、审批、
限流、审计行为，所以扩展袋只是"原样留着"，没有任何一条决策代码读它。

第三条：`timeout_ms`（1..600000）、`rate_limit.window`（`^[1-9][0-9]*(s|m|h|d)$`）、
`risk.level` 枚举、`approval.when[].op` 九个算子、各字段的长度上限，逐条对齐 schema。
JSON 整数的坑也处理了：`version: 1.0` 合法（零小数部分就是整数），`version: true` 不合法（bool 不是整数）。

## 3. 决策层之一：暴露与有效风险

```python
is_exposed(decl, scope_allowed=..., trusted_subject=..., has_input_schema=True)
```

四个条件全真才暴露：`enabled`、非空 `scope`、路由/策略允许该 scope、声明 `subject.required` 时确有可信主体。
`has_input_schema` 是**绑定层加的那一格**：规范说一个声明过 ACC 的 operation 只有在"绑定能提供足够入参
schema"时才可暴露，没有 schema 就不能把参数级审批当回事。

`trusted_subject` 是布尔输入而不是身份对象——这是有意的。**主体可信与否必须由宿主在自己的信任边界上判定**，
从模型输出或工具参数里抄来的"我是管理员"永远不算。宿主侧的主体注入是下一步（任务 I），
在那之前这一格只能由调用方显式喂布尔值，`effective_risk` 同理不猜身份。

```python
effective_risk(decl, http_method="POST")   # 显式 risk.level > execution.readonly > 绑定 GET 信号 > medium
```

默认值顺序按规范"安全默认"来：没写风险时，声明只读 → `low`，其余 → `medium`，
**永远不会默认成 low**。GET 是 OpenAPI 绑定给的原生只读信号，只在风险与 `readonly` 都没声明时才用。

## 4. 决策层之二：`approval` 的严格 JSON 值模型

这是整个包里最容易写错的地方，所以官方的 14 条审批向量全都跑在这一段上。

```
approval_outcome(decl, args, parameter_schema)
  1) 有条件时先按绑定 input schema 校验调用值   不兼容 → reject_invalid_arguments
  2) 每条 approval.when 的 param 必须在 schema 里声明   没有 → reject_invalid_arguments
  3) approval.required 为真          → approval_required（条件永远不能削弱它）
  4) 逐条比较，ANY 语义              → 命中即 approval_required，没命中才 allow
```

比较一律走 `same_json_value()`，**不做任何隐式转换**：`1 != "1"`、`True != "true"`、
`0 != False`；数组看元素顺序，对象按键集合（`{"a":1,"b":2}` 与 `{"b":2,"a":1}` 相等）。
九个算子里最容易被"顺手宽待"的是 `in` 与 `contains`：`in` 的 `value` 必须是数组，
`contains` 只对 string（子串）与 array（严格相等元素）成立，`exists` 对"路径缺失"和"显式 null"
都算不匹配——这两点在向量 `T-APPROVAL-08/11/13/14` 里各钉了一次。

**判不出来就失败关闭**。数值比较遇到非有限数、`in` 遇到标量 value、`contains` 遇到数字参数、
条件路径没在 schema 里声明——规范说"类型不兼容必须在到达业务操作之前拒绝，
不得静默当作条件不匹配"，所以这些路径全部返回 `reject_invalid_arguments` 并带上 reason，
而不是"匹配失败 → allow"。方向只有一个：**宁可多问一次人工确认，不静默放行**。

唯一的例外是 `!=`：路径缺失时 `!=` 判为命中（多要一次确认），这与参考 oracle 一致。

## 5. 官方向量怎么跑

`tests/fixtures/acc/vectors.json` 是上游一致性语料的**逐字节副本**，测试里用 SHA-256 锁死，
改动会先红在 `test_corpus_bytes_are_the_vendored_revision`，这样"改的是实现还是语料"分得开。

**这份语料不随包分发**：它是别人的 Apache-2.0 测试数据集，我们用它证明兼容层跑得对，
但没有对外分发副本的必要——`pyproject.toml` 的 sdist `exclude` 里排掉了
`tests/fixtures/acc`，缺文件时本模块整模块 `skip`（从 sdist 跑测试的人拿到的是
"这条证据不在这里"，不是报错）。**源码仓库里跑仍然是全量 26 条**，CI 就在源码仓库里跑。
出处、commit、许可证与哈希见同目录 `SOURCE.md`。

| profile | kind | 条数 |
|---|---|---|
| parser | declaration | 5 |
| runtime | approval | 14 |
| runtime | exposure | 4 |
| runtime | risk-default | 2 |
| runtime | audit-hint | 1 |
| | **合计** | **26** |

`_observe()` 按 `kind` 把向量喂进上面四个公开函数，产出与 `expected` **键集完全相同**的结果
（`{"valid": True}` 不能多带 `diagnostic`），26 条全绿。测试还钉了语料自身的元数据
（format_version / acc_version / specification_release / id 唯一 / kind 构成），
并对"上游若新增一种 kind"设了栅栏：`test_every_vector_kind_has_an_observation_branch` 会先失败，
不允许沉默通过。

## 6. OpenAPI 绑定：`x-agent-capability` 怎么落进工具注册（`openapi.py`）

规范只定义了声明长什么样，"挂在哪、怎么取"由绑定文档定。这里落四条：

```python
bind_operation(operation, method=..., tool_name=..., input_schema=...,
               query_params=..., body_fields=...,
               scope_policy=config.acc_scope_allowed, trusted_subject=config.trusted_subject)
```

| 情形 | 结果 |
|---|---|
| operation 上没有扩展 | `declared=False`，一切照旧（**未采用 ACC 的宿主零行为变化**） |
| 扩展不是 object / 声明校验失败 / 主版本不认识 | **整条 operation 跳过** + 诊断，绝不回退成"没有治理的同名工具" |
| `enabled=false`、需要可信主体但没有、scope 不在宿主路由白名单里 | 跳过 + 诊断（诊断里写清是哪一条） |
| `approval.when` 的 `param` 在归一化输入 schema 里解析不出来 | 跳过（"输入不足以安全判定"） |
| `param` 撞上"同名参数同时来自 query 与 body" | 跳过。绑定允许"拒绝该条件"，但被拒的条件等于把声明里的审批承诺削薄，而削的方向是放行更多 |
| 校验通过且可暴露 | `ToolSpec.governance` 带上 `audit_sensitive` 与 `acc` 原样快照，`guidance.when_to_use / returns` 并进工具描述 |

两个配置口在 `OpenApiSpecConfig` 上：`acc_scopes`（空 = 不因 scope 拦；尾部 `.*` 走前缀匹配）
和 `trusted_subject`（**默认 False**：主体必须由宿主在自己的信任边界上给，
模型或参数里抄来的身份不算，所以带 `subject.required` 的操作在接上主体注入之前一律不暴露）。

`ToolGovernance` 因此多了两个字段：

- `audit_sensitive`：宽口径审计提示。落地方式有讲究——规范的义务范围是 **logs**，
  所以只把**审计事件**那份的顶层值整体收口，**给审批人的副本仍按声明路径脱敏**；
  全糊掉会让人工批准失去判断依据，这个不对称是刻意的，两条都有测试钉住。
- `acc`：已校验声明的原样快照。内核不解释它（`types.py` 不 import 集成层），
  解释在 `integrations/acc`，留给审批接线用。

最后一条铁律写在 `misplaced_declarations()`：扩展只认 operation 级。作者把它贴到根节点或
pathItem 上时，绑定**不读它**（不能让它产生任何治理效果），但会把"贴错位置"作为诊断报出来。

## 7. 从声明到每一次调用：能力闸与可信主体

前面都是"读得懂"。这一节是"用得上"，而且用法只有一个方向：**外部声明只能把闸门往上抬**。

```
模型给出参数
  └─ executor：snapshot → governance.gate(arguments, subject)   ← 内核不认识 ACC
        ├─ reject 非空 → 直接失败：不过策略、不发 TOOL_CALL、不碰 handler
        ├─ require_approval → 授权闸把策略的 ALLOW 抬成人工确认（策略 DENY 仍然优先）
        └─ 沉默 → 照旧
```

**为什么是"一个 callable"而不是"内核 import ACC"**：`ToolGovernance.gate` 的类型是
`(arguments, subject) -> GateDecision`，判定由宿主或集成层提供，内核只按 `GateDecision`
的三种话行动。这样 `integrations/acc` 保持可选（执行器热路径不依赖它），自带策略引擎的宿主
也能挂同一个口子。`governance.acc` 那份声明快照只做透明记录，**它本身不产生任何执行效果**——
要让声明参与准入就得附上 gate（OpenAPI 桥用 `build_gate(declaration, input_schema)` 造一个）。
判定发生在每次调用现场，用当时的参数与当时的主体。

**主体（`Subject`）**：宿主经 `core.run(task, subject=...)` / `astream(...)` 注入，
沿 loop → 执行器 → 授权闸一路透传，不落在任何实例属性上，所以并发跑两条会话不会互相粘连。
两个消费点：

- 闸：声明 `subject.required` 而这次运行没有主体 → 直接拒，**且不沿用上一位的身份**；
- 策略：只有声明了 `yai_subject_aware = True` 的策略才会收到 `subject=` 关键字——
  用能力旗标而不是鸭子探测签名，猜探在宿主写了 `**kwargs` 时会猜错，宁可不传。

拒绝理由的可见性是刻意分开的：闸拒绝**不发事件**（与权限 DENY 同形，免得看起来"调用过"），
但理由会作为 `role="tool"` 消息回灌给模型，由模型决定换路还是收口——审计链安静，模型不瞎。

## 8. 边界与限制（照实说）

1. **入参校验只实现 `type` / `required` / `properties` / `items` 四个关键字**，`enum`、`pattern`、
   `min/max` 被忽略。向量依赖的四个都在子集内；向量之外这意味着"ACC 层的入参门槛比完整 JSON Schema 松"，
   更强的参数校验仍归宿主。
2. **一处刻意偏离参考 oracle**：`GET` + 显式 `execution.readonly: false` 我们取 `medium`，
   oracle 给 `low`。绑定文本说"显式声明优先"，取更保守的那一档；向量没有覆盖这个组合。
3. `timeout_ms` / `rate_limit` **只解析、不执行**，还没映射到执行器的超时与限流。
4. **闸只覆盖审批意图与主体准入**：`approval.required` / 命中的 `approval.when` /
   `subject.required` 已经接进每一次调用；风险等级 (`effective_risk`) 与 `scope` 是
   **描述性**字段——它们会随 `GateDecision` 带出、并落到 `PERMISSION_ASKED` 与
   `TOOL_CALL` 事件载荷里（审批人看得见"这是高影响写"，事后能按风险检索），
   但内核**没有**按风险自动升降档（那是宿主的权限模型）。
5. **主体是"注入 + 透传 + 准入"，不是"认证"**：内核不验签、不发 token、不做跨跳主体连续性，
   `Subject` 的可信性完全来自宿主在自己的信任边界上给不给。规范 §4.5 也明令这一点。
6. **清单里 `acc` 快照跟着走、`gate` 永远不跟着走**：`to_manifest_dict` 会导出 `governance`
   的数据部分（含 `acc` 快照），但函数 `gate` 与 handler 同命——不导出，也绝不凭快照重建。
   跨进程拿到清单的宿主必须**本地重新绑定**才有准入（第 21 篇第 5 节给了代码），
   否则那份声明只是一条记录。方向是有意的：外来 JSON 既不能给本机工具加闸门，
   也没有通道替本机把已绑定的闸门摘掉。

因此对外口径只有一句：**Implements the ACC v1 OpenAPI Binding Parser Profile**
（读扩展、校验、按绑定决定暴露与否、出诊断）。Runtime Profile 还缺三块证据：超时/限流映射、
按主体的资源级授权落点、以及"模型文本改不动治理"的对抗性集成证据；补齐才敢这么声称。
"ACC certified" 这种说法规范明令禁止，我们也不写。

## 9. 自检（读完本篇应回答）

1. 为什么"缺 version"和"version=2"必须给不同的诊断？
2. ACC 声明里的 `subject.required` 靠谁判定？为什么不能从工具参数里拿？
3. `approval.when` 判不出来时，allow 和 reject 哪个才是对的？为什么？
4. 契约的 `require_approval` 能把策略的 DENY 抬成"要人工确认"吗？方向为什么是单向的？
5. 为什么内核吃 `governance.gate` 这个 callable，而不是直接 import ACC 判定？
