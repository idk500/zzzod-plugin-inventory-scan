APP_ID = "drive_disk_scan"
APP_NAME = "驱动盘自动扫描"

# 扫描前识别自检：正常一页仓库远多于此数，低于它就说明画面识别出了问题（HDR/滤镜/分辨率/语言）
GRID_PRECHECK_MIN_COUNT = 8
# 仓库标题总数达到此值才做严格自检，装不满一页的仓库不因格子少而报错
GRID_PRECHECK_STRICT_TOTAL = 8
# 翻页位移校验的帧差阈值（灰度缩略图平均绝对差，正常翻页远大于 1）
SCROLL_MOVE_MIN_DIFF = 1.0

# ---------------------------------------------------------------------------
# 网格几何
#
# 仓库列表是严格等距的固定网格（实测 1920x1080 窗口：9 列、列距 135、行距 174，
# 首格中心 (167, 319)）。点击不该由「这次认出几个方格」决定，否则 HSV 阈值一被
# 画质/HDR/滤镜影响，就只点认出来的那几个，表现为「扫一两个就结束」。
# 这里以几何为默认值，视觉检测只用来校准原点与间距。
# ---------------------------------------------------------------------------
GRID_COLS_FALLBACK = 9
GRID_ROWS_FALLBACK = 4
GRID_DX_FALLBACK = 135.0
GRID_DY_FALLBACK = 174.0
GRID_ORIGIN_FALLBACK = (167.0, 319.0)

# 拟合至少需要这么多检测点，少于此数用上一次成功的网格或默认几何
GRID_FIT_MIN_POINTS = 4
# 拟合出的间距必须落在此区间，否则判定检测乱掉、退回默认几何
GRID_DX_RANGE = (110.0, 170.0)
GRID_DY_RANGE = (145.0, 215.0)
# 检测点聚类容差（按默认间距的比例算，避免手动调绝对值）
GRID_CLUSTER_TOL_RATIO = 0.35

# 空格判定：格子中心正方形内「有内容的像素」占比低于此值视为空槽，直接跳过不点击
# 实测满格 0.29~0.41，空槽接近 0
CELL_PATCH_HALF = 45
CELL_EMPTY_CONTENT_RATIO = 0.12
# 内容像素判据：亮度高于此值且不是选中态的青色
CELL_CONTENT_MIN_V = 85

# 点击生效校验：右侧详情区灰度缩略图平均绝对差超过此值才算「真的选中了新格子」
DETAIL_CHANGE_MIN_DIFF = 2.0
DETAIL_SIG_SIZE = 96
DETAIL_WAIT_ROUNDS = 6
DETAIL_WAIT_INTERVAL = 0.12
