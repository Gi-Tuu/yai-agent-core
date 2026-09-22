# 逐行讲解 20 · 双通道语义发现：`scoring.py` + `spi/embedding.py` + `semantic.py`

> 第 15 篇让 Core 能"发现工具"，但那次靠的是**关键词子串**——缺口里必须出现"天气"两个字，才能找到天气工具。
> 真实用户不会这么配合：他会说"出门该怎么穿、要不要带外套"，一个"天气"字样都没有。
> 本篇把"发现哪个工具"从**布尔命中**升级为**可排序的相关度**，并加一条**语义通道**：
> 词法（零依赖、永远可用）+ 向量语义（可选、可插拔），两条腿走路。
> 知识点：第八个 SPI、文本规范化、中文 bigram、同义词归并、余弦相似度（标准库 `math`）、
> 双通道召回、相对边际（margin）防误召回、可选依赖懒加载、本地 bge-m3 离线推理。

## 0. 先看效果：同一个缺口，两条通道的差别

运行 `examples/host_f_discovery/run_semantic.py`（默认零依赖、零 API Key，结果确定）：

```
缺口描述： 出门该怎么穿、要不要带外套
（其中没有任何"天气"字样）
纯词法通道召回： （无）→ 关键词没命中
双通道（词法+语义）召回： ['get_weather']
```

纯词法通道"看不到"天气；加上语义通道后，系统理解"出门/外套/怎么穿"指向天气，于是把
`get_weather` 找了回来。紧接着是第 15 篇那条闭环：

```
[能力缺口] 缺少：出门该怎么穿、要不要带外套
[按需发现] 新工具已注册：['get_weather']（来源 SemanticCatalog）
[工具] 调用 get_weather({'city': '湛江'})
[模型] 湛江当前晴，27℃，穿短袖即可，出门不用带外套。
```

本机若放了 bge-m3 模型（见第 5 节），加 `--local` 用**真实向量**跑同一条闭环：

```
.venv/Scripts/python.exe examples/host_f_discovery/run_semantic.py --local
```

本篇涉及三个新文件 + 两个真实后端：

| 文件 | 角色 | 第三方依赖 |
|---|---|---|
| `discovery/scoring.py` | 词法通道：规范化 + 打分 | 无（纯标准库） |
| `spi/embedding.py` | 第八个 SPI：文本嵌入契约 | 无 |
| `discovery/semantic.py` | 双通道融合目录 `SemanticCatalog` | 无（余弦用标准库） |
| `integrations/embedding/openai_compat.py` | 云端 OpenAI 兼容后端 | `[llm]` extra，懒加载 |
| `integrations/embedding/local_bge.py` | 本地 bge-m3 ONNX 后端 | `[local-embed]` extra，懒加载 |

## 1. 第八个 SPI：只约定"怎么把文本变成向量"

和前七个 SPI 一样，内核先定义插槽，不内置实现。`spi/embedding.py` 全文很短：

```python
@runtime_checkable
class EmbeddingProvider(Protocol):
    """把一批文本编码为等长向量。实现通常是 OpenAI 兼容的 /embeddings 客户端。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...
```

逐行：

- `@runtime_checkable`：让 `isinstance(obj, EmbeddingProvider)` 在运行时可用（它只检查
  有没有 `embed_texts` 这个方法，不检查签名）。
- `async def embed_texts(texts)`：**批量**输入文本，返回**等长、等维度**的向量列表。
  批量是为了一次性编码所有候选工具，比一条条调快。
- 为什么**独立于 `ModelProvider`**（聊天模型那个 SPI）？因为聊天模型不一定提供 embedding。
  实测 Agnes / DeepSeek 的免费档都只有文本/图像/视频，没有 embedding 端点。拆成两个契约，
  宿主可以只接一个嵌入后端、不接聊天模型，反之亦然。
- 契约里写明三条约定：**保序**（`result[i]` 对应 `texts[i]`）、**维度恒定**（同一后端
  输出维度固定，候选向量才能缓存复用）、**失败抛异常**（由调用方决定降级，而不是让 SPI
  自己吞掉错误）。

任何对象只要有一个签名匹配的异步 `embed_texts`，就算实现了它——这就是鸭子类型。

## 2. 词法通道：`scoring.py`（零依赖，永远可用）

语义通道需要模型，可能没装、可能没网。所以词法通道必须**永远在、零依赖、离线可测**。
它把第 15 篇的"关键词子串布尔命中"升级成 0~1 的相关度分数。

### 2.1 规范化 `normalize`：先把文本"对齐"

```python
def normalize(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    kept: list[str] = []
    for ch in text:
        if "一" <= ch <= "鿿" or ch.isalnum():
            kept.append(ch)
        else:
            kept.append(" ")
    return re.sub(r"\s+", " ", "".join(kept)).strip()
```

- `unicodedata.normalize("NFKC", ...)`：全角转半角、兼容字符归一。用户输"Ｗｅａｔｈｅｒ"
  （全角）和"weather"（半角）归一后相同。
- `.lower()`：小写化，`Weather` 与 `weather` 对齐。
- 逐字符扫描：中文（`"一" <= ch <= "鿿"` 是 CJK 基本区的区间判断）或字母数字保留，
  其余标点/符号统一折叠成空格。
- 最后 `re.sub(r"\s+", " ", ...)` 把连续空格压成一个。

这样"查一下，天气！"和"查一下天气"归一后一致。

### 2.2 同义词归并 `canonicalize`：把"带伞"映射到"天气"

```python
DEFAULT_SYNONYM_GROUPS = (
    ("天气", "气温", "温度", "下雨", "降雨", "降水", "雨伞", "带伞", "淋雨", "weather"),
    ("搜索", "查询", "查找", "检索", "搜一下", "找一下", "查一下", "search", "find", "lookup"),
    ("创建", "新增", "新建", "添加", "录入", "登记", "create", "add"),
)
```

每组第一个词是**规范词**，其余词在归一阶段被替换成规范词。于是"要不要带伞"归一后变成
"要不要天气"，和候选描述里的"天气"落到同一字面。

注意这个词表**刻意很小**：词表越大，越容易在召回阶段制造"看起来相关其实不相关"的误命中。
它只覆盖高置信、跨领域通用的几组；真正的跨概念理解交给语义通道。宿主也可以传入自己的
`groups` 覆盖或扩展。

### 2.3 分词 `tokenize`：中文单字 + bigram，拉丁按词

```python
_LATIN_WORD = re.compile(r"[a-z0-9]+")
_CJK_CHAR = re.compile(r"[一-鿿]")

def tokenize(text):
    norm = normalize(text)
    unigrams: set[str] = set(_LATIN_WORD.findall(norm))
    cjk = _CJK_CHAR.findall(norm)
    unigrams.update(cjk)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return unigrams, bigrams
```

- 拉丁/数字按"词"切：`get_weather` 归一后是 `get weather`，得到 `{"get","weather"}`。
- 中文切出**单字**（`天`、`气`）和**相邻两字 bigram**（`天气`）。
- 为什么要 bigram？单字太宽泛——"天"可能出现在"今天""明天""聊天"里；bigram"天气"
  才是稳定的语义单元。

### 2.4 相似度：对 query 归一的重叠系数

```python
def _overlap(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)
```

分母是 **query 的 token 数**（不是候选的），含义是"**query 的 token 有多少能在候选里找到**"。
这样长候选不会因为"包含的词多"而天然占优。

`token_similarity` 把单字和 bigram 加权：`0.4*单字重叠 + 0.6*bigram 重叠`，让更精确的
bigram 占主导。

### 2.5 合成词法分 `lexical_score`

```python
keyword_hit = 任何一个关键词（归一后）出现在缺口/任务原文里
token_sim    = token_similarity(query, 工具名+描述+关键词)
score = 0.6 * keyword_hit + 0.4 * token_sim
```

- 关键词命中是**强信号**（这是第 15 篇 `StaticCatalog` 的唯一判据），给 0.6 权重。
- token 重叠是**弱信号**，让"没命中关键词但高度字面重合"也能被召回，给 0.4。
- 结果包成不可变的 `LexicalHit(score, keyword_hit, token_sim)`，便于测试和观测。

## 3. 余弦相似度：内核不依赖 numpy，标准库 `math` 就够

两个向量的"方向是否一致"用余弦相似度衡量：

```python
def cosine_similarity(a, b) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = norm_a = norm_b = 0.0
    for i in range(n):
        x, y = a[i], b[i]
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    cos = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    if cos < 0.0:
        return 0.0
    return 1.0 if cos > 1.0 else cos
```

- 值域 [-1, 1]，这里截断到 [0, 1]：工具发现只关心"相似"，不关心"反义"。
- 零向量返回 0（避免除零）。
- 维度不齐按共有长度算（防御性，正常情况下同一 embedder 维度恒定）。
- **为什么不用 numpy？** 内核红线是零第三方硬依赖。算一次余弦只是点积和模长，标准库
  循环完全够用；numpy 只在"产生向量"的后端（本地 ONNX）里用，属于可选 extra。

## 4. `SemanticCatalog`：两层摘要 + 双通道召回

### 4.1 两层结构：检索用轻量摘要，命中才内省完整 Schema

工具多了，把每个工具的完整 JSON Schema 全塞进 embedding / 上下文会爆。所以：

```python
def _candidate_summary(cand) -> str:
    return "工具名 + 一句话描述 + 关键词"   # 轻量，用于 embedding 检索
```

检索阶段只对这句轻量摘要算向量；只有相关度过阈值、进入 top-k 的候选，才调用 `build_spec`
内省出完整 JSON Schema 交给内核注册（第 15 篇讲过 `build_spec`）。

### 4.2 候选向量只算一次，query 向量每次现算

候选目录相对稳定，`_candidate_vectors` 懒加载并缓存（只 embed 一次）；query 每次任务不同，
每次现算。这是把 embedding 调用次数和延迟压到最低的关键。

### 4.3 关键设计：语义是"独立召回 gate"，不是加权平均的一项

最初的实现用融合分 `final = 0.5*词法 + 0.5*语义`，结果真机 bge-m3 上**纯语义命中被稀释**：
"出门/外套"对天气的余弦是 0.48，词法分是 0，融合后只有 0.24，过不了阈值。

真实 embedding 的余弦分布被压缩在 0.3~0.7（不像测试里 0/1 概念轴那么极端）。用本地 bge-m3
实测：

| query | 天气 | 待办 | 股票 | 订单 | 记忆 | 正确目标 |
|---|---|---|---|---|---|---|
| 出门/外套（无"天气"字样） | **0.479** | 0.395 | 0.383 | 0.397 | 0.337 | 天气 ✓ |
| 今天涨了还是跌了 | 0.400 | 0.486 | **0.602** | 0.499 | 0.388 | 股票 ✓ |
| 订一张去北京的机票 | 0.477 | 0.510 | 0.440 | 0.517 | 0.409 | **无**（都不该召回）|
| 写一首五言律诗 | 0.295 | — | — | — | — | 明显无关 |

可以看到：**正确目标的余弦总是最高（排序正确）**，但绝对值都不高；而"订机票"对订单的
余弦 0.517 甚至不低——可它和第二名（待办 0.510）几乎没有差距。

据此把召回逻辑改成**两条独立通道、OR 放行**：

1. **词法 gate**：`lexical_score >= 阈值`（高精度，关键词命中即召回）。
2. **语义 gate**：
   - 选出语义余弦最高的 top1；
   - 绝对门槛 `top1 >= 0.45`（实测无关 query ≤ 0.32，语义相关 ≥ 0.48）；
   - **多候选（≥3）时额外要求边际**：top1 必须比第二名高出 `sem_min_margin`（默认 0.05）。

边际这一条专门治"订机票"：0.517 对 0.510 只差 0.007，说明没有哪个工具真正匹配，于是
**正确地什么都不召回**（让内核走澄清/告知缺口，而不是硬塞一个错误工具）。候选少于 3 个时
没有可靠的"背景分布"，不做边际判断，只看绝对门槛。

放行后，融合分 `0.5*词法 + 0.5*语义` 只用于**排序**（决定 top-k 顺序），不再用于决定
"要不要召回"。这就是检索工程里"召回阶段高召回、排序阶段精排"的分工。

阈值默认值都用真实 bge-m3 校准过，且可通过构造参数覆盖：

```python
SemanticCatalog(
    candidates,
    embedder,
    score_threshold=0.45,   # 词法门槛 / 语义绝对门槛
    sem_min_margin=0.05,    # 多候选时 top1 的领先边际
    semantic_weight=0.5,    # 排序时语义权重
)
```

## 5. 真实后端一：本地 bge-m3（离线、免费、与 AMBRACE 同源）

`integrations/embedding/local_bge.py` 加载本地 **bge-m3 int8 ONNX** 模型（CPU 推理、1024 维）。
它和 AMBRACE 的 `backend/app/memory/embedding.py` 用**同一份模型、同一套后处理**（XLM-R 的
CLS pooling + L2 归一化），所以未来 Core 内嵌进 AMBRACE 时向量空间一致，可直接共享模型和
向量缓存。

后处理逐行：

```python
enc = tokenizer.encode(text)
ids  = np.array([enc.ids], dtype=np.int64)
mask = np.array([enc.attention_mask], dtype=np.int64)
token_emb = session.run(None, {"input_ids": ids, "attention_mask": mask})[0]
cls  = token_emb[0, 0].copy()          # 取第 0 个 token（CLS）的向量
norm = np.linalg.norm(cls)
return (cls / np.maximum(norm, 1e-9)).tolist()   # L2 归一化
```

- `token_emb[0,0]`：第一批（唯一一条）、第 0 个 token（CLS）的隐藏向量。
- 除以模长做 L2 归一化后，向量模长为 1，余弦相似度就等于点积。

两个工程细节：

1. **依赖懒加载**：`tokenizers` / `onnxruntime` / `numpy` 都在 `_load()` 里才 import，且放在
   可选 extra `[local-embed]`。内核本体的 `dependencies` 仍然是空的。
2. **推理放线程池**：ONNX 是 CPU 密集，`embed_texts` 用 `asyncio.to_thread(...)` 包装同步推理，
   不阻塞事件循环（这是 AMBRACE 早期踩过的坑——在事件循环里直接推理会让所有并发请求周期性卡顿）。

模型文件约 560MB，**不入库**（`.gitignore` 忽略 `models/`，只保留 `models/README.md` 说明
从哪下载）。目录解析顺序：构造参数 `model_dir` > 环境变量 `EMBEDDING_MODEL_DIR` >
当前工作目录的 `models/bge-m3`。

## 6. 真实后端二：OpenAI 兼容云端

`integrations/embedding/openai_compat.py` 对接任何 OpenAI 兼容的 `/v1/embeddings` 端点
（通义、硅基移动、OpenAI 等）。它复用 `[llm]` extra 的 `openai` 包，懒加载：

- 密钥 / base_url / 模型名读 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `EMBEDDING_MODEL`；
- 端点可能乱序返回，代码按返回体里的 `index` 字段**重新排序**，保证 `result[i]` 对应
  `texts[i]`（契约要求保序）；
- 空输入直接返回 `[]`，不发请求。

注意：**Agnes 和 DeepSeek 的免费档实测都不提供 embedding**（对 `text-embedding-3-small`
等返回 `model_not_found`）。想要免费云端语义通道，可选用提供 embedding 免费额度的厂商
（如硅基移动的开源 bge 系列、智谱 embedding 系列）；想要完全离线、零额度、隐私好，就用
第 5 节的本地 bge-m3。

## 7. 降级与红线：语义是增强，不是必需

`SemanticCatalog` 在三个层面保证"没有语义也能活"：

1. **不配置 embedder**：`embedder=None`，整套语义逻辑跳过，退化为纯词法通道（仍是对
   `StaticCatalog` 的打分增强），行为与第 15 篇一致。
2. **候选向量编码失败**：`_embed_candidates` 捕获异常，记到 `self.last_error`，本次走词法。
3. **query 向量编码失败**：同样降级，不致命。

内核 `_discover_tools` 外层还有一层"发现失败不致命"的 try/except，这里是双保险。
`last_error` 把降级原因暴露出来，宿主可观测、可测试，而不是静默失败。

红线回顾：内核 `src/yai_core/` 不 import numpy、不 import openai、不 import onnxruntime；
所有真实后端都在 `integrations/` 且懒加载、走可选 extra。

## 8. 离线测试怎么不花一分钱

测试不允许依赖网络或 API Key（项目纪律）。语义通道这样测：

- **词法通道**：直接断言分数，纯标准库。
- **语义通道**：写一个确定性的 `FakeEmbedder`（概念轴向量），例如"天气/外套/出门"在
  weather 轴上置 1，其余为 0。它把真实 embedding"语义相近则余弦高"的性质压缩成肉眼可验证
  的 0/1 向量，离线复现"纯语义召回"。
- **降级**：`BoomEmbedder`（永远抛异常）验证自动降级为词法、`ShortEmbedder`（返回数量不对）
  验证校验逻辑。
- **云端后端**：注入 fake client，验证空输入、按 `index` 排序、模型名 / dimensions 透传，
  不发任何网络请求。
- **本地后端**：路径解析、缺模型报错、空输入短路不依赖模型；真机推理（1024 维、模长≈1、
  语义近邻排序）用 `skipif` 包住——只有本机检测到模型文件和 onnxruntime 时才跑，CI 上自动跳过。

## 9. 自检（读完本篇应能回答）

1. 为什么"关键词子串"不够？举一个词法召不回但语义能召回的例子。
2. `normalize` 做了哪几件事？为什么中文要额外切 bigram？
3. 第八个 SPI 为什么要和 `ModelProvider` 分开？
4. 为什么内核用标准库 `math` 算余弦，而不是 numpy？
5. 为什么语义通道要做成"独立召回 gate"而不是加权平均里的一项？
6. 多候选时的"边际（margin）"解决什么问题？没有它会发生什么误召回？
7. "两层摘要"分别指什么？为什么这样能支持大规模工具集？
8. 不配置 embedder、或 embedder 运行时报错，系统分别会怎样？
9. 本地 bge-m3 为什么要把推理放进 `asyncio.to_thread`？
10. 为什么 Agnes/DeepSeek 免费档跑不了语义通道？有哪两类免费替代？
