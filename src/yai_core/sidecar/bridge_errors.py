"""Bridge 层可安全回给扩展的错误。"""

from __future__ import annotations


class BridgeError(Exception):
    """Bridge 层可安全回给扩展的错误。"""

    def __init__(self, message: str, *, code: str = "bridge_error") -> None:
        super().__init__(message)
        self.code = code


class BridgeOriginError(BridgeError):
    """扩展来源未授权（fail closed）。"""
