"""本地维护 Bridge 的 LLM 配置（写 %LOCALAPPDATA%\\YAI\\bridge.json，不进浏览器）。

为什么用命令行而不是侧边栏表单：API Key 不应该经过浏览器扩展的 DOM / 历史 /
localStorage；在本地终端里改配置，面板只显示"当前模型 / base_url"（非敏感）。
改完需要关闭再打开侧边栏（Bridge 重新读配置），或重启浏览器。

用法::

    python scripts/configure_bridge_llm.py --show
    python scripts/configure_bridge_llm.py --base-url https://api.example.com/v1 --model m1
    Get-Content .env | python scripts/configure_bridge_llm.py --api-key-stdin

Key 不会出现在命令历史（--api-key-stdin 从 stdin 读，或交互式 getpass）；输出永远脱敏。
"""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "YAI" / "bridge.json"
)


def masked(config: dict[str, Any]) -> dict[str, Any]:
    """返回脱敏后的模型配置视图（永不包含完整 Key）。"""
    model = dict((config.get("initialize") or {}).get("model") or {})
    key = str(model.pop("api_key", "") or "")
    model["api_key"] = ("***" + key[-4:]) if key else ""
    return model


def update_model(
    config: dict[str, Any],
    *,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    strong_model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """返回更新后的配置副本；只改 initialize.model，其余字段原样保留。"""
    updated = copy.deepcopy(config)
    initialize = updated.setdefault("initialize", {})
    current = dict(initialize.get("model") or {})
    current.setdefault("provider", "openai-compatible")
    if provider:
        current["provider"] = provider
    for key, value in (
        ("base_url", base_url),
        ("model", model),
        ("strong_model", strong_model),
        ("api_key", api_key),
    ):
        if value:
            current[key] = value
    initialize["model"] = current
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="维护本地 Bridge 的 LLM 配置")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG))
    parser.add_argument("--show", action="store_true", help="显示当前配置（Key 脱敏）")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--strong-model", default=None)
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="从 stdin 读取 API Key（首行；不落命令历史）",
    )
    parser.add_argument(
        "--prompt-key",
        action="store_true",
        help="交互式输入 API Key（getpass，不回显）",
    )
    args = parser.parse_args(argv)

    path = Path(args.config).expanduser()
    if args.show:
        if not path.is_file():
            print(json.dumps({"ok": False, "error": f"配置不存在：{path}"}, ensure_ascii=False))
            return 1
        config = json.loads(path.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {"ok": True, "config": str(path), "model": masked(config)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    api_key = None
    if args.api_key_stdin:
        api_key = sys.stdin.readline().strip()
        if not api_key:
            print(json.dumps({"ok": False, "error": "stdin 没有读到 API Key"}, ensure_ascii=False))
            return 1
    elif args.prompt_key:
        api_key = getpass.getpass("OPENAI_API_KEY（不回显）: ").strip()
        if not api_key:
            print(json.dumps({"ok": False, "error": "未输入 API Key"}, ensure_ascii=False))
            return 1

    if not path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": f"配置不存在：{path}（先运行注册脚本）"},
                ensure_ascii=False,
            )
        )
        return 1
    original = json.loads(path.read_text(encoding="utf-8"))
    updated = update_model(
        original,
        provider=args.provider,
        base_url=args.base_url,
        model=args.model,
        strong_model=args.strong_model,
        api_key=api_key,
    )
    if updated == original:
        print(
            json.dumps(
                {"ok": True, "changed": False, "model": masked(original)},
                ensure_ascii=False,
            )
        )
        return 0
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "changed": True,
                "config": str(path),
                "model": masked(updated),
                "note": "关闭再打开侧边栏（或重启浏览器）让 Bridge 重新加载配置",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
