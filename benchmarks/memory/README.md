# 记忆质量基准（v0.26 Batch 6 / v0.27 扩量）

小型离线评测集，用来回答"记忆召回到底好不好、语义通道值不值、坏了会不会拖垮主流程"。

## 运行

```bash
python benchmarks/memory/run_bench.py             # 假嵌入器（离线、确定性）
python benchmarks/memory/run_bench.py --real-embed  # 本地 bge-m3（需 models/bge-m3 + [local-embed]）
python benchmarks/memory/run_bench.py --json        # 机器可读
```

## 指标口径

| 指标 | 定义 | 备注 |
|---|---|---|
| Recall@5 | top-5 命中的相关记忆 / 全部相关记忆，按 query 平均 | 相关集合为空记 0 |
| MRR@5 | 第一个相关结果排名的倒数，按 query 平均 | |
| 无关召回率 | top-5 里未标注为相关的占比 | 标注是**必含集**不是穷尽集；语义通道会带出相关但未标注条目，故该值偏高属预期 |
| P50 / P95 延迟 | 逐 query 调用耗时的中位数 / 95 分位 | v0.27 起按**全部热启动样本**计算，不再取多次里最快一次 |
| 降级一致性 | 嵌入器故障时的排序 == 纯词法 | 语义是增强，失败必须降级 |
| 索引一致性 | 更新 / 删除后的召回 == 按最终内容重建的库 | 守护倒排索引失效 |

## 当前本机结果（2026-11-03，v0.27 扩量后）

数据集：114 条记忆 / 60 个 query / k=5；warmup + 5 次重复、全样本 P50/P95、冷/热分离。
完整报告（含纯词法与降级数字、规模实测）见 [`docs/evaluations/memory-evaluation.md`](../../docs/evaluations/memory-evaluation.md)。

| 模式 | Recall@5 | MRR@5 | P50 | P95 |
|---|---|---|---|---|
| 语义双通道（真实 bge-m3，本机 CPU） | 0.899 | 0.896 | 28.9 ms | 38.7 ms |
| 语义双通道（假嵌入器，确定性） | 0.971 | — | 1.5 ms | — |

- 假嵌入器行的其余指标与完整四模式对照见上方报告；scale_bench.py 实测 1 000 条构建
  79.8s（O(n²)），10 000 条未跑通——当前实现不适合 10k 规模。
- **真实 BGE 数字来自本机 `models/bge-m3`，不随仓库分发、不进 CI；Fake 嵌入器只证明通道
  接线正确，不能当作真实语义效果。**

## 扩量前基线（18 条 / 12 query，v0.26 时点快照）

| 模式 | Recall@5 | MRR@5 | P50 |
|---|---|---|---|
| 纯词法 | 0.750 | 0.750 | 0.16 ms |
| 语义双通道（假嵌入器） | 1.000 | 1.000 | 0.18 ms |
| 语义双通道（真实 bge-m3，本机） | 1.000 | 0.917 | 12.27 ms |
| 嵌入器故障降级 | 0.750 | 0.750 | 0.22 ms |

- 词法零重叠、语义命中：3/12（`dark mode preference` / `mango allergy` / `budget tool`）。
- 降级结果与纯词法完全一致；更新 / 删除后索引一致。

## LoCoMo 风格标准基准（v0.40）

```bash
python benchmarks/memory/locomo_bench.py --download      # 首次从上游下载并校验
python benchmarks/memory/locomo_bench.py --json          # 纯词法 Recall@5/10 + MRR
```

10 段对话 / 5,882 轮 / 1,986 QA；每段独立建库、关闭去重、只跑同步词法召回。
本机结果：Recall@5 **0.3482** / Recall@10 **0.4163** / MRR **0.2927**（1,973 条可评估）。
边界：**纯词法基线**，不可与 Mem0/Zep 等 embedding + LLM 判定的分数直接比较；数据集
不随仓库分发（上游 NOASSERTION 许可）。

## LoCoMo 词法 vs BGE-M3 对照（v0.43）

```bash
python benchmarks/memory/locomo_embedding_bench.py --download --json --workers 8
```

同一 1,973 条 QA：词法 R@5 0.3482 / MRR 0.2927；bge-m3 语义 0.5067 / 0.4081；
RRF 混合 0.5078 / **0.4361**。本机 CPU embed 96.9s、retrieve 3.2s。

## 边界

- 语料与标注规模都很小（18 / 12），只用于回归与方向性判断，不构成"生产级检索质量"结论。
- 真实 BGE 只在本机 CPU 上跑过一次；不同模型文件 / 硬件 / 语言分布会变。
- 不测向量数据库、跨进程并发或大规模记忆延迟（那属于宿主后端选择）。
