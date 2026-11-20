"""Curriculum standards (课程标准) as verified items, matching, and requirement-level checks.

Item files are built by ``scripts/edu_build_curriculum.py`` from the official MOE PDFs; every item text is a
verbatim excerpt. Only subjects with a file are supported (pilot: math).
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


@cache
def load(subject: str) -> dict | None:
    if not re.fullmatch(r'[a-z]+', subject or ''):
        return None  # only packaged subjects have standards; custom subject names never become paths
    path = DIR / f'{subject}.json'
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    data['by_id'] = {it['id']: it for it in data['items']}
    data['idf'] = _idf([it for it in data['items'] if it['kind'] == 'content'])
    return data


def available(subject: str) -> bool:
    return load(subject) is not None


def short_label(item: dict) -> str:
    """How a citation is shown, e.g. ``对数函数②`` or ``1.8 辛亥革命与中华民国的建立``."""
    return item.get('short') or item['label'].split('·')[-1]


_PARTICLES = re.compile('[的之与和及、]')


def unit_of(item: dict) -> str:
    """The heading path above an item (必修·函数·幂函数、指数函数、对数函数), i.e. its unit or module."""
    return item['label'].rsplit('·', 1)[0]


def candidates(subject: str, query: str, n: int = 24) -> list[dict]:
    """What a model may choose the standards from: the best lexical matches plus every item of the units they are
    in. Characters alone cannot tell that k 近邻 is one of the 核心算法 of 人工智能初步, but its unit is found."""
    data = load(subject)
    found = search(subject, query, k=8)
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


def search(subject: str, query: str, k: int = 6, kinds=('content',)) -> list[dict]:
    """Best matching items for the course info. Deterministic and explainable (character bigrams, IDF-weighted)."""
    data = load(subject)
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


def context(subject: str, ids: list[str], query: str = '') -> dict:
    """What the model sees: the chosen 内容要求, plus the 学业要求 and 教学提示 of the same themes that match best."""
    data = load(subject)
    if not data:
        return {'content': [], 'requirements': [], 'hints': []}
    content = [data['by_id'][i] for i in ids if i in data['by_id'] and data['by_id'][i]['kind'] == 'content']
    prefixes = {it['id'].split('.')[1] for it in content}  # e.g. B2 = 必修 主题二
    q = _grams(query + ''.join(it['label'] + it['text'] for it in content))

    def best(kind, n):
        pool = [it for it in data['items'] if it['kind'] == kind and it['id'].split('.')[1] in prefixes]
        pool.sort(key=lambda it: -_score(data, q, it))
        return pool[:n]

    return {'content': content, 'requirements': best('requirement', 2), 'hints': best('hint', 3)}


def level(text: str) -> int:
    """Highest requirement level named in ``text`` (0 when it names none)."""
    for lv, verbs in LEVELS:
        if any(v in text for v in verbs):
            return lv
    return 0


def plan_checks(subject: str, objectives: list[dict], used: bool) -> None:
    """Annotate objectives in place with ``checks``: missing citation, or a requirement above the cited items."""
    data = load(subject)
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
