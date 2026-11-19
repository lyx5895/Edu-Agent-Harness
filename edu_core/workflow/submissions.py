"""03 作业批改: roster, submissions, grading rounds, review queue, overrides."""

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

    def import_submissions(self, pid, csv_text, round_=1, add_missing=False):
        """CSV rows: 学生代号,题号,作答. Learners are matched against the roster; unmatched rows are reported."""
        reader = csv.reader(io.StringIO(csv_text.strip()))
        rows = [r for r in reader if r and any(x.strip() for x in r)]
        if rows and (len(rows[0]) < 2 or not rows[0][1].strip().isdigit()):
            rows = rows[1:]  # header
        if not rows:
            raise ValueError('没有可导入的作答（每行：学生代号,题号,作答）')
        with self.store.tx() as c:
            self._project(c, pid)
            questions = {
                a['content']['no']: A.confirmed_in_lineage(c, a['lineage_id'])
                for a in A.current(c, pid, 'question', round_=round_)
                if a['confirmed_version']
            }
            roster = {
                r['alias']: r['id'] for r in c.execute('SELECT id, alias FROM learners WHERE project_id=?', (pid,))
            }
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
                    roster[alias] = new_id('lrn')
                    c.execute('INSERT INTO learners VALUES(?,?,?,?)', (roster[alias], pid, alias, time.time()))
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
                lineage = self._submission_lineage(c, pid, roster[alias], q['lineage_id'], round_)
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
                    )['id']
                )
            event(c, pid, 'submissions_imported', {'count': len(created), 'round': round_})
        return {'created': len(created), 'unmatched_learners': sorted(unmatched), 'problems': problems}

    def _submission_lineage(self, c, pid, learner_id, question_lineage, round_):
        for a in A.listing(c, pid, 'submission', round_=round_):
            if a['content']['learner_id'] == learner_id and a['content']['question_lineage'] == question_lineage:
                return a['lineage_id']
        return None

    def submissions(self, pid, round_=1):
        with self.store.read() as c:
            return A.current(c, pid, 'submission', round_=round_)

    # ----- grading -----

    def grade_round(self, pid, round_=1, regrade=False):
        llm = self._model()
        jobs, skipped = [], []
        with self.store.read() as c:
            p = self._project(c, pid)
            graded = {}
            for g in A.current(c, pid, 'grading', round_=round_):
                graded[g['content']['submission_lineage']] = g
            for s in A.current(c, pid, 'submission', round_=round_):
                q = A.confirmed_in_lineage(c, s['content']['question_lineage'])
                rubric = self._rubric_for(c, s['content']['question_lineage'])
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
                return job, result, metas, None
            except ModelUnavailable as e:
                return job, None, [], str(e)
            except Exception as e:  # noqa: BLE001 - one bad submission must not lose the rest of the round
                return job, None, [], f'批改出错：{type(e).__name__}: {e}'

        outcomes = self._parallel(run, jobs)
        made, failed = [], []
        with self.store.tx() as c:
            for (ctx, _question, _rub, sub, s, prior), result, metas, error in outcomes:
                if error:
                    failed.append({'learner': sub['learner_alias'], 'question_no': sub['question_no'], 'error': error})
                    continue
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
                    lineage_id=prior['lineage_id'] if prior else None,
                )
                if prior:
                    c.execute(
                        "UPDATE stale_marks SET status='resolved', resolution='redone', resolved_at=? WHERE artifact_id=? AND status='open'",
                        (time.time(), prior['id']),
                    )
                made.append(g['id'])
        return {
            'graded': len(made),
            'failed': failed,
            'skipped': skipped,
            'review_queue': len(self.review_queue(pid, round_)),
        }

    def gradings(self, pid, round_=1):
        with self.store.read() as c:
            return A.current(c, pid, 'grading', round_=round_)

    def review_queue(self, pid, round_=1):
        with self.store.read() as c:
            queue = []
            for g in A.current(c, pid, 'grading', round_=round_):
                if g['status'] != 'draft' or not g['content'].get('needs_teacher'):
                    continue
                s = A.get(c, g['content']['submission_id'])
                q = A.confirmed_in_lineage(c, g['content']['question_lineage'])
                queue.append({**g, 'answer': s['content']['text'], 'question': q['content'] if q else None})
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
            )

    def confirm_gradings(self, pid, round_=1, ids=None, teacher='teacher'):
        """Confirm gradings; without ids, every draft that is not in the review queue."""
        done = []
        with self.store.tx() as c:
            for g in A.current(c, pid, 'grading', round_=round_):
                if g['status'] != 'draft':
                    continue
                if ids is None and g['content'].get('needs_teacher'):
                    continue
                if ids is not None and g['id'] not in ids:
                    continue
                if g['content'].get('undetermined'):
                    raise Conflict(
                        f'{g["content"]["learner_alias"]} 第 {g["content"]["question_no"]} 题还有未判定的要点，请先改判'
                    )
                A.confirm(c, g['id'], teacher=teacher)
                done.append(g['id'])
        return {'confirmed': len(done)}
