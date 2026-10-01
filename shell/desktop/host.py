"""专员装配：四个彼此隔离的专员，离线演示与真实模型两种形态。

阶段 0 口径（见 ``docs/YAI灵动岛多专员-落地方案.md``）：

- 每个专员代表**一个被 AI 化的应用**及其内置 Core，工具 / 历史 / 权限各走各的；
- demo 形态全部离线脚本，无 Key 也能切换专员看动效；
- live 形态只给确实能真跑的专员接真 Core（仓库、笔记），
  其余回退演示脚本并在切换条上标"离线演示"；
- 不强耦合 host_e / AMBRACE，真接入放阶段 1（跨进程）。
"""

from __future__ import annotations

import sys

from shell._bootstrap import ROOT, bootstrap
from shell.demo_script import (
    SCRIPT,
    SCRIPT_COMPANION,
    SCRIPT_DELEGATE,
    SCRIPT_NOTES,
    SCRIPT_WAREHOUSE,
    make_demo_stream,
)
from shell.desktop.specialist import SpecialistSpec


class DemoWarehouse:
    """仓库应用：本地小库存，live 形态真跑内省 + 写操作授权。"""

    def __init__(self) -> None:
        self._items = {"螺丝": 120, "轴承": 8, "线缆": 50}

    def list_products(self) -> list[str]:
        """列出全部品名。"""
        return list(self._items)

    def get_stock(self, name: str) -> dict:
        """查询指定品名的库存数量。"""
        return {"name": name, "stock": self._items.get(name, 0)}

    def restock(self, name: str, qty: int) -> dict:
        """为指定品名补货指定数量。"""
        self._items[name] = self._items.get(name, 0) + int(qty)
        return {"name": name, "stock": self._items[name]}


class DemoNotes:
    """笔记应用：本地便签，live 形态真跑搜索与写入。"""

    def __init__(self) -> None:
        self._notes = {
            "周报": ["Core 骨架", "薄壳原生端"],
            "复盘": ["杭州未入围", "改做完整产品"],
        }

    def search_notes(self, keyword: str) -> list[str]:
        """按分类关键词搜索笔记条目。"""
        return self._notes.get(keyword, [])

    def add_note(self, keyword: str, text: str) -> dict:
        """在指定分类下新增一条笔记。"""
        self._notes.setdefault(keyword, []).append(text)
        return {"keyword": keyword, "count": len(self._notes[keyword])}


class DemoSales:
    """销售 CRM：客户 / 商机 / 订单的最小内存数据，live 形态真跑查询与录入。"""

    def __init__(self) -> None:
        self._customers = {
            "星河科技": {"contact": "王经理", "level": "A"},
            "云海贸易": {"contact": "李总", "level": "B"},
        }
        self._opportunities = [
            {"name": "星河年度续约", "customer": "星河科技",
             "stage": "方案", "amount": 120000},
            {"name": "云海增购", "customer": "云海贸易",
             "stage": "线索", "amount": 35000},
        ]
        self._orders: list[dict] = []

    def list_customers(self) -> dict:
        """列出全部客户及联系人、等级。"""
        return dict(self._customers)

    def get_customer(self, name: str) -> dict:
        """按客户名称查询单个客户资料。"""
        return self._customers.get(name, {})

    def list_opportunities(self) -> list[dict]:
        """列出商机管道及阶段、金额。"""
        return list(self._opportunities)

    def add_order(self, customer: str, product: str, amount: float) -> dict:
        """为指定客户录入一笔订单。"""
        order = {"customer": customer, "product": product, "amount": amount}
        self._orders.append(order)
        return {"order_no": len(self._orders), **order}


#: 各专员的"读工具"白名单：部分审批挡下自动放行，写操作仍要授权。
_READ_TOOLS = {
    "sales": ("list_customers", "get_customer", "list_opportunities"),
    "warehouse": ("list_products", "get_stock"),
    "notes": ("search_notes",),
}

#: 代码工具跨任务持久化目录（根 data/ 已在 .gitignore；按专员各一份）。
_DESKTOP_DATA = ROOT / "data" / "desktop"


def _make_policy(read_tools: tuple[str, ...], mode: str):
    from yai_core.policy import AllowlistPolicy

    if mode == "manual":
        return AllowlistPolicy((), mode="auto")
    if mode == "auto":
        return AllowlistPolicy(read_tools, mode="allow_all")
    return AllowlistPolicy(read_tools, mode="auto")


def _live_factory(
    host: object,
    read_tools: tuple[str, ...],
    *,
    with_sandbox: bool = False,
    storage_path=None,
    learning_path=None,
):
    """为一个小宿主生成"每任务一个真 Core"的事件流工厂。

    with_sandbox=True 时给该专员接产品层子进程沙箱（shell.desktop.sandbox），
    模型即可在授权后于沙箱中自建代码工具；不接则只保留组合工具。
    storage_path 给定时，代码工具跨任务落盘恢复（48h TTL、调用刷新）。
    learning_path 给定时，**跨任务共享**一个自校准路由选择器并在每次任务后
    落盘，让真机路由越用越准；None = 不启用学习，行为与原来完全一致。
    """
    # 每任务新建 Core，但路由学习必须在任务之间累积：选择器在闭包里共享，
    # 首个任务时才从磁盘恢复。同专员 busy 时拒绝第二个任务，访问天然串行。
    shared = {"selector": None}

    async def factory(task: str, channel):
        # 必须是 async generator（体内含 yield）：调用 factory(...) 直接返回
        # 异步迭代器，与 demo_factory 契约一致。若写成
        # ``return core.astream(...)``，factory(...) 只是 coroutine，
        # specialist 的 ``async for`` 会报
        # "'async for' requires an object with __aiter__, got coroutine"。
        from runner_common import build_model, load_dotenv
        from yai_core import AgentCore

        sandbox = None
        if with_sandbox:
            from shell.desktop.sandbox import SubprocessSandbox

            sandbox = SubprocessSandbox()

        if learning_path is not None and shared["selector"] is None:
            from shell.desktop.learning_vault import load_selector

            shared["selector"] = load_selector(learning_path)

        load_dotenv()
        model, _label = build_model()
        core = AgentCore.auto(
            host,
            model,
            channel=channel,
            policy=_make_policy(read_tools, channel.permission_mode),
            llm_router="auto",
            composition=True,
            sandbox=sandbox,
            code_storage=storage_path,
            learning=shared["selector"],
        )
        from shell.employee import attach_delegate

        attach_delegate(core, depth=1)
        async for event in core.astream(task):
            yield event
        # 任务正常跑完后落盘学习状态（取消时 astream 不回灌、也走不到这里）。
        if shared["selector"] is not None:
            from shell.desktop.learning_vault import save_selector

            save_selector(shared["selector"], learning_path)

    return factory


def build_specs() -> list[SpecialistSpec]:
    """阶段 0 的四个专员：销售 / 仓库 / 笔记 / 陪伴。"""
    # 各专员代码工具仓库路径只构造一次：装配 Core 与工具管理面板共用同一份。
    sales_storage = _DESKTOP_DATA / "sales_code_tools.json"
    wh_storage = _DESKTOP_DATA / "warehouse_code_tools.json"
    notes_storage = _DESKTOP_DATA / "notes_code_tools.json"
    # 各专员路由自学习状态文件（与代码工具仓库并列，跨任务累积、重启不丢）。
    sales_learning = _DESKTOP_DATA / "sales_route_learning.json"
    wh_learning = _DESKTOP_DATA / "warehouse_route_learning.json"
    notes_learning = _DESKTOP_DATA / "notes_route_learning.json"
    return [
        SpecialistSpec(
            id="sales",
            name="销售专员",
            glyph="销",
            demo_factory=make_demo_stream(
                SCRIPT,
                {"委派": SCRIPT_DELEGATE, "并行": SCRIPT_DELEGATE,
                 "子员工": SCRIPT_DELEGATE},
            ),
            live_factory=_live_factory(
                DemoSales(),
                _READ_TOOLS["sales"],
                with_sandbox=True,
                storage_path=sales_storage,
                learning_path=sales_learning,
            ),
            code_storage=sales_storage,
            learning_path=sales_learning,
        ),
        SpecialistSpec(
            id="warehouse",
            name="仓库专员",
            glyph="仓",
            demo_factory=make_demo_stream(SCRIPT_WAREHOUSE),
            live_factory=_live_factory(
                DemoWarehouse(),
                _READ_TOOLS["warehouse"],
                with_sandbox=True,
                storage_path=wh_storage,
                learning_path=wh_learning,
            ),
            code_storage=wh_storage,
            learning_path=wh_learning,
        ),
        SpecialistSpec(
            id="notes",
            name="笔记专员",
            glyph="笔",
            demo_factory=make_demo_stream(SCRIPT_NOTES),
            live_factory=_live_factory(
                DemoNotes(),
                _READ_TOOLS["notes"],
                with_sandbox=True,
                storage_path=notes_storage,
                learning_path=notes_learning,
            ),
            code_storage=notes_storage,
            learning_path=notes_learning,
        ),
        SpecialistSpec(
            id="companion",
            name="陪伴专员",
            glyph="陪",
            demo_factory=make_demo_stream(SCRIPT_COMPANION),
        ),
    ]


def build_workbench(permission_mode: str = "partial", *, live: bool = False):
    """装配工作台：唯一一条后台循环 + 四个专员。"""
    from shell.desktop.workbench import WorkbenchRuntime

    return WorkbenchRuntime(
        build_specs(),
        mode="live" if live else "demo",
        permission_mode=permission_mode,
        active_id="sales",
    )


def prepare_paths() -> None:
    """直跑 ``shell/desktop`` 时补齐 ``src`` 与 ``examples`` 导入路径。"""
    bootstrap()
    examples = ROOT / "examples"
    if str(examples) not in sys.path:
        sys.path.insert(0, str(examples))
