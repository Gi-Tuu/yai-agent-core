"""host_e 跨进程工具口的令牌鉴权回归（全程离线，不需要 Qt / 网络 / Key）。

覆盖两层：
- 纯函数：环回判定、Bearer 比较、启动守卫、令牌解析优先级；
- HTTP：无令牌 / 错令牌 → 401 且**不执行任何业务方法**；带令牌 → 正常。
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

from host_e_sales_crm import tool_auth  # noqa: E402
from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402
from host_e_sales_crm.web_app import Workbench, create_server  # noqa: E402

FIXED = date(2026, 9, 18)
TOKEN = "a-test-token-1234567890"


class _NoopModel:
    async def achat(self, messages, tools=None, *, tier="standard"):  # pragma: no cover
        raise AssertionError("工具端点鉴权测试不应触发模型调用")


def _crm() -> SalesCrm:
    return SalesCrm(data_path=None, clock=lambda: FIXED, persist=False)


@pytest.fixture()
def server():
    crm = _crm()
    workbench = Workbench(model_factory=lambda: _NoopModel(), model_label="离线", crm=crm)
    httpd = create_server(workbench, "127.0.0.1", 0, TOKEN)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield crm, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def _post(base, arguments, headers=None):
    req = urllib.request.Request(
        base + "/api/agent/tool",
        data=json.dumps({"name": "list_customers", "arguments": arguments}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ---------- 环回判定与启动守卫 ----------

@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.53"])
def test_loopback_hosts_accepted(host: str) -> None:
    assert tool_auth.is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.7", "example.com", ""])
def test_non_loopback_hosts_rejected(host: str) -> None:
    assert tool_auth.is_loopback(host) is False


def test_public_bind_without_explicit_token_refuses_start(monkeypatch) -> None:
    monkeypatch.delenv(tool_auth.TOKEN_ENV, raising=False)
    assert tool_auth.startup_guard("0.0.0.0", None)
    assert tool_auth.startup_guard("192.168.1.7", "")


def test_public_bind_with_explicit_token_allowed() -> None:
    assert tool_auth.startup_guard("0.0.0.0", "explicit-token") is None


def test_loopback_needs_no_explicit_token(monkeypatch) -> None:
    monkeypatch.delenv(tool_auth.TOKEN_ENV, raising=False)
    assert tool_auth.startup_guard("127.0.0.1", None) is None


# ---------- Bearer 比较 ----------

def test_token_matches_accepts_exact_bearer() -> None:
    assert tool_auth.token_matches(f"Bearer {TOKEN}", TOKEN) is True
    # scheme 大小写不敏感
    assert tool_auth.token_matches(f"bearer {TOKEN}", TOKEN) is True


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer wrong", f"Basic {TOKEN}", "Bearer", f"Bearer {TOKEN} extra"],
)
def test_token_matches_rejects_bad_headers(header: str | None) -> None:
    assert tool_auth.token_matches(header, TOKEN) is False


def test_token_matches_rejects_empty_expected() -> None:
    # 服务端没配令牌时不能"空对空"放行
    assert tool_auth.token_matches(f"Bearer {TOKEN}", "") is False


# ---------- 令牌解析 ----------

def test_resolve_token_priority(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(tool_auth.TOKEN_ENV, "from-env")
    assert tool_auth.resolve_token("explicit") == "explicit"
    assert tool_auth.resolve_token(None) == "from-env"
    monkeypatch.delenv(tool_auth.TOKEN_ENV)
    target = tmp_path / "tok"
    assert tool_auth.resolve_token(None, allow_file=False) == ""
    assert tool_auth.load_or_create_token(target) != ""
    monkeypatch.setattr(tool_auth, "TOKEN_FILE", target)
    assert tool_auth.resolve_token(None) == target.read_text(encoding="utf-8").strip()


def test_generated_token_is_stable_and_unique(tmp_path: Path) -> None:
    a = tool_auth.load_or_create_token(tmp_path / "t")
    assert tool_auth.load_or_create_token(tmp_path / "t") == a  # 复用不重生成
    assert tool_auth.load_or_create_token(tmp_path / "other") != a
    assert len(a) >= 32


# ---------- HTTP：401 且零副作用 ----------

def test_post_without_token_is_401_and_runs_nothing(server) -> None:
    crm, base = server
    before = len(crm.list_customers())
    status, body = _post(base, {}, headers=None)
    assert status == 401
    assert body["error"] == "unauthorized"
    # 未授权请求不得触达业务方法
    assert len(crm.list_customers()) == before


def test_post_with_wrong_token_is_401(server) -> None:
    status, body = _post(server[1], {}, headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_post_with_token_succeeds(server) -> None:
    status, body = _post(
        server[1], {}, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert status == 200
    assert body["ok"] is True and len(body["result"]) == 5


def test_tools_listing_also_requires_token(server) -> None:
    # 白名单清单本身就是攻击面信息
    assert _get(server[1], "/api/agent/tools") == 401
    assert _get(
        server[1], "/api/agent/tools", {"Authorization": f"Bearer {TOKEN}"}
    ) == 200


def test_business_endpoints_stay_open(server) -> None:
    # 纯 CRM 自己的页面/接口不受影响（它是普通软件，不是 Agent 口）
    assert _get(server[1], "/api/state") == 200


# ---------- 两侧令牌约定不得漂移 ----------

def test_client_and_server_share_token_convention() -> None:
    source = (ROOT / "shell" / "desktop" / "crm_client.py").read_text(encoding="utf-8")
    assert '"data" / "host_e" / "tool_token"' in source
    assert tool_auth.TOKEN_ENV in source
