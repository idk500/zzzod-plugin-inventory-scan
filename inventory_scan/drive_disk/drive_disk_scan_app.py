import os
import shutil
import time
from typing import TYPE_CHECKING, Optional

from cv2.typing import MatLike

from one_dragon.base.geometry.point import Point
from one_dragon.base.operation.operation_base import OperationResult
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import cv2_utils, os_utils
from one_dragon.utils.log_utils import log
from ..drive_disk import drive_disk_scan_const
from ..parser.drive_disk_parser import DriveDiskParser
from ..screenshot_cache import ScreenshotCache
from zzz_od.application.zzz_application import ZApplication
from zzz_od.context.zzz_context import ZContext

if TYPE_CHECKING:
    from ..ocr_worker import OcrWorker


class DriveDiskScanApp(ZApplication):

    def __init__(self, ctx: ZContext, screenshot_cache: Optional[ScreenshotCache] = None,
                 ocr_worker: Optional["OcrWorker"] = None):
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=drive_disk_scan_const.APP_ID,
            op_name=drive_disk_scan_const.APP_NAME,
            node_max_retry_times=100000,
            # 子应用没有独立工厂注册，不能走工厂取运行记录（否则抛「应用未注册」）
            use_factory_run_record=False,
        )

        # 当前位置（行，列）从(0,0)开始，对应界面上的(1,1)
        self.current_row_idx: int = 0
        self.current_col_idx: int = 0

        # 网格信息（每次换行时更新）
        self.grid_rows: list[list[Point]] = []  # 二维网格
        self.total_scanned: int = 0  # 已扫描总数

        # 截图缓存
        self.screenshot_cache: Optional[ScreenshotCache] = screenshot_cache
        self.ocr_worker = ocr_worker
        # 截图临时文件夹和序号
        self.screenshots_dir: Optional[str] = None
        self.screenshot_index: int = 0  # 全局递增序号

        # OCR处理标记
        self.ocr_processed: bool = False  # 防止重复OCR

        # 驱动盘属性解析器
        self.parser = DriveDiskParser()
        # 本次扫描的导出文件路径
        self.export_path: Optional[str] = None

        # 滚动翻页安全阀
        self.scroll_rounds: int = 0
        self._all_dup_rounds: int = 0
        self._last_scroll_new_count: int = 0

    def execute(self) -> OperationResult:
        """执行扫描"""
        try:
            result = super().execute()
            return result
        finally:
            pass

    def _prepare_screenshots_dir(self):
        """准备截图临时文件夹"""
        base_dir = os_utils.get_path_under_work_dir('.debug', 'inventory_screenshots')

        # 如果文件夹存在，清空
        if os.path.exists(base_dir):
            shutil.rmtree(base_dir)

        os.makedirs(base_dir, exist_ok=True)
        self.screenshots_dir = base_dir
        log.debug(f"截图文件夹已准备: {self.screenshots_dir}")

    def _save_screenshot(self, row: int, col: int, screenshot: MatLike):
        """保存截图到缓存并提交OCR任务"""
        if self.screenshot_cache is None:
            return

        # 裁剪驱动盘仓库区域
        try:
            storage_area = self.ctx.screen_loader.get_area('仓库-驱动仓库', '驱动盘属性')
            cropped = cv2_utils.crop_image_only(screenshot, storage_area.rect)

            # 保存到驱动盘缓存（调试模式下会同时保存到文件）
                        # 按内容去重保存 翻页重叠或误点产生的相同详情只入库一次
            index, dup = self.screenshot_cache.save_unique('drive_disk', cropped)
            self.screenshot_index = index + 1
            if dup:
                return

            # 提交OCR任务（异步处理）
            if self.ocr_worker is not None:
                self.ocr_worker.submit('disc', cropped, self.parser)
        except Exception as e:
            log.error(f"保存截图失败({row+1},{col+1}): {e}")

    @operation_node(name='初始化对齐', is_start_node=True)
    def initialize_align(self) -> OperationRoundResult:
        """开始前点击(1,1)确保坐标对齐"""
        screen = self.last_screenshot

        ctx = self.ctx.cv_service.run_pipeline('驱动盘方格', screen)
        if ctx.error_str is not None:
            return self.round_success(f'检测失败:{ctx.error_str}')

        if ctx.contours is None or len(ctx.contours) == 0:
            return self.round_success('未检测到方格')

        # 获取所有方格
        absolute_rects = ctx.get_absolute_rects()
        all_grids = []
        for x1, y1, x2, y2 in absolute_rects:
            center = Point(x1 + (x2 - x1) / 2, y1 + (y2 - y1) / 2)
            all_grids.append(center)

        # 网格化排序
        self.grid_rows = self._sort_grids(all_grids)

        if not self.grid_rows or len(self.grid_rows[0]) == 0:
            return self.round_success('网格为空')

        # 点击(1,1)确保对齐
        target = self.grid_rows[0][0]
        self.ctx.controller.click(target)

        return self.round_success(f'已对齐到(1,1) 检测到{len(self.grid_rows)}行')

    @node_from(from_name='初始化对齐')
    @node_from(from_name='扫描当前行', status='换行')  # 换行后重新检测网格
    @node_from(from_name='扫描当前行', status='滚动')  # 滚动后重新检测网格
    @operation_node(name='检测网格')
    def detect_grid(self) -> OperationRoundResult:
        """检测当前可见的驱动盘网格"""
        self.ctx.controller.mouse_move(Point(0, 0))  # 移动鼠标到(0,0)，避免遮挡方格
        time.sleep(0.02)  # 等待鼠标移动完成
        screen = self.screenshot()  # 重新截图，获取最新的网格布局

        ctx = self.ctx.cv_service.run_pipeline('驱动盘方格', screen)
        if ctx.error_str is not None:
            return self.round_success(f'检测失败:{ctx.error_str}')

        if ctx.contours is None or len(ctx.contours) == 0:
            return self.round_success('未检测到方格')

        # 获取所有方格的绝对坐标中心点
        absolute_rects = ctx.get_absolute_rects()
        all_grids = []
        for x1, y1, x2, y2 in absolute_rects:
            center = Point(x1 + (x2 - x1) / 2, y1 + (y2 - y1) / 2)
            all_grids.append(center)

        # 网格化排序
        self.grid_rows = self._sort_grids(all_grids)

        if not self.grid_rows:
            return self.round_success('网格为空')

        return self.round_success(f'检测到{len(self.grid_rows)}行 第1行{len(self.grid_rows[0])}列')

    @node_from(from_name='检测网格')
    @node_from(from_name='扫描当前行', status='换行')  # 换行后继续扫描
    @operation_node(name='扫描当前行')
    def scan_current_row(self) -> OperationRoundResult:
        """扫描当前行的所有格子（内部循环，不经过状态机）"""
        if not self.grid_rows:
            return self.round_fail('网格未初始化')

        # 检查是否超出范围
        if self.current_row_idx >= len(self.grid_rows):
            log.debug("驱动盘扫描完成")
            return self.round_success('扫描完成')

        current_row = self.grid_rows[self.current_row_idx]
        if self.current_col_idx >= len(current_row):
            log.debug("驱动盘扫描完成")
            return self.round_success('扫描完成')

        # 同行内循环点击，不经过状态机
        while self.current_col_idx < len(current_row):
            # 保存当前截图
            current_screenshot = self.last_screenshot.copy()
            self._save_screenshot(self.current_row_idx, self.current_col_idx, current_screenshot)
            self.total_scanned += 1

            # 计算下一个位置
            next_col = self.current_col_idx + 1

            # 检查是否到达行尾
            if next_col >= len(current_row):
                # 行尾，需要换行处理
                break

            # 同行点击下一个
            target = current_row[next_col]
            log.debug(f"[点击] 同行，点击({self.current_row_idx},{next_col})")
            self.ctx.controller.click(target)
            self.current_col_idx = next_col

            # 等待画面更新后重新截图
            self.screenshot()

        # 行结束：下一行存在则点击换行；否则滚轮翻页
        # 翻页后从(0,0)重扫全页 翻页重叠由缓存按内容去重兜底
        next_row = self.current_row_idx + 1
        next_col = 0

        if next_row < len(self.grid_rows) and len(self.grid_rows[next_row]) > 0:
            target = self.grid_rows[next_row][0]
            log.debug(f"[点击] 换行，点击({next_row},{next_col})")
            self.ctx.controller.click(target)
            self.current_row_idx = next_row
            self.current_col_idx = next_col
            return self.round_success('换行')

        # 没有更多已检测的行：滚轮向下翻页(安全阀: 签名判重/连续未动/轮数上限)
        self.scroll_rounds += 1
        if self.scroll_rounds > 400:
            log.warning(f"滚动轮数达到上限 {self.scroll_rounds}，强制结束扫描")
            return self.round_success('扫描完成')
        # 整页零新增判定：本轮扫到的格子全部已入库 说明列表没有前进 已到底
        if self.screenshot_cache is not None:
            new_count = self.screenshot_cache.get_new_count('drive_disk')
        else:
            new_count = self._last_scroll_new_count + 1
        new_since = new_count - self._last_scroll_new_count
        self._last_scroll_new_count = new_count
        if new_since == 0:
            self._all_dup_rounds += 1
        else:
            self._all_dup_rounds = 0
        if self._all_dup_rounds >= 2:
            log.info(f"连续 {self._all_dup_rounds} 轮整页无新增，判定已到列表底部，结束扫描")
            return self.round_success('扫描完成')
        scroll_pos = self.grid_rows[-1][0] if self.grid_rows else Point(960, 540)
        self.ctx.controller.scroll(3, scroll_pos)
        # 等滚动动画结束再点(0,0) 避免点中旧页物品
        time.sleep(0.5)
        first_pos = self.grid_rows[0][0] if self.grid_rows else Point(960, 540)
        self.ctx.controller.click(first_pos)
        log.debug(f"[滚动] 第{self.scroll_rounds}轮滚轮翻页")
        self.current_row_idx = 0
        self.current_col_idx = 0
        return self.round_success('滚动', wait=0.6)


    def _sort_grids(self, all_disks: list[Point]) -> list[list[Point]]:
        """将所有方格整理为二维网格"""
        if not all_disks:
            return []

        # 按 y 坐标排序
        sorted_by_y = sorted(all_disks, key=lambda d: d.y)

        rows = []
        current_row = [sorted_by_y[0]]
        y_tolerance = 50

        for i in range(1, len(sorted_by_y)):
            disk = sorted_by_y[i]
            if abs(disk.y - current_row[0].y) <= y_tolerance:
                current_row.append(disk)
            else:
                current_row.sort(key=lambda d: d.x)
                rows.append(current_row)
                current_row = [disk]

        if current_row:
            current_row.sort(key=lambda d: d.x)
            rows.append(current_row)

        return rows

    def on_pause(self):
        """暂停"""
        super().on_pause()

    def on_resume(self):
        """恢复"""
        super().on_resume()


def __debug():
    ctx = ZContext()
    ctx.init()
    ctx.run_context.start_running()
    app = DriveDiskScanApp(ctx)
    app.execute()


if __name__ == '__main__':
    __debug()
