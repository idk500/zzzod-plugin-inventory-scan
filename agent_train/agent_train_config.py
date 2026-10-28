from one_dragon.base.operation.application.application_config import ApplicationConfig
from . import agent_train_const


class AgentTrainConfig(ApplicationConfig):
    """代理人自动培养配置。

    设计原则(2026-10-28 与用户对齐):
    - 游戏内"培养建议"消失 != 可拉满: 完成判据自持(目标等阶配置化), 游戏建议只当提示
    - 共享材料(调查员记录/认证章/丁尼/芯片)是全局池: 切换角色前先把当前角色
      培养到位(consume_shared_first), 否则半途切换会让多角色预算互相污染
    - 皇冠(仓鼠笼访问器)是稀缺不可逆资源: 技能 11->12 必需, 默认绝不消耗,
      skill_target_level=12 必须显式 allow_crown=True 才放行(双保险)
    - 白名单制: 永不自动选择下一个角色
    """

    def __init__(self, instance_idx: int, group_id: str):
        ApplicationConfig.__init__(
            self,
            app_id=agent_train_const.APP_ID,
            instance_idx=instance_idx,
            group_id=group_id,
        )

    @property
    def probe_mode(self) -> bool:
        """探测模式: 只读代理人当前练度生成报告, 不消耗任何资源"""
        return self.get('probe_mode', True)

    @probe_mode.setter
    def probe_mode(self, new_value: bool) -> None:
        self.update('probe_mode', new_value)

    @property
    def agent_whitelist(self) -> list[str]:
        """允许培养的代理人名单(游戏内显示名), 空列表=不培养任何角色"""
        return self.get('agent_whitelist', [])

    @agent_whitelist.setter
    def agent_whitelist(self, new_value: list[str]) -> None:
        self.update('agent_whitelist', new_value)

    @property
    def target_level(self) -> int:
        """目标等级(60=满级=第5晋升, 要求绳网等级50)"""
        return self.get('target_level', 60)

    @target_level.setter
    def target_level(self, new_value: int) -> None:
        self.update('target_level', new_value)

    @property
    def skill_target_level(self) -> int:
        """各技能目标等级; 11=不碰皇冠(推荐), 12=满级但需仓鼠笼访问器"""
        return self.get('skill_target_level', 11)

    @skill_target_level.setter
    def skill_target_level(self, new_value: int) -> None:
        self.update('skill_target_level', new_value)

    @property
    def allow_crown(self) -> bool:
        """是否允许消耗皇冠(仓鼠笼访问器)。False 时 skill_target_level 强制按 11 执行"""
        return self.get('allow_crown', False)

    @allow_crown.setter
    def allow_crown(self, new_value: bool) -> None:
        self.update('allow_crown', new_value)

    @property
    def core_skill_target(self) -> int:
        """天赋(核心被动)目标档位 1-6 (A=1 ... F=6), 6=满档"""
        return self.get('core_skill_target', 6)

    @core_skill_target.setter
    def core_skill_target(self, new_value: int) -> None:
        self.update('core_skill_target', new_value)

    @property
    def consume_shared_first(self) -> bool:
        """先把当前角色培养到位(消耗共享材料)再切换下一个, 避免共用材料影响计算"""
        return self.get('consume_shared_first', True)

    @consume_shared_first.setter
    def consume_shared_first(self, new_value: bool) -> None:
        self.update('consume_shared_first', new_value)

    @property
    def denny_reserve(self) -> int:
        """丁尼保留线: 低于该值不启动培养(晋升+技能消耗巨大)"""
        return self.get('denny_reserve', 1000000)

    @denny_reserve.setter
    def denny_reserve(self, new_value: int) -> None:
        self.update('denny_reserve', new_value)

    @property
    def need_interknot_50(self) -> bool:
        """是否校验绳网等级>=50(第5晋升硬性要求)。True=不满足直接跳过该角色"""
        return self.get('need_interknot_50', True)

    @need_interknot_50.setter
    def need_interknot_50(self, new_value: bool) -> None:
        self.update('need_interknot_50', new_value)
