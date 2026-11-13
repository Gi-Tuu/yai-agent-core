"""LoCoMo 风格的长期记忆检索基准（离线、纯词法通道）。

数据集来自 ``snap-research/locomo`` 的 ``data/locomo10.json``（10 段多会话对话、
1,986 个 QA），**不随仓库分发**；用 ``--download`` 从上游拉取，脚本会校验大小与
SHA-256。上游仓库声明为 NOASSERTION，再分发前请先核对许可。

口径（刻意保守，不与 Mem0 等厂商分数混比）：

- 每段对话独立建库，每个对话轮次是一条 note（``[D1:1] speaker: text``），
  ``tags=[dia_id]`` 让检索结果能映射回证据；
- **关闭去重**：基准要保证每个轮次都可被证据 id 命中的；去重质量是另一个问题；
- 只跑 MemoryVault 的同步词法召回（无 embedding、无 LLM 判断、无网络），
  指标为 ``Recall@k``（命中证据比例）与 ``MRR``（首个证据的倒数排名）；
- 证据为空或 dia_id 不存在于对话中的 QA 跳过并计数。

用法::

    python benchmarks/memory/locomo_bench.py --download --json
    python benchmarks/memory/locomo_bench.py --data locomo10.json --k 5 10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core.memory import MemoryNote, MemoryVault  # noqa: E402

LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
LOCOMO_SIZE = 2_805_274
LOCOMO_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
SESSION_RE = re.compile(r"^session_(\d+)$")
DIA_RE = re.compile(r"\[(D\d+:\d+)\]")


class _NoDedupeVault(MemoryVault):
    """基准专用：关闭去重，保证每个对话轮次都能被证据 id 检索到。"""

    def _find_duplicate(self, content: str, tags: list[str]) -> MemoryNote | None:  # noqa: ARG002
        return None


def load_dataset(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("LoCoMo 数据集必须是非空 JSON 数组")
    return data


def verify_dataset(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "path": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def download_dataset(path: Path) -> None:
    """从上游下载 LoCoMo（仅显式 ``--download`` 时调用；不做隐式网络请求）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(LOCOMO_URL, timeout=120) as response:  # noqa: S310 - 固定上游 URL
        payload = response.read()
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) != LOCOMO_SIZE or digest != LOCOMO_SHA256:
        raise RuntimeError(
            "LoCoMo 数据集校验失败：上游文件已变化；请检查后再更新脚本中的大小/哈希"
        )
    path.write_bytes(payload)


def session_turns(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for key, value in conversation.items():
        if not SESSION_RE.match(key) or not isinstance(value, list):
            continue
        turns.extend(turn for turn in value if isinstance(turn, dict) and turn.get("dia_id"))
    return turns


def build_vault(turns: list[dict[str, Any]]) -> MemoryVault:
    vault = _NoDedupeVault()
    vault.remember_many(
        [
            {
                "content": f"[{turn['dia_id']}] {turn.get('speaker', '')}: {turn.get('text', '')}",
                "kind": "note",
                "tags": [str(turn["dia_id"])],
            }
            for turn in turns
        ]
    )
    return vault


def retrieved_ids(rows: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        tags = row.get("tags") or []
        if tags:
            ids.append(str(tags[0]))
            continue
        match = DIA_RE.search(str(row.get("content", "")))
        ids.append(match.group(1) if match else "")
    return ids


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def evaluate(
    dataset: list[dict[str, Any]],
    *,
    ks: tuple[int, ...] = (5, 10),
    max_samples: int | None = None,
) -> dict[str, Any]:
    samples = dataset[:max_samples] if max_samples else dataset
    ingest_started = time.perf_counter()
    total_turns = 0
    per_sample: list[MemoryVault] = []
    turn_ids: list[set[str]] = []
    for sample in samples:
        turns = session_turns(sample.get("conversation") or {})
        total_turns += len(turns)
        vault = build_vault(turns)
        per_sample.append(vault)
        turn_ids.append({str(turn["dia_id"]) for turn in turns})
    ingest_ms = (time.perf_counter() - ingest_started) * 1000

    max_k = max(ks)
    recall: dict[int, list[float]] = {k: [] for k in ks}
    mrr: list[float] = []
    per_category: dict[str, dict[str, list[float]]] = {}
    skipped_missing = 0
    qa_total = 0
    evaluated = 0
    retrieve_started = time.perf_counter()

    for vault, all_ids, sample in zip(per_sample, turn_ids, samples, strict=True):
        for qa in sample.get("qa") or []:
            qa_total += 1
            evidence = [str(item) for item in (qa.get("evidence") or [])]
            if not evidence or any(item not in all_ids for item in evidence):
                skipped_missing += 1
                continue
            evaluated += 1
            rows = vault.recall(str(qa.get("question") or ""), k=max_k)
            ids = retrieved_ids(rows)
            category = str(qa.get("category"))
            bucket = per_category.setdefault(
                category,
                {
                    **{f"recall@{k}": [] for k in ks},
                    "mrr": [],
                },
            )
            for k in ks:
                hit = len(set(ids[:k]) & set(evidence)) / len(evidence)
                recall[k].append(hit)
                bucket[f"recall@{k}"].append(hit)
            rank = next((index + 1 for index, item in enumerate(ids) if item in evidence), None)
            value = 1.0 / rank if rank else 0.0
            mrr.append(value)
            bucket["mrr"].append(value)

    retrieve_ms = (time.perf_counter() - retrieve_started) * 1000
    return {
        "dataset": "locomo10",
        "samples": len(samples),
        "turns": total_turns,
        "qa_total": qa_total,
        "qa_evaluated": evaluated,
        "qa_skipped_missing_evidence": skipped_missing,
        "metrics": {
            **{f"recall@{k}": _mean(values) for k, values in recall.items()},
            "mrr": _mean(mrr),
        },
        "per_category": {
            category: {
                **{f"recall@{k}": _mean(bucket[f"recall@{k}"]) for k in ks},
                "mrr": _mean(bucket["mrr"]),
                "n": len(bucket["mrr"]),
            }
            for category, bucket in sorted(per_category.items())
        },
        "timing_ms": {
            "ingest": round(ingest_ms, 2),
            "retrieve": round(retrieve_ms, 2),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LoCoMo 风格记忆检索基准（纯词法、离线）")
    parser.add_argument("--data", type=Path, default=Path("locomo10.json"))
    parser.add_argument("--download", action="store_true", help="从上游下载 dataset（需要网络）")
    parser.add_argument("--ks", type=int, nargs="+", default=[5, 10])
    parser.add_argument("--samples", type=int, default=None, help="只用前 N 段对话")
    parser.add_argument("--json", action="store_true", help="只打印结果 JSON")
    args = parser.parse_args()

    if args.download and not args.data.exists():
        download_dataset(args.data)
    if not args.data.exists():
        raise SystemExit(
            f"找不到数据集：{args.data}；先运行 --download，或从 {LOCOMO_URL} 手动下载"
        )
    ks = tuple(sorted({k for k in args.ks if k > 0}))
    if not ks:
        raise SystemExit("--ks 必须是正整数")

    dataset = load_dataset(args.data)
    info = verify_dataset(args.data)
    report = evaluate(dataset, ks=ks, max_samples=args.samples)
    report["dataset_info"] = info

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(
        f"LoCoMo：{report['samples']} 段对话 / {report['turns']} 轮 / "
        f"{report['qa_evaluated']}/{report['qa_total']} QA 可评估"
        f"（跳过 {report['qa_skipped_missing_evidence']} 条证据缺失）"
    )
    print(
        "指标："
        + " · ".join(f"Recall@{k}={report['metrics'][f'recall@{k}']:.4f}" for k in ks)
        + f" · MRR={report['metrics']['mrr']:.4f}"
    )
    for category, bucket in report["per_category"].items():
        print(
            f"  category {category}: n={bucket['n']} "
            + " ".join(f"R@{k}={bucket[f'recall@{k}']:.3f}" for k in ks)
            + f" MRR={bucket['mrr']:.3f}"
        )
    print(
        f"耗时：ingest {report['timing_ms']['ingest']}ms · "
        f"retrieve {report['timing_ms']['retrieve']}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
