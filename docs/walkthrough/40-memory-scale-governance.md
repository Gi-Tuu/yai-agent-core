# 逐行讲解 40 · 长期运行的记忆规模治理（prune 短路 + 词法倒排 + 容量上限）

> 对应文件：
> - `src/yai_core/memory/sqlite_store.py`（`_prune_locked` 改为先用
>   `COUNT(*)` / `MIN(created_at)` 索引聚合做快速闸门，未达阈值绝不取回全表）；
> - `src/yai_core/memory/vault.py`（**词法召回与写入去重**共用一套**懒构建倒排
>   索引**，与全表扫描逐字节等价；新增**可选 `max_notes` 容量上限**，默认 `None`
>   不删任何记忆）；
> - 离线测试：`tests/test_sqlite_memory.py`（新增 D 组：spy 轮对齐纯函数证明
>   短路）、`tests/test_memory_vault.py`（新增倒排"暴力对照"等价性与容量系列）。
>   全部零网络、零 API Key。
>
> 读前先读第 11 篇（SqliteStore、scope、WAL）、第 13 篇（retention 条数 / TTL
> 与轮边界对齐）、第 28 / 37 篇（MemoryVault 的 remember/recall 与语义召回）。
> 本篇不改任何 SPI、不加依赖，只治"长期运行下记忆只增不减"带来的两处规模劣化。

---

## A. 先看结论

数字员工一旦 7×24 挂在宿主里，记忆是**只写不停**的：每一轮对话都往 SQLite
追加历史，长期事实不断沉淀进 vault。审计发现两处会随时间持续劣化、且都发生在
**高频热路径**上：

| 编号 | 问题（改动前） | 后果 | 本篇做法 | 默认行为 |
|---|---|---|---|---|
| D2 | `SqliteStore._prune_locked` 每次 `append_history` 都把本 scope 全量 `(id,role,created_at)` `fetchall` 回 Python，**哪怕根本没配任何裁剪策略** | 每条消息一次 O(n) 白捞；长期历史下写入越来越慢 | 先走 `(scope,id)` 索引上的 `COUNT(*)`/`MIN` 聚合快速判断，确有可能裁剪才取行 | 裁剪结果**逐字节不变**，纯加速 |
| D1-a | vault 词法召回对全部记忆逐条算 `token_similarity` | 记忆上百后每次召回 O(n) 全量打分 | 词法 token **倒排索引**，只对有公共 token 的候选打分 | 召回结果与全扫**逐字节一致** |
| D1-b | vault 记忆 `list` 无限增长，无任何上界 | 长期部署内存 / 落盘 JSON 只涨不跌 | 可选 `max_notes` 容量上限，超限按"重要度低→最久未用→最旧"淘汰 | **默认 `None`，一条都不删** |

三条纪律和前面所有篇目一致：

1. **内核零硬依赖**：倒排索引用的是第 20 篇已有的纯标准库 `tokenize`/`canonicalize`，
   不引新库。
2. **默认安全、老行为不变**：不配策略就不裁剪、不配 `max_notes` 就不删记忆；
   D2 是纯性能重构，D1-a 召回集合严格等价，D1-b 是 opt-in 安全阀。
3. **不做 ANN / 向量库**：语义通道仍逐条算余弦（理由见 E 节），这是项目红线。

---

## B. D2：SQLite 裁剪的 COUNT/MIN 快速闸门

### B.1 改动前为什么是 O(n) 白捞

第 13 篇讲过，历史裁剪要"按对话轮对齐"，不能裸 `DELETE` 切散工具调用对，所以
`_prune_locked` 的正活儿必须把行取回 Python、交给纯函数 `count_cut` / `ttl_cut`
算边界。问题在于旧代码**一上来就取全表**：

```python
rows = conn.execute(
    "SELECT id, role, created_at FROM history WHERE scope = ? ORDER BY id ASC",
    (self._scope,),
).fetchall()
if not rows:
    return 0
```

而 `_prune_locked` 在**每次** `append_history` 末尾都会被调一次。于是：

- 用户**默认根本没配** `max_messages` / `ttl_seconds`（两者都是 `None`）时，
  裁剪逻辑注定什么也不删，却照样把整段历史 `fetchall` 回来、构造一堆 `Row`，
  再走到 `count_cut(None)` / `ttl_cut=None` 全部返回 `None`、零删除；
- 配了策略但远没到阈值时（比如上限 200 条、现在才 12 条），同样白捞一遍。

对话越长，每条新消息的写入代价越高——这是长期运行唯一会**线性劣化**的写路径。

### B.2 快速闸门：让 SQLite 在引擎内回答"要不要紧"

关键是：判断"这次到底可不可能要删"，**不需要看每一行**，两个聚合就够，而且都
能走第 11 篇建好的索引 `idx_history_scope_id ON history(scope, id)`：

- `COUNT(*)`：本 scope 一共多少行？没超过 `max_messages`，条数裁剪一定不触发；
- `MIN(created_at)`：最早一行是什么时候？连最早一行都没过期，TTL 裁剪一定不触发。

聚合在 SQLite 引擎内部完成，只回一个标量，不构造任何 `Row` 对象。改后的
`_prune_locked` 开头是三道早退闸门：

```python
conn = self._require_conn_locked()
# 闸门 0：既没设条数上限也没设 TTL —— 必然无可裁剪，连聚合都省掉。
if self._max_messages is None and self._ttl_seconds is None:
    return 0
agg = conn.execute(
    "SELECT COUNT(*), MIN(created_at) FROM history WHERE scope = ?",
    (self._scope,),
).fetchone()
total = int(agg[0] or 0)
if total == 0:
    return 0
cutoff_text: str | None = None
may_need_prune = False
if self._max_messages is not None and total > self._max_messages:
    may_need_prune = True                 # 条数确超限（最终是否裁还要看轮对齐）
if self._ttl_seconds is not None:
    cutoff = self._clock() - timedelta(seconds=self._ttl_seconds)
    cutoff_text = cutoff.strftime(_TS_FORMAT)
    earliest = agg[1]
    if earliest is not None and earliest < cutoff_text:
        may_need_prune = True             # 最早一行确已过期
if not may_need_prune:
    return 0                              # 闸门：不取回任何历史行
# 确有可能裁剪：才取全量 (id, role, created_at)，走原来的轮对齐逻辑。
rows = conn.execute(... ORDER BY id ASC).fetchall()
```

逐行看几个细节：

- **闸门 0 连 SQL 都不发**：默认配置（两个策略都 `None`）是最常见情形，这一行
  让写路径在纯 Python 里立即返回，零额外查询。
- **`MIN(created_at)` 是 TTL 的充分保守信号**：行按时间追加，最早一行都没到
  cutoff，剩下的只会更新，整 scope 必然没有过期行；反之最早一行过期了，才需要
  取行回来让 `ttl_cut` 按"整轮过期才切"的规则精算。
- **只排除"必然不裁"，边界附近宁可多取一次**：`total > max_messages` 也不代表
  一定删得掉（可能因对不齐轮首而 `count_cut` 返回 `None`），所以标志位取名
  `may_need_prune`，最终裁决仍交给原来的纯函数，**绝不靠 COUNT 裸删**。
- **`cutoff_text` 只算一次**：TTL 的 cutoff 字符串在闸门处算好，传给后面的
  过期判断复用，不再重复取时钟、重复格式化。

闸门之后的代码（取行 → `count_cut`/`ttl_cut` → 轮对齐 → 按 id 删前缀 / 整 scope
清空）与第 13 篇一字不差，所以**只要真的发生裁剪，删除集合和旧实现完全相同**。

### B.3 怎么测试"确实没白捞"

不能去 monkeypatch `sqlite3.Cursor.fetchall`——它是 C 扩展的 immutable 类型，
`setattr` 直接抛 `TypeError`。测试改用一个更干净的观测点：**纯函数
`count_cut` / `ttl_cut` 只可能在"取完全表行之后"被调用**。spy 这两个普通模块
函数，它们零调用就等价于"本次 prune 在聚合闸门处已返回、没走到取行"：

```python
def _patch_cut_counters(monkeypatch):
    from yai_core.memory import sqlite_store as mod
    seen = {"count": 0, "ttl": 0}
    orig_count, orig_ttl = mod.count_cut, mod.ttl_cut
    def counting_count(roles, max_messages):
        seen["count"] += 1
        return orig_count(roles, max_messages)
    def counting_ttl(roles, expired):
        seen["ttl"] += 1
        return orig_ttl(roles, expired)
    monkeypatch.setattr(mod, "count_cut", counting_count)
    monkeypatch.setattr(mod, "ttl_cut", counting_ttl)
    return seen
```

三个用例分别钉住：远未达阈值时两个计数器都为 0（不取回、不算边界）；超阈值时
`count_cut` 确实被调用且结果仍按轮对齐稳定保留；默认无策略时连聚合都不发生。

---

## C. D1-a：vault 词法召回的倒排索引

### C.1 改动前：每次召回都对全部记忆打分

第 28 篇的 `_rank_lexical` 是一段全表扫描：

```python
for note in self._notes:
    if not self._matches_filters(note, kinds, tags):
        continue
    sim = token_similarity(query, note.candidate_text())
    if sim > 0:
        ranked.append((note, sim))
```

`token_similarity`（第 20 篇）内部把 query / candidate 都 `canonicalize`（含
同义词归一）后 `tokenize` 成 unigram 与 bigram，再算 Jaccard 重叠。记忆越多，
每次召回算得越多，而其中**绝大多数记忆和 query 连一个共同 token 都没有**，
`sim` 本来就是 0、注定被 `if sim > 0` 丢掉。

### C.2 关键等价式

回看打分定义，`sim > 0` 当且仅当：

> query 的 unigram 与 candidate 的 unigram **有交集**，
> 或 query 的 bigram 与 candidate 的 bigram **有交集**。

那么只要预先建好"**token → 含该 token 的记忆 id 集合**"的倒排链，召回时把
query 各 token 的 posting 取并集，得到的就是"所有 `sim` 可能 > 0"的候选；
不在并集里的记忆，`token_similarity` 必然为 0，跳过它们与全扫结果**完全一致**。

先把"和 `token_similarity` 完全相同的归一 + 切词"封装成一个 helper——归一不同
会导致漏召回，所以这里必须复用同一套 `canonicalize → tokenize`：

```python
def _index_tokens(text: str) -> set[str]:
    unigrams, bigrams = tokenize(canonicalize(text, DEFAULT_SYNONYM_GROUPS))
    return unigrams | bigrams
```

**为什么 unigram 和 bigram 能放进同一个字典而不会串味？** bigram 只由 CJK 相邻
两字产生（全是两字 token），unigram 是拉丁整词或 CJK 单字；两字 bigram 既不可能
等于一个 CJK 单字，也不可能等于拉丁词，两个 token 空间天然不相交。所以
`unigrams | bigrams` 混在一个 posting 字典里，不会出现"query 的 bigram 错误命中
某条记忆的 unigram"这类假阳性。

### C.3 懒构建 + 写失效

倒排索引是记忆集合的**派生缓存**，最省心的一致性策略是"写就作废、用才重建"：

```python
def _ensure_lex_index(self) -> dict[str, set[str]]:
    if self._lex_index is None:
        index: dict[str, set[str]] = {}
        for note in self._notes:
            for token in _index_tokens(note.candidate_text()):
                index.setdefault(token, set()).add(note.id)
        self._lex_index = index
    return self._lex_index
```

- `__init__` 里 `self._lex_index = None`（尚未构建）；
- `remember`（新增或合并，合并会改文本/标签）、`forget`、容量淘汰之后都置
  `None`；
- `recall` / `arecall` 调 `_rank_lexical` 时才 `_ensure_lex_index()` 重建。

召回是高频读、写入相对低频，这样只在"写后第一次召回"付一次 O(n) 建索引，之后
每次召回只扫候选并集。`_touch`（更新访问时间）不改文本，不使索引失效。

### C.4 召回：候选并集预筛，打分与排序原样保留

```python
index = self._ensure_lex_index()
query_tokens = _index_tokens(query)
candidate_ids: set[str] | None = None
if query_tokens:
    candidate_ids = set()
    for token in query_tokens:
        posting = index.get(token)
        if posting:
            candidate_ids |= posting
ranked = []
for note in self._notes:               # 仍按原列表顺序遍历，稳定排序 tie-break 不变
    if candidate_ids is not None and note.id not in candidate_ids:
        continue
    if not self._matches_filters(note, kinds, tags):
        continue
    sim = token_similarity(query, note.candidate_text())
    if sim > 0:
        ranked.append((note, sim))
return ranked
```

两个边界都刻意保留了老行为：

- **query 切不出 token（纯标点 / 空白）**：`query_tokens` 为空，`candidate_ids`
  置 `None`，表示"不做倒排预筛"，退回对全部记忆打分（此时全扫也都算得 0、结果
  为空），与历史行为完全一致。
- **kinds/tags 过滤仍在倒排之后**：倒排候选只按"文本有无公共 token"取，权限/
  类型过滤继续用 `_matches_filters` 把关，最终集合 = 候选 ∩ 过滤 ∩ `sim>0`，
  和旧的"过滤 ∩ sim>0"相同（非候选本就 sim=0）。

### C.5 用"暴力对照"测试钉死等价性

口说无凭，测试里另写一个**不用倒排、逐条全扫**的参考实现 `_brute_force_lexical`，
然后对一批中英混合、含同义词、含无关项的语料，用多个 query（中文命中、英文命中、
跨语言零交集、纯标点、完全无关）断言两种实现返回的 `{id: sim}` **完全相等**，
再补一组带 kinds/tags 过滤的对照。倒排可能引入的任何漏召回 / 多召回都会被这组
"差分测试"立刻抓到。

### C.6 写入路径的去重也复用同一索引

倒排不只加速"读"。每次 `remember` 写入前都要跑 `_find_duplicate`，判断新内容是否与
某条既有记忆高度重复（阈值 `DEDUP_SIMILARITY = 0.92`，命中则合并标签 / 重要度而非新增）。
它和召回用的是**同一个相关度函数**（都对 `candidate_text()` 算 `token_similarity`），所以
旧实现里这段同样是 O(n) 全表逐条打分。

它可以零成本复用同一个 `_lex_index`：先用新内容的 token 取候选并集，再只对候选打分。
关键等价式和召回完全一样——公共 token 交集为空当且仅当 `token_similarity == 0`，而
0.92 的合并阈值远高于 0，**候选之外的记忆本就不可能被合并**，因此"选中哪一条、是否合并"
与全表扫描逐字节一致，只是把写入路径也从 O(n) 降为 O(候选数)。新内容切不出 token（纯
标点）时同样回退全扫。查重发生在新笔记 append 之前，此时懒构建（或重建）的索引恰好只
覆盖"要查重的既有记忆"，不会把新内容自己算进去。

测试里再写一个 `_brute_force_duplicate` 全表参考，对"完全一致 / 近改写 / 零公共 token /
纯标点"多组内容断言两条路径选中的 note id 相同（含都为 `None`），把写入路径的等价性也钉死。

---

## D. D1-b：可选 max_notes 容量上限

倒排解决了"检索 O(n)"，但记忆条数本身仍可能无限增长。D1-b 给宿主一个**有界
存储安全阀**，关键点是**默认关闭、绝不擅自删数据**。

### D.1 构造参数与校验

```python
if max_notes is not None and (
    isinstance(max_notes, bool) or not isinstance(max_notes, int) or max_notes <= 0
):
    raise ValueError("max_notes 必须是正整数，或为 None（不限制）")
self._max_notes = max_notes
```

`bool` 要单独排除（`True` 在 Python 里也是 `int`、`True == 1`，否则
`max_notes=True` 会被静默当成 1）。非法值在**构造期**就报错，而不是等运行中
悄悄丢记忆。

### D.2 淘汰谁：一条确定性的排序键

```python
ordered = sorted(
    self._notes,
    key=lambda n: (n.importance, n.last_used_at, n.created_at, n.id),
)
drop_count = len(self._notes) - self._max_notes
victim_ids = {n.id for n in ordered[:drop_count]}
```

升序排序、越靠前越先删，淘汰优先级是：

1. **重要度低**先删（`importance` 1–5）；
2. 同重要度里 **最久没被召回使用**的先删（`last_used_at`，这正是 recall 会
   `_touch` 刷新的字段，让"常用记忆"自然留下来）；
3. 再相同则**最旧**先删（`created_at`）；
4. 最后用 `id` 兜底，保证结果完全确定、可复现。

注意 **importance=5 不设硬豁免**：它只是排序最后、最难被删，但若所有记忆都是
5 星且容量超限，照样按时间淘汰。否则一旦用户把所有东西标成 5，容量就永远收敛
不了，安全阀形同虚设。被淘汰记忆的缓存语义向量 `_vectors` 同步 pop，倒排索引
置 `None`，淘汰条数累计进 `_evicted_count`。

### D.3 三个触发时机与可观测

- **写入时**：`remember` 落盘前调一次 `_enforce_cap()`（新增或合并后都调，
  合并不增条数时它立即返回 0）；
- **打开存量库时**：宿主可能运行很久后才把上限调小，所以 `__init__` 在 `_load`
  之后也收敛一次，若真的删了就立即 `_save()` 落盘（测试用"先无上限写 5 条 →
  带上限 2 重开 → 再无上限打开"验证文件里确实只剩 2 条）；
- **状态可观测**：`status()` 新增
  `"capacity": {"max_notes": ..., "evicted": ...}`，宿主能在后台看到"上限多少、
  累计淘汰了多少"。

### D.4 为什么默认 None、且不替内核改 max_messages

长期记忆是用户的个人事实，"删哪条"是产品决策而非内核该擅自做的事。所以：

- vault 的 `max_notes` 默认 `None`：**一条不删**，要由宿主显式给上限；
- 第 13 篇 SQLite 历史的 `max_messages` 同样保持默认 `None`，D2 只让"配了策略
  但没到阈值"变便宜，不改变"默认不裁剪"。

需要有界部署的宿主（比如数字员工长期运行的本地进程），在自己的部署文档 /
配置模板里给一个**宽松**值即可（例如历史保留若干千条、长期事实保留若干百条），
而不是在内核里写死一个可能误删的数字。

---

## E. 为什么语义通道不做倒排、也不做 ANN

你可能会问：D1-a 只加速了**词法**通道，`arecall` 的语义通道不还在逐条算余弦、
还是 O(n) 吗？是的，而且这是**故意**的：

1. **语义命中本就没有字面交集**。语义通道的全部价值就是召回"芒果过敏"↔
   "mango allergy"这种零公共 token 的记忆；用 token 倒排预筛会把它们直接漏掉，
   等于自废武功。要加速语义检索只能上向量索引（ANN / 向量库）。
2. **项目红线明确不做内置向量库 / ANN**（见 README"不适合谁"与架构边界）。
   内核保持零硬依赖、进程内嵌入；重索引、持久化、量化这些复杂度交给宿主或专门
   的检索服务，不进内核。
3. **`max_notes` 已经让语义通道的规模有界**。宿主给长期事实设一个合理上限后，
   逐条余弦的上限就是这个有界值（几百到一两千条，标准库 `math` 完全扛得住）；
   真到数万条规模，正确方向是外接向量库的 SPI，而不是在内核里堆 ANN。

所以本篇的取舍是：**词法用倒排把 O(n) 降到 O(候选)，语义靠容量上限保持有界，
ANN 明确不做。**

---

## F. 自测题（读完本篇应能回答）

1. D2 为什么不能在 `COUNT(*) > max_messages` 时直接 `DELETE` 多余行，而还要
   `fetchall` 走 `count_cut`？
2. `_prune_locked` 的三道早退闸门分别排除了哪种"必然不裁"的情况？为什么 TTL
   只看 `MIN(created_at)` 就够？
3. 倒排索引为什么能保证和全表扫描的召回集合"逐字节一致"？写出那条关键等价式。
4. unigram 和 bigram 为什么可以安全混放在同一个倒排字典里？
5. query 是纯标点（切不出 token）时，`_rank_lexical` 为什么要退回全扫而不是
   直接返回空？
6. `max_notes` 淘汰排序键的四个字段依次是什么？为什么 importance=5 不硬豁免？
7. 为什么 `max_notes` 和 SQLite 的 `max_messages` 默认都保持 `None`？
8. 语义通道为什么不套用词法倒排？项目为什么选择不做内置 ANN？
