"""Host-layer A2A adapter: expose a remote A2A agent as an ordinary YAI tool.

This is **not** a kernel feature. It demonstrates the positioning doc: an A2A client lives in the
host, wraps one remote agent as a `ToolSpec`, and the kernel sees only a normal tool call.

Supported subset of A2A 1.0:

- Agent Card at ``/.well-known/agent-card.json`` (JSON-RPC interface);
- ``SendMessage`` and ``GetTask`` (non-streaming); task states normalized;
- text parts in artifacts / status message;
- optional Bearer token from an environment variable.

No streaming, push notifications, file parts, OAuth dance, or agent-card signature verification.
Security: only HTTPS, or HTTP on loopback for local demos.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
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

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/a2a+json",
            "Accept": "application/a2a+json, application/json",
            "A2A-Version": "1.0",
        }
        headers.update(self.config.headers or {})
        if self.config.token_env:
            token = os.environ.get(self.config.token_env)
            if not token:
                raise ValueError(f"A2A token env {self.config.token_env} is not set")
            headers.setdefault("Authorization", f"Bearer {token}")
        return headers

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

        if "message" in result and isinstance(result["message"], dict):
            return A2AResult(
                text="\n".join(_parts_text(result["message"].get("parts"))),
                raw=result,
            )

        task_obj = result.get("task")
        if not isinstance(task_obj, dict):
            raise RuntimeError("A2A SendMessage result has neither task nor message")
        deadline = time.monotonic() + self.config.max_wait
        while True:
            status = task_obj.get("status") if isinstance(task_obj.get("status"), dict) else {}
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
            task_obj = polled.get("task") if isinstance(polled.get("task"), dict) else polled

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
            source="a2a",
        )


__all__ = ["A2AClient", "A2AConfig", "A2AAgentCard", "A2AResult", "TERMINAL_STATES"]
