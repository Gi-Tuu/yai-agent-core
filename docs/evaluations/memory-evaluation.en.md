> English translation of [中文](memory-evaluation.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Memory Retrieval Evaluation Report (v0.27 Batch 6)

**English** · [简体中文](memory-evaluation.md)

> Dataset: `benchmarks/memory/dataset.py` (114 memories / 60 queries, Chinese–English mixed + synonyms +
> zero lexical overlap + similar-but-irrelevant + conflict/update/delete/tag/multi-user semantics).
> Evaluation: `benchmarks/memory/run_bench.py` (Recall@5 / MRR@5 / irrelevant recall rate / P50 / P95).
> Environment: Windows 11, CPython 3.13, CPU; the fake embedder and the real BGE-M3 are reported separately.

## Methodology fixes (relative to v0.26)

- Each query first gets a **warmup** (1 run by default, not counted toward latency), then is repeated **5 times**;
- Latency P50/P95 is computed over **all warm-start samples** (no longer taking the fastest of several runs);
- Cold start and warm start are timed separately (`latency.cold_*`);
- `embedder_kind` (fake/real) and `device` (currently CPU) are explicitly recorded;
- The dataset was expanded from 18 memories / 12 queries to **114 memories / 60 queries**.

## Results (2026-11-03, CPU)

| Mode | Recall@5 | MRR@5 | Irrelevant rate | P50 | P95 | Cold start |
|---|---:|---:|---:|---:|---:|---:|
| Lexical only | 0.938 | 0.897 | 0.277 | 1.36 ms | 7.59 ms | 6.57 ms |
| Fake embedder, dual-channel | 0.971 | 0.839 | 0.309 | 1.50 ms | 8.11 ms | — |
| **Real BGE-M3, dual-channel** | **0.899** | **0.896** | **0.367** | **28.86 ms** | **38.71 ms** | **3.78 s (model load)** |

Supplement: lexical/semantic complementarity is "lexical only 0 / semantic only 3 / both hit 57"; under the
fake embedder the semantic channel recovers 3 additional queries with zero lexical overlap
(`dark mode preference`, `mango allergy`, `budget tool`).
When the embedder fails, the results are exactly identical to lexical-only (the degradation path is correct).
After index updates/deletes the store matches a rebuilt one (`index_consistency=True`).

## Scale test (`benchmarks/memory/scale_bench.py`)

Synthetic memories, lexical only. Since v0.31 it supports **batch writes** (`remember_many()` / `batch()`
merge N full writes to disk into 1) and an **incremental inverted index** (writes no longer rebuild the whole
table); the numbers are as follows:

| Memories | Build (batch) | Retrieval P50 | Retrieval P95 | Update (10%) | Actually stored |
|---:|---:|---:|---:|---:|---:|
| 100 | 33 ms | 0.2 ms | 0.3 ms | 4 ms | 90 |
| 1,000 | 0.59 s | 1.0 ms | 43 ms | 76 ms | 900 |
| 5,000 | 10.8 s | 7.4 ms | 106 ms | 1.58 s | 4,497 |
| 10,000 | **45.4 s** | 17.6 ms | 116 ms | 6.44 s | 8,989 |

**Conclusions and boundaries**:

- 10,000 memories **now run through** (batch writes + incremental inverted index); retrieval P50 is 17.6 ms,
  and a 45.4 s build is in the "one-off database build" range — still unsuitable for interactive bulk import;
  per-item `remember` (a full write to disk for each item) remains very slow at 10k scale.
- This is not "poor retrieval quality" but the scale boundary of the **storage/index implementation**; for
  large-scale production use `SqliteStore` or the host's own vector store, and do not advertise this benchmark's
  100-item perfect score as production retrieval quality.
- The fake embedder only proves the channel wiring; real BGE-M3's Recall@5 0.899 / P50 28.9 ms is the reference
  for this machine.

## Reproduce

```powershell
.\.venv\Scripts\python.exe benchmarks\memory\run_bench.py
.\.venv\Scripts\python.exe benchmarks\memory\run_bench.py --real-embed
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 100
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 1000 --queries 20
.\.venv\Scripts\python.exe benchmarks\memory\scale_bench.py --sizes 10000 --queries 10
```
