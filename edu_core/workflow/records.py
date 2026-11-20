"""Staleness, overview (drives 下一步) and the research export."""

from __future__ import annotations

import time

from edu_core import artifacts as A
from edu_core.storage import row
from edu_core.workflow.steps import next_steps, stage_status


class RecordsMixin:
    """Records and overview."""

    # ----- staleness, overview, export -----

    def stale(self, pid):
        with self.store.read() as c:
            return A.stale(c, pid)

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
            pending_ab = [
                r[0]
                for r in c.execute(
                    'SELECT DISTINCT candidate_group FROM generations WHERE project_id=? AND candidate_group IS NOT NULL '
                    'AND candidate_group NOT IN (SELECT candidate_group FROM choices WHERE revoked_at IS NULL)',
                    (pid,),
                )
            ]
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
        nxt = next_steps(
            {
                'counts': counts,
                'round': round_counts,
                'r': latest,
                'materials': mats,
                'has_context': bool(mats or p.get('knowledge_module')),
                'learners': learners,
                'queue': len(self.review_queue(pid, latest, assignment_id=aid or None)),
                'pending_ab': pending_ab,
                'stale': stale,
            }
        )
        return {
            'project': p,
            'counts': counts,
            'rounds': rounds,
            'materials': mats,
            'learners': learners,
            'stale': stale,
            'review_queue': queue,
            'pending_ab': pending_ab,
            'next': nxt,
            'latest_round': latest,
            'next_lesson': dict(nxt_row) if nxt_row else None,
            'stages': stage_status(counts, round_counts, latest),
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
