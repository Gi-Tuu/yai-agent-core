"""host_e 销售 CRM：业务层 + 零改造嵌入 + 权限分级 + 独立菜单，全部离线测试。"""

import asyncio
import io
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402
from host_e_sales_crm.standalone_cli import main as cli_main  # noqa: E402
from yai_core import (  # noqa: E402
    AgentCore,
    EventType,
    ModelResponse,
    ToolCallRequest,
    discover,
)
from yai_core.channels.collect import CollectChannel  # noqa: E402
from yai_core.policy import AllowlistPolicy  # noqa: E402

FIXED = date(2026, 9, 18)
READ_TOOLS = [
    "list_customers", "get_customer", "list_orders", "sum_amount",
    "list_followups", "customers_due_followup", "list_todos", "daily_brief",
]
WRITE_TOOLS = ["add_customer", "add_followup", "create_todo", "complete_todo"]


def _crm(tmp_path=None) -> SalesCrm:
    return SalesCrm(data_path=tmp_path, clock=lambda: FIXED, persist=tmp_path is not None)


class ScriptedModel:
    """按预设顺序返回响应的假模型，用于离线测试。"""

    def __init__(self, responses):
        self._responses = responses
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


def _run(core, task):
    return asyncio.run(core.run(task))


# ---------- 业务层 ----------

def test_seed_data_self_consistent():
    crm = _crm()
    assert len(crm.list_customers()) == 5
    assert len(crm.list_orders()) == 6
    assert len(crm.list_followups()) == 3
    assert len(crm.list_todos("all")) == 4
    assert len(crm.list_todos("open")) == 3


def test_list_customers_level_filter():
    crm = _crm()
    assert {c["name"] for c in crm.list_customers("重点")} == {"王敏", "李强"}


def test_get_customer_found_and_missing():
    crm = _crm()
    found = crm.get_customer("王敏")
    assert found["company"] == "华东智造集团"
    assert len(found["followups"]) == 1
    missing = crm.get_customer("不存在的人")
    assert missing["ok"] is False


def test_orders_filter_and_sum():
    crm = _crm()
    assert crm.sum_amount() == 38700
    assert crm.sum_amount(category="硬件") == 16500
    assert crm.sum_amount(region="华东") == 21500
    assert crm.sum_amount(category="服务", region="华南") == 3200
    assert len(crm.list_orders(category="软件")) == 2


def test_customers_due_followup_relative_dates():
    crm = _crm()
    due = crm.customers_due_followup(3)
    names = [d["name"] for d in due]
    assert names == ["周涛", "王敏", "李强"]  # 9 / 7 / 5 天，降序
    assert due[0]["days_overdue"] == 9
    assert [d["name"] for d in crm.customers_due_followup(6)] == ["周涛", "王敏"]


def test_list_todos_status_validation():
    crm = _crm()
    with pytest.raises(ValueError):
        crm.list_todos("bogus")
    assert len(crm.list_todos("done")) == 1


def test_daily_brief_sections():
    crm = _crm()
    brief = crm.daily_brief()
    assert "销售日报（2026-09-18）" in brief
    assert "¥38,700" in brief
    assert "周涛" in brief and "已 9 天未跟进" in brief
    assert "未完成待办（3 项）" in brief


def test_add_followup_updates_customer_and_rejects_unknown():
    crm = _crm()
    ok = crm.add_followup("王敏", "电话沟通，客户有意向下周看方案")
    assert ok["ok"] is True
    assert crm.get_customer("王敏")["last_followup"] == FIXED.isoformat()
    assert len(crm.get_customer("王敏")["followups"]) == 2
    bad = crm.add_followup("不存在", "x")
    assert bad["ok"] is False
    empty = crm.add_followup("王敏", "  ")
    assert empty["ok"] is False


def test_todo_lifecycle_and_customer_add():
    crm = _crm()
    created = crm.create_todo("下周二前发送报价单", "2026-09-22")
    assert created["ok"] is True
    assert len(crm.list_todos("open")) == 4
    done = crm.complete_todo("合同条款")
    assert done["ok"] is True and done["todo"]["done"] is True
    miss = crm.complete_todo("不存在的待办xyz")
    assert miss["ok"] is False
    added = crm.add_customer("吴用", "测试公司", "普通")
    assert added["ok"] is True and crm.get_customer("吴用")["company"] == "测试公司"
    dup = crm.add_customer("吴用", "另一家")
    assert dup["ok"] is False


def test_persistence_roundtrip_and_reset(tmp_path):
    db = tmp_path / "crm_data.json"
    crm = _crm(db)
    crm.add_followup("周涛", "重新建立联系")
    assert db.exists()
    crm2 = SalesCrm(db, clock=lambda: FIXED)
    assert len(crm2.list_followups("周涛")) == 1
    reset = crm2._reset()
    assert reset["ok"] is True and len(crm2.list_customers()) == 5
    assert crm2.list_followups("周涛") == []


# ---------- 嵌入 Core：发现 / 权限 ----------

def test_discovery_registers_twelve_native_tools():
    crm = _crm()
    specs = discover(crm)
    names = {s.name for s in specs}
    assert names == set(READ_TOOLS) | set(WRITE_TOOLS)
    assert len(specs) == 12
    assert all(s.source == "native" for s in specs)
    assert not any(n.startswith("_") for n in names)


def _core_with(model, *, auto_confirm, allowed=READ_TOOLS, mode="auto"):
    crm = _crm()
    channel = CollectChannel(auto_confirm=auto_confirm)
    core = AgentCore(model, channel=channel, policy=AllowlistPolicy(allowed, mode=mode))
    core.register_tools(discover(crm))
    return core, crm, channel


def test_read_tool_runs_without_permission_prompt():
    model = ScriptedModel([
        ModelResponse(content="", tool_calls=[
            ToolCallRequest(id="1", name="sum_amount",
                            arguments={"category": "硬件", "region": "华东"})]),
        ModelResponse(content="华东硬件销售额为 16500 元。"),
    ])
    core, _, _ = _core_with(model, auto_confirm=False)  # 读工具即使不自动确认也不该被问
    result = _run(core, "统计华东硬件销售额")
    kinds = [e.type for e in result.events]
    assert EventType.PERMISSION_ASKED not in kinds
    assert EventType.TOOL_CALL in kinds
    assert "16500" in result.final_text


def test_write_tool_asks_then_executes_when_confirmed():
    model = ScriptedModel([
        ModelResponse(content="", tool_calls=[
            ToolCallRequest(id="1", name="add_followup",
                            arguments={"customer": "王敏",
                                       "content": "电话沟通，客户有意向下周看方案"})]),
        ModelResponse(content="已为王敏记录跟进。"),
    ])
    core, crm, _ = _core_with(model, auto_confirm=True)
    result = _run(core, "给王敏记录一条跟进")
    kinds = [e.type for e in result.events]
    assert EventType.PERMISSION_ASKED in kinds
    assert any(e.data.get("tool") == "add_followup" and e.data.get("ok")
               for e in result.events if e.type == EventType.TOOL_RESULT)
    assert len(crm.get_customer("王敏")["followups"]) == 2


def test_write_tool_denied_when_rejected():
    model = ScriptedModel([
        ModelResponse(content="", tool_calls=[
            ToolCallRequest(id="1", name="create_todo",
                            arguments={"title": "不该被创建的待办"})]),
        ModelResponse(content="用户未授权，未创建待办。"),
    ])
    core, crm, _ = _core_with(model, auto_confirm=False)
    result = _run(core, "帮我新增一个待办")
    kinds = [e.type for e in result.events]
    assert EventType.PERMISSION_ASKED in kinds
    assert EventType.TOOL_CALL not in kinds
    assert all("不该被创建" not in t["title"] for t in crm.list_todos("all"))


def test_deny_all_policy_blocks_write():
    seen: dict = {}

    class DenyModel:
        async def achat(self, messages, tools=None, *, tier="standard"):
            seen["messages"] = messages
            if not any(m["role"] == "tool" for m in messages):
                return ModelResponse(content="", tool_calls=[
                    ToolCallRequest(id="1", name="add_customer",
                                    arguments={"name": "新人", "company": "X 公司"})])
            return ModelResponse(content="被策略拒绝。")

    crm = _crm()
    core = AgentCore(DenyModel(), channel=CollectChannel(),
                     policy=AllowlistPolicy(READ_TOOLS, mode="deny_all"))
    core.register_tools(discover(crm))
    result = _run(core, "新增客户新人")
    kinds = [e.type for e in result.events]
    assert EventType.TOOL_CALL not in kinds
    assert EventType.PERMISSION_ASKED not in kinds
    assert crm.get_customer("新人")["ok"] is False
    # DENY 不产生工具事件，拒绝文本作为工具消息回灌模型
    tool_msgs = [m["content"] for m in seen["messages"] if m["role"] == "tool"]
    assert any("权限策略拒绝" in content for content in tool_msgs)


# ---------- 独立菜单（无 AI） ----------

def test_standalone_menu_full_session():
    crm = _crm()
    out = io.StringIO()
    # 7 看日报 → 8 写跟进（客户+内容）→ 6 看待办（状态 open）→ 0 退出
    cli_main(inputs=["7", "8", "王敏", "菜单测试跟进", "6", "open", "0"], out=out, crm=crm)
    text = out.getvalue()
    assert "销售日报" in text
    assert "菜单测试跟进" in text
    assert len(crm.get_customer("王敏")["followups"]) == 2
    assert "再见" in text
