"""代理人扫描（攻略页方案）。

背景（2026-11-05 用户提出并实机验证）：
旧流程在「基础 / 技能 / 核心」三个页签各截一次图再拼接，每个角色 4 次点击、
4 次截图，而且依赖页签切换后的状态检测。实机上「按钮-代理人基础」在攻略页
并不存在，流程空转，导致导出里的 characters 长期恒为 0。

新流程利用代理人攻略页（菜单 → 代理人 → 左上角「代理人攻略」）：
- 左侧是 3 列网格，滚轮滚一格正好推进一行（实机确认是整行吸附，槽位固定）
- 点选格子后，右上角 AGENT INFO 显示角色名
- 所以**不需要任何角色头像模板**，OCR 名字 + 翻译表模糊匹配即可确定角色

已拥有 / 未获得的区分（用户 2026-11-05 指出）：
网格里灰色头像表示未获得，此时右侧原本的「特训方案」按钮位置会变成 EMPTY。
因此每格右键详情里检查该处是否出现「特训方案」，只有已获得才入库。

代价：技能等级 / 影画 / 核心技等级在这一页看不到，统一给默认值。
换来的是每角色只需 1 次点击 + 2 次小图 OCR，扫描稳定性和速度都大幅提升。

实机测量基准：1920x1080 客户区（框架标准分辨率）。
"""
import time
from typing import TYPE_CHECKING, Optional

import cv2
import numpy as np
from cv2.typing import MatLike

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import cv2_utils
from one_dragon.utils.log_utils import log
from ..parser.agent_parser import AgentParser
from ..zzz_compat import factory_record_kwargs
from zzz_od.application.zzz_application import ZApplication
from zzz_od.context.zzz_context import ZContext

if TYPE_CHECKING:
    from ..ocr_worker import OcrWorker
    from ..screenshot_cache import ScreenshotCache


# ---- 代理人攻略页网格参数（1920x1080 实机测量）----
COL_LEFT = (181, 338, 493)            # 三列卡片的左边界
COL_WIDTH = 155                       # 单列宽度
GRID_BOX = Rect(180, 40, 660, 1080)   # 左侧网格整体（行投影 + 稳定性比对）
TOP_MARK_BOX = Rect(180, 85, 660, 400)    # 列表顶部才有的「独家频段 / 已拥有」标签区
UP_MARK_BOX = Rect(180, 85, 660, 190)     # 「独家频段」UP 池标签（在已拥有标签上方）
FALLBACK_ROW_Y = 146                  # 行检测失败时的兜底首行中心
NAME_BOX = Rect(720, 180, 1030, 260)  # 右上角 AGENT INFO 角色名区域
OWNED_BOX = Rect(1378, 268, 1502, 322)  # 已获得时显示「特训方案」，未获得显示 EMPTY
LEVEL_DY = (46, 88)                   # 等级数字纵向范围（相对首行中心）
LEVEL_DX = (7, 62)                    # 等级数字横向偏移（相对列左边界）
SCROLL_POS = Point(416, 700)          # 滚动时鼠标停留位置（网格中央）

SCROLL_STEP = 8                       # 每次滚动格数
SCROLL_MAX_ROUNDS = 12                # 滚到边界最多几轮
SCROLL_SETTLE = 0.45                  # 每轮滚动后等待
MAX_ROWS = 80                         # 行数保险上限
MAX_EMPTY_ROWS = 3                    # 连续多少行没有新角色就认为已到未获得区


class AgentScanApp(ZApplication):

    def __init__(self, ctx: ZContext, screenshot_cache: Optional["ScreenshotCache"] = None,
                 ocr_worker: Optional["OcrWorker"] = None):
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id='agent_scan',
            op_name='角色扫描',
            # 子应用没有独立工厂注册，不能走工厂取运行记录（否则抛「应用未注册」）
            **factory_record_kwargs(),
        )
        self.screenshot_cache = screenshot_cache
        self.ocr_worker = ocr_worker
        self.parser = AgentParser()

        self.click_wait: float = 0.45    # 点击后等详情刷新
        self.scroll_wait: float = 0.5    # 滚动后等网格吸附
        self._counter: int = 0
        self._row_y: int = FALLBACK_ROW_Y

    @operation_node(name='扫描代理人', is_start_node=True)
    def scan_agents(self) -> OperationRoundResult:
        """逐行扫描代理人攻略页，读名字与等级。"""
        ctrl = self.ctx.controller

        row_y = self._goto_list_top()
        if row_y is None:
            return self.round_fail('无法定位代理人列表')
        self._row_y = row_y
        log.info(f'[角色扫描] 列表顶部首行中心 y={row_y}')

        seen: set[str] = set()
        unknown: list[str] = []
        unowned = 0
        empty_rows = 0
        row_count = 0

        for row in range(MAX_ROWS):
            row_new = 0
            row_owned = 0
            for col_idx, left in enumerate(COL_LEFT):
                center = Point(left + COL_WIDTH // 2, self._row_y)
                ctrl.click(center)
                time.sleep(self.click_wait)

                ok, screen = ctrl.screenshot()
                if not ok or screen is None:
                    log.warning(f'[角色扫描] 第{row}行第{col_idx}列截图失败')
                    continue

                if not self._is_owned(screen):
                    unowned += 1
                    log.debug(f'[角色扫描] 第{row}行第{col_idx}列未获得，跳过')
                    continue
                row_owned += 1

                raw_name = self._read_name(screen)
                if not raw_name:
                    log.debug(f'[角色扫描] 第{row}行第{col_idx}列未识别到名字')
                    continue

                key = self.parser.match_agent(raw_name)
                if key is None:
                    log.warning(f'[角色扫描] 名字 {raw_name!r} 未能匹配角色表，跳过')
                    if raw_name not in unknown:
                        unknown.append(raw_name)
                    continue

                if key in seen:
                    continue

                seen.add(key)
                row_new += 1
                level = self._read_level(screen, col_idx)
                self._append_agent(key, level)
                log.info(f'[角色扫描] 第{row}行第{col_idx}列 -> {raw_name} => {key} (Lv{level})')

            row_count += 1
            if row_new == 0:
                empty_rows += 1
                log.debug(f'[角色扫描] 第{row}行无新角色（连续{empty_rows}行，已获得{row_owned}个）')
                if empty_rows >= MAX_EMPTY_ROWS:
                    break
            else:
                empty_rows = 0

            ctrl.scroll(1, SCROLL_POS)
            time.sleep(self.scroll_wait)

        log.info(f'[角色扫描] 完成：已获得 {len(seen)} 个角色，扫描 {row_count} 行，'
                 f'跳过未获得 {unowned} 个；未匹配名字 {unknown if unknown else "无"}')
        return self.round_success('扫描完成')

    # ==================== 滚动与定位 ====================

    def _goto_list_top(self) -> Optional[int]:
        """滚到列表顶部并返回「已拥有」区首行中心 y；失败返回 None。

        scroll 的正负方向实测与文档说明相反（负数才是向上），且不同基座实现
        可能不一致，所以两个方向都试，用「顶部才有的独家频段/已拥有标签」判定。
        """
        for direction in (-SCROLL_STEP, SCROLL_STEP):
            self._scroll_until_stable(direction)
            ok, screen = self.ctx.controller.screenshot()
            if not ok or screen is None:
                continue
            if not self._has_top_marker(screen):
                log.debug('[角色扫描] 该边界没有顶部标签，换方向重试')
                continue
            centers = self._detect_row_centers(screen)
            if not centers:
                continue
            # 顶部有「独家频段」时第一行是 UP 池且滚动时固定不动，必须跳过，
            # 否则每轮都会重复读 UP 角色而漏掉「已拥有」区。
            skip = self._has_up_block(screen) and len(centers) > 1
            row_y = centers[1] if skip else centers[0]
            log.info(f'[角色扫描] 已到列表顶部（方向 {"向上" if direction < 0 else "向下"}），'
                     f'检测到 {len(centers)} 行，{"跳过 UP 行，" if skip else ""}首行中心 y={row_y}')
            return row_y
        return None

    def _has_top_marker(self, screen: MatLike) -> bool:
        """列表顶部才会出现「独家频段 / 已拥有」区块。"""
        img = cv2_utils.crop_image_only(screen, TOP_MARK_BOX)
        try:
            ocr_result = self.ctx.ocr.run_ocr(img)
        except Exception:  # noqa: BLE001
            return False
        if not ocr_result:
            return False
        return any(('独家频段' in t) or ('已拥有' in t) for t in ocr_result)

    def _has_up_block(self, screen: MatLike) -> bool:
        """顶部是否还有「独家频段」UP 池区块。"""
        img = cv2_utils.crop_image_only(screen, UP_MARK_BOX)
        try:
            ocr_result = self.ctx.ocr.run_ocr(img)
        except Exception:  # noqa: BLE001
            return False
        if not ocr_result:
            return False
        return any('独家频段' in t for t in ocr_result)

    def _scroll_until_stable(self, clicks: int) -> None:
        """朝一个方向滚到边界（连续两帧网格一致即认为到底/到顶）。"""
        prev: Optional[MatLike] = None
        for _ in range(SCROLL_MAX_ROUNDS):
            self.ctx.controller.scroll(clicks, SCROLL_POS)
            time.sleep(SCROLL_SETTLE)
            ok, cur = self.ctx.controller.screenshot()
            if not ok or cur is None:
                continue
            if prev is not None and self._grid_similar(prev, cur):
                return
            prev = cur

    @staticmethod
    def _grid_similar(a: MatLike, b: MatLike) -> bool:
        """比较两帧网格区域是否几乎一致（判断滚动是否已到边界）。"""
        try:
            ca = cv2_utils.crop_image_only(a, GRID_BOX)
            cb = cv2_utils.crop_image_only(b, GRID_BOX)
            if ca.shape != cb.shape:
                return False
            diff = np.abs(ca.astype(np.int16) - cb.astype(np.int16)).mean()
            return bool(diff < 2.5)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _detect_row_centers(screen: MatLike) -> list[int]:
        """在网格区域做纵向投影，返回可见卡片行的中心 y（自上而下）。"""
        try:
            region = cv2_utils.crop_image_only(screen, GRID_BOX)
            gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY).astype(np.float32)
            prof = gray.mean(axis=1)

            rows, start = [], None
            for i, v in enumerate(prof):
                if v >= 45:
                    if start is None:
                        start = i
                else:
                    if start is not None:
                        if i - start >= 100:      # 卡片高度远大于标题栏/间隙
                            rows.append((start, i))
                        start = None
            if start is not None and len(prof) - start >= 100:
                rows.append((start, len(prof)))
            return [int((top + bottom) / 2) + GRID_BOX.y1 for top, bottom in rows]
        except Exception:  # noqa: BLE001
            log.warning('[角色扫描] 行位置检测失败', exc_info=True)
            return []

    # ==================== 信息读取 ====================

    def _is_owned(self, screen: MatLike) -> bool:
        """右侧「特训方案」位置有文字即已获得；未获得时该处是 EMPTY。"""
        img = cv2_utils.crop_image_only(screen, OWNED_BOX)
        try:
            ocr_result = self.ctx.ocr.run_ocr(img)
        except Exception as e:  # noqa: BLE001
            log.error(f'[角色扫描] 持有状态 OCR 失败: {e}')
            return False
        if not ocr_result:
            return False
        return any('特训' in text for text in ocr_result)

    def _read_name(self, screen: MatLike) -> Optional[str]:
        """裁剪右上角名字区域并 OCR，返回最长的中文串。"""
        img = cv2_utils.crop_image_only(screen, NAME_BOX)
        try:
            ocr_result = self.ctx.ocr.run_ocr(img)
        except Exception as e:  # noqa: BLE001
            log.error(f'[角色扫描] 名字 OCR 失败: {e}')
            return None

        if not ocr_result:
            return None

        candidates = [
            text for text in ocr_result
            if any('\u4e00' <= ch <= '\u9fff' for ch in text)
        ]
        if not candidates:
            return None
        return max(candidates, key=len).strip()

    def _read_level(self, screen: MatLike, col_idx: int) -> int:
        """读卡片左下角的等级数字；读不到按满级 60 处理。"""
        left = COL_LEFT[col_idx]
        rect = Rect(left + LEVEL_DX[0], self._row_y + LEVEL_DY[0],
                    left + LEVEL_DX[1], self._row_y + LEVEL_DY[1])
        img = cv2_utils.crop_image_only(screen, rect)
        try:
            ocr_result = self.ctx.ocr.run_ocr(img)
        except Exception:  # noqa: BLE001
            return 60
        if not ocr_result:
            return 60

        for text in ocr_result:
            digits = ''.join(ch for ch in text if ch.isdigit() or ch in 'OoQq')
            digits = digits.replace('O', '0').replace('o', '0').replace('Q', '0').replace('q', '0')
            if len(digits) >= 2:
                try:
                    value = int(digits)
                except ValueError:
                    continue
                if 1 <= value <= 80:
                    return value
        return 60

    def _append_agent(self, key: str, level: int) -> None:
        """按 ZOD 角色结构落数据；技能类字段攻略页看不到，给默认值。"""
        self._counter += 1
        level = max(1, min(level, 80))
        promotion = max(0, level // 10 - 1)
        agent_data = {
            'key': key,
            'level': level,
            'core': 0,
            'mindscape': 0,
            'dodge': 1,
            'basic': 1,
            'chain': 1,
            'special': 1,
            'assist': 1,
            'promotion': promotion,
            'potential': 0,
            'equippedDiscs': {},
            'equippedWengine': '',
            'id': f'zzz_agent_{self._counter}',
        }
        if self.ocr_worker is not None:
            self.ocr_worker.scanned_agents.append(agent_data)
        else:
            log.debug(f'[角色扫描] 无 ocr_worker，角色数据仅记录日志: {agent_data}')
