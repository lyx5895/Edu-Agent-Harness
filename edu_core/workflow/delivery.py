"""Teacher deliverables and the coursemaker exchange (COURSE_WORKSPACE_DESIGN.md 9).

Deliverables are built only from adopted (confirmed) versions and say which version they are. The student paper
never contains answers, references or rubrics. Export failures are reported, never replaced by research JSON.

The coursemaker package (9.2) carries the adopted plan and questions with their versions and hashes, the objectives,
the audience and the materials coursemaker may use. It never carries student answers or rosters. Courseware coming
back is stored as a derived artifact that references the package's sources, so a later substantive change upstream
flags it like any other derived result.
"""

from __future__ import annotations

import json
import time

from edu_core import artifacts as A
from edu_core import docx
from edu_core.storage import event, new_id
from edu_core.subjects import QUESTION_TYPES
from edu_core.text import dump, sha
from edu_core.workflow.common import Conflict

KINDS = {
    'lesson_plan': '教案',
    'student_paper': '学生练习',
    'teacher_key': '教师答案与评分标准',
    'review_outline': '讲评提纲',
}
CONTRACT = 'edu-harness/coursemaker-package@1'


class DeliveryMixin:
    """Deliverable files and the coursemaker package."""

    def deliverable(self, pid, kind, fmt='html', round_=1, assignment_id=None):
        """Returns (filename, media type, bytes or text)."""
        if kind not in KINDS:
            raise ValueError('未知的导出类型')
        if fmt not in ('html', 'docx'):
            raise ValueError('导出格式应为 html 或 docx')
        with self.store.read() as c:
            p = self._project(c, pid)
            blocks = getattr(self, f'_blocks_{kind}')(c, p, round_, assignment_id)
        title = f'{p["title"]}｜{KINDS[kind]}'
        if fmt == 'docx':
            return (
                f'{title}.docx',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                docx.to_docx(blocks),
            )
        return f'{title}.html', 'text/html; charset=utf-8', docx.to_html(blocks, title)

    def _header(self, c, p, label):
        crumbs = []
        if p.get('course_id'):
            crumbs.append(self._course(c, p['course_id'])['title'])
        if p.get('offering_id'):
            crumbs.append(self._offering(c, p['offering_id'])['class_name'])
        return [
            ('h1', f'{p["title"]}　{label}'),
            ('meta', ' · '.join([*crumbs, p['pack']['name'], p.get('grade', '')]).strip(' ·')),
        ]

    def _blocks_lesson_plan(self, c, p, _round, _aid):
        plan = self._plan(c, p['id'])
        if plan is None:
            raise Conflict('还没有已确认的备课方案，不能导出教案')
        ct = plan['content']
        blocks = [*self._header(c, p, '教案'), ('meta', f'备课方案第 {plan["version"]} 版（已确认）')]
        if p.get('lesson') or p.get('lesson_scope'):
            blocks.append(('p', '　'.join(x for x in (p.get('lesson', ''), p.get('lesson_scope', '')) if x)))
        blocks.append(('h2', '教学目标'))
        blocks += [
            (
                'li',
                f'{o["id"]} {o["text"]}'
                + (f'（达成标准：{o["success_criterion"]}）' if o.get('success_criterion') else ''),
            )
            for o in ct['objectives']
        ]
        for key, label in (('key_points', '教学重点'), ('difficulties', '教学难点')):
            if ct.get(key):
                blocks += [('h2', label), *[('li', x) for x in ct[key]]]
        if ct.get('activities'):
            blocks.append(('h2', '教学过程'))
            blocks.append(
                (
                    'table',
                    [['环节', '用时', '做法', '对应目标', '检验方式']]
                    + [
                        [
                            a['name'],
                            f'{a.get("minutes", 0)} 分钟',
                            a.get('description', ''),
                            '、'.join(a.get('objective_ids', [])),
                            a.get('evidence', ''),
                        ]
                        for a in ct['activities']
                    ],
                )
            )
        # 例题 and 当堂检测: what the teacher teaches from, in print as on screen
        for i, x in enumerate(ct.get('examples', []), 1):
            if i == 1:
                blocks.append(('h2', '例题'))
            blocks += [('p', f'例 {i}　{x["problem"]}'), ('p', f'解答：{x["solution"]}')] if x.get('solution') else [('p', f'例 {i}　{x["problem"]}')]
        if ct.get('exit_check'):
            blocks.append(('h2', '当堂检测'))
            for i, x in enumerate(ct['exit_check'], 1):
                blocks.append(('p', f'{i}. {x["question"]}' + (f'　答案：{x["answer"]}' if x.get('answer') else '')))
        if ct.get('notes'):
            blocks += [('h2', '备注'), ('p', ct['notes'])]
        return blocks

    def _paper_questions(self, c, p, round_, aid):
        """(question, rubric) pairs to print: the versions this assignment pinned, or the confirmed ones."""
        try:
            scope = self.scope(c, p['id'], round_, aid)
        except Conflict:
            raise Conflict('这次布置不属于当前任务') from None
        pairs = [(q, scope.rubric(c, q['lineage_id'])) for q in scope.questions(c).values()]
        pairs = [(q, r) for q, r in pairs if q and r]
        if not pairs:
            raise Conflict('这一轮还没有已确认的题目和评分标准，不能导出')
        return sorted(pairs, key=lambda x: x[0]['content']['no'])

    def _blocks_student_paper(self, c, p, round_, aid):
        # Only stems and options: no answer key, accepted answers, reference solution or rubric (design doc 9.1).
        blocks = [*self._header(c, p, '练习'), ('p', '班级＿＿＿＿＿＿　姓名（代号）＿＿＿＿＿＿')]
        for q, _r in self._paper_questions(c, p, round_, aid):
            ct = q['content']
            blocks.append(('p', f'{ct["no"]}. {ct["stem"]}'))
            for i, opt in enumerate(ct.get('options') or []):
                blocks.append(('p', f'　　{chr(65 + i)}. {opt}'))
            blocks.append(('blank', 1 if ct.get('options') else 4))
        return blocks

    def _blocks_teacher_key(self, c, p, round_, aid):
        blocks = [*self._header(c, p, '答案与评分标准')]
        for q, r in self._paper_questions(c, p, round_, aid):
            ct = q['content']
            blocks.append(('h2', f'第 {ct["no"]} 题（{QUESTION_TYPES.get(ct["type"], ct["type"])}）'))
            blocks.append(('meta', f'题目第 {q["version"]} 版 · 评分标准第 {r["version"]} 版'))
            blocks.append(('p', ct['stem']))
            if ct.get('answer_key'):
                blocks.append(
                    (
                        'p',
                        f'答案：{ct["answer_key"]}'
                        + (f'（也可：{"、".join(ct["accepted"])}）' if ct.get('accepted') else ''),
                    )
                )
            if ct.get('reference'):
                blocks.append(('p', f'参考解答：{ct["reference"]}'))
            blocks.append(
                (
                    'table',
                    [['要点', '评分要求', '分值']]
                    + [[x['id'], x['description'], f'{x["score"]:g}'] for x in r['content']['points']]
                    + [['', '合计', f'{r["content"]["total"]:g}']],
                )
            )
        return blocks

    def _blocks_review_outline(self, c, p, round_, aid):
        plans = [x for x in A.current(c, p['id'], 'review_plan', assignment=aid or '') if x['confirmed_version']]
        if plans:
            rp = A.confirmed_in_lineage(c, plans[-1]['lineage_id'])
            ct = rp['content']
            blocks = [
                *self._header(c, p, '讲评提纲'),
                ('meta', f'讲评稿第 {rp["version"]} 版（已确认）'),
                ('h2', '讲评重点'),
                *[('li', x) for x in ct['focus']],
            ]
            for s in ct['sections']:
                blocks.append(('h2', s['title']))
                blocks += [('li', x) for x in s['key_steps']]
                if s.get('typical_quote'):
                    blocks.append(('p', f'典型错误：“{s["typical_quote"]}”'))
                if s.get('error_analysis'):
                    blocks.append(('p', s['error_analysis']))
                blocks += [('li', f'追问：{x}') for x in s.get('follow_up', [])]
            if ct.get('closing'):
                blocks += [('h2', '小结'), ('p', ct['closing'])]
            return blocks
        exps = [x for x in A.current(c, p['id'], 'explanation') if x['confirmed_version']]
        if not exps:
            raise Conflict('还没有已确认的讲评稿或讲解草稿，不能导出讲评提纲')
        ex = A.confirmed_in_lineage(c, exps[-1]['lineage_id'])
        ct = ex['content']
        blocks = [
            *self._header(c, p, '讲解提纲'),
            ('meta', f'{ct["scope_note"]} · 第 {ex["version"]} 版'),
            ('p', ct['question']),
        ]
        blocks += [
            ('h2', '讲解重点'),
            *[('li', x) for x in ct['focus']],
            ('h2', '关键步骤'),
            *[('li', x) for x in ct['steps']],
        ]
        if ct.get('pitfalls'):
            blocks += [('h2', '可能的易错点（预判）'), *[('li', x) for x in ct['pitfalls']]]
        blocks += [('h2', '课堂追问'), *[('li', x) for x in ct['follow_up']]]
        return blocks

    # ----- coursemaker (design doc 9.2) -----

    def courseware_package(self, pid):
        """The exchange package for coursemaker: adopted plan and questions with versions and hashes, objectives,
        audience, allowed materials, and a coursemaker-shaped chapter. No student text, no roster."""
        with self.store.tx() as c:
            p = self._project(c, pid)
            plan = self._plan(c, pid)
            if plan is None:
                raise Conflict('还没有已确认的备课方案，不能送去制作课件')
            ct = plan['content']
            links = {
                r['plan_objective_id']: r
                for r in c.execute('SELECT * FROM objective_links WHERE artifact_id=?', (plan['id'],))
            }
            objectives = []
            for o in ct['objectives']:
                link = links.get(o['id'])
                code = (
                    c.execute('SELECT code FROM objectives WHERE id=?', (link['objective_id'],)).fetchone()[0]
                    if link
                    else ''
                )
                objectives.append(
                    {
                        'plan_objective': o['id'],
                        'course_objective': link['objective_id'] if link else '',
                        'code': code,
                        'text': o['text'],
                        'success_criterion': o.get('success_criterion', ''),
                    }
                )
            pairs = []
            for q in A.current(c, pid, 'question', round_=1):
                conf = A.confirmed_in_lineage(c, q['lineage_id'])
                rubric = self._rubric_for(c, q['lineage_id']) if conf else None
                if conf and rubric:
                    pairs.append((conf, rubric))
            pairs.sort(key=lambda x: x[0]['content']['no'])
            sources = [plan] + [x for pair in pairs for x in pair]
            prefs = c.execute('SELECT material_prefs FROM projects WHERE id=?', (pid,)).fetchone()[0]
            excluded = set((json.loads(prefs) if prefs else {}).get('exclude') or [])
            materials = [
                {'id': m['id'], 'filename': m['filename'], 'family_id': m['family_id'], 'sha256': m['sha256']}
                for m in self._material_rows(c, pid)
                if m['id'] not in excluded
            ]
            sessions = [r[0] for r in c.execute('SELECT id FROM lesson_sessions WHERE project_id=?', (pid,))]
            course = self._course(c, p['course_id']) if p.get('course_id') else None
            unit = c.execute('SELECT id, title FROM units WHERE id=?', (p.get('unit_id', ''),)).fetchone()
            package_id = new_id('pkg')
            payload = {
                'contract': CONTRACT,
                'package_id': package_id,
                'created_at': time.time(),
                'course': {
                    'id': course['id'],
                    'title': course['title'],
                    'subject': course['subject_name'],
                    'grade': course['grade'],
                }
                if course
                else None,
                'unit': dict(unit) if unit else None,
                'project_id': pid,
                'lesson_sessions': sessions,
                'sources': [
                    {
                        'type': a['type'],
                        'id': a['id'],
                        'lineage_id': a['lineage_id'],
                        'version': a['version'],
                        'sha': a['content_sha'],
                    }
                    for a in sources
                ],
                'objectives': objectives,
                'key_points': ct.get('key_points', []),
                'difficulties': ct.get('difficulties', []),
                'activities': ct.get('activities', []),
                'questions_student': [
                    {
                        'no': q['content']['no'],
                        'type': q['content']['type'],
                        'stem': q['content']['stem'],
                        'options': q['content'].get('options', []),
                    }
                    for q, _r in pairs
                ],
                'questions_teacher': [
                    {
                        'no': q['content']['no'],
                        'answer_key': q['content'].get('answer_key', ''),
                        'reference': q['content'].get('reference', ''),
                        'rubric': r['content']['points'],
                    }
                    for q, r in pairs
                ],
                'audience': {
                    'grade': p.get('grade', ''),
                    'learner_profile': p.get('learner_profile', ''),
                    'versions': ['student', 'teacher'],
                },
                'materials_allowed': materials,
                'student_text': 'none',  # rosters and student answers are never exported (9.2)
                'presentation': {'language': 'zh-CN', 'teacher_notes': p.get('notes', '')},
                # Shaped like a coursemaker chapter (title, desc, objectives, subs, hasProject) for direct mapping.
                'coursemaker_chapter': {
                    'title': (p.get('lesson') and f'{p.get("knowledge_module") or p["title"]}｜{p["lesson"]}')
                    or p['title'],
                    'desc': p.get('lesson_scope', '') or '；'.join(ct.get('key_points', [])),
                    'objectives': [o['text'] for o in ct['objectives']],
                    'subs': [a['name'] for a in ct.get('activities', [])],
                    'hasProject': False,
                },
            }
            digest = sha(payload)
            c.execute(
                'INSERT INTO packages(id,project_id,contract,payload,sha,created_at) VALUES(?,?,?,?,?,?)',
                (package_id, pid, CONTRACT, dump(payload), digest, time.time()),
            )
            event(c, pid, 'package_created', {'package_id': package_id, 'sha': digest, 'sources': len(sources)})
            return {**payload, 'sha': digest}

    def import_courseware(self, pid, package_id, kind, files, external_run_id='', note=''):
        """Courseware made by coursemaker from a package: a derived artifact referencing the package's sources. If a
        source has a newer adopted version than the package carried, it is saved as based on an old basis."""
        if kind not in ('handout', 'slides', 'video', 'other'):
            raise ValueError('课件类型应为 handout、slides、video 或 other')
        with self.store.tx() as c:
            r = c.execute('SELECT * FROM packages WHERE id=? AND project_id=?', (package_id, pid)).fetchone()
            if r is None:
                raise KeyError('交换包不存在')
            payload = json.loads(r['payload'])
            content = {
                'package_id': package_id,
                'package_sha': r['sha'],
                'kind': kind,
                'files': [
                    {'name': str(f.get('name', ''))[:200], 'url': str(f.get('url', ''))[:500]} for f in files or []
                ],
                'external_run_id': str(external_run_id)[:120],
                'note': str(note)[:500],
            }
            refs = [(s['id'], 'source') for s in payload['sources']]
            a = A.create(c, pid, 'courseware', content, origin='imported', refs=refs, created_by='coursemaker')
            manifest = [
                {'kind': s['type'], 'id': s['id'], 'lineage_id': s['lineage_id'], 'version': s['version']}
                for s in payload['sources']
            ]
            self._guard_basis(c, pid, a['id'], manifest)
            event(c, pid, 'courseware_imported', {'artifact': a['id'], 'package_id': package_id, 'kind': kind})
            return A.get(c, a['id']) | {'stale': A.is_stale(c, a['id'])}
