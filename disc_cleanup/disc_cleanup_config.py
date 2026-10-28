from one_dragon.base.operation.application.application_config import ApplicationConfig
from . import disc_cleanup_const


class DiscCleanupConfig(ApplicationConfig):

    def __init__(self, instance_idx: int, group_id: str):
        ApplicationConfig.__init__(
            self,
            app_id=disc_cleanup_const.APP_ID,
            instance_idx=instance_idx,
            group_id=group_id,
        )

    @property
    def probe_mode(self) -> bool:
        """探测模式 只访问格子保存样本 绝不点击弃置"""
        return self.get('probe_mode', True)

    @probe_mode.setter
    def probe_mode(self, new_value: bool) -> None:
        self.update('probe_mode', new_value)

    @property
    def mark_limit(self) -> int:
        """单轮标记数量上限"""
        return self.get('mark_limit', 20)

    @mark_limit.setter
    def mark_limit(self, new_value: int) -> None:
        self.update('mark_limit', new_value)

    @property
    def capacity_free_min(self) -> int:
        """仓库余位低于该值才执行清理"""
        return self.get('capacity_free_min', 200)

    @capacity_free_min.setter
    def capacity_free_min(self, new_value: int) -> None:
        self.update('capacity_free_min', new_value)
