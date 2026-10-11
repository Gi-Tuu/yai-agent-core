"""LocalBgeEmbedder 测试。

- 不依赖模型文件/onnxruntime 的用例：路径解析、缺模型报错、空输入短路；
- 真机用例：仅当本机存在 models/bge-m3 且装了 onnxruntime 时运行，CI 无模型时跳过。
"""

import asyncio
import copy
import importlib.util
import math
import sys
import types
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


def test_missing_model_error_precedes_numpy_import(monkeypatch, tmp_path) -> None:
    # 回归：未装 [local-embed] 的环境（CI）没有 numpy，缺模型时必须报 bge-m3
    # RuntimeError，而不是先在 import numpy 处抛 ModuleNotFoundError。
    monkeypatch.setitem(sys.modules, "numpy", None)
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


def test_default_dir_from_cwd_models(monkeypatch, tmp_path) -> None:
    # 无环境变量时，回退到当前工作目录的 models/bge-m3（需存在 tokenizer.json）。
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    model_dir = tmp_path / "models" / "bge-m3"
    model_dir.mkdir(parents=True)
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert LocalBgeEmbedder().model_dir == str(model_dir)


def test_missing_onnx_raises(monkeypatch, tmp_path) -> None:
    # tokenizer 在、onnx/model_int8.onnx 缺失：报缺模型错误。
    monkeypatch.delenv("EMBEDDING_MODEL_DIR", raising=False)
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "tokenizer.json").write_text("{}", encoding="utf-8")
    emb = LocalBgeEmbedder(str(partial))
    with pytest.raises(RuntimeError, match="bge-m3"):
        asyncio.run(emb.embed_texts(["x"]))


# ---------------------------------------------------------------------------
# 用 mock 的 onnxruntime / tokenizers / numpy 覆盖"加载成功 + 后处理"路径。
#
# 真机模型约 540MB，CI 不下载；下面的用例不依赖任何第三方包，只验证与 AMBRACE
# 一致的关键后处理：取第 0 个 token（CLS）的向量并做 L2 归一化。这是两边向量
# 空间一致、未来可共享模型与缓存的核心逻辑，比简单 pragma: no cover 更有价值。
# ---------------------------------------------------------------------------


class _FakeArray:
    """最小 n 维数组，只实现 LocalBgeEmbedder 用到的运算。"""

    def __init__(self, data) -> None:
        self.data = data

    def astype(self, _dtype):  # noqa: ANN001 - 模拟 ndarray.astype，原样返回
        return self

    def __getitem__(self, idx):
        idxs = idx if isinstance(idx, tuple) else (idx,)
        cur = self.data
        for i in idxs:
            cur = cur[i]
        if isinstance(cur, list):
            return _FakeArray(copy.deepcopy(cur))
        return cur

    def copy(self) -> "_FakeArray":
        return _FakeArray(copy.deepcopy(self.data))

    def __truediv__(self, scalar: float) -> "_FakeArray":
        return _FakeArray([x / scalar for x in self.data])

    def tolist(self):
        return copy.deepcopy(self.data)


def _flatten(x):
    for e in x:
        if isinstance(e, list):
            yield from _flatten(e)
        else:
            yield e


class _FakeEncoding:
    def __init__(self, ids, mask) -> None:
        self.ids = ids
        self.attention_mask = mask


class _FakeTokenizer:
    instances: list["_FakeTokenizer"] = []

    def __init__(self, path: str) -> None:
        self.path = path
        _FakeTokenizer.instances.append(self)

    def encode(self, _text: str) -> _FakeEncoding:
        return _FakeEncoding([101, 202, 103], [1, 1, 1])

    @classmethod
    def from_file(cls, path: str) -> "_FakeTokenizer":
        return cls(path)


class _FakeSession:
    instances: list["_FakeSession"] = []
    feeds: list[dict] = []

    def __init__(self, path: str, providers=None) -> None:
        self.path = path
        self.providers = providers
        _FakeSession.instances.append(self)

    def run(self, _output_names, feed):
        _FakeSession.feeds.append(feed)
        # shape = (batch=1, seq=3, dim=2)。CLS（第 0 个 token）= [3, 4]，
        # 其余 token 是干扰值，用来证明结果确实取 [0, 0] 而不是平均/取末位。
        emb = [[[3.0, 4.0], [99.0, 99.0], [88.0, 88.0]]]
        return [_FakeArray(emb)]


def _make_fake_model_dir(tmp_path) -> Path:
    d = tmp_path / "fake-bge"
    (d / "onnx").mkdir(parents=True)
    (d / "tokenizer.json").write_text("{}", encoding="utf-8")
    (d / "onnx" / "model_int8.onnx").write_bytes(b"fake-onnx")
    return d


def _install_fake_stack(monkeypatch) -> None:
    """注入假的 numpy / tokenizers / onnxruntime，使懒加载 import 全部命中。"""
    np_mod = types.ModuleType("numpy")
    np_mod.int64 = "int64"
    np_mod.float32 = "float32"
    np_mod.array = lambda data, dtype=None: _FakeArray(copy.deepcopy(data))
    np_mod.maximum = staticmethod(lambda a, b: max(a, b))
    linalg = types.ModuleType("numpy.linalg")

    def _norm(v) -> float:
        data = v.data if isinstance(v, _FakeArray) else v
        return math.sqrt(sum(x * x for x in _flatten(data)))

    linalg.norm = _norm
    np_mod.linalg = linalg
    monkeypatch.setitem(sys.modules, "numpy", np_mod)
    monkeypatch.setitem(sys.modules, "numpy.linalg", linalg)

    tok_mod = types.ModuleType("tokenizers")
    tok_mod.Tokenizer = _FakeTokenizer
    monkeypatch.setitem(sys.modules, "tokenizers", tok_mod)

    ort_mod = types.ModuleType("onnxruntime")
    ort_mod.InferenceSession = _FakeSession
    monkeypatch.setitem(sys.modules, "onnxruntime", ort_mod)


def test_mocked_load_cls_pooling_and_l2_normalization(monkeypatch, tmp_path) -> None:
    _install_fake_stack(monkeypatch)
    _FakeTokenizer.instances.clear()
    _FakeSession.instances.clear()
    _FakeSession.feeds.clear()
    model_dir = _make_fake_model_dir(tmp_path)

    emb = LocalBgeEmbedder(str(model_dir))
    vecs = asyncio.run(emb.embed_texts(["abc", "def"]))

    # CLS=[3, 4]，norm=5，归一化后应为 [0.6, 0.8]；两条输入各自独立计算。
    assert len(vecs) == 2
    for v in vecs:
        assert len(v) == 2
        assert math.isclose(v[0], 0.6, abs_tol=1e-9)
        assert math.isclose(v[1], 0.8, abs_tol=1e-9)
        # L2 归一化后模长为 1。
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-9)

    # 模型只加载一次（_load 缓存），但每条输入都跑一次推理。
    assert len(_FakeTokenizer.instances) == 1
    assert len(_FakeSession.instances) == 1
    assert len(_FakeSession.feeds) == 2
    # 默认走 CPU provider；推理喂了 input_ids / attention_mask。
    assert _FakeSession.instances[0].providers == ["CPUExecutionProvider"]
    assert set(_FakeSession.feeds[0]) == {"input_ids", "attention_mask"}


def test_mocked_custom_providers_passthrough(monkeypatch, tmp_path) -> None:
    _install_fake_stack(monkeypatch)
    _FakeSession.instances.clear()
    model_dir = _make_fake_model_dir(tmp_path)

    emb = LocalBgeEmbedder(str(model_dir), providers=["CUDAExecutionProvider"])
    asyncio.run(emb.embed_texts(["x"]))
    assert _FakeSession.instances[0].providers == ["CUDAExecutionProvider"]


def test_mocked_missing_optional_deps_raises_import(monkeypatch, tmp_path) -> None:
    # 模型文件都在，但没装 onnxruntime/tokenizers（sys.modules 置 None 模拟缺失），
    # 应给出安装可选依赖的 ImportError。
    model_dir = _make_fake_model_dir(tmp_path)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    monkeypatch.setitem(sys.modules, "tokenizers", None)
    emb = LocalBgeEmbedder(str(model_dir))
    with pytest.raises(ImportError, match="可选依赖"):
        asyncio.run(emb.embed_texts(["x"]))
