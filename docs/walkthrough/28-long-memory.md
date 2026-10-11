# 逐行讲解 28 · 结构化长期记忆：让 Agent 主动"记住"和"回忆"

> 对应文件：`src/yai_core/memory/vault.py`，以及 `src/yai_core/core.py` 的接线。
> 读前先读第 07（InMemoryStore）、11（SqliteStore）、13（历史裁剪）章。
> 本章新增的是**另一层记忆**，不改记忆契约、不改 Loop。

## A. 先分清两种"记忆"

到上一章为止，Core 的记忆只有一种：**对话历史**（`MemoryStore` 契约）。它有两个特点：

1. 是一条**线性流水账**：user、assistant、tool 消息按时间排；
2. 会被**裁剪**：超条数 / 超 TTL / 上下文压缩时，旧消息被删掉或压成摘要。

这对"接着上句话聊"够用，但模型**没法主动回忆**。比如上周你说过"对芒果过敏"，
只要那句话不在当前上下文窗口里，模型就不知道——它没有一个"我去翻一下长期档案"的动作。

本章加的 `MemoryVault` 就是第二层：**结构化长期记忆**。对比：

| | 对话历史 `MemoryStore` | 长期记忆 `MemoryVault` |
|---|---|---|
| 形态 | 线性消息流 | 一条条带类型/标签的**事实条目** |
| 谁来用 | Loop 自动回放 | **模型主动调 `recall` 工具**去查 |
| 生命周期 | 会被裁剪/压缩 | 长期保留，除非显式 `forget` |
| 内容 | 原话 | 抽取后的事实/偏好/画像/事件 |
| 默认 | 开启 | **opt-in**，`long_memory=True` 才有 |

一句话：**对话历史是"聊天记录"，Vault 是"档案柜"；模型多了 `remember`（存档）和 `recall`（翻档案）两个工具。**

## B. 一条记忆长什么样：`MemoryNote`

```python
@dataclass
class MemoryNote:
    id: str                 # 唯一编号，形如 mem_0001_a1b2c3
    content: str            # 事实正文，例如 "用户对芒果严重过敏"
    kind: str               # 类型，见下方五种
    tags: list[str]         # 标签，例如 ["过敏", "饮食"]
    importance: int         # 重要度 1~5，越大越该被想起
    created_at: str         # 创建时间（ISO）
    last_used_at: str       # 最近一次被 recall 命中的时间
    access_count: int       # 被命中过几次
```

`kind` 有五种，决定"这是哪类档案"，检索时可以按类型过滤：

```python
VALID_KINDS = ("fact", "preference", "profile", "episode", "note")
# fact 事实 / preference 偏好喜好 / profile 画像资料 / episode 事件经历 / note 一般笔记
```

`candidate_text()` 把正文、标签、类型拼成一段文本，供打分时使用——
**检索不是只看正文，标签和类型也参与匹配**，这样查"饮食"能翻到标了"饮食"的过敏条目。

## C. 写入：`remember()` 与"去重合并"

```python
def remember(self, content, *, kind="note", tags=None, importance=3):
    ...
    return note, already_existed   # 第二个返回值表示是否命中了已有条目
```

几个边界处理（都是防御性的，值得学）：

1. **空正文直接报错**：`content.strip()` 为空就抛 `ValueError`，不存空档案；
2. **非法类型降级**：传入 `kind="xxx"` 不报错，安静降级成 `"note"`；
3. **重要度夹取**：`importance` 被夹在 1~5；传非数字则用默认值 3；
4. **标签归一**：`None` 跳过、`str()` 后 `strip()`、去重。

**最关键的是去重合并**。存之前先用 `token_similarity`（第 20 章讲过的词法打分）
和每条旧条目比相似度：

```python
if best_similarity >= self.dedup_similarity:   # 默认 0.92
    # 视为同一件事：合并，而不是新建
    existing.tags = 并集
    existing.importance = max(旧, 新)
    existing.access_count += 1
    return existing, True
```

为什么需要它：同一件事模型可能换个说法反复存（"我芒果过敏""对芒果严重过敏"），
不合并的话档案柜会塞满重复条目，召回也会重复。**返回值 `already_existed` 让工具结果
能如实告诉模型"这条已经有了"**，而不是假装新建成功。

## D. 检索：`recall()` 怎么给结果排序

```python
def recall(self, query, *, k=5, kinds=None, tags=None):
    ...
    return [ {...}, ... ]   # 每条含 content/kind/tags/importance/score
```

步骤：

1. **先过滤**：指定了 `kinds` / `tags`，就只在符合类型/标签的条目里找；
2. **逐条算相似度**：`query` 和 `candidate_text()` 做 `token_similarity`；
3. **重要度加权**：

   ```python
   score = similarity * (0.75 + 0.25 * importance / 5)
   ```

   含义：相似度是主导（占 75% 保底），重要度在其上做小幅加成——
   importance=5 的条目系数为 1.0，importance=1 的为 0.8。**重要度只能微调，不能
   让一条完全不相关的"重要"档案靠权重冲上来**，这是刻意的安全设计。
4. **排序取 top-k**：按 `(score, importance, created_at)` 降序，取前 `k` 条；
5. **触达刷新**：被返回的条目更新 `last_used_at`、`access_count += 1`。
   这让"最近常被翻出来的档案"可观测，也为以后做"长期不被用就归档"留钩子。
6. 相似度 ≤ 0（一个共同词都没有）的条目不进结果，所以无命中返回 `[]`。

## E. 落盘：原子写 + 损坏容忍

Vault 可以只在内存里跑（`storage_path=None`），也可以落到一个 JSON 文件。
落盘的两个工程细节：

```python
# 原子写：先写 .tmp，再 os.replace 覆盖，避免写到一半崩溃留下半截坏文件
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(...); os.replace(tmp, path)
```

```python
# 读的时候文件损坏 / 内容不合法：不崩溃，当作空档案冷启动
try:
    data = json.loads(path.read_text())
    ...
except (OSError, json.JSONDecodeError):
    self._notes = []     # 下次写入自然覆盖坏文件
```

父目录不存在时 `mkdir(parents=True)` 自动建。**"坏文件不致命、能自愈"是持久化
组件的通用要求**，和第 11 章 SQLite、第 24 章 learning_vault 的处理思路一致。

## F. 怎么变成模型能调的工具：`build_memory_tools()`

```python
def build_memory_tools(vault) -> list[ToolSpec]:
    return [
        ToolSpec(name="remember", ..., handler=同步闭包),
        ToolSpec(name="recall",   ..., handler=同步闭包),
    ]
```

两个闭包分别包一层 `vault.remember(...)` / `vault.recall(...)`，把返回值整理成
JSON 友好的 dict。注意它们是**普通 native 工具**（`source="native"`），**不是
meta-tool**——这点很重要，决定了它们在 Core 里怎么接线。

## G. 在 Core 里接线：为什么是普通工具

`core.py` 的改动（对照源码看）：

1. **构造参数**新增 `long_memory: bool = False`、`memory_storage`；
2. **构造 vault**：`if long_memory: self.memory_vault = MemoryVault(storage_path=...)`；
3. **注册工具**：

   ```python
   if self.memory_vault is not None:
       self.registry.register_many(build_memory_tools(self.memory_vault))
   ```

   因为 remember/recall 是普通工具，**注册进 `ToolRegistry` 就够了，Loop 一行都
   不用改**：模型发起调用 → `ToolExecutor` 走统一权限闸 → 执行闭包 → 结果回灌。
   对比 `request_capability` / `create_code_tool` 是 meta-tool，要 Loop 自己拦截。
4. **后台方法**：`long_memory_status()`（未启用返回 `enabled=False`）、
   `forget_memory(id)`（删指定档案），供宿主 UI / 管理面板调用。

## H. 边界：这一章刻意不做什么

- **不做向量长期记忆**：Vault 的检索是零依赖词法打分。更强的语义召回（同义改写、
  跨语言）应经 `Embedding SPI`（第 20 章）由宿主提供，内核不背向量模型这个重依赖；
- **不自动抽取**：v0.x 由模型在对话中显式调 `remember` 存档，不做"每轮结束自动
  从对话里抽事实"——那是后续能力，先把"能主动存、能主动取"的闭环做扎实；
- **不改记忆契约 / Loop**：Vault 是加在 `memory/` 下的并列组件，与 `MemoryStore`
  各管一层，互不影响。

## 自检（读完应能回答）

1. 对话历史 `MemoryStore` 和长期记忆 `MemoryVault` 有哪三点不同？
2. `remember` 命中高度相似的旧条目时为什么要"合并"而不是新建？合并了哪些字段？
3. `recall` 的打分公式里，为什么重要度只占 0.75~1.0 这个很小的系数区间？
4. 为什么 remember/recall 注册进 `ToolRegistry` 就能用，而不必像 meta-tool 那样
   让 Loop 特殊拦截？
5. Vault 落盘用哪两步保证"写不坏、读得回"？
