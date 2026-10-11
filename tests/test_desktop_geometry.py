"""桌面灵动岛展开体边缘钳制的离线测试（纯函数，不启动 Qt）。

坐标均为逻辑像素。真机参考：DPR=1.5、物理 2560x1600 → 逻辑约 1707x1067，
工作区扣除任务栏后约 1707x1027；面板 470x620、胶囊 340x52、球 56。
钳制算法唯一实现在 shell/desktop/geometry.py，QML 经 GeometryBridge 调用。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# shell/desktop/__init__.py 会连带导入 PySide6，CI 精简矩阵没有 Qt；
# geometry.py 本身零 Qt 依赖，故按文件路径直接加载（与 test_tool_vault.py 同模式），
# 让边缘钳制纯函数在未装 [desktop] extra 的 CI 上也能真正运行。
_ROOT = Path(__file__).resolve().parents[1]
_GEOM_PATH = _ROOT / "shell" / "desktop" / "geometry.py"


def _load_geometry():
    spec = importlib.util.spec_from_file_location("yai_desktop_geometry", _GEOM_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_geom = _load_geometry()
MARGIN = _geom.MARGIN
clamp_frame = _geom.clamp_frame

# 主屏工作区（逻辑像素，去除任务栏）。
AVAIL = (0, 0, 1707, 1027)
PANEL = (470, 620)
CAPSULE = (340, 52)


def test_top_center_panel_stays_put() -> None:
    # 球顶部居中（place_top_center: x=(1707-56)//2=825, y=10），放得下不偏移。
    x, y = clamp_frame(825, 10, *PANEL, *AVAIL)
    assert (x, y) == (825, 10)


def test_right_edge_panel_shifts_back_onscreen() -> None:
    # 球拖到右侧（物理 x=2393 / 1.5 ≈ 1595），面板右缘原本溢出到 2065。
    x, y = clamp_frame(1595, 10, *PANEL, *AVAIL)
    assert x == 1707 - 470 - MARGIN       # 1227：面板右缘 1697 = 屏宽-留白
    assert x + 470 <= 1707 - MARGIN
    assert y == 10


def test_right_edge_capsule_shifts_back_onscreen() -> None:
    x, _ = clamp_frame(1595, 10, *CAPSULE, *AVAIL)
    assert x == 1707 - 340 - MARGIN       # 1357：胶囊右缘 1697
    assert x + 340 <= 1707 - MARGIN


def test_bottom_edge_panel_shifts_up() -> None:
    # 球拖到下方 y=900，面板 620 高原本底缘到 1520 溢出工作区 1027。
    x, y = clamp_frame(825, 900, *PANEL, *AVAIL)
    assert x == 825
    assert y == 1027 - 620 - MARGIN       # 397：底缘 1017 = 工作区底-留白
    assert y + 620 <= 1027 - MARGIN


def test_negative_origin_secondary_screen() -> None:
    # 左侧副屏工作区原点为负（多屏）：球在屏内左缘不应被推走，出屏则夹回。
    avail = (-1920, 0, 1920, 1027)
    x, _ = clamp_frame(-1915, 10, *CAPSULE, *avail)
    assert x == -1920 + MARGIN           # -1910
    x2, _ = clamp_frame(-1500, 10, *CAPSULE, *avail)
    assert x2 == -1500                   # 屏内足够靠左，保持锚点


def test_tiny_screen_smaller_than_frame_falls_back_to_top_left() -> None:
    # 工作区比面板还小：区间反转时贴左上，不抛异常、不产生矛盾坐标。
    x, y = clamp_frame(300, 200, *PANEL, 0, 0, 400, 300)
    assert (x, y) == (MARGIN, MARGIN)


def test_custom_margin_zero() -> None:
    x, y = clamp_frame(825, 0, *PANEL, 0, 0, 1707, 1027, margin=0)
    assert (x, y) == (825, 0)


def test_top_right_corner_both_axes_clamped() -> None:
    # 球在右下角，x/y 同时溢出。
    x, y = clamp_frame(1650, 980, *PANEL, *AVAIL)
    assert x == 1707 - 470 - MARGIN
    assert y == 1027 - 620 - MARGIN
