"""host_e 跨进程工具口的本地令牌鉴权。

**为什么必须有这一层**：``POST /api/agent/tool`` 能执行宿主的**写操作**，而权限三档
的决策在调用方（桌面灵动岛）完成。服务端若谁都能调，等于同机任何进程都能绕过审批
直接改业务数据——上层"主体身份 / 治理元数据 / 审批"全部形同虚设。

**口径与边界（诚实声明）**：

- 令牌存放在当前用户可读的本地文件里。**能读到该文件的进程即被视为同一用户身份**：
  这一层挡的是"同机其他用户/服务、误接入的客户端、以及把端口暴露到非环回后的
  远程调用"，**不防同一用户自己写的恶意本地进程**（那要靠 OS 级隔离）；
- 绑定到非环回地址时**必须**由运维显式提供令牌（``--tool-token`` 或环境变量
  ``YAI_TOOL_TOKEN``），不接受自动生成的文件令牌，否则拒绝启动；
- 比较用 ``hmac.compare_digest`` 常量时间比较，避免按时序猜令牌。

令牌文件路径是与 ``shell/desktop/crm_client.py`` 之间的**共享约定**，两侧各写一份
常量，由 ``tests/test_host_e_tool_auth.py`` 断言二者一致以防漂移。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

#: 仓库根目录（本文件位于 examples/host_e_sales_crm/ 下两级）。
ROOT = Path(__file__).resolve().parents[2]

#: 自动令牌文件的默认位置（``data/`` 已在 .gitignore 内）。
TOKEN_FILE = ROOT / "data" / "host_e" / "tool_token"

#: 显式令牌的环境变量名。
TOKEN_ENV = "YAI_TOOL_TOKEN"

#: 环回地址集合：只有这些地址允许使用自动生成的本地令牌。
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_loopback(host: str) -> bool:
    """是否为环回绑定（未指定主机视为环回）。"""
    return (host or "").strip() in _LOOPBACK or (host or "").startswith("127.")


def load_or_create_token(path: Path | None = None) -> str:
    """读取本地令牌文件；不存在则生成 32 字节 URL 安全令牌并落盘。"""
    target = path or TOKEN_FILE
    if target.is_file():
        value = target.read_text(encoding="utf-8").strip()
        if value:
            return value
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    target.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass  # Windows 上权限语义有限，失败不影响功能
    return token


def resolve_token(explicit: str | None = None, *, allow_file: bool = True) -> str:
    """按 显式参数 → 环境变量 → 本地令牌文件 的优先级取令牌。"""
    if explicit:
        return explicit.strip()
    env = os.getenv(TOKEN_ENV, "").strip()
    if env:
        return env
    return load_or_create_token() if allow_file else ""


def token_matches(header: str | None, expected: str) -> bool:
    """校验 ``Authorization: Bearer <token>`` 头是否匹配期望令牌。"""
    if not expected or not header:
        return False
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1], expected)


def startup_guard(host: str, explicit_token: str | None) -> str | None:
    """非环回绑定却没有显式令牌时返回错误说明（调用方据此拒绝启动）。"""
    if is_loopback(host) or (explicit_token or "").strip() or os.getenv(TOKEN_ENV, ""):
        return None
    return (
        f"绑定到非环回地址 {host!r} 必须显式提供工具令牌"
        f"（--tool-token 或环境变量 {TOKEN_ENV}）；"
        "自动生成的本地令牌文件不足以支撑对外暴露。"
    )
