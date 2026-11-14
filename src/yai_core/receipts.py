"""执行收据与防篡改链。

从 ``types.py`` 拆出：收据是独立的数据契约，测试与宿主可直接从
``yai_core.receipts`` 导入；``types`` 继续 re-export 保持兼容。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

ReceiptStatus = Literal["not_started", "succeeded", "failed", "unknown", "partial"]


def _receipt_digest(payload: Mapping[str, Any]) -> str:
    """对收据事实做规范化 JSON 编码后取 SHA-256。"""
    canonical = json.dumps(
        dict(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionReceipt:
    """一次工具执行的机器可读收据，用于判断"能不能重试"。

    - ``not_started``：参数/权限/频率/预览等在 handler 之前失败，没有副作用；
    - ``succeeded``：handler 正常返回；
    - ``failed``：执行器明确知道失败（例如沙箱返回 ``ok=False``）；
    - ``unknown``：超时、同步 handler 被放弃、handler 抛错或沙箱调用异常——
      副作用可能已经发生，**不能盲目重试**；
    - ``partial``：组合工具前几步成功、后续步骤失败。

    ``idempotency_key`` 只来自宿主声明的生成器；内核的进程内缓存只在这个 key
    存在时生效，跨进程/多实例去重仍由宿主负责。
    """

    call_id: str
    tool: str
    status: ReceiptStatus
    retryable: bool = False
    idempotency_key: str = ""
    error: str = ""
    duration_ms: float = 0.0
    replayed: bool = False
    #: 后置条件检查结论：``""``（未跑 / 不适用）、``confirmed``、``rejected``、``error``。
    postcondition: str = ""
    #: 上一条收据的 ``receipt_hash``；第一条为空串。串起来就是一条防篡改链。
    prev_hash: str = ""
    #: 本条收据内容的 SHA-256（含 ``prev_hash``），由 :meth:`seal` 计算。
    receipt_hash: str = ""

    def _hash_payload(self, prev_hash: str) -> dict[str, Any]:
        """参与哈希的字段：收据的全部事实 + 前一条的哈希。"""
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "status": self.status,
            "retryable": self.retryable,
            "idempotency_key": self.idempotency_key,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "replayed": self.replayed,
            "postcondition": self.postcondition,
            "prev_hash": prev_hash,
        }

    def seal(self, prev_hash: str) -> ExecutionReceipt:
        """返回接进链之后的新收据（dataclass 是 frozen 的，不原地修改）。"""
        return replace(
            self,
            prev_hash=prev_hash,
            receipt_hash=_receipt_digest(self._hash_payload(prev_hash)),
        )

    def __post_init__(self) -> None:
        if self.status not in (
            "not_started",
            "succeeded",
            "failed",
            "unknown",
            "partial",
        ):
            raise ValueError(f"未知执行状态：{self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._hash_payload(self.prev_hash),
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionReceipt:
        """从 ``to_dict()`` 的产物还原；非法状态抛 ValueError，便于校验时判失败。"""
        return cls(
            call_id=str(data.get("call_id", "")),
            tool=str(data.get("tool", "")),
            status=data.get("status", "unknown"),
            retryable=bool(data.get("retryable", False)),
            idempotency_key=str(data.get("idempotency_key", "")),
            error=str(data.get("error", "")),
            duration_ms=float(data.get("duration_ms", 0.0)),
            replayed=bool(data.get("replayed", False)),
            postcondition=str(data.get("postcondition", "")),
            prev_hash=str(data.get("prev_hash", "")),
            receipt_hash=str(data.get("receipt_hash", "")),
        )


def verify_receipt_chain(receipts: Iterable[Mapping[str, Any]]) -> bool:
    """校验一串执行收据的防篡改链。

    规则：第一条的 ``prev_hash`` 为空；之后每条都指向上一条的 ``receipt_hash``；
    每条自身的 ``receipt_hash`` 必须等于其内容的 SHA-256。任何一条被改动、
    被抽走或被换序都会返回 False，不会抛异常。
    """
    prev = ""
    for raw in receipts:
        try:
            receipt = ExecutionReceipt.from_dict(raw)
        except (TypeError, ValueError):
            return False
        if not receipt.receipt_hash:
            return False
        if receipt.prev_hash != prev:
            return False
        if receipt.receipt_hash != _receipt_digest(receipt._hash_payload(receipt.prev_hash)):
            return False
        prev = receipt.receipt_hash
    return True
