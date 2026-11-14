"""MCP Registry 发现示例：只读搜索官方 registry，不自动安装/连接。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core.integrations.mcp.registry import McpRegistryClient  # noqa: E402


def _print_human(servers) -> None:
    for server in servers:
        latest = " latest" if server.is_latest else ""
        print(f"{server.name}  ({server.version}){latest}")
        if server.title:
            print(f"  {server.title}")
        if server.safe_description:
            print(f"  {server.safe_description}")
        for remote in server.remotes:
            auth = ", ".join(remote.required_headers) or "-"
            print(f"  remote[{remote.type}] {remote.url}  auth-headers={auth}")
        for package in server.packages:
            print(f"  package[{package.registry_type}] {package.identifier}@{package.version}")
        if server.repository_url:
            print(f"  repo: {server.repository_url}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Search the official MCP Registry (read-only)")
    parser.add_argument("query", nargs="?", default="", help="search keywords")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = McpRegistryClient().search(args.query, limit=args.limit)
    if args.json:
        payload = {
            "next_cursor": result.next_cursor,
            "servers": [asdict(server) for server in result.servers],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(result.servers)
        if result.next_cursor:
            print(f"next cursor: {result.next_cursor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
