from __future__ import annotations

import json
import os
import time
from typing import ClassVar

from one_dragon.base.operation.application import application_const
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import os_utils, str_utils
from one_dragon.utils.i18_utils import gt
from one_dragon.utils.log_utils import log
from zzz_od.context.zzz_context import ZContext
from zzz_od.application.zzz_application import ZApplication
from zzz_od.operation.back_to_normal_world import BackToNormalWorld

from . import battery_exchange_const
from .battery_exchange_config import BatteryExchangeConfig


class BatteryExchangeApp(ZApplication):
    """自动兑换电卡: 把多余储蓄电量兑换为可存储道具。

    目标物品二选一:
    - 以太电池: 快捷手册资源栏点击电池图标 -> [获取]合成 -> 道具处理,
      该链路是框架 charge_plan 已验证流程, 直接复用。
    - 储值电卡: 快速刷取材料副本用, 合成入口未实机映射, 先 OCR 找,
      找不到时 probe 存图并明确报错(绝不盲点)。

    安全设计:
    - probe 模式默认开启: 只导航 + 读资源 + 存截图, 不做任何兑换
    - 合成素材不足 -> 立即收尾(游戏侧硬校验兜底)
    - 储蓄电量保底线: 低于保底不兑换
    - 单轮张数上限: 防失控循环
    """

    STATUS_CONTINUE: ClassVar[str] = '继续兑换'

    def __init__(self, ctx: ZContext):
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=battery_exchange_const.APP_ID,
            op_name=battery_exchange_const.APP_NAME,
            node_max_retry_times=15,
            use_factory_run_record=False,
        )
        self.config: BatteryExchangeConfig = self.ctx.run_context.get_config(
            app_id=battery_exchange_const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=application_const.DEFAULT_GROUP_ID,
        )

        self.probe_mode: bool = self.config.probe_mode
        self.target_item: str = self.config.target_item
        self.max_count: int = self.config.max_count
        self.backup_reserve_min: int = self.config.backup_reserve_min

        # 资源读数(「电量检测」流水线)
        self.battery_charge: int = 0
        self.backup_charge: int = 0
        self.ether_battery_cnt: int = 0

        self.exchanged: int = 0

        probe_dir = os.path.join(
            os_utils.get_path_under_work_dir('.debug'), 'battery_probe')
        os.makedirs(probe_dir, exist_ok=True)
        self.probe_dir: str = probe_dir

    # ---------- 辅助 ----------

    @staticmethod
    def _parse_charge_ocr(ocr_result: dict) -> tuple[int, int, int]:
        """从「电量检测」流水线 OCR 结果解析 (电量, 储蓄电量, 以太电池)。

        与 charge_plan_app 同源: 非/数字为分界取左段数字, 检测框中心归入
        最近字段锚点, 全部识别失败返回 (0, 0, 0) 不抛异常。
        """
        field_centers = (173, 348, 485)
        items: list[tuple[float, int]] = []
        for text, match_list in (ocr_result or {}).items():
            if match_list is None:
                continue
            for match in match_list:
                value = str_utils.get_positive_digits(text.split('/')[0], None)
                if value is None:
                    continue
                items.append((match.x + match.w / 2, value))

        slot_values: list[tuple[float, int] | None] = [None, None, None]
        for x_center, value in items:
            field_idx = min(range(3), key=lambda i: abs(x_center - field_centers[i]))
            current = slot_values[field_idx]
            if current is None or abs(x_center - field_centers[field_idx]) < abs(current[0] - field_centers[field_idx]):
                slot_values[field_idx] = (x_center, value)

        return tuple(
            slot_values[i][1] if slot_values[i] is not None else 0
            for i in range(3)
        )

    def _save_probe(self, tag: str) -> None:
        try:
            import cv2
            screen = self.last_screenshot
            if screen is None:
                return
            ts = time.strftime('%H%M%S')
            cv2.imwrite(os.path.join(self.probe_dir, f'{ts}_{tag}.png'), screen)
        except Exception as e:
            log.warning(f'probe 存图失败: {e}')

    def _write_summary(self, reason: str) -> None:
        summary = {
            'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'reason': reason,
            'probe_mode': self.probe_mode,
            'target_item': self.target_item,
            'resources': {
                'battery_charge': self.battery_charge,
                'backup_charge': self.backup_charge,
                'ether_battery': self.ether_battery_cnt,
            },
            'exchanged': self.exchanged,
            'max_count': self.max_count,
            'backup_reserve_min': self.backup_reserve_min,
        }
        out = os.path.join(os_utils.get_path_under_work_dir('.debug'),
                           'battery_exchange_result.json')
        try:
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(summary, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log.warning(f'写汇总失败: {e}')
        log.info(f'[兑换结束] {reason} 本轮兑换 {self.exchanged} 张 {self.target_item}')

    # ---------- 节点 ----------

    @operation_node(name='开始前返回', is_start_node=True)
    def back_at_first(self) -> OperationRoundResult:
        op = BackToNormalWorld(self.ctx)
        return self.round_by_op_result(op.execute())

    @node_from(from_name='开始前返回')
    @operation_node(name='打开快捷手册')
    def goto_compendium(self) -> OperationRoundResult:
        return self.round_by_goto_screen(screen_name='快捷手册-训练')

    @node_from(from_name='打开快捷手册')
    @operation_node(name='识别资源')
    def read_resources(self) -> OperationRoundResult:
        """读快捷手册资源栏: 电量 / 储蓄电量 / 以太电池"""
        pipeline_ctx = self.ctx.cv_service.run_pipeline('电量检测', self.last_screenshot)
        self.battery_charge, self.backup_charge, self.ether_battery_cnt = \
            self._parse_charge_ocr(pipeline_ctx.ocr_result)
        log.info('剩余电量 %s 储蓄电量 %s 以太电池 %s',
                 self.battery_charge, self.backup_charge, self.ether_battery_cnt)

        if self.probe_mode:
            self._save_probe('compendium')
            return self._finish('probe 模式 只读资源不兑换')

        if self.backup_charge <= self.backup_reserve_min:
            return self._finish(
                f'储蓄电量 {self.backup_charge} 低于保底 {self.backup_reserve_min} 不兑换')
        return self.round_success()

    @node_from(from_name='识别资源')
    @operation_node(name='点击兑换入口')
    def click_item_entry(self) -> OperationRoundResult:
        if self.target_item == '以太电池':
            return self.round_by_find_and_click_area(
                self.last_screenshot, '快捷手册', '以太电池',
                success_wait=1, retry_wait=0.5,
            )
        # 储值电卡: 入口未实机映射, 全屏 OCR 查找, 找不到就 probe 存图报错
        result = self.round_by_ocr_and_click(
            self.last_screenshot, gt('储值电卡', 'game'),
            success_wait=1, retry_wait=1,
        )
        if not result.is_success:
            self._save_probe('no_stored_card_entry')
            return self.round_fail(
                '未在当前画面找到「储值电卡」入口 已存截图待实机映射')
        return result

    @node_from(from_name='点击兑换入口')
    @operation_node(name='点击合成入口')
    def click_synthesize_entry(self) -> OperationRoundResult:
        return self.round_by_ocr_and_click(
            self.last_screenshot, '[获取]合成',
            success_wait=1, retry_wait=0.5,
        )

    @node_from(from_name='点击合成入口')
    @operation_node(name='等待道具处理', timeout_seconds=20)
    def wait_item_process(self) -> OperationRoundResult:
        current_screen = self.check_and_update_current_screen(
            self.last_screenshot, ['道具处理'])
        if not current_screen:
            return self.round_retry('等待道具处理', wait=0.5)
        return self.round_success()

    @node_from(from_name='等待道具处理')
    @node_from(from_name='兑换一轮完成', status=STATUS_CONTINUE)
    @operation_node(name='检查合成素材')
    def check_material(self) -> OperationRoundResult:
        """详情标题必须是目标物品(防合错), 素材不足立即收尾"""
        area = self.ctx.screen_loader.get_area('道具处理', '详情标题')
        target_text = gt(self.target_item, 'game')
        ocr_result = self.round_by_ocr(self.last_screenshot, target_text, area=area)
        if not ocr_result.is_success:
            self._save_probe('wrong_item')
            return self.round_fail(f'当前可合成项目不是{self.target_item}')

        result = self.round_by_find_area(self.last_screenshot, '道具处理', '合成素材不足')
        if result.is_success:
            return self._finish('合成素材不足 提前收尾')

        return self.round_success()

    @node_from(from_name='检查合成素材')
    @operation_node(name='点击合成')
    def click_synthesize(self) -> OperationRoundResult:
        return self.round_by_find_and_click_area(
            self.last_screenshot, '道具处理', '按钮-合成',
            success_wait=1, retry_wait=0.5,
        )

    @node_from(from_name='点击合成')
    @operation_node(name='确认合成')
    def confirm_synthesize(self) -> OperationRoundResult:
        return self.round_by_find_and_click_area(
            self.last_screenshot, '道具处理-合成确认', '按钮-确认',
            success_wait=1, retry_wait=0.5,
        )

    @node_from(from_name='确认合成')
    @operation_node(name='确认获得')
    def confirm_obtained(self) -> OperationRoundResult:
        result = self.round_by_find_area(self.last_screenshot, '道具处理-获得', '标题-获得')
        if result.is_success:
            return self.round_by_find_and_click_area(
                self.last_screenshot, '道具处理-获得', '按钮-确认',
                success_wait=0.8, retry_wait=0.5,
            )

        result = self.round_by_find_area(self.last_screenshot, '道具处理', '标题-道具处理')
        if result.is_success:
            return self.round_success()

        return self.round_retry('等待获得弹窗', wait=0.5)

    @node_from(from_name='确认获得')
    @node_from(from_name='确认获得', success=False)
    @operation_node(name='兑换一轮完成')
    def finish_one_exchange(self) -> OperationRoundResult:
        self.exchanged += 1
        log.info(f'已兑换 {self.exchanged}/{self.max_count} 张 {self.target_item}')
        if self.exchanged < self.max_count:
            return self.round_success(self.STATUS_CONTINUE, wait=0.5)
        return self._finish(f'达到单轮上限 {self.max_count}')

    def _finish(self, reason: str) -> OperationRoundResult:
        """统一收尾: 写汇总后正常结束应用"""
        self._write_summary(reason)
        return self.round_success(reason)
