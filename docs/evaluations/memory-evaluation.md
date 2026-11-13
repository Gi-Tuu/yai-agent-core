# 记忆检索评测报告（v0.27 Batch 6）

[English](memory-evaluation.en.md) · **简体中文**

> 数据集：`benchmarks/memory/dataset.py`（114 条记忆 / 60 个 query，中英混合 + 同义 +
> 词法零重叠 + 相似不相关 + 冲突/更新/删除/标签/多用户语义）。
> 评测：`benchmarks/memory/run_bench.py`（Recall@5 / MRR@5 / 无关召回率 / P50 / P95）。
> 环境：Windows 11，CPython 3.13，CPU；假嵌入器与真实 BGE-M3 分开报告。

## 方法修正（相对 v0.26）

- 每个 query 先 **warmup**（默认 1 次，不计入延迟），再重复 **5 次**；
- 延迟用**全部热启动样本**计算 P50/P95（不再取多次里最快的一次）；
- 冷启动与热启动分开计时（`latency.cold_*`）；
- 明确记录 `embedder_kind`（fake/real）与 `device`（当前 CPU）；
- 数据集从 18 条 / 12 query 扩到 **114 条 / 60 query**。

## 结果（2026-11-03，CPU）

| 模式 | Recall@5 | MRR@5 | 无关率 | P50 | P95 | 冷启动 |
|---|---:|---:|---:|---:|---:|---:|
| 纯词法 | 0.938 | 0.897 | 0.277 | 1.36 ms | 7.59 ms | 6.57 ms |
| 假嵌入器双通道 | 0.971 | 0.839 | 0.309 | 1.50 ms | 8.11 ms | — |
| **真实 BGE-M3 双通道** | **0.899** | **0.896** | **0.367** | **28.86 ms** | **38.71 ms** | **3.78 s（模型加载）** |

补充：词法/语义互补性为“仅词法 0 / 仅语义 3 / 都命中 57”；假嵌入器下语义通道
多找回 3 条词法零重合 query（`dark mode preference`、`mango allergy`、`budget tool`）。
嵌入器故障时结果与纯词法完全一致（降级路径正确）。
索引更新/删除后与重建库一致（`index_consistency=True`）。

## 规模测试（`benchmarks/memory/scale_bench.py`）

合成记忆、纯词法。v0.31 起支持**批量写入**（`remember_many()` / `batch()`
把 N 次全量落盘合并成 1 次）与**增量倒排索引**（写入不再整表重建），数字如下：

| 记忆数 | 构建（批量） | 检索 P50 | 检索 P95 | 更新（10%） | 实际入库 |
|---:|---:|---:|---:|---:|---:|
| 100 | 33 ms | 0.2 ms | 0.3 ms | 4 ms | 90 |
| 1,000 | 0.59 s | 1.0 ms | 43 ms | 76 ms | 900 |
| 5,000 | 10.8 s | 7.4 ms | 106 ms | 1.58 s | 4,497 |
| 10,000 | **45.4 s** | 17.6 ms | 116 ms | 6.44 s | 8,989 |

**结论与边界**：

- 10,000 条**已跑通**（批量写入 + 增量倒排）；检索 P50 17.6 ms，构建 45.4 s 属"一次性
  建库"量级，仍不适合交互级批量导入；逐条 `remember`（每条全量落盘）在 10k 规模下依然很慢。
- 这不是“检索质量差”，而是**存储/索引实现**的规模边界；生产大规模场景应使用
  `SqliteStore` 或宿主自己的向量库，不应把本基准的 100 条满分宣传为生产检索质量。
- 假嵌入器只证明通道接线；真实 BGE-M3 的 Recall@5 0.899 / P50 28.9ms 才是本机参考。

## LoCoMo 风格标准基准（`benchmarks/memory/locomo_bench.py`，v0.40）

上游数据集 [LoCoMo](https://github.com/snap-research/locomo)：10 段多会话对话 / 5,882 轮 /
1,986 QA（category 1 多跳、2 时序、3 开放域、4 单跳、5 对抗）。**不随仓库分发**：脚本
`--download` 从上游拉取并校验大小 / SHA-256（2,805,274 bytes / `79fa87e9…698ff4`）。

口径：每段对话独立建库，每轮一条 note（`[dia_id] speaker: text`，`tags=[dia_id]`）；
关闭去重（基准要保证每个证据 id 可检索）；只跑同步词法召回（无 embedding / LLM / 网络）。
1,973 / 1,986 QA 可评估（13 条证据缺失跳过）。

| 指标 | 值 |
|---|---:|
| Recall@5 | **0.3482** |
| Recall@10 | **0.4163** |
| MRR | **0.2927** |

| 类别 | n | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|
| 1 多跳 | 278 | 0.142 | 0.191 | 0.230 |
| 2 时序 | 320 | 0.434 | 0.526 | 0.355 |
| 3 开放域 | 89 | 0.139 | 0.167 | 0.161 |
| 4 单跳 | 840 | 0.413 | 0.475 | 0.326 |
| 5 对抗 | 446 | 0.335 | 0.417 | 0.251 |

**诚实边界**：这是**纯词法**基线，不是 LoCoMo 排行榜意义上的语义记忆系统；与 Mem0 / Zep 等
（embedding + LLM 判定）的数字**不可直接比较**。它回答的是"不接 embedder 时，MemoryVault 的
词法召回在标准多会话数据上是什么水平"，用于后续加 embedder / 改索引时做回归对照。
本机 CPU 上 ingest 30ms、retrieve 2.3s；数据集许可为上游 NOASSERTION，再分发前需自行核对。

## 复现

```powershell
.\.venv\Scripts\python.exe benchmarks\memory\run_bench.py
.\.venv\Scripts\python.exe benchmarks\memory\run_bench.py --real-embed
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 100
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 1000 --queries 20
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 10000 --queries 10
.\.venv\Scripts\python.exe benchmarks\memory\locomo_bench.py --download --json
```
