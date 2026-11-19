"""Plumbing shared by every stage: store and model access, artifact operations, plan lookups."""

from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core.config import Settings
from edu_core.context import describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import Store, row
from edu_core.subjects import KEYED_TYPES, QUESTION_TYPES, pack
from edu_core.workflow.common import (
    Conflict,
    normalize_plan,
)


class WorkspaceBase:
    """Store, settings and model handles plus the helpers every stage uses."""

    def __init__(self, store: Store, llm, settings: Settings, rng=None):
        self.store = store
        self.llm = llm
        self.settings = settings
        self.rng = rng or random.Random()

    # ----- plumbing -----

    def _model(self):
        if self.llm is None:
            raise ModelUnavailable('未配置模型（设置 EDU_LLM_API_KEY 后重启服务）')
        return self.llm

    def _project(self, c, pid):
        p = row(c.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone())
        if p is None:
            raise KeyError(f'项目不存在：{pid}')
        p['pack'] = pack(p['subject'])
        return p

    def _project_view(self, p):
        keys = (
            'id',
            'title',
            'subject',
            'grade',
            'notes',
            'knowledge_module',
            'lesson',
            'learner_profile',
            'prev_project_id',
            'lesson_scope',
            'created_at',
        )
        return {k: p.get(k, '') for k in keys} | {
            'carryover': json.loads(p['carryover']) if p.get('carryover') else None,
            'curriculum': json.loads(p['curriculum']) if p.get('curriculum') else None,
            'has_curriculum': CUR.available(p['subject']),
            'subject_name': p['pack']['name'],
            'question_types': p['pack']['types'],
        }

    def _parallel(self, fn, items):
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(self.settings.concurrency, len(items)))) as pool:
            return list(pool.map(fn, items))

    # ----- generic artifact operations -----

    def artifact(self, aid):
        with self.store.read() as c:
            a = A.get(c, aid)
            a['stale'] = A.is_stale(c, aid)
            if a['generation_id']:
                g = row(c.execute('SELECT * FROM generations WHERE id=?', (a['generation_id'],)).fetchone())
                a['context_note'] = describe(g['context_manifest']) if g else ''
                a['generation'] = (
                    {k: g[k] for k in ('task', 'model_requested', 'model_returned', 'usage')} if g else None
                )
            history = c.execute(
                'SELECT id, version, status, origin, created_at FROM artifacts WHERE lineage_id=? ORDER BY version',
                (a['lineage_id'],),
            )
            a['history'] = [dict(h) for h in history]
            return a

    def edit(self, aid, content, teacher='teacher'):
        with self.store.tx() as c:
            src = A.get(c, aid)
            if src['type'] in ('grading', 'analysis', 'comparison'):
                raise Conflict('批改请用“改判”，分析和对比请重新生成')
            if src['type'] == 'lesson_plan' and isinstance(content, dict) and 'objectives' in content:
                old = {o['id']: o for o in src['content'].get('objectives', [])}
                for o in content['objectives'] or []:
                    prev = old.get(o.get('id'))
                    if prev:
                        for key in ('standard_ids', 'beyond_reason'):
                            if key not in o and key in prev:
                                o[key] = prev[key]
            content = self._validate_content(src['type'], content, src['content'])
            if src['type'] == 'lesson_plan':
                p = self._project(c, src['project_id'])
                CUR.plan_checks(p['subject'], content['objectives'], used=bool(content.get('curriculum')))
            return A.edit(c, aid, content, teacher=teacher)

    def confirm(self, aid, change_kind='substantive', teacher='teacher'):
        with self.store.tx() as c:
            a = A.get(c, aid)
            if a['type'] == 'question':
                rubric = self._rubric_for(c, a['lineage_id'], include_drafts=True)
                if rubric is None:
                    raise Conflict('这道题还没有评分标准')
            done = A.confirm(c, aid, change_kind=change_kind, teacher=teacher)
            if a['type'] == 'question':
                # A question and its rubric are confirmed together: grading needs both.
                rubric = self._rubric_for(c, a['lineage_id'], include_drafts=True)
                if rubric and rubric['status'] != 'confirmed':
                    A.confirm(c, rubric['id'], change_kind=change_kind, teacher=teacher)
            return done

    def _validate_content(self, type_, content, previous):
        if not isinstance(content, dict):
            raise ValueError('内容格式错误')
        merged = {**previous, **content}
        if type_ == 'lesson_plan':
            merged = normalize_plan(merged)
        elif type_ == 'question':
            if merged.get('type') not in QUESTION_TYPES:
                raise ValueError('未知题型')
            if not str(merged.get('stem', '')).strip():
                raise ValueError('题干不能为空')
            merged.setdefault('options', [])
            merged.setdefault('answer_key', '')
            merged.setdefault('accepted', [])
            merged.setdefault('reference', '')
            merged.setdefault('objective_ids', [])
            merged.setdefault('target_patterns', [])
            if merged['type'] in KEYED_TYPES and not merged['answer_key'].strip():
                raise ValueError('选择题和判断题需要填写答案')
        elif type_ == 'rubric':
            points = merged.get('points') or []
            if not points:
                raise ValueError('评分标准至少需要一个评分要点')
            clean = []
            for i, p in enumerate(points, 1):
                score = float(p.get('score', 0))
                if score <= 0 or not str(p.get('description', '')).strip():
                    raise ValueError(f'第 {i} 个评分要点缺少描述或分值')
                item = {'id': p.get('id') or f'P{i}', 'description': p['description'].strip(), 'score': score}
                for key in ('check', 'target_pattern'):
                    if p.get(key):
                        item[key] = p[key]
                clean.append(item)
            if len({p['id'] for p in clean}) != len(clean):
                raise ValueError('评分要点编号重复')
            merged['points'] = clean
            merged['total'] = round(sum(p['score'] for p in clean), 2)
        return merged

    def _next_no(self, c, pid, round_):
        return (
            c.execute(
                "SELECT COUNT(DISTINCT lineage_id) FROM artifacts WHERE project_id=? AND type='question' AND round=? AND status!='candidate'",
                (pid, round_),
            ).fetchone()[0]
            + 1
        )

    def _rubric_for(self, c, question_lineage, include_drafts=False):
        """The rubric of a question lineage: latest confirmed version, or (include_drafts) latest version."""
        statuses = ('confirmed', 'draft', 'candidate') if include_drafts else ('confirmed',)
        marks = ','.join('?' * len(statuses))
        rows = c.execute(
            f"SELECT * FROM artifacts WHERE type='rubric' AND status IN ({marks}) "
            "AND json_extract(content, '$.question_lineage')=? ORDER BY created_at DESC, version DESC LIMIT 1",
            (*statuses, question_lineage),
        ).fetchone()
        return row(rows)

    def _plan(self, c, pid, *, confirmed=True):
        """The project's lesson plan: its confirmed version, or (confirmed=False) its latest version."""
        plans = A.current(c, pid, 'lesson_plan')
        if not plans:
            return None
        return A.confirmed_in_lineage(c, plans[0]['lineage_id']) if confirmed else plans[0]

    @staticmethod
    def _goals(plan):
        """Plan objectives as handles: ``id`` is the plan artifact (for refs), ``lineage_id`` the stable objective id."""
        if not plan:
            return []
        return [{'id': plan['id'], 'lineage_id': o['id'], 'content': o} for o in plan['content']['objectives']]

    def _confirmed_objectives(self, c, pid, objective_ids=None):
        goals = self._goals(self._plan(c, pid))
        if objective_ids:
            goals = [g for g in goals if g['lineage_id'] in set(objective_ids)]
        return goals
