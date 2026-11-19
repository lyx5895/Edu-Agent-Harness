"""03 作业批改: roster, submissions, grading rounds, review queue, overrides.

Student work is scoped by ``assignment_id``: a class assignment (its pinned question and rubric versions and its
roster snapshot), or '' / None for the project's own unbound work (the project roster and confirmed versions).
"""

from __future__ import annotations

import csv
import io
import json
import time

from edu_core import artifacts as A
from edu_core import grading
from edu_core.context import Context
from edu_core.llm import ModelUnavailable
from edu_core.storage import event, new_id, record_generation
from edu_core.text import dump
from edu_core.workflow.common import (
    Conflict,
)


class SubmissionsMixin:
    """03 作业批改."""

    # ----- roster and submissions -----

    def add_learners(self, pid, aliases):
        added = []
        with self.store.tx() as c:
            self._project(c, pid)
            for alias in aliases:
                alias = alias.strip()
                if not alias:
                    continue
                if len(alias) > 40:
                    raise ValueError(f'学生代号过长：{alias[:10]}…')
                cur = c.execute(
                    'INSERT OR IGNORE INTO learners VALUES(?,?,?,?)', (new_id('lrn'), pid, alias, time.time())
                )
                if cur.rowcount:
                    added.append(alias)
        return {'added': added, 'learners': self.learners(pid)}

    def learners(self, pid):
        with self.store.read() as c:
            return [
                dict(r)
                for r in c.execute('SELECT id, alias FROM learners WHERE project_id=? ORDER BY created_at', (pid,))
            ]

    def import_submissions(self, pid, csv_text, round_=1, add_missing=False, assignment_id=None):
        """CSV rows: 学生代号,题号,作答. Learners are matched against the roster; unmatched rows are reported."""
        reader = csv.reader(io.StringIO(csv_text.strip()))
        rows = [r for r in reader if r and any(x.strip() for x in r)]
        if rows and (len(rows[0]) < 2 or not rows[0][1].strip().isdigit()):
            rows = rows[1:]  # header
        if not rows:
            raise ValueError('没有可导入的作答（每行：学生代号,题号,作答）')
        with self.store.tx() as c:
            pid, round_, aid = self._scope(c, pid, round_, assignment_id)
            self._project(c, pid)
            questions = self._scoped_questions(c, pid, round_, aid)
            roster = self._roster(c, pid, aid)
            created, unmatched, problems = [], set(), []
            for i, r in enumerate(rows, 1):
                if len(r) < 3:
                    problems.append(f'第 {i} 行格式不对')
                    continue
                alias, no, text = r[0].strip(), r[1].strip(), ','.join(r[2:]).strip()
                if alias not in roster:
                    if not add_missing:
                        unmatched.add(alias)
                        continue
                    roster[alias] = self._add_to_roster(c, pid, aid, alias)
                q = questions.get(int(no)) if no.isdigit() else None
                if q is None:
                    problems.append(f'第 {i} 行：第 {no} 题不存在或尚未确认')
                    continue
                content = {
                    'learner_id': roster[alias],
                    'learner_alias': alias,
                    'question_lineage': q['lineage_id'],
                    'question_no': int(no),
                    'text': text,
                }
                lineage = self._submission_lineage(c, pid, roster[alias], q['lineage_id'], round_, aid)
                created.append(
                    A.create(
                        c,
                        pid,
                        'submission',
                        content,
                        origin='imported',
                        refs=[(q['id'], 'question')],
                        status='confirmed',
                        created_by='teacher',
                        round_=round_,
                        lineage_id=lineage,
                        change_kind='substantive',
                        assignment_id=aid,
                    )['id']
                )
            event(c, pid, 'submissions_imported', {'count': len(created), 'round': round_, 'assignment_id': aid})
        return {'created': len(created), 'unmatched_learners': sorted(unmatched), 'problems': problems}

    def _submission_lineage(self, c, pid, learner_id, question_lineage, round_, aid=''):
        for a in A.listing(c, pid, 'submission', round_=round_, assignment=aid or ''):
            if a['content']['learner_id'] == learner_id and a['content']['question_lineage'] == question_lineage:
                return a['lineage_id']
        return None

    def submissions(self, pid, round_=1, assignment_id=None):
        with self.store.read() as c:
            pid, round_, aid = self._scope(c, pid, round_, assignment_id)
            return A.current(c, pid, 'submission', round_=round_, assignment=aid)

    # ----- grading -----

    def grade_round(self, pid, round_=1, regrade=False, assignment_id=None):
        """Grade every submission that has no current grading (or whose grading is stale).

        Each grading is written as soon as it is done, so a crash or a failed call only leaves the remaining
        submissions to grade on the next run. A grading whose inputs changed while the model ran is saved with
        an open "依据已变化" mark and does not become the valid result.
        """
        llm = self._model()
        jobs, skipped = [], []
        with self.store.read() as c:
            pid, round_, aid = self._scope(c, pid, round_, assignment_id)
            p = self._project(c, pid)
            graded = {}
            for g in A.current(c, pid, 'grading', round_=round_, assignment=aid):
                graded[g['content']['submission_lineage']] = g
            for s in A.current(c, pid, 'submission', round_=round_, assignment=aid):
                q_lin = s['content']['question_lineage']
                if aid:
                    pin = self._assignment_row(c, aid)['pinned'].get(q_lin)
                    q = A.get(c, pin['question']) if pin else None
                else:
                    q = A.confirmed_in_lineage(c, q_lin)
                rubric = self._scoped_rubric(c, q_lin, aid)
                if q is None or rubric is None:
                    skipped.append({'submission': s['id'], 'why': '题目或评分标准尚未确认'})
                    continue
                prior = graded.get(s['lineage_id'])
                # Only substantive upstream changes (open stale marks) trigger a regrade; cosmetic edits do not.
                if prior is not None and not A.is_stale(c, prior['id']) and not regrade:
                    continue
                ctx = Context('grade')
                question = ctx.add_artifact(q)
                rub = ctx.add_artifact(rubric)
                sub = ctx.add_artifact(s)
                jobs.append((ctx, question, rub, sub, s, prior))

        def save(job, result, metas):
            ctx, _question, _rub, sub, s, prior = job
            with self.store.tx() as c:
                gids = [record_generation(c, pid, m, ctx.manifest, grading.PROMPT_VERSION) for m in metas]
                content = {
                    'submission_id': s['id'],
                    'submission_lineage': s['lineage_id'],
                    'learner_id': sub['learner_id'],
                    'learner_alias': sub['learner_alias'],
                    'question_lineage': sub['question_lineage'],
                    'question_no': sub['question_no'],
                    'generation_ids': gids,
                    **result,
                }
                current = A.current(c, pid, 'grading', round_=round_, assignment=aid)
                latest = next((g for g in current if g['content']['submission_lineage'] == s['lineage_id']), None)
                g = A.create(
                    c,
                    pid,
                    'grading',
                    content,
                    origin='generated',
                    refs=ctx.refs(),
                    generation_id=gids[0] if gids else None,
                    status='draft',
                    round_=round_,
                    lineage_id=latest['lineage_id'] if latest else (prior['lineage_id'] if prior else None),
                    assignment_id=aid,
                )
                if prior:
                    c.execute(
                        "UPDATE stale_marks SET status='resolved', resolution='redone', resolved_at=? WHERE artifact_id=? AND status='open'",
                        (time.time(), prior['id']),
                    )
                self._guard_basis(c, pid, g['id'], ctx.manifest, aid)
                return g['id']

        def run(job):
            _ctx, question, rub, sub, _s, _prior = job
            try:
                result, metas = grading.grade(
                    llm,
                    subject=p['subject'],
                    question=question,
                    rubric=rub,
                    answer=sub['text'],
                    review=True,
                    audit_rate=self.settings.audit_rate,
                    rng=self.rng,
                )
                return save(job, result, metas), None
            except ModelUnavailable as e:
                return None, str(e)
            except Exception as e:  # noqa: BLE001 - one bad submission must not lose the rest of the round
                return None, f'批改出错：{type(e).__name__}: {e}'

        outcomes = self._parallel(run, jobs)
        made, failed = [], []
        for job, (gid, error) in zip(jobs, outcomes):
            sub = job[3]
            if error:
                failed.append({'learner': sub['learner_alias'], 'question_no': sub['question_no'], 'error': error})
            else:
                made.append(gid)
        return {
            'graded': len(made),
            'failed': failed,
            'skipped': skipped,
            'review_queue': len(self.review_queue(pid, round_, assignment_id=aid or None)),
        }

    def gradings(self, pid, round_=1, assignment_id=None):
        with self.store.read() as c:
            pid, round_, aid = self._scope(c, pid, round_, assignment_id)
            return A.current(c, pid, 'grading', round_=round_, assignment=aid)

    def review_queue(self, pid, round_=1, assignment_id=None):
        with self.store.read() as c:
            pid, round_, aid = self._scope(c, pid, round_, assignment_id)
            queue = []
            for g in A.current(c, pid, 'grading', round_=round_, assignment=aid):
                if g['status'] != 'draft' or not g['content'].get('needs_teacher'):
                    continue
                s = A.get(c, g['content']['submission_id'])
                q = next((r for r in g['refs'] if r['role'] == 'question'), None) if 'refs' in g else None
                q = A.get(c, q['ref_id']) if q else A.confirmed_in_lineage(c, g['content']['question_lineage'])
                queue.append(
                    {
                        **g,
                        'answer': s['content']['text'],
                        'photos': s['content'].get('photos', []),
                        'question': q['content'] if q else None,
                    }
                )
            return sorted(queue, key=lambda g: (-len(g['content']['risks']), g['content']['question_no']))

    def override(self, grading_id, points, note='', teacher='teacher'):
        """Teacher grading: new confirmed version; downstream analyses become stale."""
        with self.store.tx() as c:
            g = A.get(c, grading_id)
            if g['type'] != 'grading':
                raise ValueError('只能改判批改结果')
            if g['status'] == 'superseded':
                raise Conflict('这条批改已有更新的版本，请刷新')
            content = json.loads(dump(g['content']))
            by_id = {p['id']: p for p in content['points']}
            for change in points:
                p = by_id.get(change.get('id'))
                if p is None:
                    raise ValueError(f'未知评分要点：{change.get("id")}')
                score = float(change['score'])
                if not 0 <= score <= p['max']:
                    raise ValueError(f'{p["id"]} 的分数应在 0 到 {p["max"]} 之间')
                status = 'earned' if score >= p['max'] else ('missed' if score == 0 else 'partial')
                p.update(
                    score=score,
                    status=status,
                    source='teacher',
                    confidence='high',
                    error_tag=change.get('error_tag', p.get('error_tag', '')) if status != 'earned' else '',
                    error_note=change.get('error_note', p.get('error_note', '')) if status != 'earned' else '',
                    reason=change.get('reason') or '老师判定',
                )
                if status != 'earned' and not p['error_tag']:
                    p['error_tag'] = '其他'
            if any(p['status'] == 'undetermined' for p in content['points']):
                raise ValueError('还有未判定的评分要点，请全部给分后再提交')
            content.update(
                total=round(sum(p['score'] for p in content['points']), 2),
                undetermined=0,
                risks=[],
                needs_teacher=False,
                teacher_note=note,
                overridden_from=grading_id,
            )
            return A.create(
                c,
                g['project_id'],
                'grading',
                content,
                origin='teacher',
                refs=[(r['ref_id'], r['role']) for r in g['refs']],
                lineage_id=g['lineage_id'],
                status='confirmed',
                created_by=teacher,
                round_=g['round'],
                edit_of=grading_id,
                change_kind='substantive',
                assignment_id=g.get('assignment_id', ''),
            )

    def confirm_gradings(self, pid, round_=1, ids=None, teacher='teacher', assignment_id=None):
        """Confirm gradings; without ids, every draft that is not in the review queue.

        The batch only covers results that pass the rules: queue items and results with an open "依据已变化" mark
        are skipped (and counted); naming one of them explicitly is refused.
        """
        done, held = [], 0
        with self.store.tx() as c:
            pid, round_, aid = self._scope(c, pid, round_, assignment_id)
            for g in A.current(c, pid, 'grading', round_=round_, assignment=aid):
                if g['status'] != 'draft':
                    continue
                if ids is not None and g['id'] not in ids:
                    continue
                if g['stale']:
                    if ids is not None:
                        raise Conflict(
                            f'{g["content"]["learner_alias"]} 第 {g["content"]["question_no"]} 题的批改依据已变化，请先处理'
                        )
                    held += 1
                    continue
                if ids is None and g['content'].get('needs_teacher'):
                    held += 1
                    continue
                if g['content'].get('undetermined') or g['content'].get('needs_teacher'):
                    raise Conflict(
                        f'{g["content"]["learner_alias"]} 第 {g["content"]["question_no"]} 题还有未判定的要点或待复核的疑点，请先改判'
                    )
                A.confirm(c, g['id'], teacher=teacher)
                done.append(g['id'])
        return {'confirmed': len(done), 'held': held}
