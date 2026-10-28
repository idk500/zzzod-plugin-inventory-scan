# -*- coding: utf-8 -*-
"""驱动盘仓库网格工具 与扫描器共用的稳定帧/网格排序逻辑(独立副本 避免跨应用实例化)"""
import time
from typing import Callable, Optional

from cv2.typing import MatLike

from one_dragon.base.geometry.point import Point


def sort_grids(all_grids: list[Point]) -> list[list[Point]]:
    """将检测到的方格中心点整理为按行列排序的二维网格"""
    if not all_grids:
        return []

    sorted_by_y = sorted(all_grids, key=lambda d: d.y)

    rows = []
    current_row = [sorted_by_y[0]]
    y_tolerance = 50

    for i in range(1, len(sorted_by_y)):
        grid = sorted_by_y[i]
        if abs(grid.y - current_row[0].y) <= y_tolerance:
            current_row.append(grid)
        else:
            current_row.sort(key=lambda d: d.x)
            rows.append(current_row)
            current_row = [grid]

    if current_row:
        current_row.sort(key=lambda d: d.x)
        rows.append(current_row)

    return rows


def stable_screenshot(screenshot_fn: Callable[[], MatLike],
                      max_rounds: int = 5, interval: float = 0.25) -> MatLike:
    """等待画面稳定(连续两帧降采样灰度一致)后返回最新截图

    详情面板打开/切换有动画 中途截帧是漏扫与重复噪声的共同根因。
    超过 max_rounds 仍不稳定则按最后一帧返回(尽力而为 不阻塞流程)。
    """
    import cv2
    import numpy as np

    screen = screenshot_fn()
    prev_gray: Optional[np.ndarray] = None
    for _ in range(max_rounds):
        small = cv2.resize(screen, (240, 135))
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        if prev_gray is not None and float(
                np.abs(gray.astype(int) - prev_gray.astype(int)).mean()) < 0.5:
            break
        prev_gray = gray
        time.sleep(interval)
        screen = screenshot_fn()
    return screen
