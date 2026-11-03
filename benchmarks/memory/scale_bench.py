"""记忆检索规模基准（v0.27 Batch 6）：100 / 1,000 / 10,000 条合成记忆。

测量构建、检索（P50/P95）、更新、删除；这些数字只代表当前 MemoryVault
（JSON 全量 + 词法索引）在本机的量级，不代表生产负载或真实语义质量。

用法::

    python benchmarks/memory/scale_bench.py --sizes 100 1000
    python benchmarks/memory/scale_bench.py --sizes 10000 --queries 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core.memory import MemoryVault  # noqa: E402

#: 大规模合成语料的词表：让每条记忆的词法指纹显著不同，避免被 0.92 去重合并。
_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu 春天 夏天 秋天 冬天 河流 山谷 森林 城市 海洋 星空 沙漠 岛屿 "
    "红色 蓝色 绿色 黄色 紫色 白色 黑色 圆形 方形 三角 桥梁 道路 灯塔 港口 车站"
).split()


def _unique_phrase(index: int, prefix: str) -> str:
    digest = hashlib.sha256(f"{prefix}-{index}".encode()).digest()
    words = [_WORDS[digest[i] % len(_WORDS)] for i in range(8)]
    return f"{prefix} {index} " + " ".join(words) + f" 编号{digest[:6].hex()}"


def _measure(size: int, queries: int) -> dict[str, object]:
    vault = MemoryVault()

    started = time.perf_counter()
    note_ids: list[str] = []
    for index in range(size):
        token = hashlib.sha1(f"scale-{index}".encode()).hexdigest()[:12]
        note, _ = vault.remember(
            _unique_phrase(index, "合成记忆"),
            kind="note",
            tags=[f"tag{index % 13}", token],
        )
        note_ids.append(note.id)
    build_ms = (time.perf_counter() - started) * 1000

    latencies: list[float] = []
    for index in range(queries):
        started = time.perf_counter()
        vault.recall(f"主题编号 {index % 97}", k=5)
        latencies.append((time.perf_counter() - started) * 1000)
    ordered = sorted(latencies)

    update_started = time.perf_counter()
    for note_id in note_ids[: max(1, size // 10)]:
        vault.forget(note_id)
    for index in range(max(1, size // 10)):
        vault.remember(_unique_phrase(index, "更新记忆"))
    update_ms = (time.perf_counter() - update_started) * 1000

    delete_started = time.perf_counter()
    for note_id in note_ids[max(1, size // 10) : max(1, (2 * size) // 10)]:
        vault.forget(note_id)
    delete_ms = (time.perf_counter() - delete_started) * 1000

    return {
        "notes": size,
        "queries": queries,
        "build_ms": round(build_ms, 2),
        "retrieval_p50_ms": round(ordered[len(ordered) // 2], 3),
        "retrieval_p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3),
        "update_ms": round(update_ms, 2),
        "delete_ms": round(delete_ms, 2),
        "stored_notes": len(vault._notes),  # noqa: SLF001 - 基准脚本读取内部计数
        "storage_bytes": (
            vault.storage_path.stat().st_size
            if vault.storage_path is not None and vault.storage_path.exists()
            else 0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MemoryVault 规模基准")
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[100],
        help="默认只跑 100；1000/10000 需显式指定（当前 remember 去重是 O(n^2)，会很慢）",
    )
    parser.add_argument("--queries", type=int, default=20)
    args = parser.parse_args()
    results = []
    for size in args.sizes:
        started = time.perf_counter()
        row = _measure(size, args.queries)
        row["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False))
    print(json.dumps({"results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
