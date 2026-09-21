"""ToolSandbox SPI 契约测试：本版本只定义契约、内核不实现。

确保：可导入、runtime_checkable 能识别宿主实现、结果类型默认值正确。
"""

import asyncio

from yai_core import SandboxResult, ToolSandbox


class FakeSandbox:
    """宿主侧最小实现：一个只做加法的隔离环境（测试替身）。"""

    async def execute(self, code, inputs, *, timeout=None):
        # 测试替身不真正执行代码，只回显契约要求的结果结构。
        return SandboxResult(ok=True, output=inputs, meta={"timeout": timeout})


def test_sandbox_protocol_recognizes_host_implementation() -> None:
    sandbox = FakeSandbox()
    assert isinstance(sandbox, ToolSandbox)


def test_sandbox_result_defaults() -> None:
    ok = SandboxResult(ok=True, output=42)
    assert ok.output == 42
    assert ok.error is None
    assert ok.meta == {}

    fail = SandboxResult(ok=False, error="超时")
    assert fail.ok is False
    assert fail.error == "超时"


def test_fake_sandbox_execute_roundtrip() -> None:
    sandbox = FakeSandbox()
    result = asyncio.run(
        sandbox.execute("return inputs['x'] + 1", {"x": 1}, timeout=2.0)
    )
    assert result.ok
    assert result.output == {"x": 1}
    assert result.meta["timeout"] == 2.0
