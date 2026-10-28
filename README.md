# zzzod-plugin-inventory-scan

仓库扫描器 —— 绝区零一条龙插件：自动翻页扫描仓库中的代理人 / 驱动盘 / 音擎，OCR 识别并导出清单。

- 输出目录：`\.debug/inventory_exports`
- 安装：插件管理导入 zip，或待官方 plugin-data 收录后自动同步
- 许可：GPL-3.0（衍生自主仓）

## 驱动盘清理标记（disc_cleanup，本仓库第二个插件）

与扫盘器解耦的独立应用（同仓库并列插件包，安装后各自出现在应用列表，可单独启停）：

- 按离线规则分析产物 `.debug/cleanup_decisions.json`（由 `scripts/rule_audit.py` 生成）逐盘指纹匹配：命中 R1（进攻套装主词条错配）/ R2（双暴套装全生/防废副词条）且未装备、未满级才打弃置标记，现场不符即跳过。
- 决议存指纹（套装+槽位+主词条+副词条集合）不存坐标，执行时实时重扫仓库匹配，同指纹盘可互换。
- 余位 ≥ `capacity_free_min`（默认 200）自动跳过整轮清理；满级（L15）盘永不自动标记；弃置标记可逆，实际拆解仍由 `drive_disc_dismantle` 应用执行。
- 配置项：`probe_mode`（探测模式，只存样本绝不标记，默认开）、`mark_limit`（单轮标记上限）、`capacity_free_min`（余位门槛）。
- 依赖：需与 `inventory_scan` 插件同时安装（复用其驱动盘 OCR 解析模块）。

## 导出数据与配套优化器

「导出完整数据」会在输出目录生成 `inventory_<时间戳>.json`（ZOD 格式，dbVersion 2，含驱动盘 / 音擎 / 代理人清单）。

该文件可直接导入在线配装优化器 **[Zenless Optimizer](https://zzz.233618.xyz/)**（绝区零优化器）：在「驱动盘」页导入 JSON，即可按角色 / 队伍计算最优驱动盘组合。推荐来自 @Usagi-wusaqi。
