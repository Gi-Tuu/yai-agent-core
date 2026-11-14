"""A tiny in-process A2A 1.0 fake server for the host-adapter example/tests.

Only the non-streaming JSON-RPC subset: Agent Card, SendMessage, GetTask.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

TASKS: dict[str, dict[str, Any]] = {}


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeA2A/1.0"

    def log_message(self, *args: Any) -> None:  # silence test output
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/a2a+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/.well-known/agent-card.json":
            self.send_error(404)
            return
        port = self.server.server_address[1]
        self._json(
            200,
            {
                "name": "Fake Echo Agent",
                "description": "An offline A2A agent used by the YAI host-adapter example.",
                "version": "1.0.0",
                "supportedInterfaces": [
                    {
                        "url": f"http://127.0.0.1:{port}/a2a",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
                "capabilities": {"streaming": False, "pushNotifications": False},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "skills": [{"id": "echo", "name": "Echo", "description": "Echo a task."}],
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/a2a", "/oauth/token", "/oauth/device"}:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8")
        if self.path == "/oauth/device":
            self._json(
                200,
                {
                    "device_code": "device-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://example.com/device",
                    "interval": 0.01,
                    "expires_in": 60,
                },
            )
            return
        if self.path == "/oauth/token":
            self.server.last_oauth_form = raw_body  # type: ignore[attr-defined]
            form = parse_qs(raw_body)
            grant = (form.get("grant_type") or [""])[0]
            if grant == "authorization_code":
                token = "token-authcode"
            elif grant.startswith("urn:ietf:params:oauth:grant-type:device_code"):
                self.server.device_polls = getattr(self.server, "device_polls", 0) + 1  # type: ignore[attr-defined]
                if self.server.device_polls == 1:  # type: ignore[attr-defined]
                    self._json(400, {"error": "authorization_pending"})
                    return
                token = "token-device"
            else:
                token = "token-123"
            self._json(
                200,
                {"access_token": token, "token_type": "Bearer", "expires_in": 3600},
            )
            return
        request = json.loads(raw_body)
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        self.server.last_auth = self.headers.get("Authorization")  # type: ignore[attr-defined]

        if method == "SendStreamingMessage":
            message = params.get("message") or {}
            text = "\n".join(
                str(part.get("text") or "")
                for part in message.get("parts") or []
                if isinstance(part, dict)
            )
            task_id = f"stream-{len(TASKS) + 1}"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            def chunk(text: str) -> dict[str, Any]:
                return {
                    "taskId": task_id,
                    "contextId": "ctx-stream",
                    "artifact": {"artifactId": "a1", "parts": [{"text": text}]},
                }

            frames = [
                {
                    "task": {
                        "id": task_id,
                        "contextId": "ctx-stream",
                        "status": {"state": "TASK_STATE_WORKING"},
                    }
                },
                {"artifactUpdate": chunk("stream part 1")},
                {"artifactUpdate": chunk("stream part 2")},
                {
                    "statusUpdate": {
                        "taskId": task_id,
                        "contextId": "ctx-stream",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                    }
                },
            ]
            for frame in frames:
                self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            return

        if method == "SendMessage":
            message = params.get("message") or {}
            text = "\n".join(
                str(part.get("text") or "")
                for part in message.get("parts") or []
                if isinstance(part, dict)
            )
            task_id = f"task-{len(TASKS) + 1}"
            TASKS[task_id] = {
                "id": task_id,
                "contextId": "ctx-demo",
                "status": {
                    "state": "TASK_STATE_WORKING"
                    if "slow" in text
                    else "TASK_STATE_COMPLETED"
                },
                "artifacts": [] if "slow" in text else [
                    {"artifactId": "artifact-1", "parts": [{"text": f"echo: {text}"}]}
                ],
            }
            self._json(
                200,
                {"jsonrpc": "2.0", "id": request_id, "result": {"task": TASKS[task_id]}},
            )
            return

        if method == "GetTask":
            task_id = str(params.get("id") or "")
            task = TASKS.get(task_id)
            if task is None:
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32001, "message": "task not found"},
                    },
                )
                return
            task["status"] = {"state": "TASK_STATE_COMPLETED"}
            task["artifacts"] = [
                {"artifactId": "artifact-1", "parts": [{"text": "echo: done after polling"}]}
            ]
            self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": {"task": task}})
            return

        if method == "CreateTaskPushNotificationConfig":
            task_id = str(params.get("taskId") or "")
            config = params.get("pushNotificationConfig") or {}
            config_id = f"cfg-{len(self.server.push_configs) + 1}"  # type: ignore[attr-defined]
            stored = {"id": config_id, **config}
            self.server.push_configs[task_id] = stored  # type: ignore[attr-defined]
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"pushNotificationConfig": stored},
                },
            )
            return

        if method == "ListTaskPushNotificationConfigs":
            task_id = str(params.get("taskId") or "")
            config = self.server.push_configs.get(task_id)  # type: ignore[attr-defined]
            configs = [config] if config else []
            self._json(
                200,
                {"jsonrpc": "2.0", "id": request_id, "result": {"configs": configs}},
            )
            return

        if method == "DeleteTaskPushNotificationConfig":
            task_id = str(params.get("taskId") or "")
            self.server.push_configs.pop(task_id, None)  # type: ignore[attr-defined]
            self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": {}})
            return

        self._json(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found"},
            },
        )


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.last_auth = None  # type: ignore[attr-defined]
    server.push_configs = {}  # type: ignore[attr-defined]
    server.last_oauth_form = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
