from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.operation.application.application_config import ApplicationConfig
from one_dragon.base.operation.application.application_factory import ApplicationFactory
from one_dragon.base.operation.application_base import Application
from . import disc_cleanup_const
from .disc_cleanup_app import DiscCleanupApp
from .disc_cleanup_config import DiscCleanupConfig

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


class DiscCleanupAppFactory(ApplicationFactory):

    def __init__(self, ctx: ZContext):
        ApplicationFactory.__init__(self, disc_cleanup_const)
        self.ctx: ZContext = ctx

    def create_application(self, instance_idx: int, group_id: str) -> Application:
        return DiscCleanupApp(self.ctx)

    def create_config(
        self, instance_idx: int, group_id: str
    ) -> ApplicationConfig:
        return DiscCleanupConfig(
            instance_idx=instance_idx,
            group_id=group_id,
        )
