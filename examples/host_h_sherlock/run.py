"""宿主 H 命令行演示：YAI 嵌入真实开源项目 sherlock（92.5k stars, MIT）。

运行：.venv/Scripts/python.exe examples/host_h_sherlock/run.py
     .venv/Scripts/python.exe examples/host_h_sherlock/run.py "查 gi-tuu 的社交账号"

默认用脚本化模型、无需 API Key；配置 OPENAI_API_KEY 后自动走真实模型。
本演示的说服力在于：sherlock 是别人的 9 万星开源项目，我们没改它一行源码，
只在外面包了一个函数，Core 就用自然语言把它调起来了。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, load_dotenv, stream  # noqa: E402

bootstrap()
load_dotenv()

from host_h_sherlock.demo_agent import DEFAULT_TASK, build_core  # noqa: E402


async def main() -> None:
    core = build_core()
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    await stream(core, task, "嵌入真实开源项目 sherlock（离线确定性模型）")


if __name__ == "__main__":
    asyncio.run(main())
