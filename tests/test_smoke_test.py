"""两宿主冒烟门禁：离线模型、示例数据及猴子补丁，不读取 .env。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from functools import wraps
from pathlib import Path

import pytest
from scripts import smoke_test

from yai_core import AgentEvent, EventType


@pytest.fixture
def closed_cores(monkeypatch):
    closed = []
    original = smoke_test.AgentCore.aclose

    async def aclose(core):
        await original(core)
        closed.append(core)

    monkeypatch.setattr(smoke_test.AgentCore, "aclose", aclose)
    return closed


def test_two_offline_hosts_pass_and_close(closed_cores, capsys):
    asyncio.run(smoke_test.main())
    assert len(closed_cores) == 2
    output = capsys.readouterr().out
    assert "count_notes" in output
    assert "get_companion_profile" in output
    assert "冒烟测试通过" in output


def test_all_model_calls_fail(monkeypatch, closed_cores, capsys):
    async def fail(*args, **kwargs):
        raise RuntimeError("offline model failure")

    monkeypatch.setattr(smoke_test.OfflineScriptedModel, "achat", fail)
    with pytest.raises(RuntimeError, match="冒烟失败: error"):
        asyncio.run(smoke_test.main())
    assert len(closed_cores) == 1
    assert "冒烟测试通过" not in capsys.readouterr().out


def test_real_tool_failure(monkeypatch, closed_cores, capsys):
    @wraps(smoke_test.notes_app.count_notes)
    def fail() -> int:
        raise RuntimeError("offline tool failure")

    monkeypatch.setattr(smoke_test.notes_app, "count_notes", fail)
    with pytest.raises(RuntimeError, match="冒烟失败: tool_result"):
        asyncio.run(smoke_test.main())
    assert len(closed_cores) == 1
    assert "冒烟测试通过" not in capsys.readouterr().out


def result(ok=True, tool="business"):
    return AgentEvent(EventType.TOOL_RESULT, {"tool": tool, "ok": ok})


def done(**data):
    return AgentEvent(EventType.DONE, data)


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([], "缺少 DONE"),
        ([result()], "缺少 DONE"),
        ([done()], "没有成功的宿主业务工具调用"),
        ([result(tool="internal"), done()], "没有成功的宿主业务工具调用"),
        ([result(), result(False), done()], "tool_result"),
        ([result(), AgentEvent(EventType.TOOL_RESULT, {"tool": "business"}), done()],
         "tool_result"),
        ([result(), AgentEvent(EventType.ERROR, {"error": "failed"}), done()], "error"),
        ([result(), AgentEvent(EventType.BUDGET_EXCEEDED, {}), done()], "budget_exceeded"),
        ([result(), done(reason="budget_exceeded")], "budget_exceeded"),
        # 即使 DONE 后才出现失败也不能通过。
        ([result(), done(), AgentEvent(EventType.ERROR, {})], "error"),
    ],
)
def test_invalid_traces_fail_and_close(monkeypatch, capsys, events, message):
    class FakeCore:
        closed = False

        def list_tools(self):
            return [{"name": "business"}]

        async def astream(self, task):
            for event in events:
                yield event

        async def aclose(self):
            self.closed = True

    core = FakeCore()
    monkeypatch.setattr(smoke_test.AgentCore, "auto", lambda *args: core)
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(smoke_test.main())
    assert core.closed
    assert "冒烟测试通过" not in capsys.readouterr().out


def test_stream_exception_still_closes(monkeypatch, closed_cores):
    async def fail(core, task):
        raise RuntimeError("stream failure")
        yield  # 保持异步生成器契约。

    monkeypatch.setattr(smoke_test.AgentCore, "astream", fail)
    with pytest.raises(RuntimeError, match="stream failure"):
        asyncio.run(smoke_test.main())
    assert len(closed_cores) == 1


def test_cli_model_failure_exits_nonzero():
    root = Path(__file__).resolve().parents[1]
    # 子进程内替换离线模型后执行真实 CLI 入口；不创建临时文件或访问网络。
    code = """
import runpy
from scripts import smoke_test
async def fail(*args, **kwargs):
    raise RuntimeError("offline model failure")
smoke_test.OfflineScriptedModel.achat = fail
runpy.run_path(str(smoke_test.ROOT / "scripts" / "smoke_test.py"), run_name="__main__")
"""
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode != 0
    assert "冒烟失败: error" in completed.stderr
    assert "冒烟测试通过" not in completed.stdout
