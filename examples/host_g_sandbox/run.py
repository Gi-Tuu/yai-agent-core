"""宿主 G 命令行演示：模型现场"造"一个代码工具，在沙箱里执行。

运行：.venv/Scripts/python.exe examples/host_g_sandbox/run.py

本演示刻意用一个"脚本化模型"（见 ``demo_agent.py``），**无需 API Key、
结果完全确定**：
  1. 宿主只提供只读的 list_candidates，没有"加权评分"能力；
  2. 模型调用 meta-tool create_code_tool，现场生成 weighted_score；
  3. 内核登记工具（48h TTL），随后模型先取候选人、再调用 weighted_score；
  4. weighted_score 由宿主的 SubprocessSandbox 在隔离子进程里执行；
  5. 模型基于沙箱返回的分数给出最终排序。

网页版见 ``web_app.py``（python examples/host_g_sandbox/web_app.py）。
真实模型链路与安全边界由 tests/test_sandbox_example.py 用真实子进程覆盖。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, stream  # noqa: E402

bootstrap()

from host_g_sandbox.demo_agent import DEFAULT_TASK, build_core  # noqa: E402


async def main() -> None:
    core = build_core()
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    await stream(core, task, "离线确定性模型 + 子进程沙箱（演示代码工具）")

    # 演示后台生命周期 API：查看代码工具状态。
    print("-" * 60)
    status = core.code_tools_status()
    print(f"代码工具注册表：启用={status['enabled']}，存活={status['live']}，明细：")
    for item in status["tools"]:
        print(f"   - {item['name']}（存活={item['live']}，"
              f"永久={item['permanent']}，调用次数={item['call_count']}，"
              f"TTL={item['ttl_seconds'] // 3600}h）")


if __name__ == "__main__":
    asyncio.run(main())
