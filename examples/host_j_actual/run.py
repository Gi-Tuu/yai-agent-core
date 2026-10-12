"""host_j 命令行演示：Actual Budget 月末对账的两条第三方播种链路。

运行（默认：发现 + 授权链路）：
    .venv/Scripts/python.exe examples/host_j_actual/run.py

委派链路（根派两个只读子员工并行）：
    .venv/Scripts/python.exe examples/host_j_actual/run.py --delegate

默认离线、无需 API Key、结果确定；配置好真实模型环境变量后，runner_common
会自动切到 OpenAI 兼容后端，同一条五拍链路同样成立。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, stream  # noqa: E402

bootstrap()

from host_j_actual.demo_agent import (  # noqa: E402
    DEFAULT_DELEGATE_TASK,
    DEFAULT_TASK,
    build_delegate_core,
    build_discovery_core,
)


async def main() -> None:
    delegate = "--delegate" in sys.argv[1:]
    extra = [a for a in sys.argv[1:] if a != "--delegate"]
    task = " ".join(extra).strip()

    if delegate:
        core = build_delegate_core()
        await stream(core, task or DEFAULT_DELEGATE_TASK, "委派链路（并行只读子员工）")
    else:
        core = build_discovery_core()
        await stream(core, task or DEFAULT_TASK, "发现+授权链路（月末对账五拍）")


if __name__ == "__main__":
    asyncio.run(main())
