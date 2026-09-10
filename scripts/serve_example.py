"""在线 API 启动示例（X-Agent 部署形态）。

本地直跑（宿主机统一用 8001，容器内才是 8000）：
    uv run uvicorn scripts.serve_example:app --host 127.0.0.1 --port 8001
容器化：
    docker compose up --build   # 容器内监听 8000，宿主机映射 8001:8000
环境变量（写在项目根 .env 即可，脚本启动时自动加载）：
    OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL   真实模型（缺省走离线演示模型）
    YAI_GIT_COMMIT=<40位 commit>  YAI_PROJECT_SLUG=<slug>   X-Agent 验证端点
    commit 解析顺序：YAI_GIT_COMMIT → 平台注入（如 RENDER_GIT_COMMIT）→ 当前 git HEAD → dev
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_a_notes import capabilities  # noqa: E402
from runner_common import build_model, load_dotenv  # noqa: E402
from yai_core import AgentCore  # noqa: E402
from yai_core.batteries.fastapi_server import create_app  # noqa: E402


def _git_commit() -> str:
    """读取当前 git HEAD 的完整 40 位 commit；非 git 环境回退 dev。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "dev"


load_dotenv()
# commit 解析链：显式 YAI_GIT_COMMIT > 托管平台注入的 commit（Render 为 RENDER_GIT_COMMIT）
# > 当前 git HEAD（本地联调）> dev。保证验证端点始终能反映实际运行的代码版本。
if not os.environ.get("YAI_GIT_COMMIT"):
    os.environ["YAI_GIT_COMMIT"] = (
        os.environ.get("RENDER_GIT_COMMIT") or _git_commit()
    )

_model, _backend = build_model()
print(f"serve_example 后端：{_backend}")
print(f"验证端点 commit：{os.environ['YAI_GIT_COMMIT']}")

core = AgentCore.auto(capabilities, _model)
app = create_app(core)
