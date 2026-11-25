"""Curriculum standards (课程标准) as items, matching, and requirement-level checks.

Two sets of standards:

- 普通高中 (grades 10–12, and the default when no grade is known): ``<subject>.json``, built by
  ``scripts/edu_build_curriculum.py`` from the PDFs' text layer; every item text is a verbatim excerpt.
- 义务教育 (grades 1–9): ``compulsory/<subject>.json``, built by ``scripts/edu_build_curriculum_compulsory.py`` from
  MinerU's OCR of the scanned PDFs (``text_source`` says so). Every item has ``grades`` [lo, hi]; only the items
  whose range meets the class's grade are loaded, so a 初二 lesson never matches a 一年级 item.

Callers pass a *source key* from :func:`source` (``math`` or ``math@7-9``) wherever a subject used to go.
"""

from __future__ import annotations

import json
import math
import re
from functools import cache
from pathlib import Path

DIR = Path(__file__).parent

# Requirement levels of the verbs the standards use. 能/会 are left out: they appear at every level.
LEVELS = (
    (3, ('掌握', '熟练', '灵活运用')),
    (2, ('理解',)),
    (1, ('了解', '知道', '体会', '感悟', '感受', '经历')),
)
LEVEL_NAME = {1: '了解', 2: '理解', 3: '掌握'}

_CN = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_GRADE_WORDS = [
    (re.compile(r'高([一二三])'), lambda m: (9 + _CN[m.group(1)],) * 2),
    (re.compile(r'初([一二三])'), lambda m: (6 + _CN[m.group(1)],) * 2),
    (re.compile(r'([一二三四五六七八九])年级'), lambda m: (_CN[m.group(1)],) * 2),
    (re.compile(r'(?<!\d)([1-9])\s*年级'), lambda m: (int(m.group(1)),) * 2),
    (re.compile(r'高中'), lambda m: (10, 12)),
    (re.compile(r'初中'), lambda m: (7, 9)),
    (re.compile(r'小学'), lambda m: (1, 6)),
]


def grade_range(text: str) -> tuple[int, int] | None:
    """``初二`` → (8, 8), ``五年级`` → (5, 5), ``高一`` → (10, 10), ``小学`` → (1, 6); None when no grade is named."""
    for pattern, value in _GRADE_WORDS:
        if m := pattern.search(text or ''):
            return value(m)
    return None


def source(subject: str, *grade_texts: str) -> str:
    """Which standard a lesson is matched against: the first of ``grade_texts`` that names a grade decides (pass
    the grade field before titles). Grades 1–9 → 义务教育 (``math@7-9``), otherwise 普通高中 (``math``)."""
    for text in grade_texts:
        if r := grade_range(text):
            lo, hi = r
            return f'{subject}@{lo}-{min(hi, 9)}' if lo <= 9 else subject
    return subject


def stage_name(key: str) -> str:
    """``math@7-9`` → ``义务教育·7～9年级``; a senior-high key → ``普通高中``."""
    _, _, grades = key.partition('@')
    if not grades:
        return '普通高中'
    lo, hi = grades.split('-')
    return f'义务教育·{lo}年级' if lo == hi else f'义务教育·{lo}～{hi}年级'


@cache
def _read(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


@cache
def load(key: str) -> dict | None:
    m = re.fullmatch(r'([a-z]+)(?:@([1-9])-([1-9]))?', key or '')
    if not m:
        return None  # only packaged subjects have standards; custom subject names never become paths
    subject, lo, hi = m.group(1), m.group(2), m.group(3)
    if lo is None:
        raw = _read(DIR / f'{subject}.json')
        if raw is None:
            return None
        data = dict(raw)
    else:
        raw = _read(DIR / 'compulsory' / f'{subject}.json')
        if raw is None:
            return None
        lo, hi = int(lo), int(hi)
        items = [it for it in raw['items'] if it['grades'][0] <= hi and lo <= it['grades'][1]]
        if not any(it['kind'] == 'content' for it in items):
            return None  # e.g. 一年级英语: the standard has only 教学提示 for 预备级, nothing to plan against
        data = {**raw, 'items': items, 'grades': [lo, hi]}
    data['key'] = key
    data['stage_name'] = stage_name(key)
    data['by_id'] = {it['id']: it for it in data['items']}
    data['idf'] = _idf([it for it in data['items'] if it['kind'] == 'content'])
    return data


def available(key: str) -> bool:
    return load(key) is not None


def short_label(item: dict) -> str:
    """How a citation is shown, e.g. ``对数函数②`` or ``1.8 辛亥革命与中华民国的建立``."""
    return item.get('short') or item['label'].split('·')[-1]


_PARTICLES = re.compile('[的之与和及、]')


def unit_of(item: dict) -> str:
    """The heading path above an item (必修·函数·幂函数、指数函数、对数函数), i.e. its unit or module."""
    return item['label'].rsplit('·', 1)[0]


def candidates(key: str, query: str, n: int = 24) -> list[dict]:
    """What a model may choose the standards from: the best lexical matches plus every item of the units they are
    in. Characters alone cannot tell that k 近邻 is one of the 核心算法 of 人工智能初步, but its unit is found."""
    data = load(key)
    found = search(key, query, k=8)
    if not data or not found:
        return []
    units = list(dict.fromkeys(unit_of(it) for it in found[:3]))
    out = {it['id']: it for it in found}
    for u in units:
        for it in data['items']:
            if it['kind'] == 'content' and unit_of(it) == u:
                out.setdefault(it['id'], it)
    return list(out.values())[:n]


def _norm(text: str) -> str:
    return text.replace('图像', '图象')  # the standards write 图象


def _grams(text: str) -> set[str]:
    text = re.sub(r'[^一-鿿A-Za-z0-9]', ' ', _norm(text))
    out = set()
    for chunk in text.split():
        if re.fullmatch(r'[A-Za-z0-9]+', chunk):
            out.add(chunk.lower())
        else:
            for n in (2, 3, 4):  # longer runs reward matching a whole term (对数函数) over a common word (概念)
                out.update(chunk[i : i + n] for i in range(len(chunk) - n + 1))
    return out


def _idf(items):
    df: dict[str, int] = {}
    for it in items:
        for g in _grams(short_label(it) + it['text']):
            df[g] = df.get(g, 0) + 1
    n = len(items) or 1
    return {g: math.log((n + 1) / (c + 0.5)) for g, c in df.items()}


def _score(data, query_grams, item):
    """Own heading counts double, the text once, the enclosing unit/theme names only a little."""
    own, text = _grams(short_label(item)), _grams(item['text'])
    # the enclosing headings: math keeps unit/theme fields, the other subjects carry them in the label
    outer = _grams(' '.join(x for x in (item.get('unit'), item.get('theme')) if x) or item['label'])
    idf = data['idf']
    total = 0.0
    for g in query_grams:
        w = 2.0 if g in own else 1.0 if g in text else 0.3 if g in outer else 0.0
        total += idf.get(g, 0.0) * w
    return total


def search(key: str, query: str, k: int = 6, kinds=('content',)) -> list[dict]:
    """Best matching items for the course info. Deterministic and explainable (character bigrams, IDF-weighted)."""
    data = load(key)
    if not data or not query.strip():
        return []
    q = _grams(query)
    # A whole term the teacher typed (e.g. 相互作用与运动定律, 对数) that names an item's heading path is strong
    # evidence. Particles are dropped on both sides: 函数的概念与性质 is the heading 函数概念与性质.
    terms = [_PARTICLES.sub('', t) for t in re.split(r'[\s,，;；:：()（）]+', _norm(query))]
    terms = [t for t in terms if len(t) >= 2]

    def total(it):
        base = _score(data, q, it)
        path = _PARTICLES.sub('', _norm(it['label']))
        bonus = sum(sum(data['idf'].get(g, 0.0) for g in _grams(t)) * 0.5 for t in terms if t in path)
        return base + bonus if base > 0 else 0.0

    scored = [(s, it) for it in data['items'] if it['kind'] in kinds and (s := total(it)) > 0]
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return []
    top = scored[0][0]
    return [{**it, 'score': round(s, 2)} for s, it in scored[:k] if s >= 0.35 * top]


def _groups(item: dict) -> list[str]:
    """Which 学业要求 / 教学提示 go with an item: the theme in its id (B2 = 必修 主题二), or the ``groups`` a
    compulsory item lists (its 学段·领域, 学习任务群 or 英语级别)."""
    return item.get('groups') or [item['id'].split('.')[1]]


def context(key: str, ids: list[str], query: str = '') -> dict:
    """What the model sees: the chosen 内容要求, plus the 学业要求 and 教学提示 of the same themes that match best."""
    data = load(key)
    if not data:
        return {'content': [], 'requirements': [], 'hints': []}
    content = [data['by_id'][i] for i in ids if i in data['by_id'] and data['by_id'][i]['kind'] == 'content']
    prefixes = {g for it in content for g in _groups(it)}
    q = _grams(query + ''.join(it['label'] + it['text'] for it in content))

    def best(kind, n):
        pool = [it for it in data['items'] if it['kind'] == kind and prefixes.intersection(_groups(it))]
        pool.sort(key=lambda it: -_score(data, q, it))
        return pool[:n]

    return {'content': content, 'requirements': best('requirement', 2), 'hints': best('hint', 3)}


def level(text: str) -> int:
    """Highest requirement level named in ``text`` (0 when it names none)."""
    for lv, verbs in LEVELS:
        if any(v in text for v in verbs):
            return lv
    return 0


def plan_checks(key: str, objectives: list[dict], used: bool) -> None:
    """Annotate objectives in place with ``checks``: missing citation, or a requirement above the cited items."""
    data = load(key)
    for o in objectives:
        checks = []
        cited = [data['by_id'][i] for i in o.get('standard_ids', []) if data and i in data['by_id']]
        if used and not cited and not o.get('beyond_reason') and not o.get('inferred'):
            checks.append('未对应课标')
        if cited:
            allowed = max(level(it['text']) for it in cited)
            asked = level(o['text'])
            if allowed and asked > allowed:
                checks.append(f'要求高于课标：课标为“{LEVEL_NAME[allowed]}”，目标写到了“{LEVEL_NAME[asked]}”')
        if checks:
            o['checks'] = checks
        else:
            o.pop('checks', None)
        if cited:  # what the citation says, kept with the plan so the record stays readable
            o['standard_refs'] = [
                {'id': it['id'], 'label': short_label(it), 'text': it['text'], 'page': it['page']} for it in cited
            ]
        else:
            o.pop('standard_refs', None)
