# 逐行讲解 31 · 动态重规划 re-plan：计划走不通时，显式回到规划

> 对应文件：
> - meta-tool 定义：`src/yai_core/tools/meta.py`（常量 `REPLAN` / `build_replan_tool`）；
> - 按需注册与执行：`src/yai_core/kernel/loop.py`
>   （`_ensure_replan_tool` / `_handle_replan` / `_with_meta_schemas`，以及 astream 的 PLAN 分支）；
> - 事件类型：`src/yai_core/types.py`（`EventType.PLAN_REVISED`）；
> - 离线测试：`tests/test_replan.py`。
>
> 读前先读第 6（loop）、16（composer 组合工具）、25（上下文压缩）。本章解决
> PLAN 策略"开头规划一次、之后被计划锁死"的问题。

## A. 为什么需要显式 re-plan

PLAN 策略的流程是：先让模型把任务拆成编号步骤（`_make_plan`），再带着这份计划
进入工具循环。问题在于——**计划是在还没拿到任何工具结果时做的**，真实执行里
很容易出现：

- 第 2 步依赖的前置数据，第 1 步查出来发现根本不存在；
- 步骤顺序要调整（得先确认 A 才能去做 B）；
- 某一步的前提在执行中才知道不成立。

REACT 策略对这种情况天然免疫：它每一轮都重新让模型决定下一步，"边做边看"。
但 PLAN 一旦在开头写好步骤，模型倾向于照着原计划往下走。此前 loop 只在工具
**失败抛错**时追加一段轻量反思（`_append_failure_reflection`），它能让模型
"换个工具再试"，却没有一个明确的动作表达"**整份计划需要回到规划、产出修正版**"。

replan 就是补上这个动作：把"重规划"做成一个模型可以主动调用的 meta-tool。

## B. 把重规划做成 meta-tool：`build_replan_tool`

和 `request_capability`（缺能力）、`create_code_tool`（造纯计算工具）一样，
replan 也是一个**内核自带、由模型在执行中调用**的 meta-tool。先看它的参数结构：

```python
REPLAN = "replan"

def build_replan_tool() -> ToolSpec:
    schema = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "一句话说明原计划为什么要改，例如'第2步依赖的前置数据不存在'。"
                ),
            },
        },
        "required": [],          # reason 可带可不带，缺省用默认理由
    }
    return ToolSpec(
        name=REPLAN,
        description=(
            "仅当你正在执行一个【多步计划】、且因执行中拿到的新结果发现原计划走不通"
            "（漏了前置步骤、步骤顺序要调整、某步的前提不成立）时调用；内核会保留已"
            "完成的步骤、基于当前观察产出修正后的计划再继续执行。能用其他现有工具直接"
            "完成、或本来就是单步任务的，不要调用（直接改用工具即可）。"
        ),
        input_schema=schema,
        handler=_unreachable,    # meta-tool 没有普通 handler，由 loop 拦截分发
        source="native",
    )
```

逐块说明：

- **只有一个可选参数 `reason`**：模型用一句话说明为什么要改计划。`required`
  为空，模型不带理由也能调，内核会用默认理由"执行中发现原计划需要调整"。
- **描述里写清了"什么时候该调、什么时候别调"**：
  - 该调：正在执行多步计划、且新结果证明原计划走不通；
  - 别调：能用现有工具直接完成的小调整（那属于 REACT 自由发挥）、或单步任务。
  这层约束很重要，否则模型可能动不动就重规划，把简单任务复杂化。
- **`handler=_unreachable`**：meta-tool 不通过普通 `ToolExecutor` 执行函数，
  而是在 `_execute_tool_calls` 里被识别、交给专门的 `_handle_replan`。
  `_unreachable` 是个"理论上不该被调到"的占位 handler，万一漏拦截会明确报错，
  而不是静默成功。

### 为什么 replan 不走用户授权闸

普通工具调用前要过 `authorize_tool_call`（全部审批 / 部分审批 / 无需审批三档）。
replan **刻意不走授权闸**，原因写在 docstring 里：

> 它只重排内部计划、不产生外部副作用。

重规划不会直接执行删改数据等业务动作，但会调用模型，可能产生请求和费用；
该调用仍经过模型预算与超时边界。如果连"调整一下内部计划"都要用户点一次批准，
执行一份多步计划时就会被频繁打断。计划里的业务工具仍逐个过授权闸，不因此获准。

## C. 关键取舍：只在 PLAN 时按需注册，而不是总是注册

replan 最初被设计成"内核基础能力、构造 core 时就注册"。一跑测试就暴露了问题：

- 空宿主（一个业务工具都没有）本来应判 DIRECT（"宿主无工具，直接回答"），
  但因为注册了 replan，`len(registry)` 变成 1，"无工具"判定和"能力缺口"事件
  都被带偏；
- 多处工具计数、目录快照测试因为 registry 里凭空多了一个工具而 +1。

根因是：**replan 只在多步计划执行中才有意义**，空宿主、DIRECT、简单 REACT
都用不到它。于是改成"进入 PLAN 分支时幂等注册"。astream 里：

```python
if strategy == Strategy.PLAN:
    # replan meta-tool 只在多步计划执行中需要，进入 PLAN 时幂等注册，
    # 让模型在计划走不通时显式触发重规划（空宿主 / direct / 简单 react 不注册）。
    self._ensure_replan_tool()
    async for ev in self._make_plan(...):
        yield ev
```

注册方法是幂等的——已经注册过就什么都不做：

```python
def _ensure_replan_tool(self) -> None:
    """PLAN 策略下幂等注册 replan meta-tool（仅多步计划执行需要）。"""
    if REPLAN not in self.meta_tools:
        self.registry.register(build_replan_tool())
        self.meta_tools.add(REPLAN)
```

- `self.meta_tools` 是"本次运行已注册的 meta-tool 名字"集合（和 request_capability、
  create_code_tool 共用）。用它判断而不是 `registry.has()`，语义更清楚。
- 第一次跑 PLAN 任务时注册，之后 registry 里一直有它、且重复调用是无害的。
- 这样默认 core 保持精简：空宿主真的空，简单任务的工具目录不被 meta 污染。

> REACT 为什么不需要注册 replan？REACT 每轮都重新 achat、自由选择工具顺序，
> 计划走不通时它有 failure reflection（换工具）、request_capability（缺能力）、
> 直接说明缺口等多条闭环。replan 针对的恰恰是 PLAN"被开头计划锁死"这一个问题。

## D. `_handle_replan` 全流程：标注 → 修正计划 → 回灌

模型在执行中调用 replan 后，loop 走到 `_handle_replan`。这是本章核心，整段
拆开看：

```python
async def _handle_replan(self, call, ctx, task, replanned, tier_policy, tier_counts):
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    reason = (
        str(arguments.get("reason", "")).strip()
        or "执行中发现原计划需要调整"
    )
    revision = 1
    if replanned is not None:
        replanned[0] += 1
        revision = replanned[0]
```

第一步：取理由、算修订序号。

- `reason` 从参数取、去空白；模型没给就用默认理由。
- `replanned` 是个单元素列表 `[0]`（用列表是为了在嵌套调用里可变地累加）。
  每重规划一次就 +1，`revision` 从 1 开始，事件里告诉宿主这是第几版计划。

第二步：在独立、协议完整的消息视图中产出修正计划。

执行到 replan 时，主上下文已经保存了本轮完整的 `assistant(tool_calls)`，但
replan 本身及排在它后面的工具还没有结果。此时直接插入 user 指令，会把调用与
结果拆开，严格校验协议的模型接口会拒绝请求；即便本轮只有 replan 一个调用也一样。

```python
    plan_tier = (
        tier_policy.for_planning().tier if tier_policy is not None else STRONG
    )
    if tier_counts is not None:
        tier_counts[plan_tier] = tier_counts.get(plan_tier, 0) + 1
    messages, state_event = await self._model_messages(ctx)
    if state_event is not None:
        yield state_event
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("tool_calls"):
            completed_ids = {
                message["tool_call_id"]
                for message in messages[index + 1:]
                if message["role"] == "tool"
            }
            assistant = dict(messages[index])
            completed_calls = [
                item for item in assistant["tool_calls"] if item["id"] in completed_ids
            ]
            if completed_calls:
                assistant["tool_calls"] = completed_calls
                messages[index] = assistant
            elif assistant.get("content"):
                del assistant["tool_calls"]
                messages[index] = assistant
            else:
                del messages[index]
            break
    messages.append({
        "role": "user",
        "content": (
            "【系统重规划】原计划执行到此因“" + reason + "”需要调整。"
            "请保留已经拿到结果、无需重做的步骤，基于当前已获得的观察，"
            "重新输出 2-5 步编号计划（每行一个步骤，用 1. 2. 编号），"
            "不要重复已完成步骤；只输出步骤本身。"
        ),
    })
    resp = await self._model_chat(messages, None, tier=plan_tier)
```

- `_model_messages` 仍负责多模态转换与宿主实时状态刷新；瞬态状态不写回主上下文。
- 只在本次请求的副本中缩小最新调用批次，保留已有结果的调用；过去的完整历史和
  本批已完成的真实观察原样保留。不伪造未执行工具的结果，也不提前执行后续业务工具。
- 用新字典和新 `tool_calls` 列表替换副本，避免修改与 `Context` 共享的嵌套对象。
  若本批尚无结果，保留非空 assistant 正文；正文也为空时移除这条空消息。
- user 指令只进入规划请求。主上下文不插入额外 user 或 assistant 消息，避免
  打断工具配对，也避免给按 user 轮次裁剪的上下文制造错误边界。
- 档位默认 strong（见第 32 篇），成本感知关闭时也给 strong；`_model_chat`
  保留预算、超时及取消机制。`tools=None`，这次请求只写计划，不执行工具。

第三步：解析步骤、发 `plan_revised` 事件。

```python
    steps = [s.strip() for s in re.findall(r"\d+[.、)]\s*(.+)", resp.content)]
    if not steps:
        steps = [task]
    plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    yield AgentEvent(
        EventType.PLAN_REVISED,
        {"revision": revision, "reason": reason, "steps": steps},
    )
```

- 正则 `\d+[.、)]\s*(.+)` 匹配"1. xxx""2、xxx""3) xxx"三种编号写法，捕获
  每行编号后面的步骤正文，再 `strip()` 掉多余空白。
- **兜底**：万一模型输出里解析不出任何编号步骤，`steps` 就回退成原始任务
  `[task]`，保证事件始终携带可执行内容、不会给宿主一个空列表。
- `plan_revised` 事件包含修订序号、原因、新步骤三个字段；计划正文留给下一步的
  tool 结果，不再作为普通 assistant 消息插入尚未完成的工具批次。

第四步：回灌一条 tool 结果，把模型推回执行轨道。

```python
    ctx.add(
        ChatMessage(
            role="tool",
            content=(
                "计划已按当前观察修订（第 " + str(revision) + " 版）。\n"
                + plan_text + "\n"
                "请按新计划继续调用工具执行，不要重复已完成步骤，"
                "拿到全部结果后给出最终汇报。"
            ),
            tool_call_id=call.id,
            name=call.name,
        )
    )
```

每次 replan 只追加一条匹配调用 ID 的 tool 结果，包含完整编号计划和继续执行提示。
后续同批工具仍按原顺序执行；下一轮模型能看到完整调用批次与一一对应的结果。
若同批再次 replan，新的规划视图能看到此前已完成的重规划结果与业务观察。
模型失败、超时、取消或预算阻断时，不发成功修订事件，也不继续执行同批剩余工具。

## E. 聚焦模式：meta-tool 的 schema 必须始终可见

还有一个隐蔽问题。当宿主工具数超过 `full_schema_budget`（默认 24）时，loop
走"两层目录聚焦"：先让模型只看轻量目录、选出本任务要用的工具，再只注入这些
工具的完整 schema（见 `_focus_tool_schemas`）。模型聚焦选的是**计划里的业务
工具**，不会选 replan，于是 replan（以及 request_capability、create_code_tool）
的完整 schema 在聚焦模式下被裁掉了——模型"计划走不通却看不到重规划入口"。

修法是让 meta-tool 在任何裁剪下都始终注入：

```python
def _with_meta_schemas(self, schemas: list[dict]) -> list[dict]:
    """把内核 meta-tool 的完整 schema 合并进注入集（聚焦裁剪不丢 meta）。"""
    if not self.meta_tools:
        return schemas
    present = {s.get("function", {}).get("name") for s in schemas}
    merged = list(schemas)
    for spec in self.registry.all():
        if spec.name in self.meta_tools and spec.name not in present:
            merged.append(spec.llm_schema())
    return merged
```

- `present` 收集当前注入集里已有的函数名；
- 遍历 registry，凡是属于 meta-tool、却还没在注入集里的，把它的完整 schema 补上；
- 业务工具仍按聚焦结果裁剪，只有 meta-tool 享受"始终在场"。

它在两处被调用：聚焦成功返回时（`_focus_tool_schemas`），以及执行中发现新工具、
重算 schema 时（`_schema_after_registration` 的聚焦分支）。全量模式（工具数在
预算内）本来就 `llm_schemas()` 返回全部、已含 meta，无需补。这是个通用修正，
同时解决了三个 meta-tool 在大规模工具集下的可见性问题。

## F. 测试与口径

`tests/test_replan.py` 用 ScriptedModel 离线覆盖以下主线，**全程不需要 API Key**。
每次模型请求都严格检查工具调用与结果配对，禁止孤立、重复、缺失结果及普通消息插队：

1. **完整重规划闭环**：模型第 0 轮调 replan（带 reason）→ 内核产出 3 步修正
   计划 → 模型按新计划调 search_notes → 收尾。断言：
   - `plan_revised` 恰好一次，`revision=1`、reason 正确、steps 解析为
     `["搜索比赛笔记", "整理结果", "汇报"]`；
   - 重规划后工具仍真正执行（tool_call / tool_result 都在）；
   - DONE 的成本计数 `model_calls=5、strong_calls=2、standard_calls=3`。
2. **默认理由与步骤兜底**：replan 不带 reason 时用默认理由；模型修正计划里
   解析不出编号步骤时，steps 回退为原始任务，不崩溃。
3. **关闭成本感知回归**：`cost_aware=False` 时简单 react 全程 standard、
   strong_calls 为 0（该断言用 REACT 场景，因为规则路由对 PLAN 本就标 strong，
   与成本开关无关）。
4. **同批混合调用**：replan 单独、首位、中间、末位、同批两次；分别测试有无
   assistant 正文。规划只能看到已经完成的真实观察，业务调用恰好一次且顺序不变，
   最终上下文保留全部原始调用 ID，计划正文出现在对应 tool 结果中。
5. **跨轮重规划**：第二次修订保留第一次完整计划，修订序号和调用计数正确。
6. **失败边界**：模型异常、真实异步超时、取消、模型调用预算耗尽均不继续业务工具，
   不发成功修订事件；超时确实取消等待，取消继续向上传播。
7. **宿主现状锚**：规划调用也刷新瞬态状态、发状态变化事件，状态不写入记忆。

## G. 自检（读完应能回答）

1. PLAN 策略"一次性规划"在真实执行中会遇到什么问题？REACT 为什么没有这个问题？
2. replan meta-tool 为什么只有一个可选 `reason`？描述里为什么要写"什么时候别调"？
3. replan 为什么不走用户授权闸？这会不会绕过安全边界？
4. 为什么 replan 改成"进入 PLAN 时才注册"，而不是构造 core 时总是注册？
5. `_handle_replan` 解析不出编号步骤时怎么处理？为什么不能让 steps 为空？
6. 聚焦模式下为什么要 `_with_meta_schemas`？它还顺带修复了哪些 meta-tool 的问题？
