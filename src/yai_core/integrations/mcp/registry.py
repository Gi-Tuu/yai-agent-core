"""Official MCP Registry 发现：只读搜索 + 显式构建连接配置。

官方 registry（https://registry.modelcontextprotocol.io/v0/servers）只提供**元数据**。
这里刻意不做三件事：

- 不自动安装包、不执行 registry 里的安装命令；
- 不自动连接任何 server；`build_config()` 必须由宿主显式选择 remote 并补变量/头；
- 不把 registry 描述当可信文本：所有标题/描述都经 ``sanitize_untrusted_text``。

安全边界详见 ``docs/guides/mcp-registry.md``。本模块只用标准库，内核依赖仍为空。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from yai_core.integrations.mcp.client import McpServerConfig
from yai_core.tools.hygiene import sanitize_untrusted_text

DEFAULT_BASE_URL = "https://registry.modelcontextprotocol.io"
SEARCH_PATH = "/v0/servers"
_MAX_RESPONSE_BYTES = 2_000_000


@dataclass(frozen=True)
class RegistryHeader:
    name: str
    description: str = ""
    required: bool = False
    secret: bool = False
    default: str | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegistryRemote:
    type: str
    url: str
    variables: dict[str, dict[str, Any]] = field(default_factory=dict)
    headers: tuple[RegistryHeader, ...] = ()

    @property
    def is_https(self) -> bool:
        return self.url.lower().startswith("https://")

    @property
    def is_template(self) -> bool:
        return bool(re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", self.url))

    @property
    def required_variables(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, spec in self.variables.items()
            if isinstance(spec, dict) and spec.get("isRequired")
        )

    @property
    def required_headers(self) -> tuple[str, ...]:
        return tuple(header.name for header in self.headers if header.required)


@dataclass(frozen=True)
class RegistryPackage:
    registry_type: str
    identifier: str
    version: str = ""
    transport: str = ""
    runtime_hint: str = ""


@dataclass(frozen=True)
class RegistryServer:
    name: str
    title: str
    description: str
    version: str
    repository_url: str = ""
    remotes: tuple[RegistryRemote, ...] = ()
    packages: tuple[RegistryPackage, ...] = ()
    published_at: str = ""
    is_latest: bool = False
    status: str = "active"
    #: canonical entry SHA-256（由 registry 搜索结果填充，用于 build_config 复核）
    entry_sha256: str = ""

    @property
    def safe_description(self) -> str:
        return sanitize_untrusted_text(self.description, max_chars=300)

    def streamable_remotes(self) -> tuple[RegistryRemote, ...]:
        return tuple(remote for remote in self.remotes if remote.type == "streamable-http")

    def build_config(
        self,
        *,
        alias: str | None = None,
        remote_index: int = 0,
        variables: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        token_env: str | None = None,
        policy: RegistryPolicy | None = None,
    ) -> McpServerConfig:
        """把一个 remote 转成 ``McpServerConfig``；不发起任何网络请求。

        只接受 ``https`` + ``streamable-http``；模板变量和必填头必须由调用方显式提供，
        缺失直接抛 ``ValueError``，绝不猜默认值、绝不把 secret 写进代码。
        """
        if policy is not None:
            policy.check_server(self)
            pins = dict(policy.pinned_digests)
            expected = pins.get(self.name)
            if expected is None:
                if policy.require_pinned:
                    raise ValueError(
                        f"{self.name}: no pinned digest for this server"
                    )
            elif not self.entry_sha256 or self.entry_sha256.lower() != expected.lower():
                raise ValueError(f"{self.name}: registry entry digest mismatch")
        remotes = self.streamable_remotes()
        if not remotes:
            raise ValueError(f"{self.name}: no streamable-http remote")
        if remote_index < 0 or remote_index >= len(remotes):
            raise ValueError(f"{self.name}: remote_index out of range ({remote_index})")
        remote = remotes[remote_index]
        if not remote.is_https:
            raise ValueError(f"{self.name}: refuses non-HTTPS remote {remote.url!r}")
        if policy is not None:
            host = urlparse(remote.url).hostname or ""
            if not policy.allows_host(host):
                raise ValueError(f"{self.name}: remote host {host!r} is not in allowed_domains")

        supplied_variables = variables or {}
        missing_vars = [
            name for name in remote.required_variables if not supplied_variables.get(name)
        ]
        if missing_vars:
            raise ValueError(f"{self.name}: missing required URL variables: {missing_vars}")
        url = remote.url
        for name, value in supplied_variables.items():
            url = url.replace("{" + name + "}", value)
        if re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", url):
            raise ValueError(f"{self.name}: unresolved URL template: {url!r}")
        # 变量替换后必须重新解析：模板 host 合法不代表最终 host 合法。
        try:
            parsed_url = urlparse(url)
            final_host = parsed_url.hostname or ""
        except ValueError as exc:
            raise ValueError(f"{self.name}: invalid remote URL {url!r}") from exc
        if parsed_url.scheme != "https" or not final_host:
            raise ValueError(f"{self.name}: remote URL must be HTTPS with a host: {url!r}")
        if parsed_url.username or parsed_url.password:
            raise ValueError(f"{self.name}: remote URL must not contain credentials")
        if policy is not None and not policy.allows_host(final_host):
            raise ValueError(f"{self.name}: remote host {final_host!r} is not in allowed_domains")

        supplied_headers = {key.lower(): value for key, value in (headers or {}).items()}
        resolved_headers: dict[str, str] = {}
        for header in remote.headers:
            header_value: str | None = supplied_headers.get(header.name.lower())
            if header_value is None and not header.secret:
                header_value = header.default
            if header.required and not header_value:
                raise ValueError(f"{self.name}: missing required header {header.name!r}")
            if header_value is not None:
                resolved_headers[header.name] = header_value

        safe_alias = alias or re.sub(r"[^A-Za-z0-9_-]+", "_", self.name).strip("_") or "registry"
        return McpServerConfig(
            alias=safe_alias,
            url=url,
            headers=resolved_headers or None,
            token_env=token_env,
        )



def entry_digest(entry: dict[str, Any]) -> str:
    """SHA-256 of a canonical registry entry (for host-side pinning)."""
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RegistryPolicy:
    """Host-side trust policy for registry metadata.

    ``allowed_domains`` restricts connectable remote hosts (exact or subdomain).
    Empty means "no host restriction" only when the policy is constructed directly;
    use :meth:`strict` for the safe posture.
    """

    allowed_domains: tuple[str, ...] = ()
    allowed_repository_hosts: tuple[str, ...] = ()
    require_repository: bool = False
    require_latest: bool = True
    require_active: bool = True
    #: Optional host-side pins: (server name, sha256 of canonical entry).
    pinned_digests: tuple[tuple[str, str], ...] = ()
    require_pinned: bool = False
    #: Optional signature/provenance hook: receives the raw registry entry.
    #: Return False to drop it. This module ships no crypto implementation.
    verify_entry: Callable[[dict[str, Any]], bool] | None = None

    @classmethod
    def strict(cls, *domains: str) -> RegistryPolicy:
        if not domains:
            raise ValueError("RegistryPolicy.strict requires at least one domain")
        return cls(allowed_domains=tuple(domains), require_repository=True)

    def allows_host(self, host: str, patterns: tuple[str, ...] | None = None) -> bool:
        host = host.lower().strip(".")
        patterns = self.allowed_domains if patterns is None else patterns
        if not patterns:
            return False
        return any(
            host == pattern.lower().strip(".")
            or host.endswith("." + pattern.lower().strip("."))
            for pattern in patterns
        )

    def verify_digest(self, entry: dict[str, Any], server_name: str) -> bool:
        pins = dict(self.pinned_digests)
        if server_name not in pins:
            return not self.require_pinned
        return entry_digest(entry) == pins[server_name].lower()

    def check_server(self, server: RegistryServer) -> None:
        if self.require_latest and not server.is_latest:
            raise ValueError(f"{server.name}: not the latest version")
        if self.require_active and server.status != "active":
            raise ValueError(f"{server.name}: registry status is {server.status!r}")
        if self.require_repository and not server.repository_url:
            raise ValueError(f"{server.name}: registry entry has no repository URL")
        if self.allowed_repository_hosts:
            host = urlparse(server.repository_url).hostname or ""
            if not self.allows_host(host, self.allowed_repository_hosts):
                raise ValueError(f"{server.name}: repository host {host!r} is not allowed")


@dataclass(frozen=True)
class RegistrySearchResult:
    servers: tuple[RegistryServer, ...]
    next_cursor: str | None = None


def _parse_headers(raw: Any) -> tuple[RegistryHeader, ...]:
    if not isinstance(raw, list):
        return ()
    headers: list[RegistryHeader] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        headers.append(
            RegistryHeader(
                name=str(item["name"]),
                description=sanitize_untrusted_text(
                    str(item.get("description") or ""), max_chars=160
                ),
                required=bool(item.get("isRequired")),
                secret=bool(item.get("isSecret")),
                default=str(item["default"]) if item.get("default") is not None else None,
                choices=tuple(str(choice) for choice in item.get("choices") or ()),
            )
        )
    return tuple(headers)


def _parse_remotes(raw: Any) -> tuple[RegistryRemote, ...]:
    if not isinstance(raw, list):
        return ()
    remotes: list[RegistryRemote] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        raw_variables = item.get("variables")
        variables: dict[str, dict[str, Any]] = (
            raw_variables if isinstance(raw_variables, dict) else {}
        )
        remotes.append(
            RegistryRemote(
                type=str(item.get("type") or ""),
                url=str(item["url"]),
                variables=variables,
                headers=_parse_headers(item.get("headers")),
            )
        )
    return tuple(remotes)


def _parse_packages(raw: Any) -> tuple[RegistryPackage, ...]:
    if not isinstance(raw, list):
        return ()
    packages: list[RegistryPackage] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_transport = item.get("transport")
        transport: dict[str, Any] = raw_transport if isinstance(raw_transport, dict) else {}
        packages.append(
            RegistryPackage(
                registry_type=str(item.get("registryType") or ""),
                identifier=str(item.get("identifier") or ""),
                version=str(item.get("version") or ""),
                transport=str(transport.get("type") or ""),
                runtime_hint=str(item.get("runtimeHint") or ""),
            )
        )
    return tuple(packages)


def _parse_server(entry: Any) -> RegistryServer | None:
    if not isinstance(entry, dict):
        return None
    raw_server = entry.get("server")
    raw: Any = raw_server if isinstance(raw_server, dict) else entry
    if not isinstance(raw, dict) or not raw.get("name"):
        return None
    raw_meta = entry.get("_meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    raw_official = meta.get("io.modelcontextprotocol.registry/official")
    official: dict[str, Any] = raw_official if isinstance(raw_official, dict) else {}
    raw_repository = raw.get("repository")
    repository_url = (
        str(raw_repository.get("url") or "") if isinstance(raw_repository, dict) else ""
    )
    return RegistryServer(
        name=str(raw["name"]),
        title=sanitize_untrusted_text(str(raw.get("title") or ""), max_chars=120),
        description=sanitize_untrusted_text(str(raw.get("description") or ""), max_chars=500),
        version=str(raw.get("version") or ""),
        repository_url=repository_url,
        remotes=_parse_remotes(raw.get("remotes")),
        packages=_parse_packages(raw.get("packages")),
        published_at=str(official.get("publishedAt") or ""),
        is_latest=bool(official.get("isLatest", True)),
        status=str(official.get("status") or "active"),
        entry_sha256=entry_digest(entry),
    )


class McpRegistryClient:
    """只读 registry 客户端；``base_url`` 可注入测试服务器。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def search(
        self,
        query: str = "",
        *,
        limit: int = 10,
        cursor: str | None = None,
        policy: RegistryPolicy | None = None,
    ) -> RegistrySearchResult:
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["search"] = query
        if cursor:
            params["cursor"] = cursor
        url = f"{self.base_url}{SEARCH_PATH}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "yai-agent-core-mcp-registry/1",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed registry scheme
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ValueError("registry response too large")
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("servers"), list):
            raise ValueError("unexpected registry response: missing servers[]")
        servers_list: list[RegistryServer] = []
        for entry in data["servers"]:
            if policy is not None and policy.verify_entry is not None:
                if not policy.verify_entry(entry):
                    continue
            server = _parse_server(entry)
            if server is None:
                continue
            if policy is not None:
                if not policy.verify_digest(entry, server.name):
                    continue
                if policy.require_latest and not server.is_latest:
                    continue
                if policy.require_active and server.status != "active":
                    continue
                if policy.allowed_domains and not any(
                    policy.allows_host(urlparse(remote.url).hostname or "")
                    for remote in server.streamable_remotes()
                ):
                    continue
            servers_list.append(server)
        servers = tuple(servers_list)
        raw_metadata = data.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        return RegistrySearchResult(
            servers=servers,
            next_cursor=str(metadata["nextCursor"]) if metadata.get("nextCursor") else None,
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "McpRegistryClient",
    "RegistryHeader",
    "RegistryPackage",
    "RegistryPolicy",
    "entry_digest",
    "RegistryRemote",
    "RegistrySearchResult",
    "RegistryServer",
]
