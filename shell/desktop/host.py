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


#: 各专员的"读工具"白名单：部分审批挡下自动放行，写操作仍要授权。
_READ_TOOLS = {
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
):
    """为一个小宿主生成"每任务一个真 Core"的事件流工厂。

    with_sandbox=True 时给该专员接产品层子进程沙箱（shell.desktop.sandbox），
    模型即可在授权后于沙箱中自建代码工具；不接则只保留组合工具。
    storage_path 给定时，代码工具跨任务落盘恢复（48h TTL、调用刷新）。
    """

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
        )
        from shell.employee import attach_delegate

        attach_delegate(core, depth=1)
        async for event in core.astream(task):
            yield event

    return factory


def build_specs() -> list[SpecialistSpec]:
    """阶段 0 的四个专员：销售 / 仓库 / 笔记 / 陪伴。"""
    # 仓库专员的代码工具仓库路径只构造一次：装配 Core 与工具管理面板共用同一份。
    wh_storage = _DESKTOP_DATA / "warehouse_code_tools.json"
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
            ),
            code_storage=wh_storage,
        ),
        SpecialistSpec(
            id="notes",
            name="笔记专员",
            glyph="笔",
            demo_factory=make_demo_stream(SCRIPT_NOTES),
            live_factory=_live_factory(DemoNotes(), _READ_TOOLS["notes"]),
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
