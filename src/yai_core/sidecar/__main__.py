"""``python -m yai_core.sidecar``：从 stdin 读 NDJSON，向 stdout 写 NDJSON。"""

from __future__ import annotations

import asyncio
import sys


def main() -> int:
    from yai_core.sidecar.server import SidecarServer

    try:
        asyncio.run(SidecarServer().serve_stdio())
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
