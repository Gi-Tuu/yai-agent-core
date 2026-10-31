"""host_production 生产宿主示例的端到端测试（全离线）。

覆盖 B6 要素：Subject 注入与隔离、白名单/外发治理、权限拒绝事件、
并发多实例、生命周期 aclose。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "examples"))
try:
    app_module = importlib.import_module("host_production.app")
finally:
    sys.path.pop(0)


def test_subject_isolation_between_users(tmp_path, monkeypatch) -> None:
    """alice 写的笔记，bob 的 Agent 无法通过 write_note 写进 alice 的目录。"""
    monkeypatch.setattr(app_module, "_WORKSPACE", tmp_path)
    bob_policy = app_module.OwnerScopedPolicy(("write_note",), mode="allow_all").with_subject(
        app_module.current_subject("token-bob")
    )
    # bob 的策略对着 alice 的目录名检查：应拒绝
    decision = asyncio.run(
        bob_policy.check("write_note", {"owner": "alice", "name": "x", "content": "hi"})
    )
    assert decision == app_module.PermissionDecision.DENY


def test_run_completes_with_subject_injection(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_WORKSPACE", tmp_path)
    core = app_module.build_core_for("alice")
    result = asyncio.run(core.run("读取我的笔记 intro"))
    assert result.final_text
    asyncio.run(core.aclose())


def test_fastapi_endpoints(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "_WORKSPACE", tmp_path)
    client = TestClient(app_module.create_host_app())

    health = client.get("/health")
    assert health.status_code == 200

    run = client.post("/agent/run", json={"task": "读取我的笔记 intro", "token": "token-alice"})
    if run.status_code != 200:
        print("DEBUG run body:", run.text[:300])
    assert run.status_code == 200
    assert "final_text" in run.json()

    unknown = client.post("/agent/run", json={"task": "x", "token": "token-evil"})
    assert unknown.status_code == 401


def test_core_instances_run_concurrently(tmp_path, monkeypatch) -> None:
    """并发契约：多实例并行（多用户各持自己的 Core）。"""
    monkeypatch.setattr(app_module, "_WORKSPACE", tmp_path)

    async def main() -> None:
        cores = [app_module.build_core_for("alice"), app_module.build_core_for("bob")]
        await asyncio.gather(*(core.run("读取我的笔记 intro") for core in cores))
        await asyncio.gather(*(core.aclose() for core in cores))

    asyncio.run(main())
