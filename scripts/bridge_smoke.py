"""Native Messaging Bridge ↔ 真实 Sidecar 的端到端验收（不启动浏览器）。

模拟 Chrome 侧行为：以 ``chrome-extension://<id>/`` 作为 argv[1] 拉起真实 Bridge
进程，用 Native Messaging 帧（4 字节小端长度 + JSON）依次发 hello / status /
shutdown，并校验真实 Sidecar 的握手、状态与退出码。

这验证的是**真实进程 + 真实 Sidecar + 真实帧协议**；浏览器加载扩展本身不在本脚本
范围内（见 scripts/extension_smoke.py）。

退出码：0 通过；2 环境不具备（未安装 yai_core / 缺 Sidecar 依赖）；1 失败。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class _FrameReader:
    """后台线程按 Native Messaging 帧读取子进程 stdout，带超时的取帧。"""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            header = self._stream.read(4)
            if not header or len(header) < 4:
                self._queue.put(None)
                return
            length = int.from_bytes(header, "little")
            payload = self._stream.read(length)
            if payload is None or len(payload) != length:
                self._queue.put(None)
                return
            try:
                self._queue.put(json.loads(payload.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._queue.put(None)
                return

    def get(self, timeout: float = 30.0) -> dict[str, Any]:
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise AssertionError(f"等待 Bridge 输出超时（{timeout}s）") from exc
        if item is None:
            raise AssertionError("Bridge 进程提前关闭了 stdout")
        return item


def _send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    assert process.stdin is not None
    process.stdin.write(len(payload).to_bytes(4, "little") + payload)
    process.stdin.flush()


def _spawn(origin: str, config_path: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "yai_core.sidecar.native_bridge",
            origin,
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(ROOT),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge ↔ 真实 Sidecar 验收")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="yai-bridge-smoke-"))
    config_path = tmpdir / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "allowed_origins": ["chrome-extension://yaismoketest/"],
                "initialize": {"policy": {"mode": "auto"}, "interaction_timeout": 60},
                "max_task_chars": 400,
            }
        ),
        encoding="utf-8",
    )

    evidence: dict[str, Any] = {"origin": "chrome-extension://yaismoketest"}
    try:
        # 负例：未授权来源必须直接拒绝启动（退出码 2），不建立任何会话。
        denied = _spawn("chrome-extension://evil/", config_path)
        try:
            denied_stdout, denied_stderr = denied.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            denied.kill()
            denied_stdout, denied_stderr = denied.communicate()
        evidence["denied_exit"] = denied.returncode
        evidence["denied_stderr_has_reason"] = "拒绝启动" in denied_stderr.decode(
            "utf-8", "replace"
        )

        process = _spawn("chrome-extension://yaismoketest/", config_path)
        reader = _FrameReader(process.stdout)
        ready = reader.get(60)
        evidence["bridge_ready"] = ready.get("type")
        evidence["sidecar_protocol"] = ready.get("sidecar_protocol_version")

        _send(process, {"op": "hello", "protocol": 1})
        hello = reader.get(15)
        evidence["hello_type"] = hello.get("type")
        evidence["hello_capabilities"] = sorted((hello.get("capabilities") or {}).keys())

        _send(process, {"op": "status"})
        status = reader.get(15)
        evidence["status"] = {
            "initialized": status.get("initialized"),
            "sidecar_alive": status.get("sidecar_alive"),
            "protocol": status.get("sidecar_protocol_version"),
        }

        _send(process, {"op": "shutdown"})
        ack = reader.get(15)
        evidence["shutdown_ack"] = ack.get("type")

        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        evidence["exit_code"] = process.returncode
        stderr_text = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
        evidence["stderr_tail"] = stderr_text[-400:]

        ok = (
            evidence["denied_exit"] == 2
            and evidence["bridge_ready"] == "bridge_ready"
            and evidence.get("sidecar_protocol") == 1
            and evidence["hello_type"] == "hello_ok"
            and evidence["status"]["initialized"] is True
            and evidence["status"]["sidecar_alive"] is True
            and evidence["shutdown_ack"] == "shutdown_ack"
            and evidence["exit_code"] == 0
        )
        payload = {
            "ok": ok,
            "detail": "Bridge ↔ 真实 Sidecar 握手/状态/关闭全部通过" if ok else "存在失败项",
            **evidence,
        }
    except Exception as exc:  # noqa: BLE001 - 验收脚本要把失败原因说清楚
        payload = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
            **evidence,
        }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[bridge-smoke] {'通过' if payload['ok'] else '失败'}：{payload['detail']}")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
