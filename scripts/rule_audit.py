# -*- coding: utf-8 -*-
"""规则层预筛分析（评分保留制第 2 步的前置原型）

只做分析出报告，不做任何游戏内操作（不标记、不拆解）。

规则（2026-10-28 用户定义）:
  R1 主词条错配: 进攻型套装(攻击/属性伤/双暴/穿透) 上, 4/5/6 号位主词条为
     生命% 或 防御% —— 套装属性与主词条方向相反, 判死
  R2 双暴套装废副词条: 双暴套装(啄木鸟电音/折枝剑歌) 上, 副词条全部为
     生命/防御系 —— 数值层缺 value 也能判

豁免（永不进决议）: 已装备 / 已锁定 / 已带弃置标记 / (已满级单独列出待人工确认)

套装分类 = 知识先验 + 数据推断（用本账号已装备满级盘的主词条分布交叉验证,
不确定的新套装只靠推断并在报告中标低置信度）。
"""
import json
import os
import sys
import collections

# ---------- 配置 ----------

# 知识先验: 2pc 效果方向
SET_PRIOR = {
    # 进攻型 (atk% / 属性伤 / 双暴 / 穿透)
    'AstralVoice': 'OFF',        # 静听嘉音 atk%
    'HormonePunk': 'OFF',        # 激素朋克 atk%
    'ChaoticMetal': 'OFF',       # 混沌金属 ether
    'InfernoMetal': 'OFF',       # 炎狱重金属 fire
    'ThunderMetal': 'OFF',       # 雷重金属 electric
    'FangedMetal': 'OFF',        # 獠牙重金属 physical
    'PolarMetal': 'OFF',         # 极地重金属 ice
    'WoodpeckerElectro': 'OFF',  # 啄木鸟电音 暴击率
    'Branch&BladeSong': 'OFF',   # 折枝剑歌 暴伤
    'PufferElectro': 'OFF',      # 河豚电音 穿透
    'YunkuiTales': 'OFF',        # 云岿如我 ether(仪玄)
    # 功能型
    'SwingJazz': 'UTIL',         # 摇摆爵士 能量
    'ShockstarDisco': 'UTIL',    # 震星迪斯科 冲击
    'FreedomBlues': 'UTIL',      # 自由蓝调 精通
    'SoulRock': 'DEF',           # 灵魂摇滚 防御
    # 未知 2pc 的新套装 → 只走数据推断
    # 注意: 若账号内推断与先验冲突（dead 主词条占比 >= 0.5），推断优先 → 'HP' 豁免。
    # 2026-10-28 实证: 云岿如我/月光摇篮曲 在本账号是命破 HP 套(Manato/Yidhari/Zhao/Lucia)。
}

CRIT_SETS = {'WoodpeckerElectro', 'Branch&BladeSong'}  # R2 适用范围
DEAD_SUBS = {'hp', 'hp_', 'def', 'def_'}               # R2 的废副词条族
OFFENSIVE_MAINS = {'atk_', 'crit_', 'crit_dmg_', 'pen_',
                   'ether_dmg_', 'fire_dmg_', 'electric_dmg_',
                   'physical_dmg_', 'ice_dmg_'}
DEAD_MAINS = {'def_', 'hp_'}   # 4/5/6 号位上的坦克主词条

# ---------- 分类推断 ----------


def infer_set_class(discs):
    """用已装备满级盘的 4/5/6 号主词条分布推断套装方向。"""
    usage = collections.defaultdict(collections.Counter)
    for d in discs:
        if not d.get('location') or d['level'] < 12 or d['slotKey'] not in '456':
            continue
        s = usage[d['setKey']]
        if d['mainStatKey'] in OFFENSIVE_MAINS:
            s['off'] += 1
        elif d['mainStatKey'] in DEAD_MAINS:
            s['dead'] += 1
        else:
            s['util'] += 1
    inferred = {}
    for k, s in usage.items():
        total = sum(s.values())
        if total >= 3:
            dead_share = s['dead'] / total
            if dead_share >= 0.5:
                inferred[k] = ('HP', round(dead_share, 2), total)  # 账号实证的 HP/防御向套装, 豁免
            else:
                inferred[k] = ('OFF' if s['off'] / total >= 0.7 else
                               'DEF' if dead_share >= 0.7 else 'UTIL',
                               round(s['off'] / total, 2), total)
    return inferred


# ---------- 规则判定 ----------


def classify(discs):
    inferred = infer_set_class(discs)
    r1, r2, lv15_dead = [], [], []
    protected = 0
    for d in discs:
        if d.get('location') or d.get('lock') or d.get('trash'):
            protected += 1
            continue
        cls, conf = SET_PRIOR.get(d['setKey']), 'prior'
        if d['setKey'] in inferred:
            icls, ratio, n = inferred[d['setKey']]
            if icls == 'HP':
                cls, conf = 'HP', f'account-data({ratio}/{n}) 豁免'
            elif cls is None:
                cls, conf = icls, f'inferred({ratio}/{n})'
        if cls in (None, 'HP') or cls != 'OFF':
            continue
        dead_main = d['slotKey'] in '456' and d['mainStatKey'] in DEAD_MAINS
        dead_subs = (d['setKey'] in CRIT_SETS and d['substats'] and
                     all(s['key'] in DEAD_SUBS for s in d['substats']))
        if not (dead_main or dead_subs):
            continue
        rule = 'R1主词条错配' if dead_main else 'R2双暴套装废副词条'
        if dead_main and dead_subs:
            rule += '+R2'
        item = _reason(d, rule, conf)
        if dead_main:
            if d['level'] >= 15:
                lv15_dead.append(item)
            else:
                r1.append(item)
        else:
            r2.append(item)
    return r1, r2, lv15_dead, inferred


def _reason(d, rule, conf):
    subs = ', '.join(f"{s['key']}x{s['upgrades']}" for s in d['substats']) or '无'
    return {
        'id': d['id'], 'level': d['level'], 'rule': rule, 'conf': conf,
        'desc': (f"{d['setKey']} {d['rarity']}级 L{d['level']} "
                 f"{d['slotKey']}号 主词条[{d['mainStatKey']}] 副[{subs}]"),
        'main': d['mainStatKey'], 'set': d['setKey'], 'slot': d['slotKey'],
        'subs': sorted(s['key'] for s in d['substats']),
    }


def emit_decisions(r1, r2, out_path):
    """导出指纹决策清单 供 DiscCleanupApp 现场匹配执行（存指纹不存坐标）"""
    import json
    import time
    r1_triples = sorted({(x['set'], x['slot'], x['main']) for x in r1})
    r2_items = sorted({(x['set'], x['slot'], x['main'], tuple(x['subs'])) for x in r2})
    payload = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'rule_audit.py 规则层预筛（R1主词条错配/R2双暴套装废副词条）',
        'capacity_free_min': 200,   # 仓库余位低于此值才执行标记（用户 2026-10-28 拍板）
        'r1_triples': [list(t) for t in r1_triples],
        'r2_items': [{'set': s, 'slot': sl, 'main': m, 'subs': list(sub)}
                     for s, sl, m, sub in r2_items],
        # 现场按钮坐标：probe 阶段确认后回填（null = 只探测不标记）
        'ui': {'trash_button_rect': None, 'confirm_button_rect': None},
        'notes': '匹配规则: set+slot+main 命中 R1 且 level<15 未装备即标弃置; '
                 'R2 额外要求副词条集合一致; 满级/已装备/已锁定现场复核跳过',
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'[decisions] R1三元组 {len(r1_triples)} 组, R2条目 {len(r2_items)} 件 -> {out_path}')


# ---------- 报告 ----------


def report(discs, r1, r2, lv15, inferred):
    lines = ['# 规则层预筛报告（分析性质，未做任何游戏内操作）', '',
             f'- 数据源: 10-25 导出快照（{len(discs)} 盘）',
             '- 适用范围: 未装备、未锁定、未标记的盘（已装备/锁定/已标记自动豁免）',
             '- R1/R2 均不依赖副词条数值，规则层可先行；数值层（方案A第2步）后续叠加',
             '']
    n = len(r1) + len(r2)
    lines += [f'## 结论: 硬规则可回收 **{n} 件**（另有满级判死 {len(lv15)} 件待人工确认）', '']
    for title, items in [('R1 主词条错配（进攻套装 + 4/5/6号 生命%/防御%主词条）', r1),
                         ('R2 双暴套装全废副词条（副词条全为生命/防御系）', r2)]:
        lines.append(f'## {title}: {len(items)} 件')
        by_set = collections.Counter((x['set'], x['slot']) for x in items)
        for (s, slot), c in by_set.most_common(12):
            lines.append(f'- {s} {slot}号 ×{c}')
        lines.append('')
        lines.append('样例（前 15）:')
        lines += [f"- {x['desc']}  `{x['conf']}`" for x in items[:15]]
        lines.append('')
    if lv15:
        lines.append(f'## 满级但判死: {len(lv15)} 件 —— 已投入资源，先不进决议，人工过目')
        lines += [f"- {x['desc']}" for x in lv15[:10]]
        lines.append('')
    lines.append('## 套装分类依据（已装备满级盘主词条分布 × 知识先验，冲突时数据优先）')
    lines.append('- `HP` = 本账号实证的生命/防御向套装（命破系角色在穿），整组豁免 R1')
    lines.append('- `OFF` = 进攻型（R1 适用）；`UTIL` = 功能型（R1 不适用）')
    for k, v in sorted(inferred.items()):
        lines.append(f'- {k}: {v}')
    lines.append('')
    lines.append('## 下一步')
    lines.append('1. 你确认分类表（尤其 inferred 低置信度套装）与样例是否合理')
    lines.append('2. 确认后开发批量标弃置节点（白名单二次校验 + 单轮上限），执行这批硬规则')
    lines.append('3. 解析器补 value 后叠加数值层，再处理规则层漏掉的灰区盘')
    return '\n'.join(lines)


def main():
    base = None
    for cand in [r'E:\ZODE\.debug\inventory_exports',
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              '..', '..', '..', '.debug', 'inventory_exports')]:
        if os.path.isdir(cand):
            base = cand
            break
    files = sorted((f for f in os.listdir(base) if f.endswith('.json')),
                   key=lambda f: os.path.getmtime(os.path.join(base, f)))
    path = os.path.join(base, files[-1])
    discs = json.load(open(path, encoding='utf-8'))['discs']
    r1, r2, lv15, inferred = classify(discs)
    md = report(discs, r1, r2, lv15, inferred)
    print(md)
    out = r'E:\ZZZ-1D\zode-agent\bridge\outbox\rule-prefilter-report-2026-10-28.md'
    if os.path.isdir(os.path.dirname(out)):
        with open(out, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f'\n[saved] {out}')
    emit_decisions(r1, r2, r'E:\ZZZ-1D\.debug\cleanup_decisions.json')
    emit_decisions(r1, r2, r'E:\ZODE\.debug\cleanup_decisions.json')


if __name__ == '__main__':
    sys.exit(main())
