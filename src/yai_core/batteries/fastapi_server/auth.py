"""API Key 鉴权（v0.7，纯标准库）：opt-in，保护 /v1/*，评审硬门槛路径保持公开。

安全默认：未设置 YAI_API_KEY / YAI_API_KEYS 时鉴权**关闭**（本地开发与评审零门槛、
行为与旧版一致）。设置后，受保护路径要求以下任一头部携带正确 Key，否则返回 401、
请求不进 core（不花模型钱）：

- ``Authorization: Bearer <key>``（也容忍裸 key）
- ``X-API-Key: <key>``

即使开启鉴权，``/health`` 与 ``/.well-known/xagent-verification.json`` 也必须无条件
公开（X-Agent 技术验证硬门槛），因此列入公开白名单、跳过校验。Key 比较用
``hmac.compare_digest`` 做常量时间比较，避免按长度/前缀泄露。
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field

#: 开启鉴权也必须无条件公开的路径（X-Agent 技术验证硬门槛）。
PUBLIC_PATHS = frozenset({"/health", "/.well-known/xagent-verification.json"})
_PROTECTED_PREFIX = "/v1/"

_API_KEY_ENV = "YAI_API_KEY"
_API_KEYS_ENV = "YAI_API_KEYS"


@dataclass
class ApiKeyAuth:
    """已配置的 API Key 集合；keys 为空表示鉴权关闭（全公开）。"""

    keys: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ApiKeyAuth:
        """从环境变量构造：单个 YAI_API_KEY 或逗号分隔的 YAI_API_KEYS。"""
        environ = env if env is not None else os.environ
        raw: list[str] = []
        single = (environ.get(_API_KEY_ENV) or "").strip()
        if single:
            raw.append(single)
        raw.extend(k.strip() for k in (environ.get(_API_KEYS_ENV) or "").split(",") if k.strip())
        # 去重保序
        seen: set[str] = set()
        keys: list[str] = []
        for k in raw:
            if k not in seen:
                seen.add(k)
                keys.append(k)
        return cls(tuple(keys))

    @property
    def enabled(self) -> bool:
        return bool(self.keys)

    def should_protect(self, path: str) -> bool:
        """只保护 /v1/ 下且不在公开白名单的路径；其余一律放行。"""
        return path.startswith(_PROTECTED_PREFIX) and path not in PUBLIC_PATHS

    def check_header(
        self, authorization: str | None, x_api_key: str | None
    ) -> bool:
        """校验头部中的 Key；任何形式缺失/不匹配返回 False。"""
        presented = ""
        if authorization:
            parts = authorization.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                presented = parts[1].strip()
            elif len(parts) == 1:
                presented = parts[0].strip()
        if not presented and x_api_key:
            presented = x_api_key.strip()
        if not presented:
            return False
        return any(hmac.compare_digest(presented, k) for k in self.keys)
