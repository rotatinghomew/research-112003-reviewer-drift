"""直播内容定性核对：尺度可比性、单签资质、会签与补盲。

只读 data/ 下的登记表，生成可复核的 Markdown 报告，不改写原始资料。

用法：
    python3 qc_analysis.py
    python3 qc_analysis.py --out REPORT.md

口径：
* 锚点只取指定盲测（anchors.source=blind）与当时适用的仲裁标准
  （source=appeal），重复判读按片段去重。
* 已揭盲后复看（ratings.origin=unblinded）不计入任何锚点证据。
* 先按判读员估计循环编码约定，再比较尺度；未归一时不比较原始均值或处罚比。
* 可比较范围按类目×难度×版本报告，并给出零误差样本的 95% 上界。
"""
from __future__ import annotations

import argparse
import csv
import itertools
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
TABLES = (
    'ratings', 'anchors', 'pack_events', 'people', 'decisions', 'queue', 'appeals',
    'blind_inventory', 'blind_history', 'calendar', 'booked', 'case_load',
    'blind_returns',
)
LABELS = (0, 1, 2)
CATEGORIES = ('claims', 'context', 'service')
DIFFICULTIES = ('hard', 'ordinary')
VERSIONS = ('v1', 'v2')


def load(data_dir=DATA):
    tables = {}
    for name in TABLES:
        with (Path(data_dir) / f'{name}.csv').open(encoding='utf-8', newline='') as fh:
            tables[name] = list(csv.DictReader(fh))
    return tables


def zero_error_ub(n):
    """零误差时真实错误率的 95% 上界（rule of three）；n=0 无定义。"""
    return None if n <= 0 else 1 - 0.05 ** (1 / n)


def groups(value):
    return [g for g in value.split('|') if g]


def fmt(value, digits=3):
    return '—' if value is None else f'{value:.{digits}f}'


def md_table(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |',
           '| ' + ' | '.join('---' for _ in headers) + ' |']
    for row in rows:
        out.append('| ' + ' | '.join(str(cell) for cell in row) + ' |')
    return out


class Corpus:
    def __init__(self, tables):
        self.t = tables
        self.anchors = {a['clip']: a for a in tables['anchors']}
        self.people = {p['reviewer']: p for p in tables['people']}
        self.team = {p['reviewer']: p['team'] for p in tables['people']}
        self.roster = {p['reviewer']: p['category'] for p in tables['people']}
        self.by_clip = defaultdict(dict)
        for r in tables['ratings']:
            self.by_clip[r['clip']][r['reviewer']] = r
        self.meta = {}
        for r in tables['ratings']:
            self.meta.setdefault(r['clip'], r)
        self.inventory = {i['clip']: i for i in tables['blind_inventory']}
        self.history = defaultdict(list)
        for h in tables['blind_history']:
            self.history[h['clip']].append(h)
        self.booked = {b['clip']: b for b in tables['booked']}
        self.returns = defaultdict(dict)
        for r in tables['blind_returns']:
            self.returns[r['clip']][r['reviewer']] = r
        self.pack_events = {p['pack']: p for p in tables['pack_events']}
        self.queue = {q['case']: q for q in tables['queue']}
        self.decisions = {d['id']: d for d in tables['decisions']}
        self.appeals = {a['case']: a for a in tables['appeals']}
        self.case_load = {c['case']: c for c in tables['case_load']}
        self.minutes = {}
        self.day = {}
        for c in tables['calendar']:
            self.minutes[(c['reviewer'], c['shift'])] = int(c['minutes'])
            self.day[c['shift']] = int(c['day'])
        self.ambiguous = self._ambiguous_clips()
        self.offsets, self.convention = self.infer_offsets()

    def _ambiguous_clips(self):
        """同一片段内波次/类目/难度/版本登记冲突的片段，排除出分层统计。"""
        out = set()
        for clip, readings in self.by_clip.items():
            for field in ('wave', 'category', 'difficulty', 'standard'):
                if len({r[field] for r in readings.values()}) > 1:
                    out.add(clip)
                    break
        return out

    # ---- 编码约定 ---------------------------------------------------------
    def anchor_rows(self, sources=('blind',), max_wave=None, asof=None,
                    standard=None):
        rows = []
        for r in self.t['ratings']:
            a = self.anchors.get(r['clip'])
            if not a or a['source'] not in sources:
                continue
            if r['origin'] != 'blind':
                continue
            if r['wave'] != a['wave'] or r['standard'] != a['standard']:
                continue
            if max_wave is not None and int(r['wave']) > max_wave:
                continue
            if asof is not None and int(a['known']) > asof:
                continue
            if standard and a['standard'] != standard:
                continue
            rows.append((r, a))
        return rows

    def infer_offsets(self):
        grouped = defaultdict(list)
        for r, a in self.anchor_rows():
            grouped[r['reviewer']].append((r, a))
        offsets, diag = {}, {}
        for rev in sorted(set(self.people) | set(grouped)):
            obs = grouped.get(rev, [])
            if not obs:
                offsets[rev] = None
                diag[rev] = {'n': 0}
                continue
            scored = []
            for k in LABELS:
                errs = [(int(r['label']) - k) % 3 - int(a['label']) for r, a in obs]
                scored.append((sum(abs(e) for e in errs), sum(e == 0 for e in errs), k))
            scored.sort()
            best, second = scored[0], scored[1]
            offsets[rev] = best[2]
            diag[rev] = {
                'n': len(obs), 'offset': best[2], 'abs_error': best[0],
                'exact': best[1], 'margin': second[0] - best[0],
                'raw_mae': sum(abs(int(r['label']) - int(a['label']))
                               for r, a in obs) / len(obs),
            }
        return offsets, diag

    def harmonize(self, reviewer, label):
        off = self.offsets.get(reviewer)
        return None if off is None else (int(label) - off) % 3

    # ---- 配对 -------------------------------------------------------------
    def usable(self, clip):
        if clip in self.ambiguous:
            return {}
        return {rev: r for rev, r in self.by_clip[clip].items()
                if r['origin'] != 'unblinded'}

    def pairs(self):
        for clip, readings in self.by_clip.items():
            usable = self.usable(clip)
            if len(usable) < 2:
                continue
            for x, y in itertools.combinations(sorted(usable), 2):
                yield clip, usable[x], usable[y]

    def pair_summary(self):
        total = raw = har = anchor_pairs = 0
        for clip, x, y in self.pairs():
            total += 1
            anchor_pairs += clip in self.anchors
            raw += int(x['label']) == int(y['label'])
            hx = self.harmonize(x['reviewer'], x['label'])
            hy = self.harmonize(y['reviewer'], y['label'])
            har += hx is not None and hx == hy
        return {'total': total, 'raw': raw, 'harmonized': har,
                'anchor': anchor_pairs}

    def raw_pair_gap(self, left, right):
        gaps = []
        for clip, readings in self.by_clip.items():
            usable = self.usable(clip)
            if left in usable and right in usable:
                gaps.append(int(usable[left]['label']) - int(usable[right]['label']))
        return gaps

    # ---- 重叠 -------------------------------------------------------------
    def overlap(self):
        out = defaultdict(Counter)
        for clip, readings in self.by_clip.items():
            if clip in self.ambiguous:
                continue
            m = self.meta[clip]
            key = (m['category'], m['difficulty'], m['standard'])
            out[key]['clips'] += 1
            teams = {self.team[rev] for rev in readings}
            if len(teams) < 2:
                continue
            out[key]['cross_team'] += 1
            blind = [rev for rev, r in readings.items() if r['origin'] == 'blind']
            if len(blind) >= 2:
                out[key]['blind_pairs'] += 1
            a = self.anchors.get(clip)
            if a and a['source'] == 'blind' and len(blind) >= 2:
                out[key]['blind_anchor_pairs'] += 1
        return out

    # ---- 锚点证据 ---------------------------------------------------------
    def support(self, reviewer, category, standard=None, max_wave=None,
                asof=None, sources=('blind',)):
        rows = []
        for r in self.t['ratings']:
            if r['reviewer'] != reviewer or r['category'] != category:
                continue
            if r['clip'] in self.ambiguous:
                continue
            a = self.anchors.get(r['clip'])
            if not a or a['source'] not in sources:
                continue
            if r['origin'] != 'blind':
                continue
            if max_wave is not None and int(r['wave']) > max_wave:
                continue
            if asof is not None and int(a['known']) > asof:
                continue
            if standard and a['standard'] != standard:
                continue
            rows.append((r, a))
        return rows

    def summary(self, reviewer, rows):
        off = self.offsets.get(reviewer)
        if off is None or not rows:
            return None
        errs = [(int(r['label']) - off) % 3 - int(a['label']) for r, a in rows]
        n = len(errs)
        return {'n': n, 'mae': sum(abs(e) for e in errs) / n,
                'max': max(abs(e) for e in errs),
                'signed': sum(errs) / n,
                'errors': Counter(errs), 'ub': zero_error_ub(n)}

    def cross_partners(self, reviewer, category, rows):
        partners = set()
        for r, _ in rows:
            for other in self.usable(r['clip']):
                if other != reviewer and self.team[other] != self.team[reviewer]:
                    partners.add(other)
        return partners

    def qualification(self):
        out = {}
        for rev in sorted(self.people):
            for cat in CATEGORIES:
                rows = self.support(rev, cat)
                s = self.summary(rev, rows)
                v1 = sum(1 for _, a in rows if a['standard'] == 'v1')
                v2 = sum(1 for _, a in rows if a['standard'] == 'v2')
                hard_only = bool(rows) and all(
                    self.meta[r['clip']]['difficulty'] == 'hard' for r, _ in rows)
                out[(rev, cat)] = {
                    'rows': rows, 'n': 0 if not s else s['n'],
                    'v1': v1, 'v2': v2,
                    'mae': None if not s else s['mae'],
                    'ub': None if not s else s['ub'],
                    'hard_only': hard_only,
                    'partners': self.cross_partners(rev, cat, rows),
                }
        return out

    def direction_table(self):
        rows = []
        for rev in sorted(self.people):
            for cat in CATEGORIES:
                for wave in ('1', '2', '3'):
                    obs = [(r, a) for r, a in self.support(rev, cat)
                           if r['wave'] == wave]
                    s = self.summary(rev, obs)
                    if s:
                        rows.append((rev, cat, wave, s))
        return rows

    # ---- 决定复核 ---------------------------------------------------------
    RULES = (
        ('指定盲测 v2 / 波次内', {'sources': ('blind',), 'wave': True, 'standard': 'v2'}),
        ('指定盲测 v1 / 波次内', {'sources': ('blind',), 'wave': True, 'standard': 'v1'}),
        ('跨版本合并 / 波次内', {'sources': ('blind',), 'wave': True, 'standard': None}),
        ('含仲裁锚点（仍只计盲判）/ 波次内', {'sources': ('blind', 'appeal'), 'wave': True, 'standard': None}),
        ('指定盲测 v2 / 已知日', {'sources': ('blind',), 'wave': False, 'standard': 'v2'}),
    )

    def decision_rows(self, d, exclude_clip=None):
        out = []
        for label, opt in self.RULES:
            rows = self.support(
                d['reviewer'], d['category'], standard=opt['standard'],
                max_wave=int(d['wave']) if opt['wave'] else None,
                asof=int(d['asof']), sources=opt['sources'])
            if exclude_clip:
                rows = [(r, a) for r, a in rows if r['clip'] != exclude_clip]
            s = self.summary(d['reviewer'], rows)
            out.append((label, rows, s))
        return out

    # ---- 队列与会签 -------------------------------------------------------
    def readers(self, clip):
        return set(self.usable(clip))

    def countersigners(self, category, clip, author, standard='v2',
                       min_anchors=4, max_error=0.25):
        good, blocked = [], []
        for rev, p in sorted(self.people.items()):
            reasons = []
            if rev == author:
                reasons.append('作者')
            if rev in self.readers(clip):
                reasons.append('参与原判')
            if not int(p['can_countersign']):
                reasons.append('无会签许可')
            if int(p['available']) <= 0:
                reasons.append('件数已满')
            rows = self.support(rev, category, standard=standard)
            s = self.summary(rev, rows)
            if not s or s['n'] < min_anchors:
                reasons.append(f'锚点{0 if not s else s["n"]}')
            elif s['mae'] > max_error:
                reasons.append('误差超限')
            shifts = [sh for (r, sh) in self.minutes if r == rev]
            if not shifts:
                reasons.append('无班次')
            row = {'reviewer': rev, 'team': p['team'],
                   'n': 0 if not s else s['n'],
                   'mae': None if not s else s['mae'],
                   'shifts': shifts, 'reasons': reasons}
            if reasons:
                blocked.append(row)
            else:
                good.append(row)
        return good, blocked

    # ---- 补盲 -------------------------------------------------------------
    def knowledge(self, clip):
        return {h['team']: (int(h['date']), h['knowledge'])
                for h in self.history.get(clip, [])}

    def consumed(self, clip):
        return (clip in self.booked and self.booked[clip]['state'] == 'reviewed') \
            or bool(self.returns.get(clip))

    def bridge_plan(self):
        plan = []
        for clip, inv in sorted(self.inventory.items()):
            eligible = groups(inv['eligible_groups'])
            known = self.knowledge(clip)
            dirty = {t: known[t] for t in eligible if t in known}
            used = self.consumed(clip)
            options = []
            for shift in sorted(self.day):
                if int(inv['allowed_from']) > self.day[shift]:
                    continue
                pool = [rev for rev in self.people
                        if self.minutes.get((rev, shift), 0) >= int(inv['minutes'])
                        and int(self.people[rev]['available']) > 0]
                by_team = defaultdict(list)
                for rev in pool:
                    by_team[self.team[rev]].append(rev)
                for t1, t2 in itertools.combinations(eligible, 2):
                    for r1 in by_team.get(t1, []):
                        for r2 in by_team.get(t2, []):
                            options.append({'shift': shift, 'pair': (r1, r2),
                                            'teams': (t1, t2)})
            plan.append({'clip': clip, 'inv': inv, 'eligible': eligible,
                         'dirty': dirty, 'used': used, 'options': options})
        return plan

    def roster_vs_work(self):
        out = []
        for rev in sorted(self.people):
            seen = Counter(r['category'] for r in self.t['ratings']
                           if r['reviewer'] == rev and r['origin'] != 'unblinded')
            out.append((rev, self.team[rev], self.roster[rev], seen))
        return out

    def quality_flags(self):
        flags = []
        for clip, readings in self.by_clip.items():
            for field in ('wave', 'category', 'difficulty', 'standard'):
                values = {r[field] for r in readings.values()}
                if len(values) > 1:
                    flags.append((clip, field, sorted(values)))
        return flags


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def harmonized_mean(c, rows):
    vals = [c.harmonize(r['reviewer'], r['label']) for r in rows]
    vals = [v for v in vals if v is not None]
    return None if not vals else sum(vals) / len(vals)


THRESHOLD = {'claims': 0.28, 'context': 0.23, 'service': 0.25}


def render(c: Corpus) -> str:
    out = []
    add = out.append

    usable_readings = [r for r in c.t['ratings'] if r['origin'] != 'unblinded']
    unblinded = [r for r in c.t['ratings'] if r['origin'] == 'unblinded']
    pairs = c.pair_summary()
    origins = Counter(r['origin'] for r in c.t['ratings'])
    pair_origins = Counter()
    for _, x, y in c.pairs():
        pair_origins[tuple(sorted((x['origin'], y['origin'])))] += 1
    blind_anchors = [a for a in c.anchors.values() if a['source'] == 'blind']
    appeal_anchors = [a for a in c.anchors.values() if a['source'] == 'appeal']

    add('# 直播内容定性核对：尺度可比性、复核资质与会签/补盲安排')
    add('')
    add('> 依据：`data/` 下 13 张登记表（合成业务记录）。'
        '本报告由 `qc_analysis.py` 只读生成，不改写任何原始资料。')
    add('> 文中“归一后”均指先按判读员的循环编码约定换算回同一标尺；'
        '原始标签仍逐条保留，可审计。')
    add('')

    # ---------------- 0 摘要 ----------------
    add('## 0. 结论摘要')
    add('')
    add('1. **“甲组判得重”不成立，先要归一编码约定。** 原始标签的差是逐人的'
        '循环编码偏移（0/1），不是严宽。归一后同一片段的两读一致 '
        f"{pairs['harmonized']}/{pairs['total']}，不归一时 {pairs['raw']}/{pairs['total']}。"
        '原始差只在 `+1` 与 `-2` 之间环绕，任何单向的“更严/更宽”都产生不了环绕。'
        '**因此不用原始标签均值、原始处罚比作能力评分。**')
    add(f'2. **本期可作独立验证的锚点只有 {len(blind_anchors)} 个指定盲测片段，'
        '且全部是困难样本。** 另有 '
        f'{len(appeal_anchors)} 个仲裁锚点，但它们片段的原判都来自 `work`，'
        '又集中在此前争议最多的 claims，不能反过来给原判做独立验证，'
        '也不能外推为整期标准。')
    add('3. **可连接范围：**claims/service 的困难组有跨队盲测锚点桥；'
        'claims/service 的一般组只有双盲配对、**没有外部锚点**；'
        'context 几乎只在 alpha 队内可比，跨队锚点桥仅 3 个困难片段；'
        'neutral（c1）没有判读记录，与任何队都不连通。')
    add('4. **单签/双签：**a1、b1 可在 claims、service 的**困难样本**继续单签'
        '（点估计零误差，但 95% 上界仍高于决定门槛，属有条件）；'
        'a2、a3 的 context 必须双签；**所有一般难度一律双签**；'
        'b2、b3、c1 目前无该类目资格。')
    add('5. **待签件：**k5 改由 b1 在 s2 会签；k2 现有 b3 会签不具资格，改由 a1 会签；'
        'k1、k4 本班次找不到“有资格且未参与原判”的会签人，暂缓、不先恢复单签；'
        'k3 已执行，只按复议 `evidence` 范围复核，证据与锚点一致，不改判、不批量转误判。')
    add('6. **补盲：**干净且班次可行的成对盲测只剩 **p7**（v2）与 **p6**（v1）。'
        '其中 v2 的只有 p7（service/一般/v2，alpha+neutral，s2）；'
        'p6 是旧标准（v1），只改善历史可比性、不提高现行资格。'
        'context 的 v2 缺口无干净片段可补；p1/p3/p5 有队提前见过，'
        'p8 泄露 beta，p2 已阅不可重排。p8 泄盲后释放 z1 的 s2 时段，改排 p7。')
    add('7. **不确定性：**现有独立案例数下，零误差的 95% 上界约 0.22–0.95；'
        '“全对”不等于“已保证误差 ≤0.25”。决定门槛与资格结论都标出这一差距。')
    add('')

    # ---------------- 1 分层 ----------------
    add('## 1. 实际片段、重复判读与揭盲后复看分开')
    add('')
    comparable_clips = len(c.by_clip) - len(c.ambiguous)
    comparable_readings = sum(len(c.usable(x)) for x in c.by_clip)
    add(f'* **实际片段**：{len(c.by_clip)} 个；判读记录 {len(c.t["ratings"])} 条。'
        f'剔除揭盲复看与登记冲突片段（{"、".join(sorted(c.ambiguous))}）后，'
        f'进入比较的片段 {comparable_clips} 个、非揭盲判读 {comparable_readings} 条；'
        f'锚点 {len(c.anchors)} 个片段（仲裁锚点只确认标签）。')
    repeats = sum(max(0, len(c.usable(clip)) - 1) for clip in c.by_clip)
    add(f'* **同一片段的重复判读**：{repeats} 条'
        '（多数片段由两名判读员各判一次）。两读可构成一对独立判读，'
        '但**一个片段只算一个案例**：不能把两读当成两份互不相关的验证证据，'
        '也不能用它把样本量翻倍。')
    add(f'* **已揭盲后复看**：{len(unblinded)} 条，已从锚点、配对与资格证据全部剔除。')
    for r in unblinded:
        a = c.anchors.get(r['clip'])
        add(f"  * `{r['clip']}` / {r['reviewer']}（`known={r['known']}`，"
            f"登记难度 `{r['difficulty']}`）归一后判读 "
            f"`{c.harmonize(r['reviewer'], r['label'])}`，锚点 "
            f"`{a['label'] if a else '无'}`。获知后的复看只作参考。")
    add('')
    add('按来源拆分：')
    add('')
    add('| 来源 | 条数 | 用途 |')
    add('| --- | --- | --- |')
    add(f"| 盲判 `blind` | {origins['blind']} | 可作独立判读；只有盲-盲配对能当验证 |")
    add(f"| 工作记录 `work` | {origins['work']} | 原始业务判断；不能当盲判验证 |")
    add(f"| 揭盲后复看 `unblinded` | {origins['unblinded']} | 全部剔除 |")
    add('')
    add('配对按来源拆分（两读来源永远相同，因此不存在“盲判 vs 工作记录”的混合对）：')
    add('')
    add('| 配对类型 | 片段数 | 归一后一致 | 可作独立验证 |')
    add('| --- | --- | --- | --- |')
    for key in (('blind', 'blind'), ('work', 'work')):
        n = pair_origins.get(key, 0)
        agree = sum(
            1 for _, x, y in c.pairs()
            if tuple(sorted((x['origin'], y['origin']))) == key
            and c.harmonize(x['reviewer'], x['label'])
            == c.harmonize(y['reviewer'], y['label']))
        independent = '是' if key == ('blind', 'blind') else '否（非盲判）'
        add(f'| {key[0]}-{key[1]} | {n} | {agree}/{n} | {independent} |')
    add('')
    add(f"**关键：只有 {pair_origins.get(('blind', 'blind'), 0)} 个盲-盲配对能充当独立验证；"
        f"{pair_origins.get(('work', 'work'), 0)} 个工作记录配对是原始业务判断，"
        '虽也与锚点一致，但属于“原判 vs 原判”，不独立。')
    add('')

    # ---------------- 2 编码 ----------------
    add('## 2. 先归一编码约定，再谈尺度')
    add('')
    add(f'在 {len(blind_anchors)} 个指定盲测锚点上，逐人穷举模 3 偏移 `k`，'
        '取使残差 `(label-k) mod 3 - 锚点` 绝对值最小的唯一解：')
    add('')
    rows = []
    for rev in sorted(c.people):
        d = c.convention.get(rev, {'n': 0})
        if not d['n']:
            rows.append([rev, c.team[rev], '—', 0, '—', '—', '—', '无锚点'])
            continue
        rows.append([rev, c.team[rev], d['offset'], d['n'], d['exact'],
                     fmt(d['raw_mae']), d['margin'],
                     '唯一解' if d['margin'] > 0 else '不唯一'])
    out += md_table(['判读员', '团队', '偏移 k', '锚点 n', '归一后精确',
                     '未归一 MAE', '与次优差距', '可识别性'], rows)
    add('')
    add('* 归一后每个判读员在全部盲测锚点上的残差都是 0；'
        '留出第 3 波复验、以及在仲裁片段上同一判读员的一致性同样为 0'
        '（后者只说明换算自洽，不作独立验证）。')
    add(f'* 全部 {pairs["total"]} 对同片段判读，归一后一致 **{pairs["harmonized"]}**，'
        f'不归一只有 **{pairs["raw"]}**。原始差的取值只在 `+1` 与 `-2` 之间，'
        '是同一个模 3 差。')
    add('')
    add('**同方向的反例更直观（原始均值 vs 归一）：**')
    add('')
    srows = []
    for cat in ('claims', 'service'):
        for team in ('alpha', 'beta'):
            tr = [r for r in c.t['ratings']
                  if r['category'] == cat and c.team[r['reviewer']] == team
                  and r['origin'] != 'unblinded' and r['clip'] not in c.ambiguous]
            raw = sum(int(r['label']) for r in tr) / len(tr)
            srows.append([cat, team, len(tr), fmt(raw, 2), fmt(harmonized_mean(c, tr), 2)])
    out += md_table(['类目', '团队', 'n', '原始均值', '归一后均值'], srows)
    add('')
    add('claims 的原始图形是“beta 更严”，service 却是“alpha 更严”——方向互相颠倒；'
        '归一后两组完全相同。这说明原始差是编码约定，不是尺度。'
        '归一只是编码换算，不修改原记录。')
    add('')

    # ---------------- 3 锚点 ----------------
    add('## 3. 本期有效锚点与适用面')
    add('')
    add(f'* **指定盲测锚点 {len(blind_anchors)} 个**：v1 有 12 个（全在第 1 波），'
        'v2 有 24 个（第 2、3 波各 12）。每个片段两名判读员盲读，可作独立验证。')
    add(f'* **仲裁锚点 {len(appeal_anchors)} 个**：全部在 claims，'
        '每波 2 个。它们确定这些争议片段的最终标签，'
        '但片段上的判读来源都是 `work`，正是被仲裁的原判，**不独立**。')
    add('* **全部 42 个锚点都是困难样本。** 一般难度没有任何外部真值。')
    add('')
    arows = []
    for cat in CATEGORIES:
        bc = [a for a in blind_anchors if c.meta[a['clip']]['category'] == cat]
        ac = [a for a in appeal_anchors if c.meta[a['clip']]['category'] == cat]
        arows.append([cat, len(bc), len(ac),
                      ', '.join(sorted({a['standard'] for a in bc})) or '—',
                      'hard'])
    out += md_table(['类目', '盲测锚点', '仲裁锚点', '盲测覆盖版本', '难度'], arows)
    add('')
    add('仲裁锚点不能当独立验证的理由有两层：')
    add('')
    add('1. **同源。** 这些片段的判读就是被仲裁的工作记录，'
        '拿它与仲裁标签比对，是同一份材料同时充当“被检验判断”和“验证证据”。')
    add('2. **选择性。** 它们是争议最多的少数片段，不代表整期分布；'
        '把“仲裁后改对”当作全期尺度标准，会系统性高估一致性。')
    add('')
    add('本期在仲裁片段上没有其他判读员的盲判记录，因此仲裁锚点对'
        '“谁判得准”的独立信息量为 0；它们只用于确认这些片段的最终标签。')
    add('')

    # ---------------- 4 可连接范围 ----------------
    add('## 4. 可连接的比较范围、误判方向与不确定性')
    add('')
    ov = c.overlap()
    orows = []
    for cat in CATEGORIES:
        for dif in DIFFICULTIES:
            for ver in VERSIONS:
                v = ov.get((cat, dif, ver))
                if not v:
                    continue
                ap = v.get('blind_anchor_pairs', 0)
                if ap >= 4:
                    verdict = '可连接（跨队锚点）'
                elif ap >= 2:
                    verdict = '有限连接'
                elif v.get('blind_pairs', 0) >= 2:
                    verdict = '仅配对一致（无锚点）'
                else:
                    verdict = '不可连接'
                orows.append([cat, dif, ver, v['clips'], v.get('cross_team', 0),
                              v.get('blind_pairs', 0), ap, verdict])
    out += md_table(['类目', '难度', '版本', '片段', '跨队片段', '双盲配对',
                     '跨队锚点配对', '可连接性'], orows)
    add('')
    add('**读法：**“跨队锚点配对”才同时具备跨团队与外部真值；'
        '只有“双盲配对”时两人可能一起偏，无法判断绝对尺度；'
        '“跨队片段”但配对是 `work-work` 的，也不能当独立连接。')
    add('')
    add('* claims/service 困难组：a1↔b1、a2↔b2 均有跨队锚点桥，可连接。')
    add('* claims/service 一般组：v2 各有 8 个跨队双盲配对，'
        '但**锚点为 0**，只能证明彼此一致。')
    add('* context：v1 困难仅 1 个、v2 困难 2 个跨队锚点配对；'
        '一般难度 v2 只有 1 个双盲配对、0 个锚点。'
        'a3 的 9 个锚点全部只与同队的 a2 配对，**从未跨团队**。')
    add('* neutral（c1）无判读，完全未连接。')
    add('')
    add('**误判方向（按类目×波次，归一后）：**')
    add('')
    dmap = {(rev, cat): {} for rev in c.people for cat in CATEGORIES}
    for rev, cat, wave, s in c.direction_table():
        dmap[(rev, cat)][wave] = s
    drows = []
    for rev in sorted(c.people):
        for cat in CATEGORIES:
            waves = dmap[(rev, cat)]
            if not waves:
                continue
            cells = []
            total = 0
            for wave in ('1', '2', '3'):
                s = waves.get(wave)
                if not s:
                    cells.append('—')
                else:
                    total += s['n']
                    cells.append(f"{s['n']} 例/" +
                                 '、'.join(f'{k}×{v}' for k, v in sorted(s['errors'].items())))
            signed = [s['signed'] for s in waves.values()]
            drows.append([rev, cat, *cells, total,
                          '+0.00' if all(abs(x) < 1e-9 for x in signed) else '见分布'])
    out += md_table(['判读员', '类目', '第1波', '第2波', '第3波', '合计', '平均方向'], drows)
    add('')
    add('有锚点的格子全部是 0 例偏差，**在困难样本上检不出方向性误判**。'
        '这不代表一般难度也没问题：一般难度没有锚点，方向不可判定。')
    add('')
    add('**零误差不等于高精度。** n 个独立案例零误差时，'
        '真实错误率的 95% 上界为 `1-0.05^(1/n)`：')
    add('')
    add('| 独立案例 n | 零误差 95% 上界 |')
    add('| --- | --- |')
    for n in (2, 3, 4, 6, 7, 8, 9, 10, 12, 17, 19):
        add(f'| {n} | {zero_error_ub(n):.3f} |')
    add('')
    add('注意 n 是**独立片段数**，不是判读条数。'
        '“6 个锚点全对”只说明真实错误率大概率不超过 0.39，'
        '达不到 d1 的 0.28；这一点在第 5、6 节逐项标注。')
    add('')

    # ---------------- 5 资格 ----------------
    add('## 5. 单签 / 双签名单')
    add('')
    add('判据分两层：')
    add('')
    add('* **操作层**：同类目 v2 盲测锚点 ≥4、归一后点估计 MAE ≤0.25、'
        '且有至少 1 个跨队锚点搭档。满足即可在**已覆盖的困难样本**继续单签。')
    add(f'* **统计层**：零误差 95% 上界 ≤ 本类目决定门槛'
        f'（claims {THRESHOLD["claims"]}、context {THRESHOLD["context"]}、'
        f'service {THRESHOLD["service"]}）。本期只有极少数格子满足。')
    add('')
    add('一般难度全期无锚点，无论点在哪个格，**一般难度一律双签**。')
    add('')
    qual = c.qualification()
    qrows = []
    for rev in sorted(c.people):
        for cat in CATEGORIES:
            q = qual[(rev, cat)]
            if q['n'] == 0:
                continue
            v2 = c.support(rev, cat, standard='v2')
            s2 = c.summary(rev, v2)
            cross_n = len({r['clip'] for r, _ in q['rows']
                           if len({c.team[x] for x in c.usable(r['clip'])}) > 1})
            partners = ', '.join(sorted(q['partners'])) or '无'
            ub = None if not s2 else s2['ub']
            reliable = q['v2'] >= 4 and q['mae'] is not None and q['mae'] <= 0.25
            if reliable and cross_n >= 4:
                if ub is not None and ub <= THRESHOLD[cat]:
                    verdict = '可单签（困难，达统计门槛）'
                else:
                    verdict = '可单签（困难，上界偏高）'
            elif reliable and cross_n >= 1:
                verdict = '跨队桥过薄→双签'
            elif reliable:
                verdict = '仅队内配对→双签'
            elif q['n'] >= 2:
                verdict = '证据不足→双签'
            else:
                verdict = '无资格→双签'
            qrows.append([rev, c.team[rev], cat, q['n'], f"{q['v1']}/{q['v2']}",
                          fmt(q['mae']), fmt(ub), cross_n, partners, verdict])
    out += md_table(['判读员', '团队', '类目', '锚点 n', 'v1/v2', '点估计 MAE',
                     'v2 零误差 95% 上界', '跨队锚点片段', '跨队搭档', '结论'], qrows)
    add('')
    add('**可直接单签（仅困难样本）：**')
    add('* **a1**：claims（v2 锚点 6、跨队 9）、service（v2 锚点 7、跨队 10），'
        '点估计零误差，与 b1 有充分跨队锚点互证。')
    add('* **b1**：claims（6、9）、service（7、10），同上。')
    add('* 但两人的 95% 上界（0.35–0.39）仍高于决定门槛（0.25–0.28），'
        '所以是**有条件单签**：困难样本可单签，出现新类型或一般难度必须双签。')
    add('')
    add('**必须双人会签：**')
    add('* **a2 / a3 的 context**：a2 的 v2 锚点 8、点估计零误差，'
        '但 v2 零误差 95% 上界为 0.31（跨版本合并 12 例时为 0.22），'
        '且跨队锚点片段只有 3 个（搭档 b2 本身 v2 仅 2 个），桥太薄；'
        'a3 的 v2 锚点 6，上界 0.39，且 9 个锚点**全部只与同队 a2** 配对，'
        '从未跨团队。两人都不满足独立单签。')
    add('* **一切一般难度**：claims/service 的一般组没有锚点；'
        'context 一般组连跨队双盲配对都只有 1 个。')
    add('')
    add('**无该类目资格：** b2（claims v2=2、context v2=2、service v2=1，均 <4）；'
        'a2 的 service（v2=1）、a1/b1 的 context（0）；b3、c1（无任何判读）。')
    add('')
    add('**任务分层要按实际做过的类目，不能只看名册：**')
    add('')
    rrows = []
    for rev, team, roster, seen in c.roster_vs_work():
        rrows.append([rev, team, roster,
                      ', '.join(f'{k}:{v}' for k, v in sorted(seen.items())) or '无记录'])
    out += md_table(['判读员', '团队', '名册类目', '实际非揭盲判读'], rrows)
    add('')
    add('a1 名册 claims 但实际做 claims+service；a3 名册 service 却**只做过 context**；'
        'a2、b2 名册 single 类目但三类都做。资格与排班必须按实际证据，'
        '否则会出现“名册上有 service、实际零 service 锚点”的错配（见 k5/d3）。')
    add('')

    # ---------------- 6 待签件 ----------------
    add('## 6. 待签件逐件处置')
    add('')
    add('规则：单签资格受限时，尚未生效的结论若只靠其签字，'
        '须由**有相应资格且未参与原判**的人会签；'
        '不因人员状态批量改判；已执行只按复议明示范围重开。')
    add('')
    for case, q in c.queue.items():
        a = c.anchors.get(q['clip'])
        readers = sorted(c.readers(q['clip']))
        d = c.decisions[q['decision']]
        add(f"### {case} · `{q['clip']}` · {q['category']} · {q['state']}")
        add('')
        readings = '；'.join(
            f"{rev}={'原判' if r['origin'] != 'blind' else '盲判'}→"
            f"{c.harmonize(rev, r['label'])}" for rev, r in c.by_clip[q['clip']].items())
        add(f"* 承办 `{q['author']}`；现有会签 `{q['second'] or '无'}`；"
            f"锚点 `{a['label'] if a else '无'}`（{a['source'] if a else '—'}）；"
            f"归一后各读：{readings}。")
        if q['author'] not in readers:
            add(f"* **承办人不在该片段的判读记录中**（实际判读为 "
                f"{'、'.join(readers)}），承办与“原判”须分开核对。")
        if case in c.appeals:
            ap = c.appeals[case]
            add(f"* 复议：范围 `{ap['scope']}`，处置 `{ap['disposition']}`。")
        add(f"* 依据决定 {d['id']}：{d['category']}，波次 ≤{d['wave']}，"
            f"asof {d['asof']}，锚点下限 {d['min_anchors']}，"
            f"误差上限 {d['max_error']}，范围 `{d['scope']}`。")
        good, blocked = c.countersigners(q['category'], q['clip'], q['author'])
        if good:
            add('* 合格会签候选：' + '；'.join(
                f"**{g['reviewer']}**（{g['team']}，v2 锚点 {g['n']}，"
                f"班次 {'/'.join(g['shifts']) or '无'}）" for g in good))
        else:
            add('* 合格会签候选：**无**。')
        add('')
    add('**逐件结论：**')
    add('')
    add('* **k5（service，pending）**：承办 a3 的 service 单签**零 service 锚点**'
        '（其 38 条判读全是 context），且 **a3 本人从未判读过 c100**，d3 不成立；'
        '但底层 a2、b2 两条盲判归一后均与锚点 2 一致。'
        '故只需补合规会签、不改判：由 **b1** 在 s2 会签（3 分钟）。'
        'a1 虽也合格，但 s2 已排 p7 盲测，不能再兼会签。')
    add('* **k4（context，pending）**：d2 为 `joint`，a2 不得单签；'
        'a3 虽有点估计零误差的 context 锚点，但**全与 a2 同队、且本班次无排班**；'
        'b2 是 c065 原判参与者；c1 无证据。'
        '**本班次无可合规会签人，暂缓生效，不先恢复单签。**')
    add('* **k2（claims，countersigned）**：承办 b1 **本人不在 c060 的判读记录中**'
        '（该片段两读为 a2、b2）；现有会签 b3 又**零锚点、零判读**，'
        '两人都不构成对 c060 的有效签署。改由 **a1**（claims v2 锚点 6、'
        '未参与 c060）会签，b1 的承办与底层盲判保留，不作废、不改判。')
    add('* **k1（claims，pending）**：c054 是**一般难度**，全期一般难度无锚点；'
        '本类目合格且非承办者的会签人只有 a1，而 a1 恰好是 c054 原判参与者，'
        '必须回避（b1 既是承办也是原判）。'
        '**本班次无法合规生效，按个案升级；不得先恢复单签。**')
    add('* **k3（claims，executed）**：复议范围仅 `evidence`。'
        'c072 锚点为 2，a1、b1 归一后均为 2，证据复核与锚点一致。'
        '**不改判、不扩大重开范围、不因原始标签差异批量转误判。**'
        '若制度要求未参与者正式复核，claims 只有 a1/b1 有资格，而两人都参与原判，'
        '须先补第三人。')
    add('* k2 的复议范围是 `wording`、处置 `keep`，不触发重开。')
    add('')
    add('**决定本身的证据检查（不是结论，而是“决定当时能否被证据支持”）：**')
    add('')
    dres = []
    for did, d in c.decisions.items():
        for label, rows, s in c.decision_rows(d):
            dres.append([did, d['reviewer'], d['category'], label,
                         0 if not s else s['n'],
                         fmt(None if not s else s['mae']),
                         fmt(None if not s else s['ub']),
                         d['min_anchors'], d['max_error']])
    out += md_table(['决定', '判读员', '类目', '证据口径', 'n', 'MAE',
                     '95% 上界', '锚点下限', '误差上限'], dres)
    add('')
    add('* **d1（b1，claims，min 4）**：纯 v2 只有 3 个锚点，**不满足下限**；'
        '只有把 v1 与 v2 合并（6 个）或计入仲裁口径才达到 4。'
        '合并可辩，但必须在决定里明示“跨版本合并”，否则单签资格的依据不成立。'
        '即使合并，95% 上界 0.39 仍高于 0.28。')
    add('* **d2（a2，context，min 4）**：纯 v2 恰有 4 个，满足下限；'
        '上界 0.53（4 例）或 0.31（合并）**都高于 0.23**，'
        '所以“点估计零误差”不足以支撑 0.23 的误差承诺。')
    add('* **d3（a3，service，min 4）**：service 锚点 **0 个**，任何口径都不满足下限。')
    add('')

    # ---------------- 7 补盲 ----------------
    add('## 7. 下一轮成对盲测与班次安排')
    add('')
    add('桥接成立需同时满足：两队都未提前得知答案、能按同一版本独立判读、'
        '片段未被用尽。逐片段核查：')
    add('')
    plan = c.bridge_plan()
    brows = []
    for p in plan:
        inv = p['inv']
        state = []
        if p['used']:
            state.append('已阅/已返回，不可重排')
        if p['dirty']:
            state.append('已见：' + '、'.join(
                f'{t}({d[1]},{d[0]}日)' for t, d in sorted(p['dirty'].items())))
        if not state:
            state.append('干净')
        if p['used'] or p['dirty']:
            feas = '桥接不成立（不列入可用计划）'
        else:
            feas = '、'.join(f"{o['shift']}:{o['pair'][0]}+{o['pair'][1]}"
                            for o in p['options']) or '无可行成对班次'
        brows.append([p['clip'], inv['category'], inv['difficulty'],
                      inv['standard'], inv['eligible_groups'], inv['minutes'],
                      inv['allowed_from'], '；'.join(state), feas])
    out += md_table(['片段', '类目', '难度', '版本', '可用组', '分钟', '起用日',
                     '状态', '可行成对排班'], brows)
    add('')
    add('**结论：**')
    add('')
    add('* **p7 是本期唯一干净、班次可行的 v2 成对盲测**：'
        'service/一般/v2，alpha+neutral，s2 由 a1 与 c1 各用 2 分钟。'
        '它把从未连接的 neutral 经 a1 接入同一标尺，'
        '并首次给出涉及 neutral 的 service 一般难度独立读数；'
        '注意它只有 1 个片段，不能单独支撑“该层已校准”。')
    add('* **p2** 已阅（z2 reviewed）并已返回，不能撤销重排；'
        '其两读归一后同为 1，只能作回顾性补充。'
        '两条返回的 `known` 分别是 17、18，独立判读是否成立缺少登记依据，'
        '且片段已用尽，**不得算作新一轮独立桥接**。')
    add('* **p1/p3/p5** 分别有 alpha/neutral/beta 提前见过，'
        '不满足“两队都不得提前得知”，本期不用。')
    add('* **p8** 预约后泄露 beta（第 15 日）；唯一未阅的 z1（a1+b1，s2）'
        '应**释放时段**。若改用 alpha+neutral，c1 在 s2 只有 4 分钟 < 5，'
        '本班次不可行。')
    add('* **p4** 需 neutral 5 分钟，c1 最多 4 分钟，本班次不可行。')
    add('* **p6** 是 v1 context 困难，只对旧标准历史可比性有用。'
        '它能新增 1 个 context/v1/困难 的跨队独立配对（现仅 1 个），'
        '但**不产生新的外部锚点**，因此只改善可比性、不提升任何人资格，'
        '且**不得并入 v2 结论**。')
    add('* **context 的 v2 缺口本期无法补**：唯一的 v2 context 片段 p5 已被 beta 见过。')
    add('')
    add('**s2 班次账本（释放 z1 后重排）：**')
    add('')
    add('| 人员 | 团队 | s2 分钟 | 安排 | 剩余 |')
    add('| --- | --- | --- | --- | --- |')
    add('| a1 | alpha | 4 | p7 盲测 2（**不兼会签**） | 2 |')
    add('| b1 | beta | 4 | k5 会签 3 | 1 |')
    add('| c1 | neutral | 4 | p7 盲测 2 | 2 |')
    add('| b3 | beta | 3 | 不安排（无资格） | 3 |')
    add('')
    add('* z1 释放后 a1、b1 的 s2 时段回到池中；上表已按释放后重排。')
    add('* **a1 不再兼 k5**：同一时段既排盲测又承诺会签，会让两项都不可靠；'
        'k5 由 b1 承担。')
    add('* **k2 的重新会签可排 s1**：a1 在 s1 有 5 分钟空档，'
        '与 s2 的 p7 盲测不冲突；但 `case_load` 未登记 k2 的会签时长，'
        '排班前需补登记并按实际时长核对。')
    add('* **本班次可处理 2 件待签**：k5 会签（b1，s2）与 k2 重新会签（a1，s1）；'
        'k1、k4 因无合格会签人不能分派。')
    add('')
    add('**单个片段连编码约定都识别不了。** 每人的标签可差一个模 3 偏移，'
        '3 个级别恰好有 3 种可能；只有 1 个共读片段时，'
        '“一致”与“约定的偏移差正好抵消”无法区分。'
        '因此 p7 对 c1 最多给出一次一致性观察，**不能据此确定 c1 的标尺**，'
        '更不能据此恢复 c1 的单签。要识别一队，至少需要 **≥2 个同层共读片段**；'
        '要同时分离人员差异与案例差异，还需要同一对判读员在不同难度上重复。')
    add('')
    add('**能否区分人员差异与案例差异：**每个待补层最多 1 个干净片段，'
        '单个共享片段会把“人员差异”与“案例难度差异”混在一起，无法分离。'
        '当前库存不满足“≥2 个同层共读片段”。'
        '**不允许用彼此不相交的案例宣称已校准**：'
        '那只能说明各自看过一些片段，不能说明两队在共同难度上对齐。')
    add('')

    # ---------------- 8 泄露影响 ----------------
    add('## 8. 若补测提前泄露：受影响部分分开看')
    add('')
    add('以 p8 泄露 beta 与 b2-claims 包泄露为例，主管应按三块分别处理：')
    add('')
    add('| 受影响块 | 具体范围 | 处理 |')
    add('| --- | --- | --- |')
    add('| 尺度比较 | p8 的 alpha↔beta 桥作废；'
        'b2-claims 在泄露日（13）后不得再由 beta 复用于桥接 | '
        '已有 `known=8` 的判读早于泄露，保留；只拒绝新增 beta 读数 |')
    add('| 资质 | 无新增 beta 资格；beta 的 claims 资格仍只靠既有 6 个 v2 锚点 | '
        '不因一次泄露撤销既有资格，也不据此新增 |')
    add('| 待签件 | z1（p8，未阅）释放时段；已阅 z2 不受影响 | '
        'k1/k4 不因泄露获得新会签人 |')
    add('')
    add('可单独保留的部分：a1/b1 在 claims、service 困难样本上的既有独立会签与锚点证据'
        '不受 p8 影响；p7 与 p8 无关，照常安排。'
        '下一班应补的是 service 一般难度（p7）与 context 的 v1 跨队配对（p6），'
        '不是重复同一张熟悉题卷。')
    add('')

    # ---------------- 9 预登记 ----------------
    add('## 9. 预登记：若补测结果仍不足，哪些原判断不改变')
    add('')
    add('在补测开始前先固定以下清单，避免事后按结果挑解释：')
    add('')
    add('1. **不改判已执行件**：k3 只在 `evidence` 范围内复核，'
        '不因补测不足扩大范围、不改成误判。')
    add('2. **不恢复单签**：p7 结果返回前，a2/a3 的 context 仍双签，'
        '一般难度仍双签；不得以“已排盲测”为由临时恢复。')
    add('3. **不改变困难样本的既有资格**：a1、b1 的 claims/service 困难单签'
        '有 6–7 个跨队锚点支撑，补测不足不影响这部分。')
    add('4. **不提升 b2/b3/c1**：b2 锚点 <4，b3/c1 无判读；'
        '即使 p7 让 c1 与 a1 配对成功，单片段既识别不了 c1 的编码约定，'
        '也不足以建立其资格。')
    add('5. **不把一般难度标为已校准**：p7 只补一致性与 neutral 连接，'
        '不产生一般难度外部锚点。')
    add('6. **不做跨版本合并**：p6（v1）结果只用于 v1 历史可比性，不并入 v2。')
    add('7. **个案不批量**：k1、k4 仍按个案升级，不因同批人员状态批量改判。')
    add('8. **不追溯扩大仲裁锚点**：6 个仲裁片段仍只作标签确认，'
        '不升级为全期标准。')
    add('')

    # ---------------- 10 数据质量 ----------------
    add('## 10. 数据质量与需人工确认的事项')
    add('')
    for clip, field, values in c.quality_flags():
        add(f'* `{clip}` 同片段 `{field}` 登记不一致：{values}。'
            '该片段**已整体排除**出分层统计、锚点与配对，需回查原始登记。')
    add('* 名册类目与实际判读不一致（第 5 节），资格以实际证据为准。')
    add('* `b3`、`c1` 有会签许可但**零判读记录**；b3 已被用于 k2 会签，'
        '属无证据签署，需纠正。')
    add('* `blind_returns` 的 p2 两条返回 `known` 不同（17 / 18），'
        '无法证明两次判读独立；且 p2 已阅，按不可重排处理。')
    add('* 仲裁锚点的原判来源都是 `work`，不能作为独立验证证据（第 3 节）。')
    add('* `pack_events` 的 b2-claims 泄露日（13）晚于该包全部判读的 `known=8`，'
        '已有数据不受污染；但泄露后 beta 不得再用于桥接。')
    add('')

    # ---------------- 11 方法 ----------------
    add('## 11. 方法与复现')
    add('')
    add('* 读入 `data/*.csv`，按 README 的列契约解析；空字段不当作 0。')
    add('* **编码归一**：在 `origin=blind`、锚点 `source=blind`、波次与版本一致的片段上，'
        '逐人选择唯一使残差最小的模 3 偏移。归一只是换算，不改原记录。')
    add('* **证据口径**：只计 `origin=blind`；重复判读按片段计一次；'
        '揭盲复看与登记冲突片段剔除。仲裁锚点只用于标签确认，不作独立验证。')
    add('* **不确定性**：零误差采用 rule-of-three 上界 `1-0.05^(1/n)`，'
        '`n` 为独立片段数。')
    add('* **复现**：')
    add('')
    add('```sh')
    add('python3 qc_analysis.py --out REPORT.md')
    add('python3 -m unittest discover -s tests -v')
    add('```')
    add('')
    return '\n'.join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='输出 Markdown 路径；缺省打印到标准输出')
    parser.add_argument('--data', type=Path, default=DATA)
    args = parser.parse_args()
    report = render(Corpus(load(args.data)))
    if args.out:
        args.out.write_text(report, encoding='utf-8')
    else:
        print(report)


if __name__ == '__main__':
    main()
