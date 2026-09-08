"""在线 API 启动示例（X-Agent 部署形态）。

启动：
    uvicorn scripts.serve_example:app --host 0.0.0.0 --port 8000
环境变量：
    OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL   真实模型（缺省走离线演示模型）
    YAI_GIT_COMMIT=<40位 commit>  YAI_PROJECT_SLUG=<slug>   X-Agent 验证端点
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from yai_core import AgentCore  # noqa: E402
from yai_core.batteries.fastapi_server import create_app  # noqa: E402
from host_a_notes import capabilities  # noqa: E402

if os.getenv("OPENAI_API_KEY"):
    from yai_core import OpenAICompatProvider  # noqa: E402

    _model = OpenAICompatProvider()
else:
    from demo_model import OfflineScriptedModel  # noqa: E402

    _model = OfflineScriptedModel()

core = AgentCore.auto(capabilities, _model)
app = create_app(core)
