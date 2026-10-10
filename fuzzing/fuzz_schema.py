"""Fuzz target：JSON Schema 递归清洗 :func:`sanitize_schema`.

MCP / OpenAPI 桥喂进来的 ``input_schema`` 可能是任意深度、任意键型、混有
OpenAPI 私有注解的结构；递归清洗必须在这些输入下不崩溃、且保持类型形状。

平台/运行方式同 :mod:`fuzz_features`：

    uv run python fuzzing/fuzz_schema.py -max_total_time=45
"""

from __future__ import annotations

import json
import sys

import atheris

from yai_core.tools.schema import sanitize_schema


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    raw = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 2048))

    # 用随机 JSON 文本驱动：能解析就喂给清洗器，解析失败直接返回。
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return

    out = sanitize_schema(obj)

    # 不变量：根节点类型形状保持——dict→dict（空 dict 也会变成 EMPTY_OBJECT_SCHEMA）、
    # list→list、其余标量原样返回。
    if isinstance(obj, dict):
        assert isinstance(out, dict)
    elif isinstance(obj, list):
        assert isinstance(out, list)
    else:
        assert out == obj


if __name__ == "__main__":
    atheris.SetUp(sys.argv, TestOneInput)
    atheris.Fuzz()
