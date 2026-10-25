# 逐行讲解 06 · `kernel/loop.py`：Core 的心脏（最重要，慢读）

> 建议配合断点读：在 `astream`、`_react_cycle` 里设断点，跑 `scripts/smoke_test.py` 看每一步。
> 知识点：异步生成器、`yield` 事件流、async for、递归重入、ReAct 循环、消息角色闭环。

## 块 1 · 系统提示词模板（模块常量 `_SYSTEM_TEMPLATE`）
```python
_SYSTEM_TEMPLATE = """你是运行在宿主软件内部的 AI 助手。\
你可以使用宿主提供的工具（见下方工具目录）。\
需要扩展能力时按此选择：\
固定多步编排宿主已有工具（含先取数、再加工）用 compose_tool；\
数据由调用方提供、只需全新纯计算（换算 / 评分 / 统计）用 create_code_tool\
（沙箱内不能回调宿主工具）；\
宿主根本没有该能力，就明确指出缺失处或调用 request_capability。\
不要编造工具不存在的数据。当任务完成时，直接给出可交付的最终结果。

宿主当前提供的能力：
{tools}
"""
```
- 这是发给模型的"人设与规矩"。`{tools}` 占位符运行时被 `registry.catalog_text()` 替换——**轻量目录**：每个工具一行"名字 + 一句话摘要"。
- 行尾反斜杠 `\` 表示字符串里不换行（人设各句连成一段），空行才真正分段。
- 当前人设给的是**三条扩展路径**（按"宿主到底缺什么"分流，不是笼统一句"能力不足就提出"）：
  1. **固定多步编排宿主已有工具**（含先取数、再加工）→ `compose_tool`（只编排已注册工具，不生成代码）；
  2. **数据由调用方提供、只需全新纯计算**（换算 / 评分 / 统计）→ `create_code_tool`（沙箱内**不能**回调宿主工具）；
  3. **宿主根本没有该能力** → 明确指出缺失处，或调 `request_capability` 让内核去发现。
- **保留的护栏一条没删**："不要编造工具不存在的数据"仍然在，防幻觉比提主动性更优先；
  提示词也没有承诺"自动安装/自动发现"，内核只负责把缺口喊出来，补不补、怎么补由宿主决定。
- **完整参数结构不在这里**：系统提示只放轻量目录，完整 JSON Schema 在 function-calling 的
  `tools` 参数里按需注入（两层工具目录；源码里 `catalog_text()` 上方那两行注释就是这个意思）。

## 块 2 · 构造（`def __init__`）
```python
class AgentLoop:
    def __init__(
        self,
        model: ModelProvider,
        registry: ToolRegistry,
        executor: ToolExecutor,
        channel: Channel,
        memory: MemoryStore,
        router: AdaptiveRouter | None = None,
        discovery: ToolDiscovery | None = None,
        *,
        max_iters: int = 6,
        max_clarify_rounds: int = 2,
        full_schema_budget: int = 24,
        strict_direct: bool = False,
        ...
    ) -> None:
        ...
        self.router = router or AdaptiveRouter()
        self.max_iters = max_iters
        self.max_clarify_rounds = max_clarify_rounds
```
- 五大依赖全部注入。`router or AdaptiveRouter()`：调用方没给就用默认款（`None or x` 结果是 x，这是常见的默认值惯用法）。
- 两个失控保护：`max_iters=6` 限制工具循环轮数；`max_clarify_rounds=2` 限制澄清反问轮数（见块 3.5）。
- 这两个参数**已经能从 `AgentCore` 透传**：`AgentCore(max_iters=6, max_clarify_rounds=2)` 在装配时原样传给 `AgentLoop`（`core.py` 里 `max_iters=` / `max_clarify_rounds=` 两行），默认值仍是 6 / 2，宿主可按需改。
- `...` 省略的都是**可选增强**，不传即关闭：工具发现源、schema 预算、direct 严格模式、meta-tool、上下文压缩、成本感知选档、模型硬超时、诊断回调、任务预算、主动召回、宿主实时状态。

## 块 3 · astream：总调度（`def astream`）

```python
async def astream(
    self,
    task: str,
    *,
    _clarify_depth: int = 0,
    subject: Subject | None = None,
) -> AsyncIterator[AgentEvent]:
```
- 关键语法：`async def` + 函数体内有 `yield` = **异步生成器**。调用它得到一个"异步事件流"，外部用 `async for event in core.astream(...)` 逐个取事件。好处：任务还在跑，UI 就能实时显示"正在调工具…"，而不是干等 30 秒拿一个最终结果。

```python
    decision = await self.router.aclassify(
        task, self.registry, discoverable=self._discoverable_catalog()
    )
    strategy = decision.strategy
    yield AgentEvent(
        EventType.STRATEGY_SELECTED,
        {
            "strategy": strategy.value,
            "source": decision.source,
            "reason": decision.reason,
            "tier": decision.tier,
        },
    )
```
- 第一步决策策略，立刻"喊出来"（发事件）。`aclassify` 是 v0.2 的异步入口：
  配置了 LLM 路由就先模型分类、失败回退规则，返回带 `source/reason/tier` 的 RouteDecision
  （见 05 篇 C 节）；事件里保留 `strategy` 键，老的消费方不受影响。
- 多出来的 `discoverable=` 参数：宿主配了发现源时，把"可按需启用"的轻量目录一并交给路由；
  发现源没实现 `list_discoverable` 或调用失败就返回 None，退化成"只在缺口出现后被动发现"。

```python
    # 能力缺口感知：发缺口事件；配置发现源时把缺口闭环成新工具（本轮即可用）。
    async for ev in self._emit_capability_gap(decision, task):
        yield ev
```
`_emit_capability_gap` 内部（规则路径、澄清、无工具部署形态不发）：

```python
    if (
        decision.missing_capability
        and decision.strategy != Strategy.CLARIFY
        and len(self.registry) > 0
    ):
        yield AgentEvent(
            EventType.CAPABILITY_MISSING,
            {
                "task": task,
                "missing": decision.missing_capability,
                "available_tools": [s.name for s in self.registry.all()],
                "strategy": decision.strategy.value,
            },
        )
```
- 紧接着首个路由事件的，是**能力缺口事件**。模型在分类时若发现"现有工具不足以完成任务"，
  路由决策里会带一句缺口描述（如"天气查询能力"），Loop 把它喊给宿主：
  AMBRACE 这类宿主可以监听这个事件，引导用户安装插件、接入 MCP Server。
  这是"嵌入式内核 ↔ 宿主插件生态"之间的桥。
- **四条不发边界**（都有测试）：① 规则路径没有缺口判断，`missing_capability` 恒为 None；
  ② LLM 分类异常回退规则时不发；③ 策略是 clarify 时即使模型误填了缺口也不发（信息不足时
  先澄清，谈不上缺能力）；④ 宿主一个工具都没有的部署形态不发（分类短路走规则，
  且这种纯聊天形态不需要插件引导）。
- 事件 data 只有四个字段：`task/missing/available_tools/strategy`——**只描述缺口，
  不规定宿主该做什么**（不给"建议安装 XX"之类的动作字段），内核不越界替宿主做产品决策。
- 发完事件后**照常按选定策略继续执行**，缺口事件不改变本次流程。

- 签名里还有个下划线开头的内部参数 `_clarify_depth`（块 3.5 讲）：澄清递归的"层数计数器"，外部调用方不传，只在递归自调时 +1。

```python
    if strategy == CLARIFY and _clarify_depth >= self.max_clarify_rounds:
        strategy = DIRECT
        yield STRATEGY_SELECTED(..., source="rules", tier=decision.tier)  # 降级也发事件

    if strategy == CLARIFY:
        preview = 截断到 80 字的 task
        question = _CLARIFY_QUESTION.format(task=preview)
        yield CLARIFY_REQUESTED {"question": question, "round": depth + 1}
        answer = (await self.channel.ask(question) or "").strip()
        if not answer:
            yield MODEL_MESSAGE(收尾文案) → memory 双写 → yield DONE(strategy=clarify)
            return
        merged = f"{task}\n补充信息：{answer}"
        async for ev in self.astream(merged, _clarify_depth=depth + 1):
            yield ev
        return
```
- 澄清分支曾经只有三行（问用户 → 拿回答 → 递归），实测会无限反问，现在加了**三道保险**，见块 3.5。

```python
    ctx = Context(_SYSTEM_TEMPLATE.format(tools=self.registry.catalog_text()))
    for msg in self.memory.history():
        ctx.add(msg)
    ctx.add(ChatMessage(role="user", content=task))
```
- 建消息本：system（含工具清单）→ 历史记忆 → 当前用户任务。
- `str.format(tools=...)` 填充模板。
- `{tools}` 填的是**轻量目录**（`registry.catalog_text()`，每个工具一行 name + 一句话摘要）；
  完整 JSON Schema 不在这里，而是在 function-calling 的 `tools` 参数里按需注入
  （源码里 `catalog_text()` 上方那两行注释写的就是这件事，见块 5.1）。
- `registry.describe` 是**另一个**方法（完整描述文本），内核里已没有调用点。

```python
    if strategy == Strategy.PLAN:
        self._ensure_replan_tool()
        async for ev in self._make_plan(
            ctx,
            task,
            tier=decision.tier,
            tier_policy=self.tier_policy,
            tier_counts=tier_counts,
        ):
            yield ev
```
- plan 策略：先规划（额外一轮强模型调用），事件同样转发。
- `_ensure_replan_tool()` 幂等注册 `replan` meta-tool：多步计划走不通时，模型可以显式
  要求重规划（空宿主 / direct / 简单 react 不注册）。

```python
    # 计划只是"助手说过的话"，必须再推一把，模型才会进入工具执行；
    # 否则真实模型会把计划本身当成最终答复（离线脚本模型曾掩盖此问题）。
    ctx.add(
        ChatMessage(
            role="user",
            content="请按上面的计划逐步调用工具执行，拿到全部结果后给出最终汇报。",
        )
    )
```
- **这是接真实模型后才暴露、离线测试没抓到的坑**（Day 1-3 修复）。
- 拆完计划后，消息本最后两条是 `user：拆步骤指令 → assistant：计划文本`，对话停在"助手刚说完计划"。真实模型会认为轮次该结束了，于是把计划原文当最终答复，**一个工具都不调**。
- 修复办法：补一条 user 消息明确下令"按计划执行"。这模拟了多轮对话里用户的追问，模型才会带着计划进入下面的 ReAct 循环。
- 教训：**离线脚本模型（ScriptedModel）只会按剧本走，测不出"模型愿不愿意继续"这类问题**；关键路径必须用真实模型回归一次。

```python
    final_text = ""
    async for ev in self._react_cycle(
        ctx,
        use_tools=strategy != Strategy.DIRECT,
        task=task,
        strategy=strategy,
        tier_policy=self.tier_policy,
        tier_counts=tier_counts,
        subject=subject,
    ):
        yield ev
        if ev.type == EventType.MODEL_MESSAGE:
            final_text = ev.data.get("text", final_text)
```
- 进入主循环。`use_tools=strategy != DIRECT`：只有 react/plan 才把工具清单给模型；direct 首轮不给。
  默认仍兼容"模型在 direct 下凭记忆返回 tool_calls"的旧行为；宿主传 `strict_direct=True`
  （`CoreConfig` 同名项）后改为失败关闭：不执行、发 `ERROR`、保留文字回答。
  另外，模型把 `<tool_call>` / `<arg_key>` 等 XML 调用写进正文时，内核不把它当最终答案、
  也不尝试解析执行，而是发 `ERROR(stage=model_protocol)` 并换成正面的失败说明。
- 一边转发事件，一边顺手记下最终文本（MODEL_MESSAGE 事件里）。`dict.get(k, 默认)` 找不到键时保留旧值。

```python
    await self.memory.append_history(ChatMessage(role="user", content=task))
    await self.memory.append_history(ChatMessage(role="assistant", content=final_text))
    done_data: dict[str, object] = {
        "strategy": strategy.value,
        "final_text": final_text,
        "standard_calls": tier_counts[STANDARD],
        "strong_calls": tier_counts[STRONG],
        "model_calls": tier_counts[STANDARD] + tier_counts[STRONG],
    }
    yield AgentEvent(EventType.DONE, done_data)
```
- 收尾：把这一轮问答写进记忆，发 DONE 事件（携带最终结果）。
- DONE 里还带**成本证据**：`standard_calls` / `strong_calls` / `model_calls` 是本次任务的
  模型调用次数（规划 + 执行 + 收尾口径，不含路由分类与上下文压缩调用）；配了预算时
  还会附上 `budget` 快照，触发熔断则附 `reason="budget_exceeded"`。

## 块 3.5 · 澄清分支的三道保险（为什么不能无限反问）

早期版本是"发事件 → `channel.ask` → 拿回答重新 `astream(回答)`"，实测在 host_e 出现死循环：
用户说"发 xx 公司"→ 内核问意图 → 用户答意图 → 内核**丢掉原任务**只拿回答路由 → 又问公司 →
无限乒乓，且没有退出路径。修复靠三道保险：

1. **轮数上限（防无限递归）**：`_clarify_depth >= max_clarify_rounds`（默认 2）时降级 DIRECT。
   递归深度被硬限制在 2，生成器链最多 3 层，栈安全；降级时连工具一起关掉（DIRECT），
   因为信息不全时硬调工具比不调更糟，由模型直接说明缺什么。降级本身也是一次策略变化，
   必须补发 STRATEGY_SELECTED 事件。
2. **上下文累积（防乒乓）**：递归传的是 `merged = f"{task}\n补充信息：{answer}"`，
   原任务和每一轮补充都串在一起重新路由，模型看得到完整对话。
3. **空回答体面收尾（防永久挂起）**：`(answer or "").strip()` 为空（用户放弃 / 通道超时 /
   网页点终止）时，不再递归、不调模型，发固定收尾文案、双写 memory、发 DONE 后 return。

配套细节：反问用固定话术 `_CLARIFY_QUESTION`（不调模型、离线可测，领域名词只作举例以保持内核通用）；
任务预览截断 80 字防 UI 撑爆；CLARIFY_REQUESTED 事件带 `round` 字段（1 起递增）。
测试见 `tests/test_loop.py` 的 5 个 clarify 用例：收敛、跨轮累积、两轮上限降级、长任务截断、空回答收尾，
并断言整条事件链**恰好一个 DONE**。

## 块 4 · _make_plan：先拆步骤（`def _make_plan`）
```python
    prompt = (
        "把下面的任务拆成 2-5 个可执行步骤，每行一个步骤，用 1. 2. 3. 编号，"
        "只输出步骤本身：\n" + task
    )
    ctx.add(ChatMessage(role="user", content=prompt))
    # 档位：成本感知开启时由策略决定（规划默认 strong），否则沿用路由建议 tier。
    plan_tier = tier_policy.for_planning().tier if tier_policy is not None else tier
    messages, state_event = await self._model_messages(ctx)
    resp = await self._model_chat(messages, None, tier=plan_tier)
```
- 拼一个"只做规划"的提示词，加进消息本。
- 第二个参数 `None`（`tools` 位置）= 规划轮不许调工具，只输出文字。
- 档位不再写死 `"strong"`：开成本感知时由 `TierPolicy.for_planning()` 决定（默认 strong），
  关闭时沿用路由给出的 tier。
- 模型调用统一走 `self._model_chat(messages, None, tier=plan_tier)`——它是内核**唯一**的模型
  调用入口：任务预算熔断闸、单次调用硬超时（`asyncio.timeout`）、档位透传都在这一层；
  超时会被转成带说明的 `TimeoutError`，由上层发 ERROR 事件体面收尾。
- `_model_messages(ctx)` 负责组装出站消息：先把宿主实时状态渲染成**瞬态** system 块
  塞进本次消息（不进历史、不进记忆），并顺带把 `HOST_STATE` 事件带出来。

```python
steps = [s.strip() for s in re.findall(r"\d+[.、)]\s*(.+)", resp.content)]
if not steps:
    steps = [task]
plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
ctx.add(ChatMessage(role="assistant", content=plan_text))
yield AgentEvent(EventType.PLAN_CREATED, {"steps": steps})
```
- 正则 `\d+[.、)]\s*(.+)` 匹配 "1. / 1、/ 1)" 开头的步骤行，`(.+)` 捕获步骤正文。
- 模型没按格式输出时兜底：把整个任务当成唯一步骤，**绝不让空计划卡住流程**。
- 把计划作为 assistant 消息放回消息本——这样后续工具循环"看得到自己刚定的计划"。
- 发 PLAN_CREATED 事件，UI 可展示计划。

## 块 5 · _react_cycle：ReAct 工具循环（`def _react_cycle`）

### 5.1 循环开头
```python
    if use_tools:
        tools_schema, focused_names = await self._focus_tool_schemas(task, tier_counts)
    else:
        tools_schema, focused_names = None, None
    ...
    for iteration in range(self.max_iters):
```
- 工具 schema 不是一次性全量塞进去：`_focus_tool_schemas` 决定本轮注入哪份完整 schema——
  工具数不超过 `full_schema_budget`（默认 24）就一次给全量；超过就先让模型只看**轻量目录**
  挑出工具名，再只注入被选中那些的完整 schema（聚焦失败回退全量，宁多勿漏）。
- `use_tools=False`（direct）时 `tools_schema` 为 None，模型本轮看不到任何工具。
- `for iteration in range(self.max_iters)`：最多 n 轮；`iteration` 供成本感知选档用
  （执行到第 3 轮起仍未收尾就升 strong）。

### 5.2 调模型，网络错误不崩
```python
    try:
        messages, state_event = await self._model_messages(ctx)
        if state_event is not None:
            yield state_event
        resp = await self._model_chat(messages, tools_schema, tier=tier)
    except BudgetExceeded:
        # 预算熔断冒泡到 astream 统一收尾，不能被下面的"模型失败体面 return"吞掉。
        raise
    except Exception as exc:  # noqa: BLE001
        yield AgentEvent(EventType.ERROR, {"error": f"{type(exc).__name__}: {exc}"})
        return
```
- 把当前消息本快照 + 工具清单发给模型。模型挂了（网络/Key/限流/硬超时）就发 ERROR 事件并优雅结束，而不是抛栈崩溃。
- 档位 `tier` 由成本感知策略每轮给出（未开启时恒 `standard`），档位相对上轮变了才发 `TIER_CHANGED` 事件。
- **预算熔断故意不吞**：`BudgetExceeded` 直接向上冒泡到 `astream` 的统一熔断点，改发
  `BUDGET_EXCEEDED` 事件与固定诚实收尾文案。

### 5.3 模型不调工具 = 任务收尾
```python
    if not resp.tool_calls:
        text = resp.content.strip()
        ctx.add(ChatMessage(role="assistant", content=text))
        yield AgentEvent(EventType.MODEL_MESSAGE, {"text": text, "tier": tier})
        return
```
- 空列表/None 都算"没有工具调用"（`not []` 为 True）。
- 模型给的自然语言就是最终答案：入消息本、发事件、`return` 结束循环。**循环的唯一正常出口在这里。**
- 正文里出现裸 XML 调用标记时，先走 `_looks_like_raw_tool_call` 分支：发
  `ERROR(stage=model_protocol)` 并把固定说明文案当最终回答——既不把假调用当答案展示，
  也不去解析执行它。

### 5.4 模型要调工具：先把"调用请求"记账
```python
ctx.add(ChatMessage(
    role="assistant", content=resp.content,
    tool_calls=[{"id": tc.id, "type": "function",
                 "function": {"name": tc.name,
                              "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                for tc in resp.tool_calls],
))
```
- OpenAI 协议要求：assistant 发起工具调用时，要先以 assistant 角色把调用记录原样放回消息历史，否则下一轮会报"tool_call 找不到对应 assistant 消息"。
- 这段源码抽成了独立方法 `_append_assistant_tool_calls(ctx, resp)`，循环里只是一行调用。
- 注意 `arguments` 要序列化成 **JSON 字符串**（协议要求字符串，不是对象）。

### 5.5 逐个执行并把结果回填
```python
for call in calls:
    tool_events, ok, result_text = await self.executor.execute(
        call.name,
        call.arguments,
        subject=subject,
        budget=self._active_budget,
        call_id=call.id,
    )
    for tool_event in tool_events:
        yield tool_event
    ctx.add(ChatMessage(role="tool", content=result_text,
                        tool_call_id=call.id, name=call.name))
```
- 模型可能一轮要多个工具，逐个执行；这段源码在 `_execute_tool_calls` 里（内核 meta-tool
  先被内核自己拦下处理，不进普通执行器）。
- executor 返回的事件逐个转发（TOOL_CALL/TOOL_RESULT/PERMISSION_ASKED）。
- `subject=` 是宿主注入的**可信主体**（能力闸用）；`budget=` 是任务级预算计数器——组合工具的
  真实步骤也会逐次过同一道闸，不能把"一次组合"记成一次业务调用；超限异常冒泡到 `astream`
  统一熔断。
- `call_id=call.id` 是 v0.14 **执行收据**的来源：执行器用它把 `ExecutionReceipt`
  （not_started / succeeded / failed / unknown / partial）挂到事件上，宿主据此对账；
  宿主给了 `idempotency_key` 时，同 key 重试不再进 handler（进程内缓存，跨进程仍归宿主）。
- **关键闭环**：每个工具结果以 `role="tool"` 消息放回消息本，并用 `tool_call_id` 对应到是哪次调用。下一轮模型就能"看到"工具返回了什么，再决定继续调还是收尾。

### 5.6 迭代上限兜底（`def _finish_at_iteration_limit`）
```python
# for 循环正常跑完（6 轮都在调工具、没收尾）才会走到这里
async for ev in self._finish_at_iteration_limit(ctx, tier_counts):
    yield ev
```
```python
ctx.add(
    ChatMessage(
        role="user",
        content="已达到工具调用上限，请基于已有结果直接给出最终答案。",
    )
)
messages, state_event = await self._model_messages(ctx)
resp = await self._model_chat(messages, None, tier=STANDARD)
yield AgentEvent(
    EventType.MODEL_MESSAGE,
    {"text": resp.content.strip(), "tier": STANDARD},
)
```
- 如果 6 轮都没结束，强制再问模型一次且**不给工具**（想调也没得调），逼它基于已有观察交答案。这保证 run 一定会终止。
- 这一轮固定用标准档 `STANDARD`（源码里写死，不随成本感知升档），同样走 `_model_chat` 这一个出口。

## 一张时序表（react 两轮的消息本变化）

| 时刻 | 消息本内容（角色：内容） |
|---|---|
| 开始 | system：人设+工具清单 → user：任务 |
| 第1轮模型返回工具调用 | + assistant：我要调 search_notes |
| 执行后 | + tool：工具返回的 JSON |
| 第2轮模型给最终答案 | + assistant：最终自然语言 → MODEL_MESSAGE，结束 |

## 自检（这篇必须全懂再往下）

1. 异步生成器和普通 async 函数的区别？为什么 Agent 过程适合"边跑边 yield"？
2. clarify 分支靠哪三道保险保证一定能结束？为什么递归时要把原任务和补充信息拼起来？
3. 为什么 assistant 的 tool_calls 和 tool 结果都必须写回消息本？缺了 tool_call_id 会怎样？
4. 循环有哪三个出口？（正常收尾 / 错误 / 上限兜底）
5. direct 策略下 `tools_schema` 是什么？模型此时还能调工具吗？`strict_direct` 改变了哪一步？
6. 为什么 plan 拆完步骤后必须再补一条 user 消息？离线脚本模型为什么没暴露这个问题？
