# coding=utf-8
"""插件工作区资产管理：主仓 git 干净承诺。

背景（2026-11-06 用户反馈）：插件把识别管线/画面定义复制进主仓 assets/、
wiki 图标库复制进 assets/wiki_data/，导致用户在 git 克隆里跑一条龙后
`git status` 出现大量不被忽略的脏文件。

框架硬编码从工作目录加载（cv_service.PIPELINE_DIR、screen_info 分离文件
目录），插件资产必须临时注入主仓 assets，无法完全避免。本模块把污染窗口
压到最小：

1. wiki_data（图标库+翻译字典）整体迁到 plugin_data/（主仓 .gitignore
   已覆盖），零注入；
2. 必须注入的文件（screen_info/pipelines yml）复制/合并前登记清单：
   - created = 插件整体新建的文件，退出时删除；
   - merged  = 增量合并进官方已有文件的，先把原件备份到 plugin_data，
     退出时恢复原件；
3. 进程正常退出（atexit）自动恢复+删除，主仓 git 回到干净状态；
4. 强杀进程导致的残留由下一次插件启动 restore_previous() 清理兜底。

注意：注入文件在整个插件运行期必须存在——_ensure_screen_areas 的自愈
reload(from_separated_files=True) 会重读这些文件，所以清理只挂在
atexit，不挂在单次操作结束。
"""

import atexit
import json
import os
import shutil

from one_dragon.utils import os_utils
from one_dragon.utils.log_utils import log


def plugin_wiki_dir() -> str:
    """wiki_data（图标库+翻译字典）的插件私有目录。

    放 plugin_data/ 下：主仓 .gitignore 已覆盖，不产生 git 脏文件。
    """
    return os_utils.get_path_under_work_dir('plugin_data', 'wiki_data')


class InjectedAssets:
    """注入主仓 assets 的文件登记与恢复。"""

    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        base = os_utils.get_path_under_work_dir('plugin_data', plugin_id)
        self._manifest_path = os.path.join(base, 'injected_assets.json')
        self._backup_dir = os.path.join(base, 'backup')
        self._manifest: dict = {'created': [], 'merged': {}}
        self._loaded = False

    # ---------- 清单 ----------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if os.path.exists(self._manifest_path):
                with open(self._manifest_path, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._manifest['created'] = list(data.get('created') or [])
                    self._manifest['merged'] = dict(data.get('merged') or {})
        except Exception as e:
            log.warning(f'[{self.plugin_id}] 注入清单读取失败(按空清单处理): {e}')

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._manifest_path), exist_ok=True)
            with open(self._manifest_path, 'w', encoding='utf-8') as f:
                json.dump(self._manifest, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log.warning(f'[{self.plugin_id}] 注入清单写入失败: {e}')

    # ---------- 登记 ----------

    def register_create(self, target: str) -> None:
        """登记一个插件整体新建的文件（相对工作目录路径）。"""
        self._load()
        if target not in self._manifest['created']:
            self._manifest['created'].append(target)
            self._save()

    def register_merge(self, target: str) -> None:
        """登记一个被增量合并改写的官方已有文件，首次合并时备份原件。"""
        self._load()
        if target in self._manifest['merged']:
            return  # 已有最初备份
        src = os.path.join(os_utils.get_path_under_work_dir(), target)
        if not os.path.exists(src):
            return
        os.makedirs(self._backup_dir, exist_ok=True)
        safe_name = target.replace('\\', '_').replace('/', '_')
        dst = os.path.join(self._backup_dir, safe_name)
        try:
            shutil.copy2(src, dst)
            self._manifest['merged'][target] = dst
            self._save()
        except Exception as e:
            log.warning(f'[{self.plugin_id}] 官方文件备份失败(跳过恢复): {target}: {e}')

    # ---------- 恢复 ----------

    def restore_previous(self) -> None:
        """启动时清理上次运行残留（强杀兜底）。"""
        self._load()
        self._restore_all()

    def register_exit_cleanup(self) -> None:
        """注册进程退出时的清理。atexit 在正常退出时执行；强杀由下次启动兜底。"""
        atexit.register(self._atexit_restore)

    def _atexit_restore(self) -> None:
        try:
            self._load()
            self._restore_all()
        except Exception as e:  # atexit 内不允许异常外抛
            log.warning(f'[{self.plugin_id}] 退出清理异常: {e}')

    def _restore_all(self) -> None:
        work_dir = os_utils.get_path_under_work_dir()
        restored, removed = 0, 0
        for target, backup in list(self._manifest['merged'].items()):
            full = os.path.join(work_dir, target)
            try:
                if os.path.exists(backup):
                    shutil.copy2(backup, full)
                    os.remove(backup)
                    restored += 1
            except Exception as e:
                log.warning(f'[{self.plugin_id}] 官方文件恢复失败: {target}: {e}')
            self._manifest['merged'].pop(target, None)

        for target in list(self._manifest['created']):
            full = os.path.join(work_dir, target)
            try:
                if os.path.exists(full):
                    os.remove(full)
                    removed += 1
            except Exception as e:
                log.warning(f'[{self.plugin_id}] 注入文件删除失败: {target}: {e}')
            self._manifest['created'].remove(target)

        self._prune_dirs()
        self._save()
        if restored or removed:
            log.info(f'[{self.plugin_id}] 已清理注入资产: 恢复官方文件 {restored} 个, 删除注入文件 {removed} 个')

    def _prune_dirs(self) -> None:
        """删除注入留下的空目录（只删 assets 下插件用过的子目录，不碰别的）。"""
        work_dir = os_utils.get_path_under_work_dir()
        for sub in ('assets/image_analysis_pipelines', 'assets/game_data/screen_info'):
            full = os.path.join(work_dir, sub)
            try:
                if os.path.isdir(full) and not os.listdir(full):
                    os.rmdir(full)
            except Exception:
                pass
