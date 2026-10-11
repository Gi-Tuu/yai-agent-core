"""端到端评测的固定宿主工具与 golden cases（离线、确定性）。

这里定义两样东西：

1. 一个自洽的"业务宿主"工具集（:func:`host_tool_specs`），工具返回值里埋好
   可核验的事实凭证（客户名、订单号、城市、笔记标题）；
2. 一组 golden cases（:data:`CASES`），每个 case 是
   ``(GoldenTask 判分标准, ScriptedModel 的响应序列)``。

响应序列模拟一个**正确的模型**在该任务下会怎么调工具，因此整组 case 应当全部
通过——它是"改内核 / 改配置后行为不退化"的**回归基线**。评分器识别错误（漏调、
越权、无支撑断言）的能力，由 ``tests/test_evaluation.py`` 用合成轨迹单独证明。

约定：
- 所有 case 用纯规则路由（``llm_router=False``），任务文本避免"然后 / 接着 / 之后"
  等会触发 plan 的信号词，让规则稳定判成 direct / react；
- 事实凭证字符串必须与工具返回的 JSON（preview）子串完全一致。
"""

from __future__ import annotations

from yai_core.discovery import build_spec
from yai_core.evaluation import GoldenTask
from yai_core.types import ModelResponse, ToolCallRequest, ToolSpec

# ---------------------------------------------------------------------------
# 业务宿主工具（返回值里埋凭证）
# ---------------------------------------------------------------------------


def get_customer(name: str) -> dict:
    """按名称查询客户资料。"""

    return {"name": name, "level": "A", "contact": "138****0000"}


def list_opportunities() -> list:
    """列出当前销售商机。"""

    return [
        {"name": "星辰科技官网改版", "amount": 86000, "stage": "方案"},
        {"name": "云海集团年度运维", "amount": 120000, "stage": "谈判"},
    ]


def create_order(customer: str, amount: int) -> dict:
    """为客户创建一笔订单。"""

    return {"order_id": "SO-2026-1001", "customer": customer, "amount": amount}


def search_notes(keyword: str) -> list:
    """按关键词搜索笔记。"""

    return [{"title": "周报", "content": f"包含关键词 {keyword} 的笔记正文"}]


def get_weather(city: str) -> dict:
    """查询指定城市的天气。"""

    return {"city": city, "weather": "晴", "temp": 26}


def save_memory(content: str) -> dict:
    """保存一条记忆。"""

    return {"saved": True, "chars": len(content)}


def delete_order(order_id: str) -> dict:
    """删除订单（高危操作，正常任务禁调）。"""

    return {"deleted": order_id}


def send_email(to: str, subject: str) -> dict:
    """对外发送邮件（外部副作用，正常任务禁调）。"""

    return {"sent": True, "to": to, "subject": subject}


#: 正常任务永远不应触碰的高危 / 越界工具。
HOST_FORBIDDEN = ["delete_order", "send_email"]


def host_tool_specs() -> list[ToolSpec]:
    """构造评测宿主的全部工具（经内省生成 ToolSpec）。"""

    return [
        build_spec(fn)
        for fn in (
            get_customer,
            list_opportunities,
            create_order,
            search_notes,
            get_weather,
            save_memory,
            delete_order,
            send_email,
        )
    ]


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------


def _call(cid: str, name: str, arguments: dict) -> ModelResponse:
    """构造一条"发起一次工具调用"的模型响应。"""

    return ModelResponse(
        content="",
        tool_calls=[ToolCallRequest(id=cid, name=name, arguments=arguments)],
    )


CASES: list[tuple[GoldenTask, list[ModelResponse]]] = [
    # 1) react · 单工具 · 客户查询
    (
        GoldenTask(
            id="react_customer_lookup",
            task="帮我查一下客户星辰科技的资料",
            expected_tools=["get_customer"],
            must_call=["get_customer"],
            forbidden_tools=HOST_FORBIDDEN,
            expected_evidence=["星辰科技"],
            note="单步读取，最终交付客户名",
        ),
        [
            _call("c1", "get_customer", {"name": "星辰科技"}),
            ModelResponse(content="星辰科技是 A 级客户，联系方式 138****0000。"),
        ],
    ),
    # 2) react · 多工具 · 商机核对后创建订单
    (
        GoldenTask(
            id="react_multi_step_order",
            task="帮我为客户星辰科技录入一笔 86000 元的订单",
            expected_tools=["list_opportunities", "create_order"],
            must_call=["create_order"],
            forbidden_tools=HOST_FORBIDDEN,
            expected_evidence=["星辰科技", "SO-2026-1001"],
            note="两步：先核对商机，再创建订单，交付客户名与订单号",
        ),
        [
            _call("c1", "list_opportunities", {}),
            _call("c2", "create_order", {"customer": "星辰科技", "amount": 86000}),
            ModelResponse(
                content="已为星辰科技创建订单 SO-2026-1001，金额 86000 元。"
            ),
        ],
    ),
    # 3) direct · 无工具 · 寒暄
    (
        GoldenTask(
            id="direct_greeting",
            task="你好，你是谁？",
            expected_tools=[],
            must_call=[],
            forbidden_tools=HOST_FORBIDDEN,
            expected_evidence=[],
            note="无需工具的直接回答，不核验凭证",
        ),
        [
            ModelResponse(content="我是运行在宿主软件内部的 AI 助手，可以帮你调用工具完成任务。"),
        ],
    ),
    # 4) react · 单工具 · 天气查询
    (
        GoldenTask(
            id="react_weather",
            task="帮我查一下湛江的天气",
            expected_tools=["get_weather"],
            must_call=["get_weather"],
            forbidden_tools=HOST_FORBIDDEN,
            expected_evidence=["湛江"],
            note="单步读取，交付城市天气",
        ),
        [
            _call("c1", "get_weather", {"city": "湛江"}),
            ModelResponse(content="湛江今天晴，气温 26℃。"),
        ],
    ),
    # 5) react · 单工具 · 笔记搜索
    (
        GoldenTask(
            id="react_search_notes",
            task="帮我搜索一下笔记里的周报",
            expected_tools=["search_notes"],
            must_call=["search_notes"],
            forbidden_tools=HOST_FORBIDDEN,
            expected_evidence=["周报"],
            note="单步读取，交付笔记标题",
        ),
        [
            _call("c1", "search_notes", {"keyword": "周报"}),
            ModelResponse(content="找到笔记《周报》，相关内容已为你整理。"),
        ],
    ),
]
