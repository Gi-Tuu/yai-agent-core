"""宿主 E 演示：同一个 SalesCrm 对象零改造嵌入 YAI Agent Core，得到数字员工。

运行：.venv/Scripts/python.exe examples/host_e_sales_crm/run_agent.py [可选任务文本]
对照：standalone_cli.py 是同一个软件没有 AI 时的用法。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, build_model, load_dotenv  # noqa: E402

bootstrap()

from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402
from yai_core import AgentCore, EventType  # noqa: E402
from yai_core.policy import AllowlistPolicy  # noqa: E402

# 读工具自动放行；写工具（add_*/create_*/complete_*）一律先问人
READ_TOOLS = [
    "list_customers", "get_customer", "list_orders", "sum_amount",
    "list_followups", "customers_due_followup", "list_todos", "daily_brief",
]

DEFAULT_TASK = (
    "看看哪些客户超过 3 天没跟进，给最久没跟进的客户记录一条跟进"
    "“电话沟通，客户有意向下周看方案”，然后新建一个待办“下周二前发送报价单”，"
    "最后生成今天的销售日报。"
)


class DemoCliChannel:
    """示例内通道：事件美观打印 + 写权限交互确认 + 澄清提问（不改 src）。"""

    async def emit(self, event) -> None:
        d = event.data
        if event.type == EventType.STRATEGY_SELECTED:
            print(f"[路由] {d['strategy']}（来源：{d.get('source', '')}）—— {d.get('reason', '')}")
        elif event.type == EventType.PLAN_CREATED:
            print("[计划] 拆出步骤：")
            for i, step in enumerate(d["steps"], 1):
                print(f"   {i}. {step}")
        elif event.type == EventType.TOOL_CALL:
            print(f"[工具] 调用 {d['tool']}({d['arguments']})")
        elif event.type == EventType.TOOL_RESULT:
            preview = d.get("preview") or d.get("error", "")
            print(f"[观察] {'成功' if d.get('ok') else '失败'}：{str(preview)[:160]}")
            if not d.get("ok"):
                print(f"       错误：{d.get('error', '')}")
        elif event.type == EventType.PERMISSION_ASKED:
            # 权限 UI 由 confirm() 的交互提示承担，这里不重复打印
            # （事件仍正常进入事件流，API/测试/工作台不受影响）。
            pass
        elif event.type == EventType.CLARIFY_REQUESTED:
            print(f"[澄清] {d['question']}")
        elif event.type == EventType.CAPABILITY_MISSING:
            print(f"[能力缺口] 缺少：{d['missing']}（现有工具：{d['available_tools']}）")
        elif event.type == EventType.MODEL_MESSAGE:
            print(f"[模型] {d.get('text', '')}")
        elif event.type == EventType.ERROR:
            print(f"[错误] {d.get('error', '')}")
        elif event.type == EventType.DONE:
            print("-" * 60)
            print(f"完成（策略 {d['strategy']}）")
            print("最终结果：", d["final_text"])

    async def ask(self, question: str) -> str:
        answer = await asyncio.to_thread(input, f"  [澄清] {question} > ")
        if not sys.stdin.isatty():
            print()
        return answer

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        answer = await asyncio.to_thread(
            input, f"  [确认] 允许执行 {tool_name}({arguments})? [y/N] "
        )
        if not sys.stdin.isatty():
            print()  # 管道输入没有回车回显，补换行避免与下一日志同行
        return answer.strip().lower() in ("y", "yes", "是")


async def main() -> None:
    load_dotenv()
    model, backend = build_model()
    crm = SalesCrm()
    core = AgentCore.auto(
        crm,
        model,
        channel=DemoCliChannel(),
        policy=AllowlistPolicy(READ_TOOLS, mode="auto"),
        llm_router="auto",
    )
    task = " ".join(sys.argv[1:]).strip() or DEFAULT_TASK
    print("=" * 60)
    print("后端：", backend)
    print("自动发现的工具：", [t["name"] for t in core.list_tools()])
    print("任务：", task)
    print("-" * 60)
    async for _ in core.astream(task):
        pass


if __name__ == "__main__":
    asyncio.run(main())
