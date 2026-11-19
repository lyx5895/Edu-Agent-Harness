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


def main():
    subject, pdf = sys.argv[1], Path(sys.argv[2])
    if subject != 'math':
        raise SystemExit('only math is supported in the pilot')
    items = build_math(pdf)
    out = {
        'subject': subject,
        'title': SOURCE_TITLE.format(name='数学'),
        'source_url': SOURCE_URL,
        'source_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(),
        'scope': '必修、选择性必修（选修课程未收录）',
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
