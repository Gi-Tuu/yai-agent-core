"""员工薄壳开箱启动器。

两种模式：

- 演示（默认）：离线脚本事件，**无 API Key 也能看全部动效**，便于核对与录屏；
- ``--live``：接真实模型，装配一个内置最小宿主，验证真内核链路
  （S2 起在 core 上注册 delegate，支持子员工）。

用法::

    .\\.venv\\Scripts\\python.exe shell\\serve.py
    .\\.venv\\Scripts\\python.exe shell\\serve.py --live --port 8300

然后浏览器打开 http://127.0.0.1:8300 （默认自动打开）。
录屏一键开场：http://127.0.0.1:8300/?autoplay
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# 直接运行时准备好三类路径：shell 包、yai_core、examples（live 复用 runner_common）。
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from shell.demo_script import demo_stream  # noqa: E402
from shell.proxy import create_server  # noqa: E402
from shell.session import PERMISSION_MODES, ShellSession  # noqa: E402


class DemoWarehouse:
    """内置最小宿主：本地小仓库，供 --live 验证真内核工具/授权链路。"""

    def __init__(self) -> None:
        self._items = {"螺丝": 120, "轴承": 8, "线缆": 50}

    def list_products(self) -> list[str]:
        return list(self._items)

    def get_stock(self, name: str) -> dict:
        return {"name": name, "stock": self._items.get(name, 0)}

    def restock(self, name: str, qty: int) -> dict:
        self._items[name] = self._items.get(name, 0) + int(qty)
        return {"name": name, "stock": self._items[name]}


_READ_TOOLS = ["list_products", "get_stock"]


def _make_policy(mode: str):
    from yai_core.policy import AllowlistPolicy

    if mode == "manual":
        return AllowlistPolicy((), mode="auto")
    if mode == "auto":
        return AllowlistPolicy(_READ_TOOLS, mode="allow_all")
    return AllowlistPolicy(_READ_TOOLS, mode="auto")


def build_live_session(permission_mode: str) -> ShellSession:
    """构建接真实模型的会话（每任务一个 core，S2 在此注册 delegate）。"""
    from runner_common import build_model, load_dotenv

    load_dotenv()
    model, label = build_model()
    model_factory = lambda: model  # noqa: E731 - 所有 core 共用同一后台 loop
    host = DemoWarehouse()
    mode_holder = {"mode": permission_mode}

    async def live_factory(task: str, channel):
        from yai_core import AgentCore

        core = AgentCore.auto(
            host,
            model_factory(),
            channel=channel,
            policy=_make_policy(mode_holder["mode"]),
            llm_router="auto",
            composition=True,
        )
        # S2：注册 delegate，根员工可并行委派子员工（深度 ≤ 2、并发 ≤ 5）。
        from shell.employee import attach_delegate

        attach_delegate(core, depth=1)
        return core.astream(task)

    session = ShellSession(live_factory, permission_mode=permission_mode)

    def _set_permission(mode: str) -> str:
        result = session_set(mode)
        mode_holder["mode"] = mode
        return result

    session_set = session.set_permission_mode
    session.set_permission_mode = _set_permission  # type: ignore[assignment]
    session.model_label = label  # type: ignore[attr-defined]
    return session


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="YAI 员工薄壳启动器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--live", action="store_true", help="接真实模型（默认离线演示模式）"
    )
    parser.add_argument(
        "--permission", choices=PERMISSION_MODES, default="partial",
        help="初始权限挡位（默认 partial）",
    )
    parser.add_argument(
        "--core-off", action="store_true", help="启动时停用 Core"
    )
    args = parser.parse_args(argv)

    if args.live:
        session = build_live_session(args.permission)
        mode_label = "真实模型"
    else:
        session = ShellSession(
            demo_stream, permission_mode=args.permission, mode="demo"
        )
        mode_label = "离线演示（无 Key）"

    if args.core_off:
        session.set_enabled(False)

    httpd = create_server(session, args.host, args.port)
    url = f"http://{args.host}:{httpd.server_port}"
    print("=" * 60)
    print("YAI 员工薄壳（浮窗 + 员工委派）")
    print(f"  本地地址 : {url}")
    print(f"  运行模式 : {mode_label}")
    if args.live:
        print(f"  模型后端 : {getattr(session, 'model_label', '')}")
    print(f"  Core 状态: {'启用' if session.enabled else '停用'}")
    print(f"  权限挡位 : {session.permission_mode}")
    print("  录屏开场 : " + url + "/?autoplay")
    print("  Ctrl+C 停止。")
    print("=" * 60)

    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
