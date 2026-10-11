"""桌面灵动岛展开体的屏幕边缘钳制（零 Qt 依赖，便于离线测试）。

形态机以球左上角为锚，球→胶囊向右长、胶囊→面板向下长。球被拖到屏幕
边缘后，固定锚点会让展开体溢出屏幕（实测右侧只露一条）。``clamp_frame``
在展开时把展开体左上角夹进当前屏幕工作区（去除任务栏的
availableGeometry，逻辑像素）；收回球时由 QML 回到球锚点。

QML 侧经 ``app.GeometryBridge`` 调用本函数，**钳制算法以此文件为唯一实现**，
修改规则请同步改这里的测试。
"""

from __future__ import annotations

#: 展开体距屏幕工作区边缘的最小留白（逻辑像素）。
MARGIN = 10


def clamp_frame(
    ball_x: int,
    ball_y: int,
    frame_w: int,
    frame_h: int,
    avail_x: int,
    avail_y: int,
    avail_w: int,
    avail_h: int,
    margin: int = MARGIN,
) -> tuple[int, int]:
    """计算展开体左上角，使其整体落在屏幕工作区内。

    Args:
        ball_x/ball_y: 球锚点左上角（逻辑像素，屏幕坐标）。
        frame_w/frame_h: 展开目标尺寸（胶囊或面板，逻辑像素）。
        avail_x/avail_y/avail_w/avail_h: 窗口所在屏幕工作区几何
            （``Screen.desktopAvailableX/Y/Width/Height``，逻辑像素）。
        margin: 与工作区边缘的最小留白。

    Returns:
        展开体左上角 ``(x, y)``。水平/垂直方向：工作区放得下时尽量保持球
        锚点，溢出则向内平移；工作区比展开体还小（极小屏）时贴工作区左上，
        至少保证左上角在屏内（不抛异常、不产生反转区间）。
    """
    lo_x = avail_x + margin
    hi_x = avail_x + avail_w - frame_w - margin
    lo_y = avail_y + margin
    hi_y = avail_y + avail_h - frame_h - margin

    # hi < lo 表示工作区放不下展开体：退化为贴左上，避免 clamp 区间反转。
    nx = lo_x if hi_x < lo_x else max(lo_x, min(hi_x, ball_x))
    ny = lo_y if hi_y < lo_y else max(lo_y, min(hi_y, ball_y))
    return nx, ny
