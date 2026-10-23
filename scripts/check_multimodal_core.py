"""最小化的 AgentCore 多模态链路核对：真实模型是否真的收到了图片。

与 ``check_multimodal_wire.py`` 的区别：那个脚本直接调用模型后端，只核对
出站格式；这个脚本把图片放进 AgentCore 的会话历史，走完整的内核链路。
没有 API Key 时会退回离线脚本模型，只验证链路不报错，不代表模型看见了图。

用法::

    .\\.venv\\Scripts\\python.exe scripts\\check_multimodal_core.py ^
        "docs\\assets\\desktop-panel.png"

可选的第二个参数是给模型的任务文本。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from runner_common import build_model, load_dotenv  # noqa: E402
from yai_core import AgentCore, ChatMessage, ImagePart  # noqa: E402


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


async def main() -> int:
    parser = argparse.ArgumentParser(description="AgentCore 多模态真机核对")
    parser.add_argument(
        "image",
        nargs="?",
        default=str(ROOT / "docs" / "assets" / "desktop-panel.png"),
        help="要送进模型的图片路径",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default="请描述这张截图里有什么，并读出你能看到的主要文字。",
        help="给模型的文字任务",
    )
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.is_file():
        print(f"图片不存在：{image_path}", file=sys.stderr)
        return 2
    if image_path.stat().st_size > 5 * 1024 * 1024:
        print("图片超过 5 MB 内联上限，请先压缩。", file=sys.stderr)
        return 2

    load_dotenv()
    model, label = build_model()
    print(f"backend: {label}")
    if "离线" in label:
        print("警告：没有真实模型 Key，下面只验证内核链路，不代表模型看图。")

    core = AgentCore(model)
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    await core.memory.append_history(
        ChatMessage(
            role="user",
            content="用户发来一张图片。",
            parts=[
                ImagePart(
                    media_type=_media_type(image_path),
                    data=data,
                    ref=f"file://{image_path}",
                )
            ],
        )
    )
    try:
        result = await core.run(args.task)
        print(f"strategy: {result.strategy.value}")
        print(f"answer: {result.final_text}")
        for event in result.events:
            if event.type.value == "error":
                print(f"ERROR: {event.data}")
        return 0
    finally:
        await core.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
