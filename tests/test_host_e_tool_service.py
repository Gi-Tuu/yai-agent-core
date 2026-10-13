"""host_e 跨进程工具服务（ToolService）单元测试 + /api/agent/tool HTTP 契约测试。

全程离线：不起灵动岛、不需要 PySide6 / 网络 / API Key。
- ToolService 是 host_e 进程内的白名单反射服务，纯标准库、不 import yai_core；
- HTTP 端点 /api/agent/tool 供桌面灵动岛销售专员跨进程调用；
- 三层错误语义：业务校验失败 → 200 内嵌 result.ok=false（交给模型自纠）；
  未知工具 → 404；参数错误 / 非对象入参 → 400。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402
from host_e_sales_crm.tool_service import (  # noqa: E402
    DISCOVERY_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    ToolArgumentError,
    ToolService,
    UnknownToolError,
)
from host_e_sales_crm.web_app import Workbench, create_server  # noqa: E402

FIXED = date(2026, 9, 18)


def _crm() -> SalesCrm:
    return SalesCrm(data_path=None, clock=lambda: FIXED, persist=False)


class _NoopModel:
    """跨进程工具端点不会调用模型，构造 Workbench 仅需一个占位工厂。"""

    async def achat(self, messages, tools=None, *, tier="standard"):  # pragma: no cover
        raise AssertionError("纯工具端点不应触发模型调用")


# ---------- ToolService：白名单反射与参数收敛 ----------

def test_whitelist_counts_match_crm_contract():
    svc = ToolService(_crm())
    assert len(READ_TOOLS) == 10
    assert len(WRITE_TOOLS) == 12
    assert len(DISCOVERY_TOOLS) == 2
    assert len(svc.describe()) == 24
    groups = {row["group"] for row in svc.describe()}
    assert groups == {"read", "write", "discovery"}
    assert svc.is_write("create_order") and not svc.is_write("list_customers")
    assert "create_order" in svc.write_tools
    # 按需能力（天气/含税）不是写工具
    assert not svc.is_write("calc_quote_with_tax")


def test_read_and_write_tools_call_real_business_methods():
    crm = _crm()
    svc = ToolService(crm)
    customers = svc.call("list_customers")
    assert isinstance(customers, list) and len(customers) == 5
    # 写工具真实落到同一个 SalesCrm
    added = svc.call("add_customer", {"name": "孙琪", "company": "跨进程测试公司"})
    assert added.get("ok") is True
    assert any(c["name"] == "孙琪" for c in crm.list_customers())


def test_discovery_tools_are_reachable():
    svc = ToolService(_crm())
    quote = svc.call("calc_quote_with_tax", {"amount": 10000, "rate": 13})
    assert quote["tax"] == 1300.0 and quote["amount_incl_tax"] == 11300.0
    weather = svc.call("get_visit_weather", {"city": "广州"})
    assert weather["city"] == "广州"


def test_unknown_tool_and_non_object_arguments_rejected():
    svc = ToolService(_crm())
    with pytest.raises(UnknownToolError):
        svc.call("__init__", {})  # 私有方法永远不可达
    with pytest.raises(UnknownToolError):
        svc.call("drop_everything", {})
    with pytest.raises(ToolArgumentError):
        svc.call("list_customers", ["not", "a", "dict"])
    with pytest.raises(ToolArgumentError):
        svc.call("list_customers", "not-a-dict")


def test_parameter_coercion_and_extra_args_ignored():
    svc = ToolService(_crm())
    # JSON 文本数字按参数名收敛为 int / float
    due = svc.call("customers_due_followup", {"days": "3"})
    assert isinstance(due, list)
    quote = svc.call("calc_quote_with_tax", {"amount": 1000, "rate": "13"})
    assert quote["tax_rate_pct"] == 13.0
    # 无法转换的数字参数报错
    with pytest.raises(ToolArgumentError):
        svc.call("customers_due_followup", {"days": "abc"})
    # 多余参数被忽略，缺省参数走默认值
    assert isinstance(svc.call("list_customers", {"level": "", "bogus": 1}), list)


def test_business_validation_dict_passthrough_and_raised_error():
    """业务层有两种校验形态，ToolService 都要忠实透传：
    - 自返 ok:false 字典（如 create_order 金额非法）→ 原样返回；
    - 直接抛 ValueError（如 list_todos 非法状态）→ 不吞，交给 HTTP 层统一。
    """
    svc = ToolService(_crm())
    bad_order = svc.call("create_order", {
        "customer": "周涛", "category": "硬件", "amount": -1,
    })
    assert isinstance(bad_order, dict) and bad_order.get("ok") is False
    assert bad_order.get("error")
    with pytest.raises(ValueError):
        svc.call("list_todos", {"status": "bogus"})


# ---------- HTTP /api/agent/tool：跨进程契约 ----------

#: 本文件内所有 HTTP 用例共用的工具口令牌。
TOKEN = "test-tool-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

def _start_tool_server(crm: SalesCrm | None = None):
    crm = crm or _crm()
    workbench = Workbench(model_factory=lambda: _NoopModel(), model_label="离线", crm=crm)
    httpd = create_server(workbench, "127.0.0.1", 0, TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    return crm, httpd, base


def _post_tool(base, name, arguments):
    payload = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/agent/tool", data=payload,
        headers={"Content-Type": "application/json", **AUTH}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_json(base, path):
    req = urllib.request.Request(base + path, headers=AUTH)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_tools_catalog_endpoint_lists_all_whitelisted():
    crm, httpd, base = _start_tool_server()
    try:
        status, body = _get_json(base, "/api/agent/tools")
        assert status == 200
        assert len(body["tools"]) == 24
        assert {row["name"] for row in body["tools"] if row["group"] == "discovery"} == set(
            DISCOVERY_TOOLS
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_read_write_and_business_failure_semantics():
    crm, httpd, base = _start_tool_server()
    try:
        # 读工具 200
        s1, b1 = _post_tool(base, "list_customers", {})
        assert s1 == 200 and b1["ok"] is True and len(b1["result"]) == 5
        # 写工具 200 且真实落库
        s2, b2 = _post_tool(base, "create_todo", {"title": "跨进程待办", "due": ""})
        assert s2 == 200 and b2["ok"] is True
        assert any(t["title"] == "跨进程待办" for t in crm.list_todos("all"))
        # 业务校验失败：HTTP 仍 200，result 内 ok=false（交给模型自我纠正）
        s3, b3 = _post_tool(base, "create_order", {
            "customer": "周涛", "category": "硬件", "amount": -1,
        })
        assert s3 == 200 and b3["ok"] is True
        assert b3["result"].get("ok") is False and b3["result"].get("error")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_unknown_tool_404_bad_arguments_400():
    crm, httpd, base = _start_tool_server()
    try:
        s1, b1 = _post_tool(base, "not_a_tool", {})
        assert s1 == 404 and b1["error"] == "unknown_tool"
        # 无法收敛的参数类型
        s2, b2 = _post_tool(base, "customers_due_followup", {"days": "abc"})
        assert s2 == 400 and b2["error"] == "bad_arguments"
        # arguments 不是对象
        s3, b3 = _post_tool(base, "list_customers", [1, 2])
        assert s3 == 400 and b3["error"] == "bad_arguments"
    finally:
        httpd.shutdown()
        httpd.server_close()
