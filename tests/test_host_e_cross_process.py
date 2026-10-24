"""灵动岛销售专员（HostECrmClient）↔ host_e 跨进程 HTTP 端到端测试。

验证桌面灵动岛作为独立进程，经 127.0.0.1 操控纯 CRM 宿主：
- 读 / 写 / 按需能力全部真实往返，写操作落到宿主同一个 SalesCrm；
- 业务失败以 ok:false 结果返回（不抛，供模型自纠），协议错误（404/400）才抛；
- 宿主不在线时 ping 为 False，调用给出可操作的中文启动提示；
- 按需能力目录关键词与进程内 catalog 同源。

需要 PySide6（导入 shell.desktop 包会连带加载桌面端）；未装 ``[desktop]``
extra 时整体跳过。全程离线，不访问公网 / API Key。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import urllib.request
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

from shell.desktop.crm_client import HostECrmClient, build_crm_catalog  # noqa: E402

from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402
from host_e_sales_crm.web_app import Workbench, create_server  # noqa: E402

FIXED = date(2026, 9, 18)


class _NoopModel:
    async def achat(self, messages, tools=None, *, tier="standard"):  # pragma: no cover
        raise AssertionError("跨进程工具端点不应触发模型调用")


def _crm() -> SalesCrm:
    return SalesCrm(data_path=None, clock=lambda: FIXED, persist=False)


@pytest.fixture()
def live_pair():
    """启动一个纯 CRM host_e（随机端口），返回 (宿主 CRM, 客户端, 清理函数)。"""
    crm = _crm()
    workbench = Workbench(model_factory=lambda: _NoopModel(), model_label="离线", crm=crm)
    token = "test-tool-token"
    httpd = create_server(workbench, "127.0.0.1", 0, token)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    client = HostECrmClient(base_url=base, timeout=10.0, token=token)
    yield crm, client
    httpd.shutdown()
    httpd.server_close()


def test_selection_state_reads_through_agent_endpoint(live_pair):
    _, client = live_pair
    assert client._selection() == {"selected_customer": None}

    payload = json.dumps({"name": "王敏"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        client.base_url + "/api/selection",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        assert resp.status == 200
    assert client._selection()["selected_customer"] == "王敏"


def test_ping_and_read_tools_roundtrip(live_pair):
    crm, client = live_pair
    assert client.ping() is True
    assert len(client.list_customers()) == 5
    assert client.sum_amount() == 38700
    assert len(client.list_orders(category="硬件")) >= 1
    # 字符串数字参数经服务端收敛
    assert isinstance(client.customers_due_followup(days="3"), list)


def test_write_tools_roundtrip_and_land_in_host_crm(live_pair):
    crm, client = live_pair
    added = client.add_customer("孙琪", "跨进程客户公司", "重点")
    assert added.get("ok") is True
    client.create_todo("跨进程待办", "")
    # 宿主进程的同一个 SalesCrm 立即可见（灵动岛写入 → 网页快照轮询会刷新）
    assert any(c["name"] == "孙琪" for c in crm.list_customers())
    assert any(t["title"] == "跨进程待办" for t in crm.list_todos("all"))
    detail = client.get_customer("孙琪")
    assert detail["company"] == "跨进程客户公司"


def test_on_demand_capabilities_roundtrip(live_pair):
    crm, client = live_pair
    quote = client.calc_quote_with_tax(10000, 13.0)
    assert quote["amount_incl_tax"] == 11300.0
    weather = client.get_visit_weather("广州")
    assert weather["city"] == "广州" and "advice" in weather


def test_business_failure_returns_result_not_raise(live_pair):
    crm, client = live_pair
    # 金额非法：服务端转成 200 内嵌 ok:false，客户端原样交回给模型自我纠正
    result = client.create_order("周涛", "硬件", -1)
    assert isinstance(result, dict) and result.get("ok") is False and result.get("error")


def test_protocol_errors_raise_runtime_error(live_pair):
    crm, client = live_pair
    # 未知工具 → 404
    with pytest.raises(RuntimeError, match="不支持工具"):
        client._call("definitely_not_a_tool")
    # 参数无法转换 → 400
    with pytest.raises(RuntimeError, match="参数有误"):
        client.customers_due_followup(days="not-a-number")


def test_offline_host_ping_false_and_call_hints_startup():
    # 指向一个无人监听的端口（由 OS 拒绝连接）
    client = HostECrmClient(base_url="http://127.0.0.1:1", timeout=2.0)
    assert client.ping() is False
    with pytest.raises(RuntimeError, match="web_app.py"):
        client.list_customers()


def test_cross_process_catalog_shares_keywords_with_inproc(live_pair):
    crm, client = live_pair

    async def discover():
        catalog = build_crm_catalog(client)
        weather = await catalog.discover("天气查询能力", task="明天广州拜访要带伞吗", available=[])
        tax = await catalog.discover("含税报价能力", task="算一下价税合计", available=[])
        return [s.name for s in weather], [s.name for s in tax]

    weather, tax = asyncio.run(discover())
    assert weather == ["get_visit_weather"]
    assert tax == ["calc_quote_with_tax"]
