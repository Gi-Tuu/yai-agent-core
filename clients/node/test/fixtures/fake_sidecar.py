"""Test fixture: minimal sidecar session server (stdio NDJSON + JSON-RPC 2.0).

Only implements the session plane the TypeScript client needs: initialize / ping /
agent/run (with one approval round-trip and two events) / agent/cancel / shutdown.
Tool calls do not exist here by design — the real sidecar exposes host tools via
MCP/OpenAPI.
"""

from __future__ import annotations

import json
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    run_counter = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        message_id = message.get("id")
        if method is None:
            continue  # 宿主对反向请求的响应，在 agent/run 分支里同步读取
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocol": 1,
                        "kernel_version": "test",
                        "tools": [{"name": "echo", "description": "echo tool", "source": "native"}],
                    },
                }
            )
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": message_id, "result": {"ok": True}})
        elif method == "agent/run":
            run_counter += 1
            run_id = f"run-{run_counter}"
            send({"jsonrpc": "2.0", "id": message_id, "result": {"run_id": run_id}})
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "agent/event",
                    "params": {
                        "run_id": run_id,
                        "seq": 0,
                        "type": "strategy_selected",
                        "data": {"strategy": "direct"},
                    },
                }
            )
            send(
                {
                    "jsonrpc": "2.0",
                    "id": f"srv-{run_counter}",
                    "method": "approval/request",
                    "params": {"tool": "echo", "arguments": {}, "context": {}},
                }
            )
            response = json.loads(sys.stdin.readline() or "{}")
            approved = bool((response.get("result") or {}).get("approved"))
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "agent/event",
                    "params": {
                        "run_id": run_id,
                        "seq": 1,
                        "type": "done",
                        "data": {"final_text": "approved" if approved else "denied"},
                    },
                }
            )
        elif method == "agent/cancel":
            run_id = (message.get("params") or {}).get("run_id")
            if not run_id:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "result": {"cancelled": False, "reason": "missing run_id"},
                    }
                )
            else:
                send({"jsonrpc": "2.0", "id": message_id, "result": {"cancelled": True}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": message_id, "result": {"ok": True}})
            return
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
