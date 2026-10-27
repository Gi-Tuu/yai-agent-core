"""一条命令跑完本地全部门禁，给新人与发版前自检用。

顺序从快到慢、从便宜到贵，任何一步失败立刻停下（后面的步骤依赖前面的结论）：

    1. 锁定文件与 pyproject 一致        uv lock --check
    2. Lint（CI 同命令）                ruff check src tests examples scripts fuzzing shell
    3. 内核类型检查                     mypy src/yai_core
    4. 版本与打包守卫（快）             pytest 版本一致性 + sdist 链接完整性
    5. 全量离线测试 + 覆盖率门禁        pytest --cov=src/yai_core --cov-fail-under=90
    6. 两宿主冒烟（带业务断言）         python scripts/smoke_test.py

全程离线：不需要网络、不需要 API Key。

用法::

    python scripts/check_all.py            # 全量
    python scripts/check_all.py --fast     # 跳过第 5 步（改一行文档时用）
    python scripts/check_all.py --list     # 只打印会跑什么
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _steps(*, fast: bool) -> list[tuple[str, list[str] | None]]:
    """返回 (说明, 命令)；命令为 None 表示当前环境缺工具、应跳过。"""
    uv = shutil.which("uv")
    steps: list[tuple[str, list[str] | None]] = [
        ("锁定文件", [uv, "lock", "--check"] if uv else None),
        (
            "Lint",
            [PY, "-m", "ruff", "check", "src", "tests", "examples", "scripts", "fuzzing", "shell"],
        ),
        ("类型检查", [PY, "-m", "mypy", "src/yai_core"]),
        (
            "版本与打包守卫",
            [
                PY,
                "-m",
                "pytest",
                "-q",
                "tests/test_version_consistency.py",
                "tests/test_sdist_link_integrity.py",
            ],
        ),
        (
            "全量离线测试 + 覆盖率",
            [PY, "-m", "pytest", "-q", "--cov=src/yai_core", "--cov-fail-under=90"],
        ),
        ("两宿主冒烟", [PY, "scripts/smoke_test.py"]),
    ]
    if fast:
        steps = [s for s in steps if s[0] != "全量离线测试 + 覆盖率"]
    return steps


def main() -> int:
    parser = argparse.ArgumentParser(description="本地全部门禁（离线）")
    parser.add_argument("--fast", action="store_true", help="跳过全量测试，只跑快检")
    parser.add_argument("--list", action="store_true", help="只打印步骤，不执行")
    args = parser.parse_args()

    steps = _steps(fast=args.fast)
    if args.list:
        for index, (label, cmd) in enumerate(steps, 1):
            print(f"{index}. {label}" + ("" if cmd is not None else "（环境缺 uv，将跳过）"))
        return 0

    results: list[tuple[str, str, float]] = []
    for index, (label, cmd) in enumerate(steps, 1):
        if cmd is None:
            print(f"[{index}/{len(steps)}] {label} … 跳过（未安装 uv）")
            results.append((label, "跳过", 0.0))
            continue
        print(f"[{index}/{len(steps)}] {label} …", flush=True)
        started = time.monotonic()
        # 不捕获输出：长步骤（全量测试）要能看到进度；失败时用退出码定位。
        code = subprocess.run(cmd, cwd=ROOT).returncode
        elapsed = time.monotonic() - started
        if code != 0:
            print(f"\n✗ {label} 失败（exit {code}）")
            return code
        results.append((label, "通过", elapsed))
        print(f"  ✓ {label}（{elapsed:.1f}s）")

    print("\n结果汇总：")
    for label, status, elapsed in results:
        print(f"  {status:4s} {label}" + (f"（{elapsed:.1f}s）" if elapsed else ""))
    print("\n全部门禁通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
