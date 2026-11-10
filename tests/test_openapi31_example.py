"""可直接运行的 OpenAPI 3.1 示例：注册真实工具，但不发送任何 HTTP 请求。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")

from examples.host_n_openapi31 import run as example  # noqa: E402


def assert_schemas(output: str) -> None:
    report = json.loads(output)
    assert report["notes"] == []
    assert len(report["tools"]) == 1
    tool = report["tools"][0]
    assert tool["name"] == "openapi31_list_items"
    properties = tool["input_schema"]["properties"]
    assert properties["note"]["type"] == ["string", "null"]
    assert properties["mode"]["enum"] == ["preview"]
    assert "const" not in properties["mode"]
    assert properties["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 3,
        "description": "At most three items in this demonstration",
    }


def test_example_registers_sanitized_schemas_without_http(monkeypatch, capsys) -> None:
    def fail_send(*args, **kwargs):
        pytest.fail("Registration must not send HTTP requests")

    async def fail_async_send(*args, **kwargs):
        pytest.fail("Registration must not send HTTP requests")

    monkeypatch.setattr(httpx.Client, "send", fail_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", fail_async_send)
    asyncio.run(example.main())
    assert_schemas(capsys.readouterr().out)


def test_example_cli_runs_outside_repository_without_keys(tmp_path: Path) -> None:
    env = os.environ.copy()
    for name in ("OPENAPI_AUTH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(example.__file__).resolve())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert_schemas(completed.stdout)
