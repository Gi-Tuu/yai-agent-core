"""Host-layer A2A adapter: expose a remote A2A agent as an ordinary YAI tool.

This is **not** a kernel feature. It demonstrates the positioning doc: an A2A client lives in the
host, wraps one remote agent as a `ToolSpec`, and the kernel sees only a normal tool call.

Supported subset of A2A 1.0:

- Agent Card at ``/.well-known/agent-card.json`` (JSON-RPC interface);
- ``SendMessage`` and ``GetTask`` (non-streaming); task states normalized;
- text parts in artifacts / status message;
- optional Bearer token / OAuth2 (client credentials, authorization code, device code).
- streaming (SendStreamingMessage), push config methods.

No webhook receiver, file parts, or agent-card signature verification.
Security: only HTTPS, or HTTP on loopback for local demos.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from yai_core.tools.hygiene import sanitize_untrusted_text
from yai_core.types import ToolSpec

TERMINAL_STATES = {
    "COMPLETED",
    "FAILED",
    "CANCELED",
    "CANCELLED",
    "REJECTED",
    "INPUT_REQUIRED",
    "AUTH_REQUIRED",
}


@dataclass(frozen=True)
class A2AOAuth2:
    """OAuth2 client-credentials configuration (no browser flow)."""

    token_url: str
    client_id: str
    client_secret_env: str
    scope: str = ""
    audience: str | None = None


@dataclass(frozen=True)
class A2AOAuth2AuthorizationCode:
    """OAuth2 authorization-code configuration (browser redirect handled by host)."""

    authorization_url: str
    token_url: str
    client_id: str
    redirect_uri: str
    client_secret_env: str | None = None
    scope: str = ""
    #: Use PKCE (S256) by default; disable only for providers that require it.
    use_pkce: bool = True

    def build_authorization_url(self, state: str, *, code_challenge: str | None = None) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }
        if self.scope:
            params["scope"] = self.scope
        if state:
            params["state"] = state
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        separator = "&" if "?" in self.authorization_url else "?"
        return f"{self.authorization_url}{separator}{urlencode(params)}"


@dataclass(frozen=True)
class A2AOAuth2DeviceCode:
    """OAuth2 device-code configuration (user approves on another device)."""

    device_authorization_url: str
    token_url: str
    client_id: str
    client_secret_env: str | None = None
    scope: str = ""


@dataclass(frozen=True)
class A2AConfig:
    base_url: str
    card_path: str = "/.well-known/agent-card.json"
    token_env: str | None = None
    headers: dict[str, str] | None = None
    timeout: float = 15.0
    poll_interval: float = 0.5
    max_wait: float = 60.0
    #: Use SendStreamingMessage when the agent card advertises streaming.
    streaming: bool = False
    #: Optional OAuth2 client-credentials credentials.
    oauth2: A2AOAuth2 | A2AOAuth2AuthorizationCode | A2AOAuth2DeviceCode | None = None


@dataclass(frozen=True)
class A2AAgentCard:
    name: str
    description: str
    version: str
    url: str
    protocol_version: str = "1.0"
    skills: tuple[dict[str, Any], ...] = ()

    @property
    def safe_description(self) -> str:
        return sanitize_untrusted_text(self.description, max_chars=300)


@dataclass(frozen=True)
class A2AResult:
    text: str
    task_id: str = ""
    state: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _normalize_state(value: Any) -> str:
    state = str(value or "").upper()
    return state.removeprefix("TASK_STATE_")


def _validate_endpoint(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return url
    raise ValueError(f"A2A endpoint must be HTTPS (or loopback HTTP): {url!r}")


def _parts_text(parts: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(parts, list):
        return texts
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            texts.append(str(part["text"]))
        elif part.get("data") is not None:
            texts.append(json.dumps(part["data"], ensure_ascii=False))
    return texts


def _task_text(task: dict[str, Any]) -> str:
    texts: list[str] = []
    artifacts = task.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict):
                texts.extend(_parts_text(artifact.get("parts")))
    status = task.get("status")
    if not texts and isinstance(status, dict):
        message = status.get("message")
        if isinstance(message, dict):
            texts.extend(_parts_text(message.get("parts")))
    return "\n".join(texts).strip()


class A2AClient:
    """Minimal JSON-RPC A2A client (host layer)."""

    def __init__(self, config: A2AConfig) -> None:
        self.config = config
        self._oauth_token: str | None = None
        self._oauth_expires_at = 0.0
        self._oauth_state: str | None = None
        self._pkce_verifier: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/a2a+json",
            "Accept": "application/a2a+json, application/json",
            "A2A-Version": "1.0",
        }
        headers.update(self.config.headers or {})
        if self.config.oauth2 is not None:
            token = self._oauth_access_token(required=not self.config.token_env)
            if token:
                headers.setdefault("Authorization", f"Bearer {token}")
        if self.config.token_env:
            token = os.environ.get(self.config.token_env)
            if not token:
                raise ValueError(f"A2A token env {self.config.token_env} is not set")
            headers.setdefault("Authorization", f"Bearer {token}")
        return headers

    def _form_post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        _validate_endpoint(url)
        request = Request(
            url,
            data=urlencode(form).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310
                raw = response.read(200_000)
        except HTTPError as exc:
            raw = exc.read(200_000)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("OAuth endpoint returned a non-object response")
        return data

    def _store_token(self, data: dict[str, Any]) -> str:
        token = data.get("access_token")
        if not token:
            raise RuntimeError("OAuth token endpoint returned no access_token")
        expires = float(data.get("expires_in") or 3600)
        self._oauth_token = str(token)
        self._oauth_expires_at = time.monotonic() + max(30.0, expires - 30.0)
        return self._oauth_token

    def _oauth_access_token(self, *, required: bool = True) -> str | None:
        oauth = self.config.oauth2
        if oauth is None:
            return None
        if self._oauth_token and time.monotonic() < self._oauth_expires_at:
            return self._oauth_token
        if not isinstance(oauth, A2AOAuth2):
            if required:
                raise RuntimeError(
                    "OAuth authorization/device flow has no token yet; call "
                    "authorization_url()+exchange_code() or "
                    "start_device_flow()+poll_device_token() first"
                )
            return None
        secret = os.environ.get(oauth.client_secret_env)
        if not secret:
            raise ValueError(f"OAuth client secret env {oauth.client_secret_env} is not set")
        form = {
            "grant_type": "client_credentials",
            "client_id": oauth.client_id,
            "client_secret": secret,
        }
        if oauth.scope:
            form["scope"] = oauth.scope
        if oauth.audience:
            form["audience"] = oauth.audience
        return self._store_token(self._form_post(oauth.token_url, form))

    def authorization_url(self, state: str = "") -> str:
        oauth = self.config.oauth2
        if not isinstance(oauth, A2AOAuth2AuthorizationCode):
            raise ValueError("config.oauth2 is not an authorization-code configuration")
        challenge: str | None = None
        if oauth.use_pkce:
            self._pkce_verifier = secrets.token_urlsafe(64)
            digest = hashlib.sha256(self._pkce_verifier.encode("ascii")).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        self._oauth_state = state or None
        return oauth.build_authorization_url(state, code_challenge=challenge)

    def exchange_code(self, code: str, *, state: str | None = None) -> None:
        oauth = self.config.oauth2
        if not isinstance(oauth, A2AOAuth2AuthorizationCode):
            raise ValueError("config.oauth2 is not an authorization-code configuration")
        if self._oauth_state is not None and state != self._oauth_state:
            raise ValueError("OAuth state mismatch; refusing to exchange the code")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": oauth.client_id,
            "redirect_uri": oauth.redirect_uri,
        }
        if oauth.use_pkce:
            if not self._pkce_verifier:
                raise ValueError("PKCE verifier missing; call authorization_url() first")
            form["code_verifier"] = self._pkce_verifier
        secret = os.environ.get(oauth.client_secret_env) if oauth.client_secret_env else None
        if secret:
            form["client_secret"] = secret
        self._store_token(self._form_post(oauth.token_url, form))
        self._oauth_state = None
        self._pkce_verifier = None

    def start_device_flow(self) -> dict[str, Any]:
        oauth = self.config.oauth2
        if not isinstance(oauth, A2AOAuth2DeviceCode):
            raise ValueError("config.oauth2 is not a device-code configuration")
        form = {"client_id": oauth.client_id}
        if oauth.scope:
            form["scope"] = oauth.scope
        data = self._form_post(oauth.device_authorization_url, form)
        if not data.get("device_code") or not data.get("user_code"):
            raise RuntimeError("device authorization endpoint returned no device_code/user_code")
        return data

    def poll_device_token(
        self, device_code: str, *, interval: float | None = None, timeout: float = 300
    ) -> None:
        oauth = self.config.oauth2
        if not isinstance(oauth, A2AOAuth2DeviceCode):
            raise ValueError("config.oauth2 is not a device-code configuration")
        delay = interval if interval is not None else 5.0
        deadline = time.monotonic() + timeout
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": oauth.client_id,
        }
        secret = os.environ.get(oauth.client_secret_env) if oauth.client_secret_env else None
        if secret:
            form["client_secret"] = secret
        while time.monotonic() < deadline:
            data = self._form_post(oauth.token_url, form)
            if data.get("access_token"):
                self._store_token(data)
                return
            error = str(data.get("error") or "")
            if error not in {"authorization_pending", "slow_down"}:
                raise RuntimeError(f"device token error: {error or data}")
            time.sleep(max(0.01, delay))
            if error == "slow_down":
                delay += 1
        raise TimeoutError("device authorization timed out")

    def fetch_card(self) -> A2AAgentCard:
        card_url = _validate_endpoint(
            self.config.base_url.rstrip("/") + self.config.card_path
        )
        request = Request(card_url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310
            data = json.loads(response.read(1_000_000).decode("utf-8"))
        if not isinstance(data, dict) or not data.get("name"):
            raise ValueError("invalid A2A Agent Card")
        interfaces = data.get("supportedInterfaces") or []
        endpoint = str(data.get("url") or "")
        protocol_version = "1.0"
        for item in interfaces:
            if not isinstance(item, dict):
                continue
            if str(item.get("protocolBinding", "")).upper() == "JSONRPC":
                endpoint = str(item.get("url") or endpoint)
                protocol_version = str(item.get("protocolVersion") or protocol_version)
                break
        if not endpoint:
            raise ValueError("A2A Agent Card has no JSON-RPC endpoint")
        return A2AAgentCard(
            name=sanitize_untrusted_text(str(data["name"]), max_chars=120) or "a2a_agent",
            description=sanitize_untrusted_text(str(data.get("description") or ""), max_chars=300),
            version=str(data.get("version") or ""),
            url=_validate_endpoint(endpoint),
            protocol_version=protocol_version,
            skills=tuple(skill for skill in data.get("skills") or [] if isinstance(skill, dict)),
        )

    def _rpc(self, endpoint: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        request = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310
            body = json.loads(response.read(2_000_000).decode("utf-8"))
        if not isinstance(body, dict):
            raise RuntimeError("A2A response is not a JSON object")
        if body.get("error"):
            error = body["error"]
            raise RuntimeError(f"A2A JSON-RPC error: {error}")
        result = body.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("A2A response has no result object")
        return result

    def send(self, task: str, *, context_id: str | None = None) -> A2AResult:
        if self.config.streaming:
            return self.stream(task, context_id=context_id)
        card = self.fetch_card()
        message: dict[str, Any] = {
            "role": "user",
            "messageId": f"msg-{uuid.uuid4()}",
            "parts": [{"text": task}],
        }
        if context_id:
            message["contextId"] = context_id
        result = self._rpc(card.url, "SendMessage", {"message": message})

        message_obj = result.get("message")
        if isinstance(message_obj, dict):
            return A2AResult(
                text="\n".join(_parts_text(message_obj.get("parts"))),
                raw=result,
            )

        raw_task = result.get("task")
        if not isinstance(raw_task, dict):
            raise RuntimeError("A2A SendMessage result has neither task nor message")
        task_obj: dict[str, Any] = cast(dict[str, Any], raw_task)
        deadline = time.monotonic() + self.config.max_wait
        while True:
            raw_status = task_obj.get("status")
            status: dict[str, Any] = (
                cast(dict[str, Any], raw_status)
                if isinstance(raw_status, dict)
                else {}
            )
            state = _normalize_state(status.get("state"))
            if state in TERMINAL_STATES:
                return A2AResult(
                    text=_task_text(task_obj),
                    task_id=str(task_obj.get("id") or ""),
                    state=state,
                    raw=result,
                )
            if time.monotonic() >= deadline:
                return A2AResult(
                    text=_task_text(task_obj),
                    task_id=str(task_obj.get("id") or ""),
                    state=state or "TIMEOUT",
                    raw=result,
                )
            time.sleep(self.config.poll_interval)
            polled = self._rpc(card.url, "GetTask", {"id": task_obj.get("id")})
            raw_polled = polled.get("task")
            task_obj = (
                cast(dict[str, Any], raw_polled)
                if isinstance(raw_polled, dict)
                else polled
            )

    def stream(self, task: str, *, context_id: str | None = None, on_text: Any = None) -> A2AResult:
        """Consume ``SendStreamingMessage`` SSE frames and aggregate text.

        ``on_text`` receives each text chunk as it arrives (host UI can forward it);
        the returned ``A2AResult`` carries the full text and terminal state.
        """
        card = self.fetch_card()
        message: dict[str, Any] = {
            "role": "user",
            "messageId": f"msg-{uuid.uuid4()}",
            "parts": [{"text": task}],
        }
        if context_id:
            message["contextId"] = context_id
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "SendStreamingMessage",
            "params": {"message": message},
        }
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        request = Request(
            card.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        chunks: list[str] = []
        raw_events: list[dict[str, Any]] = []
        task_id = ""
        state = ""
        with urlopen(request, timeout=self.config.timeout) as response:  # noqa: S310
            for raw_line in response:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                raw_events.append(event)
                task_obj = event.get("task")
                if isinstance(task_obj, dict):
                    task_id = str(task_obj.get("id") or task_id)
                    for text in _task_text(task_obj).splitlines():
                        if text:
                            chunks.append(text)
                            if on_text:
                                on_text(text)
                update = event.get("artifactUpdate")
                if isinstance(update, dict):
                    artifact = update.get("artifact")
                    if isinstance(artifact, dict):
                        for text in _parts_text(artifact.get("parts")):
                            chunks.append(text)
                            if on_text:
                                on_text(text)
                status = event.get("statusUpdate")
                if isinstance(status, dict):
                    task_id = str(status.get("taskId") or task_id)
                    status_obj = status.get("status")
                    if isinstance(status_obj, dict):
                        state = _normalize_state(status_obj.get("state"))
                        if state in TERMINAL_STATES:
                            break
        return A2AResult(
            text="\n".join(chunks).strip(),
            task_id=task_id,
            state=state or "INCOMPLETE",
            raw={"events": raw_events},
        )

    def create_push_config(
        self, task_id: str, webhook_url: str, *, token: str | None = None
    ) -> dict[str, Any]:
        _validate_endpoint(webhook_url)
        config: dict[str, Any] = {"url": webhook_url}
        if token:
            config["token"] = token
        card = self.fetch_card()
        return self._rpc(
            card.url,
            "CreateTaskPushNotificationConfig",
            {"taskId": task_id, "pushNotificationConfig": config},
        )

    def list_push_configs(self, task_id: str) -> dict[str, Any]:
        card = self.fetch_card()
        return self._rpc(card.url, "ListTaskPushNotificationConfigs", {"taskId": task_id})

    def delete_push_config(self, task_id: str, config_id: str) -> dict[str, Any]:
        card = self.fetch_card()
        return self._rpc(
            card.url,
            "DeleteTaskPushNotificationConfig",
            {"taskId": task_id, "id": config_id},
        )

    def as_tool(self, *, name: str | None = None) -> ToolSpec:
        card = self.fetch_card()
        tool_name = name or f"a2a_{card.name}"
        tool_name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in tool_name)[:64]

        async def handler(task: str) -> str:
            result = self.send(task)
            return result.text or f"[A2A {card.name} state={result.state}]"

        return ToolSpec(
            name=tool_name,
            description=(
                f"Delegate a task to remote A2A agent {card.name}: "
                f"{card.safe_description or 'no description'}"
            ),
            input_schema={
                "type": "object",
                "properties": {"task": {"type": "string", "description": "Task to delegate"}},
                "required": ["task"],
            },
            handler=handler,
            source="native",
        )


__all__ = [
    "A2AClient",
    "A2AConfig",
    "A2AAgentCard",
    "A2AOAuth2",
    "A2AOAuth2AuthorizationCode",
    "A2AOAuth2DeviceCode",
    "A2AResult",
    "TERMINAL_STATES",
]
