from __future__ import annotations

import json
import os
import time
from typing import ClassVar

from one_dragon.base.operation.application import application_const
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import os_utils
from one_dragon.utils.log_utils import log
from zzz_od.application.zzz_application import ZApplication
from zzz_od.context.zzz_context import ZContext
from zzz_od.operation.back_to_normal_world import BackToNormalWorld

from . import agent_train_const
from .agent_train_config import AgentTrainConfig


class AgentTrainApp(ZApplication):
    """代理人自动培养 (v0.1: probe 先行)。

    风险模型(培养消耗不可逆, 是全链路最高风险):
    1. 白名单制 —— 永不自动选择下一个角色, 只处理配置里明确列出的人
    2. 完成判据自持 —— 目标等级/技能级/天赋档全部来自本应用配置,
       游戏内"培养建议"只当提示绝不作信号(该建议随版本变动)
    3. 皇冠双保险 —— skill_target_level>11 且 allow_crown=False 时
       强制按 11 执行并告警(皇冠=仓鼠笼访问器, 稀缺不可逆)
    4. 共享材料预算 —— consume_shared_first=True 时按白名单顺序逐人
       培养到位再切换, 不做并行预算
    5. 资源预检 —— 丁尼低于保留线直接不启动

    v0.1 交付范围: probe 模式(导航到代理人列表, 读练度, 出报告)。
    执行链路(升级/晋升/技能/天赋的界面操作)待 probe 实机映射后开放,
    在此之前执行模式会明确报错退出, 绝不盲操作。
    """

    STATUS_PROBE_DONE: ClassVar[str] = 'probe完成'

    def __init__(self, ctx: ZContext):
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=agent_train_const.APP_ID,
            op_name=agent_train_const.APP_NAME,
            node_max_retry_times=10,
            use_factory_run_record=False,
        )
        self.config: AgentTrainConfig = self.ctx.run_context.get_config(
            app_id=agent_train_const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=application_const.DEFAULT_GROUP_ID,
        )

        self.whitelist: list[str] = list(self.config.agent_whitelist or [])

        # 生效目标(考虑皇冠双保险后的最终口径)
        self.eff_target_level: int = self.config.target_level
        self.eff_skill_level: int = self.config.skill_target_level
        if self.eff_skill_level > 11 and not self.config.allow_crown:
            log.warning('[皇冠保护] skill_target_level=%d 但 allow_crown=False, '
                        '强制按 11 执行(不消耗仓鼠笼访问器)', self.eff_skill_level)
            self.eff_skill_level = 11

        probe_dir = os.path.join(
            os_utils.get_path_under_work_dir('.debug'), 'agent_train_probe')
        os.makedirs(probe_dir, exist_ok=True)
        self.probe_dir: str = probe_dir
        self.probe_result: dict = {}

    # ---------- 辅助 ----------

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

    def _write_report(self, reason: str) -> None:
        report = {
            'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'reason': reason,
            'probe_mode': self.config.probe_mode,
            'whitelist': self.whitelist,
            'effective_targets': {
                'target_level': self.eff_target_level,
                'skill_target_level': self.eff_skill_level,
                'core_skill_target': self.config.core_skill_target,
                'allow_crown': self.config.allow_crown,
                'consume_shared_first': self.config.consume_shared_first,
                'denny_reserve': self.config.denny_reserve,
                'need_interknot_50': self.config.need_interknot_50,
            },
            'probe_result': self.probe_result,
        }
        out = os.path.join(self.probe_dir, 'report.json')
        try:
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log.warning(f'写报告失败: {e}')
        log.info(f'[培养报告] {reason} 白名单 {self.whitelist} -> {out}')

    # ---------- 节点 ----------

    @operation_node(name='开始前返回', is_start_node=True)
    def back_at_first(self) -> OperationRoundResult:
        op = BackToNormalWorld(self.ctx)
        return self.round_by_op_result(op.execute())

    @node_from(from_name='开始前返回')
    @operation_node(name='配置预检')
    def precheck_config(self) -> OperationRoundResult:
        if not self.whitelist:
            return self.round_fail('白名单为空 不培养任何角色(永不自动选人)')
        if self.eff_target_level > 60 or self.eff_target_level < 1:
            return self.round_fail(f'目标等级非法: {self.eff_target_level}')
        if self.eff_skill_level < 1 or self.eff_skill_level > 12:
            return self.round_fail(f'技能目标等级非法: {self.eff_skill_level}')
        if not 1 <= self.config.core_skill_target <= 6:
            return self.round_fail(f'天赋目标档位非法: {self.config.core_skill_target}')
        log.info('[配置预检] 目标: 等级%d 技能%d 天赋%d档 白名单%s '
                 '先消耗共享材料再切换=%s',
                 self.eff_target_level, self.eff_skill_level,
                 self.config.core_skill_target, self.whitelist,
                 self.config.consume_shared_first)
        return self.round_success()

    @node_from(from_name='配置预检')
    @operation_node(name='前往代理人列表')
    def goto_agent_list(self) -> OperationRoundResult:
        return self.round_by_goto_screen(screen_name='代理人-列表')

    @node_from(from_name='前往代理人列表')
    @operation_node(name='probe采集')
    def probe_collect(self) -> OperationRoundResult:
        """读取代理人列表页: 白名单角色在场情况 + 存截图。

        v0.1 只做到这里; 练度明细(等级/技能级/天赋档)需要进入角色详情页,
        待本报告的截图完成 UI 映射后开放。
        """
        import cv2
        screen = self.screenshot()
        if screen is None:
            return self.round_retry('截图失败', wait=1)

        ocr_result = self.ctx.ocr.run_ocr(screen)
        texts = list((ocr_result or {}).keys())

        found: dict[str, bool] = {}
        for name in self.whitelist:
            found[name] = any(name in t for t in texts)
        self.probe_result = {
            'whitelist_on_list': found,
            'ocr_text_count': len(texts),
            'note': '练度明细需角色详情页UI映射, 见同目录截图',
        }
        self._save_probe('agent_list')
        log.info(f'[probe] 白名单在场情况: {found}')
        return self.round_success(self.STATUS_PROBE_DONE)

    @node_from(from_name='probe采集', status=STATUS_PROBE_DONE)
    @operation_node(name='写probe报告')
    def finish_probe(self) -> OperationRoundResult:
        self._write_report('probe完成 不消耗任何资源')
        return self.round_success()

    @node_from(from_name='配置预检', status='跳过probe直接执行')
    @operation_node(name='执行培养')
    def run_training(self) -> OperationRoundResult:
        """执行链路占位: 升级/晋升/技能/天赋界面操作待 probe 实机映射后实现。

        绝不在 UI 未映射时盲操作(培养消耗不可逆)。
        """
        self._write_report('执行链路待实机映射 未做任何操作')
        return self.round_fail(
            '执行链路待实机映射: 请先跑 probe 模式出报告, '
            '用 .debug/agent_train_probe/ 截图完成 UI 映射后再开放')
