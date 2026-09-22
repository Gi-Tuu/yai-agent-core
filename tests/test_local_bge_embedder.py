"""LocalBgeEmbedder 测试。

- 不依赖模型文件/onnxruntime 的用例：路径解析、缺模型报错、空输入短路；
- 真机用例：仅当本机存在 models/bge-m3 且装了 onnxruntime 时运行，CI 无模型时跳过。
"""

import asyncio
import importlib.util
import math
from pathlib import Path

import pytest

from yai_core.integrations.embedding import LocalBgeEmbedder

_MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / "bge-m3"
_HAS_MODEL = (_MODEL_DIR / "onnx" / "model_int8.onnx").is_file() and (
    _MODEL_DIR / "tokenizer.json"
).is_file()
_HAS_ORT = importlib.util.find_spec("onnxruntime") is not None
_REAL = _HAS_MODEL and _HAS_ORT


def test_default_dir_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(tmp_path))
    assert LocalBgeEmbedder().model_dir == str(tmp_path)


def test_default_dir_none_when_no_env_and_no_cwd_model(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # 该目录下没有 models/bge-m3
    assert LocalBgeEmbedder().model_dir is None


def test_missing_model_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    emb = LocalBgeEmbedder(str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="bge-m3"):
        asyncio.run(emb.embed_texts(["天气"]))


def test_empty_input_short_circuits_without_loading(tmp_path) -> None:
    # 即使模型目录不存在，空输入也直接返回 []，不触发加载/报错。
    emb = LocalBgeEmbedder(str(tmp_path / "nope"))
    assert asyncio.run(emb.embed_texts([])) == []


@pytest.mark.skipif(not _REAL, reason="本机缺少 bge-m3 模型或 onnxruntime，跳过真机推理")
def test_real_embed_dim_and_unit_norm() -> None:
    emb = LocalBgeEmbedder(str(_MODEL_DIR))
    vecs = asyncio.run(emb.embed_texts(["出门该怎么穿、要不要带外套", "查询天气"]))
    assert len(vecs) == 2
    assert all(len(v) == 1024 for v in vecs)
    # L2 归一化后模长应为 1。
    norm = math.sqrt(sum(x * x for x in vecs[0]))
    assert abs(norm - 1.0) < 1e-3


@pytest.mark.skipif(not _REAL, reason="本机缺少 bge-m3 模型或 onnxruntime，跳过真机推理")
def test_real_embed_semantic_neighbors() -> None:
    emb = LocalBgeEmbedder(str(_MODEL_DIR))
    vecs = asyncio.run(
        emb.embed_texts(
            ["出门该怎么穿、要不要带外套", "查询实时天气", "帮我写一首五言律诗"]
        )
    )

    def cos(a, b):
        d = sum(x * y for x, y in zip(a, b, strict=True))
        return d / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    related = cos(vecs[0], vecs[1])
    unrelated = cos(vecs[0], vecs[2])
    # 语义排序正确，且相关项越过语义门槛、显著高于无关项。
    assert related >= 0.45
    assert related > unrelated + 0.1
