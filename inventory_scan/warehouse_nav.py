"""仓库页面鲁棒导航（issue#4 修复：非 16:9 窗口布局漂移）。

根因：游戏窗口宽高比 ≠ 16:9（如 1910x1054）时游戏会重排仓库页布局
（网格变窄、右上页签条整体平移），基座把截图拉伸回 1920x1080 后，
固定 pc_rect 与真实 UI 错位 —— goto_screen 认不出「仓库-音擎仓库」、
TAB-驱动盘 固定坐标点空，导致 仓库/大世界 反复横跳。

本模块不依赖整页固定 rect 识别：
1. 顶部条宽区 OCR 锚定「仓库」判定当前页签（标题位置在各窗口宽度下基本稳定）
2. 页签图标模板匹配定位（平移容忍）后点击，再以标题校验切换结果
3. 打开仓库仍走基座 goto_screen（大世界→菜单→底部-仓库 点击链实测在漂移窗口下同样有效）
"""

from typing import Optional

from one_dragon.base.geometry.rectangle import Rect
from one_dragon.utils import cv2_utils
from one_dragon.utils.log_utils import log

# 顶部标题条 OCR 区域：全宽 x 顶部 200px（design 坐标，截图已归一化）
_TITLE_RECT = Rect(0, 0, 1920, 200)

# 页签图标条搜索带：右上横带。标准 16:9 下页签条在 x≈1450-1760，
# 漂移窗口下整体左移可到 x≈1200，取 (1050, 70)-(1920, 250) 覆盖全部情况。
_TAB_SEARCH_RECT = Rect(1050, 70, 1920, 250)


def read_top_title(operation) -> str:
    """宽区 OCR 顶部标题条，返回拼接文本（容忍布局漂移）。

    Args:
        operation: 具备 ctx 与 screenshot() 的 Operation 实例
    """
    screen = operation.screenshot()
    results = operation.ctx.ocr.crop_and_run_ocr(screen, _TITLE_RECT)
    return ' '.join(results.keys())


def _load_tab_template(tab_asset_name: str):
    """加载插件自带的页签图标模板，BGR 转 RGB 与框架截图色序一致。"""
    import cv2
    from pathlib import Path
    tpl_path = Path(__file__).parent / 'assets' / 'ui' / tab_asset_name
    if not tpl_path.exists():
        return None
    tpl = cv2.imread(str(tpl_path), cv2.IMREAD_COLOR)
    if tpl is None:
        return None
    return cv2.cvtColor(tpl, cv2.COLOR_BGR2RGB)


def click_warehouse_tab(operation, tab_asset_name: str, target_keyword: str,
                        label: str = '仓库') -> bool:
    """在仓库页之间切换页签：模板匹配页签图标（平移容忍）后点击并校验标题。

    Args:
        operation: 具备 ctx/screenshot/ctx.controller 的 Operation 实例
        tab_asset_name: 插件 assets/ui 下的页签模板文件名
        target_keyword: 点击后标题应出现的关键字（如 '驱动仓库'）
        label: 日志前缀

    Returns:
        bool: 切换后标题是否包含目标关键字
    """
    template = _load_tab_template(tab_asset_name)
    if template is None:
        log.warning(f'{label} 页签模板缺失: {tab_asset_name}')
        return False

    import time
    screen = operation.screenshot()
    x1, y1 = int(_TAB_SEARCH_RECT.x1), int(_TAB_SEARCH_RECT.y1)
    x2, y2 = int(_TAB_SEARCH_RECT.x2), int(_TAB_SEARCH_RECT.y2)
    region = screen[y1:y2, x1:x2]
    match_list = cv2_utils.match_template(source=region, template=template,
                                          threshold=0.7, ignore_inf=True)
    best = match_list.max
    if best is None:
        log.warning(f'{label} 页签模板未命中（窗口布局漂移过大或模板过期）')
        return False

    center = best.center
    design_x, design_y = x1 + center.x, y1 + center.y
    log.info(f'{label} 页签模板命中 design=({design_x},{design_y}) 相似度={best.confidence:.3f}')
    from one_dragon.base.geometry.point import Point
    operation.ctx.controller.click(Point(design_x, design_y))
    time.sleep(1.5)

    title = read_top_title(operation)
    ok = target_keyword in title
    log.info(f'{label} 页签切换后标题: {title!r} 命中={ok}')
    return ok


def goto_drive_warehouse(operation, base_goto) -> Optional[str]:
    """鲁棒导航到 仓库-驱动仓库。

    Args:
        operation: 应用实例（具备 screenshot/ctx）
        base_goto: 基座 round_by_goto_screen 方法（兜底路径）

    Returns:
        None 表示成功；否则返回失败原因文本
    """
    # 快路径：当前已在驱动仓库
    title = read_top_title(operation)
    if '驱动仓库' in title:
        return None

    # 已在音擎仓库 → 模板匹配点驱动盘页签
    if '音擎仓库' in title:
        if click_warehouse_tab(operation, 'tab_drive.png', '驱动仓库', label='驱动盘'):
            return None
        return '音擎仓库页点驱动盘页签失败'

    # 其它画面 → 基座导航打开仓库（点击链在漂移窗口下有效，识别结果不作准）
    result = base_goto(screen_name='仓库-音擎仓库')
    if result.is_fail:
        return f'基座导航打开仓库失败: {result.status}'

    # 打开后用宽区标题复核（不信任 goto_screen 的固定 rect 识别）
    for _ in range(3):
        title = read_top_title(operation)
        if '驱动仓库' in title:
            return None
        if '音擎仓库' in title:
            if click_warehouse_tab(operation, 'tab_drive.png', '驱动仓库', label='驱动盘'):
                return None
            return '音擎仓库页点驱动盘页签失败'
        import time
        time.sleep(1)
    return f'打开仓库后标题未识别: {title!r}'
