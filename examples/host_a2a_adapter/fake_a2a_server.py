"""A tiny in-process A2A 1.0 fake server for the host-adapter example/tests.

Only the non-streaming JSON-RPC subset: Agent Card, SendMessage, GetTask.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

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
        if self.path != "/a2a":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        self.server.last_auth = self.headers.get("Authorization")  # type: ignore[attr-defined]

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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
