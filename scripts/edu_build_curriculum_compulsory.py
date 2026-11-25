"""Build compulsory-education curriculum-standard item files (义务教育课程标准 2022年版) from MinerU output.

The 2022 PDFs published by MOE are image-only scans, so unlike the senior-high build (``edu_build_curriculum.py``)
there is no text layer to check against: the texts are MinerU's OCR (``edu_mineru_curriculum.py``), and each file
says so in ``text_source``. What this script does check is its own parsing: every item is cut from MinerU blocks in
reading order, and each subject has minimum item counts per 学段 so a format drift fails the build loudly.

    python scripts/edu_build_curriculum_compulsory.py MINERU_DIR [math chinese english]

``MINERU_DIR/<subject>/`` is what ``edu_mineru_curriculum.py`` wrote. Output: ``edu_core/curriculum/compulsory/``.

Items carry ``grades`` ([lo, hi], 1–9): the runtime keeps only the items whose range contains the class's grade.
``groups`` ties 学业要求 / 教学提示 to the content items they belong with (same 学段·领域, 学习任务群 or 级别).
"""

from __future__ import annotations

import glob
import html
import json
import re
import sys
import unicodedata
from pathlib import Path

BASE_URL = 'http://www.moe.gov.cn/srcsite/A26/s8001/202204/'
SOURCES = {  # the per-subject PDF attachments of 教育部关于印发义务教育课程方案和课程标准（2022年版）的通知
    'chinese': ('语文', 'W020220420582344386456.pdf'),
    'math': ('数学', 'W020220510531636118932.pdf'),
    'english': ('英语', 'W020220420582349487953.pdf'),
    'politics': ('道德与法治', 'W020220420582343475848.pdf'),
    'history': ('历史', 'W020220420582345700037.pdf'),
    'geography': ('地理', 'W020220420582354066450.pdf'),
    'science': ('科学', 'W020220420582355009892.pdf'),
    'physics': ('物理', 'W020220420582357585169.pdf'),
    'chemistry': ('化学', 'W020230605524875861384.pdf'),
    'biology': ('生物学', 'W020220420582359998122.pdf'),
    'ai': ('信息科技', 'W020220420582361024968.pdf'),  # 人工智能: the senior-high pack uses 信息技术 too
}
NOTICE_URL = BASE_URL + 't20220420_619921.html'
TEXT_SOURCE = '扫描版 PDF，经 MinerU（vlm 模型）识别；未与 PDF 文字层逐字核对（官方 PDF 没有文字层）'

CN = '一二三四五六七八九十'
CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
STAGES = {1: (1, 2), 2: (3, 4), 3: (5, 6), 4: (7, 9)}
STAGE_NAME = {n: f'第{CN[n - 1]}学段（{a}～{b}年级）' for n, (a, b) in STAGES.items()}

# --------------------------------------------------------------------------- text


_SUP = str.maketrans('0123456789+-', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻')
_LATEX = [
    (r'\\times', '×'), (r'\\div', '÷'), (r'\\pm', '±'), (r'\\neq', '≠'), (r'\\approx', '≈'),
    (r'\\leqslant|\\leq\b|\\le\b', '≤'), (r'\\geqslant|\\geq\b|\\ge\b', '≥'), (r'\\pi\b', 'π'),
    (r'\\cdots|\\ldots|\\dots', '…'), (r'\\angle', '∠'), (r'\\triangle', '△'), (r'\\parallel', '∥'),
    (r'\\perp', '⊥'), (r'\\cong', '≌'), (r'\\sim\b', '∽'), (r'\\%', '%'), (r'\\circ', '°'),
    (r'\\odot', '⊙'), (r'\\Rightarrow', '⇒'), (r'\\quad|\\,|\\;|\\!', ''),
    (r'\\alpha', 'α'), (r'\\beta', 'β'), (r'\\gamma', 'γ'), (r'\\Delta', 'Δ'), (r'\\rho', 'ρ'), (r'\\mu', 'μ'),
    (r'\\Omega', 'Ω'), (r'\\lambda', 'λ'), (r'\\rightarrow|\\to\b', '→'), (r'\\uparrow', '↑'), (r'\\downarrow', '↓'),
]  # fmt: skip
# symbols are also written outside $…$ (卢瑟福\alpha粒子)
_SYMBOLS = [(pat, rep) for pat, rep in _LATEX if len(rep) == 1]


def _formula(m: re.Match) -> str:
    t = m.group(1)
    t = re.sub(r'\\left|\\right', '', t)
    t = re.sub(r'\\(?:text|mathrm|mathbf)\s*\{([^{}]*)\}', r'\1', t)
    for _ in range(2):  # one level of nesting is enough for the standards
        t = re.sub(r'\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}', r'\1/\2', t)
        t = re.sub(r'\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}', r'\1√\2', t)
        t = re.sub(r'\\sqrt\s*\{([^{}]*)\}', r'√\1', t)
    for pat, rep in _LATEX:
        t = re.sub(pat, rep, t)
    t = re.sub(r'\^\s*\{\s*°\s*\}|\^\s*°', '°', t)
    t = re.sub(r'\^\s*\{\s*([0-9+\-]+)\s*\}|\^\s*([0-9])', lambda m: (m.group(1) or m.group(2)).translate(_SUP), t)
    t = t.replace('{', '').replace('}', '')
    return re.sub(r'\s+', '', t)


def norm(text: str) -> str:
    """MinerU text → the form items are stored in (the same conventions as the senior-high files: NFKC except the
    circled numbers, no spaces next to CJK)."""
    text = html.unescape(text or '')
    text = text.replace('\\~', '～').replace('\\%', '%').replace('\\*', '*')  # MinerU escapes Markdown
    text = re.sub(r'\$([^$]+)\$', _formula, text)
    if '\\' in text:
        for pat, rep in _SYMBOLS:
            text = re.sub(pat, rep, text)
    text = ''.join(ch if ch in CIRCLED else unicodedata.normalize('NFKC', ch) for ch in text)
    text = re.sub(r'(?<=[^\x00-\x7f])[ \t]+|[ \t]+(?=[^\x00-\x7f])', '', text)
    text = re.sub(r'(?<=[,;:])[ \t]+(?=\d)', '', text)  # "2, 3, 5" and "2,3,5" are both in the scans
    return text.strip()


def ends_sentence(text: str) -> bool:
    return text.endswith(('。', '!', '?', ';', ':', ')', '”', '"'))


def short_heading(text: str) -> bool:
    """MinerU's text_level is unreliable for in-body headings like ``(5) 圆``: a heading is short and has no clause."""
    return len(text) <= 16 and not re.search(r'[,。;:]', text)


# --------------------------------------------------------------------------- MinerU blocks


def load_blocks(src: Path) -> list[dict]:
    """All body blocks in reading order, with absolute page numbers (chunks of a split PDF are offset)."""
    out = []
    chunks = sorted(src.glob('*-*/'), key=lambda p: int(p.name.split('-')[0]))
    for chunk in chunks:
        offset = int(chunk.name.split('-')[0]) - 1
        (content,) = [Path(p) for p in glob.glob(str(chunk / '*_content_list.json')) if '_v2' not in p]
        for b in json.loads(content.read_text(encoding='utf-8')):
            if b['type'] not in ('text', 'table', 'equation'):
                continue  # headers, page numbers, footnotes, images
            page = b['page_idx'] + 1 + offset
            if b['type'] == 'table':
                caption = norm(' '.join(b.get('table_caption') or []))
                out.append({'type': 'table', 'html': b.get('table_body') or '', 'caption': caption, 'page': page})
            else:
                # a block occasionally holds several numbered lines (生物学 5.2 … 5.4.2): split them; a block whose
                # lines are only a paragraph wrapped by the layout stays one block
                raw = b.get('text', '')
                lines = re.split(r'\n+', raw) if re.search(r'\n+\s*\d+(?:\.\d+)+', raw) else [raw]
                for line in lines:
                    if text := norm(line):
                        out.append({'type': 'text', 'text': text, 'level': b.get('text_level'), 'page': page})
    return out


def between(blocks, start: str, stop: str) -> list[dict]:
    i = next(i for i, b in enumerate(blocks) if b.get('text', '').startswith(start))
    j = next(j for j in range(i + 1, len(blocks)) if blocks[j].get('text', '').startswith(stop))
    return blocks[i + 1 : j]


def table_grid(body: str) -> list[list[str]]:
    """HTML table → rows of cell texts, with rowspan/colspan cells repeated where they reach."""
    rows, carry = [], {}  # carry: column -> (text, rows left)
    for tr in re.findall(r'<tr>(.*?)</tr>', body, re.S):
        cells = re.findall(r'<td([^>]*)>(.*?)</td>', tr, re.S)
        row, col = [], 0
        for attrs, inner in cells:
            while col in carry:
                text, left = carry[col]
                row.append(text)
                carry[col] = (text, left - 1)
                if left - 1 == 0:
                    del carry[col]
                col += 1
            text = norm(re.sub(r'<[^>]+>', '', inner))
            span = int(m.group(1)) if (m := re.search(r'colspan="(\d+)"', attrs)) else 1
            down = int(m.group(1)) if (m := re.search(r'rowspan="(\d+)"', attrs)) else 1
            for _ in range(span):
                row.append(text)
                if down > 1:
                    carry[col] = (text, down - 1)
                col += 1
        while col in carry:
            text, left = carry[col]
            row.append(text)
            carry[col] = (text, left - 1)
            if left - 1 == 0:
                del carry[col]
            col += 1
        rows.append(row)
    return rows


def split_numbered(cell: str) -> list[tuple[str, str]]:
    """``1.歌谣;2.对话`` → [('1', '歌谣'), ('2', '对话')]; ``+1. …`` keeps the plus. Unnumbered cells split on ``;``."""
    parts = re.split(r'[;。]?(?=(?<![\d.+])\+?\d{1,2}\.(?!\d))', cell)
    parts = [p.strip(' ;') for p in parts if p.strip(' ;')]
    out = []
    for p in parts:
        if m := re.match(r'(\+?\d{1,2})\.\s*(.+)', p, re.S):
            out.append((m.group(1), m.group(2).strip()))
        elif out:
            out[-1] = (out[-1][0], out[-1][1] + p)
        else:
            out.extend((str(k), s.strip()) for k, s in enumerate(re.split(r';', p), 1) if s.strip())
    return out


class Items:
    """Accumulates items; a block that does not start a new item continues the open one (page breaks)."""

    def __init__(self):
        self.items, self.cur = [], None

    def start(self, **item):
        self.end()
        self.cur = item

    def add(self, text):
        self.cur['text'] += text

    def end(self):
        if self.cur:
            self.cur['text'] = self.cur['text'].strip()
            if self.cur['text']:
                self.items.append(self.cur)
        self.cur = None

    def finish(self):
        self.end()
        return self.items


def stage_of(text):
    m = re.fullmatch(r'第([一二三四])学段\((\d)～(\d)年级\)', text.replace('~', '～'))
    return CN.index(m.group(1)) + 1 if m else None


ITEM_RE = re.compile(r'^\((\d{1,2})\)(.+)')
CIRCLED_RE = re.compile(f'^([{CIRCLED}])(.+)')


# --------------------------------------------------------------------------- 数学


def build_math(blocks):
    """领域 → 学段 → 【内容要求】【学业要求】【教学提示】 → 小标题 → (1) / ① items."""
    body = between(blocks, '四、课程内容', '五、学业质量')
    domains = {'数与代数': 'A', '图形与几何': 'B', '统计与概率': 'C', '综合与实践': 'D'}
    b = Items()
    domain = stage = section = sub1 = sub2 = None
    sub1_no = sub2_no = ''
    n_req = n_hint = 0
    for blk in body:
        if blk['type'] != 'text':
            continue
        t, page = blk['text'], blk['page']
        if (m := re.fullmatch(r'\([一二三四]\)(\S+)', t)) and m.group(1) in domains:
            b.end()
            domain, stage, section = m.group(1), None, None
            continue
        if t in ('小学部分', '初中部分'):
            continue
        if (s := stage_of(t)) is not None:
            b.end()
            stage, section, sub1, sub2, sub1_no, sub2_no = s, None, None, None, '', ''
            continue
        if t in ('【内容要求】', '【学业要求】', '【教学提示】'):
            b.end()
            section, sub1, sub2, sub1_no, sub2_no, n_req, n_hint = t[1:-1], None, None, '', '', 0, 0
            continue
        if domain is None or stage is None or section is None:
            continue  # the domain introductions
        lo, hi = STAGES[stage]
        group = f'S{stage}{domains[domain]}'
        base = {'grades': [lo, hi], 'stage': STAGE_NAME[stage], 'groups': [group], 'page': page}
        path = [STAGE_NAME[stage], domain]
        if (m := re.fullmatch(r'(\d)\.(\S.{0,14})', t)) and short_heading(m.group(2)):
            b.end()
            sub1, sub2, sub1_no, sub2_no = m.group(2), None, m.group(1), ''
            continue
        if m := re.fullmatch(r'((?:主题活动|项目学习)\d+):(\S+)', t):
            b.end()
            sub1 = f'{m.group(1)}:{m.group(2)}'
            if section == '内容要求':  # the activity's description paragraphs follow
                b.start(id=f'math.{group}.{m.group(1).replace("主题活动", "T").replace("项目学习", "P")}', kind='content',
                        label='·'.join([*path, sub1]), short=sub1, text='', **base)  # fmt: skip
            continue
        if (m := ITEM_RE.match(t)) and short_heading(m.group(2)):
            b.end()
            sub2, sub2_no = m.group(2), m.group(1)
            continue
        if section == '内容要求':
            m = (CIRCLED_RE.match(t) if sub2 else None) or ITEM_RE.match(t)
            if m:
                no = m.group(1)
                where = sub2 or sub1 or domain
                num = str(CIRCLED.index(no) + 1) if no in CIRCLED else no
                ident = '.'.join(x for x in ('math', group, sub1_no, sub2_no, num) if x)
                label_no = no if no in CIRCLED else f'({no})'
                b.start(id=ident, kind='content', label='·'.join([*path, *(x for x in (sub1, sub2) if x), label_no]),
                        short=f'{where}{label_no}', text=m.group(2), **base)  # fmt: skip
            elif b.cur and (not ends_sentence(b.cur['text']) or b.cur['id'].split('.')[-1][:1] in 'TP'):
                b.add(t)
            elif b.cur is None and (sub1 or sub2):  # a 小标题 with a single unnumbered requirement (1.数据分类)
                ident = '.'.join(x for x in ('math', group, sub1_no, sub2_no, '1') if x)
                b.start(id=ident, kind='content', label='·'.join([*path, *(x for x in (sub1, sub2) if x)]),
                        short=sub2 or sub1, text=t, **base)  # fmt: skip
            continue
        if section == '学业要求':
            if b.cur and b.cur['kind'] == 'requirement' and not ends_sentence(b.cur['text']):
                b.add(t)
                continue
            n_req += 1
            where = sub2 or sub1 or domain
            b.start(id=f'math.{group}.R{n_req}', kind='requirement', label='·'.join([*path, '学业要求', where]),
                    short=f'学业要求·{where}', text=t, **base)  # fmt: skip
            continue
        if section == '教学提示':
            if b.cur and b.cur['kind'] == 'hint' and not ends_sentence(b.cur['text']):
                b.add(t)
                continue
            n_hint += 1
            b.start(id=f'math.{group}.H{n_hint}', kind='hint', label='·'.join([*path, '教学提示']), short='教学提示',
                    text=t, **base)  # fmt: skip
    items = b.finish()
    for it in items:
        # 标有 * 的为选学内容（不作考试要求）; a * inside an item marks only that clause and stays in the text
        if it['kind'] == 'content' and it['text'].startswith('*'):
            it['text'], it['optional'] = it['text'][1:], True
    return items, '数学', '课程内容（四个学段的内容要求、学业要求、教学提示）'


# --------------------------------------------------------------------------- 语文

TASK_GROUPS = [
    '语言文字积累与梳理',
    '实用性阅读与交流',
    '文学阅读与创意表达',
    '思辨性阅读与表达',
    '整本书阅读',
    '跨学科学习',
]
ALL_TASKS = [f'T{i}' for i in range(1, 7)]


def build_chinese(blocks):
    b = Items()
    # 学段要求 (课程目标): what each 学段 should reach, by 识字与写字 / 阅读与鉴赏 / 表达与交流 / 梳理与探究
    stage = area = None
    for blk in between(blocks, '(三)学段要求', '四、课程内容'):
        if blk['type'] != 'text':
            continue
        t, page = blk['text'], blk['page']
        if (s := stage_of(t)) is not None:
            b.end()
            stage, area = s, None
            continue
        if m := re.fullmatch(r'【(\S+)】', t):
            b.end()
            area = m.group(1)
            continue
        if stage is None or area is None:
            continue
        if m := re.match(r'(\d{1,2})\.(.+)', t):
            lo, hi = STAGES[stage]
            b.start(id=f'chinese.S{stage}.{"ABCD"[["识字与写字", "阅读与鉴赏", "表达与交流", "梳理与探究"].index(area)]}{m.group(1)}',
                    kind='requirement', label=f'{STAGE_NAME[stage]}·学段要求·{area}·{m.group(1)}',
                    short=f'学段要求·{area}{m.group(1)}', text=m.group(2), grades=[lo, hi], stage=STAGE_NAME[stage],
                    groups=ALL_TASKS, page=page)  # fmt: skip
        elif b.cur and not ends_sentence(b.cur['text']):
            b.add(t)
    b.end()
    # 学习任务群: 学习内容 by 学段, and the group's 教学提示 (for every 学段)
    task = section = stage = None
    for blk in between(blocks, '(二)内容组织与呈现方式', '五、学业质量'):
        if blk['type'] != 'text':
            continue
        t, page = blk['text'], blk['page']
        if t in TASK_GROUPS:
            b.end()
            task, section, stage = TASK_GROUPS.index(t) + 1, None, None
            continue
        if t in ('【学习内容】', '【教学提示】'):
            b.end()
            section, stage = t[1:-1], None
            continue
        if (s := stage_of(t)) is not None:
            b.end()
            stage = s
            continue
        if task is None or section is None:
            continue
        name = TASK_GROUPS[task - 1]
        m = ITEM_RE.match(t)
        if section == '学习内容' and stage:
            lo, hi = STAGES[stage]
            if m:
                b.start(id=f'chinese.S{stage}T{task}.{m.group(1)}', kind='content',
                        label=f'{STAGE_NAME[stage]}·{name}·({m.group(1)})', short=f'{name}({m.group(1)})',
                        text=m.group(2), grades=[lo, hi], stage=STAGE_NAME[stage], groups=[f'T{task}'], page=page)  # fmt: skip
            elif b.cur and not ends_sentence(b.cur['text']):
                b.add(t)
        elif section == '教学提示':
            if m:
                b.start(id=f'chinese.T{task}.H{m.group(1)}', kind='hint', label=f'{name}·教学提示({m.group(1)})',
                        short='教学提示', text=m.group(2), grades=[1, 9], stage='第一至第四学段', groups=[f'T{task}'],
                        page=page)  # fmt: skip
            elif b.cur and not ends_sentence(b.cur['text']):
                b.add(t)
    return b.finish(), '语文', '学段要求；六个学习任务群各学段的学习内容与教学提示'


# --------------------------------------------------------------------------- 英语

LEVELS = {'预备级': (0, 1, 2), '一级': (1, 3, 4), '二级': (2, 5, 6), '三级': (3, 7, 9)}


def _level(text):
    """``二级`` / ``二级+`` / ``三级——8年级`` → (level no, lo, hi, optional)."""
    m = re.search(r'(预备级|[一二三]级)(\+?)(?:[—-]+(\d)年级)?', text)
    if not m:
        return None
    no, lo, hi = LEVELS[m.group(1)]
    if m.group(3):
        lo = hi = int(m.group(3))
    return no, lo, hi, bool(m.group(2))


def build_english(blocks):
    b = Items()
    out = b.items

    def add(ident, kind, label, short, text, level, page, optional=False):
        no, lo, hi, _ = level
        item = {'id': ident, 'kind': kind, 'label': label, 'short': short, 'text': text, 'grades': [lo, hi],
                'stage': f'{["预备级", "一级", "二级", "三级"][no]}（{lo}～{hi}年级）' if lo != hi else f'三级（{lo}年级）',
                'groups': [f'J{no}'], 'page': page}  # fmt: skip
        if optional:
            item['optional'] = True  # 「+」：学有余力的学生的拓展要求，不作为基本要求
        out.append(item)

    # 学段目标 (课程目标): 表现 × 级别 tables → requirements
    g_no = 0
    for blk in between(blocks, '(三)学段目标', '四、课程内容'):
        if blk['type'] != 'table':
            continue
        rows = table_grid(blk['html'])
        if not rows:
            continue
        g_no += 1
        head = rows[0]
        for r_no, row in enumerate(rows[1:], 1):
            for col in range(1, min(len(head), len(row))):
                level = _level(head[col])
                if not level or not row[col]:
                    continue
                for no, text in split_numbered(row[col]):
                    add(f'english.J{level[0]}.G{g_no}.{r_no}.{no}', 'requirement',
                        f'{head[col]}·学段目标·{row[0]}·{no}', f'学段目标·{row[0]}{no}', text, level,
                        blk['page'])  # fmt: skip
    # 内容要求: one table per element (and per 级别 for 主题 / 语言技能)
    element, t_no = None, 0
    for blk in between(blocks, '(一)内容要求', '(二)教学提示'):
        if blk['type'] == 'text':
            t = blk['text']
            if m := re.fullmatch(r'(?:\d\.|\(\d\))(\S{2,6})', t):
                element = m.group(1)
            continue
        rows = table_grid(blk['html'])
        if not rows or not element:
            continue  # the empty stub MinerU leaves where a table continues from the previous page
        t_no += 1
        caption_level = _level(blk['caption'])
        head = rows[0]
        for r_no, row in enumerate(rows[1:], 1):
            cells = [c for c in row if c]
            if not cells:
                continue
            if head[0] == '级别':  # 级别 | [学习策略 |] 内容要求
                level = _level(row[0])
                where = row[1] if len(row) > 2 else ''
            elif caption_level:  # 主题 (范畴 | 主题群 | 子主题内容) and 语言技能 (技能 | 内容要求)
                level, where = caption_level, '·'.join(dict.fromkeys(row[:-1]))
            else:
                continue
            if not level:
                continue
            text_cell = row[-1]
            for no, text in split_numbered(text_cell):
                plus = no.startswith('+') or text_cell.startswith('+')
                lv = (level[0], level[1], level[2], plus)
                name = ['预备级', '一级', '二级', '三级'][level[0]] + ('+' if plus else '')
                if level[1] == level[2] and level[0] == 3:
                    name = f'三级·{level[1]}年级'
                path = '·'.join(x for x in (name, element, where) if x)
                add(f'english.J{level[0]}.C{t_no}.{r_no}.{no.replace("+", "P")}', 'content', f'{path}·{no}',
                    f'{where or element}{no}', text.lstrip('+'), lv, blk['page'], optional=plus)  # fmt: skip
    # 教学提示 by 级别: 第一/第二… paragraphs, and the 预备级 activity table
    level, n = None, 0
    for blk in between(blocks, '(二)教学提示', '五、学业质量'):
        if blk['type'] == 'table':
            if level:
                for r_no, row in enumerate(table_grid(blk['html'])[1:], 1):
                    for no, text in split_numbered(row[-1]):
                        add(f'english.J{level[0]}.HT{r_no}.{no}', 'hint', f'预备级·学习活动·{row[0]}·{no}',
                            f'学习活动·{row[0]}{no}', text, level, blk['page'])  # fmt: skip
            continue
        t = blk['text']
        if m := re.fullmatch(r'\d\.(预备级|[一二三]级)\(\d[~～]\d年级\)', t):
            level, n = _level(m.group(1)), 0
            continue
        if not level:
            continue
        if (
            re.match(r'第[一二三四五六七八九]+,', t)
            or not out
            or out[-1]['kind'] != 'hint'
            or ends_sentence(out[-1]['text'])
        ):
            if re.search(r'参考表\d+。$', t):
                continue
            n += 1
            add(f'english.J{level[0]}.H{n}', 'hint', f'{["预备级", "一级", "二级", "三级"][level[0]]}·教学提示·{n}',
                '教学提示', t, level, blk['page'])  # fmt: skip
        else:
            out[-1]['text'] += t
    return out, '英语', '学段目标；内容要求（六要素，分级）；各级教学提示'


# --------------------------------------------------------------------------- 初中分科: 物理 化学 生物学 历史 地理

JUNIOR = (7, 9)
JUNIOR_STAGE = '第四学段（7～9年级）'
SECTION_RE = re.compile(r'^(?:【|\d\.)\s*(内容要求|学业要求|教学提示)】?$')
THEME_RE = re.compile(r'^\(([一二三四五六七八九十]+)\)(\S.*)$')
GEO_THEME_RE = re.compile(r'^主题([一二三四五六七八九十]+)(\S.*)$')


def build_topics(subject, name, blocks, *, numbered_items_only_deep=False, split_clauses=False, dotted_headings=False):
    """主题 → 【内容要求】【学业要求】【教学提示】. Items: ``1.1.1 …``, ``(1) …``, ``- …`` or plain paragraphs.

    The 初中分科 standards do not say which grade a theme is taught in, so every item is for 7–9 年级.
    ``numbered_items_only_deep``: 生物学 states its concepts as ``1.1 …`` / ``1.1.1 …`` lines, which are items.
    """
    b = Items()
    theme = section = sub = sub1 = None
    t_no, n = 0, {}
    skipping = False  # 活动建议 and 例 lines under 内容要求 are illustrations, not requirements
    in_example = False
    for blk in between(blocks, '四、课程内容', '五、学业质量'):
        if blk['type'] != 'text':
            continue
        t, page = blk['text'].replace('\\-', '-'), blk['page']
        m = GEO_THEME_RE.match(t) or (THEME_RE.match(t) if not GEO_THEME_RE.match(t) else None)
        if m and short_heading(m.group(2)) and (subject != 'geography' or t.startswith('主题')):
            b.end()
            t_no += 1
            theme, section, sub, sub1, skipping = m.group(2), None, None, None, False
            continue
        if sm := SECTION_RE.match(t):
            b.end()
            section, sub, sub1, skipping = sm.group(1), None, None, False
            continue
        if theme is None or section is None:
            continue
        group = f'M{t_no}'
        base = {'grades': list(JUNIOR), 'stage': JUNIOR_STAGE, 'groups': [group], 'page': page}

        def start(kind, text, short, label_tail, t_no=t_no, theme=theme, base=base):
            key = (t_no, kind)
            n[key] = n.get(key, 0) + 1
            prefix = {'content': '', 'requirement': 'R', 'hint': 'H'}[kind]
            label = '·'.join(x for x in (theme, sub, label_tail) if x)  # noqa: B023 (sub is read when called)
            b.start(id=f'{subject}.M{t_no}.{prefix}{n[key]}', kind=kind, short=short, label=label, text=text, **base)

        if section == '内容要求':
            if t.startswith('活动建议'):
                b.end()
                skipping = True
                continue
            if re.match(r'例\s?\d+', t) or t.startswith(('说明:', '说明：')):
                in_example = not ends_sentence(t)  # an example cut by a page break continues on the next block
                continue
            num = re.match(r'^(\d+(?:\.\d+)+)\s*(.+)$', t)
            if in_example and not num:
                in_example = not ends_sentence(t)
                continue
            in_example = False
            if num and dotted_headings:  # 化学 4.2.1 / 历史 1.1: a heading over its paragraphs
                b.end()
                sub1 = sub = num.group(2)
                continue
            if num and (numbered_items_only_deep or not short_heading(num.group(2))):
                skipping = False
                start('content', num.group(2), f'{sub or theme} {num.group(1)}', num.group(1))
                continue
            if num or (re.match(r'^\d\.\s*\S{2,16}$', t) and short_heading(t)):
                b.end()
                sub1 = sub = num.group(2) if num else re.sub(r'^\d\.\s*', '', t)
                skipping = False
                continue
            if skipping:
                continue
            if (m := ITEM_RE.match(t)) and short_heading(m.group(2)):  # (1) 陆地和海洋: a heading under 1. 自然环境
                b.end()
                sub = f'{sub1}·{m.group(2)}' if sub1 else m.group(2)
                continue
            if (m := ITEM_RE.match(t)) or (m := re.match(r'^[-·•]\s*()(.+)$', t)):
                start('content', m.group(2), sub or theme, f'({m.group(1)})' if m.group(1) else '')
                continue
            if b.cur and b.cur['kind'] == 'content' and not ends_sentence(b.cur['text']):
                b.add(t)
                continue
            if split_clauses:  # 历史: one paragraph per period, each ;-clause a requirement
                for clause in [c.strip() for c in re.split(r';', t) if c.strip()]:
                    start('content', clause if ends_sentence(clause) else clause + '。', sub or theme, '')
                b.end()
                continue
            start('content', t, sub or theme, '')
            continue
        # 学业要求 / 教学提示: one item per paragraph or numbered point; short lines are sub-headings
        kind = 'requirement' if section == '学业要求' else 'hint'
        if short_heading(re.sub(r'^(?:\d\.|\(\d\))\s*', '', t)) and not ends_sentence(t):
            b.end()
            continue
        if b.cur and b.cur['kind'] == kind and not ends_sentence(b.cur['text']):
            b.add(t)
            continue
        # numbering and list marks are not part of the text; OCR sometimes drops the dot (4能够…)
        text = re.sub(r'^(?:\(\d{1,2}\)|\d{1,2}(?:\.\d{1,2})+|\d{1,2}\.?(?=[\u4e00-\u9fff])|\d{1,2}\.|[-·•])\s*', '', t)
        start(
            kind,
            text,
            '学业要求' if kind == 'requirement' else '教学提示',
            '学业要求' if kind == 'requirement' else '教学提示',
        )
    return b.finish(), name, '课程内容（各学习主题的内容要求、学业要求、教学提示；活动建议与示例未收录）'


def build_physics(blocks):
    return build_topics('physics', '物理', blocks)


def build_chemistry(blocks):
    return build_topics('chemistry', '化学', blocks, dotted_headings=True)


def build_biology(blocks):
    return build_topics('biology', '生物学', blocks, numbered_items_only_deep=True)


def build_history(blocks):
    return build_topics('history', '历史', blocks, split_clauses=True, dotted_headings=True)


def build_geography(blocks):
    return build_topics('geography', '地理', blocks)


# --------------------------------------------------------------------------- 道德与法治 / 科学 / 信息科技


def stage_in(text):
    """第一学段 / 1~2年级 anywhere in a heading or table cell → 学段 number."""
    t = text.replace('~', '～')
    if m := re.search(r'第([一二三四])学段', t):
        return CN.index(m.group(1)) + 1
    if m := re.search(r'(\d)～(\d)年级', t):
        return {(1, 2): 1, (3, 4): 2, (5, 6): 3, (7, 9): 4}.get((int(m.group(1)), int(m.group(2))))
    return None


def split_bullets(cell):
    # a dot between digits is a date (12·4 国家宪法日), not a bullet
    return [p.strip() for p in re.split(r'(?<!\d)[·•]|[·•](?!\d)', cell) if len(p.strip()) >= 4]


def split_sequential(cell):
    """``1观察…2识别…`` (科学 numbers its points without a dot): split only at the next expected number, so a
    number inside the text (说出26个) is never taken for a point."""
    out, pos = [], 0
    m = re.match(r'(\d{1,2})', cell)
    if not m:
        return [(None, cell)]
    no = int(m.group(1))
    pos = len(m.group(1))
    while True:
        nxt = re.search(rf'(?<!\d){no + 1}(?=[一-鿿(“《])', cell[pos:])
        end = pos + nxt.start() if nxt else len(cell)
        # OCR sometimes drops a digit (22, 23 … read as 2, 2 …): a sentence end followed by a number still starts a
        # point, whatever the number says; the caller renumbers points that do not go up
        for k, piece in enumerate(re.split(r'(?<=。)\d{1,2}(?=[\u4e00-\u9fff(“《])', cell[pos:end])):
            out.append((no + k * 0.001 if k else no, piece.strip(' .、')))
        if not nxt:
            return out
        no, pos = no + 1, end + len(str(no + 1))


def build_politics(blocks):
    """Per 学段: a table 学习主题 | 内容要求 | 教学提示 (bullets ·), then 【学业要求】 paragraphs."""
    b = Items()
    out = b.items
    stage, n = None, {}
    for blk in between(blocks, '四、课程内容', '五、学业质量'):
        if blk['type'] == 'text':
            t = blk['text']
            if (s := stage_in(t)) and THEME_RE.match(t):
                stage = s
                continue
            if t == '【学业要求】' or not stage:
                continue
            if short_heading(t):
                continue
            if out and out[-1]['kind'] == 'requirement' and not ends_sentence(out[-1]['text']):
                out[-1]['text'] += t
                continue
            lo, hi = STAGES[stage]
            n[(stage, 'R')] = n.get((stage, 'R'), 0) + 1
            out.append({'id': f'politics.S{stage}.R{n[(stage, "R")]}', 'kind': 'requirement',
                        'label': f'{STAGE_NAME[stage]}·学业要求', 'short': '学业要求',
                        'text': re.sub(r'^(?:\(\d{1,2}\)|\d{1,2}\.)\s*', '', t), 'grades': [lo, hi],
                        'stage': STAGE_NAME[stage], 'groups': [f'S{stage}'], 'page': blk['page']})  # fmt: skip
            continue
        if not stage:
            continue
        lo, hi = STAGES[stage]
        for row in table_grid(blk['html']):
            if len(row) < 3 or row[0] == '学习主题':
                continue
            topic = row[0]
            group = f'S{stage}'
            for kind, cell in (('content', row[1]), ('hint', row[2])):
                parts = split_bullets(cell)
                if not parts:
                    continue
                if not cell.lstrip().startswith(('·', '•')) and out:
                    # a row MinerU split at a page break: its first part continues the previous item
                    prev = next((it for it in reversed(out) if it['kind'] == kind), None)
                    if prev and not ends_sentence(prev['text']):
                        prev['text'] += parts.pop(0)
                for text in parts:
                    key = (stage, kind)
                    n[key] = n.get(key, 0) + 1
                    tag = '' if kind == 'content' else 'H'
                    out.append({'id': f'politics.S{stage}.{tag}{n[key]}', 'kind': kind,
                                'label': f'{STAGE_NAME[stage]}·{topic}' + ('·教学提示' if kind == 'hint' else ''),
                                'short': topic if kind == 'content' else '教学提示', 'text': text, 'grades': [lo, hi],
                                'stage': STAGE_NAME[stage], 'groups': [group], 'page': blk['page']})  # fmt: skip
    return out, '道德与法治', '各学段学习主题的内容要求、教学提示与学业要求'


def build_science(blocks):
    """Per 核心概念: a table 学段 | 学习内容 | 内容要求 (points 1, 2, … without dots) and 学段 | 学业要求."""
    out = []
    concept = c_no = None
    section = None
    c_no = 0
    n, last = {}, {}
    for blk in between(blocks, '四、课程内容', '五、学业质量'):
        if blk['type'] == 'text':
            t = blk['text']
            if (m := THEME_RE.match(t)) and short_heading(m.group(2)):
                concept, c_no, section = m.group(2), c_no + 1, None
                continue
            if sm := SECTION_RE.match(t):
                section = sm.group(1)
                continue
            if section == '教学提示' and concept and not short_heading(re.sub(r'^(?:\d\.|\(\d\))\s*', '', t)):
                if out and out[-1]['kind'] == 'hint' and not ends_sentence(out[-1]['text']):
                    out[-1]['text'] += t
                    continue
                n[(c_no, 'H')] = n.get((c_no, 'H'), 0) + 1
                out.append({'id': f'science.C{c_no}.H{n[(c_no, "H")]}', 'kind': 'hint', 'label': f'{concept}·教学提示',
                            'short': '教学提示', 'text': re.sub(r'^(?:\(\d{1,2}\)|\d{1,2}\.)\s*', '', t),
                            'grades': [1, 9], 'stage': '各学段', 'groups': [f'C{c_no}'], 'page': blk['page']})  # fmt: skip
            continue
        if not concept or section not in ('内容要求', '学业要求'):
            continue
        stage = None
        for row in table_grid(blk['html']):
            if not row or row[0] == '学段':
                continue
            stage = stage_in(row[0]) or stage
            if not stage:
                continue
            lo, hi = STAGES[stage]
            base = {'grades': [lo, hi], 'stage': STAGE_NAME[stage], 'groups': [f'C{c_no}'], 'page': blk['page']}
            if section == '学业要求':
                text = row[-1]
                prev = out[-1] if out else None
                if (
                    prev
                    and prev['kind'] == 'requirement'
                    and prev['grades'] == [lo, hi]
                    and prev['id'].startswith(f'science.C{c_no}.')
                    and not ends_sentence(prev['text'])
                ):
                    prev['text'] += text
                    continue
                out.append({'id': f'science.C{c_no}.S{stage}.R', 'kind': 'requirement', 'label': f'{STAGE_NAME[stage]}·{concept}·学业要求',
                            'short': '学业要求', 'text': text, **base})  # fmt: skip
                continue
            topic = re.sub(r'^\d+\.\d+\s*', '', row[1]) if len(row) > 2 else concept
            topic_no = m.group(0).strip() if len(row) > 2 and (m := re.match(r'^\d+\.\d+', row[1])) else ''
            for no, text in split_sequential(row[-1]):
                if no is None:  # a row continued from the previous page
                    prev = next((it for it in reversed(out) if it['kind'] == 'content'), None)
                    if prev:
                        prev['text'] += text
                    continue
                key = (c_no, stage)
                no = int(no) if int(no) > last.get(key, 0) else last.get(key, 0) + 1  # see split_sequential
                last[key] = no
                out.append({'id': f'science.C{c_no}.S{stage}.{no}', 'kind': 'content',
                            'label': f'{STAGE_NAME[stage]}·{concept}·{topic_no} {topic}'.strip(),
                            'short': f'{topic}{no}', 'text': text, **base})  # fmt: skip
    # 学业要求 rows of a 学段 that a page break split were merged above; give each its stable id
    seen = {}
    for it in out:
        if it['kind'] == 'requirement':
            seen[it['id']] = seen.get(it['id'], 0) + 1
            it['id'] += str(seen[it['id']])
    return out, '科学', '十三个核心概念各学段的内容要求、学业要求与教学提示'


def build_ai(blocks):
    """学段 → 模块 (一)… → 1.内容要求 (1)… / 2.学业要求 / 3.教学提示; 跨学科主题 activities are kept as 教学提示."""
    b = Items()
    stage = module = section = None
    m_no, n = 0, {}
    for blk in between(blocks, '四、课程内容', '五、学业质量'):
        if blk['type'] != 'text':
            continue
        t, page = blk['text'], blk['page']
        if (s := stage_of(t)) is not None:
            b.end()
            stage, module, section = s, None, None
            continue
        if stage is None:
            continue
        if (m := THEME_RE.match(t)) and len(m.group(2)) <= 20:
            b.end()
            m_no += 1
            module = m.group(2)
            section = '跨学科主题' if module.startswith('跨学科主题') else None
            continue
        if sm := SECTION_RE.match(t):
            b.end()
            section = sm.group(1)
            continue
        if module is None or section is None:
            continue
        lo, hi = STAGES[stage]
        base = {'grades': [lo, hi], 'stage': STAGE_NAME[stage], 'groups': [f'M{m_no}'], 'page': page}
        kind = {'内容要求': 'content', '学业要求': 'requirement'}.get(section, 'hint')
        if section == '跨学科主题' and re.match(r'^\d\.\s*\S{2,16}$', t) and short_heading(t):
            b.end()
            continue
        if kind == 'content' and (m := ITEM_RE.match(t)):
            b.start(id=f'ai.S{stage}.M{m_no}.{m.group(1)}', kind='content', label=f'{STAGE_NAME[stage]}·{module}·({m.group(1)})',
                    short=f'{module}({m.group(1)})', text=m.group(2), **base)  # fmt: skip
            continue
        if b.cur and b.cur['kind'] == kind and (not ends_sentence(b.cur['text']) or t.startswith(('例如', '比如'))):
            b.add(t)
            continue
        key = (m_no, kind)
        n[key] = n.get(key, 0) + 1
        tag = {'content': 'C', 'requirement': 'R', 'hint': 'H'}[kind]
        b.start(id=f'ai.S{stage}.M{m_no}.{tag}{n[key]}', kind=kind,
                label=f'{STAGE_NAME[stage]}·{module}·' + ('学业要求' if kind == 'requirement' else '教学提示'),
                short='学业要求' if kind == 'requirement' else '教学提示',
                text=re.sub(r'^(?:\(\d{1,2}\)|\d{1,2}\.)\s*', '', t), **base)  # fmt: skip
    return b.finish(), '信息科技', '各学段模块的内容要求、学业要求、教学提示（跨学科主题收录为教学提示）'


BUILDERS = {
    'math': build_math,
    'chinese': build_chinese,
    'english': build_english,
    'physics': build_physics,
    'chemistry': build_chemistry,
    'biology': build_biology,
    'history': build_history,
    'geography': build_geography,
    'politics': build_politics,
    'science': build_science,
    'ai': build_ai,
}


# --------------------------------------------------------------------------- checks and output

MIN_CONTENT = {  # per grade 1..9: a parse that drops a 学段 or a table fails here
    'math': {1: 15, 3: 25, 5: 30, 7: 80},
    'chinese': {1: 15, 3: 15, 5: 15, 7: 15},
    'english': {3: 60, 5: 60, 7: 80},
    'physics': {7: 40},
    'chemistry': {7: 40},
    'biology': {7: 40},
    'history': {7: 25},
    'geography': {7: 25},
    'politics': {1: 10, 3: 10, 5: 10, 7: 10},
    'science': {1: 20, 3: 30, 5: 30, 7: 50},
    'ai': {1: 8, 3: 8, 5: 8, 7: 8},
}


def check(subject, items):
    ids = [it['id'] for it in items]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SystemExit(f'{subject}: 重复的编号 {sorted(dup)[:10]}')
    for it in items:
        if len(it['text']) < 4:
            raise SystemExit(f'{subject}: 条目过短 {it}')
        if re.match(r'\(?\d{1,2}[.)]', it['text']) and it['kind'] == 'content':
            raise SystemExit(f'{subject}: 条目开头还带着编号，可能是两条没拆开 {it}')
        lo, hi = it['grades']
        if not 1 <= lo <= hi <= 9:
            raise SystemExit(f'{subject}: 年级范围不对 {it}')
    for grade, least in MIN_CONTENT[subject].items():
        n = sum(1 for it in items if it['kind'] == 'content' and it['grades'][0] <= grade <= it['grades'][1])
        if n < least:
            raise SystemExit(f'{subject}: {grade} 年级只有 {n} 条内容要求（至少应有 {least}）')
    left = [it['id'] for it in items if '\\' in it['text']]
    if left:
        print(f'  注意：{len(left)} 条含未转换的公式记号：{left[:8]}')


def main():
    src = Path(sys.argv[1])
    subjects = sys.argv[2:] or list(SOURCES)
    dest_dir = Path(__file__).resolve().parents[1] / 'edu_core' / 'curriculum' / 'compulsory'
    dest_dir.mkdir(parents=True, exist_ok=True)
    for subject in subjects:
        sdir = src / subject
        source = json.loads((sdir / 'source.json').read_text(encoding='utf-8'))
        blocks = load_blocks(sdir)
        items, _, scope = BUILDERS[subject](blocks)
        check(subject, items)
        out = {
            'subject': subject,
            'stage': 'compulsory',
            'title': f'义务教育{SOURCES[subject][0]}课程标准（2022年版）',
            'source_url': BASE_URL + SOURCES[subject][1],
            'source_notice': NOTICE_URL,
            'source_sha256': source['source_sha256'],
            'text_source': TEXT_SOURCE,
            'scope': scope,
            'items': items,
        }
        dest = dest_dir / f'{subject}.json'
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        kinds = {}
        for it in items:
            kinds[it['kind']] = kinds.get(it['kind'], 0) + 1
        by_grade = {g: sum(1 for it in items if it['kind'] == 'content' and it['grades'][0] <= g <= it['grades'][1])
                    for g in range(1, 10)}  # fmt: skip
        print(f'{dest.name}: {len(items)} 条 {kinds}  各年级内容要求 {by_grade}')


if __name__ == '__main__':
    main()
