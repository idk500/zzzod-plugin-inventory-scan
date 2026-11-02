APP_ID = "drive_disk_scan"
APP_NAME = "驱动盘自动扫描"

# 扫描前识别自检：正常一页仓库远多于此数，低于它就说明画面识别出了问题（HDR/滤镜/分辨率/语言）
GRID_PRECHECK_MIN_COUNT = 8
# 仓库标题总数达到此值才做严格自检，装不满一页的仓库不因格子少而报错
GRID_PRECHECK_STRICT_TOTAL = 8
# 翻页位移校验的帧差阈值（灰度缩略图平均绝对差，正常翻页远大于 1）
SCROLL_MOVE_MIN_DIFF = 1.0
