from __future__ import annotations

from typing import TYPE_CHECKING

from one_dragon.base.operation.application.application_config import ApplicationConfig
from one_dragon.base.operation.application.application_factory import ApplicationFactory
from one_dragon.base.operation.application_base import Application
from . import agent_train_const
from .agent_train_app import AgentTrainApp
from .agent_train_config import AgentTrainConfig

if TYPE_CHECKING:
    from zzz_od.context.zzz_context import ZContext


class AgentTrainAppFactory(ApplicationFactory):

    def __init__(self, ctx: ZContext):
        ApplicationFactory.__init__(self, agent_train_const)
        self.ctx: ZContext = ctx

    def create_application(self, instance_idx: int, group_id: str) -> Application:
        return AgentTrainApp(self.ctx)

    def create_config(
        self, instance_idx: int, group_id: str
    ) -> ApplicationConfig:
        return AgentTrainConfig(
            instance_idx=instance_idx,
            group_id=group_id,
        )
