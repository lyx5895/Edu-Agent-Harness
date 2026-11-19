"""Build a curriculum-standard item file from the official MOE PDF.

Source: 教育部《普通高中课程方案和语文等学科课程标准（2017年版2020年修订）》, attachment
http://www.moe.gov.cn/srcsite/A26/s8001/202006/W020200603315372317586.zip

    python scripts/edu_build_curriculum.py math path/to/3.普通高中数学课程标准（2017年版2020年修订）.pdf

Writes ``edu_core/curriculum/<subject>.json``. Every item text is checked to be a verbatim substring of the
normalized PDF text (the PDF itself is not committed; its sha256 is recorded).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

from pypdf import PdfReader

SOURCE_URL = 'http://www.moe.gov.cn/srcsite/A26/s8001/202006/W020200603315372317586.zip'
SOURCE_TITLE = '普通高中{name}课程标准（2017年版2020年修订）'

# The PDF's math italics come out as CJK lookalikes (a common FangZheng font mapping).
_LOWER = '犪犫犮犱犲犳犵犺犻犼犽犾犿狀狅狆狇狉狊狋狌狏狑狓狔狕'
_UPPER = '犃犅犆犇犈犉犌犎犐犑犓犔犕犖犗犘犙犚犛犜犝犞犠犡犢犣'
GLYPHS = {**dict(zip(_LOWER, 'abcdefghijklmnopqrstuvwxyz')), **dict(zip(_UPPER, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')), '槡': '√'}

CN_NUM = '一二三四五六七八九十'
CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮'


OPTIONAL = '[选学]'  # the PDF marks 选学内容 (不作为考试要求) with a private-use icon


def normalize(text: str) -> str:
    text = ''.join(GLYPHS.get(ch, ch) for ch in text)
    text = text.replace('\ue012', OPTIONAL).replace('\ue010', '')  # U+E010 is a decorative separator
    # NFKC, except circled numbers (①) which mark the items and must survive
    text = ''.join(ch if ch in CIRCLED else unicodedata.normalize('NFKC', ch) for ch in text)
    # Drop spaces next to non-ASCII characters (the PDF pads CJK punctuation); keep spaces between ASCII words.
    text = re.sub(r'(?<=[^\x00-\x7f])[ \t]+|[ \t]+(?=[^\x00-\x7f])', '', text)
    return text


def tighten_math(text: str) -> str:
    """Math PDFs letter-space formulas (``l oga x``, ``V e n n``); ASCII runs carry no real word spaces there."""
    return re.sub(r'(?<=[A-Za-z0-9+\-=(]) +(?=[A-Za-z0-9+\-=)])', '', text)


def page_footnote(line):
    return bool(re.match(r'\[\d+\]\S', line))


def page_lines(pdf: Path, name: str):
    header = normalize(SOURCE_TITLE.format(name=name)).replace(' ', '')
    for no, page in enumerate(PdfReader(str(pdf)).pages, 1):
        for raw in (page.extract_text() or '').splitlines():
            line = normalize(raw).strip()
            if name == '数学':
                line = tighten_math(line)
            if re.match(r'\[\d+\]', line) and page_footnote(line):
                continue  # footnotes sit at the page bottom, in the middle of running text
            if not line or line.replace(' ', '') == header or line.startswith('/G') or re.fullmatch(r'[\d ]+', line):
                continue
            yield no, line


def build_math(pdf: Path):
    lines = list(page_lines(pdf, '数学'))
    full = ''.join(line for _, line in lines)
    start = next(i for i, (_, l) in enumerate(lines) if l == '主题一预备知识')
    end = next(i for i, (_, l) in enumerate(lines) if l == '(三)选修课程' and i > start)
    course, theme, section, unit, sub = '必修', None, None, None, None
    items, current = [], None
    sub_optional = False
    t_idx = u_idx = s_idx = 0

    def flush():
        nonlocal current
        if current:
            current['text'] = current['text'].strip()
            items.append(current)
            current = None

    def new(kind, text, page, label, ident):
        nonlocal current
        flush()
        optional = kind == 'content' and (sub_optional or text.startswith(OPTIONAL))
        current = {'id': ident, 'kind': kind, 'course': course, 'theme': theme, 'unit': unit, 'sub': sub,
                   'label': label, 'text': text.removeprefix(OPTIONAL) if optional else text, 'page': page}
        if optional:
            current['optional'] = True  # 选学内容，不作为考试要求

    for page, line in lines[start:end]:
        if line.startswith('(二)选择性必修课程'):
            flush()
            course, t_idx, theme, section = '选择性必修', 0, None, None
            continue
        m = re.fullmatch(r'主题([一二三四五六七八九])(\S+)', line)
        if m and not re.search(r'\d', line):
            flush()
            t_idx += 1
            theme, section, unit, sub, u_idx = m.group(2).strip(), None, None, None, 0
            continue
        if line in ('【内容要求】', '【教学提示】', '【学业要求】'):
            flush()
            section, unit, sub, u_idx = line[1:-1], None, None, 0
            s_idx = 0
            continue
        if theme is None or section is None or line.startswith('['):
            continue  # hour tables before the first theme; footnotes like [1]
        if line.startswith('主题单元建议课时'):
            continue
        cb = 'B' if course == '必修' else 'X'
        base = f'math.{cb}{t_idx}'
        if section == '内容要求':
            m = re.match(r'(\d+)\s*\.\s*(\S.*)', line) or re.fullmatch(r'(\d+)([^\d\s。,;:()]{1,20})', line)
            if m and len(m.group(2)) <= 30 and not m.group(2).endswith('。'):
                flush()
                u_idx, s_idx = int(m.group(1)), 0
                unit, sub, sub_optional = m.group(2), None, False
                continue
            m = re.match(r'\((\d+)\)(.*)', line)
            if m and len(m.group(2)) <= 30 and not m.group(2).endswith('。'):
                flush()
                s_idx = int(m.group(1))
                sub = m.group(2)
                sub_optional = sub.startswith(OPTIONAL)
                sub = sub.removeprefix(OPTIONAL)
                continue
            if line[0] in CIRCLED:
                k = CIRCLED.index(line[0]) + 1
                new('content', line[1:], page, f'{course}·{theme}·{unit or ""}·{sub or ""}{line[0]}'.replace('··', '·'),
                    f'{base}.{u_idx}.{s_idx}.{k}')
                continue
            if sub and current is None:
                new('content', line, page, f'{course}·{theme}·{unit or ""}·{sub}'.replace('··', '·'), f'{base}.{u_idx}.{s_idx}')
                continue
            if current is None:
                # unit introduction / 内容包括 lines (also the theme's own intro when there is no unit)
                new('intro', line, page, f'{course}·{theme}·{unit or ""}'.rstrip('·'), f'{base}.{u_idx}.intro')
                continue
            current['text'] += line
            if line.endswith('。') and current['kind'] == 'intro':
                flush()
            continue
        kind = 'hint' if section == '教学提示' else 'requirement'
        if current is None or current['kind'] != kind:
            s_idx += 1
            new(kind, line, page, f'{course}·{theme}·{section}', f'{base}.{"T" if kind == "hint" else "R"}{s_idx}')
        else:
            current['text'] += line
        if line.endswith('。') and len(current['text']) > 40:
            flush()
    flush()
    # merge intro fragments of the same id
    merged = []
    for it in items:
        if merged and it['id'] == merged[-1]['id']:
            merged[-1]['text'] += it['text']
        else:
            merged.append(it)
    ids = [it['id'] for it in merged]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SystemExit(f'duplicate ids: {sorted(dup)}')
    bad = [it['id'] for it in merged if it['text'] not in full]
    if bad:
        for it in merged:
            if it['id'] in bad:
                print('NOT VERBATIM', it['id'], it['label'], it['text'][:200])
        raise SystemExit(f'{len(bad)} items are not verbatim')
    return merged


# ---------------------------------------------------------------------------------------------------------------
# Generic parser for the other subjects. Each standard has its own layout; a config names the markers.
# ---------------------------------------------------------------------------------------------------------------

SECTION = {'【内容要求】': 'content', '【教学提示】': 'hint', '【学业要求】': 'requirement'}
COURSES = {'(一)必修课程': '必修', '(二)选择性必修课程': '选择性必修'}
ITEM_RE = re.compile(r'^(\d+(?:\.\d+)+)\s*(\S.*)$')


def clean_lines(pdf: Path, name: str):
    """Page lines without running headers (│四、课程内容│) and table continuation marks."""
    return [(p, l) for p, l in page_lines(pdf, name) if '│' not in l and l != '续表']


class Builder:
    """Collects items; every item keeps the contiguous line runs it was built from, for the verbatim check."""

    def __init__(self, sid, lines):
        self.sid, self.lines = sid, lines
        self.full = ''.join(l for _, l in lines)
        self.items, self.cur, self.last_idx = [], None, None

    def start(self, kind, ident, label, short, page, idx, text='', **extra):
        self.end()
        self.cur = {'id': ident, 'kind': kind, 'label': label, 'short': short, 'text': '', 'page': page, '_runs': [], **extra}
        if text:
            self.add(idx, text)

    def add(self, idx, text):
        if self.cur is None:
            return
        if self.cur['_runs'] and self.cur.get('_last') == idx - 1:
            self.cur['_runs'][-1] += text
        else:
            self.cur['_runs'].append(text)
        self.cur['text'] += text
        self.cur['_last'] = idx

    def end(self):
        if self.cur and self.cur['text'].strip():
            self.items.append(self.cur)
        self.cur = None

    def finish(self):
        self.end()
        seen = {}
        for it in self.items:  # numbering restarts in some modules; keep ids unique
            n = seen.get(it['id'], 0)
            seen[it['id']] = n + 1
            if n:
                it['id'] = f'{it["id"]}~{n + 1}'
        bad = [it for it in self.items if any(r not in self.full for r in it['_runs'])]
        for it in bad:
            print('NOT VERBATIM', it['id'], it['label'], it['text'][:120])
        if bad:
            raise SystemExit(f'{self.sid}: {len(bad)} items are not verbatim')
        for it in self.items:
            del it['_runs']
            it.pop('_last', None)
        return self.items


def build_generic(sid, name, pdf, cfg):
    lines = clean_lines(pdf, name)
    b = Builder(sid, lines)
    start = max(i for i, (_, l) in enumerate(lines) if l == '(一)必修课程')
    end = next(i for i, (_, l) in enumerate(lines) if l == '(三)选修课程' and i > start)
    course, module, group, section = '必修', None, None, None
    m_idx = g_idx = h_idx = 0
    title_pending = None  # titled items: "1.1 早期中华文明" then the paragraph
    skipping = False
    for idx in range(start, end):
        page, line = lines[idx]
        if line in COURSES:
            b.end()
            course, module, group, section, m_idx = COURSES[line], None, None, None, 0
            continue
        mod_re = cfg.get('module_by_course', {}).get(course, cfg['module'])
        m = mod_re.match(line)
        if m:
            b.end()
            m_idx += 1
            module, group, section, g_idx = re.sub(r'\s*:\s*', ':', m.group(m.lastindex).strip()), None, None, 0
            continue
        if module is None:
            continue
        sec = SECTION.get(line) or next((v for k, v in cfg.get('sections', {}).items() if re.fullmatch(k, line)), None)
        if sec:
            b.end()
            section, skipping, h_idx = sec, sec == 'skip', 0 if sec != section else h_idx
            continue
        grp_re = cfg.get('group_by_course', {}).get(course, cfg.get('group'))
        if grp_re and (m := grp_re.match(line)):
            b.end()
            g_idx += 1
            group, h_idx = re.sub(r'\s*:\s*', ':', m.group(m.lastindex).strip()), 0
            if cfg.get('group_by_course', {}).get(course):
                section = None  # 化学 选择性必修: each 主题 opens its own 【内容要求】
            continue
        if section == 'content' and cfg.get('skip') and cfg['skip'].match(line):
            b.end()
            skipping = True
            continue
        cb = 'B' if course == '必修' else 'X'
        base = f'{sid}.{cb}{m_idx}' + (f'.G{g_idx}' if cfg.get('group_by_course', {}).get(course) else '')
        where = '·'.join(x for x in (course, module, group) if x)
        if section == 'content' and cfg.get('topic') and (m := cfg['topic'].match(line)) and len(m.group(2)) <= 30 \
                and not re.search(r'[。;,]', m.group(2)):
            b.end()
            group = m.group(2)
            skipping = False
            continue
        if section == 'content' and (m := ITEM_RE.match(line)) and m.group(1).count('.') in cfg.get('depths', (1, 2)):
            skipping = False
            num, rest = m.group(1), m.group(2)
            if cfg.get('titled') and len(rest) <= 40 and not re.search(r'[。;]', rest):
                b.end()
                title_pending = (num, rest, page)
                b.start('content', f'{base}.{num}', f'{where}·{num}{rest}', f'{num} {rest}', page, idx)
                continue
            short = f'{module} {num}' if cfg.get('short_module', True) else num
            b.start('content', f'{base}.{num}', f'{where}·{num}', short, page, idx, rest)
            continue
        if section == 'content' and cfg.get('inline_hint') and line.startswith(cfg['inline_hint']):
            h_idx += 1
            b.start('hint', f'{base}.T{h_idx}', f'{where}·教学提示', '教学提示', page, idx, line[1:])
            continue
        if skipping or section not in ('content', 'hint', 'requirement'):
            continue
        cur = b.cur
        if section == 'content':
            if cur is None:
                continue  # module introductions
            if cfg.get('item_end') and cur['kind'] == 'content' and cur['text'].endswith('。'):
                # two-column tables: once the requirement sentence ends, the rest belongs to the hint column
                prev = next((it for it in reversed(b.items) if it['kind'] == 'hint' and it['id'].startswith(base)), None)
                if prev is not None and not line.startswith(cfg.get('inline_hint', '\0')):
                    b.end()
                    b.cur = prev
                    b.items.remove(prev)
                    b.add(idx, line)
                continue
            b.add(idx, line)
            continue
        kind = section
        if cur is None or cur['kind'] != kind:
            h_idx += 1
            tag = 'T' if kind == 'hint' else 'R'
            b.start(kind, f'{base}.{tag}{h_idx}', f'{where}·{"教学提示" if kind == "hint" else "学业要求"}',
                    '教学提示' if kind == 'hint' else '学业要求', page, idx, line)
        else:
            b.add(idx, line)
        if line.endswith('。') and len(b.cur['text']) > 60:
            b.end()
    items = b.finish()
    for it in items:
        it['text'] = it['text'].strip()
    return items


CONFIGS = {
    'politics': {
        'name': '思想政治', 'module': re.compile(r'^模块(\d+)\s*:\s*(.+)$'),
        'topic': re.compile(r'^(\d+)\.([^\d\s.].*)$'), 'depths': (1,), 'inline_hint': '◆', 'item_end': True,
        'sections': {r'内容要求教学提示': 'content'},
    },
    'history': {
        'name': '历史', 'module': re.compile(r'^(中外历史纲要|模块\d+(\S+))$'), 'depths': (1,), 'titled': True,
    },
    'geography': {
        'name': '地理', 'module': re.compile(r'^(地理\d|选择性必修\d(\S+))$'), 'depths': (1,),
    },
    'physics': {
        'name': '物理', 'module': re.compile(r'^\d+\.((?:选择性)?必修\d)$'), 'depths': (2,),
        'topic': re.compile(r'^(\d+\.\d+)([^\d\s.].*)$'),
        'skip': re.compile(r'^(例\d+|活动建议)'),
        'sections': {r'(?:选择性)?必修\d的教学提示': 'hint', r'(?:选择性)?必修\d的学业要求': 'requirement'},
    },
    'chemistry': {
        'name': '化学', 'module': re.compile(r'^主题\d+\s*:\s*(.+)$'), 'depths': (1,), 'titled': True,
        'module_by_course': {'选择性必修': re.compile(r'^模块\d+(\S+)$')},
        'group_by_course': {'选择性必修': re.compile(r'^主题\d+\s*:\s*(.+)$')},
        'skip': re.compile(r'^\d+\.学生必做实验|^●'),
    },
    'biology': {
        'name': '生物学', 'module': re.compile(r'^模块\d+(\S+)$'), 'depths': (1, 2), 'short_module': True,
        'group': re.compile(r'^概念\d+(\S.*)$'),
    },
    'ai': {
        'name': '信息技术', 'module': re.compile(r'^模块\d+\s*:\s*(.+)$'), 'depths': (1,),
    },
}


def build_chinese(pdf):
    """语文: 18 学习任务群 ((1)(2)… under 学习目标与内容 / 教学提示), then the 学习要求 per course."""
    lines = clean_lines(pdf, '语文')
    b = Builder('chinese', lines)
    start = next(i for i, (_, l) in enumerate(lines) if l == '(一)学习任务群')
    end = next(i for i, (_, l) in enumerate(lines) if l == '五、学业质量' and i > start)
    group = g_idx = None
    section, req_course, n_req = None, None, 0
    for idx in range(start + 1, end):
        page, line = lines[idx]
        if m := re.fullmatch(r'学习任务群(\d+)(\S+)', line):
            b.end()
            g_idx, group, section = int(m.group(1)), f'学习任务群{m.group(1)}{m.group(2)}', None
            continue
        if line in ('1.学习目标与内容', '2.教学提示'):
            b.end()
            section = 'content' if line.startswith('1') else 'hint'
            continue
        if line == '(二)学习要求':
            b.end()
            group, section = None, 'requirement'
            continue
        if section == 'requirement':
            if line.endswith('课程学习要求') or line.startswith('选择性必修和选修课程学习要'):
                b.end()
                req_course = '必修' if line.startswith('必修') else '选择性必修和选修'
                continue
            if m := re.match(r'^(\d+)\.(\S.*)$', line):
                n_req += 1
                b.start('requirement', f'chinese.R{n_req}', f'{req_course}课程学习要求·{m.group(1)}',
                        f'{req_course}学习要求 {m.group(1)}', page, idx, m.group(2))
                continue
            b.add(idx, line)
            continue
        if group is None or section is None:
            continue
        if m := re.match(r'^\((\d+)\)(\S.*)$', line):
            kind = 'content' if section == 'content' else 'hint'
            tag = '' if kind == 'content' else 'T'
            label = f'{group}·{"学习目标与内容" if kind == "content" else "教学提示"}({m.group(1)})'
            b.start(kind, f'chinese.G{g_idx}.{tag}{m.group(1)}', label,
                    f'任务群{g_idx}({m.group(1)})' if kind == 'content' else '教学提示', page, idx, m.group(2))
            continue
        b.add(idx, line)
    items = b.finish()
    for it in items:
        it['text'] = it['text'].strip()
    return items


def build_english(pdf):
    """英语: tables 表2–表11 list numbered requirements per course row (必修 / 选择性必修 / 选修)."""
    lines = clean_lines(pdf, '英语')
    b = Builder('english', lines)
    element, course, t_no = None, None, 0
    hint_n = 0
    in_table = in_hint = False
    vertical, sub, sub_n = '', None, 0
    for idx, (page, line) in enumerate(lines):
        if m := re.fullmatch(r'表(\d+)普通高中英语课程(\S+)内容要求', line):
            b.end()
            t_no, element, in_table, in_hint = int(m.group(1)), m.group(2), True, False
            course = '不分课程类别' if element == '主题语境' else None
            sub, vertical, sub_n = None, '', 0
            continue
        if t_no and line == '【教学提示】':
            b.end()
            in_table, in_hint, hint_n = False, True, 0
            continue
        if re.fullmatch(r'表\d+.*', line) or line == '【内容要求】' or re.fullmatch(r'\([一二三四五六七]\)\S{2,8}', line):
            b.end()
            in_table = in_hint = False
            if t_no >= 11 and re.fullmatch(r'表1[2-9].*', line):
                break
            continue
        if in_hint and element:
            if b.cur is None or b.cur['kind'] != 'hint':
                hint_n += 1
                b.start('hint', f'english.T{t_no}.{hint_n}', f'{element}·教学提示', '教学提示', page, idx, line)
            else:
                b.add(idx, line)
            if line.endswith('。') and len(b.cur['text']) > 60:
                b.end()
            continue
        if not in_table:
            continue
        nxt = lines[idx + 1][1] if idx + 1 < len(lines) else ''
        prev = lines[idx - 1][1]
        if len(line) == 1 and re.fullmatch(r'[\u4e00-\u9fff]', line):
            vertical += line  # row labels printed one character per line (理/解/性/技/能)
            continue
        if vertical:
            if len(vertical) >= 3:
                sub, sub_n = vertical, sub_n + 1
            vertical = ''
        if line == '必修' and prev == '选择性':
            continue
        if line == '必修':
            b.end(); course, sub = '必修', None; continue  # noqa: E702
        if line == '选择性' and nxt == '必修' or line == '选择性必修':
            b.end(); course, sub = '选择性必修', None; continue  # noqa: E702
        if line.startswith('选修'):
            b.end(); course, sub = '选修', None; continue  # noqa: E702
        if m := re.match(r'^(\d+)\.(\S.*)$', line):
            if course in (None, '选修'):
                b.end()
                continue
            cb = {'必修': 'B', '选择性必修': 'X'}.get(course, 'A')
            where = f'{element}·{sub}' if sub else element
            b.start('content', f'english.{cb}{t_no}.' + (f'{sub_n}.' if sub else '') + m.group(1), f'{course}·{where}·{m.group(1)}',
                    f'{sub or element}（{course}）{m.group(1)}', page, idx, m.group(2))
            continue
        if b.cur is not None and b.cur['kind'] == 'content' and not b.cur['text'].endswith(('。', ';')):
            b.add(idx, line)
    items = b.finish()
    for it in items:
        it['text'] = it['text'].strip()
    return items


def build(subject, pdf):
    if subject == 'chinese':
        return build_chinese(pdf), '语文', '学习任务群 1–18 的学习目标与内容、教学提示；必修与选择性必修、选修学习要求'
    if subject == 'english':
        return build_english(pdf), '英语', '必修、选择性必修（选修课程未收录）'
    if subject == 'math':
        return build_math(pdf), '数学', '必修、选择性必修（选修课程未收录）'
    cfg = CONFIGS[subject]
    return build_generic(subject, cfg['name'], pdf, cfg), cfg['name'], '必修、选择性必修（选修课程未收录）'


def main():
    subject, pdf = sys.argv[1], Path(sys.argv[2])
    items, name, scope = build(subject, pdf)
    out = {
        'subject': subject,
        'title': SOURCE_TITLE.format(name=name),
        'source_url': SOURCE_URL,
        'source_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
        'scope': scope,
        'items': items,
    }
    dest = Path(__file__).resolve().parents[1] / 'edu_core' / 'curriculum' / f'{subject}.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    kinds = {}
    for it in items:
        kinds[it['kind']] = kinds.get(it['kind'], 0) + 1
    print(f'{dest}: {len(items)} items {kinds}')


if __name__ == '__main__':
    main()
