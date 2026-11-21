"""Assignments (布置记录): one class doing one round of a task's questions (COURSE_WORKSPACE_DESIGN.md 5.3, 6.3, 10).

An assignment pins the confirmed question and rubric versions it was given and snapshots the class roster.
Submissions, gradings, analyses, patterns, comparisons and 讲评稿 made for it carry its id, so two classes using
the same questions never share student data. Later content versions do not change an assignment: the teacher
either corrects this assignment's rubric (and regrades) or re-pins it to the newer version.
"""

from __future__ import annotations

import time

from edu_core import artifacts as A
from edu_core.storage import event, new_id
from edu_core.text import dump
from edu_core.workflow.common import Conflict
from edu_core.workflow.scope import Scope

STATUS_LABEL = {
    'waiting': '待收集',
    'partial': '部分收到',
    'grading': '待批改',
    'review': '待复核',
    'done': '已完成',
}


class AssignmentsMixin:
    """布置记录: assign to a class, status, rubric correction and re-pinning."""

    def assign(self, pid, offering_id, round_=1, title='', parent_id='', teacher='teacher'):
        """布置 the confirmed questions of ``round_`` to a class: pin their versions and snapshot the roster."""
        with self.store.tx() as c:
            p = self._project(c, pid)
            o = self._offering(c, offering_id)
            if p.get('course_id') != o['course_id']:
                raise Conflict('这个班没有关联到该任务所在的课程')
            pinned = {}
            for q in A.current(c, pid, 'question', round_=round_):
                conf = A.confirmed_in_lineage(c, q['lineage_id'])
                if not conf:
                    continue
                rubric = self._rubric_for(c, q['lineage_id'])
                if rubric is None:
                    raise Conflict(f'第 {conf["content"]["no"]} 题还没有已确认的评分标准')
                pinned[q['lineage_id']] = {
                    'question': conf['id'],
                    'rubric': rubric['id'],
                    'rubric_lineage': rubric['lineage_id'],
                    'no': conf['content']['no'],
                }
            if not pinned:
                raise Conflict(f'第 {round_} 轮还没有已确认的题目，确认后才能布置')
            if parent_id:
                parent = self._assignment_row(c, parent_id)
                if parent['offering_id'] != offering_id:
                    raise Conflict('讲评后再练只能布置给同一个班')
            roster = [
                {'id': m['id'], 'alias': m['alias']}
                for m in c.execute(
                    'SELECT id, alias FROM class_members WHERE class_id=? AND left_at IS NULL ORDER BY joined_at',
                    (o['class_id'],),
                )
            ]
            aid = new_id('asg')
            if not title.strip():
                title = p['title'] if round_ == 1 else f'{p["title"]}｜第 {round_} 轮'
            c.execute(
                'INSERT INTO assignments(id,project_id,offering_id,round,parent_id,title,status,roster,pinned,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (
                    aid,
                    pid,
                    offering_id,
                    round_,
                    parent_id or '',
                    title.strip(),
                    'assigned',
                    dump(roster),
                    dump(pinned),
                    time.time(),
                ),
            )
            event(
                c,
                pid,
                'assigned',
                {
                    'assignment_id': aid,
                    'offering_id': offering_id,
                    'round': round_,
                    'questions': len(pinned),
                    'teacher': teacher,
                },
            )
            return self._assignment_views(c, assignment_id=aid)[0]

    def _assignment_views(self, c, *, project_id=None, offering_id=None, assignment_id=None):
        sql, args = 'SELECT id FROM assignments WHERE 1=1', []
        for col, val in (('project_id', project_id), ('offering_id', offering_id), ('id', assignment_id)):
            if val is not None:
                sql += f' AND {col}=?'
                args.append(val)
        out = []
        for r in c.execute(sql + ' ORDER BY created_at', args).fetchall():
            a = self._assignment_row(c, r['id'])
            o = self._offering(c, a['offering_id'])
            subs = A.current(c, a['project_id'], 'submission', assignment=a['id'])
            gradings = {
                g['content']['submission_lineage']: g
                for g in A.current(c, a['project_id'], 'grading', assignment=a['id'])
            }
            collection = Scope.from_row(self, a).coverage(c, subs)
            received = len({s['content']['learner_id'] for s in subs})
            ungraded = sum(1 for s in subs if s['lineage_id'] not in gradings)
            queue = sum(1 for g in gradings.values() if g['status'] == 'draft' and g['content'].get('needs_teacher'))
            unconfirmed = sum(1 for g in gradings.values() if g['status'] != 'confirmed' or g['stale'])
            if not subs:
                status = 'waiting'
            elif ungraded:
                status = 'grading'
            elif queue or unconfirmed:
                status = 'review'
            elif collection['missing']:
                status = 'partial'
            else:
                status = 'done'
            out.append(
                {
                    'id': a['id'],
                    'project_id': a['project_id'],
                    'offering_id': a['offering_id'],
                    'class_name': o['class_name'],
                    'round': a['round'],
                    'parent_id': a['parent_id'],
                    'title': a['title'],
                    'created_at': a['created_at'],
                    'roster': len(a['roster']),
                    'questions': len(a['pinned']),
                    'received': received,
                    'submissions': len(subs),
                    **collection,
                    'ungraded': ungraded,
                    'review_queue': queue,
                    'status': status,
                    'status_label': STATUS_LABEL[status],
                    'updates': self._updates_available(c, a),
                }
            )
        return out

    def assignments(self, pid=None, offering_id=None):
        with self.store.read() as c:
            return self._assignment_views(c, project_id=pid, offering_id=offering_id)

    def assignment(self, aid):
        with self.store.read() as c:
            views = self._assignment_views(c, assignment_id=aid)
            if not views:
                raise KeyError(f'布置记录不存在：{aid}')
            a = self._assignment_row(c, aid)
            view = views[0]
            view['pinned'] = [
                {
                    'question_lineage': q_lin,
                    'no': pin.get('no'),
                    'question_version': A.get(c, pin['question'])['version'],
                    'rubric_version': A.get(c, pin['rubric'])['version'],
                }
                for q_lin, pin in sorted(a['pinned'].items(), key=lambda kv: kv[1].get('no') or 0)
            ]
            view['members'] = a['roster']
            return view

    @staticmethod
    def _updates_available(c, a):
        """Pinned questions whose content has a newer confirmed version: 新版可用 (the assignment keeps its own)."""
        out = []
        for q_lin, pin in a['pinned'].items():
            q_new = A.confirmed_in_lineage(c, q_lin)
            r_new = A.confirmed_in_lineage(c, pin.get('rubric_lineage')) if pin.get('rubric_lineage') else None
            changed = []
            if q_new and q_new['id'] != pin['question']:
                changed.append('question')
            if r_new and r_new['id'] != pin['rubric']:
                changed.append('rubric')
            # The teacher chose to keep the original for exactly these versions: no reminder until a newer one.
            kept = pin.get('kept') or {}
            if (
                changed
                and kept.get('question') == (q_new or {}).get('id')
                and kept.get('rubric') == (r_new or {}).get('id')
            ):
                changed = []
            if changed:
                out.append({'question_lineage': q_lin, 'no': pin.get('no'), 'changed': changed})
        return out

    def keep_pinned(self, aid, question_lineage, teacher='teacher'):
        """保持原版本: the assignment deliberately keeps its pinned question and rubric. The newer versions that exist
        now stop being reported; a later version is reported again."""
        with self.store.tx() as c:
            a, pin = self._pin(c, aid, question_lineage)
            q = A.confirmed_in_lineage(c, question_lineage)
            r = A.confirmed_in_lineage(c, pin.get('rubric_lineage')) if pin.get('rubric_lineage') else None
            pin['kept'] = {'question': (q or {}).get('id'), 'rubric': (r or {}).get('id')}
            c.execute('UPDATE assignments SET pinned=? WHERE id=?', (dump(a['pinned']), aid))
            event(
                c,
                a['project_id'],
                'assignment_kept_version',
                {'assignment_id': aid, 'question_lineage': question_lineage, 'teacher': teacher},
            )
            return {'ok': True}

    def _pin(self, c, aid, question_lineage):
        a = self._assignment_row(c, aid)
        pin = a['pinned'].get(question_lineage)
        if pin is None:
            raise Conflict('这道题不在这次布置里')
        return a, pin

    def _others_on(self, c, rubric_id, except_id):
        """Other assignments still pinned to this rubric version: listed for the teacher, never changed silently."""
        out = []
        for r in c.execute('SELECT id FROM assignments WHERE id!=?', (except_id,)).fetchall():
            a = self._assignment_row(c, r['id'])
            if any(pin['rubric'] == rubric_id for pin in a['pinned'].values()):
                o = self._offering(c, a['offering_id'])
                out.append({'id': a['id'], 'title': a['title'], 'class_name': o['class_name'], 'round': a['round']})
        return out

    def _flag_assignment(self, c, a, question_lineage, cause_id):
        """Open stale marks on this assignment's gradings of one question and on what was derived from them."""
        flagged = 0
        for g in A.current(c, a['project_id'], 'grading', assignment=a['id']):
            if g['content']['question_lineage'] == question_lineage:
                A.open_stale(c, a['project_id'], g['id'], cause_id)
                flagged += 1
        if flagged:
            for t in ('analysis', 'problem_pattern', 'comparison', 'review_plan'):
                for x in A.current(c, a['project_id'], t, assignment=a['id']):
                    A.open_stale(c, a['project_id'], x['id'], cause_id)
        return flagged

    def correction_impact(self, aid, question_lineage):
        """Preview of 更正本次评分依据: gradings to redo here, and other assignments that use the same version."""
        with self.store.read() as c:
            a, pin = self._pin(c, aid, question_lineage)
            gradings = [
                g
                for g in A.current(c, a['project_id'], 'grading', assignment=aid)
                if g['content']['question_lineage'] == question_lineage
            ]
            return {
                'rubric': A.get(c, pin['rubric']),
                'gradings': len(gradings),
                'confirmed_gradings': sum(1 for g in gradings if g['confirmed_version']),
                'other_assignments': self._others_on(c, pin['rubric'], aid),
            }

    def correct_rubric(self, aid, question_lineage, content, note='', teacher='teacher'):
        """更正本次评分依据 (design doc 6.3): a new rubric version that only this assignment switches to.

        Old gradings stay; this assignment's gradings of the question get an open stale mark, so the next grading
        run redoes them and the new results become valid once confirmed. Other assignments pinned to the old version
        are returned for the teacher to decide one by one.
        """
        with self.store.tx() as c:
            a, pin = self._pin(c, aid, question_lineage)
            base = A.get(c, pin['rubric'])
            content = self._validate_content('rubric', content, base['content'])
            if A.sha_of(content) == base['content_sha']:
                raise ValueError('评分标准没有变化')
            new = A.create(
                c,
                a['project_id'],
                'rubric',
                content,
                origin='teacher',
                refs=[(r['ref_id'], r['role']) for r in base['refs']],
                lineage_id=base['lineage_id'],
                status='confirmed',
                created_by=teacher,
                round_=base['round'],
                edit_of=base['id'],
                change_kind='substantive',
            )
            a['pinned'][question_lineage] = {**pin, 'rubric': new['id']}
            c.execute('UPDATE assignments SET pinned=? WHERE id=?', (dump(a['pinned']), aid))
            flagged = self._flag_assignment(c, a, question_lineage, new['id'])
            others = self._others_on(c, base['id'], aid)
            event(
                c,
                a['project_id'],
                'rubric_corrected',
                {'assignment_id': aid, 'from': base['id'], 'to': new['id'], 'note': note, 'teacher': teacher},
            )
            return {'rubric': new, 'regrade': flagged, 'other_assignments': others}

    def repin(self, aid, question_lineage, teacher='teacher'):
        """Switch one assignment to the latest confirmed question and rubric of a lineage, then regrade it."""
        with self.store.tx() as c:
            a, pin = self._pin(c, aid, question_lineage)
            q = A.confirmed_in_lineage(c, question_lineage)
            rubric = self._rubric_for(c, question_lineage)
            if q is None or rubric is None:
                raise Conflict('这道题没有已确认的新版本')
            if q['id'] == pin['question'] and rubric['id'] == pin['rubric']:
                return {'changed': False, 'regrade': 0}
            a['pinned'][question_lineage] = {
                **pin,
                'question': q['id'],
                'rubric': rubric['id'],
                'rubric_lineage': rubric['lineage_id'],
            }
            c.execute('UPDATE assignments SET pinned=? WHERE id=?', (dump(a['pinned']), aid))
            flagged = self._flag_assignment(
                c, a, question_lineage, rubric['id'] if rubric['id'] != pin['rubric'] else q['id']
            )
            event(
                c,
                a['project_id'],
                'assignment_repinned',
                {'assignment_id': aid, 'question_lineage': question_lineage, 'teacher': teacher},
            )
            return {'changed': True, 'regrade': flagged}
