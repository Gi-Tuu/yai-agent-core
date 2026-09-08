# 逐行讲解 05 · `kernel/router.py` 与 `kernel/context.py`

> Router 决定"用什么打法"，Context 管理"发给模型的消息本"。都很短，但都是决策核心。

## A. `kernel/router.py`：自适应路由

### A1. 信号词表（L13-L24）
```python
_PLAN_HINTS = ("然后", "接着", "之后", "再把", "分步", "步骤", "先", "并且",
               "同时", "对比", "整理成", "汇总成", "最终", "一共", "分别")
_ACTION_HINTS = ("查", "找", "搜", "列出", "统计", "计算", "导出", "获取",
                 "读取", "记录", "新增", "整理", "分析", "筛选", "生成")
_CLARIFY_HINTS = ("随便", "你看着办", "什么都行", "帮我弄一下")
```
- 三个元组就是三张"关键词表"。这是 v0.1 的确定性规则实现：**零成本、离线可跑、每条规则都能写单元测试**（见 `tests/test_router.py`）。
- 元组而不是列表：这些表不该被运行时修改，元组语义更准确。

### A2. 类与构造（L27-L29）
```python
class AdaptiveRouter:
    def __init__(self, *, min_plan_steps: int = 2) -> None:
        self.min_plan_steps = min_plan_steps
```
- 预留参数 `min_plan_steps`（v0.2 LLM 分类器会用到）。v0.1 先占住扩展位，这叫"为变化留门，但不提前实现"。

### A3. classify 决策函数（L31-L47）——按顺序读，顺序就是优先级
```python
def classify(self, task: str, registry: ToolRegistry) -> Strategy:
    text = task.strip()
    if not text:
        return Strategy.CLARIFY
```
- `strip()` 去掉首尾空白；空任务无法处理 → 反问。

```python
    if any(h in text for h in _CLARIFY_HINTS) and len(text) < 12:
        return Strategy.CLARIFY
```
- `any(生成器)`：只要有一个信号词命中就为 True。
- **为什么还要 `len(text) < 12`？** 防止误杀：一句很长、很具体但恰好含"随便"二字的任务不该被当成模糊请求。规则系统要靠这种"组合条件"降低误判。

```python
    if len(registry) == 0:
        return Strategy.DIRECT
```
- 宿主一个工具都没有，再复杂也没法调工具，只能让模型直接回答。注意它在行动判断**之前**——能力边界优先于用户意图。

```python
    wants_action = any(h in text for h in _ACTION_HINTS)
    multi_step = sum(1 for h in _PLAN_HINTS if h in text) >= 1
    if multi_step and wants_action:
        return Strategy.PLAN
    if wants_action:
        return Strategy.REACT
    return Strategy.DIRECT
```
- `wants_action`：像需要动手的任务。
- `multi_step`：`sum(1 for h in ... if 命中)` 是"数命中了几个多步信号"的生成器写法；命中 ≥1 就算多步。
- 决策优先级：**多步且要动手 → PLAN（先规划再执行）；只动手 → REACT；其余 → DIRECT。**
- 最后一行注释解释了一个反直觉点：DIRECT 时模型并非被禁止用工具，只是首轮不塞工具清单（在 06 篇看 `use_tools`）。

> v0.2 会加 LLM 分类器，但**规则实现保留作兜底**：模型分类失败/超时时回退到这里。这种"智能方案 + 确定性兜底"是生产级 Agent 的常见结构。

## B. `kernel/context.py`：消息本与 token 预算

### B1. 初始化（L9-L11）
```python
def __init__(self, system_prompt: str, *, max_chars: int = 24000) -> None:
    self.max_chars = max_chars
    self.messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
```
- 一次运行对应一个 Context。创建时**第一条永远是 system 消息**（角色设定 + 工具清单）。
- v0.1 用字符数粗略估 token（中文约 1 字 ≈ 1～2 token），所以预算叫 `max_chars`；v2 再换精确的 tokenizer。

### B2. 追加并自动压缩（L13-L25）
```python
def add(self, message: ChatMessage) -> None:
    self.messages.append(message)
    self._compact()
```
- 每次追加后立刻检查是否超预算，调用方不用操心。

```python
def _compact(self) -> None:
    total = sum(len(m.content) for m in m for m in self.messages)  # 示意
```
实际代码：
```python
    total = sum(len(m.content) for m in self.messages)
    i = 1
    while total > self.max_chars and i < len(self.messages) - 1:
        total -= len(self.messages[i].content)
        i += 1
    if i > 1:
        self.messages = [self.messages[0], *self.messages[i:]]
```
逐行：
- 先算所有消息内容总字符数（生成器求和）。
- `i = 1`：**从下标 1 开始**，因为下标 0 是 system 消息，永远保留；最后一条也保留（`i < len-1`），因为它通常是当前问题。
- while 循环：只要还超预算，就"虚拟跳过"第 i 条并累加 i。
- `[self.messages[0], *self.messages[i:]]`：`*` 解包——保留 system，拼接从 i 开始的剩余消息，中间旧消息被丢弃。
- 这是最简单的"滑动窗口"记忆压缩；v2 会换成摘要式压缩。

### B3. 输出给模型（L27-L28）
```python
def llm_messages(self) -> list[dict]:
    return [m.to_llm_dict() for m in self.messages]
```
- 把内部 ChatMessage 列表统一转成模型接口要的字典列表。再次体现"内部对象、边界翻译"。

## 两个文件如何协作

Loop 开始时：
1. `router.classify(任务, 注册表)` → 得到策略；
2. `Context(系统提示词)` → 建消息本；
3. 之后每轮模型回复、工具结果都 `ctx.add(...)`，超预算自动压缩；
4. 每次调模型前 `ctx.llm_messages()` 取最新快照。

## 自检

1. classify 的四个 if 顺序能不能调换？为什么"没有工具"必须排在前面？
2. "随便帮我写个周报框架，要包含本周进展和下周计划"会被路由成什么？为什么？
3. `_compact` 为什么从下标 1 开始、且保留最后一条？
4. 给 `_ACTION_HINTS` 加一个你常用的动词，并在 `tests/test_router.py` 加对应测试。
