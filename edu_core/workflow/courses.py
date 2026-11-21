"""Course workspace (COURSE_WORKSPACE_DESIGN.md 2, 4, 5, 12): courses, classes, tasks, lesson sessions, 本班学情,
the home page, and mapping old per-lesson projects into courses.

A course is the long-lived space (背景, shared materials). A class keeps its own roster and is linked to a course
by an offering (授课关系), which holds that class's progress. A task is one piece of teacher work; its working
area is a project, so every existing stage keeps working unchanged inside it. Lesson sessions are created by the
system when a lesson plan is first adopted for a class, never by drafting alone.
"""

from __future__ import annotations

import json
import re
import time

from edu_core import artifacts as A
from edu_core.access import CURRENT_USER
from edu_core.storage import event, new_id, row
from edu_core.subjects import pack
from edu_core.workflow.common import Conflict

KINDS = {'prep': '备课', 'practice': '出一份练习', 'grading': '批改作业', 'explain': '准备讲解', 'legacy': '旧课时项目'}
TAB_OF_KIND = {'prep': 'prep', 'practice': 'questions', 'grading': 'grading', 'explain': 'review', 'legacy': 'prep'}
STAGE_NAMES = {'prep': '01 备课设计', 'questions': '02 出题改题', 'grading': '03 作业批改', 'analysis': '04 学情分析', 'review': '05 讲题答疑'}
SESSION_STATUS = {'planned': '计划中', 'taught': '已授课', 'cancelled': '已取消'}


def _today():
    return time.strftime('%Y-%m-%d')


GRADE_RE = re.compile(r'(高[一二三]|初[一二三]|[一二三四五六]年级|七年级|八年级|九年级)')


def _suggest_course(p):
    """A course name for an old project, e.g. 高一数学·对数函数: grade + subject (or the title's first part when it
    names the subject) + knowledge module. Only a suggestion; the teacher edits it before mapping."""
    subject = pack(p['subject'])['name']
    grade = p['grade'] or (m.group(1) if (m := GRADE_RE.search(p['title'])) else '')
    head = re.split(r'[｜|·]', p['title'])[0].strip()
    base = head if subject in head and len(head) <= 12 else f'{grade}{subject}'
    module = (p.get('knowledge_module') or '').strip()
    return {
        'title': f'{base}·{module}' if module and module not in base else base,
        'subject': p['subject'],
        'grade': grade,
    }


class CoursesMixin:
    """Courses, classes, offerings, tasks, sessions, home and legacy mapping."""

    # ----- courses -----

    def _course(self, c, cid):
        r = row(c.execute('SELECT * FROM courses WHERE id=?', (cid,)).fetchone()) if cid else None
        if r is None:
            raise KeyError(f'课程不存在：{cid}')
        r['subject_name'] = pack(r['subject'])['name']
        return r

    def create_course(self, title, subject, grade='', term='', textbook='', notes='', owner=''):
        """A course needs only a name and a subject (any subject; unknown ones use the general flow)."""
        if not (title or '').strip():
            raise ValueError('请填写课程名称')
        if not (subject or '').strip():
            raise ValueError('请选择或填写学科')
        cid = new_id('crs')
        with self.store.tx() as c:
            c.execute(
                'INSERT INTO courses(id,title,subject,grade,term,textbook,notes,owner,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    cid,
                    title.strip(),
                    pack(subject)['id'],
                    grade.strip(),
                    term.strip(),
                    textbook.strip(),
                    notes.strip(),
                    owner,
                    time.time(),
                ),
            )
            self._record_owner(c, 'course', cid)
            event(c, cid, 'course_created', {'title': title, 'subject': subject})
            return self._course(c, cid)

    def update_course(self, cid, **fields):
        allowed = ('title', 'grade', 'term', 'textbook', 'notes', 'archived')
        with self.store.tx() as c:
            self._course(c, cid)
            changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
            if 'title' in changes and not str(changes['title']).strip():
                raise ValueError('请填写课程名称')
            for k, v in changes.items():
                c.execute(
                    f'UPDATE courses SET {k}=? WHERE id=?', (int(bool(v)) if k == 'archived' else str(v).strip(), cid)
                )
            if changes:
                event(c, cid, 'course_updated', changes)
            return self._course(c, cid)

    def courses(self, include_archived=False):
        with self.store.read() as c:
            sql = (
                'SELECT id FROM courses'
                + ('' if include_archived else ' WHERE archived=0')
                + ' ORDER BY created_at DESC'
            )
            visible = self.visible_course_ids(c)
            return [
                self._course(c, r['id']) for r in c.execute(sql).fetchall() if visible is None or r['id'] in visible
            ]

    def course(self, cid):
        with self.store.read() as c:
            out = self._course(c, cid)
            out['offerings'] = self._offering_views(c, cid)
            out['materials'] = [
                {'id': r['id'], 'filename': r['filename'], 'chars': len(r['text'])}
                for r in c.execute(
                    "SELECT * FROM materials WHERE course_id=? AND project_id='' ORDER BY created_at", (cid,)
                )
            ]
            out['tasks'] = self._task_views(c, course_id=cid, status=None)
            return out

    # ----- classes and offerings -----

    def create_class(self, name, aliases=(), notes=''):
        if not (name or '').strip():
            raise ValueError('请填写班级名称')
        cid = new_id('cls')
        with self.store.tx() as c:
            c.execute(
                'INSERT INTO class_groups(id,name,notes,created_at) VALUES(?,?,?,?)',
                (cid, name.strip(), notes.strip(), time.time()),
            )
            self._insert_members(c, cid, aliases, 'roster')
            self._record_owner(c, 'class', cid)
            event(c, cid, 'class_created', {'name': name})
        return self.class_detail(cid)

    def _insert_members(self, c, class_id, aliases, source):
        added = []
        for alias in aliases:
            alias = (alias or '').strip()
            if not alias:
                continue
            if len(alias) > 40:
                raise ValueError(f'学生代号过长：{alias[:10]}…')
            cur = c.execute(
                'INSERT OR IGNORE INTO class_members(id,class_id,alias,source,joined_at) VALUES(?,?,?,?,?)',
                (new_id('mem'), class_id, alias, source, time.time()),
            )
            if cur.rowcount:
                added.append(alias)
            else:
                c.execute('UPDATE class_members SET left_at=NULL WHERE class_id=? AND alias=?', (class_id, alias))
        return added

    def add_members(self, class_id, aliases):
        with self.store.tx() as c:
            self._class(c, class_id)
            added = self._insert_members(c, class_id, aliases, 'roster')
        return {'added': added, **self.class_detail(class_id)}

    def remove_member(self, class_id, member_id):
        """A learner leaves the class; past assignments keep their roster snapshot."""
        with self.store.tx() as c:
            c.execute(
                'UPDATE class_members SET left_at=? WHERE id=? AND class_id=?', (time.time(), member_id, class_id)
            )
        return self.class_detail(class_id)

    def _class(self, c, class_id):
        r = row(c.execute('SELECT * FROM class_groups WHERE id=?', (class_id,)).fetchone())
        if r is None:
            raise KeyError(f'班级不存在：{class_id}')
        return r

    def class_detail(self, class_id):
        with self.store.read() as c:
            out = self._class(c, class_id)
            out['members'] = [
                dict(m)
                for m in c.execute(
                    'SELECT id, alias, source FROM class_members WHERE class_id=? AND left_at IS NULL ORDER BY joined_at',
                    (class_id,),
                )
            ]
            return out

    def classes(self):
        with self.store.read() as c:
            u, visible = CURRENT_USER.get(), self.visible_course_ids(c)

            def mine(r):
                if visible is None or r['owner_id'] == u['id']:
                    return True
                return any(
                    o[0] in visible for o in c.execute('SELECT course_id FROM offerings WHERE class_id=?', (r['id'],))
                )

            return [
                {
                    **dict(r),
                    'members': c.execute(
                        'SELECT COUNT(*) FROM class_members WHERE class_id=? AND left_at IS NULL', (r['id'],)
                    ).fetchone()[0],
                }
                for r in c.execute('SELECT * FROM class_groups ORDER BY created_at').fetchall()
                if mine(r)
            ]

    def link_class(self, course_id, class_id):
        """授课关系: this class takes this course. Content is shared; progress, assignments and 学情 are per class."""
        with self.store.tx() as c:
            self._course(c, course_id)
            self._class(c, class_id)
            r = c.execute('SELECT id FROM offerings WHERE course_id=? AND class_id=?', (course_id, class_id)).fetchone()
            oid = r[0] if r else new_id('off')
            if not r:
                c.execute(
                    'INSERT INTO offerings(id,course_id,class_id,created_at) VALUES(?,?,?,?)',
                    (oid, course_id, class_id, time.time()),
                )
                event(c, course_id, 'class_linked', {'offering_id': oid, 'class_id': class_id})
            return self._offering(c, oid)

    def _offering(self, c, oid):
        r = row(
            c.execute(
                'SELECT o.*, g.name AS class_name FROM offerings o JOIN class_groups g ON g.id=o.class_id WHERE o.id=?',
                (oid,),
            ).fetchone()
        )
        if r is None:
            raise KeyError(f'授课关系不存在：{oid}')
        return r

    def _offering_views(self, c, course_id):
        out = []
        for r in c.execute('SELECT id FROM offerings WHERE course_id=? ORDER BY created_at', (course_id,)).fetchall():
            o = self._offering(c, r['id'])
            o['members'] = c.execute(
                'SELECT COUNT(*) FROM class_members WHERE class_id=? AND left_at IS NULL', (o['class_id'],)
            ).fetchone()[0]
            o['sessions'] = self._session_views(c, o['id'])
            out.append(o)
        return out

    # ----- tasks -----

    def start_task(
        self,
        course_id,
        kind,
        title='',
        offering_id='',
        request='',
        knowledge_module='',
        lesson='',
        lesson_scope='',
        owner='',
        unit_id=None,
    ):
        """开始工作: a task with its own working area. Course background is reused; nothing is asked twice.

        The task joins a topic: ``unit_id`` if given, else the topic named like its 知识模块 (created on first use,
        so topics fill in as the teacher works instead of having to be planned up front).
        """
        if kind not in KINDS or kind == 'legacy':
            raise ValueError('未知的工作类型')
        with self.store.read() as c:
            course = self._course(c, course_id)
            if course['archived']:
                raise Conflict('课程已归档，请先取消归档')
            if offering_id and self._offering(c, offering_id)['course_id'] != course_id:
                raise Conflict('这个班没有关联到该课程')
            # 学生基础 last written for this class (or course) is offered again as an editable default.
            prev = c.execute(
                "SELECT learner_profile FROM projects WHERE course_id=? AND learner_profile!='' "
                'ORDER BY (offering_id=?) DESC, created_at DESC LIMIT 1',
                (course_id, offering_id or ''),
            ).fetchone()
        # A task without a topic still needs a name that tells it apart in the switch list: 高一数学·批改作业.
        name = (
            title.strip()
            or '｜'.join(x for x in (knowledge_module.strip(), lesson.strip()) if x)
            or f'{course["title"]}·{KINDS[kind]}'
        )
        project = self.create_project(
            name,
            course['subject'],
            grade=course['grade'],
            notes=request,
            owner=owner,
            knowledge_module=knowledge_module,
            lesson=lesson,
            learner_profile=prev[0] if prev else '',
        )
        tid = new_id('task')
        with self.store.tx() as c:
            c.execute(
                'UPDATE projects SET course_id=?, offering_id=?, lesson_scope=? WHERE id=?',
                (course_id, offering_id or '', lesson_scope.strip()[:500], project['id']),
            )
            if unit_id:
                if self._unit(c, unit_id)['course_id'] != course_id:
                    raise Conflict('这个主题不属于该课程')
            else:
                unit_id = self._ensure_unit(c, course_id, knowledge_module)
            c.execute('UPDATE projects SET unit_id=? WHERE id=?', (unit_id or '', project['id']))
            now = time.time()
            c.execute(
                'INSERT INTO tasks(id,course_id,offering_id,project_id,kind,title,request,status,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (tid, course_id, offering_id or '', project['id'], kind, name, request.strip(), 'open', now, now),
            )
            event(c, project['id'], 'task_started', {'task_id': tid, 'kind': kind, 'course_id': course_id})
            return {
                'task': self._task_views(c, task_id=tid)[0],
                'project': self._project_view(self._project(c, project['id'])),
                'tab': TAB_OF_KIND[kind],
            }

    def update_task(self, tid, status=None, title=None):
        with self.store.tx() as c:
            if status is not None:
                if status not in ('open', 'done', 'archived'):
                    raise ValueError('未知的任务状态')
                c.execute('UPDATE tasks SET status=?, updated_at=? WHERE id=?', (status, time.time(), tid))
            if title is not None and title.strip():
                c.execute('UPDATE tasks SET title=?, updated_at=? WHERE id=?', (title.strip(), time.time(), tid))
            # 回查时不能只看"进行中"：刚标记完成的任务状态已经不是 open 了
            views = self._task_views(c, task_id=tid, status=None)
            if not views:
                raise KeyError(f'任务不存在：{tid}')
            return views[0]

    def tasks(self, course_id=None, status='open'):
        with self.store.read() as c:
            return self._task_views(c, course_id=course_id, status=status)

    def _task_views(self, c, *, course_id=None, status='open', task_id=None):
        sql, args = 'SELECT * FROM tasks WHERE 1=1', []
        visible = self.visible_course_ids(c)
        if visible is not None:
            sql += f' AND course_id IN ({",".join("?" * len(visible)) or "NULL"})'
            args += sorted(visible)
        if task_id:
            sql, args = sql + ' AND id=?', [*args, task_id]
        if course_id:
            sql, args = sql + ' AND course_id=?', [*args, course_id]
        if status:
            sql, args = sql + ' AND status=?', [*args, status]
        out = []
        for t in c.execute(sql + ' ORDER BY updated_at DESC', args).fetchall():
            t = dict(t)
            t['kind_label'] = KINDS.get(t['kind'], t['kind'])
            if t['title'].strip() in ('', *KINDS.values()):  # older tasks were named just "备课": say which course
                t['title'] = f'{self._course(c, t["course_id"])["title"]}·{t["kind_label"]}'
            t['class_name'] = self._offering(c, t['offering_id'])['class_name'] if t['offering_id'] else ''
            t['pending'] = self._task_pending(c, t['project_id'])
            t['tab'] = TAB_OF_KIND.get(t['kind'], 'prep')
            p = self._project(c, t['project_id'])
            topic = '｜'.join(x for x in (p.get('knowledge_module'), p.get('lesson')) if x)
            if topic and t['title'] in (
                KINDS.get(t['kind']),
                f'{self._course(c, t["course_id"])["title"]}·{t["kind_label"]}',
            ):
                t['title'] = topic
            defaults = {
                'prep': ('继续编辑教案', '待补充要求或生成教案', '#course-module'),
                'practice': ('继续准备练习', '待准备题目', '#generate-questions'),
                'grading': ('继续批改作业', '待导入试卷或学生作答', '#sub-csv'),
                'explain': ('继续准备讲解', '待填写题目或审阅讲解', '#ex-question'),
            }
            t['action_label'], t['progress_label'], t['target'] = defaults.get(t['kind'], defaults['prep'])
            plans = A.current(c, t['project_id'], 'lesson_plan')
            questions = A.current(c, t['project_id'], 'question')
            if t['kind'] == 'prep' and plans:
                t['target'] = '#lesson-plan'
                t['progress_label'] = (
                    '教案已确认，可导出或继续修改' if plans[-1]['status'] == 'confirmed' else '教案待审阅'
                )
                t['action_label'] = '查看教案' if plans[-1]['status'] == 'confirmed' else '审阅教案'
            elif t['kind'] == 'practice' and questions:
                t['target'] = '#questions'
                t['action_label'] = '核对题目'
                t['progress_label'] = f'{len(questions)} 道题，核对后可布置'
            elif t['kind'] == 'grading' and not questions:
                t['tab'], t['target'], t['action_label'] = 'grading', '#g-paper-text', '导入试卷'
            t['assignment_id'], t['scope_label'] = '', ''
            # A task is named after how it started, but the work goes on: a 备课 task often ends up holding the
            # questions, the answers and the second round. Past its own kind, the card follows the loop's next step.
            if (t['kind'] == 'prep' and questions) or A.current(c, t['project_id'], 'submission'):
                self._follow_loop(t)
            if any('待复核' in x for x in t['pending']):
                t['tab'], t['target'], t['action_label'] = 'grading', '#queue', '处理待复核作答'
            out.append(t)
        return out

    def _follow_loop(self, t):
        """Point a task card at the project's actual next step, in the scope the workspace opens by default."""
        ov = self.overview(t['project_id'])
        step = next((s for s in ov['next'] if not s.get('optional')), None)
        a = next((x for x in ov['assignments'] if x['id'] == ov['assignment_id']), None)
        if a:
            t['assignment_id'] = a['id']
            t['scope_label'] = f'{a["class_name"]} · 第 {a["round"]} 轮'
        if step:
            t['tab'], t['target'], t['action_label'] = step['tab'], step['target'], step['label']
            t['progress_label'] = f'进行到「{STAGE_NAMES.get(step["tab"], "")}」'
        else:
            t['progress_label'] = '本轮已完成，可以布置再练或进入下一课时'

    def _task_pending(self, c, pid):
        """What waits for the teacher in a task, from the actual records (首页“继续工作”)."""
        notes = []
        plans = A.current(c, pid, 'lesson_plan')
        if plans and not plans[-1]['confirmed_version']:
            notes.append('备课方案待确认')
        elif plans and plans[-1]['status'] == 'draft':
            notes.append('备课方案有未确认的修改')
        drafts = sum(1 for q in A.current(c, pid, 'question') if q['status'] == 'draft')
        if drafts:
            notes.append(f'{drafts} 道题待确认')
        queue = sum(
            1 for g in A.current(c, pid, 'grading') if g['status'] == 'draft' and g['content'].get('needs_teacher')
        )
        if queue:
            notes.append(f'{queue} 份批改待复核')
        stale = len(A.stale(c, pid))
        if stale:
            notes.append(f'{stale} 项结果需要更新')
        return notes

    # ----- lesson sessions -----

    def _on_plan_confirmed(self, c, plan):
        """First adoption of a lesson plan in a class task creates its planned session; a later adoption updates the
        plan of a session that is still planned. Drafts alone never create sessions (design doc 5.2)."""
        p = self._project(c, plan['project_id'])
        self._link_plan_objectives(c, plan)
        if not p.get('offering_id'):
            return
        s = c.execute(
            'SELECT * FROM lesson_sessions WHERE project_id=? AND offering_id=? ORDER BY created_at LIMIT 1',
            (p['id'], p['offering_id']),
        ).fetchone()
        now = time.time()
        if s is None:
            task = c.execute('SELECT id FROM tasks WHERE project_id=? LIMIT 1', (p['id'],)).fetchone()
            title = p.get('lesson') or p.get('knowledge_module') or p['title']
            if p.get('lesson') and p.get('knowledge_module'):
                title = f'{p["knowledge_module"]}｜{p["lesson"]}'
            c.execute(
                'INSERT INTO lesson_sessions(id,offering_id,project_id,task_id,title,status,plan_artifact_id,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    new_id('ses'),
                    p['offering_id'],
                    p['id'],
                    task[0] if task else '',
                    title,
                    'planned',
                    plan['id'],
                    now,
                    now,
                ),
            )
        elif s['status'] == 'planned':
            c.execute(
                'UPDATE lesson_sessions SET plan_artifact_id=?, updated_at=? WHERE id=?', (plan['id'], now, s['id'])
            )

    def _session_views(self, c, offering_id):
        rows = [
            dict(r)
            for r in c.execute('SELECT * FROM lesson_sessions WHERE offering_id=? ORDER BY created_at', (offering_id,))
        ]
        today = _today()
        last_taught = max((r['updated_at'] for r in rows if r['status'] == 'taught'), default=None)
        for r in rows:
            r['status_label'] = SESSION_STATUS[r['status']]
            r['overdue'] = r['status'] == 'planned' and (
                (bool(r['planned_date']) and r['planned_date'] < today)
                or (not r['planned_date'] and last_taught is not None and r['created_at'] < last_taught)
            )
            if r['plan_artifact_id']:
                plan = c.execute('SELECT version FROM artifacts WHERE id=?', (r['plan_artifact_id'],)).fetchone()
                r['plan_version'] = plan[0] if plan else None
        return rows

    def sessions(self, offering_id):
        with self.store.read() as c:
            self._offering(c, offering_id)
            return self._session_views(c, offering_id)

    def update_session(self, sid, *, status=None, planned_date=None, title=None):
        """Mark a session taught, reschedule or cancel it. Nothing is marked taught automatically."""
        with self.store.tx() as c:
            s = c.execute('SELECT * FROM lesson_sessions WHERE id=?', (sid,)).fetchone()
            if s is None:
                raise KeyError(f'课时不存在：{sid}')
            changes = {}
            if status is not None:
                if status not in SESSION_STATUS:
                    raise ValueError('课时状态应为 planned、taught 或 cancelled')
                changes['status'] = status
            if planned_date is not None:
                if planned_date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', planned_date):
                    raise ValueError('日期格式应为 YYYY-MM-DD')
                changes['planned_date'] = planned_date
            if title is not None and title.strip():
                changes['title'] = title.strip()
            for k, v in changes.items():
                c.execute(f'UPDATE lesson_sessions SET {k}=?, updated_at=? WHERE id=?', (v, time.time(), sid))
            if changes.get('status') == 'taught':
                c.execute(
                    'UPDATE offerings SET progress=? WHERE id=?', (changes.get('title', s['title']), s['offering_id'])
                )
            event(c, s['project_id'] or s['offering_id'], 'session_updated', {'session_id': sid, **changes})
            return next(r for r in self._session_views(c, s['offering_id']) if r['id'] == sid)

    def use_plan_for_class(self, pid, offering_id):
        """用于其他班: the same confirmed plan version becomes a planned session of another class of the course."""
        with self.store.tx() as c:
            p = self._project(c, pid)
            o = self._offering(c, offering_id)
            if p.get('course_id') != o['course_id']:
                raise Conflict('这个班没有关联到该课程')
            plan = self._plan(c, pid)
            if plan is None:
                raise Conflict('备课方案还没有确认')
            now = time.time()
            sid = new_id('ses')
            title = p.get('lesson') or p.get('knowledge_module') or p['title']
            c.execute(
                'INSERT INTO lesson_sessions(id,offering_id,project_id,task_id,title,status,plan_artifact_id,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (sid, offering_id, pid, '', title, 'planned', plan['id'], now, now),
            )
            event(c, pid, 'plan_reused', {'offering_id': offering_id, 'plan': plan['id'], 'session_id': sid})
            return next(r for r in self._session_views(c, offering_id) if r['id'] == sid)

    def derive_for_class(self, pid, offering_id, title=''):
        """本班调整版 (design doc 5.3): a new task for another class of the course that starts from this task's adopted
        lesson plan and questions. The copies are drafts in new lineages that point back to their source
        (``edit_of``, ``derived_from``), so the content layer keeps the derivation and each class adopts its own
        version. Materials are copied in the same 同源 family. Nothing of the source changes.
        """
        with self.store.read() as c:
            p = self._project(c, pid)
            o = self._offering(c, offering_id)
            if not p.get('course_id') or p['course_id'] != o['course_id']:
                raise Conflict('这个班没有关联到该任务所在的课程')
            plan = self._plan(c, pid)
            questions = []
            for q in A.current(c, pid, 'question', round_=1):
                conf = A.confirmed_in_lineage(c, q['lineage_id'])
                rubric = self._rubric_for(c, q['lineage_id']) if conf else None
                if conf and rubric:
                    questions.append((conf, rubric))
            if plan is None and not questions:
                raise Conflict('这项任务还没有已确认的备课方案或题目，确认后才能做本班调整版')
            mats = [
                dict(r) for r in c.execute('SELECT * FROM materials WHERE project_id=? ORDER BY created_at', (pid,))
            ]
        started = self.start_task(
            p['course_id'],
            'prep' if plan else 'practice',
            title=title.strip() or f'{p["title"]}（{o["class_name"]}调整版）',
            offering_id=offering_id,
            request=p.get('notes', ''),
            knowledge_module=p.get('knowledge_module', ''),
            lesson=p.get('lesson', ''),
            lesson_scope=p.get('lesson_scope', ''),
            unit_id=p.get('unit_id') or None,
        )
        new_pid = started['project']['id']
        with self.store.tx() as c:
            c.execute('UPDATE projects SET curriculum=? WHERE id=?', (p.get('curriculum', ''), new_pid))
            for m in mats:
                c.execute(
                    'INSERT INTO materials(id,project_id,family_id,kind,filename,sha256,text,created_at,course_id) VALUES(?,?,?,?,?,?,?,?,?)',
                    (
                        new_id('mat'),
                        new_pid,
                        m['family_id'],
                        m['kind'],
                        m['filename'],
                        m['sha256'],
                        m['text'],
                        time.time(),
                        m['course_id'],
                    ),
                )
            new_plan = None
            if plan:
                new_plan = A.create(
                    c,
                    new_pid,
                    'lesson_plan',
                    {**plan['content'], 'derived_from': {'project': pid, 'artifact': plan['id']}},
                    origin='derived',
                    edit_of=plan['id'],
                    created_by='teacher',
                )
            for no, (q, rubric) in enumerate(sorted(questions, key=lambda x: x[0]['content']['no']), 1):
                refs = [(new_plan['id'], 'lesson_plan')] if new_plan and q['content'].get('objective_ids') else []
                nq = A.create(
                    c,
                    new_pid,
                    'question',
                    {**q['content'], 'no': no, 'derived_from': {'project': pid, 'artifact': q['id']}},
                    origin='derived',
                    edit_of=q['id'],
                    refs=refs,
                    created_by='teacher',
                )
                A.create(
                    c,
                    new_pid,
                    'rubric',
                    {**rubric['content'], 'question_lineage': nq['lineage_id']},
                    origin='derived',
                    edit_of=rubric['id'],
                    refs=[(nq['id'], 'question'), *refs],
                    created_by='teacher',
                )
            event(
                c,
                new_pid,
                'derived_for_class',
                {'from_project': pid, 'offering_id': offering_id, 'questions': len(questions), 'plan': bool(plan)},
            )
            started['project'] = self._project_view(self._project(c, new_pid))
        started['copied'] = {'plan': bool(plan), 'questions': len(questions), 'materials': len(mats)}
        return started

    def suggest_next_lesson(self, offering_id):
        """准备下一节课: a suggestion from this class's sessions and 学情. It never marks anything as taught."""
        with self.store.read() as c:
            o = self._offering(c, offering_id)
            sessions = [s for s in self._session_views(c, offering_id) if s['status'] != 'cancelled']
            last = sessions[-1] if sessions else None
            module, lesson = '', ''
            if last and last['project_id']:
                p = self._project(c, last['project_id'])
                module = p.get('knowledge_module', '')
                m = re.search(r'第\s*(\d+)\s*课时', p.get('lesson', ''))
                lesson = re.sub(r'第\s*\d+\s*课时', f'第 {int(m.group(1)) + 1} 课时', p['lesson']) if m else ''
            status = self._class_status(c, offering_id)
        return {
            'offering_id': offering_id,
            'class_name': o['class_name'],
            'progress': o['progress'],
            'last_session': last,
            'knowledge_module': module,
            'lesson': lesson,
            'unfinished': [s for s in sessions if s['overdue']],
            'class_status': status,
            'note': '建议的主题和课次可以修改；上一节课是否已上，以老师标记为准。',
        }

    # ----- 本班学情 (design doc 7.1) -----

    def _class_status(self, c, offering_id, prefs=None, *, include_excluded=False):
        """What a new task of this class may read about the class: confirmed problem patterns that the latest
        re-practice did not show as improved, and the summary of the latest confirmed evidence analysis.
        Only this offering's assignments are read; never another class.

        ``prefs`` are the task's choices (design doc 7.1): ``exclude`` lists pattern lineages the teacher removed
        from “已参考”, ``exclude_summary`` drops the analysis summary. With ``include_excluded`` the removed items
        are returned too, marked ``excluded``, so the page can offer them back.
        """
        prefs = prefs or {}
        excluded = set(prefs.get('exclude') or [])
        o = self._offering(c, offering_id)
        rows = [
            self._assignment_row(c, r['id'])
            for r in c.execute(
                'SELECT id FROM assignments WHERE offering_id=? ORDER BY created_at', (offering_id,)
            ).fetchall()
        ]
        patterns, trend, analysis = {}, {}, None
        for a in rows:
            for x in A.listing(c, a['project_id'], 'problem_pattern', statuses=('confirmed',), assignment=a['id']):
                patterns[x['lineage_id']] = x
            comps = A.current(c, a['project_id'], 'comparison', assignment=a['id'])
            if comps:
                for r in comps[-1]['content']['patterns']:
                    trend[r['pattern_lineage']] = r
            done = [
                A.confirmed_in_lineage(c, x['lineage_id'])
                for x in A.current(c, a['project_id'], 'analysis', assignment=a['id'])
                if x['confirmed_version']
            ]
            done = [x for x in done if x and x['content'].get('mode') == 'evidence']
            if done:
                analysis = done[-1]
        rows_out, used = [], []
        for lin, x in patterns.items():
            ct, t = x['content'], trend.get(lin)
            if t and t['enough_evidence'] and not t['persisting']:
                continue  # re-practice showed it improved
            off = lin in excluded
            if off and not include_excluded:
                continue
            if not off:
                used.append(x['id'])
            rows_out.append(
                {
                    'lineage': lin,
                    'excluded': off,
                    'name': ct['name'],
                    'description': ct['description'],
                    'affected': f'{len(ct["affected_learners"])}/{ct["total_learners"]}',
                    'after_practice': (
                        f'再练后仍有 {len(t["persisting"])} 人出错' if t and t['enough_evidence'] else '还没有再练结果'
                    ),
                }
            )
        summary = analysis['content'].get('summary', '') if analysis else ''
        summary_excluded = bool(prefs.get('exclude_summary')) and bool(summary)
        if summary_excluded and not include_excluded:
            summary, analysis = '', None
        if not (rows_out or summary):
            return None
        plan_row = c.execute(
            "SELECT plan_artifact_id FROM lesson_sessions WHERE offering_id=? AND plan_artifact_id!='' AND status!='cancelled' "
            "ORDER BY (status='taught') DESC, updated_at DESC LIMIT 1",
            (offering_id,),
        ).fetchone()
        objectives = []
        if plan_row:
            plan = A.get(c, plan_row[0])
            objectives = [o_['text'] for o_ in plan['content'].get('objectives', [])]
        return {
            'from_project': '',
            'from_title': o['class_name'],
            'from_lesson': o['progress'],
            'summary': summary,
            'note': ('部分分析：' + analysis['content']['coverage']['label'])
            if analysis and analysis['content'].get('partial')
            else '',
            'patterns': rows_out,
            'review_focus': [],
            'objectives': objectives,
            'summary_excluded': summary_excluded,
            'analysis_ids': [analysis['id']] if analysis and not summary_excluded else [],
            'pattern_ids': used,
        }

    def class_status(self, offering_id):
        with self.store.read() as c:
            return self._class_status(c, offering_id)

    def task_class_status(self, pid):
        """本班学情 as a task of a class will send it, with the items the teacher removed shown as excluded."""
        with self.store.read() as c:
            p = self._project(c, pid)
            if not p.get('offering_id'):
                return None
            prefs = json.loads(p['status_prefs']) if p.get('status_prefs') else {}
            return self._class_status(c, p['offering_id'], prefs, include_excluded=True)

    def set_status_prefs(self, pid, exclude=None, exclude_summary=None):
        """The teacher adds or removes 本班学情 items for this task; later model calls follow the choice."""
        with self.store.tx() as c:
            p = self._project(c, pid)
            prefs = json.loads(p['status_prefs']) if p.get('status_prefs') else {}
            if exclude is not None:
                prefs['exclude'] = sorted(set(exclude))
            if exclude_summary is not None:
                prefs['exclude_summary'] = bool(exclude_summary)
            c.execute('UPDATE projects SET status_prefs=? WHERE id=?', (json.dumps(prefs, ensure_ascii=False), pid))
            event(c, pid, 'status_prefs_updated', prefs)
        return self.task_class_status(pid)

    # ----- topics (单元或主题, design doc 2.1 and 第3节 “按目标或任务查看”) -----

    def _unit(self, c, unit_id):
        r = row(c.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone())
        if r is None:
            raise KeyError(f'主题不存在：{unit_id}')
        return r

    def _ensure_unit(self, c, course_id, title):
        """The topic of this course with this title, created on first use; '' for no title."""
        title = (title or '').strip()[:60]
        if not title:
            return ''
        r = c.execute('SELECT id FROM units WHERE course_id=? AND title=?', (course_id, title)).fetchone()
        if r:
            return r[0]
        uid = new_id('unit')
        pos = c.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM units WHERE course_id=?', (course_id,)).fetchone()[
            0
        ]
        c.execute(
            'INSERT INTO units(id,course_id,title,position,created_at) VALUES(?,?,?,?,?)',
            (uid, course_id, title, pos, time.time()),
        )
        return uid

    def create_unit(self, course_id, title):
        if not (title or '').strip():
            raise ValueError('请填写主题名称')
        with self.store.tx() as c:
            self._course(c, course_id)
            return self._unit(c, self._ensure_unit(c, course_id, title))

    def rename_unit(self, unit_id, title):
        if not (title or '').strip():
            raise ValueError('请填写主题名称')
        with self.store.tx() as c:
            u = self._unit(c, unit_id)
            if c.execute(
                'SELECT 1 FROM units WHERE course_id=? AND title=? AND id!=?', (u['course_id'], title.strip(), unit_id)
            ).fetchone():
                raise Conflict('本课程已有同名主题')
            c.execute('UPDATE units SET title=? WHERE id=?', (title.strip()[:60], unit_id))
            return self._unit(c, unit_id)

    def set_project_unit(self, pid, unit_id):
        """Move a task to another topic of its course ('' = no topic)."""
        with self.store.tx() as c:
            p = self._project(c, pid)
            if unit_id and self._unit(c, unit_id)['course_id'] != p.get('course_id'):
                raise Conflict('这个主题不属于该任务所在的课程')
            c.execute('UPDATE projects SET unit_id=? WHERE id=?', (unit_id or '', pid))
            return self._project_view(self._project(c, pid))

    def units(self, course_id):
        with self.store.read() as c:
            self._course(c, course_id)
            out = []
            for u in c.execute('SELECT * FROM units WHERE course_id=? ORDER BY position', (course_id,)).fetchall():
                u = dict(u)
                pids = [r[0] for r in c.execute('SELECT id FROM projects WHERE unit_id=?', (u['id'],))]
                u['tasks'] = len(pids)
                u['assignments'] = (
                    c.execute(
                        f'SELECT COUNT(*) FROM assignments WHERE project_id IN ({",".join("?" * len(pids))})', pids
                    ).fetchone()[0]
                    if pids
                    else 0
                )
                out.append(u)
            return out

    def unit_view(self, unit_id):
        """按主题查看: what was planned and taught, what was tested, what evidence there is, per class, and what is
        still open. Objectives come from the adopted lesson plans; attainment from confirmed evidence analyses only.
        No single mastery score is computed (design doc 7.4)."""
        with self.store.read() as c:
            u = self._unit(c, unit_id)
            projects = [
                self._project(c, r[0])
                for r in c.execute('SELECT id FROM projects WHERE unit_id=? ORDER BY created_at', (unit_id,))
            ]
            pids = {p['id'] for p in projects}
            objectives = []
            linked = {}
            for p in projects:
                plan = self._plan(c, p['id'])
                if not plan:
                    continue
                for r in c.execute('SELECT * FROM objective_links WHERE artifact_id=?', (plan['id'],)):
                    linked.setdefault(r['objective_id'], []).append(
                        {'title': p['title'], 'plan_version': plan['version']}
                    )
            for oid, used in linked.items():
                o = c.execute('SELECT code, needs_review FROM objectives WHERE id=?', (oid,)).fetchone()
                v = self._latest_objective_version(c, oid)
                objectives.append(
                    {
                        'id': oid,
                        'code': o['code'],
                        'text': v['text'],
                        'success_criterion': v['success_criterion'],
                        'version': v['version'],
                        'needs_review': bool(o['needs_review']),
                        'from': '、'.join(u['title'] for u in used),
                        'plan_version': used[-1]['plan_version'],
                    }
                )
            classes = []
            for o in self._offering_views(c, u['course_id']):
                assignments = [a for a in self._assignment_views(c, offering_id=o['id']) if a['project_id'] in pids]
                evidence, patterns = [], []
                for a in assignments:
                    done = [
                        A.confirmed_in_lineage(c, x['lineage_id'])
                        for x in A.current(c, a['project_id'], 'analysis', assignment=a['id'])
                        if x['confirmed_version']
                    ]
                    done = [x for x in done if x and x['content'].get('mode') == 'evidence']
                    if done:
                        ct = done[-1]['content']
                        evidence.append(
                            {
                                'assignment': a['title'],
                                'partial': bool(ct.get('partial')),
                                'coverage': (ct.get('coverage') or {}).get('label', ''),
                                'objectives': [
                                    {k: r[k] for k in ('目标', '题号', '人数', '得分率', '全部得分人数')}
                                    for r in ct.get('objective_stats') or []
                                ],
                            }
                        )
                    for x in A.listing(
                        c, a['project_id'], 'problem_pattern', statuses=('confirmed',), assignment=a['id']
                    ):
                        patterns.append(
                            {
                                'assignment': a['title'],
                                'name': x['content']['name'],
                                'affected': f'{len(x["content"]["affected_learners"])}/{x["content"]["total_learners"]}',
                            }
                        )
                sessions = [s for s in o['sessions'] if s['project_id'] in pids]
                if sessions or assignments:
                    classes.append(
                        {
                            'offering_id': o['id'],
                            'class_name': o['class_name'],
                            'sessions': sessions,
                            'assignments': assignments,
                            'evidence': evidence,
                            'patterns': patterns,
                        }
                    )
            tasks = [t for t in self._task_views(c, course_id=u['course_id'], status=None) if t['project_id'] in pids]
        return {'unit': u, 'objectives': objectives, 'classes': classes, 'tasks': tasks}

    # ----- home (design doc 4.1) -----

    def home(self, course_id=None):
        """我的教学: courses, work to continue and what needs the teacher, all from the actual records."""
        with self.store.read() as c:
            visible = self.visible_course_ids(c)
            courses = [
                self._course(c, r['id'])
                for r in c.execute('SELECT id FROM courses WHERE archived=0 ORDER BY created_at DESC')
                if visible is None or r['id'] in visible
            ]
            course = next((x for x in courses if x['id'] == course_id), courses[0] if courses else None)
            offerings = self._offering_views(c, course['id']) if course else []
            tasks = self._task_views(c, course_id=course['id']) if course else []
            attention = []
            for o in offerings:
                for s in o['sessions']:
                    if s['overdue']:
                        attention.append(
                            {
                                'kind': 'session',
                                'offering_id': o['id'],
                                'class_name': o['class_name'],
                                'session': s,
                                'text': f'{o["class_name"]}·{s["title"]}：计划时间已过，还没有标记是否已上',
                            }
                        )
                for a in self._assignment_views(c, offering_id=o['id']):
                    if a['review_queue']:
                        attention.append(
                            {
                                'kind': 'assignment',
                                'assignment': a,
                                'project_id': a['project_id'],
                                'text': f'{a["class_name"]}·{a["title"]}：{a["review_queue"]} 份批改待复核',
                            }
                        )
                    elif a['updates']:
                        attention.append(
                            {
                                'kind': 'update',
                                'assignment': a,
                                'project_id': a['project_id'],
                                'text': f'{a["class_name"]}·{a["title"]}：{len(a["updates"])} 道题有新版本（本次布置仍用原版本）',
                            }
                        )
            # Old projects have no owner: only an admin (or the local single user) sees them to map them.
            legacy = (
                c.execute("SELECT COUNT(*) FROM projects WHERE course_id=''").fetchone()[0] if visible is None else 0
            )
        return {
            'courses': courses,
            'course': course,
            'offerings': offerings,
            'tasks': tasks,
            'attention': attention,
            'legacy_projects': legacy,
            'units': self.units(course['id']) if course else [],
            'kinds': {k: v for k, v in KINDS.items() if k != 'legacy'},
        }

    # ----- mapping old projects into courses (design doc 12) -----

    def migration_preview(self):
        """待整理: old projects not yet in a course, grouped only by their 进入下一课时 links. Same titles or same
        student codes are never merged automatically; the teacher confirms every mapping."""
        with self.store.read() as c:
            projects = [
                dict(r) for r in c.execute("SELECT * FROM projects WHERE course_id='' ORDER BY created_at").fetchall()
            ]
            parent = {p['id']: p['id'] for p in projects}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            for p in projects:
                if p['prev_project_id'] in parent:
                    parent[find(p['id'])] = find(p['prev_project_id'])
            groups = {}
            for p in projects:
                learners = [
                    r[0]
                    for r in c.execute('SELECT alias FROM learners WHERE project_id=? ORDER BY created_at', (p['id'],))
                ]
                work = sorted(
                    {
                        r[0]
                        for r in c.execute(
                            f"SELECT DISTINCT round FROM artifacts WHERE project_id=? AND assignment_id='' AND type IN ({','.join('?' * len(A.ASSIGNMENT_TYPES))})",
                            (p['id'], *A.ASSIGNMENT_TYPES),
                        )
                    }
                )
                groups.setdefault(find(p['id']), []).append(
                    {
                        'id': p['id'],
                        'title': p['title'],
                        'subject': p['subject'],
                        'grade': p['grade'],
                        'lesson': p['lesson'],
                        'knowledge_module': p['knowledge_module'],
                        'learners': learners,
                        'rounds_with_student_work': work,
                        'artifacts': c.execute(
                            'SELECT COUNT(*) FROM artifacts WHERE project_id=?', (p['id'],)
                        ).fetchone()[0],
                    }
                )
            courses = [self._course(c, r['id']) for r in c.execute('SELECT id FROM courses ORDER BY created_at DESC')]
        out = []
        for members in groups.values():
            first = members[0]
            out.append(
                {
                    'projects': members,
                    'subject': first['subject'],
                    'suggested_course': _suggest_course(first),
                    'existing_courses': [x for x in courses if x['subject'] == first['subject']],
                    'basis': '按“进入下一课时”的前后关系分组' if len(members) > 1 else '单个项目',
                }
            )
        return {'groups': out, 'note': '系统只提出候选，不按相同标题或相同学生代号自动合并；确认后才执行。'}

    def map_projects(self, project_ids, *, course_id=None, course=None, class_id=None, class_name=None, backup=True):
        """Put old projects into a course (and optionally a class), as the teacher confirmed.

        Each project becomes a task workspace. With a class, the project roster joins the class (the teacher has
        confirmed these codes are that class's students) and each round's student work becomes one assignment of
        that class, pinned to the round's confirmed versions; later rounds follow the earlier one. Artifact ids,
        versions and references do not change. The database is backed up first, and counts are checked before
        the transaction commits.
        """
        project_ids = list(dict.fromkeys(project_ids or []))
        if not project_ids:
            raise ValueError('请选择要整理的项目')
        backup_path = self.store.backup(f'map-{int(time.time())}') if backup else None
        with self.store.tx() as c:
            projects = [self._project(c, pid) for pid in project_ids]
            if any(p.get('course_id') for p in projects):
                raise Conflict('有项目已经整理到课程里了')
            subjects = {p['subject'] for p in projects}
            if course_id:
                crs = self._course(c, course_id)
            else:
                if not course or not course.get('title'):
                    raise ValueError('请选择已有课程，或填写新课程名称')
                crs = {'id': new_id('crs'), 'subject': pack(course.get('subject') or projects[0]['subject'])['id']}
                c.execute(
                    'INSERT INTO courses(id,title,subject,grade,created_at) VALUES(?,?,?,?,?)',
                    (
                        crs['id'],
                        course['title'].strip(),
                        crs['subject'],
                        (course.get('grade') or '').strip(),
                        time.time(),
                    ),
                )
            if subjects != {crs['subject']}:
                raise Conflict('项目的学科和课程不一致，不能放进同一门课程')
            before = self._mapping_counts(c, project_ids)
            offering_id = ''
            if class_id or class_name:
                if not class_id:
                    class_id = new_id('cls')
                    c.execute(
                        'INSERT INTO class_groups(id,name,created_at) VALUES(?,?,?)',
                        (class_id, class_name.strip(), time.time()),
                    )
                self._class(c, class_id)
                r = c.execute(
                    'SELECT id FROM offerings WHERE course_id=? AND class_id=?', (crs['id'], class_id)
                ).fetchone()
                offering_id = r[0] if r else new_id('off')
                if not r:
                    c.execute(
                        'INSERT INTO offerings(id,course_id,class_id,created_at) VALUES(?,?,?,?)',
                        (offering_id, crs['id'], class_id, time.time()),
                    )
            made = []
            for p in projects:
                pid = p['id']
                now = time.time()
                c.execute(
                    'UPDATE projects SET course_id=?, offering_id=?, unit_id=? WHERE id=?',
                    (crs['id'], offering_id, self._ensure_unit(c, crs['id'], p.get('knowledge_module', '')), pid),
                )
                tid = new_id('task')
                c.execute(
                    'INSERT INTO tasks(id,course_id,offering_id,project_id,kind,title,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (tid, crs['id'], offering_id, pid, 'legacy', p['title'], 'done', p['created_at'], now),
                )
                c.execute(
                    "UPDATE artifacts SET course_id=?, task_id=? WHERE project_id=? AND course_id=''",
                    (crs['id'], tid, pid),
                )
                c.execute('UPDATE materials SET course_id=? WHERE project_id=?', (crs['id'], pid))
                if offering_id:
                    made += self._map_rounds(c, p, offering_id, class_id)
            after = self._mapping_counts(c, project_ids)
            if after != before:
                raise RuntimeError(f'整理前后数据不一致，已撤销：{before} → {after}')
            event(
                c,
                crs['id'],
                'projects_mapped',
                {'projects': project_ids, 'offering_id': offering_id, 'assignments': made, 'backup': backup_path},
            )
        return {
            'course_id': crs['id'],
            'offering_id': offering_id,
            'assignments': made,
            'backup': backup_path,
            'checked': after,
        }

    def _map_rounds(self, c, p, offering_id, class_id):
        pid = p['id']
        member = {}
        for lr in c.execute('SELECT id, alias FROM learners WHERE project_id=? ORDER BY created_at', (pid,)).fetchall():
            m = c.execute(
                'SELECT id FROM class_members WHERE class_id=? AND alias=?', (class_id, lr['alias'])
            ).fetchone()
            mid = m[0] if m else new_id('mem')
            if not m:
                c.execute(
                    'INSERT INTO class_members(id,class_id,alias,source,joined_at) VALUES(?,?,?,?,?)',
                    (mid, class_id, lr['alias'], f'legacy:{pid}', time.time()),
                )
            # Historical submissions name the project learner id; the snapshot keeps it so the assignment stays
            # consistent, with the class member alongside.
            member[lr['alias']] = {'id': lr['id'], 'alias': lr['alias'], 'member_id': mid}
        rounds = sorted(
            {
                r[0]
                for r in c.execute(
                    f"SELECT DISTINCT round FROM artifacts WHERE project_id=? AND assignment_id='' AND type IN ({','.join('?' * len(A.ASSIGNMENT_TYPES))})",
                    (pid, *A.ASSIGNMENT_TYPES),
                )
            }
        )
        made, parent = [], ''
        for rnd in rounds:
            pinned = {}
            for q in A.current(c, pid, 'question', round_=rnd):
                conf = A.confirmed_in_lineage(c, q['lineage_id'])
                rubric = self._rubric_for(c, q['lineage_id']) if conf else None
                if conf and rubric:
                    pinned[q['lineage_id']] = {
                        'question': conf['id'],
                        'rubric': rubric['id'],
                        'rubric_lineage': rubric['lineage_id'],
                        'no': conf['content']['no'],
                    }
            aid = new_id('asg')
            title = p['title'] if rnd == 1 else f'{p["title"]}｜第 {rnd} 轮'
            c.execute(
                'INSERT INTO assignments(id,project_id,offering_id,round,parent_id,title,status,roster,pinned,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (
                    aid,
                    pid,
                    offering_id,
                    rnd,
                    parent,
                    title,
                    'assigned',
                    json.dumps(list(member.values()), ensure_ascii=False),
                    json.dumps(pinned, ensure_ascii=False),
                    p['created_at'],
                ),
            )
            c.execute(
                f"UPDATE artifacts SET assignment_id=? WHERE project_id=? AND round=? AND assignment_id='' AND type IN ({','.join('?' * len(A.ASSIGNMENT_TYPES))})",
                (aid, pid, rnd, *A.ASSIGNMENT_TYPES),
            )
            made.append(aid)
            parent = aid
        return made

    @staticmethod
    def _mapping_counts(c, pids):
        marks = ','.join('?' * len(pids))
        return {
            'artifacts': c.execute(f'SELECT COUNT(*) FROM artifacts WHERE project_id IN ({marks})', pids).fetchone()[0],
            'refs': c.execute(
                f'SELECT COUNT(*) FROM artifact_refs r JOIN artifacts a ON a.id=r.artifact_id WHERE a.project_id IN ({marks})',
                pids,
            ).fetchone()[0],
            'confirmed_grading_total': round(
                sum(
                    json.loads(r[0]).get('total', 0)
                    for r in c.execute(
                        f"SELECT content FROM artifacts WHERE project_id IN ({marks}) AND type='grading' AND status='confirmed'",
                        pids,
                    )
                ),
                2,
            ),
        }


__all__ = ['KINDS', 'CoursesMixin']
