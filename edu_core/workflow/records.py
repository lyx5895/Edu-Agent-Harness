"""Staleness, overview (drives 下一步) and the research export."""

from __future__ import annotations

import json
import time

from edu_core import artifacts as A
from edu_core import progress as live
from edu_core.storage import row
from edu_core.workflow.steps import blockers, next_steps, stage_status


class RecordsMixin:
    """Records and overview."""

    # ----- staleness, overview, export -----

    def stale(self, pid):
        """Open 依据已变化 marks, each told apart for the teacher: which class, what exactly (部分/完整分析, which
        pattern, whose answer), and whether a newer result of the same kind already exists in that class and round."""
        with self.store.read() as c:
            marks = A.stale(c, pid)
            classes = {a['id']: a['class_name'] for a in self._assignment_views(c, project_id=pid)}
            for m in marks:
                m['class_name'] = classes.get(m['assignment_id'], '')
                art = row(
                    c.execute('SELECT content, created_at FROM artifacts WHERE id=?', (m['artifact_id'],)).fetchone()
                )
                ct = (art or {}).get('content') or {}
                m['detail'] = ''
                if m['type'] == 'analysis':
                    m['detail'] = (
                        '部分分析'
                        if ct.get('partial')
                        else ('可能的易错点' if ct.get('mode') == 'pitfalls' else '完整分析')
                    )
                elif m['type'] == 'problem_pattern':
                    m['detail'] = ct.get('name', '')
                elif m['type'] == 'grading':
                    m['detail'] = f'{ct.get("learner_alias", "")} 第 {ct.get("question_no", "")} 题'
                newer = c.execute(
                    'SELECT 1 FROM artifacts WHERE project_id=? AND type=? AND assignment_id=? AND round=? '
                    "AND status!='superseded' AND created_at>? AND lineage_id!=? "
                    "AND id NOT IN (SELECT artifact_id FROM stale_marks WHERE status='open') LIMIT 1",
                    (pid, m['type'], m['assignment_id'], m['round'], (art or {}).get('created_at', 0), m['lineage_id']),
                ).fetchone()
                m['redone'] = bool(newer)
            return marks

    def progress(self, pid):
        """Model operations running in this project right now: their current step, items done, seconds so far, and
        how long this kind of operation took recently (the median of the last 10 that succeeded)."""
        with self.store.read() as c:
            self._project(c, pid)
            out = []
            for x in live.running(pid):
                past = [
                    r[0]
                    for r in c.execute(
                        "SELECT finished_at-started_at FROM runs WHERE kind=? AND status='succeeded' "
                        'AND finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 10',
                        (x['kind'],),
                    )
                ]
                typical = sorted(past)[len(past) // 2] if past else None
                out.append(
                    {
                        'kind': x['kind'],
                        'label': x['label'],
                        'step': x['step'],
                        'done': x['done'],
                        'total': x['total'],
                        'elapsed': round(time.time() - x['started_at']),
                        'typical': round(typical) if typical else None,
                    }
                )
            return out

    def resolve_stale(self, mark_id, resolution):
        with self.store.tx() as c:
            A.resolve_stale(c, mark_id, resolution)
        return {'ok': True}

    def overview(self, pid, assignment_id=None):
        """Project status and 下一步. Student work is counted for one scope: ``assignment_id``, else the project's
        unbound work, else (when all its student work is in class assignments) the latest assignment."""
        with self.store.read() as c:
            p = self._project_view(self._project(c, pid))
            assignments = self._assignment_views(c, project_id=pid)
            if assignment_id is None and assignments:
                unbound = c.execute(
                    f"SELECT 1 FROM artifacts WHERE project_id=? AND assignment_id='' AND type IN ({','.join('?' * len(A.ASSIGNMENT_TYPES))}) LIMIT 1",
                    (pid, *A.ASSIGNMENT_TYPES),
                ).fetchone()
                if not unbound:
                    assignment_id = assignments[-1]['id']
            scope = self.scope(c, pid, None, assignment_id)
            aid = scope.assignment_id
            course, offerings, class_name, units = None, [], '', []
            if p.get('course_id'):
                crs = self._course(c, p['course_id'])
                course = {'id': crs['id'], 'title': crs['title']}
                offerings = [{'id': o['id'], 'class_name': o['class_name']} for o in self._offering_views(c, crs['id'])]
                class_name = next((o['class_name'] for o in offerings if o['id'] == p.get('offering_id')), '')
                units = [
                    {'id': r['id'], 'title': r['title']}
                    for r in c.execute('SELECT id, title FROM units WHERE course_id=? ORDER BY position', (crs['id'],))
                ]
            task = c.execute('SELECT id, kind, status FROM tasks WHERE project_id=? LIMIT 1', (pid,)).fetchone()
            plan_pending = not self._plan(c, pid) and self._teacher_draft(self._plan(c, pid, confirmed=False))

            def scoped(t, **kw):
                return scope.current(c, t, **kw)

            counts = {}
            for t in A.TYPES:
                items = scoped(t)
                counts[t] = {'total': len(items), 'confirmed': sum(1 for a in items if a['confirmed_version'])}
            rounds = sorted(
                {r[0] for r in c.execute('SELECT DISTINCT round FROM artifacts WHERE project_id=?', (pid,))}
            ) or [1]
            mats = c.execute('SELECT COUNT(*) FROM materials WHERE project_id=?', (pid,)).fetchone()[0]
            learners = len(scope.roster(c))
            open_ab = c.execute(
                "SELECT candidate_group, display, shown_first, paired_a_index FROM comparisons "
                "WHERE project_id=? AND status='shown' AND candidate_group NOT IN "
                '(SELECT candidate_group FROM choices WHERE revoked_at IS NULL) ORDER BY created_at',
                (pid,),
            ).fetchall()
            pending_ab = [r['candidate_group'] for r in open_ab]
            ab_pairs = []
            for r in open_ab:
                display = json.loads(r['display'] or '{}')
                ab_pairs.append(
                    {
                        'group': r['candidate_group'],
                        'a': (display.get('A') or {}).get('question'),
                        'b': (display.get('B') or {}).get('question'),
                        # The position the teacher actually saw: A first is not assumed.
                        'shown_first': r['shown_first'] or display.get('shown_first', 'A'),
                        'paired_a_index': r['paired_a_index'],
                    }
                )
            latest = scope.round if scope.is_assignment else max(rounds)
            nxt_row = c.execute(
                'SELECT id, title FROM projects WHERE prev_project_id=? ORDER BY created_at DESC LIMIT 1', (pid,)
            ).fetchone()
            round_counts = {}
            for t in A.TYPES:
                items = scoped(t, round_=latest)
                round_counts[t] = {'total': len(items), 'confirmed': sum(1 for a in items if a['confirmed_version'])}
        stale = self.stale(pid)
        if scope.is_assignment:
            queue = len(self.review_queue(pid, latest, assignment_id=scope.assignment_id))
        else:
            queue = sum(len(self.review_queue(pid, r)) for r in rounds)
        st = {
            'task_kind': task['kind'] if task else '',
            'counts': counts,
            'round': round_counts,
            'r': latest,
            'materials': mats,
            'has_context': bool(mats or p.get('knowledge_module')),
            'plan_pending': plan_pending,
            'learners': learners,
            'queue': len(self.review_queue(pid, latest, assignment_id=aid or None)),
            'pending_ab': pending_ab,
            'stale': stale,
            'aid': aid or '',
        }
        nxt = next_steps(st)
        return {
            'project': p,
            'counts': counts,
            'rounds': rounds,
            'materials': mats,
            'learners': learners,
            'stale': stale,
            'review_queue': queue,
            'pending_ab': pending_ab,
            'ab_pairs': ab_pairs,
            'next': nxt,
            'latest_round': latest,
            'next_lesson': dict(nxt_row) if nxt_row else None,
            'stages': stage_status(counts, round_counts, latest),
            'blockers': blockers(st),
            'assignment_id': aid,
            'assignments': assignments,
            'course': course,
            'class_name': class_name,
            'offerings': offerings,
            'units': units,
            'task': dict(task) if task else None,
        }

    def export(self, pid):
        """Research export (design doc 10): artifacts with all versions, references, generations, choices, events."""
        with self.store.read() as c:
            p = self._project_view(self._project(c, pid))
            arts = [row(r) for r in c.execute('SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at', (pid,))]
            refs = [
                dict(r)
                for r in c.execute(
                    'SELECT r.* FROM artifact_refs r JOIN artifacts a ON a.id=r.artifact_id WHERE a.project_id=?',
                    (pid,),
                )
            ]
            gens = [
                row(r) for r in c.execute('SELECT * FROM generations WHERE project_id=? ORDER BY created_at', (pid,))
            ]
            choices = [dict(r) for r in c.execute('SELECT * FROM choices WHERE project_id=?', (pid,))]
            events = [row(r) for r in c.execute('SELECT * FROM events WHERE project_id=? ORDER BY id', (pid,))]
            marks = [dict(r) for r in c.execute('SELECT * FROM stale_marks WHERE project_id=?', (pid,))]
            mats = self._material_rows(c, pid)
            learners = [dict(r) for r in c.execute('SELECT id, alias FROM learners WHERE project_id=?', (pid,))]
            assignments = [row(r) for r in c.execute('SELECT * FROM assignments WHERE project_id=?', (pid,))]
            tasks = [dict(r) for r in c.execute('SELECT * FROM tasks WHERE project_id=?', (pid,))]
            sessions = [dict(r) for r in c.execute('SELECT * FROM lesson_sessions WHERE project_id=?', (pid,))]
            runs = [dict(r) for r in c.execute('SELECT * FROM runs WHERE project_id=? ORDER BY started_at', (pid,))]
            comparisons = [row(r) for r in c.execute('SELECT * FROM comparisons WHERE project_id=?', (pid,))]
            cases = [row(r) for r in c.execute('SELECT * FROM research_cases WHERE project_id=?', (pid,))]
            groups = self._source_groups(c, [pid])
            accounts = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        by_id = {a['id']: a for a in arts}
        edits = []
        for a in arts:
            if a['edit_of'] and a['edit_of'] in by_id:
                before = by_id[a['edit_of']]['content']
                changed = sorted(k for k in set(before) | set(a['content']) if before.get(k) != a['content'].get(k))
                edits.append(
                    {'artifact_id': a['id'], 'edit_of': a['edit_of'], 'origin': a['origin'], 'changed_fields': changed}
                )
        for a in arts:
            a['source_group'] = groups.get(a['id'], '')
        manifest = {
            'project': p,
            'exported_at': time.time(),
            'counts': {
                # Separate counts (design doc 11.8): teacher tasks, runs, internal model calls, versions and cases.
                'teacher_tasks': len(tasks),
                'runs': len(runs),
                'model_calls': len(gens),
                'research_cases': len(cases),
                'assignments': len(assignments),
                'material_families': len({m['family_id'] for m in mats}),
                'source_groups': len(set(groups.values())),
                'course_projects': 1,
                # Only real accounts identify contributors; without them the number of teachers is not guessed.
                'contributors': len({r['user_id'] for r in runs if r['user_id']}) if accounts else None,
                'model_responses': len(gens),
                'artifact_versions': len(arts),
                'artifact_lineages': len({a['lineage_id'] for a in arts}),
            },
            'note': '研究案例按任务阶段整理，重试、内部复核和多个版本不增加案例；同一来源组的产物应作为一组切分。学生只以代号出现，导出前仍需去标识检查。研究导出不是教师文件，教师文件请用“导出文件”。',
        }
        return {
            'manifest': manifest,
            'materials': mats,
            'learners': learners,
            'artifacts': arts,
            'refs': refs,
            'generations': gens,
            'choices': choices,
            'events': events,
            'stale_marks': marks,
            'edits': edits,
            'assignments': assignments,
            'tasks': tasks,
            'lesson_sessions': sessions,
            'runs': runs,
            'comparisons': comparisons,
            'research_cases': cases,
        }
