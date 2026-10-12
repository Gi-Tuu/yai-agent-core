# 逐行讲解 37 · 长期记忆语义召回与任务起始主动召回

> 对应文件：
> - `src/yai_core/memory/vault.py`（新增异步 `arecall`：词法 OR 语义双通道；
>   embedder 注入、向量懒缓存、失败降级纯词法；`recall` 工具改走异步入口）；
> - `src/yai_core/kernel/loop.py`（新增 `_inject_auto_recall`：任务开始先把相关
>   长期记忆作为 system 上下文注入，并发 `memories_recalled` 事件）；
> - `src/yai_core/types.py`（新增事件类型 `MEMORIES_RECALLED`）；
> - `src/yai_core/config.py`（新增 `memory_embedder` / `auto_memory_recall` /
>   `auto_recall_k` 三个配置与交叉校验）；
> - `src/yai_core/core.py`（把 embedder 传给 vault、把 `vault.arecall` 绑成回调
>   交给 loop）；
> - 离线测试：`tests/test_memory_vault.py`（新增 8 组异步语义用例）、
>   `tests/test_auto_memory_recall.py`（新建，core 级主动召回端到端）、
>   `tests/test_core_config.py`（新增 3 组配置校验）。全部零网络、零 API Key。
>
> 读前先读第 26 篇（结构化长期记忆 remember/recall 与落盘加密）、第 20 篇
> （EmbeddingProvider SPI 与语义目录）。本篇复用第 20 篇的余弦相似度，不引新依赖。
>
> 第 26 篇的记忆只能**按字面关键词**召回：你记住"用户对芒果严重过敏"，下次问
> "mango allergy?" 一个字都对不上，记忆等于白存。而且模型还得**自己想起来先调
> recall**，没想到就用不上。本篇解决两件事：
> 1. **语义召回**：词面零重叠、含义相关也能找回（可选接入 EmbeddingProvider）；
> 2. **主动召回**：任务一开始，内核自动先检索记忆塞进上下文，不依赖模型"想起来"。

---

## A. 先看结论

| 改动 | 解决的问题 | 一句话做法 |
|---|---|---|
| 异步 `arecall` | 同步 `recall` 只能字面匹配，跨语言/换说法就失忆 | 新增 async 入口，词法通道与语义通道并行打分、取并集融合；同步 `recall` 保留不动 |
| embedder 注入 + 向量懒缓存 | 内核不能硬依赖向量模型，也不能每次召回都重算全部向量 | vault 持有可选 `EmbeddingProvider`，记忆向量按需批量算一次并缓存，增删即失效 |
| 失败降级 | 向量模型挂了不能连累记忆/主任务 | embedder 抛错或返回数量不符 → 记 `semantic_last_error`，退回纯词法，召回不报错 |
| 任务起始主动召回 | 模型没想到调 recall，画像就用不上 | loop 在历史之后、user 任务之前自动调一次召回，命中就注入 system 消息并发事件 |
| 配置三开关 | 能力要可选、可关、可校验 | `memory_embedder` / `auto_memory_recall` / `auto_recall_k`，非法组合构造期即报错 |

纪律不变：**内核零硬依赖**（语义打分只用标准库 `math`，向量模型是第八个 SPI、
可选 extra）；**默认行为安全**（不接 embedder 就是纯词法、不开 auto 就不主动注入，
老行为一字不变）；**全部离线可测**（概念轴 Fake embedder + ScriptedModel）。

---

## B. 为什么是"词法 OR 语义"，而不是用语义替换词法

词法匹配（第 26 篇的 token 重叠 + 子串）有两个语义给不了的优点：

1. **精确**：型号、订单号、人名、专有名词，词法命中就是命中，向量反而可能"近义
   误伤"（把"张三"和"章三"算成很像）。
2. **零成本、确定性**：不算向量、不调网络，离线测试稳定。

所以双通道是**并集融合**，不是二选一：

```
            任务 query
               │
       ┌───────┴────────┐
       │                │
   词法通道          语义通道（可选 embedder）
 token_similarity   embed → 余弦 cosine_similarity
       │                │  (低于阈值 0.45 丢弃)
       └───────┬────────┘
          按记忆 id 取并集
     relevance = max(词法分, 语义分)
               │
      叠重要度加权 → 排序 → top-k
```

一条记忆只要被**任一通道**认定相关就进入候选，融合分取两者较大值（`max` 而不是
相加）：避免"词法已经满分、语义又加一遍"导致分数虚高不可比。

> 阈值 `SEMANTIC_RECALL_THRESHOLD = 0.45` 与第 20 篇发现目录的默认口径一致。
> 记忆召回是"返回 top-k 多条供参考"，不是"只选一个工具"，所以**不加**发现目录那
> 套 margin（冠亚军间隔）要求——记忆宁可多给一两条让模型自己判断。

---

## C. `vault.py`：异步 `arecall` 的结构

### C.1 构造器：embedder 是可选注入，向量缓存与失效

```python
def __init__(self, storage_path=None, *, clock=None, cipher=None, embedder=None):
    ...
    self._embedder = embedder
    self._vectors: dict[str, Sequence[float]] = {}   # note.id -> 向量，懒缓存
    self.semantic_last_error: str | None = None      # 最近一次语义通道错误（可观测）
```

- `embedder` 为 `None` 时，`arecall` 直接等价于同步词法 `recall`（见 C.4 短路），
  这保证**不接向量模型的宿主零成本、零行为差异**。
- 向量按 `note.id` 缓存。记忆会被**合并**（同内容去重）和**删除**，这两种情况旧
  向量必须失效，否则会拿旧向量给新内容打分：

```python
# remember 合并到已有条目时
self._vectors.pop(existing.id, None)
# forget 删除时
self._vectors.pop(note_id, None)
```

### C.2 词法通道抽成可复用的纯函数

原来词法排序逻辑内联在 `recall` 里，现在抽成两个静态/私有方法，让同步、异步两个
入口共用同一份词法实现，避免"两套词法打分漂移"：

- `_matches_filters(note, kinds, tags)`：kind / tag 过滤条件（同步异步都要先过滤）；
- `_rank_lexical(query, *, kinds, tags)`：只做 token 相关度排序，返回
  `[(note, sim), ...]`，**不含**重要度加权、touch、落盘这些副作用。

同步 `recall` 在词法排序结果上再做"重要度加权 → top-k → touch → 保存"，行为与
第 26 篇完全一致（所以原来十几个同步测试一行不用改）。

### C.3 语义通道：懒批量算向量

```python
async def _ensure_vectors(self, notes) -> bool:
    missing = [n for n in notes if n.id not in self._vectors]
    if not missing:
        return True
    try:
        vectors = await self._embedder.embed_texts([n.content for n in missing])
    except Exception as exc:                 # 向量服务不可用
        self.semantic_last_error = f"...{exc}"
        return False
    if len(vectors) != len(missing):         # 返回数量对不上，宁可不用语义
        self.semantic_last_error = "嵌入向量数量与待嵌入记忆数不一致"
        return False
    for note, vector in zip(missing, vectors):
        self._vectors[note.id] = vector
    return True
```

两个要点：

1. **懒**：只有真正走语义召回、且该记忆没算过向量时才算。纯词法命中的场景永远不
   触发向量计算。
2. **批量**：一次 `embed_texts` 把所有缺失向量补齐（embedding API 通常按批计价、
   按批更快），而不是每条记忆调一次。

两道防线都失败时返回 `False`，调用方据此**降级**而不是抛异常。

### C.4 `arecall` 主干

```python
async def arecall(self, query, *, k=DEFAULT_RECALL_K, kinds=None, tags=None):
    if self._embedder is None:
        return self.recall(query, k=k, kinds=kinds, tags=tags)   # 短路：纯词法

    lex_by_id = {n.id: (n, sim) for n, sim in self._rank_lexical(...)}
    sem_by_id: dict[str, tuple[MemoryNote, float]] = {}
    candidates = [过了 filters 的全部记忆]
    if await self._ensure_vectors(candidates):
        query_vector = await self._embed_query(query)
        if query_vector is not None:
            for note in candidates:
                score = cosine_similarity(query_vector, self._vectors[note.id])
                if score >= SEMANTIC_RECALL_THRESHOLD:
                    sem_by_id[note.id] = (note, score)

    ids = set(lex_by_id) | set(sem_by_id)                 # 并集
    ranked = []
    for note_id in ids:
        lex = lex_by_id.get(note_id, (None, 0.0))[1]
        sem = sem_by_id.get(note_id, (None, 0.0))[1]
        note = (lex_by_id.get(note_id) or sem_by_id[note_id])[0]
        relevance = max(lex, sem)                          # 融合取大
        boost = 0.75 + 0.25 * (note.importance / 5)       # 重要度加权（同词法入口）
        ranked.append((note, relevance * boost))
    ranked.sort(key=lambda x: x[1], reverse=True)
    ...  # top-k、touch、落盘，与 recall 同一套收尾
```

`cosine_similarity` 直接复用 `discovery/semantic.py`（标准库 `math` 实现，截断到
[0,1]、维度不齐按共有长度、零向量返回 0）。这里特意 `from ...discovery.semantic
import cosine_similarity`：**memory 依赖 discovery 的纯函数，反向不成立**
（discovery 不 import memory），所以没有循环导入。

### C.5 recall 工具改走异步入口

工具执行器本来就支持异步 handler（`tools/executor.py` 里
`if inspect.isawaitable(result): result = await result`），所以把 recall 工具的
handler 改成 `async def`、内部 `await vault.arecall(...)` 即可，模型侧调用方式不变：

```python
async def _recall_handler(query, k=DEFAULT_RECALL_K, kinds=None, tags=None):
    return await vault.arecall(query, k=k, kinds=kinds, tags=tags)
```

remember 仍是同步的（写入不涉及向量计算）。工具描述从"关键词检索"改为
"按语义 / 关键词相关度检索"，让模型知道这条能力变强了。

---

## D. `loop.py`：任务起始主动召回

语义再强，模型**没想到先调 recall** 还是白搭。主动召回把"要不要回忆"从模型的
自由发挥变成内核的固定动作。

### D.1 注入位置：历史之后、user 任务之前

`astream` 里组装上下文的顺序变成：

```python
ctx = Context(_SYSTEM_TEMPLATE.format(tools=self.registry.catalog_text()))
for msg in self.memory.history():      # 1. 系统提示 + 历史对话
    ctx.add(msg)
async for recall_event in self._inject_auto_recall(ctx, task):  # 2. 主动召回
    yield recall_event
ctx.add(ChatMessage(role="user", content=task))   # 3. 本次任务
```

注意 clarify（信息不足反问）路径在更上方已经 `return`，**反问时不做召回**——任务
都还没说清，召回没有意义。

### D.2 `_inject_auto_recall`：增强能力绝不阻断主任务

```python
async def _inject_auto_recall(self, ctx, task):
    if self._auto_recall is None:
        return
    try:
        hits = await self._auto_recall(task)
    except Exception as exc:                       # 记忆坏了不能让任务失败
        self._diag("auto_recall_failed", f"...{exc}")
        return
    if not isinstance(hits, list) or not hits:
        return
    ...  # 取出 content/kind，拼成若干 "- （kind）content" 行
    ctx.add(ChatMessage(role="system", content=(
        "以下是与当前任务相关的长期记忆，供你参考；若与用户本次的明确指令冲突，"
        "以用户本次指令为准：\n" + "\n".join(lines))))
    yield AgentEvent(EventType.MEMORIES_RECALLED,
                     {"count": n, "items": [{"id", "kind", "content"(截断120字)}]})
```

三个设计决定值得记住：

1. **提示词里写明"以用户本次指令为准"**：记忆可能过时（用户去年不吃辣，今年吃
   了）。当前明确指令永远压过旧画像，避免模型拿陈旧记忆反驳用户。
2. **注入的是 `system` 消息，不是伪装成 user**：角色边界清晰，也不会污染对话历史。
3. **任何异常都吞掉降级 + 发诊断**：记忆是"锦上添花"，召回服务超时/报错绝不能把
   整个任务拖挂。这和预算、超时一样，是"增强功能默认可失败"的一贯纪律。

`_auto_recall` 回调由 Core 注入（见 E），loop 本身不认识 vault、不认识 embedder，
只认一个 `async (task) -> list[dict]` 契约——**内核不反向依赖记忆实现**。

### D.3 事件可观测：不静默决策

命中召回会发 `MEMORIES_RECALLED`，事件里带条数和每条的 id/kind/摘要。宿主 UI
（灵动岛）可以据此显示"已参考 N 条长期记忆"，调试时也能确认"模型到底看到了哪些
记忆"。这遵循项目铁律：**每个自适应动作都必须有事件，禁止黑箱**。

---

## E. `config.py` / `core.py`：接线与构造期校验

### E.1 三个配置项

```python
memory_embedder: EmbeddingProvider | None = None   # 第八 SPI，None=纯词法
auto_memory_recall: bool = False                   # 任务起始是否主动召回
auto_recall_k: int = 3                             # 主动召回注入几条
```

`validate()` 里的交叉校验把错误挡在构造期，而不是等任务跑到一半：

```python
if self.auto_memory_recall and not self.long_memory:
    raise ValueError("auto_memory_recall=True 时必须同时启用 long_memory=True")
if not _is_positive_int(self.auto_recall_k):
    raise ValueError("auto_recall_k 必须是 >=1 的整数")
if self.memory_embedder is not None and not inspect.iscoroutinefunction(
        getattr(self.memory_embedder, "embed_texts", None)):
    raise ValueError("memory_embedder 必须实现 async embed_texts(texts) 方法")
```

- 主动召回依赖长期记忆 vault，没开 `long_memory` 却开召回是无意义组合，直接拒绝；
- embedder 契约是**异步** `embed_texts`（要能调远程），传个同步函数进来大概率是
  接错了，用 `inspect.iscoroutinefunction` 提前拦住。

### E.2 Core 把两者缝起来

```python
# vault 拿到 embedder（语义召回）
self.memory_vault = MemoryVault(storage_path=...,, cipher=..., embedder=memory_embedder)
# loop 拿到"绑死了 k 的 arecall"作为主动召回回调（functools.partial 固定 k）
auto_recall=(
    functools.partial(self.memory_vault.arecall, k=auto_recall_k)
    if (auto_memory_recall and self.memory_vault is not None)
    else None
),
```

用 `functools.partial(vault.arecall, k=...)` 把"召回几条"提前绑死，loop 拿到的就是
一个干净的 `async (task) -> list[dict]`，不需要知道 k、也不需要持有 vault。

---

## F. 离线测试怎么证明"词法零重叠、语义能找回"

语义功能最容易写成"看起来有用、其实没法验证"。测试用一个**确定性概念轴**
Fake embedder（和第 20 篇同款）：文本里出现某概念的中/英文触发词，对应维度就置 1。

```python
_MEMORY_AXES = (
    ("mango", ("芒果", "过敏", "mango", "allergy")),
    ("dark",  ("深色", "暗色", "dark", "夜间模式")),
    ("rain",  ("下雨", "伞", "rain")),
)
class MemoryFakeEmbedder:
    async def embed_texts(self, texts):
        return [[1.0 if any(w in t.lower() for w in words) else 0.0
                 for _, words in _MEMORY_AXES] for t in texts]
```

关键用例 `test_arecall_semantic_finds_when_lexical_zero_overlap`：

```python
vault = MemoryVault(embedder=MemoryFakeEmbedder()); _seed(vault)
assert vault.recall("mango allergy safe?") == []        # 先证明词法确实召不回
results = await vault.arecall("mango allergy safe?")
assert "芒果" in results[0]["content"]                  # 语义通道把它找回
```

**先断言词法为空、再断言语义命中**，这条测试才有说服力——否则无法区分"是语义找到
的"还是"词法本来就能找到"。

其余覆盖：无关 query（向量全 0）返回空；embedder 抛错 / 返回数量不符都降级到词法
且 `semantic_last_error` 有记录；forget / 合并后缓存向量正确失效；status 暴露
`semantic_enabled`。

core 级 `tests/test_auto_memory_recall.py` 用一个 `RecordingModel`（继承
ScriptedModel，额外记下每次收到的 messages）验证三件事：

1. 开 auto + 词法命中 → 有 `MEMORIES_RECALLED` 事件，且模型确实收到了含"芒果"的
   system 记忆；
2. 关 auto（默认）→ 无事件、无注入，老行为不变；
3. 开 auto + embedder + 英文 query（词法 0）→ 语义通道端到端把记忆注入成功。

模型收到的消息是 `ctx.llm_messages()` 产出的 dict（`role`/`content`），断言时按
dict 取字段。

---

## G. 宿主怎么用（接入示例）

```python
from yai_core import AgentCore
from yai_core.integrations.embedding import LocalBgeEmbedder   # [local-embed] extra

embedder = LocalBgeEmbedder()   # 本地 bge-m3，离线；也可换 OpenAI 兼容 embedding
core = AgentCore.auto(
    my_app,
    long_memory=True,
    memory_embedder=embedder,     # 不接则 recall/arecall 退化为纯词法
    auto_memory_recall=True,      # 任务开始自动回忆
    auto_recall_k=3,
)
```

- 只要长期记忆、不要语义：不传 embedder（零额外依赖）；
- 要语义但仍让模型主动调 recall：传 embedder、不开 auto；
- 全都要：两个都开，这是"最像有记忆的陪伴产品"的形态。

---

## H. 小结

- **召回**从"字面匹配"升级为"词法 OR 语义"，但词法保留、语义可选、失败降级，
  内核依旧零硬依赖；
- **回忆时机**从"等模型想起来"变成"任务开始自动做"，且明确"当前指令压过旧记忆"；
- 向量懒缓存 + 增删失效，保证语义通道不重复计费、不拿旧向量打新内容；
- 全部能力经 SPI / 回调注入，loop 不认识 vault、内核不认识向量模型，分层不回潮。

下一篇（38）预留：把语义召回从"长期记忆"推广到更多宿主知识，或做召回质量的离线
评测集（命中率 / 误召回率），让"记忆好不好"也能像路由一样跑出数字。
