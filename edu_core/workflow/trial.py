"""Teacher-trial metrics (COURSE_WORKSPACE_DESIGN.md 13.2).

The trial's success criteria are fixed before any teacher uses the system: no repeated entry of course
background, roster or standards beyond the first; no class mix-ups when one exercise serves two classes;
at least half of the plans and questions a teacher edits are actually used; total time to a usable result
no worse than the teacher's usual way; more than half willing to continue.

This module derives those numbers from what the system already records, so nobody has to fill in a form
afterwards. The two definitions everything rests on:

**usable result** -- the first ``confirm`` event in a task (a plan, a question, a grading or a 讲评稿 the
teacher adopted). Total time runs from the task's creation to that moment, so waiting for the model,
reviewing and fixing are all included.

**used in teaching** -- a lesson plan is used when a lesson session pins its lineage and is marked taught;
a question is used when an assignment pins it and at least one student answer was imported for it.
Confirming or assigning alone is not use.

Numbers that only the teacher can supply are kept apart: the time their usual way took, and whether they
want to continue (``record_trial_feedback``). Missing records are reported as missing, never as zero.
"""

from __future__ import annotations

import json
import statistics
import time

from edu_core.access import CURRENT_USER
from edu_core.storage import event

TARGETS = {
    'repeated_entry': '连续三节课中课程背景、名单和课标选择的重复录入 0 次（首次填写之外）',
    'cross_class_errors': '两个班复用同一份练习时的串班错误 0 次',
    'adoption': '教案和题目经教师修改后实际用于教学的比例不低于一半',
    'time': '从开始到得到可用成果的总耗时（含等待、审核、修正）不超过教师原有方式',
    'willingness': '试用结束后表示愿意继续使用的教师过半数',
}

DEFINITIONS = {
    'usable': '任务里第一份被确认的产物：从任务建立到这一刻，等待、审核和修改都计入总耗时',
    'wait': '任务内模型运行（runs）的时长合计，其余算老师自己的时间',
    'plan_used': '教案被某个课时采用（plan_artifact_id 同一谱系）且该课时标记为已授课',
    'question_used': '题目被某次布置固定，且这次布置已导入至少一份作答',
    'repeated_entry': '任务建立后再次填写课时信息的次数、再次录入名单的次数、手工勾选课标的项目数；都是代理指标，不判断是否真的重复',
    'cross_class_errors': '作答或批改里的学生不在该次布置的名单快照里',
}


def _epoch(value, *, end=False):
    """A report boundary: epoch seconds, 'YYYY-MM-DD' (whole days, local) or None."""
    if value in (None, ''):
        return None
    if isinstance(value, int | float):
        return float(value)
    day = time.strptime(value, '%Y-%m-%d')
    start = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    return start + 86400 - 1e-6 if end else start


def _teacher(owner):
    return owner or 'local'


class TrialMixin:
    """Read-only trial measurements plus one small recorded answer (willingness)."""

    def trial_report(self, course_id=None, since=None, until=None):
        lo, hi = _epoch(since), _epoch(until, end=True)
        with self.store.read() as c:
            tasks = self._trial_tasks(c, course_id, lo, hi)
            rows = [self._trial_task_row(c, t) for t in tasks]
            return {
                'targets': TARGETS,
                'definitions': DEFINITIONS,
                'summary': self._trial_summary(c, rows, tasks),
                'tasks': rows,
            }

    def record_trial_feedback(self, willing, note='', teacher=''):
        """The teacher's answer to "would you keep using it?". Latest answer per teacher wins."""
        actor = CURRENT_USER.get()
        teacher = (teacher or (actor or {}).get('id') or 'local').strip()
        if not teacher:
            raise ValueError('缺少老师标识')
        payload = {
            'teacher': teacher,
            'willing': bool(willing),
            'note': (note or '').strip()[:500],
            'at': time.time(),
        }
        with self.store.tx() as c:
            event(c, '', 'trial_feedback', payload)
        return payload

    # ----- rows -----

    def _trial_tasks(self, c, course_id, lo, hi):
        sql = (
            'SELECT t.*, p.owner_id FROM tasks t JOIN projects p ON p.id=t.project_id '
            'WHERE (? IS NULL OR t.course_id=?)'
        )
        args = [course_id or None, course_id or None]
        if lo is not None:
            sql += ' AND t.created_at>=?'
            args.append(lo)
        if hi is not None:
            sql += ' AND t.created_at<=?'
            args.append(hi)
        return [dict(r) for r in c.execute(sql + ' ORDER BY t.created_at', args)]

    def _trial_task_row(self, c, t):
        pid, tid = t['project_id'], t['id']
        arts = [dict(r) for r in c.execute('SELECT * FROM artifacts WHERE task_id=?', (tid,))]
        contents = {
            a['id']: json.loads(a['content']).get('regenerated_from')
            for a in arts
            if a['type'] in ('question', 'rubric')
        }
        usable_at = self._first_confirm(c, pid, {a['id'] for a in arts})
        wait = sum(
            (r['finished_at'] or 0) - r['started_at']
            for r in c.execute(
                'SELECT started_at, finished_at FROM runs WHERE task_id=? AND finished_at IS NOT NULL', (tid,)
            )
        )
        total = usable_at - t['created_at'] if usable_at is not None else None
        plan_used, questions = self._usage(c, pid, arts)
        return {
            'id': tid,
            'title': t['title'],
            'kind': t['kind'],
            'course_id': t['course_id'],
            'offering_id': t['offering_id'],
            'teacher': _teacher(t['owner_id']),
            'started_at': t['created_at'],
            'usable_at': usable_at,
            'total_seconds': round(total, 3) if total is not None else None,
            'wait_seconds': round(wait, 3),
            'teacher_seconds': round(max(0.0, total - wait), 3) if total is not None else None,
            'versions': len(arts),
            'edits': sum(1 for a in arts if a['origin'] in ('edited', 'revised')),
            'overrides': sum(1 for a in arts if a['origin'] == 'teacher'),
            'regenerations': sum(1 for v in contents.values() if v),
            'plan_used': plan_used,
            'questions_used': questions['used'],
            'questions_total': questions['total'],
            'project_updates': c.execute(
                "SELECT COUNT(*) FROM events WHERE project_id=? AND kind='project_updated' AND at>=?",
                (pid, t['created_at']),
            ).fetchone()[0],
            'roster_entry_calls': c.execute(
                "SELECT COUNT(*) FROM events WHERE project_id=? AND kind='learners_added' AND at>=?",
                (pid, t['created_at']),
            ).fetchone()[0],
            'manual_curriculum': self._manual_curriculum(c, pid),
        }

    def _first_confirm(self, c, pid, artifact_ids):
        if not artifact_ids:
            return None
        marks = ','.join('?' * len(artifact_ids))
        r = c.execute(
            "SELECT MIN(at) FROM events WHERE project_id=? AND kind='confirm' "
            f"AND json_extract(payload,'$.artifact_id') IN ({marks})",
            (pid, *artifact_ids),
        ).fetchone()
        return r[0] if r else None

    def _usage(self, c, pid, arts):
        """Whether this task's plan and questions were actually used in teaching (see module docstring)."""
        plan_lineages = {a['lineage_id'] for a in arts if a['type'] == 'lesson_plan'}
        plan_ids = {a['id'] for a in arts if a['type'] == 'lesson_plan'}
        taught = {
            r[0]
            for r in c.execute(
                'SELECT plan_artifact_id FROM lesson_sessions WHERE project_id=? AND status=?', (pid, 'taught')
            )
        }
        plan_used = bool(plan_ids & taught) or any(
            c.execute(
                'SELECT 1 FROM lesson_sessions s JOIN artifacts a ON a.id=s.plan_artifact_id '
                'WHERE s.project_id=? AND s.status=? AND a.lineage_id=? LIMIT 1',
                (pid, 'taught', lin),
            ).fetchone()
            for lin in plan_lineages
        )
        lineages = {a['lineage_id'] for a in arts if a['type'] == 'question'}
        used, total = 0, 0
        for r in c.execute('SELECT id, pinned FROM assignments WHERE project_id=?', (pid,)).fetchall():
            for lin in json.loads(r[1] or '{}'):
                if lin not in lineages:
                    continue
                total += 1
                answered = c.execute(
                    "SELECT 1 FROM artifacts WHERE type='submission' AND assignment_id=? "
                    "AND json_extract(content,'$.question_lineage')=? LIMIT 1",
                    (r[0], lin),
                ).fetchone()
                if answered:
                    used += 1
        return plan_used, {'used': used, 'total': total}

    def _manual_curriculum(self, c, pid):
        r = c.execute('SELECT curriculum FROM projects WHERE id=?', (pid,)).fetchone()
        try:
            cur = json.loads(r[0]) if r and r[0] else {}
        except ValueError:
            return 0
        return 1 if cur.get('chosen_by') == 'teacher' else 0

    # ----- summary -----

    def _trial_summary(self, c, rows, tasks):
        usable = [r for r in rows if r['total_seconds'] is not None]
        plan_tasks = [r for r in rows if self._task_has_plan(c, r['id'])]
        questions_total = sum(r['questions_total'] for r in rows)
        teachers = {r['teacher'] for r in rows}
        latest = self._latest_feedback(c)
        answered = teachers & set(latest)
        return {
            'tasks': len(rows),
            'usable': len(usable),
            'adoption': {
                'plans': len(plan_tasks),
                'plans_used': sum(1 for r in plan_tasks if r['plan_used']),
                'questions': questions_total,
                'questions_used': sum(r['questions_used'] for r in rows),
            },
            'time': {
                'tasks': len(usable),
                'total_seconds': round(sum(r['total_seconds'] for r in usable), 3) if usable else None,
                'median_seconds': round(statistics.median(r['total_seconds'] for r in usable), 3) if usable else None,
                'wait_seconds': round(sum(r['wait_seconds'] for r in usable), 3) if usable else None,
                'teacher_seconds': round(sum(r['teacher_seconds'] for r in usable), 3) if usable else None,
            },
            'repeated_entry': {
                'project_updates': sum(r['project_updates'] for r in rows),
                'roster_entry_calls': sum(r['roster_entry_calls'] for r in rows),
                'manual_curriculum': sum(r['manual_curriculum'] for r in rows),
            },
            'cross_class_errors': self._cross_class_errors(c, {t['project_id'] for t in tasks}),
            'willingness': {
                'willing': sum(1 for p in latest.values() if p.get('willing') is True),
                'unwilling': sum(1 for p in latest.values() if p.get('willing') is False),
                'unanswered': len(teachers - answered),
            },
        }

    def _task_has_plan(self, c, tid):
        return bool(
            c.execute("SELECT 1 FROM artifacts WHERE task_id=? AND type='lesson_plan' LIMIT 1", (tid,)).fetchone()
        )

    def _cross_class_errors(self, c, pids):
        """Student work whose learner is not in the roster snapshot of the assignment it belongs to."""
        errors = 0
        for pid in pids:
            for r in c.execute('SELECT id, roster FROM assignments WHERE project_id=?', (pid,)).fetchall():
                roster = {m['id'] for m in json.loads(r[1] or '[]')}
                for a in c.execute(
                    "SELECT content FROM artifacts WHERE assignment_id=? AND type IN ('submission','grading')", (r[0],)
                ):
                    if json.loads(a[0]).get('learner_id') not in roster:
                        errors += 1
        return errors

    def _latest_feedback(self, c):
        latest = {}
        for r in c.execute("SELECT payload FROM events WHERE kind='trial_feedback' ORDER BY at, rowid"):
            try:
                p = json.loads(r[0])
            except ValueError:
                continue
            latest[p.get('teacher') or 'local'] = p
        return latest
