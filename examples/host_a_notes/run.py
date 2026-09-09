"""宿主 A 演示：同一个 Core 自动适配笔记应用。

运行：
  离线（无需 Key，脚本假模型）：.venv/Scripts/python.exe examples/host_a_notes/run.py
  真实模型（DeepSeek）：先在项目根 .env 填好 OPENAI_API_KEY，再运行同上命令
  自定义任务：在命令末尾追加任务文本，例如
      .venv/Scripts/python.exe examples/host_a_notes/run.py 帮我找出生活类笔记
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from demo_model import OfflineScriptedModel  # noqa: E402
from host_a_notes import capabilities  # noqa: E402
from yai_core import AgentCore, EventType, OpenAICompatProvider  # noqa: E402

DEFAULT_TASK = "列出我的全部笔记"


def load_dotenv() -> None:
    """极简 .env 加载器：只认 KEY=VALUE，不覆盖已存在的环境变量，内核保持零依赖。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_core() -> tuple[AgentCore, str]:
    if os.getenv("OPENAI_API_KEY"):
        model = OpenAICompatProvider()
        backend = f"真实模型 {os.getenv('OPENAI_BASE_URL', '(SDK默认)')} :: {model.model}"
    else:
        model = OfflineScriptedModel()
        backend = "离线脚本模型（未检测到 OPENAI_API_KEY）"
    # auto：内省 capabilities 模块，自动注册 list_notes/search_notes/count_notes
    return AgentCore.auto(capabilities, model), backend


async def main() -> None:
    load_dotenv()
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    core, backend = build_core()

    print("=" * 60)
    print("后端：", backend)
    print("自动发现的工具：", [t["name"] for t in core.list_tools()])
    print("任务：", task)
    print("-" * 60)

    async for event in core.astream(task):
        if event.type == EventType.STRATEGY_SELECTED:
            print(f"[路由] 选定策略：{event.data['strategy']}")
        elif event.type == EventType.PLAN_CREATED:
            print("[计划] 拆出步骤：")
            for i, step in enumerate(event.data["steps"], 1):
                print(f"   {i}. {step}")
        elif event.type == EventType.TOOL_CALL:
            print(f"[工具] 调用 {event.data['tool']}({event.data['arguments']})")
        elif event.type == EventType.TOOL_RESULT:
            preview = event.data.get("preview") or event.data.get("error", "")
            print(f"[观察] 返回：{str(preview)[:120]}")
        elif event.type == EventType.MODEL_MESSAGE:
            print(f"[模型] {event.data['text']}")
        elif event.type == EventType.ERROR:
            print(f"[错误] {event.data['error']}")
        elif event.type == EventType.DONE:
            print("-" * 60)
            print(f"完成（策略 {event.data['strategy']}）")
            print("最终结果：", event.data["final_text"])


if __name__ == "__main__":
    asyncio.run(main())
