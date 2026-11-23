"""Helpers shared by several stages: rubric and question builders, draft filtering, plan normalization."""

from __future__ import annotations

import json
import re

from edu_core import artifacts as A
from edu_core import tools
from edu_core.subjects import KEYED_TYPES
from edu_core.workflow.schemas import (
    PointDraft,
    QuestionDraft,
)

Conflict = A.Conflict


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _rubric_from(points: list[PointDraft], question_lineage: str, target_pattern=None):
    rows = []
    for i, p in enumerate(points, 1):
        point = {'id': f'P{i}', 'description': p.description, 'score': float(p.score)}
        if p.check_kind != 'none' and p.check_expected.strip():
            point['check'] = {'kind': p.check_kind, 'expected': p.check_expected.strip()}
        if target_pattern:
            point['target_pattern'] = target_pattern
        rows.append(point)
    return {'question_lineage': question_lineage, 'total': round(sum(r['score'] for r in rows), 2), 'points': rows}


def _key_problem(d):
    """Structural checks the server can make before a keyed question is stored: a wrong key would otherwise become
    the grading truth without anyone noticing."""
    if d.type in ('single_choice', 'multi_choice'):
        n = len(d.options)
        if n < 2 or n > 8:
            return f'选择题应有 2 到 8 个选项（现有 {n} 个）'
        if len({o.strip() for o in d.options}) != n:
            return '选项有重复'
        letters = tools.choice_letters(d.answer_key)
        if not letters:
            return '答案键为空或不是选项字母'
        if not letters <= set('ABCDEFGH'[:n]):
            return '答案键超出选项范围'
        if d.type == 'multi_choice' and len(letters) < 2:
            return '多选题至少需要两个正确选项'
    elif d.type == 'true_false':
        if d.answer_key.strip().lower() not in ('对', '错', '正确', '错误', 'true', 'false', 't', 'f'):
            return '判断题答案应为对或错'
    elif d.type == 'fill_blank' and not (d.answer_key.strip() or any(a.strip() for a in d.accepted)):
        return '填空题缺少答案'
    return ''


def usable_drafts(drafts, allowed, n_patterns=None):
    """Keep drafts the server can use; keyed questions without points get a default point. Returns (kept, dropped)."""
    kept, dropped = [], []
    for d in drafts:
        if d.type not in allowed:
            dropped.append(f'题型 {d.type} 不在允许范围内：{d.stem[:30]}')
            continue
        if n_patterns is not None and not 0 <= d.target_pattern_index < n_patterns:
            dropped.append(f'没有对应到共性问题：{d.stem[:30]}')
            continue
        problem = _key_problem(d)
        if problem:
            dropped.append(f'{problem}：{d.stem[:30]}')
            continue
        if not d.points:
            if d.type in KEYED_TYPES and d.answer_key.strip():
                d = d.model_copy(
                    update={
                        'points': [PointDraft(description='答案正确', score=2, check_kind='none', check_expected='')]
                    }
                )
            else:
                dropped.append(f'缺少评分要点：{d.stem[:30]}')
                continue
        kept.append(d)
    return kept, dropped


def _question_content(d: QuestionDraft, no: int, round_: int, objective_ids, target_patterns=()):
    keyed = d.type in KEYED_TYPES or d.type == 'fill_blank'
    return {
        'no': no,
        'type': d.type,
        'stem': d.stem.strip(),
        'options': d.options if d.type in ('single_choice', 'multi_choice') else [],
        'answer_key': d.answer_key.strip() if keyed else '',
        'accepted': [a for a in d.accepted if a.strip()] if d.type == 'fill_blank' else [],
        'reference': d.reference.strip(),
        'objective_ids': list(objective_ids),
        'target_patterns': list(target_patterns),
        'round': round_,
    }


def normalize_plan(plan):
    """Validate a lesson plan; objectives keep stable ids (O1, O2, ...) across edits, new ones get the next id."""
    objectives, used = [], set()
    raw = plan.get('objectives') or []
    taken = [int(m.group(1)) for o in raw if (m := re.fullmatch(r'O(\d+)', str(o.get('id', ''))))]
    nxt = max(taken, default=0)
    for o in raw:
        text = str(o.get('text', '')).strip()
        if not text:
            continue
        oid = str(o.get('id') or '')
        if not re.fullmatch(r'O\d+', oid) or oid in used:
            nxt += 1
            oid = f'O{nxt}'
        used.add(oid)
        item = {'id': oid, 'text': text[:200], 'success_criterion': str(o.get('success_criterion', '')).strip()[:300]}
        if o.get('inferred'):
            item['inferred'] = True
        ids = [str(x) for x in o.get('standard_ids') or [] if str(x).strip()]
        if ids:
            item['standard_ids'] = ids
        if str(o.get('beyond_reason') or '').strip():
            item['beyond_reason'] = str(o['beyond_reason']).strip()[:200]
        objectives.append(item)
    if not objectives:
        raise ValueError('备课方案至少需要一条教学目标')
    activities = []
    for a in plan.get('activities') or []:
        name = str(a.get('name', '')).strip()
        if not name:
            continue
        activities.append(
            {
                'name': name[:60],
                'minutes': max(0, min(int(a.get('minutes') or 0), 90)),
                'description': str(a.get('description', '')).strip()[:400],
                'objective_ids': [x for x in a.get('objective_ids', []) if x in used],
                **({'evidence': str(a['evidence']).strip()[:200]} if str(a.get('evidence') or '').strip() else {}),
            }
        )

    def items(key, fields, limits):
        """Structured sections (例题, 当堂检测): keep the given text fields, drop empty entries."""
        out = []
        for x in plan.get(key) or []:
            if not isinstance(x, dict):
                continue
            entry = {f: str(x.get(f, '')).strip()[:n] for f, n in zip(fields, limits)}
            if not entry[fields[0]]:
                continue
            if 'objective_ids' in x:
                entry['objective_ids'] = [i for i in x.get('objective_ids') or [] if i in used]
            out.append(entry)
        return out

    def lines(key):
        return [str(x).strip() for x in plan.get(key) or [] if str(x).strip()]

    out = {
        'objectives': objectives,
        'key_points': lines('key_points'),
        'difficulties': lines('difficulties'),
        'activities': activities,
        'notes': str(plan.get('notes', '')).strip()[:600],
    }
    # besides goals and steps, what is needed to teach from: worked examples and a short exit check
    extra = {
        'examples': items('examples', ('problem', 'solution'), (1500, 2500)),
        'exit_check': items('exit_check', ('question', 'answer'), (800, 800)),
    }
    out.update({k: v for k, v in extra.items() if v})
    if plan.get('curriculum'):
        out['curriculum'] = plan['curriculum']  # which standard items the plan was drafted against
    return out


def evidence_checks(plan):
    """Mark objectives that no step collects evidence for (达成标准 with nowhere to come from). Plans without
    activities (objectives inferred for a practice) are not lesson plans in this sense and are left alone."""
    if not plan.get('activities'):
        return
    collected = {i for a in plan['activities'] if a.get('evidence') for i in a.get('objective_ids', [])}
    collected |= {i for x in plan.get('exit_check', []) for i in x.get('objective_ids', [])}
    for o in plan['objectives']:
        checks = [c for c in o.get('checks', []) if c != NO_EVIDENCE]
        if o['id'] not in collected:
            checks.append(NO_EVIDENCE)
        if checks:
            o['checks'] = checks
        else:
            o.pop('checks', None)


NO_EVIDENCE = '课上没有安排检验这条目标的环节'

WEAK_MATCH = 6.0  # below this best score the automatic curriculum pick is left to the teacher
