"""Fuzz target：任务特征抽取 :meth:`TaskFeatures.from_task` 与 :meth:`key`.

``from_task`` 直接面对宿主里**任意、不可信、可能含畸形 Unicode** 的自然语言：
正则、``lower``、``strip``、子串命中都在这条路径上，是典型的 fuzz 点。

平台说明：``atheris`` 在 PyPI 只发布 **Linux x86_64 + CPython 3.12/3.13/3.14**
的 wheel（无 Windows / macOS / cp311）。本文件不是 pytest 用例（放在 ``fuzzing/``，
不被 ``tests`` 收集），由 CI 的 ``fuzz`` job 运行：

    uv run python fuzzing/fuzz_features.py -max_total_time=45
"""

from __future__ import annotations

import sys

import atheris

from yai_core.learning.features import TaskFeatures

_LENGTH_BUCKETS = frozenset({"empty", "short", "medium", "long"})
_TOOL_BUCKETS = frozenset({"none", "few", "many"})


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    task = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 1024))

    feats = TaskFeatures.from_task(task)

    # 不变量：分桶值合法、key 结构稳定（bandit 状态表依赖固定 9 元组）。
    assert feats.length_bucket in _LENGTH_BUCKETS
    assert feats.tools_bucket in _TOOL_BUCKETS
    key = feats.key()
    assert isinstance(key, tuple)
    assert len(key) == 9

    # 不变量：strip 后为空的任务必落入 empty 桶。
    if not task.strip():
        assert feats.length_bucket == "empty"


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
