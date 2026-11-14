"""MCP Registry 接入示例：搜索 → 显式选择 → 连接 → 列出/调用工具。

安全口径：

- 必须用 `--name` 明确指定 registry 条目，**不会自动挑第一个并连接**；
- 只连接 `https` + `streamable-http` remote；缺变量/必填头直接报错；
- 默认只列工具；真正调用要再加 `--call <tool>`，由操作者显式选择。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core.integrations.mcp import McpRegistryClient, McpToolBridge  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    result = McpRegistryClient().search(args.query, limit=args.limit)
    server = next((item for item in result.servers if item.name == args.name), None)
    if server is None:
        print(f"registry 中没有找到 {args.name!r}（query={args.query!r}）", file=sys.stderr)
        return 1

    headers = {}
    for item in args.header or []:
        if "=" not in item:
            print(f"--header 需要 NAME=VALUE：{item!r}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        headers[key] = value
    try:
        config = server.build_config(
            alias=args.alias or server.name,
            remote_index=args.remote_index,
            variables=dict(
                item.split("=", 1) for item in (args.variable or []) if "=" in item
            ),
            headers=headers,
            token_env=args.token_env,
        )
    except ValueError as exc:
        print(f"拒绝构建配置：{exc}", file=sys.stderr)
        return 2

    print(f"server : {server.name} {server.version}")
    print(f"remote : {config.url}")
    print(f"alias  : {config.alias}")
    bridge = McpToolBridge(config)
    specs = await bridge.connect()
    try:
        print(f"tools  : {len(specs)}")
        for spec in specs:
            print(f"  - {spec.name}: {spec.description}")
        if args.call:
            spec = next((item for item in specs if item.name == args.call), None)
            if spec is None:
                print(f"没有工具 {args.call!r}", file=sys.stderr)
                return 1
            arguments = json.loads(args.args or "{}")
            print(f"call   : {args.call} {arguments}")
            value = await spec.handler(**arguments)
            print(str(value)[:2000])
    finally:
        await bridge.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP Registry 显式接入示例")
    parser.add_argument("--query", default="", help="registry 搜索词")
    parser.add_argument("--name", required=True, help="registry server 的精确 name")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--alias", default=None)
    parser.add_argument("--remote-index", type=int, default=0)
    parser.add_argument("--variable", action="append", help="URL 模板变量 key=value")
    parser.add_argument("--header", action="append", help="鉴权头 Name=Value")
    parser.add_argument("--token-env", default=None, help="Bearer token 的环境变量名")
    parser.add_argument("--call", default=None, help="显式调用某个工具")
    parser.add_argument("--args", default="{}", help="工具参数 JSON")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
