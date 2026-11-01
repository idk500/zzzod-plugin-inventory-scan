"""基座兼容层：子应用没有独立工厂注册，不能走工厂运行记录。

背景（issue #1/#2，2026-11-01）：
- #2886 后基类 Application.__init__ 新增 use_factory_run_record 参数；
- zode 基座的 ZApplication 透传了该形参 → 传 False 即可；
- 官方基座的 ZApplication **未**透传该形参（主仓 #2952 已确认不暴露给 ZApp），
  探测不到时若什么都不传，Application 默认 use_factory_run_record=True，
  会去工厂查子应用运行记录 → 抛「应用未注册 <app_id>」。
  此时改传显式空记录对象兜底：Application 对非 None 记录不再查工厂，
  handle_init / _update_record_after_stop 对记录的判空调用全部空转，不落盘。
"""

import inspect


class NoRunRecord:
    """空运行记录：满足 Application 判空调用，不落盘。"""

    def check_and_update_status(self) -> None:
        pass

    def update_status(self, new_status, only_status: bool = False) -> None:
        pass


def factory_record_kwargs() -> dict:
    """按本机基座签名返回子应用构造所需的运行记录参数。"""
    from zzz_od.application.zzz_application import ZApplication
    params = inspect.signature(ZApplication.__init__).parameters
    if 'use_factory_run_record' in params:
        return {'use_factory_run_record': False}
    if 'run_record' in params:
        return {'run_record': NoRunRecord()}
    return {}
