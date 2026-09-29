"""语言中立能力清单（manifest）的批量导入 / 导出与文件 IO。

定位（避免重复造轮子）：
- manifest 把工具能力序列化为 JSON，是跨进程 / 跨语言交换"能力"的统一格式；
- **composite 工作流（steps）与 code 工具（code，需宿主沙箱）是"数据即能力"**，
  导出后在另一个 Core 实例注册即可（在沙箱内）执行；
- native/openapi/mcp 的 ``handler`` 是运行时对象、不可序列化，manifest 只携带
  其能力声明，导入后 ``handler=None``，需绑定后端；跨语言接入这类能力请直接
  走 MCP / OpenAPI 集成（它们自带 handler）。
- 本模块**不另造调用通道**（"manifest + 自有 JSON-RPC"等于简化版 MCP）。

文件格式：

    {"manifest_version": 1, "tools": [ {ToolSpec manifest}, ... ]}
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from yai_core.types import ToolSpec

#: manifest 文件格式版本（顶层包装）；不兼容升级时递增。
MANIFEST_VERSION = 1


def specs_to_manifest(specs: Iterable[ToolSpec]) -> dict[str, Any]:
    """把若干工具规格打包成一份 manifest dict。"""
    return {
        "manifest_version": MANIFEST_VERSION,
        "tools": [spec.to_manifest_dict() for spec in specs],
    }


def specs_from_manifest(data: dict[str, Any]) -> list[ToolSpec]:
    """解析 manifest dict，返回工具规格（handler 均为 None）。"""
    if not isinstance(data, dict):
        raise TypeError(f"manifest 必须是 object，得到 {type(data)!r}")
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"不支持的 manifest_version: {data.get('manifest_version')!r}，"
            f"期望 {MANIFEST_VERSION}"
        )
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise ValueError("manifest 缺少 tools 数组")
    return [ToolSpec.from_manifest_dict(item) for item in tools]


def write_manifest(path: str | Path, specs: Iterable[ToolSpec]) -> None:
    """把工具规格序列化为 JSON 文件（父目录自动创建，UTF-8）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(specs_to_manifest(specs), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_manifest(path: str | Path) -> list[ToolSpec]:
    """从 JSON 文件读取工具规格。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return specs_from_manifest(data)
