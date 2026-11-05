# 记忆检索评测报告（v0.27 Batch 6）

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

合成记忆、纯词法；`remember` 内部仍做去重（O(n²)），因此数字反映当前实现边界：

| 记忆数 | 构建 | 检索 P50 | 检索 P95 | 更新（10%） | 实际入库 |
|---:|---:|---:|---:|---:|---:|
| 100 | 776 ms | 8.5 ms | 14.7 ms | 147 ms | 90 |
| 1,000 | 79.8 s | 86.6 ms | 152.5 ms | 14.95 s | 900 |
| 10,000 | **未跑通**（按 O(n²) 外推约 2 小时；不适合在 CI/本机冒烟） | — | — | — | — |

**结论与边界**：

- 当前 `MemoryVault` 适合小规模（约 100–1,000 条）个人长期记忆；**不适合 10,000 条
  规模**，主要瓶颈是 `remember()` 的 O(n²) 去重与全量 JSON 重写。
- 这不是“检索质量差”，而是**存储/索引实现**的规模边界；生产大规模场景应使用
  `SqliteStore` 或宿主自己的向量库，不应把本基准的 100 条满分宣传为生产检索质量。
- 假嵌入器只证明通道接线；真实 BGE-M3 的 Recall@5 0.899 / P50 28.9ms 才是本机参考。

## 复现

```powershell
.\.venv\Scripts\python.exe benchmarks\memory\run_bench.py
.\.venv\Scripts\python.exe benchmarks\memory\run_bench.py --real-embed
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 100
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 1000 --queries 20
```
