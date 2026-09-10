"""在线 API 启动示例（X-Agent 部署形态）。

本地直跑（宿主机统一用 8001，容器内才是 8000）：
    uv run uvicorn scripts.serve_example:app --host 127.0.0.1 --port 8001
容器化：
    docker compose up --build   # 容器内监听 8000，宿主机映射 8001:8000
环境变量（写在项目根 .env 即可，脚本启动时自动加载）：
    OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL   真实模型（缺省走离线演示模型）
    YAI_GIT_COMMIT=<40位 commit>  YAI_PROJECT_SLUG=<slug>   X-Agent 验证端点
    commit 解析顺序：YAI_GIT_COMMIT → 平台注入（如 RENDER_GIT_COMMIT）→ 当前 git HEAD → dev
    （只接受完整 40 位哈希，镜像默认值 dev 等占位会被跳过）
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

_SHA40 = re.compile(r"[0-9a-f]{40}")

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


def _valid_sha(value: str | None) -> str | None:
    """只接受完整 40 位小写哈希；dev、空串、短 SHA 等占位值一律视为无效。"""
    if value and _SHA40.fullmatch(value.strip()):
        return value.strip()
    return None


load_dotenv()
# commit 解析链：YAI_GIT_COMMIT（构建期固化）→ 平台注入（Render 为 RENDER_GIT_COMMIT）
# → 当前 git HEAD（本地联调）→ dev。
# 注意镜像 ENV 里烤着默认值 dev（非合法哈希），必须跳过它，平台注入的真实 SHA 才能生效。
os.environ["YAI_GIT_COMMIT"] = (
    _valid_sha(os.environ.get("YAI_GIT_COMMIT"))
    or _valid_sha(os.environ.get("RENDER_GIT_COMMIT"))
    or _git_commit()
)

_model, _backend = build_model()
print(f"serve_example 后端：{_backend}")
print(f"验证端点 commit：{os.environ['YAI_GIT_COMMIT']}")

core = AgentCore.auto(capabilities, _model)


@contextlib.asynccontextmanager
async def mcp_lifespan(_app):
    """启动时按环境变量挂载外部 MCP Server（如 DeepWiki 公共端点）；关闭时断开。

    挂载失败（没装 mcp extra / 外部 Server 不可达）只告警、不阻断启动：
    本地 native 工具与验证端点必须始终可用。
    """
    bridges = []
    try:
        from yai_core.integrations.mcp import attach_mcp_tools, config_from_env

        cfg = config_from_env(alias="remote")
        if cfg is not None:
            try:
                bridge = await attach_mcp_tools(core.registry, cfg)
                bridges.append(bridge)
                names = [s.name for s in bridge.specs]
                print(f"MCP 已挂载（{cfg.alias}）：{names}")
            except Exception as exc:  # noqa: BLE001 - 外部依赖不可用是运行时常态
                print(f"[警告] 外部 MCP 挂载失败，仅提供本地工具：{type(exc).__name__}: {exc}")
        yield
    finally:
        for bridge in bridges:
            with contextlib.suppress(Exception):
                await bridge.aclose()


app = create_app(core, lifespan=mcp_lifespan)
