# 本地向量模型（bge-m3，不入库）

语义工具发现（`SemanticCatalog` 的语义通道）可选地使用本地 **bge-m3** 向量模型，
CPU 推理、完全离线、免费、1024 维，多语言（中文效果好）。

本目录的模型文件体积约 560MB，**已被 `.gitignore` 忽略，不随仓库分发**。需要本地
语义通道时，把下面两个文件放到本目录：

```
models/bge-m3/
├── tokenizer.json
└── onnx/
    └── model_int8.onnx
```

## 获取方式

- 从 bge-m3 官方发布（BAAI/bge-m3）取得 int8 ONNX 版本：`tokenizer.json` 与
  `onnx/model_int8.onnx`；
- 或从已部署该模型的项目（如 AMBRACE 的 `backend/models/bge-m3`）复制同样两个文件。

## 安装依赖并启用

```bash
uv pip install -e '.[local-embed]'
```

随后即可把本地 embedder 注入语义目录（不传路径时默认查找本目录，也可用环境变量
`EMBEDDING_MODEL_DIR` 覆盖）：

```python
from yai_core import SemanticCatalog
from yai_core.integrations.embedding import LocalBgeEmbedder

catalog = SemanticCatalog(candidates, LocalBgeEmbedder())
```

不安装、不放模型也完全不影响使用：不配置 embedder 时语义通道自动关闭，
工具发现退化为零依赖的词法通道。
