"""host_g 子进程沙箱示例：用**真实子进程**（非 mock）验证隔离与端到端。

全部离线：不依赖网络、不依赖 API Key，只启动本地一次性 Python 子进程。
覆盖：纯计算 / math / print 捕获、import 与 open 与反射被拒、缺 run、
超时被杀，以及 AgentCore 现场 create_code_tool → 沙箱执行的完整链路。
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_g_sandbox import capabilities as cap  # noqa: E402
from host_g_sandbox.sandbox import SubprocessSandbox  # noqa: E402
from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec  # noqa: E402

# worker 对 print 捕获上限 4000 字符，再加一条截断提示，总量应远小于此。
_MAX_STDOUT_WITH_MARKER = 4000 + 32

WEIGHTED_CODE = """
def run(inputs):
    candidates = inputs.get("candidates", [])
    weights = inputs.get("weights", {"skill": 0.6, "experience": 0.4})
    scored = []
    for c in candidates:
        item = dict(c)
        item["score"] = round(
            item["skill"] * weights["skill"]
            + item["experience"] * weights["experience"],
            2,
        )
        scored.append(item)
    return sorted(scored, key=lambda x: x["score"], reverse=True)
"""


# ---------- 沙箱单元（真实子进程） ----------

def test_sandbox_runs_pure_computation_and_math() -> None:
    sb = SubprocessSandbox()
    code = (
        "def run(inputs):\n"
        "    return {'double': inputs['x'] * 2, 'root': round(math.sqrt(16), 1)}"
    )
    r = asyncio.run(sb.execute(code, {"x": 21}))
    assert r.ok, r.error
    assert r.output == {"double": 42, "root": 4.0}


def test_sandbox_captures_print_into_meta() -> None:
    sb = SubprocessSandbox()
    r = asyncio.run(
        sb.execute("def run(inputs):\n    print('hi')\n    return 1", {})
    )
    assert r.ok and r.output == 1
    assert r.meta.get("stdout") == "hi\n"


def test_sandbox_blocks_import() -> None:
    sb = SubprocessSandbox()
    r = asyncio.run(sb.execute("import os\ndef run(inputs):\n    return 1", {}))
    assert not r.ok
    assert "ImportError" in r.error or "import" in r.error.lower()


def test_sandbox_blocks_open() -> None:
    sb = SubprocessSandbox()
    code = "def run(inputs):\n    return open('AGENTS.md').read()[:5]"
    r = asyncio.run(sb.execute(code, {}))
    assert not r.ok and "open" in r.error


def test_sandbox_blocks_reflection() -> None:
    sb = SubprocessSandbox()
    code = "def run(inputs):\n    return getattr(__builtins__, 'x')"
    r = asyncio.run(sb.execute(code, {}))
    assert not r.ok and "getattr" in r.error


def test_sandbox_reports_missing_run() -> None:
    sb = SubprocessSandbox()
    r = asyncio.run(sb.execute("x = 1", {}))
    assert not r.ok and "run" in r.error


def test_sandbox_kills_infinite_loop_on_timeout() -> None:
    sb = SubprocessSandbox(timeout=1.0)
    r = asyncio.run(
        sb.execute(
            "def run(inputs):\n    while True:\n        pass", {}, timeout=1.5
        )
    )
    assert not r.ok and "超时" in r.error
    # 确实等满了超时窗口才返回，说明子进程是被超时机制终止的。
    assert r.meta.get("elapsed_ms", 0) >= 1000


def test_sandbox_rejects_oversized_output() -> None:
    sb = SubprocessSandbox()
    # 返回 70KB 字符串，超过 worker 64KB 的结果上限。
    r = asyncio.run(sb.execute("def run(inputs):\n    return 'x' * 70000", {}))
    assert not r.ok
    assert "结果过大" in r.error


def test_sandbox_caps_unbounded_print() -> None:
    sb = SubprocessSandbox()
    code = (
        "def run(inputs):\n"
        "    for i in range(100000):\n"
        "        print('line', i)\n"
        "    return 1"
    )
    r = asyncio.run(sb.execute(code, {}))
    # 无限打印不应卡死 / 撑爆内存，任务正常完成，stdout 被截断。
    assert r.ok and r.output == 1
    out = r.meta.get("stdout", "")
    assert len(out) <= _MAX_STDOUT_WITH_MARKER
    assert "截断" in out


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="RLIMIT_AS 内存硬限仅在 Unix 可用，Windows 靠超时兜底",
)
def test_sandbox_enforces_memory_limit_on_unix() -> None:
    sb = SubprocessSandbox()
    # 尝试分配 300MB，超过 256MB 的地址空间上限。
    r = asyncio.run(sb.execute("def run(inputs):\n    return 'x' * (300 * 1024 * 1024)", {}))
    assert not r.ok
    assert "内存" in r.error


# ---------- 端到端：AgentCore 现场造工具并在沙箱执行 ----------

class _CreateThenScoreModel:
    """创建 weighted_score → 取候选人 → 调用沙箱工具 → 收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="create_code_tool",
                        arguments={
                            "name": "weighted_score",
                            "description": "按权重给候选人算综合分并排序",
                            "input_schema": {
                                "type": "object",
                                "properties": {
                                    "candidates": {"type": "array"},
                                    "weights": {"type": "object"},
                                },
                                "required": ["candidates"],
                            },
                            "code": WEIGHTED_CODE,
                        },
                    )
                ],
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c2", name="list_candidates", arguments={})
                ],
            )
        if self.calls == 3:
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c3",
                        name="weighted_score",
                        arguments={
                            "candidates": cap.SAMPLE_CANDIDATES,
                            "weights": {"skill": 0.6, "experience": 0.4},
                        },
                    )
                ],
            )
        return ModelResponse(content="排序：林晓 81.6 > 周岚 81.0 > 陈默 80.0。")


def test_end_to_end_create_and_run_in_real_subprocess() -> None:
    core = AgentCore(
        _CreateThenScoreModel(),
        sandbox=SubprocessSandbox(),
        auto_approve_tools=True,
    )
    core.register_tools([build_spec(cap.list_candidates)])

    result = asyncio.run(core.run("列出候选人并按技能0.6经验0.4算综合分排序"))

    assert any(e.type.value == "code_tool_created" for e in result.events)
    calls = [e.data["tool"] for e in result.events if e.type.value == "tool_call"]
    assert calls == ["create_code_tool", "list_candidates", "weighted_score"]

    # 三个工具结果都成功；weighted_score 是真实子进程沙箱算出来的。
    tool_results = [e for e in result.events if e.type.value == "tool_result"]
    assert tool_results and all(e.data["ok"] for e in tool_results)
    scored = tool_results[-1].data.get("preview", "")
    assert "林晓" in scored and "81.6" in scored and "81.0" in scored

    assert "林晓" in result.final_text
    assert core.code_tools_status()["live"] == 1
