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

        # 网格几何（x0, y0, dx, dy）。拟合成功后缓存，翻页时检测不足也能沿用
        self._grid_spec: Optional[tuple[float, float, float, float]] = None
        # 扫描统计：空格跳过数、点击后详情未变化次数
        self.empty_cells: int = 0
        self.click_miss: int = 0

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

    # ------------------------------------------------------------------
    # 网格拟合
    #
    # 思路：仓库列表是等距固定网格，视觉检测只用来校准原点与间距。
    # 即使只认出 2~3 个方格，也能按几何外推出整页 9x4 的点击点，
    # 不会出现「认出几个就只点几个」的漏扫。
    # ------------------------------------------------------------------

    def _storage_rect(self):
        """驱动盘仓库列表区域"""
        return self.ctx.screen_loader.get_area('仓库-驱动仓库', '驱动盘仓库').rect

    def _detail_rect(self):
        """右侧详情面板区域（固定区域，OCR 与点击校验都用它）"""
        return self.ctx.screen_loader.get_area('仓库-驱动仓库', '驱动盘属性').rect

    def _detail_signature(self, screenshot: MatLike) -> Optional[MatLike]:
        """右侧详情区的灰度缩略图，用于判断「这一下点击有没有真的切换格子」"""
        try:
            rect = self._detail_rect()
            patch = screenshot[rect.y1:rect.y2, rect.x1:rect.x2]
            if patch is None or patch.size == 0:
                return None
            gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
            return cv2.resize(gray, (drive_disk_scan_const.DETAIL_SIG_SIZE,
                                     drive_disk_scan_const.DETAIL_SIG_SIZE))
        except Exception as e:
            log.debug(f'提取详情区指纹失败: {e}')
            return None

    def _cell_content_ratio(self, screenshot: MatLike, point: Point) -> float:
        """格子中心区域「有内容的像素」占比，用于跳过空槽

        内容判据只看亮度，并排除选中态的青色横条，因此不受色相偏移影响，
        用户开滤镜/HDR 时仍然成立。实测满格 0.29~0.41，空槽接近 0。
        """
        half = drive_disk_scan_const.CELL_PATCH_HALF
        x, y = int(point.x), int(point.y)
        h, w = screenshot.shape[0], screenshot.shape[1]
        patch = screenshot[max(0, y - half):min(h, y + half), max(0, x - half):min(w, x + half)]
        if patch.size == 0:
            return 1.0  # 取不到就不判空，避免误跳过有效格
        hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
        v = hsv[:, :, 2].astype(int)
        s = hsv[:, :, 1].astype(int)
        hue = hsv[:, :, 0].astype(int)
        cyan = (np.abs(hue - 84) <= 70) & (s >= 245) & (v >= 245)
        content = (v > drive_disk_scan_const.CELL_CONTENT_MIN_V) & (~cyan)
        return float(content.mean())

    def _fit_axis(self, values: list[float], default_pitch: float,
                  pitch_range: tuple[float, float], start: int, end: int
                  ) -> Optional[tuple[float, float, int]]:
        """把一维检测坐标拟合成 (首格坐标, 间距, 格数)

        先按容差聚类去掉重复检出，再取相邻间距的中位数作 pitch（比均值抗离群），
        最后把首格向左回推到区域起点，保证「第一格」不会因为没检出而丢失。
        """
        if len(values) < 2:
            return None

        vals = sorted(values)
        tol = default_pitch * drive_disk_scan_const.GRID_CLUSTER_TOL_RATIO
        groups: list[list[float]] = [[vals[0]]]
        for v in vals[1:]:
            if v - groups[-1][-1] <= tol:
                groups[-1].append(v)
            else:
                groups.append([v])
        centers = [float(np.mean(g)) for g in groups]

        pitch = default_pitch
        if len(centers) >= 2:
            diffs = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
            candidate = float(np.median(diffs))
            if pitch_range[0] <= candidate <= pitch_range[1]:
                pitch = candidate
            else:
                log.warning(f'网格间距拟合结果 {candidate:.1f} 超出合理区间 {pitch_range}，改用默认 {default_pitch}')

        origin = centers[0]
        if end > start:
            steps = int((origin - start) // pitch)
            if steps > 0:
                origin -= steps * pitch
        count = int((end - origin) // pitch) + 1 if end > origin else 1
        return origin, pitch, max(1, count)

    def _detect_points(self, screen: MatLike) -> list[Point]:
        """跑一次方格检测，返回所有候选格的中心点（可能为空）"""
        points: list[Point] = []
        cv_ctx = self.ctx.cv_service.run_pipeline('驱动盘方格', screen)
        if cv_ctx.error_str is not None:
            log.warning(f'方格检测流水线报错：{cv_ctx.error_str}')
            return points
        if cv_ctx.contours is None or len(cv_ctx.contours) == 0:
            log.warning('方格检测未认出任何轮廓')
            return points
        for x1, y1, x2, y2 in cv_ctx.get_absolute_rects():
            points.append(Point(x1 + (x2 - x1) / 2, y1 + (y2 - y1) / 2))
        return points

    def _resolve_grid(self, points: list[Point]) -> tuple[list[list[Point]], str]:
        """由检测点解析出完整网格；点不足时沿用上次网格或默认几何"""
        rect = self._storage_rect()

        spec: Optional[tuple[float, float, float, float]] = None
        note = ''
        if len(points) >= drive_disk_scan_const.GRID_FIT_MIN_POINTS:
            fx = self._fit_axis([p.x for p in points], drive_disk_scan_const.GRID_DX_FALLBACK,
                                drive_disk_scan_const.GRID_DX_RANGE, rect.x1, rect.x2)
            fy = self._fit_axis([p.y for p in points], drive_disk_scan_const.GRID_DY_FALLBACK,
                                drive_disk_scan_const.GRID_DY_RANGE, rect.y1, rect.y2)
            if fx is not None and fy is not None:
                x0, dx, cols = fx
                y0, dy, rows = fy
                spec = (x0, y0, dx, dy)
                note = f'拟合 {cols}列x{rows}行 间距({dx:.0f},{dy:.0f}) 原点({x0:.0f},{y0:.0f}) 检测点{len(points)}'

        if spec is None and self._grid_spec is not None:
            spec = self._grid_spec
            note = f'检测点仅 {len(points)} 个，沿用上一次网格'
        if spec is None:
            x0, y0 = drive_disk_scan_const.GRID_ORIGIN_FALLBACK
            spec = (x0, y0, drive_disk_scan_const.GRID_DX_FALLBACK, drive_disk_scan_const.GRID_DY_FALLBACK)
            note = f'检测点仅 {len(points)} 个，使用默认网格几何'

        x0, y0, dx, dy = spec
        cols = int((rect.x2 - x0) // dx) + 1 if rect.x2 > x0 else drive_disk_scan_const.GRID_COLS_FALLBACK
        rows = int((rect.y2 - y0) // dy) + 1 if rect.y2 > y0 else drive_disk_scan_const.GRID_ROWS_FALLBACK
        cols = max(1, min(cols, drive_disk_scan_const.GRID_COLS_FALLBACK + 1))
        rows = max(1, min(rows, drive_disk_scan_const.GRID_ROWS_FALLBACK + 1))

        grid = [[Point(x0 + c * dx, y0 + r * dy) for c in range(cols)] for r in range(rows)]
        self._grid_spec = (x0, y0, dx, dy)
        return grid, f'{note}，本页按 {cols}列x{rows}行 扫描'

    def _wait_detail_changed(self, prev: Optional[MatLike]) -> bool:
        """等待右侧详情面板相对点击前发生变化"""
        if prev is None:
            return True
        for _ in range(drive_disk_scan_const.DETAIL_WAIT_ROUNDS):
            time.sleep(drive_disk_scan_const.DETAIL_WAIT_INTERVAL)
            cur = self._detail_signature(self.screenshot())
            if cur is None:
                return True
            diff = float(np.abs(cur.astype(np.int16) - prev.astype(np.int16)).mean())
            if diff >= drive_disk_scan_const.DETAIL_CHANGE_MIN_DIFF:
                return True
        return False

    def _park_mouse(self) -> None:
        """把鼠标移到左上角，去掉格子上的悬停高亮

        截图里带悬停高亮时，同一格两次截图的像素不一致，内容去重（MD5）失效会重复入库，
        并让「整页无新增」永远不成立——翻到列表底部后一直空转。
        """
        self.ctx.controller.mouse_move(Point(0, 0))
        time.sleep(0.03)

    def _ensure_selected(self, target: Point, row: int, col: int) -> bool:
        """点击一个格子并确认它真的被选中

        Returns:
            True 需要截取该格；False 该格是空槽，跳过
        """
        screen = self.screenshot()
        ratio = self._cell_content_ratio(screen, target)
        if ratio < drive_disk_scan_const.CELL_EMPTY_CONTENT_RATIO:
            self.empty_cells += 1
            log.debug(f'({row + 1},{col + 1}) 判定为空槽（内容占比 {ratio:.3f}），跳过')
            return False

        prev = self._detail_signature(screen)
        self.ctx.controller.click(target)
        if self._wait_detail_changed(prev):
            self._park_mouse()
            return True

        # 再点一次，轻微右移，兼容点击落在格子边缘没触发的情况
        self.ctx.controller.click(Point(target.x + 8, target.y))
        if self._wait_detail_changed(prev):
            self._park_mouse()
            return True

        self.click_miss += 1
        log.warning(
            f'({row + 1},{col + 1}) 点击后详情面板未变化：可能是格子边缘点偏，'
            f'也可能与前一个盘完全同款。仍按当前画面取样，由内容去重兜底'
        )
        self._park_mouse()
        return True

    def _scan_done(self, reason: str) -> OperationRoundResult:
        """统一的扫描结束出口，顺带把本页统计打出来"""
        log.info(
            f'扫描结束（{reason}）：有效截图 {self.total_scanned} 个，'
            f'空槽跳过 {self.empty_cells} 个，点击未生效 {self.click_miss} 次'
        )
        return self.round_success('扫描完成')


    @operation_node(name='初始化对齐', is_start_node=True)
    def initialize_align(self) -> OperationRoundResult:
        """开始前点击(1,1)确保坐标对齐

        对齐点取自网格几何的首格，而不是「这次认出来的最小坐标」——
        少检时也不会对到一个中间格上。
        """
        screen = self.last_screenshot
        points = self._detect_points(screen)
        grid, note = self._resolve_grid(points)
        self.grid_rows = grid

        if not grid or not grid[0]:
            return self.round_success('网格为空')

        self.ctx.controller.click(grid[0][0])
        return self.round_success(f'已对齐到(1,1)：{note}')

    @node_from(from_name='初始化对齐')
    @operation_node(name='识别自检')
    def precheck_recognition(self) -> OperationRoundResult:
        """扫描前识别自检

        方格检测用的是固定 HSV 阈值（要求饱和度/亮度都接近 255），HDR、显卡滤镜、
        亮度对比度、分辨率或 UI 缩放一变就可能只剩一两个轮廓。

        现在点击点由网格几何外推，检测只做校准，所以「少检」本身不会再造成漏扫，
        这里只在两个边界上把关：
        - 检测点 >= 8：正常，记录一条信息；
        - 4~7 个点：几何仍然可信，告警后继续；
        - < 4 个点且仓库标题读到有货：画面既认不出格子也确认不了，存诊断图并中止。
        """
        self.ctx.controller.mouse_move(Point(0, 0))
        time.sleep(0.2)
        screen = self.screenshot()

        points = self._detect_points(screen)
        grid_count = len(points)
        grid, note = self._resolve_grid(points)
        if grid and grid[0]:
            self.grid_rows = grid

        self.repo_total = ocr_repo_total(self)

        if grid_count >= drive_disk_scan_const.GRID_PRECHECK_MIN_COUNT:
            log.info(f'识别自检通过[{self.op_name}]：检测到 {grid_count} 个方格，仓库共有 {self.repo_total} 个'
                     f'（{note}）')
            return self.round_success('继续扫描')

        if grid_count >= drive_disk_scan_const.GRID_FIT_MIN_POINTS:
            log.warning(
                f'识别自检[{self.op_name}]：仅检测到 {grid_count} 个方格，已按网格几何补齐（{note}）。'
                f'扫描会继续进行；若结果数量明显偏少，请检查 HDR/显卡滤镜/语言/分辨率设置'
            )
            return self.round_success('继续扫描')

        if self.repo_total is not None and self.repo_total > 0:
            self._save_debug_screenshot(f'precheck_fail_{self.app_id}')
            msg = (
                f'画面识别异常[{self.op_name}]：仓库内共有 {self.repo_total} 个，但只检测到 {grid_count} 个方格，'
                f'连网格原点都无法校准，已中止。请检查：游戏语言=简体中文、分辨率 1920x1080(16:9)、'
                f'HDR 关闭、显卡滤镜/色彩增强/护眼模式关闭、Windows 显示缩放 100%。'
                f'诊断截图：.debug/inventory_screenshots/precheck_fail_{self.app_id}.png'
            )
            log.error(msg)
            return self.round_fail(msg)

        log.warning(
            f'识别自检[{self.op_name}]：仅检测到 {grid_count} 个方格（仓库总数读取为 {self.repo_total}），'
            f'按默认网格几何继续（{note}）'
        )
        return self.round_success('继续扫描')

    @node_from(from_name='识别自检', status='继续扫描')
    @node_from(from_name='扫描当前行', status='换行')  # 换行后重新检测网格
    @node_from(from_name='扫描当前行', status='滚动')  # 滚动后重新检测网格
    @operation_node(name='检测网格')
    def detect_grid(self) -> OperationRoundResult:
        """解析当前可见网格，生成整页点击点

        视觉检测只用来校准几何。认出来的方格越少，越依赖网格先验外推，
        不会再出现「认出两个就只点两个」的漏扫。
        """
        self.ctx.controller.mouse_move(Point(0, 0))  # 移动鼠标到(0,0)，避免遮挡方格
        time.sleep(0.05)
        screen = self.screenshot()

        points = self._detect_points(screen)
        grid, note = self._resolve_grid(points)
        self.grid_rows = grid

        if not grid or not grid[0]:
            return self._scan_done('网格为空')

        log.info(f'[网格] {note}')
        return self.round_success(note)

    @node_from(from_name='检测网格')
    @node_from(from_name='扫描当前行', status='换行')  # 换行后继续扫描
    @operation_node(name='扫描当前行')
    def scan_current_row(self) -> OperationRoundResult:
        """扫描当前行的所有格子（内部循环，不经过状态机）"""
        if not self.grid_rows:
            # 空仓库(或本页确实没有驱动盘)属正常结束，不再当作失败
            return self._scan_done('未解析出网格，按空仓库处理')

        # 检查是否超出范围
        if self.current_row_idx >= len(self.grid_rows):
            return self._scan_done('已扫完所有行')

        current_row = self.grid_rows[self.current_row_idx]
        if self.current_col_idx >= len(current_row):
            return self._scan_done('当前行已到末尾')

        # 逐格处理：判空 -> 点击 -> 确认真的选中 -> 稳定截图入库
        while self.current_col_idx < len(current_row):
            row_i = self.current_row_idx
            col_i = self.current_col_idx
            target = current_row[col_i]

            if not self._ensure_selected(target, row_i, col_i):
                self.current_col_idx += 1
                continue

            # 等详情面板动画结束(连续两帧一致)再截取
            # 动画过渡帧会让同一盘像素指纹变化(重复入库) 或与上一盘过渡帧相同(误判重复漏扫)
            current_screenshot = self._stable_screenshot()
            self._save_screenshot(row_i, col_i, current_screenshot)
            self.total_scanned += 1
            self.current_col_idx += 1

        # 行结束：下一行存在则换行；否则滚轮翻页
        # 点击统一由 scan_current_row 的逐格逻辑负责，这里不预点，
        # 免得同一格被点两次、详情面板自然不变而被误判成「点击失效」
        next_row = self.current_row_idx + 1

        if next_row < len(self.grid_rows) and len(self.grid_rows[next_row]) > 0:
            self.current_row_idx = next_row
            self.current_col_idx = 0
            return self.round_success('换行')

        # 没有更多已检测的行：滚轮向下翻页(安全阀: 签名判重/连续未动/位移校验/轮数上限)
        self.scroll_rounds += 1
        if self.scroll_rounds > 400:
            log.warning(f"滚动轮数达到上限 {self.scroll_rounds}，强制结束扫描")
            return self._scan_done('滚动轮数达到上限')
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
            return self._scan_done(f'连续 {self._all_dup_rounds} 轮整页无新增')

        # 翻页有效性校验：滚轮没生效时旧逻辑会当成「已到底」静默结束
        before = self._grid_region_signature()
        self.ctx.controller.scroll(3, self._scroll_list_center())
        time.sleep(0.5)  # 等滚动动画结束
        after = self._grid_region_signature()

        if before is not None and after is not None and before.shape == after.shape:
            diff = float(np.abs(before.astype(np.int16) - after.astype(np.int16)).mean())
            if diff < drive_disk_scan_const.SCROLL_MOVE_MIN_DIFF:
                remaining = None if self.repo_total is None else self.repo_total - new_count
                # 滚不动有两种含义，按「翻了几轮 + 已扫到多少」区分：
                # 刚开局就滚不动且几乎没扫到东西 = 滚轮没生效（真故障）；
                # 已经翻过几页才滚不动 = 列表到底（正常结束）。
                scanned_ratio = (new_count / self.repo_total) if self.repo_total else 1.0
                stalled_early = self.scroll_rounds <= 2 and scanned_ratio < 0.5

                if stalled_early:
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

                return self._scan_done(
                    f'滚动后画面未移动(帧差 {diff:.2f})，已识别 {new_count} 个'
                    f'（仓库总数 {self.repo_total}），判定已到列表底部'
                )
            log.info(f'[滚动] 第{self.scroll_rounds}轮翻页位移正常，帧差 {diff:.2f}')
        else:
            log.warning('[滚动] 缺少有效网格区域，跳过位移校验')

        # 翻页后从 (0,0) 重扫全页；点击同样交给逐格逻辑，避免重复点击同一格
        self.current_row_idx = 0
        self.current_col_idx = 0
        return self.round_success('滚动', wait=0.6)

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
