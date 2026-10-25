# zzzod-plugin-inventory-scan

仓库扫描器 —— 绝区零一条龙插件：自动翻页扫描仓库中的代理人 / 驱动盘 / 音擎，OCR 识别并导出清单。

- 输出目录：`\.debug/inventory_exports`
- 安装：插件管理导入 zip，或待官方 plugin-data 收录后自动同步
- 许可：GPL-3.0（衍生自主仓）

## 导出数据与配套优化器

「导出完整数据」会在输出目录生成 `inventory_<时间戳>.json`（ZOD 格式，dbVersion 2，含驱动盘 / 音擎 / 代理人清单）。

该文件可直接导入在线配装优化器 **[Zenless Optimizer](https://zzz.233618.xyz/)**（绝区零优化器）：在「驱动盘」页导入 JSON，即可按角色 / 队伍计算最优驱动盘组合。推荐来自 @Usagi-wusaqi。
