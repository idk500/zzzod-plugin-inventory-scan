# -*- coding: utf-8 -*-
"""驱动盘批量标弃置应用（规则层清理执行器）——独立插件应用

设计要点（2026-10-28 与用户对齐）:
- 决议存指纹（套装+槽位+主词条+副词条集合）不存坐标/ID，执行时重新实时扫描逐格
  打开详情面板 OCR 实时指纹，命中决议且现场未装备才打弃置标记，现场不符即跳过。
- 满级（L15）盘永不自动标记（人工过目桶）。
- 探测模式（probe_mode）：只访问格子保存详情截图与 OCR 结果，绝不点击标记。
- 仓库余位 >= capacity_free_min 时整个清理跳过（用户拍板门槛 200）。
- 弃置标记可逆（可手动取消），拆解仍由独立的 drive_disc_dismantle 应用另行执行。

独立化（与扫盘器解耦）:
- 本应用注册为独立 app_id=disc_cleanup，不再挂在仓库扫描器流程尾部；
  触发条件（余位门槛）、探测/标记模式、单轮上限均来自自身配置 DiscCleanupConfig。
- 决议文件固定为 .debug/cleanup_decisions.json（离线规则分析产物，非配置）。
- 复用 inventory_scan 插件的 DriveDiskParser（跨插件导入，同仓库部署）。
"""
import json
import os
import time
from typing import Optional

from cv2.typing import MatLike

from one_dragon.base.geometry.point import Point
from one_dragon.base.geometry.rectangle import Rect
from one_dragon.base.operation.application import application_const
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import cv2_utils, os_utils
from one_dragon.utils.log_utils import log
from zzz_od.application.zzz_application import ZApplication
from zzz_od.context.zzz_context import ZContext
from zzz_od.operation.back_to_normal_world import BackToNormalWorld

# 跨插件导入: 复用仓库扫描器的驱动盘 OCR 解析器 (依赖 inventory_scan 插件)
from inventory_scan.parser.drive_disk_parser import DriveDiskParser

from . import disc_cleanup_const
from .disc_cleanup_config import DiscCleanupConfig
from .disc_cleanup_rules import DiscRules
from .grid_ops import sort_grids, stable_screenshot


class DiscCleanupApp(ZApplication):

    def __init__(self, ctx: ZContext):
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=disc_cleanup_const.APP_ID,
            op_name=disc_cleanup_const.APP_NAME,
            node_max_retry_times=100000,
            use_factory_run_record=False,
        )
        self.config: DiscCleanupConfig = self.ctx.run_context.get_config(
            app_id=disc_cleanup_const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=application_const.DEFAULT_GROUP_ID,
        )

        # v2: 判定规则改为内置矩阵(套装分类×死主词条), decisions.json 仅保留 UI 坐标
        decisions_path = os.path.join(
            os_utils.get_path_under_work_dir('.debug'), 'cleanup_decisions.json')
        self.decisions = {}
        if os.path.exists(decisions_path):
            with open(decisions_path, encoding='utf-8') as f:
                self.decisions = json.load(f)
        ui = self.decisions.get('ui', {})
        # 弃置按钮坐标内置默认(2026-10-28 视觉确认), decisions.json 可覆盖
        self.trash_button_rect = ui.get('trash_button_rect', [1420, 808, 1475, 858])
        self.confirm_button_rect = ui.get('confirm_button_rect')

        self.rules = DiscRules()

        # 运行参数全部来自本应用配置
        self.capacity_free_min: int = self.config.capacity_free_min
        # 无弃置按钮坐标时强制探测模式(首次运行安全兜底)
        self.probe_mode: bool = self.config.probe_mode or self.trash_button_rect is None
        self.mark_limit: int = self.config.mark_limit

        self.parser = DriveDiskParser()

        self.current_row_idx: int = 0
        self.current_col_idx: int = 0
        self.visited: int = 0
        self.marked: list[dict] = []
        self.seen_fps: set[tuple] = set()  # 本轮已处理过的完整指纹(翻页重叠去重, 防二次标记被取消)
        # 翻页安全阀(与扫描器同款): 整页零新增×2 判定到底 + 滚动轮数上限
        self._page_new_fps: int = 0
        self._all_dup_pages: int = 0
        self.scroll_rounds: int = 0
        self.skipped_eq: int = 0      # 已装备跳过
        self.skipped_lv15: int = 0    # 满级跳过
        self.skipped_seen: int = 0    # 本轮重复指纹跳过
        self.skipped_unknown_set: int = 0  # 未知套装保留(矩阵未覆盖)
        self.parse_fail: int = 0
        self.probe_saved: int = 0
        self._skip_all: bool = False
        self._finish_reason: Optional[str] = None
        self.probe_dir = os.path.join(
            os_utils.get_path_under_work_dir('.debug'), 'cleanup_probe')
        # 跨会话状态: 已标记指纹集合（弃置点击疑似开关式, 防止重跑时取消标记）
        self.state_path = os.path.join(
            os_utils.get_path_under_work_dir('.debug'), 'cleanup_state.json')
        self.marked_fps: set[tuple] = set()
        self.skipped_marked: int = 0
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding='utf-8') as f:
                    self.marked_fps = {tuple(x) for x in json.load(f)}
                log.info(f'载入历史标记状态 {len(self.marked_fps)} 条')
            except Exception as e:
                log.error(f'载入标记状态失败: {e}')
        self._ensure_plugin_assets()

    def _ensure_plugin_assets(self) -> None:
        """插件自带识别管线合并到工作目录 供框架按工作目录路径加载"""
        import shutil
        from pathlib import Path

        plugin_assets = Path(__file__).parent / "assets"
        if not plugin_assets.exists():
            return
        dst_pipelines = Path(os_utils.get_path_under_work_dir("assets", "image_analysis_pipelines"))
        dst_pipelines.mkdir(parents=True, exist_ok=True)
        for yml in (plugin_assets / "image_analysis_pipelines").iterdir():
            target = dst_pipelines / yml.name
            if not target.exists():
                shutil.copy2(yml, target)

    # ---------- 节点 ----------

    @operation_node(name='开始前返回', is_start_node=True)
    def back_at_first(self) -> OperationRoundResult:
        op = BackToNormalWorld(self.ctx)
        return self.round_by_op_result(op.execute())

    @node_from(from_name='开始前返回')
    @operation_node(name='前往驱动盘仓库')
    def goto_warehouse(self) -> OperationRoundResult:
        return self.round_by_goto_screen(screen_name='仓库-驱动仓库')

    @node_from(from_name='前往驱动盘仓库')
    @operation_node(name='检查仓库余位')
    def check_capacity(self) -> OperationRoundResult:
        """OCR 仓库标题「驱动仓库 [ x / y ]」取余位，余位充足则不清理（用户门槛 200）

        注意：页面顶部条同时有右上角邦布等计数器（形如 243/240），
        必须锚定含「仓库」字样的 OCR 文本，否则会读错对象。
        """
        import re
        screen = self.screenshot()
        results = self.ctx.ocr.crop_and_run_ocr(screen, Rect(0, 0, screen.shape[1], 200))
        candidates = []  # (is_warehouse, used, cap)
        for text in results.keys():
            m = re.search(r'(\d+)\s*/\s*(\d+)', text)
            if m:
                candidates.append(('仓库' in text, int(m.group(1)), int(m.group(2))))
        if not candidates:
            log.warning('未读到仓库容量 x/y，按需继续（probe 模式下无副作用）')
            return self.round_success('未读到容量 继续执行')
        warehouse = [c for c in candidates if c[0]]
        _, used, cap = warehouse[0] if warehouse else candidates[0]
        if not warehouse:
            log.warning(f'容量候选无「仓库」锚定 可能读错计数器: {candidates}')
        free = cap - used
        log.info(f'仓库容量: {used}/{cap} 余位 {free} (门槛 {self.capacity_free_min})')
        if free >= self.capacity_free_min:
            self._skip_all = True
            return self.round_success(f'余位充足({free}) 跳过清理')
        return self.round_success(f'余位不足({free}) 继续清理')

    @node_from(from_name='检查仓库余位')
    @operation_node(name='初始化网格')
    def initialize_align(self) -> OperationRoundResult:
        if self._skip_all:
            return self._finish('余位充足 跳过清理')
        screen = self.last_screenshot
        self.ctx.controller.mouse_move(Point(0, 0))
        time.sleep(0.05)
        screen = self.screenshot()
        ctx = self.ctx.cv_service.run_pipeline('驱动盘方格', screen)
        if ctx.error_str is not None or not ctx.contours:
            return self.round_retry(f'网格检测失败:{ctx.error_str}', wait=1)
        all_grids = [Point(x1 + (x2 - x1) / 2, y1 + (y2 - y1) / 2)
                     for x1, y1, x2, y2 in ctx.get_absolute_rects()]
        self.grid_rows = sort_grids(all_grids)
        if not self.grid_rows or not self.grid_rows[0]:
            return self.round_retry('网格为空', wait=1)
        self.ctx.controller.click(self.grid_rows[0][0])
        os.makedirs(self.probe_dir, exist_ok=True)
        return self.round_success(f'检测到{len(self.grid_rows)}行 已对齐(1,1)')

    @node_from(from_name='初始化网格')
    @node_from(from_name='遍历处理', status='换行')
    @node_from(from_name='遍历处理', status='滚动')
    @operation_node(name='检测新页网格')
    def detect_grid(self) -> OperationRoundResult:
        self.ctx.controller.mouse_move(Point(0, 0))
        time.sleep(0.05)
        screen = self.screenshot()
        ctx = self.ctx.cv_service.run_pipeline('驱动盘方格', screen)
        if ctx.error_str is not None or not ctx.contours:
            return self.round_retry(f'网格检测失败:{ctx.error_str}', wait=1)
        all_grids = [Point(x1 + (x2 - x1) / 2, y1 + (y2 - y1) / 2)
                     for x1, y1, x2, y2 in ctx.get_absolute_rects()]
        self.grid_rows = sort_grids(all_grids)
        if not self.grid_rows:
            return self.round_retry('网格为空', wait=1)
        return self.round_success(f'检测到{len(self.grid_rows)}行')

    @node_from(from_name='检测新页网格')
    @node_from(from_name='初始化网格')
    @operation_node(name='遍历处理')
    def visit_cells(self) -> OperationRoundResult:
        """逐格处理当前行（同步 OCR + 指纹匹配 + 探测/标记）"""
        if self._skip_all:
            # 余位充足路径：initialize_align 提前结束但默认路由仍会流入本节点
            return self._finish('余位充足 跳过清理')
        if not hasattr(self, 'grid_rows') or self.current_row_idx >= len(self.grid_rows):
            return self._finish('遍历完成')
        current_row = self.grid_rows[self.current_row_idx]
        if self.current_col_idx >= len(current_row):
            return self._finish('遍历完成')

        while self.current_col_idx < len(current_row):
            screen = stable_screenshot(lambda: self.screenshot())
            self._process_cell(screen)
            if self._finish_reason is not None:
                return self._finish(self._finish_reason)

            next_col = self.current_col_idx + 1
            if next_col >= len(current_row):
                break
            self.ctx.controller.click(current_row[next_col])
            self.current_col_idx = next_col

        next_row = self.current_row_idx + 1
        if next_row < len(self.grid_rows) and self.grid_rows[next_row]:
            self.ctx.controller.click(self.grid_rows[next_row][0])
            self.current_row_idx = next_row
            self.current_col_idx = 0
            return self.round_success('换行')

        # 翻页安全阀(与扫描器同款): 本页零新增说明列表没有前进, 连续2页判到底
        if self._page_new_fps == 0:
            self._all_dup_pages += 1
        else:
            self._all_dup_pages = 0
        self._page_new_fps = 0
        if self._all_dup_pages >= 2:
            return self._finish('已到列表底部')
        self.scroll_rounds += 1
        if self.scroll_rounds > 400:
            log.warning(f'滚动轮数达到上限 {self.scroll_rounds} 强制结束')
            return self._finish('滚动轮数上限')

        # 翻页：滚轮 + 回到本页第一行第一格（与扫描器一致），页头重叠由 seen_fps 去重
        scroll_pos = self.grid_rows[-1][0] if self.grid_rows else Point(960, 540)
        self.ctx.controller.scroll(3, scroll_pos)
        time.sleep(0.6)
        first_pos = self.grid_rows[0][0] if self.grid_rows else Point(960, 540)
        self.ctx.controller.click(first_pos)
        self.current_row_idx = 0
        self.current_col_idx = 0
        return self.round_success('滚动', wait=0.6)

    # ---------- 单格处理 ----------

    def _process_cell(self, screen: MatLike):
        self.visited += 1
        area = self.ctx.screen_loader.get_area('仓库-驱动仓库', '驱动盘属性')
        part = cv2_utils.crop_image_only(screen, area.rect)

        ocr_result = self.ctx.ocr.run_ocr(part)
        if not ocr_result:
            self.parse_fail += 1
            return
        ocr_items = []
        for text, match_list in ocr_result.items():
            for match in match_list:
                ocr_items.append({
                    'text': text,
                    'confidence': getattr(match, 'confidence', 1.0),
                    'position': (match.x, match.y, match.x + match.w, match.y + match.h),
                })
        disc = self.parser.parse_ocr_result(ocr_items, part)
        if not disc or not disc.get('setKey') or disc['setKey'] == 'Unknown':
            self.parse_fail += 1
            return
        sub_fp = tuple(sorted(s['key'] for s in disc['substats']))
        full_fp = (disc['setKey'], disc['slotKey'], disc['mainStatKey'],
                   disc['level'], sub_fp)
        if full_fp in self.seen_fps:
            # 翻页重叠/同指纹重复: 同指纹盘可互换, 本轮已处理过不再二次点击(防取消标记)
            self.skipped_seen += 1
            return
        # 去重先于槽位过滤: 任何新盘都计入页新增(供到底判定), 槽位1/2/3同样去重省OCR
        self.seen_fps.add(full_fp)
        self._page_new_fps += 1

        if disc.get('slotKey') not in '456':
            return  # 矩阵只涉及 4/5/6 号位, 1/2/3 号位主词条固定无需处理

        dead = self.rules.is_dead_main(
            disc['setKey'], disc['slotKey'], disc['mainStatKey'])
        if dead is None:
            # 未知套装: fail-safe 保留, 提醒补矩阵
            self.skipped_unknown_set += 1
            return

        # R1: 主词条死词条 且 未满级
        hit_r1 = dead and disc['level'] < 15
        # R2: 主词条有效但副词条潜力耗尽(仅 L12+ 且 4 条全低价值)
        hit_r2 = (not dead) and self.rules.is_r2_candidate(disc)

        if full_fp in self.marked_fps and (hit_r1 or hit_r2):
            self.skipped_marked += 1  # 历史会话已标记过
            return

        if disc.get('location'):
            if hit_r1 or hit_r2:
                self.skipped_eq += 1
            return
        if dead and disc['level'] >= 15:
            self.skipped_lv15 += 1
            return

        if not (hit_r1 or hit_r2):
            return

        if self.probe_mode:
            self._save_probe(screen, part, disc, hit_r1)
            return

        self._mark_disc(screen, disc, hit_r1)

    def _save_probe(self, screen: MatLike, part: MatLike, disc: dict, hit_r1: bool):
        if self.probe_saved >= 12:
            if self._finish_reason is None:
                self._finish_reason = f'探测样本已够({self.probe_saved})'
            return
        self.probe_saved += 1
        import cv2
        tag = f'{"R1" if hit_r1 else "R2"}_{disc["setKey"]}_{disc["slotKey"]}_{disc["mainStatKey"]}_L{disc["level"]}'
        tag = ''.join(c if c.isalnum() or c in '_&' else '-' for c in tag)[:80]
        cv2.imwrite(os.path.join(self.probe_dir, f'{self.probe_saved:02d}_full.png'),
                    cv2.cvtColor(screen, cv2.COLOR_RGB2BGR))
        cv2.imwrite(os.path.join(self.probe_dir, f'{self.probe_saved:02d}_detail.png'),
                    cv2.cvtColor(part, cv2.COLOR_RGB2BGR))
        with open(os.path.join(self.probe_dir, f'{self.probe_saved:02d}_ocr.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(disc, f, ensure_ascii=False, indent=1)
        log.info(f'[probe] {self.probe_saved}: {disc["setKey"]} {disc["slotKey"]}号 '
                 f'{disc["mainStatKey"]} L{disc["level"]}')

    def _mark_disc(self, screen: MatLike, disc: dict, hit_r1: bool):
        if len(self.marked) >= self.mark_limit:
            self._finish_reason = f'达到单轮标记上限 {self.mark_limit}'
            return
        rect = self.trash_button_rect
        center = Point((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        self.ctx.controller.click(center)
        time.sleep(1.0)
        # 兼容确认弹窗：优先用配置坐标，否则 OCR 中央区域找「确认」
        if self.confirm_button_rect:
            c = self.confirm_button_rect
            self.ctx.controller.click(Point((c[0] + c[2]) / 2, (c[1] + c[3]) / 2))
            time.sleep(0.8)
        else:
            self._click_ocr_confirm()
        self.marked.append({
            'set': disc['setKey'], 'slot': disc['slotKey'],
            'main': disc['mainStatKey'], 'level': disc['level'],
            'subs': [s['key'] for s in disc['substats']],
            'rule': 'R1' if hit_r1 else 'R2',
        })
        self.marked_fps.add((disc['setKey'], disc['slotKey'], disc['mainStatKey'],
                             disc['level'], tuple(sorted(s['key'] for s in disc['substats']))))
        try:  # 立即落盘标记状态, 中断也不丢
            with open(self.state_path, 'w', encoding='utf-8') as f:
                json.dump([list(x) for x in self.marked_fps], f, ensure_ascii=False)
        except Exception as e:
            log.error(f'保存标记状态失败: {e}')
        log.info(f'[标记 {len(self.marked)}/{self.mark_limit}] '
                 f'{disc["setKey"]} {disc["slotKey"]}号 {disc["mainStatKey"]} L{disc["level"]}')

    def _click_ocr_confirm(self):
        import re
        screen = self.screenshot()
        results = self.ctx.ocr.crop_and_run_ocr(screen, Rect(660, 420, 600, 380))
        for text, mrl in results.items():
            if re.search(r'确\s*认', text):
                pt = mrl.max.center
                self.ctx.controller.click(pt)
                time.sleep(0.8)
                return

    # ---------- 收尾 ----------

    def _finish(self, reason: str) -> OperationRoundResult:
        summary = {
            'visited': self.visited, 'marked': self.marked,
            'marked_count': len(self.marked),
            'skipped_equipped': self.skipped_eq, 'skipped_lv15': self.skipped_lv15,
            'skipped_seen': self.skipped_seen, 'skipped_marked': self.skipped_marked,
            'skipped_unknown_set': self.skipped_unknown_set,
            'parse_fail': self.parse_fail, 'probe_saved': self.probe_saved,
            'probe_mode': self.probe_mode,
        }
        out = os.path.join(os_utils.get_path_under_work_dir('.debug'),
                           'cleanup_result.json')
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        log.info(f'[清理结束] {reason} 访问{self.visited} 标记{len(self.marked)} '
                 f'装备中跳过{self.skipped_eq} 满级跳过{self.skipped_lv15} '
                 f'未知套装保留{self.skipped_unknown_set} '
                 f'解析失败{self.parse_fail}')
        return self.round_success(reason)


def __debug():
    ctx = ZContext()
    ctx.init()
    ctx.run_context.start_running()
    app = DiscCleanupApp(ctx)
    app.execute()


if __name__ == '__main__':
    __debug()
