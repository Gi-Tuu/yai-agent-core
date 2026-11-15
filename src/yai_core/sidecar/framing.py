"""Native Messaging 帧编解码与超帧截断。

从 ``sidecar/native_bridge.py`` 拆出；``native_bridge`` 继续 re-export。
"""

from __future__ import annotations

import json
import struct
from typing import Any

from yai_core.sidecar.bridge_errors import BridgeError

#: Native Messaging 单帧上限：Chrome 对 host→browser 的消息有 1 MiB 限制。
MAX_MESSAGE_BYTES = 1_048_576
_FRAME_HEADER = struct.Struct("<I")


# ---------------------------------------------------------------- 帧编解码


def encode_frame(message: dict[str, Any]) -> bytes:
    """把 JSON 对象编码成 Native Messaging 帧（4 字节小端长度 + UTF-8）。"""
    payload = json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")
    return _FRAME_HEADER.pack(len(payload)) + payload


def read_frame(stream: Any) -> dict[str, Any] | None:
    """从二进制流读一帧；EOF 返回 None。格式非法/超限抛 :class:`BridgeError`。"""
    header = stream.read(_FRAME_HEADER.size)
    if header is None or len(header) == 0:
        return None
    if len(header) != _FRAME_HEADER.size:
        raise BridgeError("帧头不完整（连接可能已断开）")
    (length,) = _FRAME_HEADER.unpack(header)
    if length <= 0:
        raise BridgeError("帧长度非法")
    if length > MAX_MESSAGE_BYTES:
        raise BridgeError(f"帧超过上限：{length} > {MAX_MESSAGE_BYTES}")
    payload = stream.read(length)
    if payload is None or len(payload) != length:
        raise BridgeError("帧体不完整（连接可能已断开）")
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"帧不是合法 JSON：{exc}") from exc
    if not isinstance(message, dict):
        raise BridgeError("帧必须是 JSON object")
    return message


def write_frame(stream: Any, message: dict[str, Any]) -> None:
    stream.write(encode_frame(message))
    stream.flush()


# ---------------------------------------------------------------- 超帧截断
#
# Chrome 丢弃超限帧 / 杀 host，所以 Bridge 必须把超帧换成小帧；但"只留 type"
# 会让扩展永远收不到 done/cancelled/error，运行态卡死。截断帧因此保留：
# - 顶层：type / truncated / original_bytes / method / request_id（可审计）；
# - 通知：顶层 event_type / run_id，以及精简后的 params.type/run_id/seq/status/error/reason；
# - 只保留（可截断的）final_text，绝不透传 data.verification / data.evidence 等大字段。
# 扩展据此识别终态并清理运行态（见 extension/service_worker.js、sidepanel.js）。

#: 截断帧里 final_text 的上限（人看的摘要足够，不需要全文）。
TRUNCATED_FINAL_TEXT_MAX = 200
#: 截断帧里 run_id / type / method 等标识类字段的上限。
TRUNCATED_META_MAX = 120
#: 截断帧里 error / reason / question 等解释性字段的上限。
TRUNCATED_ERROR_MAX = 300


def _frame_payload_size(message: dict[str, Any]) -> int:
    return len(encode_frame(message)) - _FRAME_HEADER.size


def _truncated_scalar(
    value: Any, *, limit: int, allow_number: bool = False
) -> Any:
    """只保留短标量：str 截断、int/float/bool 原样；dict/list 一律丢弃。"""
    if isinstance(value, bool):
        return value if allow_number else None
    if isinstance(value, (int, float)):
        return value if allow_number else None
    if isinstance(value, str):
        return value[:limit]
    return None


def build_truncated_message(
    message: dict[str, Any],
    original_bytes: int,
    *,
    compact: bool = False,
) -> dict[str, Any]:
    """把超帧消息压成可识别终态的小帧（不包含 evidence / verification 等大字段）。

    Args:
        message: 原始出站消息（Bridge → 浏览器协议）。
        original_bytes: 原始 JSON 负载字节数，写进 ``original_bytes`` 便于排障。
        compact: 极端小上限时的二次瘦身：丢 ``data`` 与 tool/question，只留终态标识。
    """
    msg_type = message.get("type")
    replacement: dict[str, Any] = {
        "type": msg_type if isinstance(msg_type, str) else "notification",
        "truncated": True,
        "original_bytes": int(original_bytes),
    }
    request_id = message.get("request_id")
    if isinstance(request_id, str):
        replacement["request_id"] = request_id[:TRUNCATED_META_MAX]
    method = message.get("method")
    if isinstance(method, str):
        replacement["method"] = method[:TRUNCATED_META_MAX]
    # Bridge 自身的 error 帧：保留 code 与短 message。
    code = message.get("code")
    if isinstance(code, str):
        replacement["code"] = code[:TRUNCATED_META_MAX]
    text = message.get("message")
    if isinstance(text, str):
        replacement["message"] = text[:TRUNCATED_ERROR_MAX]

    params = message.get("params")
    if not isinstance(params, dict):
        return replacement

    slim: dict[str, Any] = {}
    for key in ("type", "run_id"):
        value = _truncated_scalar(params.get(key), limit=TRUNCATED_META_MAX)
        if value is not None:
            slim[key] = value
    seq = params.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        slim["seq"] = seq
    status = _truncated_scalar(params.get("status"), limit=TRUNCATED_META_MAX)
    if status is not None:
        slim["status"] = status
    for key in ("error", "reason"):
        value = _truncated_scalar(params.get(key), limit=TRUNCATED_ERROR_MAX)
        if value is not None:
            slim[key] = value
    if not compact:
        # host_request 需要最小的上下文（工具名 / 问题），但不透传 arguments/preview。
        for key in ("tool", "question"):
            value = _truncated_scalar(params.get(key), limit=TRUNCATED_ERROR_MAX)
            if value is not None:
                slim[key] = value
        data = params.get("data")
        if isinstance(data, dict):
            final_text = data.get("final_text")
            if isinstance(final_text, str) and final_text:
                slim["data"] = {"final_text": final_text[:TRUNCATED_FINAL_TEXT_MAX]}
    if slim:
        replacement["params"] = slim
        # 便于扩展在不解析嵌套 params 的情况下识别终态（任务书允许 event_type/run_id）。
        event_type = slim.get("type")
        if isinstance(event_type, str):
            replacement["event_type"] = event_type
        run_id = slim.get("run_id")
        if isinstance(run_id, str):
            replacement["run_id"] = run_id
    return replacement
