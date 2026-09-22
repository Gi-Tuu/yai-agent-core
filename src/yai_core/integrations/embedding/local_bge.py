"""本地 bge-m3 ONNX 嵌入后端（CPU、离线、免费、1024 维）。

与 AMBRACE 的 ``backend/app/memory/embedding.py`` 使用**同一份模型、同一套后处理**
（XLM-R 的 CLS pooling + L2 归一化），因此两者向量空间一致：未来 Core 内嵌进
AMBRACE 时可以直接共享模型文件与向量缓存，不需要做任何向量适配。

设计约束：
- onnxruntime / tokenizers / numpy 全部懒加载，放在 ``[local-embed]`` 可选 extra，
  内核本体仍零第三方硬依赖；
- 模型文件约 540MB（int8 ONNX + tokenizer），**不打包进仓库**，通过 ``model_dir``
  参数或 ``EMBEDDING_MODEL_DIR`` 环境变量指向本地 bge-m3 目录（可直接复用
  AMBRACE 的 ``backend/models/bge-m3``）；
- ONNX 推理是 CPU 密集型，用 ``asyncio.to_thread`` 放到线程池，不阻塞事件循环。

目录需包含：
    <model_dir>/tokenizer.json
    <model_dir>/onnx/model_int8.onnx
"""

from __future__ import annotations

import asyncio
import os

# bge-m3 输出 1024 维（仅用于文档与自检，真实维度以模型输出为准）。
BGE_M3_DIM = 1024
_TOKENIZER_FILE = "tokenizer.json"
_ONNX_FILE = os.path.join("onnx", "model_int8.onnx")


def default_model_dir() -> str | None:
    """解析模型目录：环境变量 EMBEDDING_MODEL_DIR > 当前工作目录的 models/bge-m3。

    演示脚本通常从项目根运行，因此默认能找到项目自带的 ``models/bge-m3``；
    库被安装到其他项目时，用环境变量或显式参数指定更可靠。
    """
    env_path = os.getenv("EMBEDDING_MODEL_DIR", "").strip()
    if env_path:
        return env_path
    cwd_candidate = os.path.join(os.getcwd(), "models", "bge-m3")
    if os.path.isfile(os.path.join(cwd_candidate, _TOKENIZER_FILE)):
        return cwd_candidate
    return None


class LocalBgeEmbedder:
    """``EmbeddingProvider`` 的本地 bge-m3 ONNX 实现（需安装 ``.[local-embed]``）。"""

    def __init__(
        self,
        model_dir: str | None = None,
        *,
        providers: list[str] | None = None,
    ) -> None:
        self.model_dir = model_dir or default_model_dir()
        self.providers = providers or ["CPUExecutionProvider"]
        self._model: tuple[object, object] | None = None

    def _missing_error(self) -> RuntimeError:
        return RuntimeError(
            "未找到本地 bge-m3 模型。请把 model_dir 参数或环境变量 EMBEDDING_MODEL_DIR "
            "指向包含 tokenizer.json 与 onnx/model_int8.onnx 的目录（可直接复用 AMBRACE 的 "
            "backend/models/bge-m3），并安装可选依赖：uv pip install -e '.[local-embed]'"
        )

    def _load(self) -> tuple[object, object]:
        if self._model is not None:
            return self._model
        if not self.model_dir:
            raise self._missing_error()
        tokenizer_path = os.path.join(self.model_dir, _TOKENIZER_FILE)
        onnx_path = os.path.join(self.model_dir, _ONNX_FILE)
        if not os.path.isfile(tokenizer_path) or not os.path.isfile(onnx_path):
            raise self._missing_error()

        try:
            import onnxruntime as ort  # noqa: PLC0415
            from tokenizers import Tokenizer  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "使用 LocalBgeEmbedder 需要安装可选依赖：uv pip install -e '.[local-embed]'"
            ) from exc

        tokenizer = Tokenizer.from_file(tokenizer_path)
        session = ort.InferenceSession(onnx_path, providers=self.providers)
        self._model = (tokenizer, session)
        return self._model

    def _embed_one_sync(self, text: str) -> list[float]:
        # 先完成模型/可选依赖检查：缺模型抛 bge-m3 RuntimeError、缺 onnxruntime/tokenizers
        # 抛安装提示。必须在 import numpy 之前——未装 [local-embed] 的环境（如 CI）没有
        # numpy，若先 import 会抛 ModuleNotFoundError，掩盖真正的"缺模型"错误。
        # numpy 是 onnxruntime 的伴随依赖，_load() 成功后必然可用。
        tokenizer, session = self._load()
        # 与 AMBRACE 完全一致：单条编码 → ONNX → CLS pooling → L2 归一化。
        import numpy as np  # noqa: PLC0415
        enc = tokenizer.encode(text)
        ids = np.array([enc.ids], dtype=np.int64)
        mask = np.array([enc.attention_mask], dtype=np.int64)
        token_emb = session.run(
            None, {"input_ids": ids, "attention_mask": mask}
        )[0].astype(np.float32)
        cls = token_emb[0, 0].copy()
        norm = np.linalg.norm(cls)
        return (cls / np.maximum(norm, 1e-9)).tolist()

    def _embed_batch_sync(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one_sync(text) for text in texts]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 推理放线程池，避免阻塞事件循环（与 AMBRACE 同样的考虑）。
        return await asyncio.to_thread(self._embed_batch_sync, list(texts))
