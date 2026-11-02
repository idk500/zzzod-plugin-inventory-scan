"""仓库页面标题数量 OCR。

页面顶部条形如「驱动仓库 [ 137 / 380 ]」，取分子（已拥有数量）。
主流程用它做导出后的数量审计，扫描前自检用它区分「仓库本来就空」与「画面认不出来」。
"""

import re
from typing import Optional

from one_dragon.utils.log_utils import log


def ocr_repo_total(operation, label: Optional[str] = None) -> Optional[int]:
    """OCR 当前仓库页标题里的已拥有数量，读不到返回 None。

    注意必须锚定含「仓库」字样的文本：页面顶部条还有右上角邦布等计数器会干扰。

    Args:
        operation: 具备 ctx 与 screenshot() 的 Operation 实例
        label: 日志前缀，缺省用 operation.op_name（驱动盘/音擎扫描共用本函数）
    """
    from one_dragon.base.geometry.rectangle import Rect

    prefix = label if label is not None else getattr(operation, 'op_name', '仓库')
    try:
        screen = operation.screenshot()
        results = operation.ctx.ocr.crop_and_run_ocr(screen, Rect(0, 0, screen.shape[1], 200))
        texts = list(results.keys())
        log.info(f'{prefix}仓库页顶部 OCR: {texts}')

        hits: list[tuple[bool, int]] = []
        for text in texts:
            matched = re.search(r'(\d+)\s*/\s*(\d+)', text)
            if matched:
                hits.append(('仓库' in text, int(matched.group(1))))

        anchored = [item for item in hits if item[0]]
        if anchored:
            return anchored[0][1]
        if hits:
            log.warning(f'{prefix}总数候选无「仓库」锚定 取首个: {hits}')
            return hits[0][1]
        return None
    except Exception as e:
        log.error(f'{prefix} OCR 仓库总数失败: {e}')
        return None
