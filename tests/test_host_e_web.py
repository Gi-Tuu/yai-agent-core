"""host_e 网页工作台：WebChannel 授权交互 + 标准库 HTTP/SSE 端到端，全部离线测试。"""

import asyncio
import json
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_e_sales_crm.catalog_capabilities import (  # noqa: E402
    build_catalog,
    calc_quote_with_tax,
    catalog_summary,
    get_visit_weather,
)
from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402
from host_e_sales_crm.web_app import (  # noqa: E402
    WebChannel,
    Workbench,
    create_server,
    resolve_pending,
)
from yai_core import ModelResponse, ToolCallRequest  # noqa: E402

FIXED = date(2026, 9, 18)


def _crm() -> SalesCrm:
    return SalesCrm(data_path=None, clock=lambda: FIXED, persist=False)


# ---------- WebChannel：授权/澄清的挂起与兑现 ----------

def test_web_channel_confirm_approve_and_deny():
    async def scenario():
        loop = asyncio.get_running_loop()
        run: dict = {"queue": queue.Queue(), "pending": None, "active": True}
        channel = WebChannel(run, loop)

        approve_task = asyncio.ensure_future(
            channel.confirm("add_followup", {"customer": "周涛"})
        )
        await asyncio.sleep(0)
        kind, future = run["pending"]
        assert kind == "confirm" and not future.done()
        prompt = run["queue"].get_nowait()
        assert prompt["type"] == "permission_request"
        assert prompt["data"]["tool"] == "add_followup"
        assert resolve_pending(run, "confirm", True) is True
        assert await approve_task is True
        assert run["pending"] is None

        deny_task = asyncio.ensure_future(channel.confirm("create_todo", {"title": "x"}))
        await asyncio.sleep(0)
        run["queue"].get_nowait()
        resolve_pending(run, "confirm", False)
        assert await deny_task is False

    asyncio.run(scenario())


def test_web_channel_ask_answer_and_wrong_kind_rejected():
    async def scenario():
        loop = asyncio.get_running_loop()
        run: dict = {"queue": queue.Queue(), "pending": None, "active": True}
        channel = WebChannel(run, loop)

        ask_task = asyncio.ensure_future(channel.ask("找哪位客户？"))
        await asyncio.sleep(0)
        assert run["queue"].get_nowait()["type"] == "clarify_request"
        # 类型不匹配（把回答当授权）必须失败，不能误兑现
        assert resolve_pending(run, "confirm", True) is False
        assert resolve_pending(run, "ask", "周涛") is True
        assert await ask_task == "周涛"

    asyncio.run(scenario())


# ---------- 标准库 HTTP / SSE 端到端 ----------

class _OneToolModel:
    """首轮（含分类失败回退后的 ReAct）调工具，看到工具结果后收尾。"""

    def __init__(self, tool_name, arguments):
        self.tool_name = tool_name
        self.arguments = arguments

    async def achat(self, messages, tools=None, *, tier="standard"):
        if any(m.get("role") == "tool" for m in messages):
            return ModelResponse(content="任务完成")
        return ModelResponse(
            content="",
            tool_calls=[ToolCallRequest(id="1", name=self.tool_name, arguments=self.arguments)],
        )


class _BlockModel:
    """卡在首轮的模型，用于验证同一时刻只允许一个任务。"""

    def __init__(self):
        self.gate = asyncio.Event()

    async def achat(self, messages, tools=None, *, tier="standard"):
        await self.gate.wait()
        return ModelResponse(content="好的")


def _start_server(model, crm=None):
    crm = crm or _crm()
    workbench = Workbench(model_factory=lambda: model, model_label="离线测试模型", crm=crm)
    httpd = create_server(workbench, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    return workbench, httpd, base


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _collect_stream(base, run_id, on_permission=None):
    """在工作线程里消费 SSE；遇到授权请求时按回调回传决策。"""
    events = []
    url = f"{base}/api/stream?run_id={run_id}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            if event["type"] == "permission_request" and on_permission is not None:
                approved = on_permission(event["data"])
                _post(base, "/api/decision", {"run_id": run_id, "approved": approved})
            if event["type"] in ("done", "error"):
                break
    return events


def _run_and_collect(base, task, on_permission=None, timeout=25):
    status, body = _post(base, "/api/run", {"task": task})
    assert status == 200, body
    run_id = body["run_id"]
    box = {}

    def worker():
        box["events"] = _collect_stream(base, run_id, on_permission)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "SSE 收集超时"
    return box["events"]


def test_state_lists_eighteen_tools_grouped_by_access():
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}))
    try:
        status, state = _get_json(base, "/api/state")
        assert status == 200
        tools = state["tools"]
        assert len(tools) == 18
        reads = [t for t in tools if t["access"] == "read"]
        writes = [t for t in tools if t["access"] == "write"]
        assert len(reads) == 10 and len(writes) == 8
        assert {t["name"] for t in writes} == {
            "add_customer", "update_customer", "add_followup", "create_order",
            "create_opportunity", "update_opportunity_stage", "create_todo", "complete_todo",
        }
        assert {t["name"] for t in reads} == {
            "list_customers", "search_customers", "get_customer", "list_orders",
            "sum_amount", "list_opportunities", "list_followups",
            "customers_due_followup", "list_todos", "daily_brief",
        }
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_readonly_task_runs_over_sse():
    crm = _crm()
    workbench, httpd, base = _start_server(
        _OneToolModel("sum_amount", {"category": "硬件", "region": "华东"}), crm
    )
    try:
        events = _run_and_collect(base, "统计华东硬件销售额")
        kinds = [e["type"] for e in events]
        assert "tool_call" in kinds and "tool_result" in kinds
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["data"]["tool"] == "sum_amount"
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["data"]["ok"] is True
        assert "16500" in result["data"]["preview"]
        assert kinds[-1] == "done"
        assert events[-1]["data"]["final_text"] == "任务完成"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_write_tool_approved_over_http_changes_crm():
    crm = _crm()
    workbench, httpd, base = _start_server(
        _OneToolModel("add_followup", {"customer": "周涛", "content": "网页授权测试跟进"}), crm
    )
    try:
        assert len(crm.list_followups("周涛")) == 0
        events = _run_and_collect(base, "给周涛记录一条跟进", on_permission=lambda data: True)
        kinds = [e["type"] for e in events]
        assert "permission_request" in kinds and "tool_call" in kinds
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["data"]["ok"] is True
        followups = crm.list_followups("周涛")
        assert len(followups) == 1
        assert followups[0]["content"] == "网页授权测试跟进"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_write_tool_denied_over_http_leaves_crm_untouched():
    crm = _crm()
    workbench, httpd, base = _start_server(
        _OneToolModel("create_todo", {"title": "不该出现的待办"}), crm
    )
    try:
        before = len(crm.list_todos("all"))
        events = _run_and_collect(base, "新建一个待办", on_permission=lambda data: False)
        assert any(e["type"] == "permission_request" for e in events)
        # DENY 不产生 tool_call / tool_result
        assert not any(e["type"] == "tool_call" for e in events)
        assert len(crm.list_todos("all")) == before
        assert events[-1]["type"] == "done"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_concurrent_run_rejected_but_reset_cancels_busy_run():
    crm = _crm()
    model = _BlockModel()
    workbench, httpd, base = _start_server(model, crm)
    try:
        status, body = _post(base, "/api/run", {"task": "先占住的任务"})
        assert status == 200
        run_id = body["run_id"]
        # 同一时刻第二个任务仍然拒绝
        status2, body2 = _post(base, "/api/run", {"task": "第二个任务"})
        assert status2 == 409 and body2["error"] == "busy"

        collected: list = []
        thread = threading.Thread(
            target=lambda: collected.extend(_collect_stream(base, run_id)), daemon=True
        )
        thread.start()
        for _ in range(50):
            _, state = _get_json(base, "/api/state")
            if state["active"]:
                break
            time.sleep(0.1)
        # 重置不再 409：先终止卡住的任务再重置
        status3, body3 = _post(base, "/api/reset", {})
        assert status3 == 200 and body3["ok"] is True
        thread.join(10)
        assert not thread.is_alive()
        assert collected[-1]["type"] == "cancelled"
        _, state2 = _get_json(base, "/api/state")
        assert not state2["active"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cancel_unblocks_stuck_run_and_reset_succeeds():
    """卡在模型等待（等价于卡在澄清/授权）的任务可被终止，随后重置不再 409。"""
    crm = _crm()
    model = _BlockModel()
    workbench, httpd, base = _start_server(model, crm)
    try:
        status, body = _post(base, "/api/run", {"task": "卡住的任务"})
        assert status == 200
        run_id = body["run_id"]

        collected: list = []
        thread = threading.Thread(
            target=lambda: collected.extend(_collect_stream(base, run_id)), daemon=True
        )
        thread.start()
        for _ in range(50):
            _, state = _get_json(base, "/api/state")
            if state["active"]:
                break
            time.sleep(0.1)
        assert state["active"]

        status2, body2 = _post(base, "/api/cancel", {"run_id": run_id})
        assert status2 == 200 and body2["ok"] is True
        thread.join(10)
        assert not thread.is_alive()
        assert collected[-1]["type"] == "cancelled"

        # 重置会先终止残留任务，这里应当直接成功而不是 409
        status3, body3 = _post(base, "/api/reset", {})
        assert status3 == 200 and body3["ok"] is True
        _, state2 = _get_json(base, "/api/state")
        assert not state2["active"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cancel_unknown_run_returns_404():
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}))
    try:
        status, body = _post(base, "/api/cancel", {"run_id": "nope"})
        assert status == 404 and body["ok"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_snapshot_returns_native_crm_data():
    crm = _crm()
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}), crm)
    try:
        status, snap = _get_json(base, "/api/snapshot")
        assert status == 200
        assert len(snap["customers"]) == 5
        assert len(snap["orders"]) == 6
        assert snap["total_amount"] == 38700
        assert [d["name"] for d in snap["due_followup"]] == ["周涛", "王敏", "李强"]
        assert len(snap["todos"]) == 4
        assert "销售日报" in snap["brief"]
        # 商机管道 + 枚举单一来源
        assert len(snap["opportunities"]) == 4
        assert snap["pipeline_amount"] == 239000
        assert snap["enums"]["stages"] == [
            "初步接触", "需求确认", "方案报价", "谈判", "赢单", "输单"
        ]
        assert set(snap["enums"]["categories"]) == {"硬件", "软件", "服务"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_native_actions_work_without_agent_and_reflect_in_snapshot():
    crm = _crm()
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}), crm)
    try:
        s1, b1 = _post(base, "/api/crm/followup",
                       {"customer": "周涛", "content": "网页原生录入的跟进"})
        assert s1 == 200 and b1["ok"] is True
        s2, b2 = _post(base, "/api/crm/todo", {"title": "网页原生待办", "due": ""})
        assert s2 == 200 and b2["ok"] is True
        s3, b3 = _post(base, "/api/crm/complete", {"title": "网页原生待办"})
        assert s3 == 200 and b3["ok"] is True
        # 业务校验仍由 SalesCrm 负责
        s4, b4 = _post(base, "/api/crm/followup", {"customer": "不存在的人", "content": "x"})
        assert s4 == 200 and b4["ok"] is False

        _, snap = _get_json(base, "/api/snapshot")
        assert any(f["content"] == "网页原生录入的跟进" for f in snap["followups"])
        native_todo = [t for t in snap["todos"] if t["title"] == "网页原生待办"][0]
        assert native_todo["done"] is True
        # 周涛录入跟进后不再超期
        assert all(d["name"] != "周涛" for d in snap["due_followup"])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_native_add_customer_works_without_agent_and_validates():
    """新增客户是软件原生功能（无 Core 可用）；业务校验仍由 SalesCrm 负责。"""
    crm = _crm()
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}), crm)
    try:
        s1, b1 = _post(base, "/api/crm/customer",
                       {"name": "孙琪", "company": "西南测试公司", "level": "重点"})
        assert s1 == 200 and b1["ok"] is True
        # 重名被业务层拒绝
        s2, b2 = _post(base, "/api/crm/customer",
                       {"name": "孙琪", "company": "另一家", "level": "普通"})
        assert s2 == 200 and b2["ok"] is False
        # 空姓名被业务层拒绝
        s3, b3 = _post(base, "/api/crm/customer",
                       {"name": "  ", "company": "某公司", "level": "普通"})
        assert s3 == 200 and b3["ok"] is False

        _, snap = _get_json(base, "/api/snapshot")
        assert any(
            c["name"] == "孙琪" and c["company"] == "西南测试公司" and c["level"] == "重点"
            for c in snap["customers"]
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_native_crm_editor_ops_work_without_agent():
    """客户编辑 / 订单录入 / 商机新建与阶段推进都是软件原生功能（无 Core 可用）；
    业务校验仍由 SalesCrm 负责。"""
    crm = _crm()
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}), crm)
    try:
        # 1) 客户编辑：补电话、改等级与状态
        s1, b1 = _post(base, "/api/crm/customer_edit", {
            "name": "周涛", "company": "华南服务外包园", "phone": "13800000099",
            "level": "重点", "status": "合作中",
        })
        assert s1 == 200 and b1["ok"] is True
        s1b, b1b = _post(base, "/api/crm/customer_edit",
                         {"name": "不存在的人", "status": "合作中"})
        assert s1b == 200 and b1b["ok"] is False

        # 2) 订单录入（用公司名）；金额非法被拒
        s2, b2 = _post(base, "/api/crm/order_create", {
            "customer": "华南服务外包园", "category": "服务", "amount": 12000,
            "region": "华南", "product": "年度运维",
        })
        assert s2 == 200 and b2["ok"] is True
        s2b, b2b = _post(base, "/api/crm/order_create",
                         {"customer": "华南服务外包园", "category": "硬件", "amount": -1})
        assert s2b == 200 and b2b["ok"] is False

        # 3) 新建商机；重名被拒
        s3, b3 = _post(base, "/api/crm/opp_create",
                       {"title": "测试新商机", "customer": "华南服务外包园", "amount": 50000})
        assert s3 == 200 and b3["ok"] is True
        s3b, b3b = _post(base, "/api/crm/opp_create",
                         {"title": "测试新商机", "customer": "华南服务外包园", "amount": 1})
        assert s3b == 200 and b3b["ok"] is False

        # 4) 阶段推进（标题模糊匹配）
        s4, b4 = _post(base, "/api/crm/opp_stage",
                       {"title": "测试新商机", "stage": "需求确认"})
        assert s4 == 200 and b4["ok"] is True

        _, snap = _get_json(base, "/api/snapshot")
        zhou = [c for c in snap["customers"] if c["name"] == "周涛"][0]
        assert zhou["phone"] == "13800000099"
        assert zhou["level"] == "重点" and zhou["status"] == "合作中"
        new_orders = [o for o in snap["orders"] if o.get("product") == "年度运维"]
        assert len(new_orders) == 1 and new_orders[0]["amount"] == 12000
        new_opp = [o for o in snap["opportunities"] if o["title"] == "测试新商机"][0]
        assert new_opp["stage"] == "需求确认" and new_opp["amount"] == 50000
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_native_actions_rejected_while_agent_running():
    crm = _crm()
    model = _BlockModel()
    workbench, httpd, base = _start_server(model, crm)
    try:
        _, body = _post(base, "/api/run", {"task": "占住"})
        for _ in range(50):
            _, state = _get_json(base, "/api/state")
            if state["active"]:
                break
            time.sleep(0.1)
        assert state["active"]
        status, result = _post(base, "/api/crm/todo", {"title": "不该写入"})
        assert status == 409 and result["error"] == "busy"
        _, snap = _get_json(base, "/api/snapshot")
        assert all(t["title"] != "不该写入" for t in snap["todos"])
        # 收尾：终止并重置
        assert _post(base, "/api/cancel", {"run_id": body["run_id"]})[0] == 200
        assert _post(base, "/api/reset", {})[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_not_found_returns_404():
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}))
    try:
        try:
            urllib.request.urlopen(base + "/api/stream?run_id=nope", timeout=5)
            raise AssertionError("应当 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- 按需能力目录：纯函数 + 关键词匹配 ----------

def test_visit_weather_fixed_city_and_rain_advice():
    gz = get_visit_weather("广州")
    assert gz["city"] == "广州" and gz["weather"] == "阵雨" and gz["rain"] is True
    assert "带伞" in gz["advice"]
    zj = get_visit_weather("湛江")
    assert zj["weather"] == "晴" and zj["rain"] is False


def test_visit_weather_unknown_city_is_deterministic():
    a = get_visit_weather("某不存在的城市")
    b = get_visit_weather("某不存在的城市")
    assert a == b  # 同名城市散列兜底，结果稳定


def test_calc_quote_with_tax_basic_and_negative():
    r = calc_quote_with_tax(10000, 13.0)
    assert r["tax"] == 1300.0 and r["amount_incl_tax"] == 11300.0
    assert r["amount_excl_tax"] == 10000 and r["tax_rate_pct"] == 13.0
    bad = calc_quote_with_tax(-5, 13.0)
    assert bad["ok"] is False and "负" in bad["error"]


def test_catalog_summary_lists_two_on_demand_capabilities():
    summary = catalog_summary()
    names = {c["name"] for c in summary}
    assert names == {"get_visit_weather", "calc_quote_with_tax"}
    assert all(c["description"] for c in summary)


def test_catalog_matches_keywords_but_not_unrelated_need():
    async def scenario():
        catalog = build_catalog()
        weather = await catalog.discover(
            "天气查询能力", task="明天去广州拜访要带伞吗", available=[]
        )
        assert [s.name for s in weather] == ["get_visit_weather"]
        tax = await catalog.discover(
            "含税报价计算能力", task="S001 价税合计多少", available=[]
        )
        assert [s.name for s in tax] == ["calc_quote_with_tax"]
        none = await catalog.discover(
            "图像生成能力", task="帮我画一张海报", available=[]
        )
        assert none == []  # 目录里没有的能力永远不会被发现

    asyncio.run(scenario())


def test_state_exposes_on_demand_catalog_separately_from_tools():
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}))
    try:
        _, state = _get_json(base, "/api/state")
        # 默认工具 18 个（候选函数不挂 SalesCrm，不被内省注册）
        assert len(state["tools"]) == 18
        catalog = state["catalog"]
        assert {c["name"] for c in catalog} == {
            "get_visit_weather", "calc_quote_with_tax"
        }
        tool_names = {t["name"] for t in state["tools"]}
        assert "get_visit_weather" not in tool_names  # 未发现前不在默认工具里
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- 端到端：能力缺口 -> 发现 -> 授权 -> 本轮可用（SSE） ----------

class _GapWeatherModel:
    """自报 live-router 的离线模型：分类时报缺口，随后调用被发现的天气工具，最后收尾。"""

    yai_live_router = True

    def __init__(self):
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if tools is None:
            # 第 1 次：LLM 路由分类，判定现有 CRM 工具不足以查天气
            return ModelResponse(
                content=(
                    '{"strategy":"react","tier":"standard",'
                    '"reason":"需要查询拜访城市天气，现有工具无法完成",'
                    '"missing_capability":"天气查询能力"}'
                )
            )
        if not any(m.get("role") == "tool" for m in messages):
            # 第 2 次：天气工具已在本轮被发现注册，直接调用
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="w1", name="get_visit_weather", arguments={"city": "广州"}
                )],
            )
        # 第 3 次：拿到工具结果后收尾
        return ModelResponse(content="广州明天有阵雨，建议带伞并和客户确认行程。")


def test_discovery_loop_over_sse_gap_discover_authorize_execute():
    crm = _crm()
    workbench, httpd, base = _start_server(_GapWeatherModel(), crm)
    try:
        events = _run_and_collect(
            base,
            "明天去广州拜访客户，天气怎么样，要带伞吗？",
            on_permission=lambda data: True,  # 新发现的只读工具首次调用仍需授权
        )
        kinds = [e["type"] for e in events]

        gap = next(e for e in events if e["type"] == "capability_missing")
        assert "天气" in gap["data"]["missing"]
        # 18 个 CRM 业务工具 + 1 个 compose_tool meta-tool（composition=True）
        assert len(gap["data"]["available_tools"]) == 19
        assert "compose_tool" in gap["data"]["available_tools"]

        found = next(e for e in events if e["type"] == "tool_discovered")
        assert found["data"]["registered"] == ["get_visit_weather"]
        assert found["data"]["source"] == "StaticCatalog"

        # 发现不等于授权：新工具不在读白名单，仍弹了授权卡
        assert "permission_request" in kinds
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["data"]["tool"] == "get_visit_weather"
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["data"]["ok"] is True
        assert "阵雨" in result["data"]["preview"] and "带伞" in result["data"]["preview"]

        assert kinds[-1] == "done"
        assert "带伞" in events[-1]["data"]["final_text"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- 架构边界：唯一嵌入点（业务系统零 yai_core 依赖） ----------

def test_crm_and_http_shell_have_no_direct_yai_core_import():
    """crm_app.py 是纯业务系统、web_app.py 是纯 HTTP 壳，都不直接 import yai_core；
    只有 agent_bridge.py（唯一嵌入点）直接装配 Core。"""
    host_dir = ROOT / "examples" / "host_e_sales_crm"
    crm_src = (host_dir / "crm_app.py").read_text(encoding="utf-8")
    web_src = (host_dir / "web_app.py").read_text(encoding="utf-8")
    bridge_src = (host_dir / "agent_bridge.py").read_text(encoding="utf-8")
    yai_import = re.compile(r"^\s*(from\s+yai_core|import\s+yai_core)\b", re.M)
    # 纯业务系统：连 yai_core 字样都不应出现
    assert "yai_core" not in crm_src
    # HTTP 壳：docstring 可以提及，但不能有 import 语句
    assert not yai_import.search(web_src)
    # 唯一嵌入点：直接装配 Core
    assert yai_import.search(bridge_src)


# ---------- Core 开关：一键对比"有无 Core" ----------

def test_core_toggle_blocks_run_but_native_crm_keeps_working():
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}))
    try:
        _, state = _get_json(base, "/api/state")
        assert state["core_enabled"] is True

        # 关闭 Core：纯 CRM 形态，AI 任务被拒
        status, body = _post(base, "/api/core", {"enabled": False})
        assert status == 200 and body["core_enabled"] is False
        _, state_off = _get_json(base, "/api/state")
        assert state_off["core_enabled"] is False
        status2, body2 = _post(base, "/api/run", {"task": "统计销售额"})
        assert status2 == 503 and body2["error"] == "core_disabled"

        # 原生 CRM 功能完全不受 Core 关闭影响
        s3, b3 = _post(base, "/api/crm/followup",
                       {"customer": "周涛", "content": "无 Core 时原生录入"})
        assert s3 == 200 and b3["ok"] is True
        _, snap = _get_json(base, "/api/snapshot")
        assert any(f["content"] == "无 Core 时原生录入" for f in snap["followups"])

        # 重新开启 Core：数字员工恢复
        s4, b4 = _post(base, "/api/core", {"enabled": True})
        assert s4 == 200 and b4["core_enabled"] is True
        events = _run_and_collect(base, "统计华东硬件销售额")
        assert events[-1]["type"] == "done"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_disabling_core_cancels_active_run():
    """在任务执行中关闭 Core，会先终止任务，避免"纯 CRM"形态下后台继续改写数据。"""
    crm = _crm()
    model = _BlockModel()
    workbench, httpd, base = _start_server(model, crm)
    try:
        _, body = _post(base, "/api/run", {"task": "占住的任务"})
        run_id = body["run_id"]
        collected: list = []
        thread = threading.Thread(
            target=lambda: collected.extend(_collect_stream(base, run_id)), daemon=True
        )
        thread.start()
        for _ in range(50):
            _, state = _get_json(base, "/api/state")
            if state["active"]:
                break
            time.sleep(0.1)
        assert state["active"]

        status, resp = _post(base, "/api/core", {"enabled": False})
        assert status == 200 and resp["core_enabled"] is False
        thread.join(10)
        assert not thread.is_alive()
        assert collected[-1]["type"] == "cancelled"
        _, state2 = _get_json(base, "/api/state")
        assert not state2["active"] and state2["core_enabled"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_start_in_core_off_mode_rejects_run():
    """命令行 --core-off 启动（纯 CRM）时，AI 任务直接 503，直到显式开启。"""
    crm = _crm()
    workbench = Workbench(
        model_factory=lambda: _OneToolModel("sum_amount", {}),
        model_label="离线测试模型", crm=crm, core_enabled=False,
    )
    httpd = create_server(workbench, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, body = _post(base, "/api/run", {"task": "统计"})
        assert status == 503 and body["error"] == "core_disabled"
        # 原生功能照常
        s, b = _post(base, "/api/crm/todo", {"title": "纯 CRM 待办", "due": ""})
        assert s == 200 and b["ok"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- 权限三挡：全部审批 / 部分审批 / 无需审批 ----------

def test_manual_mode_asks_even_for_read_tool():
    """全部审批：连读工具（默认白名单内）都要先问人。"""
    workbench, httpd, base = _start_server(_OneToolModel("list_customers", {}))
    try:
        status, body = _post(base, "/api/permission", {"mode": "manual"})
        assert status == 200 and body["permission_mode"] == "manual"
        events = _run_and_collect(
            base, "列出全部客户", on_permission=lambda data: True
        )
        assert any(e["type"] == "permission_request" for e in events)
        assert events[-1]["type"] == "done"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_auto_mode_runs_write_tool_without_prompt():
    """无需审批：写工具也直接执行，不弹授权卡（仅本地演示）。"""
    crm = _crm()
    workbench, httpd, base = _start_server(
        _OneToolModel("create_todo", {"title": "自动放行待办"}), crm
    )
    try:
        status, _ = _post(base, "/api/permission", {"mode": "auto"})
        assert status == 200
        events = _run_and_collect(base, "新建一个待办")  # 不传 on_permission
        assert not any(e["type"] == "permission_request" for e in events)
        assert any(t["title"] == "自动放行待办" for t in crm.list_todos("all"))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_partial_mode_is_default_and_asks_for_write_only():
    """默认 partial：读工具放行、写工具询问。"""
    workbench, httpd, base = _start_server(
        _OneToolModel("sum_amount", {"category": "硬件", "region": "华东"})
    )
    try:
        _, state = _get_json(base, "/api/state")
        assert state["permission_mode"] == "partial"
        events = _run_and_collect(base, "统计华东硬件销售额")
        assert not any(e["type"] == "permission_request" for e in events)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_bad_permission_mode_returns_400():
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}))
    try:
        status, body = _post(base, "/api/permission", {"mode": "weird"})
        assert status == 400 and body["error"] == "bad_mode"
        _, state = _get_json(base, "/api/state")
        assert state["permission_mode"] == "partial"  # 未被篡改
    finally:
        httpd.shutdown()
        httpd.server_close()
