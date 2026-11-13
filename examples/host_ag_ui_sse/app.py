"""AG-UI SSE 宿主示例：POST /agent → text/event-stream，离线脚本模型、零 Key。

运行::

    uv sync --extra server
    uv run uvicorn examples.host_ag_ui_sse.app:app --port 8000

然后用 curl 或任意 AG-UI 前端（CopilotKit / @ag-ui/client）连接 ``/agent``。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from yai_core import AgentCore, ModelResponse, ScriptedModel
from yai_core.integrations.ag_ui import AgUiTranslator, encode_sse

app = FastAPI(title="YAI AG-UI SSE demo")


def _last_user_text(body: dict) -> str:
    """从 AG-UI RunAgentInput 的 messages 中取最后一条用户消息（纯文本）。"""
    for message in reversed(body.get("messages") or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = "".join(parts).strip()
            if text:
                return text
    return "你好"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent")
async def agent(body: dict) -> StreamingResponse:
    thread_id = str(body.get("threadId") or "thread-local")
    run_id = str(body.get("runId") or "run-local")
    task = _last_user_text(body)

    core = AgentCore(ScriptedModel([ModelResponse(content=f"AG-UI demo：收到「{task}」。")]))

    async def events():
        translator = AgUiTranslator(thread_id=thread_id, run_id=run_id)
        for event in translator.start():
            yield encode_sse(event)
        async for agent_event in core.astream(task):
            for event in translator.translate(agent_event):
                yield encode_sse(event)
        for event in translator.finish():
            yield encode_sse(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
