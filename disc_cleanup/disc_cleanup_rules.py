# -*- coding: utf-8 -*-
"""驱动盘弃置判定规则 v2 —— 套装效果需求 × 主词条错配矩阵

演进背景（2026-10-28 复盘）:
- v1 使用 40 条 (套装,部位,主词条) 三元组黑名单 + R2 副词条判定，
  存在两类问题: ① 组合未覆盖全部套装(云岿如我/灵魂摇滚等漏网);
  ② R2 对 L0 盘判"副词条全废"抹杀了剩余升级档的翻盘可能。
- v2 核心规律（用户拍板）: 套装效果吃什么属性, 决定哪些主词条是"死词条"。
  例: 双爆输出套装 × 防御主词条 = 废; 其镜像同样成立(辅助套 × 攻击主词条)。

口径说明（可配置覆盖）:
- 未分类套装一律不判废(fail-safe 保留), 并计数提醒补矩阵。
- R2 仅对 L12+(副词条已开满 4 条、仅剩 1 档强化)的盘生效,
  低级盘保留升级翻盘空间。
- 锁定盘不在此层判定(标记动作对锁定盘会被游戏拒绝; 弃置标记本身可逆)。
- 争议口径: 异常套 6 号位 def_/hp_ 按"有效"处理(异常角色生存流),
  如需收紧改 DEAD_MATRIX['anomaly']['6']。
"""
import os
from typing import Optional

from one_dragon.utils import os_utils

# 套装 → 类型分类（按效果需求属性）
DEFAULT_SET_TYPE = {
    # 输出向: 效果吃 攻击/双暴/属性伤害
    'WoodpeckerElectro': 'output', 'Branch&BladeSong': 'output',
    'AstralVoice': 'output', 'HormonePunk': 'output',
    'PufferElectro': 'output', 'ShadowHarmony': 'output',
    'InfernoMetal': 'output', 'MoonlightLullaby': 'output',
    'FangedMetal': 'output',
    # 异常向: 效果吃 异常精通/异常积蓄
    'ChaoticMetal': 'anomaly', 'PolarMetal': 'anomaly',
    'ThunderMetal': 'anomaly', 'FreedomBlues': 'anomaly',
    # 击破向: 效果吃 冲击力/失衡值
    'ShockstarDisco': 'stun',
    # 辅助/防御向: 效果吃 生命/防御/能量
    'SoulRock': 'support', 'YunkuiTales': 'support',
    'WhiteWaterBallad': 'support', 'SwingJazz': 'support',
    "Phaethon'sMelody": 'support',
}

# 各类型在 4/5/6 号位的"死主词条"（1/2/3 号主词条固定, 不参与判定）
DEFAULT_DEAD_MATRIX = {
    'output':  {'4': {'def_', 'hp_', 'anomProf'},
                '5': {'def_', 'hp_'},
                '6': {'def_', 'hp_', 'impact', 'anomMas_'}},
    'anomaly': {'4': {'def_', 'hp_'},
                '5': {'def_', 'hp_'},
                '6': {'impact'}},
    'stun':    {'4': {'atk_', 'anomProf'},
                '5': {'atk_', 'pen_'},
                '6': {'ether_dmg_', 'fire_dmg_', 'ice_dmg_',
                      'electric_dmg_', 'phys_dmg_', 'anomMas_', 'atk_'}},
    'support': {'4': {'atk_', 'anomProf'},
                '5': {'pen_'},
                '6': {'ether_dmg_', 'fire_dmg_', 'ice_dmg_',
                      'electric_dmg_', 'phys_dmg_'}},
}

# R2(副词条潜力)判定: 低价值副词条集合 / 生效门槛
R2_LOW_SUBS = {'hp', 'hp_', 'def', 'def_'}
R2_MIN_LEVEL = 12   # 仅 L12+ 生效(副词条 4 条已齐, 仅剩 1 档强化)
R2_MIN_SUBS = 4     # 副词条须已开满 4 条且全部为低价值


class DiscRules:
    """矩阵规则加载器: 内置默认 + 可选配置文件覆盖"""

    def __init__(self):
        self.set_type = dict(DEFAULT_SET_TYPE)
        self.dead_matrix = {k: {s: set(v) for s, v in row.items()}
                            for k, row in DEFAULT_DEAD_MATRIX.items()}
        self.r2_low_subs = set(R2_LOW_SUBS)
        self.r2_min_level = R2_MIN_LEVEL
        self.r2_min_subs = R2_MIN_SUBS
        self._load_override()

    def _load_override(self):
        """配置目录 disc_cleanup_rules.yml 存在则覆盖内置矩阵
        格式: {set_type: {套装: 类型}, dead_matrix: {类型: {部位: [词条]}},
               r2_min_level: int}  —— 只覆盖出现的键, 便于增量调整"""
        path = os.path.join(os_utils.get_path_under_work_dir('config'),
                            'disc_cleanup_rules.yml')
        if not os.path.exists(path):
            return
        try:
            from one_dragon.utils import yaml_utils
            data = yaml_utils.load(path)
            if not isinstance(data, dict):
                return
            for k, v in (data.get('set_type') or {}).items():
                self.set_type[k] = v
            for t, row in (data.get('dead_matrix') or {}).items():
                for slot, subs in row.items():
                    self.dead_matrix.setdefault(t, {}).setdefault(slot, set()).update(subs)
            if 'r2_min_level' in data:
                self.r2_min_level = int(data['r2_min_level'])
        except Exception as e:  # 配置损坏时保持内置默认
            from one_dragon.utils.log_utils import log
            log.warning(f'disc_cleanup_rules.yml 加载失败, 使用内置矩阵: {e}')

    def is_dead_main(self, set_key: str, slot: str, main_key: str) -> Optional[bool]:
        """主词条是否死词条; 未知套装返回 None(调用方应跳过保留)"""
        t = self.set_type.get(set_key)
        if t is None:
            return None
        return main_key in self.dead_matrix.get(t, {}).get(str(slot), set())

    def is_r2_candidate(self, disc: dict) -> bool:
        """主词条有效但副词条潜力耗尽: L12+ 且 4 条副词条全为低价值"""
        if disc.get('level', 0) < self.r2_min_level:
            return False
        subs = disc.get('substats', [])
        if len(subs) < self.r2_min_subs:
            return False
        return all(s.get('key') in self.r2_low_subs for s in subs)
