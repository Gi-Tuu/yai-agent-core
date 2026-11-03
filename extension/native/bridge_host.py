"""Native Messaging Host 的可执行入口（v0.26 Batch 4）。

Chrome / Edge 的 native host manifest 的 ``path`` 必须指向**可执行文件**，
而 ``python.exe`` 不能携带 ``-m yai_core.sidecar.native_bridge`` 参数。因此推荐
用 PyInstaller 把本文件打成单文件 exe::

    pyinstaller --onefile --name yai-bridge extension/native/bridge_host.py

然后把 manifest 的 ``path`` 指向生成的 ``yai-bridge.exe``。开发机上也可以直接
``python extension/native/bridge_host.py chrome-extension://<id>/`` 调试。
"""

from __future__ import annotations

import asyncio

from yai_core.sidecar.native_bridge import main

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
