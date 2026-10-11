# 逐行讲解 30 · 远程发现源按 need 筛选：保守收窄，空命中回退全量

> 对应文件：
> - 词法筛选纯函数：`src/yai_core/discovery/scoring.py`
>   （`spec_text` / `_directed` / `spec_relevance` / `filter_specs_by_need` 与新常量）；
> - 远程发现源双通道：`src/yai_core/discovery/remote_sources.py`
>   （构造加 `embedder`、`discover` 双通道 OR 合并、新增 `_semantic_select`）；
> - 离线测试：`tests/test_remote_discovery.py`（由 5 个扩到 11 个）。
>
> 读前先读第 20（双通道语义发现）、26（远程发现源）。本章是给第 26 篇的远程源
> 补上"按缺口收窄"——此前它把外部工具**全量**返回。

## A. 为什么远程源必须按 need 收窄

第 26 篇的 `RemoteSourceDiscovery` 已经能懒连接 MCP / OpenAPI 源、把外部能力变成
`ToolSpec`。但它最初的 `discover` 只做了两件事：跳过已注册工具（available）、同批
重名去重，然后**把剩下的全部返回**。

这在源里工具少的时候没问题，真实情况却不是这样：

- 一个公共 MCP Server 可能暴露几十个工具，一个 OpenAPI 服务可能有上百个端点；
- 这些工具最终要拼进 system prompt 让模型挑，全塞回去会**爆上下文**，模型也容易在
  一大串工具里选错；
- 用户只是问"明天会下雨吗"，把"删订单""发邮件""季度财报"全端上来是噪声。

进程内的 `SemanticCatalog`（第 20 篇）早就会按缺口筛选，但它吃的是
`DiscoveredCandidate`（候选自带 `keywords`）。远程源构造出来的是 `ToolSpec`，
**没有 keywords 字段**（`ToolSpec` 字段是 name / description / input_schema /
handler / source / steps / code），所以不能直接复用，得有一条"对已构造 ToolSpec
按 need 打分"的通道。

## B. 第一个坑：不能照搬 SemanticCatalog 的阈值

最省事的想法是：拿现成的 `token_similarity` 对 need+task 打个分，套用
`SemanticCatalog` 的阈值 0.45。一跑就发现不对：

- 相关的天气工具只有 **0.17**，够不到 0.45；
- 但它仍是无关订单工具（0.02）的 7 倍。

根因是**量纲不同**。`token_similarity(a, b)` 是单向的、对 `a` 归一：

```python
def token_similarity(a, b, *, groups=DEFAULT_SYNONYM_GROUPS):
    a_uni, a_bi = tokenize(canonicalize(a, groups))
    b_uni, b_bi = tokenize(canonicalize(b, groups))
    if not a_uni:
        return 0.0
    uni = len(a_uni & b_uni) / len(a_uni)          # a 的 token 有多少在 b 里
    if not a_bi:
        return uni
    bi = len(a_bi & b_bi) / len(a_bi)
    return round(0.4 * uni + 0.6 * bi, 4)
```

- `SemanticCatalog` 的 0.45 很大程度靠候选登记的 `keywords` 做**布尔强命中**
  （`keyword_hit` 权重 0.6）撑起来；
- 远程 `ToolSpec` 没有 keywords，只靠 name+description 的 token 重叠；
- 而 query 是 need+task 两句自然语言、token 很多，工具短描述只覆盖其中一小部分，
  对 query 归一时分母被撑得很大，分数被严重稀释。

**结论：相关度阈值不能抄，必须按"纯 name+description、双向"这个口径重新标定。**

## C. 双向取大：让"工具讲的是不是这件事"也投一票

单向"query → 工具"回答的是"我要的东西有多少落在这个工具里"，query 一长就吃亏。
反过来"工具 → query"回答的是另一个问题：**这个工具讲的是不是这件事**——工具描述
短、领域词集中，覆盖率高且稳定，对长短 query 都稳健。

于是先抽出一个有方向的内部函数：

```python
def _directed(a_uni, a_bi, b_uni, b_bi):
    """以 a 为基准算覆盖：a 的 unigram/bigram 有多少在 b 里。"""
    if not a_uni:
        return 0.0
    uni = len(a_uni & b_uni) / len(a_uni)
    if not a_bi:                      # a 没有 bigram（极短）时退化为 unigram
        return uni
    bi = len(a_bi & b_bi) / len(a_bi)
    return 0.4 * uni + 0.6 * bi
```

工具用于匹配的文本，就是"名字 + 描述"拼起来：

```python
def spec_text(spec: ToolSpec) -> str:
    """远程工具用于相关性匹配的轻量文本：工具名 + 描述。"""
    return " ".join(
        part for part in (spec.name or "", spec.description or "") if part
    )
```

相关度把两个方向都算一遍、**取较大者**：

```python
def spec_relevance(need, task, spec, *, groups=DEFAULT_SYNONYM_GROUPS):
    q_uni, q_bi = tokenize(canonicalize(f"{need}\n{task}", groups))
    c_uni, c_bi = tokenize(canonicalize(spec_text(spec), groups))
    # query→工具（长 query 易稀释）与 工具→query（描述短、领域词集中）取大。
    return round(
        max(
            _directed(q_uni, q_bi, c_uni, c_bi),
            _directed(c_uni, c_bi, q_uni, q_bi),
        ),
        4,
    )
```

### 用实测分布标定阈值

拿几组 query / 工具实测双向分：

| query | 天气工具 | 订单工具 | 库存/财务 |
|---|---|---|---|
| 天气（长："出门要不要带伞、会不会下雨"） | **0.229** | 0.024 | 0 |
| 天气（中："明天会不会下雨"） | **0.287** | 0 | 0 |
| 天气（短："明天下雨吗"） | **0.333** | 0 | 0 |
| 订机票（源里没有对应工具） | 0.029 | 0.108 | 0 |

规律很清楚：**相关工具落在 0.23~0.33，无关工具 ≤0.11**。据此定三个常量：

```python
# 双向词法（取 query→工具、工具→query 两方向较大者）实测：相关工具约 0.23~0.33，
# 无关工具 ≤0.11。阈值定 0.20：过阈值且与最高分边际足够（margin）才收窄；一个达标
# 都没有则回退全量。跌破阈值只会导致"不收窄、返回全量"，不会漏召回。
DEFAULT_SPEC_NEED_THRESHOLD = 0.20
DEFAULT_SPEC_MARGIN = 0.08
DEFAULT_REMOTE_TOP_K = 8
```

- **阈值 0.20**：相关的长 query（0.229）勉强过、无关最高（0.108）安全挡住；
- **margin 0.08**：只保留与最高分同档的领先者，防弱相关/噪声混进来（和
  `SemanticCatalog` 的 gate 同款思想，词法噪声更大，margin 放宽到 0.08）；
- **top_k 8**：收窄后仍有数量上限。

## D. 词法保守筛选：空命中一定回退全量

`filter_specs_by_need` 是纯函数，把上面的打分、阈值、margin、top_k 串起来：

```python
def filter_specs_by_need(
    specs, need, *, task,
    threshold=DEFAULT_SPEC_NEED_THRESHOLD,
    top_k=DEFAULT_REMOTE_TOP_K,
    margin=DEFAULT_SPEC_MARGIN,
    groups=DEFAULT_SYNONYM_GROUPS,
):
    if not specs:
        return [], False, {}
    # 1) 逐个打分，记录 (分数, 原始下标, spec)；下标用于同分稳定排序。
    scored = [
        (spec_relevance(need, task, spec, groups=groups), index, spec)
        for index, spec in enumerate(specs)
    ]
    scores = {spec.name: score for score, _, spec in scored}
    # 2) 过阈值才算命中。
    hits = [item for item in scored if item[0] >= threshold]
    if not hits:
        # ★ 一个达标都没有：原样返回全部，narrowed=False（绝不因筛选漏召回）。
        return list(specs), False, scores
    # 3) 命中按 (-分数, 下标) 降序，再用 margin 只留与最高分同档者，取 top_k。
    hits.sort(key=lambda item: (-item[0], item[1]))
    top_score = hits[0][0]
    keep = [item for item in hits if item[0] >= top_score - margin]
    selected = [spec for _, _, spec in keep[:top_k]]
    return selected, True, scores
```

最关键的是 `if not hits: return list(specs), False, scores` 这一行体现的**保守
口径**：

- 收窄是"我有把握这些相关"时才做的动作；
- 一旦没有任何工具过阈值，说明把握不足，**宁可全量返回也不赌**——全量顶多让模型
  多挑几下，漏召回却可能让任务直接做不成；
- 所以"跌破阈值"的后果永远是**安全降级（不收窄）**，而不是"返回空"。

## E. 语义通道：跨概念高召回，失败不致命

词法解决"字面改写"，但"明天下雨吗" vs 描述写成"获取大气湿度与降水概率"时，
词法分仍可能不到 0.2。跨概念的高召回要靠向量。`RemoteSourceDiscovery` 因此加了
一个**可选** embedder（`EmbeddingProvider`，第八个 SPI，方法名是 `embed_texts`）：

```python
async def _semantic_select(self, deduped, need, task):
    """语义 gate：embedder 缺失/失败不致命；top1 过阈值且与次选边际足够才选中。"""
    if self.embedder is None or not deduped:
        return []
    try:
        query_vec = (
            await self.embedder.embed_texts(
                [canonicalize(f"{need}\n{task}", self.synonym_groups)]
            )
        )[0]
        spec_vecs = await self.embedder.embed_texts(
            [
                canonicalize(spec_text(spec), self.synonym_groups)
                for spec in deduped
            ]
        )
    except Exception:  # 语义通道失败退回词法/全量，不致命
        return []
    ranked = sorted(
        (
            (cosine_similarity(query_vec, vec), index, spec)
            for index, (spec, vec) in enumerate(
                zip(deduped, spec_vecs, strict=True)
            )
        ),
        key=lambda item: (-item[0], item[1]),
    )
    if not ranked or ranked[0][0] < DEFAULT_SCORE_THRESHOLD:
        return []
    top = ranked[0][0]
    return [
        spec for score, _, spec in ranked
        if score >= top - DEFAULT_SEMANTIC_MARGIN
    ]
```

要点：

- 复用 `semantic.py` 的余弦与语义 gate 常量（`DEFAULT_SCORE_THRESHOLD=0.45`、
  `DEFAULT_SEMANTIC_MARGIN=0.05`），不另造一套；
- `zip(..., strict=True)`：embedder 契约要求返回与输入等长，不等长就暴露错误；
- 整个编码过程包了 try：**embedder 没装 / 报错 / 超时都不致命**，返回 `[]`，
  交给词法通道和全量兜底。

## F. discover：双通道 OR 合并，都不中才回退全量

`discover` 在去重之后，把词法、语义两通道的结果合并：

```python
if not self.filter_by_need:
    self.last_narrowed = False
    self.last_scores = {}
    return deduped                       # 宿主显式关闭筛选：保持全量行为

lex_selected, lex_narrowed, scores = filter_specs_by_need(   # 词法
    deduped, need, task=task,
    threshold=self.score_threshold, top_k=self.top_k,
    margin=self.score_margin, groups=self.synonym_groups,
)
sem_selected = await self._semantic_select(deduped, need, task)  # 语义

if not lex_narrowed and not sem_selected:
    self.last_narrowed = False
    self.last_scores = scores
    return deduped                       # ★ 两通道都未识别：回退全量

merged, names = [], set()
# 语义命中（已按余弦降序）在前，词法收窄集补充，按 name 去重。
for spec in [*sem_selected, *(lex_selected if lex_narrowed else [])]:
    if spec.name not in names:
        names.add(spec.name)
        merged.append(spec)
self.last_narrowed = True
self.last_scores = scores
return merged
```

注意两个细节：

- 词法回退（`lex_narrowed=False`）时 `lex_selected` 装的是**全量**，这时只能用语义
  结果、不能把全量再拼回去——所以写的是 `lex_selected if lex_narrowed else []`；
- 只有"词法也没中、语义也空"才回退全量；任一通道收窄即收窄，两通道是 **OR（并集）**，
  互相补漏：词法零依赖永远可用，语义跨概念高召回。

构造函数相应增加了 `embedder=None` 与 `score_margin`，并保留 `last_narrowed` /
`last_scores` 两个观测字段，方便宿主调试和测试断言。

## G. 测试与真机口径

`tests/test_remote_discovery.py` 在原 5 个（聚合去重 / 单源失败 / 懒连接一次 /
aclose / 工厂）基础上新增 6 个：

- 词法 4 个：能收窄到相关工具、无相关时回退全量、`filter_by_need=False` 全量、
  `top_k` 限制数量；
- 语义 2 个：`FakeEmbedder` 验证跨概念近义表述也能收窄；`BadEmbedder` 抛错时
  仍能靠词法收窄、不致命。

全部离线、零网络、不需要 API Key，**11 个全过**；全量测试与 ruff、mypy 均绿。

真机 bge-m3 语义通道：本机 `D:\AMBRACE\backend\models\bge-m3` 目前**只有
tokenizer.json、缺 ONNX 权重**，真机端到端待补齐权重后再跑；在此之前，语义通道的
正确性由确定性 `FakeEmbedder` 走完整异步链路保证，余弦计算复用已验证的
`cosine_similarity`。对外不把"语义通道"宣传成已在真机量化验证。

## H. 自检（读完应能回答）

1. 为什么远程源不能直接套 `SemanticCatalog` 的 0.45 阈值？
2. `spec_relevance` 为什么要算两个方向、取 max？各方向回答的是什么问题？
3. `filter_specs_by_need` 在没有任何工具过阈值时返回什么？为什么不能返回空？
4. 词法回退时，`discover` 合并为什么用 `lex_selected if lex_narrowed else []`？
5. embedder 报错时整条发现链为什么还能正常工作？
