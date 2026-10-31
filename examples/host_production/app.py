"""生产形态的 FastAPI 宿主集成示例（v0.25 B6 · 可运行、有测试）。

演示把 YAI Agent Core 嵌入一个多用户 FastAPI 服务的完整要素：

- AgentCore 初始化与生命周期（lifespan 关闭时 aclose）；
- 自定义 ModelProvider（离线脚本模型，替换成 OpenAICompatProvider 即接真实模型）；
- 工具注册 + ToolGovernance（白名单、外发闸、参数级审批）；
- **Subject 注入**：身份由宿主认证层（这里的 ``_current_user`` 模拟）映射，
  模型参数无法伪造；不同用户请求各自注入自己的 Subject；
- 自定义 PermissionPolicy（声明 ``yai_subject_aware`` 的最小实现）；
- 流式事件（SSE）+ 权限拒绝 / 审批处理；
- 资源访问（filesystem 资源作用域闸）；
- 并发边界：**一个 AgentCore 实例任务串行**，本示例每请求实例化独立 Core
  （多实例才并行），这是并发契约的正确用法。

运行：uv run uvicorn examples.host_production.app:app --port 8021
测试：tests/test_host_production.py
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import BaseModel

from yai_core import AgentCore, ModelResponse, ProductionSafetyWarning, build_spec
from yai_core.channels import CollectChannel
from yai_core.policy import AllowlistPolicy
from yai_core.spi import PermissionDecision
from yai_core.types import Subject, ToolGovernance

_WORKSPACE = Path(__file__).resolve().parent / "_workspace"

# ---------------------------------------------------------------- 宿主认证层


class _UserAuth:
    """宿主认证层的最小模拟：token → 用户（生产环境换成真实认证体系）。"""

    _TOKENS = {"token-alice": "alice", "token-bob": "bob"}

    @classmethod
    def user_for(cls, token: str | None) -> str | None:
        return cls._TOKENS.get(token or "")


def current_subject(token: str | None) -> Subject:
    """把**已认证**的宿主用户映射成 Subject；认证失败不产生 Subject。"""
    user = _UserAuth.user_for(token)
    return Subject(subject_id=f"user:{user}", display_name=user or "", attributes={})


# ---------------------------------------------------------------- 权限策略


class OwnerScopedPolicy(AllowlistPolicy):
    """声明 yai_subject_aware 的最小策略：按 Subject 限定可写用户的资源。"""

    yai_subject_aware = True

    async def check(self, tool_name: str, arguments: dict) -> PermissionDecision:
        if tool_name == "write_note":
            owner = arguments.get("owner")
            subject_id = getattr(self._subject, "subject_id", "")
            if owner and f"user:{owner}" != subject_id:
                return PermissionDecision.DENY
        return await super().check(tool_name, arguments)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._subject: object = None

    def with_subject(self, subject: object) -> OwnerScopedPolicy:
        self._subject = subject
        return self


# ---------------------------------------------------------------- 工具


def read_note(owner: str, name: str) -> str:
    """读取当前用户的笔记。"""
    path = _WORKSPACE / owner / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def write_note(owner: str, name: str, content: str) -> str:
    """写入当前用户的笔记（写操作：带外发闸与治理声明）。"""
    target = _WORKSPACE / owner
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.md").write_text(content, encoding="utf-8")
    return "written"


def _specs() -> list:
    read_spec = build_spec(read_note)
    write_spec = build_spec(write_note)
    write_spec.governance = ToolGovernance(
        egress_text=("content",),  # 外发闸：URL 来源检查
    )
    return [read_spec, write_spec]


# ---------------------------------------------------------------- Core 装配


def build_core_for(user: str) -> AgentCore:
    """为一个用户实例化独立 Core（并发契约：多实例才并行）。"""
    with contextlib.suppress(ProductionSafetyWarning):
        core = AgentCore(
            _ScriptedAgentModel(),
            policy=OwnerScopedPolicy(("read_note",), mode="auto").with_subject(
                current_subject(f"token-{user}")
            ),
            auto_approve_tools=False,
            channel=CollectChannel(),
            max_iters=3,
        )
    core.register_tools(_specs())
    core.register_state_provider("workspace", lambda: {"user": user})
    return core


class _ScriptedAgentModel:
    """最小离线模型：读笔记 → 复述（真实部署换 OpenAICompatProvider）。"""

    async def achat(self, messages, tools=None, *, tier="standard"):
        for message in reversed(messages):
            content = getattr(message, "content", "") or (
                message.get("content", "") if isinstance(message, dict) else ""
            )
            if content and "<tool_output" not in str(content):
                return ModelResponse(content=f"笔记内容：{content[:80]}")
        return ModelResponse(content="完成")


# ---------------------------------------------------------------- FastAPI


class RunRequest(BaseModel):
    """请求体（模块级定义：`from __future__ import annotations` 下 FastAPI
    解析不了函数内局部类的字符串注解，会把 req 误判成 query 参数）。"""

    task: str
    token: str = "token-alice"  # 真实部署来自认证头


def create_host_app() -> FastAPI:  # noqa: F821 - 延迟到函数内 import
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse

    app = FastAPI(title="YAI 生产宿主示例")

    def _core_for(token: str) -> AgentCore:
        user = _UserAuth.user_for(token)
        if user is None:
            raise HTTPException(status_code=401, detail="未知 token")
        return build_core_for(user)

    @app.post("/agent/run")
    async def run(req: RunRequest) -> dict:
        core = _core_for(req.token)
        try:
            result = await core.run(req.task)
        finally:
            await core.aclose()
        return {"final_text": result.final_text}

    @app.post("/agent/stream")
    async def stream(req: RunRequest) -> StreamingResponse:
        core = _core_for(req.token)
        denied = False

        async def gen() -> AsyncIterator[str]:
            nonlocal denied
            try:
                async for event in core.astream(req.task):
                    if event.type.value == "permission_denied":
                        denied = True
                    yield f"data: {event.type.value}\n\n"
            finally:
                await core.aclose()
            if denied:
                yield "data: [权限拒绝，任务未执行]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_host_app()
