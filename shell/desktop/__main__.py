"""``python -m shell.desktop`` 入口：原生桌面端（PySide6 + QML）。"""

from __future__ import annotations

from shell.desktop.app import main

if __name__ == "__main__":
    raise SystemExit(main())
