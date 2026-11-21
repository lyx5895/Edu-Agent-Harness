"""一节课的闭环 (LESSON_LOOP_DESIGN.md): the first version for one class.

A course is the background filled once: subject, grade, one class with its roster, and course materials. Each lesson
is a project of that course. Its student work is the project's own (unbound) work: the latest confirmed questions
and rubrics apply, and a substantive change marks the gradings and analyses built on the old version as needing an
update. Assignments, lesson sessions, topics and further classes stay in the data layer and are not used here.
"""

from __future__ import annotations

import time

from edu_core import artifacts as A
from edu_core.storage import event, new_id
from edu_core.workflow.common import Conflict
from edu_core.workflow.steps import stage_status

# 新的一节课: where the teacher starts. Each start is an existing task kind, so its first next step fits.
STARTS = {'prep': 'prep', 'grading': 'grading', 'explain': 'explain'}
START_TAB = {'prep': 'prep', 'grading': 'grading', 'explain': 'review'}
STAGE_KEYS = ('prep', 'questions', 'grading', 'analysis', 'review')
FIRST_DONE = {
    'prep': '已备课',
    'questions': '已出题',
    'grading': '已批改',
    'analysis': '已分析学情',
    'review': '已讲评',
}


class LessonsMixin:
    # ----- the course: background filled once -----

    def setup_course(self, title, subject, grade='', class_name='', roster=()):
        """First use: the course, its one class and the roster, in one step."""
        crs = self.create_course(title, subject, grade=grade)
        cls = self.create_class(class_name.strip() or '本班', list(roster))
        self.link_class(crs['id'], cls['id'])
        return self.lesson_course(crs['id'])

    @staticmethod
    def _the_class(c, course_id):
        """The class a course is taught to: its first linked class (later ones are not used in this version)."""
        r = c.execute(
            'SELECT o.id AS offering_id, o.class_id, g.name FROM offerings o JOIN class_groups g ON g.id=o.class_id '
            'WHERE o.course_id=? ORDER BY o.created_at LIMIT 1',
            (course_id,),
        ).fetchone()
        return dict(r) if r else None

    def lesson_course(self, course_id):
        """Course background as the lesson pages show it: the course, its class and roster, its materials."""
        with self.store.read() as c:
            crs = self._course(c, course_id)
            cls = self._the_class(c, course_id)
            members = (
                [
                    dict(m)
                    for m in c.execute(
                        'SELECT id, alias FROM class_members WHERE class_id=? AND left_at IS NULL ORDER BY joined_at',
                        (cls['class_id'],),
                    )
                ]
                if cls
                else []
            )
            materials = [
                {'id': r['id'], 'filename': r['filename'], 'chars': len(r['text'])}
                for r in c.execute(
                    "SELECT id, filename, text FROM materials WHERE course_id=? AND project_id='' ORDER BY created_at",
                    (course_id,),
                )
            ]
        return {**crs, 'class': cls, 'members': members, 'materials': materials}

    def ensure_class(self, course_id, class_name='本班'):
        """A course made before this version may have no class yet: give it one, so a roster has a home."""
        with self.store.read() as c:
            self._course(c, course_id)
            cls = self._the_class(c, course_id)
        if cls is None:
            new = self.create_class(class_name.strip() or '本班', [])
            self.link_class(course_id, new['id'])
        return self.lesson_course(course_id)

    # ----- lessons -----

    def lessons(self, course_id):
        """首页: every lesson of the course, newest first, with where its loop stands and the one thing to do next."""
        with self.store.read() as c:
            self._course(c, course_id)
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT p.id, p.created_at, COALESCE(t.status, 'open') AS status FROM projects p "
                    'LEFT JOIN tasks t ON t.project_id=p.id WHERE p.course_id=? ORDER BY p.created_at DESC',
                    (course_id,),
                )
            ]
            touched = {
                r[0]: r[1]
                for r in c.execute(
                    'SELECT project_id, MAX(created_at) FROM artifacts '
                    'WHERE project_id IN (SELECT id FROM projects WHERE course_id=?) GROUP BY project_id',
                    (course_id,),
                )
            }
        out = []
        for r in rows:
            if r['status'] == 'archived':
                continue
            ov = self.overview(r['id'])  # a lesson's own work; old lessons with only class assignments show those
            p = ov['project']
            step = next((s for s in ov['next'] if not s.get('optional')), None)
            # A stage done in an earlier round stays done on the lesson line; 继续 still follows the current round.
            stages = self._stages_any_round(r['id'], ov)
            done = [k for k in STAGE_KEYS if stages.get(k)]
            out.append(
                {
                    'id': p['id'],
                    'title': '｜'.join(x for x in (p.get('knowledge_module'), p.get('lesson')) if x) or p['title'],
                    'created_at': r['created_at'],
                    'updated_at': max(touched.get(p['id']) or 0, r['created_at']),
                    'round': ov['latest_round'],
                    'stages': stages,
                    'next': step,
                    # With nothing left to do, say how far the lesson went rather than leaving the line empty.
                    'state': FIRST_DONE[done[-1]] if done else '刚开始',
                    'review_queue': ov['review_queue'],
                    'stale': len(ov['stale']),
                }
            )
        out.sort(key=lambda x: x['updated_at'], reverse=True)
        return out

    def _stages_any_round(self, pid, ov):
        """Which stages a lesson has completed in any of its rounds (the overview reports the latest round only)."""
        out = {k: bool(ov['stages'].get(k)) for k in STAGE_KEYS}
        with self.store.read() as c:
            scope = self.scope(c, pid, None, ov['assignment_id'] or None)
            for r in range(1, ov['latest_round']):
                rc = {}
                for t in A.TYPES:
                    items = scope.current(c, t, round_=r)
                    rc[t] = {'total': len(items), 'confirmed': sum(1 for a in items if a['confirmed_version'])}
                for k, v in stage_status(ov['counts'], rc, r).items():
                    if k in out and v:
                        out[k] = True
        return out

    def new_lesson(self, course_id, knowledge_module, lesson='', start='prep'):
        """新的一节课: a lesson of the course, with the class roster, opened where the teacher said they start."""
        if start not in STARTS:
            raise ValueError('未知的开始方式')
        if not (knowledge_module or '').strip():
            raise Conflict('先写下这节课的主题，例如：对数函数')
        with self.store.read() as c:
            cls = self._the_class(c, course_id)
        started = self.start_task(
            course_id,
            STARTS[start],
            offering_id=cls['offering_id'] if cls else '',
            knowledge_module=knowledge_module.strip(),
            lesson=(lesson or '').strip(),
        )
        pid = started['project']['id']
        if cls:
            with self.store.tx() as c:
                self._copy_roster(c, cls['class_id'], pid)
        return {'project': self._lesson_project(pid), 'tab': START_TAB[start]}

    def _lesson_project(self, pid):
        with self.store.read() as c:
            return self._project_view(self._project(c, pid))

    @staticmethod
    def _copy_roster(c, class_id, pid):
        """The class roster becomes the lesson's roster; aliases already there are kept as they are."""
        have = {r[0] for r in c.execute('SELECT alias FROM learners WHERE project_id=?', (pid,))}
        for (alias,) in c.execute(
            'SELECT alias FROM class_members WHERE class_id=? AND left_at IS NULL ORDER BY joined_at', (class_id,)
        ).fetchall():
            if alias not in have:
                c.execute(
                    'INSERT INTO learners(id,project_id,alias,created_at) VALUES(?,?,?,?)',
                    (new_id('lrn'), pid, alias, time.time()),
                )

    def _class_of_project(self, c, pid):
        """The class id behind a lesson, or None for a lesson outside any course."""
        r = c.execute(
            'SELECT o.class_id FROM projects p JOIN offerings o ON o.id=p.offering_id WHERE p.id=?', (pid,)
        ).fetchone()
        return r[0] if r else None

    def _share_with_class(self, c, pid, aliases):
        """Learners added in a lesson join the class too, so the next lesson starts with them."""
        class_id = self._class_of_project(c, pid)
        if class_id:
            self._insert_members(c, class_id, aliases, 'lesson')

    def lesson_after(self, c, pid, new_pid):
        """进入下一课时 inside a course: the new lesson belongs to the same course and class, has a task row (so it
        lists like any lesson) and the class roster."""
        p = self._project(c, pid)
        if not p.get('course_id'):
            return
        c.execute(
            'UPDATE projects SET course_id=?, offering_id=?, unit_id=? WHERE id=?',
            (p['course_id'], p.get('offering_id', ''), p.get('unit_id', ''), new_pid),
        )
        n = self._project(c, new_pid)
        now = time.time()
        title = '｜'.join(x for x in (n.get('knowledge_module'), n.get('lesson')) if x) or n['title']
        c.execute(
            'INSERT INTO tasks(id,course_id,offering_id,project_id,kind,title,request,status,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (new_id('task'), p['course_id'], p.get('offering_id', ''), new_pid, 'prep', title, '', 'open', now, now),
        )
        class_id = self._class_of_project(c, new_pid)
        if class_id:
            self._copy_roster(c, class_id, new_pid)
        event(c, new_pid, 'lesson_in_course', {'course_id': p['course_id']})
