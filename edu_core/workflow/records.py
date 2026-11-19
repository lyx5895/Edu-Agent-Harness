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

    def overview(self, pid):
        with self.store.read() as c:
            p = self._project_view(self._project(c, pid))
            counts = {}
            for t in A.TYPES:
                items = A.current(c, pid, t)
                counts[t] = {'total': len(items), 'confirmed': sum(1 for a in items if a['confirmed_version'])}
            rounds = sorted(
                {r[0] for r in c.execute('SELECT DISTINCT round FROM artifacts WHERE project_id=?', (pid,))}
            ) or [1]
            mats = c.execute('SELECT COUNT(*) FROM materials WHERE project_id=?', (pid,)).fetchone()[0]
            learners = c.execute('SELECT COUNT(*) FROM learners WHERE project_id=?', (pid,)).fetchone()[0]
            pending_ab = [
                r[0]
                for r in c.execute(
                    'SELECT DISTINCT candidate_group FROM generations WHERE project_id=? AND candidate_group IS NOT NULL '
                    'AND candidate_group NOT IN (SELECT candidate_group FROM choices)',
                    (pid,),
                )
            ]
            latest = max(rounds)
            nxt_row = c.execute(
                'SELECT id, title FROM projects WHERE prev_project_id=? ORDER BY created_at DESC LIMIT 1', (pid,)
            ).fetchone()
            round_counts = {}
            for t in A.TYPES:
                items = A.current(c, pid, t, round_=latest)
                round_counts[t] = {'total': len(items), 'confirmed': sum(1 for a in items if a['confirmed_version'])}
        stale = self.stale(pid)
        queue = sum(len(self.review_queue(pid, r)) for r in rounds)
        nxt = next_steps(
            {
                'counts': counts,
                'round': round_counts,
                'r': latest,
                'materials': mats,
                'has_context': bool(mats or p.get('knowledge_module')),
                'learners': learners,
                'queue': len(self.review_queue(pid, latest)),
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
            mats = [dict(r) for r in c.execute('SELECT * FROM materials WHERE project_id=?', (pid,))]
            learners = [dict(r) for r in c.execute('SELECT id, alias FROM learners WHERE project_id=?', (pid,))]
        by_id = {a['id']: a for a in arts}
        edits = []
        for a in arts:
            if a['edit_of'] and a['edit_of'] in by_id:
                before = by_id[a['edit_of']]['content']
                changed = sorted(k for k in set(before) | set(a['content']) if before.get(k) != a['content'].get(k))
                edits.append(
                    {'artifact_id': a['id'], 'edit_of': a['edit_of'], 'origin': a['origin'], 'changed_fields': changed}
                )
        teachers = {a['created_by'] for a in arts if a['created_by'] not in ('model', 'server')}
        manifest = {
            'project': p,
            'exported_at': time.time(),
            'counts': {
                'reviewed_tasks': len({(g['task'], g['candidate_group'] or g['id']) for g in gens}),
                'material_families': len({m['family_id'] for m in mats}),
                'course_projects': 1,
                'teachers': len(teachers),
                'model_responses': len(gens),
                'artifact_versions': len(arts),
                'artifact_lineages': len({a['lineage_id'] for a in arts}),
            },
            'note': '多次回答与多个版本不计为新增案例；同一材料家族的产物应作为一组切分。学生只以代号出现，导出前仍需去标识检查。',
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
        }
