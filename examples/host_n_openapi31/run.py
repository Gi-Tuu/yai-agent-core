"""OpenAPI 3.1 离线发现示例：注册工具并打印清洗后的 schema，不执行 HTTP 请求。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core.integrations.openapi import OpenApiSpecConfig, OpenApiToolBridge  # noqa: E402
from yai_core.tools.registry import ToolRegistry  # noqa: E402


def example_spec() -> dict:
    """同一操作展示 null 联合类型、const 和本地 $ref 的兄弟键覆盖。"""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Offline schema demo", "version": "1"},
        "servers": [{"url": "https://example.invalid"}],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "summary": "List items (schema demonstration only)",
                    "parameters": [
                        {"name": "note", "in": "query", "schema": {"type": ["string", "null"]}},
                        {
                            "name": "mode",
                            "in": "query",
                            "schema": {"type": "string", "const": "preview"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {
                                "$ref": "#/components/schemas/Limit",
                                "maximum": 3,
                                "description": "At most three items in this demonstration",
                            },
                        },
                    ],
                    "responses": {"200": {"description": "Not requested by this example"}},
                }
            }
        },
        "components": {
            "schemas": {
                "Limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Shared limit before the operation overrides it",
                }
            }
        },
    }


def reject_request(request: httpx.Request) -> httpx.Response:
    """即使误调用工具也在内存中失败，绝不连接示例地址。"""
    raise RuntimeError("This example only registers tools; HTTP requests are disabled.")


async def main() -> None:
    config = OpenApiSpecConfig(
        alias="openapi31",
        prefix="openapi31",
        spec=example_spec(),
        auth_token_env=None,
        read_only=True,
    )
    registry = ToolRegistry()
    async with OpenApiToolBridge(config, transport=httpx.MockTransport(reject_request)) as bridge:
        registry.register_many(bridge.specs)
        print(
            json.dumps(
                {
                    "tools": [
                        {"name": tool.name, "input_schema": tool.input_schema}
                        for tool in registry.all()
                    ],
                    "notes": bridge.notes,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
