"""演示脚本：一次"越界被拒 → 改内被问 → 批准落盘"的完整审批链。

这条链把三处机制串成一次可断言的流程：

1. **越界被拒**：宿主给文件能力挂了资源作用域闸（``filesystem_gate``），
   写 ``secrets/`` 下的路径**在闸口就被拒**——而且"批准的目标 == 执行的目标"
   （规范化用的是与 handler 同一个函数，不靠约定）。
2. **改内被问**：改写成白名单内的路径后，调用**继续往下走**——命中参数级审批
   （金额超限）→ 发出 ``PERMISSION_ASKED`` → 通道确认。
3. **批准落盘**：人批准后才真正落盘，且事件流里能看见"为什么问、动哪个资源、
   风险多大"。

**能做什么 / 不能做什么**（照实说）：

- 本脚本跑的是**内核与治理层**，全程离线、无需任何模型或 Key；它打印的是真实事件流。
- 它**不渲染**灵动岛的 QML 像素（离屏渲染拿不到真实画面），所以"卡片长什么样"
  仍需人眼点一次——这一步在本仓库里只能由人确认。
- 想要 GIF：跑本脚本拿到事件时间线后，用真实桌面端手工复现一遍并录屏；
  本脚本的输出可以直接当"该出现什么"的对照表。

用法::

    python scripts/demo_approval_flow.py            # 打印事件时间线
    python scripts/demo_approval_flow.py --json     # 额外输出机器可读的事件序列
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from yai_core.batteries.filesystem import (  # noqa: E402
    FileSystemResources,
    filesystem_gate,
)
from yai_core.channels import CollectChannel  # noqa: E402
from yai_core.policy import AllowlistPolicy  # noqa: E402
from yai_core.tools import ToolExecutor, ToolRegistry  # noqa: E402
from yai_core.types import ApprovalPolicy, ApprovalRule, EventType, ToolGovernance  # noqa: E402


class _ApproveChannel(CollectChannel):
    """记录 confirm 调用，并按脚本设定回答"批 / 不批"。"""

    def __init__(self, approve: bool = True) -> None:
        super().__init__()
        self.approve = approve
        self.confirmed: list[tuple[str, dict]] = []

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        self.confirmed.append((tool_name, dict(arguments)))
        return self.approve


def _build(root: Path, approve: bool) -> tuple[ToolExecutor, _ApproveChannel]:
    """装配：一个带 ``amount`` 参数的宿主函数 + 资源作用域闸 + 参数级审批。

    这里刻意**不用**内置的 ``file_create``：它的绑定 schema 里没有 ``amount``，
    审批条件就会因"参数路径未声明"被正确拒绝（那是内核该做的事）。真实宿主里，
    要审批的参数本来就是工具自己的入参。
    """
    from yai_core import build_spec

    backend = FileSystemResources(root)

    async def write_note(path: str, content: str, amount: int) -> dict:
        """把一条笔记写进宿主目录（金额一并记录，便于按金额审批）。"""
        result = await backend.create(path, content)
        return {"path": result.path, "amount": amount, "bytes": result.size}

    registry = ToolRegistry()
    spec = build_spec(write_note)
    # 写操作挂资源作用域闸：只有 notes/ 下可写。
    spec.governance = ToolGovernance(
        gate=filesystem_gate(prefixes=("notes",), write=True),
        # 参数级审批：金额超过 1000 要人确认。
        approval=ApprovalPolicy(
            prompt="写入 {path}（金额 {amount} 元），确认吗？",
            when=(ApprovalRule(param="amount", op=">", value=1000, label="金额超限"),),
        ),
    )
    registry.register(spec)
    channel = _ApproveChannel(approve=approve)
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="auto"), channel)
    return executor, channel


def _timeline(events) -> list[str]:
    lines: list[str] = []
    for event in events:
        data = event.data
        if event.type == EventType.PERMISSION_ASKED:
            lines.append(
                "  → 问人："
                + json.dumps(
                    {
                        "tool": data.get("tool"),
                        "arguments": data.get("arguments"),
                    },
                    ensure_ascii=False,
                )
            )
        elif event.type == EventType.TOOL_CALL:
            lines.append(f"  → 执行：{data.get('tool')} {data.get('arguments')}")
        elif event.type == EventType.TOOL_RESULT:
            ok = data.get("ok")
            detail = data.get("error") or data.get("preview")
            lines.append(f"  → 结果：ok={ok} {detail}")
        elif event.type == EventType.ERROR:
            lines.append(f"  → 错误：{data.get('message') or data}")
    return lines


async def run(root: Path, approve: bool) -> dict:
    executor, channel = _build(root, approve)

    print("【1】越界写 secrets/ 下的路径（作用域闸应直接拒，不问人）")
    events, ok, text = await executor.execute(
        "write_note", {"path": "secrets/leak.txt", "content": "x", "amount": 10}
    )
    for line in _timeline(events):
        print(line)
    print(f"  结果：ok={ok} · {text}")
    print(f"  问人次数：{len(channel.confirmed)}（应为 0：闸口就拒了）")
    print()

    print("【2】改写成白名单内的路径，但金额超限（应问人）")
    events, ok, text = await executor.execute(
        "write_note", {"path": "notes/report.txt", "content": "金额 5000", "amount": 5000}
    )
    for line in _timeline(events):
        print(line)
    print(f"  结果：ok={ok} · 问人 {len(channel.confirmed)} 次"
          f"（应为 1，且答案={'批准' if approve else '拒绝'}）")
    print()

    print("【3】落盘结果")
    target = root / "notes" / "report.txt"
    print(f"  文件存在：{target.exists()}"
          + (f" · 内容：{target.read_text(encoding='utf-8')!r}" if target.exists() else ""))
    print()

    return {
        "out_of_scope_ok": ok,
        "confirmations": len(channel.confirmed),
        "written": target.exists(),
        "timeline": _timeline(events),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="审批链演示")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读结果")
    parser.add_argument(
        "--refuse", action="store_true", help="让人在审批处拒绝（看零副作用）"
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="yai-demo-") as tmp:
        root = Path(tmp) / "root"
        (root / "notes").mkdir(parents=True)
        (root / "secrets").mkdir(parents=True)
        result = asyncio.run(run(root, approve=not args.refuse))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
