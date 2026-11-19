"""Plumbing shared by every stage: store and model access, artifact operations, plan lookups."""

from __future__ import annotations

import contextvars
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core.config import Settings
from edu_core.context import describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import Store, row
from edu_core.subjects import KEYED_TYPES, QUESTION_TYPES, pack
from edu_core.text import dump
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
            'course_id',
            'offering_id',
            'unit_id',
        )
        return {k: p.get(k, '') for k in keys} | {
            'carryover': json.loads(p['carryover']) if p.get('carryover') else None,
            'status_prefs': json.loads(p['status_prefs']) if p.get('status_prefs') else {},
            'curriculum': json.loads(p['curriculum']) if p.get('curriculum') else None,
            'has_curriculum': CUR.available(p['subject']),
            'subject_name': p['pack']['name'],
            'question_types': p['pack']['types'],
        }

    # ----- idempotency (design doc 8) -----

    IDEMPOTENCY_ABANDONED = 15 * 60  # a 'running' key older than this belongs to a crashed request

    def once(self, key, scope, fn):
        """Run ``fn`` once per (scope, key): a retry with the same key returns the stored result.

        Without a key the call simply runs. A key still running raises Conflict; a failed run frees the key.
        """
        if not key:
            return fn()
        key = f'{scope}:{key}'[:240]
        with self.store.tx() as c:
            r = c.execute('SELECT status, result, created_at FROM idempotency WHERE key=?', (key,)).fetchone()
            if r and r['status'] == 'done':
                return json.loads(r['result'])
            if r and r['status'] == 'running' and time.time() - r['created_at'] < self.IDEMPOTENCY_ABANDONED:
                raise Conflict('同一操作正在进行，请稍后刷新查看结果')
            c.execute(
                'INSERT OR REPLACE INTO idempotency(key,scope,status,result,created_at) VALUES(?,?,?,?,?)',
                (key, scope, 'running', '', time.time()),
            )
        try:
            result = fn()
        except BaseException:
            with self.store.tx() as c:
                c.execute('DELETE FROM idempotency WHERE key=?', (key,))
            raise
        with self.store.tx() as c:
            c.execute("UPDATE idempotency SET status='done', result=? WHERE key=?", (dump(result), key))
        return json.loads(dump(result))

    # ----- assignment scope (COURSE_WORKSPACE_DESIGN.md 10.1, 10.2) -----

    def _assignment_row(self, c, aid):
        a = row(c.execute('SELECT * FROM assignments WHERE id=?', (aid,)).fetchone())
        if a is None:
            raise KeyError(f'布置记录不存在：{aid}')
        a['roster'] = json.loads(a['roster'] or '[]')
        a['pinned'] = json.loads(a['pinned'] or '{}')
        return a

    def _scope(self, c, pid, round_, assignment_id):
        """(project, round, assignment) of student work: a class assignment, or the project's own unbound work ('')."""
        if not assignment_id:
            if pid is None:
                raise ValueError('缺少项目')
            return pid, round_ or 1, ''
        a = self._assignment_row(c, assignment_id)
        if pid and a['project_id'] != pid:
            raise Conflict('这次布置不属于当前项目')
        return a['project_id'], a['round'], a['id']

    def _scoped_questions(self, c, pid, round_, aid):
        """题号 -> question version used by this work: the pinned version of a class assignment, else the confirmed one."""
        if aid:
            a = self._assignment_row(c, aid)
            out = {}
            for pin in a['pinned'].values():
                q = A.get(c, pin['question'])
                out[q['content']['no']] = q
            return out
        return {
            q['content']['no']: A.confirmed_in_lineage(c, q['lineage_id'])
            for q in A.current(c, pid, 'question', round_=round_)
            if q['confirmed_version']
        }

    def _scoped_rubric(self, c, question_lineage, aid):
        if aid:
            pin = self._assignment_row(c, aid)['pinned'].get(question_lineage)
            return A.get(c, pin['rubric']) if pin else None
        return self._rubric_for(c, question_lineage)

    def _roster(self, c, pid, aid):
        """alias -> learner id: the assignment's roster snapshot, or the project's learners."""
        if aid:
            return {m['alias']: m['id'] for m in self._assignment_row(c, aid)['roster']}
        return {r['alias']: r['id'] for r in c.execute('SELECT id, alias FROM learners WHERE project_id=?', (pid,))}

    def _add_to_roster(self, c, pid, aid, alias):
        """A learner outside the roster: added to the class (and this assignment's snapshot) or to the project."""
        from edu_core.storage import new_id

        if not aid:
            lid = new_id('lrn')
            c.execute('INSERT INTO learners VALUES(?,?,?,?)', (lid, pid, alias, time.time()))
            return lid
        a = self._assignment_row(c, aid)
        class_id = c.execute('SELECT class_id FROM offerings WHERE id=?', (a['offering_id'],)).fetchone()[0]
        m = c.execute('SELECT id FROM class_members WHERE class_id=? AND alias=?', (class_id, alias)).fetchone()
        mid = m[0] if m else new_id('mem')
        if not m:
            c.execute(
                'INSERT INTO class_members(id,class_id,alias,source,joined_at) VALUES(?,?,?,?,?)',
                (mid, class_id, alias, 'import', time.time()),
            )
        roster = a['roster'] + [{'id': mid, 'alias': alias}]
        c.execute('UPDATE assignments SET roster=? WHERE id=?', (dump(roster), aid))
        return mid

    # ----- write-back check (design doc 8) -----

    def _basis_changed(self, c, manifest, assignment_id=''):
        """Artifacts that replaced what a model call read while it ran: ids of the newer versions.

        For a class assignment the pinned version is the basis, so a newer content version alone is not a change;
        re-pinning or correcting it is.
        """
        pins = self._assignment_row(c, assignment_id)['pinned'] if assignment_id else {}
        pinned_ids = {}
        for q_lin, pin in pins.items():
            pinned_ids[q_lin] = pin['question']
            pinned_ids[pin.get('rubric_lineage')] = pin['rubric']
        changed = []
        for m in manifest:
            if m.get('kind') not in A.TYPES or 'lineage_id' not in m:
                continue
            if m['lineage_id'] in pinned_ids:
                if pinned_ids[m['lineage_id']] != m['id']:
                    changed.append(pinned_ids[m['lineage_id']])
                continue
            newer = c.execute(
                "SELECT id FROM artifacts WHERE lineage_id=? AND status='confirmed' AND version>? "
                'ORDER BY version DESC LIMIT 1',
                (m['lineage_id'], m['version']),
            ).fetchone()
            if newer:
                changed.append(newer[0])
        return changed

    def _guard_basis(self, c, pid, artifact_id, manifest, assignment_id=''):
        """A result computed from inputs that changed meanwhile is saved as "based on an old basis": it gets an open
        stale mark and cannot be confirmed until the teacher keeps it or reruns it."""
        changed = self._basis_changed(c, manifest, assignment_id)
        if changed:
            A.open_stale(c, pid, artifact_id, changed[0])
        return bool(changed)

    def _parallel(self, fn, items):
        if not items:
            return []
        # Each item runs in a copy of the caller's context, so the current run and user reach the worker threads.
        with ThreadPoolExecutor(max_workers=max(1, min(self.settings.concurrency, len(items)))) as pool:
            futures = [pool.submit(contextvars.copy_context().run, fn, item) for item in items]
            return [f.result() for f in futures]

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
        """Adopt a version. One rule set for every caller (design doc 6.1): results with an open "依据已变化" mark
        and gradings with undetermined points or open risks cannot be adopted through this generic path."""
        with self.store.tx() as c:
            a = A.get(c, aid)
            if a['status'] != 'confirmed' and A.is_stale(c, aid):
                raise Conflict('它的依据已经变化：请先在“记录与导出”中选择保留原结果或重新生成')
            if a['type'] == 'grading' and (a['content'].get('undetermined') or a['content'].get('needs_teacher')):
                raise Conflict('这条批改还有未判定的要点或待复核的疑点，请用“改判”逐项给分')
            if a['type'] == 'question':
                rubric = self._rubric_for(c, a['lineage_id'], include_drafts=True)
                if rubric is None:
                    raise Conflict('这道题还没有评分标准')
            done = A.confirm(c, aid, change_kind=change_kind, teacher=teacher)
            if a['type'] == 'lesson_plan':
                self._on_plan_confirmed(c, done)
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
