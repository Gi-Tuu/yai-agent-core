"""host_e 网页工作台：WebChannel 授权交互 + 标准库 HTTP/SSE 端到端，全部离线测试。"""

import asyncio
import json
import queue
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


def test_state_lists_twelve_tools_grouped_by_access():
    workbench, httpd, base = _start_server(_OneToolModel("sum_amount", {}))
    try:
        status, state = _get_json(base, "/api/state")
        assert status == 200
        tools = state["tools"]
        assert len(tools) == 12
        reads = [t for t in tools if t["access"] == "read"]
        writes = [t for t in tools if t["access"] == "write"]
        assert len(reads) == 8 and len(writes) == 4
        assert {t["name"] for t in writes} == {
            "add_customer", "add_followup", "create_todo", "complete_todo"
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
