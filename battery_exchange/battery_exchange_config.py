from one_dragon.base.operation.application.application_config import ApplicationConfig
from . import battery_exchange_const


class BatteryExchangeConfig(ApplicationConfig):

    def __init__(self, instance_idx: int, group_id: str):
        ApplicationConfig.__init__(
            self,
            app_id=battery_exchange_const.APP_ID,
            instance_idx=instance_idx,
            group_id=group_id,
        )

    @property
    def probe_mode(self) -> bool:
        """探测模式 只导航+读资源+存截图 绝不点击合成"""
        return self.get('probe_mode', True)

    @probe_mode.setter
    def probe_mode(self, new_value: bool) -> None:
        self.update('probe_mode', new_value)

    @property
    def target_item(self) -> str:
        """兑换目标: 以太电池(恢复60电量, 流程已验证) / 储值电卡(快速刷取用, 入口待实机映射)"""
        return self.get('target_item', '以太电池')

    @target_item.setter
    def target_item(self, new_value: str) -> None:
        self.update('target_item', new_value)

    @property
    def max_count(self) -> int:
        """单轮兑换数量上限"""
        return self.get('max_count', 5)

    @max_count.setter
    def max_count(self, new_value: int) -> None:
        self.update('max_count', new_value)

    @property
    def backup_reserve_min(self) -> int:
        """储蓄电量保底线: 低于该值不兑换(留足日常补体存量)"""
        return self.get('backup_reserve_min', 0)

    @backup_reserve_min.setter
    def backup_reserve_min(self, new_value: int) -> None:
        self.update('backup_reserve_min', new_value)
