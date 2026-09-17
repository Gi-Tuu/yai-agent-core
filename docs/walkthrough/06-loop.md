# 逐行讲解 06 · `kernel/loop.py`：Core 的心脏（最重要，慢读）

> 建议配合断点读：在 `astream`、`_react_cycle` 里设断点，跑 `scripts/smoke_test.py` 看每一步。
> 知识点：异步生成器、`yield` 事件流、async for、递归重入、ReAct 循环、消息角色闭环。

## 块 1 · 系统提示词模板（L27-L33）
```python
_SYSTEM_TEMPLATE = """你是运行在宿主软件内部的 AI 助手。\
你只能通过"工具"操作宿主的能力，不要编造工具不存在的数据。\
当任务完成时，直接给出可交付的最终结果。

宿主当前提供的能力：
{tools}
"""
```
- 这是发给模型的"人设与规矩"。`{tools}` 占位符运行时被 `registry.describe()` 替换。
- 行尾反斜杠 `\` 表示字符串里不换行（三句人设连成一段），空行才真正分段。

## 块 2 · 构造（L48-L72）
```python
def __init__(self, model, registry, executor, channel, memory,
             router: AdaptiveRouter | None = None, *,
             max_iters: int = 6, max_clarify_rounds: int = 2) -> None:
    ...
    self.router = router or AdaptiveRouter()
    self.max_iters = max_iters
    self.max_clarify_rounds = max_clarify_rounds
```
- 五大依赖全部注入。`router or AdaptiveRouter()`：调用方没给就用默认款（`None or x` 结果是 x，这是常见的默认值惯用法）。
- 两个失控保护：`max_iters=6` 限制工具循环轮数；`max_clarify_rounds=2` 限制澄清反问轮数（见块 3.5）。
- 这两个参数目前都没有从 `AgentCore` 透传（处境一致），用默认值；配置化留待后续版本。

## 块 3 · astream：总调度（L56-L92）

```python
async def astream(self, task: str) -> AsyncIterator[AgentEvent]:
```
- 关键语法：`async def` + 函数体内有 `yield` = **异步生成器**。调用它得到一个"异步事件流"，外部用 `async for event in core.astream(...)` 逐个取事件。好处：任务还在跑，UI 就能实时显示"正在调工具…"，而不是干等 30 秒拿一个最终结果。

```python
    decision = await self.router.aclassify(task, self.registry)
    strategy = decision.strategy
    yield AgentEvent(EventType.STRATEGY_SELECTED, {
        "strategy": strategy.value, "source": decision.source,
        "reason": decision.reason, "tier": decision.tier})
```
- 第一步决策策略，立刻"喊出来"（发事件）。`aclassify` 是 v0.2 的异步入口：
  配置了 LLM 路由就先模型分类、失败回退规则，返回带 `source/reason/tier` 的 RouteDecision
  （见 05 篇 C 节）；事件里保留 `strategy` 键，老的消费方不受影响。

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
    ctx = Context(_SYSTEM_TEMPLATE.format(tools=self.registry.describe()))
    for msg in self.memory.history():
        ctx.add(msg)
    ctx.add(ChatMessage(role="user", content=task))
```
- 建消息本：system（含工具清单）→ 历史记忆 → 当前用户任务。
- `str.format(tools=...)` 填充模板。

```python
    if strategy == Strategy.PLAN:
        async for ev in self._make_plan(ctx, task):
            yield ev
```
- plan 策略：先规划（额外一轮强模型调用），事件同样转发。

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
    async for ev in self._react_cycle(ctx, use_tools=strategy != Strategy.DIRECT):
        yield ev
        if ev.type == EventType.MODEL_MESSAGE:
            final_text = ev.data.get("text", final_text)
```
- 进入主循环。`use_tools=strategy != DIRECT`：只有 react/plan 才把工具清单给模型；direct 首轮不给。
- 一边转发事件，一边顺手记下最终文本（MODEL_MESSAGE 事件里）。`dict.get(k, 默认)` 找不到键时保留旧值。

```python
    await self.memory.append_history(ChatMessage(role="user", content=task))
    await self.memory.append_history(ChatMessage(role="assistant", content=final_text))
    yield AgentEvent(EventType.DONE, {"strategy": strategy.value, "final_text": final_text})
```
- 收尾：把这一轮问答写进记忆，发 DONE 事件（携带最终结果）。

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

## 块 4 · _make_plan：先拆步骤（L94-L106）
```python
prompt = ("把下面的任务拆成 2-5 个可执行步骤，每行一个步骤，用 1. 2. 3. 编号，"
          "只输出步骤本身：\n" + task)
ctx.add(ChatMessage(role="user", content=prompt))
resp = await self.model.achat(ctx.llm_messages(), tools=None, tier="strong")
```
- 拼一个"只做规划"的提示词，加进消息本。
- `tools=None`：规划轮不许调工具，只输出文字。
- `tier="strong"`：模型路由钩子，规划这种难活交给强模型。

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

## 块 5 · _react_cycle：ReAct 工具循环（L108-L164）

### 5.1 循环开头
```python
tools_schema = self.registry.llm_schemas() if use_tools else None
for _ in range(self.max_iters):
```
- 准备工具清单（direct 时为 None）。
- `for _ in range(n)`：`_` 表示"这个循环变量我用不到"；最多 n 轮。

### 5.2 调模型，网络错误不崩
```python
try:
    resp = await self.model.achat(ctx.llm_messages(), tools=tools_schema)
except Exception as exc:
    yield AgentEvent(EventType.ERROR, {"error": f"{type(exc).__name__}: {exc}"})
    return
```
- 把当前消息本快照 + 工具清单发给模型。模型挂了（网络/Key/限流）就发 ERROR 事件并优雅结束，而不是抛栈崩溃。

### 5.3 模型不调工具 = 任务收尾
```python
if not resp.tool_calls:
    text = resp.content.strip()
    ctx.add(ChatMessage(role="assistant", content=text))
    yield AgentEvent(EventType.MODEL_MESSAGE, {"text": text})
    return
```
- 空列表/None 都算"没有工具调用"（`not []` 为 True）。
- 模型给的自然语言就是最终答案：入消息本、发事件、`return` 结束循环。**循环的唯一正常出口在这里。**

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
- 注意 `arguments` 要序列化成 **JSON 字符串**（协议要求字符串，不是对象）。

### 5.5 逐个执行并把结果回填
```python
for call in resp.tool_calls:
    tool_events, ok, result_text = await self.executor.execute(call.name, call.arguments)
    for tool_event in tool_events:
        yield tool_event
    ctx.add(ChatMessage(role="tool", content=result_text,
                        tool_call_id=call.id, name=call.name))
```
- 模型可能一轮要多个工具，逐个执行。
- executor 返回的事件逐个转发（TOOL_CALL/TOOL_RESULT/PERMISSION_ASKED）。
- **关键闭环**：每个工具结果以 `role="tool"` 消息放回消息本，并用 `tool_call_id` 对应到是哪次调用。下一轮模型就能"看到"工具返回了什么，再决定继续调还是收尾。

### 5.6 迭代上限兜底（L156-L164）
```python
# for 循环正常跑完（6 轮都在调工具、没收尾）才会走到这里
ctx.add(ChatMessage(role="user",
    content="已达到工具调用上限，请基于已有结果直接给出最终答案。"))
resp = await self.model.achat(ctx.llm_messages(), tools=None)
yield AgentEvent(EventType.MODEL_MESSAGE, {"text": resp.content.strip()})
```
- 如果 6 轮都没结束，强制再问模型一次且**不给工具**（想调也没得调），逼它基于已有观察交答案。这保证 run 一定会终止。

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
5. direct 策略下 `tools_schema` 是什么？模型此时还能调工具吗？
6. 为什么 plan 拆完步骤后必须再补一条 user 消息？离线脚本模型为什么没暴露这个问题？
