# -*- coding: utf-8 -*-
"""扫盘器导出审计：数量核对 + 重复指纹 + 分布体检

用法:
    python audit_export.py [export.json]
    不带参数时取 .debug/inventory_exports 下最新一份导出。

审计项:
    1. audit 元数据（游戏内总数 vs 导出条数，需 v1.0.11+ 扫描产出）
    2. 副词条数值完整性（value 缺失会让 ZOD 无法评分）
    3. 重复指纹（套装+稀有度+等级+部位+主词条+副词条 key/upgrades）
    4. 稀有度/部位分布合理性
"""
import collections
import json
import os
import sys


def find_latest_export() -> str:
    candidates = [
        r'E:\ZODE\.debug\inventory_exports',
        r'E:\ZZZ-1D\.debug\inventory_exports',
        os.path.join(os.getcwd(), '.debug', 'inventory_exports'),
    ]
    for base in candidates:
        if os.path.isdir(base):
            files = sorted((f for f in os.listdir(base) if f.endswith('.json')),
                           key=lambda f: os.path.getmtime(os.path.join(base, f)))
            if files:
                return os.path.join(base, files[-1])
    raise FileNotFoundError('找不到导出文件，请传入路径参数')


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else find_latest_export()
    data = json.load(open(path, encoding='utf-8'))
    discs = data.get('discs', [])
    print(f'导出文件: {path}')
    print(f'驱动盘: {len(discs)}  音擎: {len(data.get("wengines", []))}  代理人: {len(data.get("characters", []))}')

    # 1. 数量审计
    audit = data.get('audit')
    if audit:
        flag = 'OK' if audit['delta'] == 0 else '!! 不一致 !!'
        print(f'[数量审计] 游戏内 {audit["game_total"]} / 导出 {audit["exported"]} / 差值 {audit["delta"]}  {flag}')
    else:
        print('[数量审计] 无 audit 元数据（旧版导出，建议用 v1.0.11+ 重扫）')

    # 2. 副词条数值完整性
    if discs:
        no_val = sum(1 for d in discs
                     if d.get('substats') and any('value' not in s for s in d['substats']))
        print(f'[数值完整性] 副词条缺 value: {no_val}/{len(discs)}'
              + ('  !! ZOD 无法评分，需升级解析器 !!' if no_val else '  OK'))

    # 3. 重复指纹
    def fp(d):
        subs = tuple(sorted((s['key'], s.get('upgrades', 0)) for s in d.get('substats', [])))
        return (d['setKey'], d['rarity'], d['level'], d['slotKey'], d['mainStatKey'], subs)

    fps = collections.Counter(fp(d) for d in discs)
    dups = {k: v for k, v in fps.items() if v > 1}
    dup_items = sum(dups.values())
    print(f'[重复指纹] 唯一 {len(fps)} / 总数 {len(discs)} / 重复组 {len(dups)} 组涉及 {dup_items} 条')
    if dup_items:
        # 指纹未含副词条数值 L0 同词条盘可能是真实重复 仅提示比例
        ratio = dup_items / max(len(discs), 1)
        print(f'           重复占比 {ratio:.1%}（指纹不含副词条数值，L0 同词条盘可能为真实重复，仅作趋势参考）')

    # 4. 分布
    rarity = collections.Counter(d['rarity'] for d in discs)
    slot = collections.Counter(d['slotKey'] for d in discs)
    print(f'[稀有度分布] {dict(rarity)}')
    print(f'[部位分布] {dict(sorted(slot.items()))}')


if __name__ == '__main__':
    main()
