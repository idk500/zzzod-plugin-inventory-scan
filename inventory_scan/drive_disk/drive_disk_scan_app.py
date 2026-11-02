import os
import shutil
import time
from typing import TYPE_CHECKING, Optional

import cv2
import numpy as np
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
from ..repo_total import ocr_repo_total
from ..screenshot_cache import ScreenshotCache
from ..zzz_compat import factory_record_kwargs
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
            **factory_record_kwargs(),
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

        # 仓库标题 OCR 读到的已拥有总数（扫描前自检时读取，用于翻页位移校验）
        self.repo_total: Optional[int] = None

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

    def _stable_screenshot(self, max_rounds: int = 5, interval: float = 0.25) -> MatLike:
        """等待画面稳定(连续两帧降采样灰度一致)后返回最新截图

        详情面板打开/切换有动画 中途截帧是漏扫与重复噪声的共同根因。
        超过 max_rounds 仍不稳定则按最后一帧返回(尽力而为 不阻塞流程)。
        """
        import cv2
        import numpy as np

        screen = self.screenshot()
        prev_gray: Optional[np.ndarray] = None
        for _ in range(max_rounds):
            small = cv2.resize(screen, (240, 135))
            gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
            if prev_gray is not None and float(np.abs(gray.astype(int) - prev_gray.astype(int)).mean()) < 0.5:
                break
            prev_gray = gray
            time.sleep(interval)
            screen = self.screenshot()
        return screen

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

    def _save_debug_screenshot(self, name: str) -> Optional[str]:
        """保存整屏诊断截图，失败不影响流程"""
        try:
            debug_dir = os_utils.get_path_under_work_dir('.debug', 'inventory_screenshots')
            os.makedirs(debug_dir, exist_ok=True)
            path = os.path.join(debug_dir, f'{name}.png')
            cv2.imwrite(path, cv2.cvtColor(self.screenshot(), cv2.COLOR_RGB2BGR))
            log.info(f'诊断截图已保存: {path}')
            return path
        except Exception as e:
            log.error(f'保存诊断截图失败: {e}')
            return None

    def _grid_region_signature(self) -> Optional[MatLike]:
        """取当前网格外接矩形的固定尺寸灰度缩略图，用于比较翻页前后画面是否真的移动

        用外接矩形而不是整屏，避免右侧详情面板和顶部计数条的变化干扰判断。
        """
        if not self.grid_rows:
            return None
        try:
            xs = [p.x for row in self.grid_rows for p in row]
            ys = [p.y for row in self.grid_rows for p in row]
            x1 = max(0, int(min(xs)) - 30)
            y1 = max(0, int(min(ys)) - 30)
            x2 = int(max(xs)) + 30
            y2 = int(max(ys)) + 30
            region = self.screenshot()[y1:y2, x1:x2]
            if region.size == 0:
                return None
            gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
            return cv2.resize(gray, (160, 160))
        except Exception as e:
            log.error(f'提取网格区域截图失败: {e}')
            return None

    def _scroll_list_center(self) -> Point:
        """列表区域中心点，作为滚轮落点

        旧逻辑把滚轮打在最后一行的第一个格子上，格子检测不全时可能落在列表外或滚动条外，
        滚轮事件被吞掉却当成「已到底」。
        """
        if not self.grid_rows:
            return Point(960, 540)
        xs = [p.x for row in self.grid_rows for p in row]
        ys = [p.y for row in self.grid_rows for p in row]
        return Point((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

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
    @operation_node(name='识别自检')
    def precheck_recognition(self) -> OperationRoundResult:
        """扫描前识别自检

        方格检测用的是固定 HSV 阈值（要求饱和度/亮度都接近 255），HDR、显卡滤镜、
        亮度对比度、分辨率或 UI 缩放一变就可能只剩一两个轮廓。旧逻辑对此完全不报错，
        表现为「只扫一两个就结束」的静默漏扫。这里把「方格数」和仓库标题总数交叉校验，
        把静默降级变成明确失败；顺手重测一次网格，避免初始化时鼠标压住格子导致的少检。
        """
        self.ctx.controller.mouse_move(Point(0, 0))
        time.sleep(0.2)
        screen = self.screenshot()

        grid_count = 0
        cv_ctx = self.ctx.cv_service.run_pipeline('驱动盘方格', screen)
        if cv_ctx.error_str is None and cv_ctx.contours is not None and len(cv_ctx.contours) > 0:
            centers = []
            for x1, y1, x2, y2 in cv_ctx.get_absolute_rects():
                centers.append(Point(x1 + (x2 - x1) / 2, y1 + (y2 - y1) / 2))
            rows = self._sort_grids(centers)
            if rows:
                self.grid_rows = rows
                grid_count = sum(len(row) for row in rows)

        self.repo_total = ocr_repo_total(self)

        if grid_count >= drive_disk_scan_const.GRID_PRECHECK_MIN_COUNT:
            log.info(f'识别自检通过[{self.op_name}]：检测到 {grid_count} 个方格，仓库共有 {self.repo_total} 个')
            return self.round_success('继续扫描')

        if self.repo_total is not None and self.repo_total > grid_count:
            self._save_debug_screenshot(f'precheck_fail_{self.app_id}')
            msg = (
                f'画面识别异常[{self.op_name}]：仓库内共有 {self.repo_total} 个，但只检测到 {grid_count} 个方格，'
                f'继续扫描会严重漏扫，已中止。请检查：游戏语言=简体中文、分辨率 1920x1080(16:9)、'
                f'HDR 关闭、显卡滤镜/色彩增强/护眼模式关闭、Windows 显示缩放 100%。'
                f'诊断截图：.debug/inventory_screenshots/precheck_fail_{self.app_id}.png'
            )
            log.error(msg)
            return self.round_fail(msg)

        log.warning(
            f'识别自检[{self.op_name}]：仅检测到 {grid_count} 个方格（仓库总数读取为 {self.repo_total}），'
            f'无法确认是否为环境问题，按现状继续'
        )
        return self.round_success('继续扫描')

    @node_from(from_name='识别自检', status='继续扫描')
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
            # 空仓库(或本页确实没有驱动盘)属正常结束，不再当作失败
            log.info('未检测到任何驱动盘方格，按空仓库处理')
            return self.round_success('扫描完成')

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
            # 等详情面板动画结束(连续两帧一致)再截取
            # 动画过渡帧会让同一盘像素指纹变化(重复入库) 或与上一盘过渡帧相同(误判重复漏扫)
            current_screenshot = self._stable_screenshot()
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

        # 没有更多已检测的行：滚轮向下翻页(安全阀: 签名判重/连续未动/位移校验/轮数上限)
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

        # 翻页有效性校验：滚轮没生效时旧逻辑会当成「已到底」静默结束
        before = self._grid_region_signature()
        self.ctx.controller.scroll(3, self._scroll_list_center())
        time.sleep(0.5)  # 等滚动动画结束
        after = self._grid_region_signature()

        if before is not None and after is not None and before.shape == after.shape:
            diff = float(np.abs(before.astype(np.int16) - after.astype(np.int16)).mean())
            if diff < drive_disk_scan_const.SCROLL_MOVE_MIN_DIFF:
                # 去重后少 1~2 个属正常的重复内容差异，超过就不该再当成「到底」
                remaining = None if self.repo_total is None else self.repo_total - new_count
                reached_bottom = (
                    (remaining is not None and remaining <= 2)
                    or (remaining is None and new_since == 0)
                )
                if reached_bottom:
                    log.info(
                        f'滚动后画面未移动(帧差 {diff:.2f})，已识别 {new_count} 个'
                        f'（仓库总数 {self.repo_total}），判定已到列表底部'
                    )
                    return self.round_success('扫描完成')

                self._save_debug_screenshot(f'scroll_no_move_{self.app_id}')
                detail = (
                    f'仓库共 {self.repo_total} 个，已识别 {new_count} 个，尚有 {remaining} 个未扫到'
                    if remaining is not None
                    else f'本轮已新增 {new_since} 个但画面未移动，无法确认是否到底'
                )
                msg = (
                    f'滚轮翻页无效：{detail}。继续扫描只会重复同一页，已中止。'
                    f'常见原因是详情面板遮挡列表或滚动位置落在列表之外；'
                    f'诊断截图 .debug/inventory_screenshots/scroll_no_move_{self.app_id}.png'
                )
                log.error(msg)
                return self.round_fail(msg)
            log.info(f'[滚动] 第{self.scroll_rounds}轮翻页位移正常，帧差 {diff:.2f}')
        else:
            log.warning('[滚动] 缺少有效网格区域，跳过位移校验')

        first_pos = self.grid_rows[0][0] if self.grid_rows else Point(960, 540)
        self.ctx.controller.click(first_pos)
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
