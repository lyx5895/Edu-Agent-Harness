"""05 讲题答疑: 讲评稿, 讲评后再练 and the two-round comparison."""

from __future__ import annotations

from edu_core import artifacts as A
from edu_core.context import Context, describe
from edu_core.storage import record_generation
from edu_core.subjects import QUESTION_TYPES, SUGGESTED_ONLY_TYPES
from edu_core.text import verbatim_quote
from edu_core.workflow.common import (
    Conflict,
    _json,
    usable_drafts,
)
from edu_core.workflow.prompts import (
    PRACTICE_SYSTEM,
    REVIEW_SYSTEM,
)
from edu_core.workflow.schemas import (
    QuestionSet,
    ReviewOutput,
)


class ReviewMixin:
    """05 讲题答疑."""

    # ----- 05 讲题答疑 -----

    def _round_patterns(self, c, pid, round_):
        """Confirmed problem patterns as they were in ``round_`` (a later round may add a newer version)."""
        best = {}
        for a in A.listing(c, pid, 'problem_pattern', statuses=('confirmed', 'superseded'), round_=round_):
            if a['status'] == 'superseded' and not a['change_kind']:
                continue  # a superseded draft was never adopted
            if a['lineage_id'] not in best or a['version'] > best[a['lineage_id']]['version']:
                best[a['lineage_id']] = a
        return sorted(best.values(), key=lambda a: a['created_at'])

    def draft_review(self, pid, round_=None, pattern_ids=None):
        """讲评稿: focus, key steps, typical errors quoted from students, and follow-up questions per pattern."""
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            round_ = round_ or self.max_round(pid)
            pats = self._round_patterns(c, pid, round_)
            if pattern_ids:
                pats = [x for x in pats if x['id'] in pattern_ids or x['lineage_id'] in pattern_ids]
            if not pats:
                raise Conflict(f'第 {round_} 轮还没有已确认的问题模式，请先在学情分析中确认')
            ctx = Context('draft_review')
            ctx.add_other('project', pid)
            plan = self._plan(c, pid)
            if plan:
                ctx.add_artifact(plan)
            goals = [g['content']['text'] for g in self._goals(plan)]
            key_points = plan['content'].get('key_points', []) if plan else []
            analyses = [a for a in A.current(c, pid, 'analysis', round_=round_)]
            summary = ''
            if analyses:
                ctx.add_artifact(analyses[-1], require_confirmed=False)
                summary = analyses[-1]['content'].get('summary', '')
            nos = sorted({e['question_no'] for x in pats for e in x['content']['evidence']})
            originals = []
            for q in A.current(c, pid, 'question', round_=round_):
                conf = A.confirmed_in_lineage(c, q['lineage_id'])
                if conf and conf['content']['no'] in nos:
                    rubric = self._rubric_for(c, q['lineage_id'])
                    originals.append(
                        {
                            '题号': conf['content']['no'],
                            '题目': ctx.add_artifact(conf)['stem'][:600],
                            '参考答案': conf['content']['reference'][:600],
                            '评分要点': [x['description'] for x in ctx.add_artifact(rubric)['points']]
                            if rubric
                            else [],
                        }
                    )
            pat_list = [
                {
                    'index': i,
                    '名称': ctx.add_artifact(x, require_confirmed=x['status'] == 'confirmed')['name'],
                    '描述': x['content']['description'],
                    '涉及人数': f'{len(x["content"]["affected_learners"])}/{x["content"]["total_learners"]}',
                    '学生原文': [e['quote'] for e in x['content']['evidence'][:4]],
                }
                for i, x in enumerate(pats)
            ]
            prior = next(iter(A.current(c, pid, 'review_plan', round_=round_)), None)
        user = _json(
            {
                '学科': p['pack']['name'],
                '教学目标': goals,
                '本课重点': key_points,
                '学情概括': summary,
                '原题': originals,
                '问题模式': pat_list,
            }
        )
        out, meta = llm.complete(
            'generator', 'draft_review', REVIEW_SYSTEM.format(subject=p['pack']['name']), user, ReviewOutput
        )
        sections, dropped = [], []
        for sec in out.sections:
            if not 0 <= sec.pattern_index < len(pats):
                dropped.append(sec.title)
                continue
            pat = pats[sec.pattern_index]
            quote, source = '', None
            if sec.typical_quote.strip():
                for e in pat['content']['evidence']:
                    found = verbatim_quote(sec.typical_quote, e['quote'])
                    if found:
                        quote, source = found, {'question_no': e['question_no'], 'point_id': e['point_id']}
                        break
            sections.append(
                {
                    'pattern_lineage': pat['lineage_id'],
                    'pattern_name': pat['content']['name'],
                    'title': sec.title,
                    'key_steps': [x for x in sec.key_steps if x.strip()],
                    'typical_quote': quote,
                    'typical_source': source,
                    'quote_rejected': bool(sec.typical_quote.strip()) and not quote,
                    'error_analysis': sec.error_analysis,
                    'follow_up': [x for x in sec.follow_up if x.strip()],
                }
            )
        content = {
            'round': round_,
            'focus': out.focus,
            'sections': sections,
            'closing': out.closing,
            'dropped': dropped,
        }
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'review-plan-v1')
            plan = A.create(
                c,
                pid,
                'review_plan',
                content,
                origin='generated',
                refs=ctx.refs(),
                generation_id=gid,
                round_=round_,
                lineage_id=prior['lineage_id'] if prior else None,
            )
        return {'review_plan': plan, 'context_note': describe(ctx.manifest)}

    def review_plans(self, pid, round_=None):
        with self.store.read() as c:
            return A.current(c, pid, 'review_plan', round_=round_)

    # ----- 讲评后再练 and comparison -----

    def max_round(self, pid):
        with self.store.read() as c:
            return c.execute('SELECT COALESCE(MAX(round),1) FROM artifacts WHERE project_id=?', (pid,)).fetchone()[0]

    def practice(self, pid, pattern_ids=None, count=1, types=None, round_=None):
        """讲评后再练: questions targeting the confirmed problem patterns of ``round_`` (default: latest), as a new round."""
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            round_ = round_ or self.max_round(pid)
            pats = self._round_patterns(c, pid, round_)
            if pattern_ids:
                pats = [x for x in pats if x['id'] in pattern_ids or x['lineage_id'] in pattern_ids]
            if not pats:
                raise Conflict(f'第 {round_} 轮还没有已确认的问题模式，请先在学情分析中确认')
            ctx = Context('practice')
            ctx.add_other('project', pid)
            review = next((r for r in A.current(c, pid, 'review_plan', round_=round_) if r['confirmed_version']), None)
            review_focus = []
            if review:
                review = A.confirmed_in_lineage(c, review['lineage_id'])
                review_focus = ctx.add_artifact(review)['focus']
            plan = self._plan(c, pid)
            if plan:
                ctx.add_artifact(plan)
            objs = self._goals(plan)
            goal_list = [{'index': i, '目标': o['content']['text']} for i, o in enumerate(objs)]
            pat_list = [
                {
                    'index': i,
                    '名称': ctx.add_artifact(x, require_confirmed=x['status'] == 'confirmed')['name'],
                    '描述': x['content']['description'],
                    '学生原文示例': [e['quote'] for e in x['content']['evidence'][:3]],
                }
                for i, x in enumerate(pats)
            ]
            nos = sorted({e['question_no'] for x in pats for e in x['content']['evidence']})
            originals = []
            for q in A.current(c, pid, 'question', round_=round_):
                if q['confirmed_version'] and q['content']['no'] in nos:
                    conf = A.confirmed_in_lineage(c, q['lineage_id'])
                    originals.append({'题号': conf['content']['no'], '题目': ctx.add_artifact(conf)['stem'][:500]})
            new_round = self.max_round(pid) + 1
        allowed = [
            t for t in (types or p['pack']['types']) if t in p['pack']['types'] and t not in SUGGESTED_ONLY_TYPES
        ]
        system = PRACTICE_SYSTEM.format(
            subject=p['pack']['name'], types='、'.join(f'{t}（{QUESTION_TYPES[t]}）' for t in allowed)
        )
        user = _json(
            {
                '学科': p['pack']['name'],
                '教学目标': goal_list,
                '问题模式': pat_list,
                '原题': originals,
                '讲评重点': review_focus,
                '每个问题模式出题数': max(1, min(int(count), 3)),
            }
        )
        out, meta = llm.complete('generator', 'practice', system, user, QuestionSet)
        drafts, dropped = usable_drafts(out.questions, allowed, n_patterns=len(pats))

        def review(d):
            pat = pats[d.target_pattern_index]['content']
            return self._review_ctx(p, objs, d, pattern=f'{pat["name"]}：{pat["description"]}')

        checks = self._parallel(lambda d: self._self_check(p, d, review(d)), drafts)
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'practice-v1')
            made = self._save_questions(
                c, pid, list(zip(drafts, checks)), objs, gid, status='draft', round_=new_round, patterns=pats
            )
        return {'round': new_round, 'questions': made, 'dropped': dropped, 'context_note': describe(ctx.manifest)}

    def compare(self, pid, round_a=1, round_b=2):
        """两轮对比 of each confirmed problem pattern of ``round_a`` against the confirmed gradings of ``round_b``.

        Retested, improved, persisting and new are all counted on the same basis: rubric points of ``round_b``
        written for this pattern (``target_pattern``). Losing a point with the same error tag on another question
        is listed as ``same_tag_elsewhere`` for the teacher to look at, but does not count as the pattern
        persisting: a tag is broader than a pattern.
        """
        with self.store.read() as c:
            pats = self._round_patterns(c, pid, round_a)
            gradings_b = [
                A.confirmed_in_lineage(c, g['lineage_id'])
                for g in A.current(c, pid, 'grading', round_=round_b)
                if g['confirmed_version']
            ]
            gradings_b = [g for g in gradings_b if g]
        if not pats:
            raise Conflict(f'第 {round_a} 轮还没有已确认的问题模式')
        if not gradings_b:
            raise Conflict(f'第 {round_b} 轮还没有已确认的批改')
        rows = []
        for pat in pats:
            before = set(pat['content']['affected_learners'])
            tags = set(pat['content'].get('tags', []))
            retested, still, elsewhere = set(), set(), set()
            for g in gradings_b:
                learner = g['content']['learner_alias']
                for pt in g['content']['points']:
                    lost = pt['status'] in ('partial', 'missed')
                    if pt.get('target_pattern') == pat['lineage_id']:
                        retested.add(learner)
                        if lost:
                            still.add(learner)
                    elif lost and pt.get('error_tag') in tags:
                        elsewhere.add(learner)
            rows.append(
                {
                    'pattern_id': pat['id'],
                    'pattern_lineage': pat['lineage_id'],
                    'name': pat['content']['name'],
                    'before': sorted(before),
                    'retested': sorted(retested),
                    'improved': sorted((before & retested) - still),
                    'persisting': sorted(before & still),
                    'new': sorted(still - before),
                    'not_retested': sorted(before - retested),
                    'same_tag_elsewhere': sorted(elsewhere),
                    'enough_evidence': bool(retested),
                }
            )
        with self.store.tx() as c:
            content = {'round_a': round_a, 'round_b': round_b, 'patterns': rows}
            refs = [(x['id'], 'problem_pattern') for x in pats] + [(g['id'], 'grading') for g in gradings_b]
            a = A.create(
                c, pid, 'comparison', content, origin='generated', refs=refs, round_=round_b, created_by='server'
            )
        return a
