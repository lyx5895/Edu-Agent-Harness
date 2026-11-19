"""Course objectives (COURSE_WORKSPACE_DESIGN.md 7.4): stable objectives of a course whose wording is versioned.

Plan objectives (O1, O2… inside one lesson plan) are linked to course objectives when the plan is adopted:

* a later version of the same plan keeps the link of the same plan objective;
* otherwise an objective of the course with the same wording (in the same topic first) is reused;
* otherwise a new course objective is created.

When a linked objective's wording changes, a new description version is recorded and the objective is flagged
``needs_review``: the questions mapped to it should be checked again. The teacher can relink any plan objective.
Evidence is shown per class and per assignment from confirmed analyses only; no single mastery score is computed.
"""

from __future__ import annotations

import re
import time

from edu_core import artifacts as A
from edu_core.storage import event, new_id, row
from edu_core.workflow.common import Conflict


def _norm(text):
    return re.sub(r'[\s，。、；;,.]', '', text or '')


class ObjectivesMixin:
    """Course objectives and the per-objective view."""

    def _latest_objective_version(self, c, oid):
        return row(
            c.execute(
                'SELECT * FROM objective_versions WHERE objective_id=? ORDER BY version DESC LIMIT 1', (oid,)
            ).fetchone()
        )

    def _new_objective(self, c, course_id, unit_id, text, criterion, source):
        n = c.execute('SELECT COUNT(*) FROM objectives WHERE course_id=?', (course_id,)).fetchone()[0] + 1
        oid = new_id('obj')
        c.execute(
            'INSERT INTO objectives(id,course_id,unit_id,code,created_at) VALUES(?,?,?,?,?)',
            (oid, course_id, unit_id or '', f'G{n}', time.time()),
        )
        c.execute(
            'INSERT INTO objective_versions(objective_id,version,text,success_criterion,source_artifact,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (oid, 1, text, criterion, source, time.time()),
        )
        return oid

    def _link_plan_objectives(self, c, plan):
        """Link every objective of an adopted plan to a course objective (see module docstring)."""
        p = self._project(c, plan['project_id'])
        if not p.get('course_id'):
            return []
        previous = {}
        for r in c.execute(
            'SELECT l.plan_objective_id, l.objective_id FROM objective_links l JOIN artifacts a ON a.id=l.artifact_id '
            'WHERE a.lineage_id=? AND a.id!=? ORDER BY a.version',
            (plan['lineage_id'], plan['id']),
        ):
            previous[r['plan_objective_id']] = r['objective_id']
        course_objs = []
        for r in c.execute('SELECT * FROM objectives WHERE course_id=?', (p['course_id'],)).fetchall():
            v = self._latest_objective_version(c, r['id'])
            course_objs.append((r['id'], r['unit_id'], _norm(v['text'])))
        links = []
        for o in plan['content'].get('objectives', []):
            text, crit = o['text'], o.get('success_criterion', '')
            oid = previous.get(o['id'])
            if oid is None:
                same = [x for x in course_objs if x[2] == _norm(text)]
                same.sort(key=lambda x: x[1] != p.get('unit_id', ''))  # same topic first
                oid = same[0][0] if same else None
            if oid is None:
                oid = self._new_objective(c, p['course_id'], p.get('unit_id', ''), text, crit, plan['id'])
                course_objs.append((oid, p.get('unit_id', ''), _norm(text)))
            latest = self._latest_objective_version(c, oid)
            version = latest['version']
            if _norm(latest['text']) != _norm(text) or latest['success_criterion'] != crit:
                version += 1
                c.execute(
                    'INSERT INTO objective_versions(objective_id,version,text,success_criterion,source_artifact,created_at) '
                    'VALUES(?,?,?,?,?,?)',
                    (oid, version, text, crit, plan['id'], time.time()),
                )
                if _norm(latest['text']) != _norm(text):
                    c.execute('UPDATE objectives SET needs_review=1 WHERE id=?', (oid,))
            c.execute(
                'INSERT OR REPLACE INTO objective_links(artifact_id,plan_objective_id,objective_id,version,source,created_at) '
                'VALUES(?,?,?,?,?,?)',
                (plan['id'], o['id'], oid, version, 'auto', time.time()),
            )
            links.append((o['id'], oid))
        return links

    def relink_objective(self, artifact_id, plan_objective_id, objective_id, teacher='teacher'):
        """The teacher says this plan objective is (or is not) that course objective. ``objective_id`` '' creates a
        new course objective for it."""
        with self.store.tx() as c:
            plan = A.get(c, artifact_id)
            if plan['type'] != 'lesson_plan':
                raise ValueError('只能为备课方案里的目标调整对应关系')
            p = self._project(c, plan['project_id'])
            o = next((x for x in plan['content'].get('objectives', []) if x['id'] == plan_objective_id), None)
            if o is None:
                raise KeyError(f'备课方案里没有目标 {plan_objective_id}')
            if objective_id:
                r = c.execute('SELECT course_id FROM objectives WHERE id=?', (objective_id,)).fetchone()
                if r is None or r[0] != p.get('course_id'):
                    raise Conflict('这个课程目标不属于该课程')
            else:
                objective_id = self._new_objective(
                    c, p['course_id'], p.get('unit_id', ''), o['text'], o.get('success_criterion', ''), plan['id']
                )
            version = self._latest_objective_version(c, objective_id)['version']
            c.execute(
                'INSERT OR REPLACE INTO objective_links(artifact_id,plan_objective_id,objective_id,version,source,created_at) '
                'VALUES(?,?,?,?,?,?)',
                (artifact_id, plan_objective_id, objective_id, version, 'teacher', time.time()),
            )
            event(
                c,
                plan['project_id'],
                'objective_relinked',
                {
                    'plan': artifact_id,
                    'plan_objective': plan_objective_id,
                    'objective': objective_id,
                    'teacher': teacher,
                },
            )
            return {'objective_id': objective_id}

    def mark_objective_reviewed(self, objective_id, teacher='teacher'):
        """The teacher has checked the questions mapped to an objective whose wording changed."""
        with self.store.tx() as c:
            r = c.execute('SELECT course_id FROM objectives WHERE id=?', (objective_id,)).fetchone()
            if r is None:
                raise KeyError('课程目标不存在')
            c.execute('UPDATE objectives SET needs_review=0 WHERE id=?', (objective_id,))
            event(c, r[0], 'objective_reviewed', {'objective': objective_id, 'teacher': teacher})
        return {'ok': True}

    def course_objectives(self, course_id, unit_id=None):
        with self.store.read() as c:
            self._course(c, course_id)
            sql, args = 'SELECT * FROM objectives WHERE course_id=?', [course_id]
            if unit_id is not None:
                sql, args = sql + ' AND unit_id=?', [*args, unit_id]
            out = []
            for r in c.execute(sql + ' ORDER BY created_at', args).fetchall():
                v = self._latest_objective_version(c, r['id'])
                plans = c.execute(
                    'SELECT COUNT(DISTINCT artifact_id) FROM objective_links WHERE objective_id=?', (r['id'],)
                ).fetchone()[0]
                out.append(
                    {
                        **dict(r),
                        'text': v['text'],
                        'success_criterion': v['success_criterion'],
                        'version': v['version'],
                        'plans': plans,
                    }
                )
            return out

    def _objective_uses(self, c, objective_id):
        """(project, plan artifact, plan objective id) for every adopted plan linked to this objective, latest plan
        version per lineage only."""
        best = {}
        for r in c.execute(
            'SELECT l.artifact_id, l.plan_objective_id, a.project_id, a.lineage_id, a.version, a.status '
            'FROM objective_links l JOIN artifacts a ON a.id=l.artifact_id WHERE l.objective_id=?',
            (objective_id,),
        ):
            if r['status'] != 'confirmed':
                continue
            key = r['lineage_id']
            if key not in best or r['version'] > best[key]['version']:
                best[key] = dict(r)
        return list(best.values())

    def objective_view(self, objective_id):
        """按目标查看: what taught it, which questions test it, and the evidence per class and assignment."""
        with self.store.read() as c:
            o = row(c.execute('SELECT * FROM objectives WHERE id=?', (objective_id,)).fetchone())
            if o is None:
                raise KeyError('课程目标不存在')
            versions = [
                dict(r)
                for r in c.execute(
                    'SELECT * FROM objective_versions WHERE objective_id=? ORDER BY version', (objective_id,)
                )
            ]
            uses = self._objective_uses(c, objective_id)
            plans, questions, classes = [], [], {}
            for u in uses:
                p = self._project(c, u['project_id'])
                plans.append(
                    {
                        'project_id': p['id'],
                        'title': p['title'],
                        'plan_version': u['version'],
                        'plan_objective': u['plan_objective_id'],
                    }
                )
                qs = [
                    A.confirmed_in_lineage(c, q['lineage_id'])
                    for q in A.current(c, p['id'], 'question')
                    if q['confirmed_version'] and u['plan_objective_id'] in (q['content'].get('objective_ids') or [])
                ]
                questions += [
                    {
                        'project_id': p['id'],
                        'no': q['content']['no'],
                        'round': q['round'],
                        'stem': q['content']['stem'][:120],
                    }
                    for q in qs
                    if q
                ]
                for a in self._assignment_views(c, project_id=p['id']):
                    cls = classes.setdefault(a['offering_id'], {'class_name': a['class_name'], 'assignments': []})
                    entry = {
                        'assignment': a['title'],
                        'round': a['round'],
                        'status': a['status_label'],
                        'tested': False,
                        'evidence': None,
                    }
                    pinned = self._assignment_row(c, a['id'])['pinned']
                    entry['tested'] = any(
                        u['plan_objective_id'] in (A.get(c, pin['question'])['content'].get('objective_ids') or [])
                        for pin in pinned.values()
                    )
                    done = [
                        A.confirmed_in_lineage(c, x['lineage_id'])
                        for x in A.current(c, p['id'], 'analysis', assignment=a['id'])
                        if x['confirmed_version']
                    ]
                    done = [x for x in done if x and x['content'].get('mode') == 'evidence']
                    if done:
                        ct = done[-1]['content']
                        stat = next(
                            (s for s in ct.get('objective_stats') or [] if s['id'] == u['plan_objective_id']), None
                        )
                        if stat:
                            subs = A.current(c, p['id'], 'submission', assignment=a['id'])
                            entry['evidence'] = {
                                'rate': stat['得分率'],
                                'full': stat['全部得分人数'],
                                'learners': stat['人数'],
                                'partial': bool(ct.get('partial')),
                                'coverage': (ct.get('coverage') or {}).get('label', ''),
                                # Imported answers say nothing about help received: never counted as independent.
                                'help': '未知（导入的作答）' if subs else '无数据',
                            }
                    cls['assignments'].append(entry)
            for cls in classes.values():
                tested = [x for x in cls['assignments'] if x['tested']]
                with_ev = [x for x in tested if x['evidence']]
                cls['state'] = '无数据' if not tested else ('有证据' if with_ev else '已测、待确认分析')
                if len(with_ev) > 1:
                    cls['state'] = '多次测评，分别看各次结果'
        return {
            **o,
            'text': versions[-1]['text'],
            'versions': versions,
            'plans': plans,
            'questions': questions,
            'classes': list(classes.values()),
            'note': '只汇总已确认的分析；不同难度、帮助条件的结果分别列出，不合成掌握分数。',
        }
