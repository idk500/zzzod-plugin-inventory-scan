import os
import shutil

from one_dragon.base.operation.application import application_const
from one_dragon.base.operation.operation_edge import node_from
from one_dragon.base.operation.operation_node import operation_node
from one_dragon.base.operation.operation_round_result import OperationRoundResult
from one_dragon.utils import os_utils
from one_dragon.utils.log_utils import log
from pathlib import Path
from . import inventory_scan_const
from .agent.agent_scan_app import AgentScanApp
from .drive_disk.drive_disk_scan_app import (
    DriveDiskScanApp,
)
from .inventory_scan_config import InventoryScanConfig
from .ocr_worker import OcrWorker
from .repo_total import ocr_repo_total
from .screenshot_cache import ScreenshotCache
from .warehouse_nav import click_warehouse_tab, goto_drive_warehouse, read_top_title
from .wengine.wengine_scan_app import WengineScanApp
from zzz_od.application.zzz_application import ZApplication
from zzz_od.context.zzz_context import ZContext
from zzz_od.operation.back_to_normal_world import BackToNormalWorld


class InventoryScanApp(ZApplication):

    def __init__(self, ctx: ZContext):
        ZApplication.__init__(
            self,
            ctx=ctx,
            app_id=inventory_scan_const.APP_ID,
            op_name=inventory_scan_const.APP_NAME,
        )
        self.config: InventoryScanConfig = self.ctx.run_context.get_config(
            app_id=inventory_scan_const.APP_ID,
            instance_idx=self.ctx.current_instance_idx,
            group_id=application_const.DEFAULT_GROUP_ID,
        )
        self._scan_drive_disk: bool = self.config.scan_drive_disk
        self._scan_wengine: bool = self.config.scan_wengine
        self._scan_agent: bool = self.config.scan_agent
        self._ensure_plugin_assets()

        self.screenshots_dir = os_utils.get_path_under_work_dir('.debug', 'inventory_screenshots')
        self.screenshot_cache = ScreenshotCache(save_dir=self.screenshots_dir, debug_mode=False)

        self.ocr_worker = OcrWorker(ctx)
        # 音擎导航的逃逸只做一次标记
        self._wengine_escaped: bool = False
        # 扫描前 OCR 到的游戏内驱动盘总数(用于导出审计) None 表示没读到
        self._disc_game_total: "int | None" = None

        self.drive_disk_scanner = DriveDiskScanApp(ctx, screenshot_cache=self.screenshot_cache, ocr_worker=self.ocr_worker)
        self.wengine_scanner = WengineScanApp(ctx, screenshot_cache=self.screenshot_cache, ocr_worker=self.ocr_worker)
        self.agent_scanner = AgentScanApp(ctx, screenshot_cache=self.screenshot_cache, ocr_worker=self.ocr_worker)

    @operation_node(name='开始前返回', is_start_node=True)
    def back_at_first(self) -> OperationRoundResult:
        """返回大世界"""
        op = BackToNormalWorld(self.ctx)
        return self.round_by_op_result(op.execute())

    @node_from(from_name='开始前返回')
    @operation_node(name='准备截图文件夹')
    def prepare_screenshots_dir(self) -> OperationRoundResult:
        """准备截图文件夹并启动OCR工作线程"""
        import shutil
        if os.path.exists(self.screenshots_dir):
            try:
                shutil.rmtree(self.screenshots_dir)
            except PermissionError as e:
                # 上轮截图被看图器/杀软/OneDrive 占用时清不掉:降级为复用旧目录,不中断本轮扫描
                log.warning(f'旧截图目录清理失败,本轮复用现有目录: {e}')
        self.screenshot_cache.reset_all()
        self.ocr_worker.reset()
        self.ocr_worker.start()
        self.drive_disk_scanner.screenshots_dir = self.screenshots_dir
        self.wengine_scanner.screenshots_dir = self.screenshots_dir
        self.agent_scanner.screenshots_dir = self.screenshots_dir
        self._warn_window_aspect()
        return self.round_success('截图文件夹已准备，OCR工作线程已启动')

    def _warn_window_aspect(self) -> None:
        """issue#4 预警：非 16:9 窗口下游戏会重排仓库布局，固定 rect 全部漂移。

        鲁棒导航可兜底，但强烈建议标准窗口。检测失败静默跳过（不阻塞扫描）。
        """
        try:
            import ctypes
            import ctypes.wintypes
            import win32gui

            hwnd = None

            def _cb(h, _):
                nonlocal hwnd
                if win32gui.GetWindowText(h) == '绝区零' and win32gui.IsWindowVisible(h):
                    hwnd = h

            win32gui.EnumWindows(_cb, None)
            if hwnd is None:
                return
            rc = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rc))
            w, h = rc.right, rc.bottom
            if w <= 0 or h <= 0:
                return
            ratio = w / h
            if abs(ratio - 16 / 9) > 0.04:
                log.warning(
                    f'游戏窗口 {w}x{h}（宽高比 {ratio:.3f}）偏离 16:9，'
                    f'游戏会重排仓库页布局导致自动导航不稳定；'
                    f'建议把游戏窗口分辨率调为 1280x720 或 1920x1080')
        except Exception:
            pass

    @node_from(from_name='准备截图文件夹')
    @operation_node(name='导航到驱动盘界面', node_max_retry_times=5)
    def goto_drive_disk_screen(self) -> OperationRoundResult:
        """导航到驱动盘界面

        issue#4：非 16:9 窗口下游戏重排仓库布局，基座固定 pc_rect 错位导致
        goto_screen 横跳。改用宽区标题 OCR + 页签模板匹配的鲁棒导航，
        失败时回退基座 goto_screen（16:9 窗口下仍走最快路径）。
        """
        if not self._scan_drive_disk:
            return self.round_success('跳过驱动盘')
        fail_reason = goto_drive_warehouse(self, self.round_by_goto_screen)
        if fail_reason is None:
            return self.round_success('已到达驱动仓库')
        return self.round_retry(f'导航驱动仓库失败: {fail_reason}', wait=1.5)

    @node_from(from_name='导航到驱动盘界面')
    @operation_node(name='扫描驱动盘')
    def scan_drive_disks(self) -> OperationRoundResult:
        """扫描驱动盘"""
        if not self._scan_drive_disk:
            log.debug("已跳过驱动盘扫描")
            return self.round_success('驱动盘已跳过')
        log.debug("开始扫描驱动盘...")
        self._disc_game_total = self._ocr_disc_total()
        result = self.drive_disk_scanner.execute()
        if result.success:
            log.debug("驱动盘扫描完成")
            return self.round_success('驱动盘扫描完成')
        else:
            log.error(f"驱动盘扫描失败: {result.status}")
            return self.round_fail(f'驱动盘扫描失败: {result.status}')

    def _ocr_disc_total(self) -> "int | None":
        """OCR 驱动盘仓库页标题「驱动仓库 [ x / y ]」取 x 读不到返回 None

        用于导出后的数量审计(游戏内总数 vs 导出条数) 识别漏扫。
        实现见 repo_total.ocr_repo_total，扫描侧的自检复用同一份逻辑。
        """
        return ocr_repo_total(self, label='驱动盘')

    @node_from(from_name='扫描驱动盘')
    @operation_node(name='导航到音擎界面', node_max_retry_times=5)
    def goto_wengine_screen(self) -> OperationRoundResult:
        """导航到音擎界面

        issue#4：非 16:9 窗口下基座固定 rect 识别/点击失效。
        优先用宽区标题 OCR 判位：已在音擎仓库直接成功；在驱动仓库则模板匹配点
        音擎页签（免去返回大世界的长路径）；其它画面才走原逃逸+基座路由。
        """
        self.ocr_worker.pause()
        if not self._scan_wengine:
            self.ocr_worker.resume()
            return self.round_success('跳过音擎')

        title = read_top_title(self)
        if '音擎仓库' in title:
            self.ocr_worker.resume()
            return self.round_success('已在音擎仓库')
        if '驱动仓库' in title:
            if click_warehouse_tab(self, 'tab_wengine.png', '音擎仓库', label='音擎'):
                self.ocr_worker.resume()
                return self.round_success('已切换到音擎仓库')
            # 点页签失败 → 走原逃逸路径兜底
            log.warning('音擎页签模板切换失败，回退返回大世界路由')

        # 逃逸只做一次: goto 中间态会重入本节点 每轮逃逸会把上轮打开的菜单关掉形成死循环
        if not self._wengine_escaped:
            op = BackToNormalWorld(self.ctx)
            back_result = self.round_by_op_result(op.execute())
            if not back_result.is_success:
                self.ocr_worker.resume()
                return back_result
            self._wengine_escaped = True
        result = self.round_by_goto_screen(screen=self.screenshot(), screen_name='仓库-音擎仓库')
        if result.is_success or result.is_fail:
            self.ocr_worker.resume()
        return result

    @node_from(from_name='导航到音擎界面')
    @operation_node(name='扫描音擎')
    def scan_wengines(self) -> OperationRoundResult:
        """扫描音擎"""
        if not self._scan_wengine:
            log.debug("已跳过音擎扫描")
            return self.round_success('音擎已跳过')
        log.debug("开始扫描音擎...")
        result = self.wengine_scanner.execute()
        if result.success:
            log.debug("音擎扫描完成")
            return self.round_success('音擎扫描完成')
        else:
            log.error(f"音擎扫描失败: {result.status}")
            return self.round_fail(f'音擎扫描失败: {result.status}')

    @node_from(from_name='扫描音擎')
    @operation_node(name='导航到代理人界面', node_max_retry_times=30)
    def goto_agent_screen(self) -> OperationRoundResult:
        """导航到代理人界面"""
        self.ocr_worker.pause()
        if not self._scan_agent:
            self.ocr_worker.resume()
            return self.round_success('跳过角色')

        nav_result = self.round_by_goto_screen(screen_name='代理人-信息')
        if nav_result.is_fail:
            self.ocr_worker.resume()
        if not nav_result.is_success:
            return nav_result

        screen = self.screenshot()
        if self._is_button_colorful(screen, '代理人-信息', '按钮-代理人基础'):
            self.ocr_worker.resume()
            return self.round_success('已到达代理人界面')

        # 游戏会记住上次停留的子页 不在基础页就直接点基础页签
        return self.round_by_find_and_click_area(screen, '代理人-信息', '按钮-代理人基础', success_wait=1)

    def _is_button_colorful(self, screen, screen_name: str, area_name: str) -> bool:
        """
        检测按钮区域是否出现彩色（中间40%区域）
        """
        import cv2
        try:
            area = self.ctx.screen_loader.get_area(screen_name, area_name)

            x1, y1 = int(area.rect.x1), int(area.rect.y1)
            x2, y2 = int(area.rect.x2), int(area.rect.y2)
            region = screen[y1:y2, x1:x2]

            # 左右各裁剪30%，只检测中间40%
            width = region.shape[1]
            crop_left = int(width * 0.3)
            crop_right = int(width * 0.7)
            region = region[:, crop_left:crop_right]

            region_hsv = cv2.cvtColor(region, cv2.COLOR_RGB2HSV)
            avg_s = float(region_hsv[:, :, 1].mean())

            return avg_s > 20
        except Exception:
            return False

    @node_from(from_name='导航到代理人界面')
    @operation_node(name='扫描角色')
    def scan_agents(self) -> OperationRoundResult:
        """扫描角色"""
        if not self._scan_agent:
            log.debug("已跳过角色扫描")
            return self.round_success('角色已跳过')
        log.debug("开始扫描角色...")
        result = self.agent_scanner.execute()
        if result.success:
            log.debug("角色扫描完成")
            return self.round_success('角色扫描完成')
        else:
            log.error(f"角色扫描失败: {result.status}")
            return self.round_fail(f'角色扫描失败: {result.status}')

    @node_from(from_name='扫描角色')
    @operation_node(name='等待OCR完成')
    def wait_ocr_complete(self) -> OperationRoundResult:
        """等待OCR工作线程完成所有任务"""
        log.debug("等待OCR处理完成...")
        self.ocr_worker.wait_complete()
        self.ocr_worker.stop()
        self.screenshot_cache.reset_all()
        log.debug("OCR处理完成，已清空内存缓存")

        return self.round_success('OCR处理完成')

    @node_from(from_name='等待OCR完成')
    @operation_node(name='导出完整数据')
    def export_all_data(self) -> OperationRoundResult:
        """导出所有扫描数据到一个JSON文件"""
        import json
        import time

        try:
            export_dir = os_utils.get_path_under_work_dir('.debug', 'inventory_exports')
            os.makedirs(export_dir, exist_ok=True)

            timestamp = int(time.time())
            export_path = os.path.join(export_dir, f'inventory_{timestamp}.json')

            export_data = {
                'format': 'ZOD',
                'dbVersion': 2,
                'source': 'Zenless Optimizer',
                'version': 1,
                'discs': self.ocr_worker.scanned_discs,
                'wengines': self.ocr_worker.scanned_wengines,
                'characters': self.ocr_worker.scanned_agents
            }

            # 翻页重叠去重: 同指纹(套装+部位+等级+主词条+副词条key与档数)视为重复条目
            raw_discs = export_data['discs']
            seen_fps: set = set()
            deduped: list = []
            for disc in raw_discs:
                fp = (disc.get('setKey'), disc.get('slotKey'), disc.get('level'),
                      disc.get('mainStatKey'),
                      tuple(sorted((s.get('key'), s.get('upgrades', 1))
                                   for s in disc.get('substats', []))))
                if fp in seen_fps:
                    continue
                seen_fps.add(fp)
                deduped.append(disc)
            if len(deduped) != len(raw_discs):
                log.warning(f'导出去重: 原始 {len(raw_discs)} 条 → 去重后 {len(deduped)} 条 '
                            f'(移除翻页重叠重复 {len(raw_discs) - len(deduped)} 条)')
            export_data['discs'] = deduped

            json_str = json.dumps(export_data, indent=2, ensure_ascii=False)

            # 数量审计：游戏内总数 vs 导出条数 差值非零说明存在漏扫或多记
            if self._disc_game_total is not None:
                exported = len(export_data['discs'])
                delta = exported - self._disc_game_total
                export_data['audit'] = {
                    'game_total': self._disc_game_total,
                    'exported': exported,
                    'delta': delta,
                }
                json_str = json.dumps(export_data, indent=2, ensure_ascii=False)
                if delta != 0:
                    log.warning(f'数量审计不一致: 游戏内 {self._disc_game_total} 导出 {exported} 差值 {delta}')
                else:
                    log.info(f'数量审计一致: 游戏内 {self._disc_game_total} = 导出 {exported}')

            with open(export_path, 'w', encoding='utf-8') as f:
                f.write(json_str)

            log.info(f'已导出完整数据到: {export_path}')
            log.info(f'驱动盘: {len(export_data["discs"])}个, 音擎: {len(export_data["wengines"])}个, 角色: {len(export_data["characters"])}个')
            log.info('可导入在线优化器（Zenless Optimizer）: https://zzz.233618.xyz/')

            return self.round_success('完整数据已导出，可导入在线优化器')

        except Exception as e:
            log.error(f'导出完整数据失败: {e}')
            return self.round_fail(f'导出失败: {e}')

    def _ensure_plugin_assets(self) -> None:
        """插件自带资产合并到工作目录 识别管线与 wiki 图标库供框架按工作目录路径加载"""
        try:
            self._copy_plugin_assets()
            self._merge_screen_info()
        except Exception as e:
            # 真·只读安装/杀软锁定等环境问题:不裸崩,记录后继续(管线缺失会导致识别降级,但应用仍可启动)
            log.error(f'插件资产合并失败(识别功能可能降级),请检查工作目录 assets 写权限: {e}')

    def _merge_screen_info(self) -> None:
        """把插件补充的仓库画面区域合并进工作目录 screen_info 并重载。

        官仓基座的 storage_drive_disc.yml 缺少 驱动盘仓库/驱动盘属性/驱动盘进度条
        三个扫描依赖区域（zode 分支新增，见官仓 issue#4 系列）——缺了会导致
        方格检测流水线取不到区域直接崩溃。按区域名做增量合并，不动官仓既有定义。
        """
        import yaml

        plugin_screen_dir = Path(__file__).parent / "assets" / "screen_info"
        if not plugin_screen_dir.exists():
            return
        dst_screen_dir = Path(os_utils.get_path_under_work_dir("assets", "game_data", "screen_info"))
        dst_screen_dir.mkdir(parents=True, exist_ok=True)

        changed = False
        for yml in plugin_screen_dir.glob('*.yml'):
            target = dst_screen_dir / yml.name
            with yml.open(encoding='utf-8') as f:
                plugin_data = yaml.safe_load(f)
            if not isinstance(plugin_data, dict):
                continue
            plugin_areas = {a.get('area_name'): a for a in plugin_data.get('area_list', []) if isinstance(a, dict)}

            if target.exists():
                with target.open(encoding='utf-8') as f:
                    base_data = yaml.safe_load(f)
                if not isinstance(base_data, dict):
                    base_data = plugin_data
                base_areas = {a.get('area_name') for a in base_data.get('area_list', []) if isinstance(a, dict)}
                missing = [v for k, v in plugin_areas.items() if k not in base_areas]
                if not missing:
                    continue
                base_data.setdefault('area_list', []).extend(missing)
                merged = base_data
            else:
                merged = plugin_data

            with target.open('w', encoding='utf-8') as f:
                yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
            log.info(f'插件画面定义已合并: {yml.name} (+{len(plugin_areas) if not target.exists() else "增量"})')
            changed = True

        if changed:
            try:
                self.ctx.screen_loader.reload(from_separated_files=True)
                log.info('screen_loader 已重载（插件画面区域生效）')
            except Exception as e:
                log.warning(f'screen_loader 重载失败（重启应用后生效）: {e}')

    def _copy_plugin_assets(self) -> None:
        plugin_assets = Path(__file__).parent / "assets"
        dst_pipelines = Path(os_utils.get_path_under_work_dir("assets", "image_analysis_pipelines"))
        dst_pipelines.mkdir(parents=True, exist_ok=True)
        for yml in (plugin_assets / "image_analysis_pipelines").iterdir():
            target = dst_pipelines / yml.name
            if not target.exists():
                shutil.copy2(yml, target)
        dst_wiki = Path(os_utils.get_path_under_work_dir("assets", "wiki_data"))
        dst_wiki.mkdir(parents=True, exist_ok=True)
        for item in (plugin_assets / "wiki_data").iterdir():
            target = dst_wiki / item.name
            if item.is_dir():
                # icons 等子目录递归复制（copy2 遇目录会 PermissionError）
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif not target.exists():
                shutil.copy2(item, target)
