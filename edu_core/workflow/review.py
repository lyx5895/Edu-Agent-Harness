"""05 讲题答疑: 讲评稿, 讲评后再练 and the two-round comparison."""

from __future__ import annotations

from edu_core import artifacts as A
from edu_core.context import Context, describe
from edu_core.storage import record_generation
from edu_core.subjects import QUESTION_TYPES, SUGGESTED_ONLY_TYPES
from edu_core.text import sha, verbatim_quote
from edu_core.workflow.common import (
    Conflict,
    _json,
    usable_drafts,
)
from edu_core.workflow.prompts import (
    EXPLAIN_SYSTEM,
    PRACTICE_SYSTEM,
    REVIEW_SYSTEM,
)
from edu_core.workflow.runs import operation
from edu_core.workflow.schemas import (
    ExplainOutput,
    QuestionSet,
    ReviewOutput,
)


class ReviewMixin:
    """05 讲题答疑."""

    # ----- 05 讲题答疑 -----

    def _round_patterns(self, c, pid, round_, aid=''):
        """Confirmed problem patterns as they were in ``round_`` (a later round may add a newer version)."""
        best = {}
        for a in A.listing(
            c, pid, 'problem_pattern', statuses=('confirmed', 'superseded'), round_=round_, assignment=aid
        ):
            if a['status'] == 'superseded' and not a['change_kind']:
                continue  # a superseded draft was never adopted
            if a['lineage_id'] not in best or a['version'] > best[a['lineage_id']]['version']:
                best[a['lineage_id']] = a
        return sorted(best.values(), key=lambda a: a['created_at'])

    @operation('draft_review')
    def draft_review(self, pid, round_=None, pattern_ids=None, assignment_id=None):
        """讲评稿: focus, key steps, typical errors quoted from students, and follow-up questions per pattern."""
        llm = self._model()
        with self.store.read() as c:
            pid, round_, aid = self._scope(c, pid, round_ or self.max_round(pid) if pid else None, assignment_id)
            p = self._project(c, pid)
            pats = self._round_patterns(c, pid, round_, aid)
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
            analyses = [a for a in A.current(c, pid, 'analysis', round_=round_, assignment=aid)]
            summary = ''
            if analyses:
                ctx.add_artifact(analyses[-1], require_confirmed=False)
                summary = analyses[-1]['content'].get('summary', '')
                if analyses[-1]['content'].get('partial'):
                    summary += f'（部分分析：{analyses[-1]["content"]["coverage"]["label"]}）'
            nos = sorted({e['question_no'] for x in pats for e in x['content']['evidence']})
            originals = []
            for conf in self._scoped_questions(c, pid, round_, aid).values():
                if conf and conf['content']['no'] in nos:
                    rubric = self._scoped_rubric(c, conf['lineage_id'], aid)
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
            prior = next(iter(A.current(c, pid, 'review_plan', round_=round_, assignment=aid)), None)
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
        sections, dropped = _review_sections(out, pats)
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
                assignment_id=aid,
            )
            self._guard_basis(c, pid, plan['id'], ctx.manifest, aid)
        return {'review_plan': plan, 'context_note': describe(ctx.manifest)}

    @operation('explain')
    def explain(self, pid, question, ask='', student_answer='', teacher='teacher'):
        """直接准备讲解 (design doc 5.5): from a pasted question (and optionally one student's answer), without any
        学情分析. Without an answer it is a general explanation with possible pitfalls; with one, the comment is about
        that answer only, and every quote must be found in it verbatim. It is never presented as a class result.
        """
        question, ask, student_answer = (x.strip() for x in (question or '', ask or '', student_answer or ''))
        if not question:
            raise ValueError('请粘贴题目')
        if len(question) > 4000 or len(student_answer) > 4000 or len(ask) > 500:
            raise ValueError('题目或作答过长')
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('explain')
            ctx.add_other('project', pid)
            ctx.add_other(
                'pasted',
                sha(question + '\n' + student_answer),
                sha(question + student_answer),
                has_answer=bool(student_answer),
            )
            materials = self._material_context(c, ctx, pid, limit=3000)
            prior = next(iter(A.current(c, pid, 'explanation')), None)
        user = _json(
            {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '题目': question,
                '老师想讲的问题': ask or '讲清这道题的解法',
                **({'学生作答': student_answer} if student_answer else {}),
                **({'参考材料': materials} if materials else {}),
            }
        )
        out, meta = llm.complete(
            'generator', 'explain', EXPLAIN_SYSTEM.format(subject=p['pack']['name']), user, ExplainOutput
        )
        quotes, rejected = [], 0
        for q in out.quotes if student_answer else []:
            found = verbatim_quote(q, student_answer) if q.strip() else None
            if found:
                quotes.append(found)
            elif q.strip():
                rejected += 1
        content = {
            'mode': 'answer' if student_answer else 'general',
            'scope_note': '只针对这一份作答，不代表全班情况'
            if student_answer
            else '通用讲解；易错点是预判，不是本班学情',
            'question': question,
            'ask': ask,
            'student_answer': student_answer,
            'focus': [x for x in out.focus if x.strip()],
            'steps': [x for x in out.steps if x.strip()],
            'pitfalls': [x for x in out.pitfalls if x.strip()],
            'answer_comment': out.answer_comment.strip() if student_answer else '',
            'quotes': quotes,
            'quotes_rejected': rejected,
            'follow_up': [x for x in out.follow_up if x.strip()],
        }
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'explain-v1')
            a = A.create(
                c,
                pid,
                'explanation',
                content,
                origin='generated',
                generation_id=gid,
                lineage_id=prior['lineage_id'] if prior and prior['content'].get('question') == question else None,
            )
        return {'explanation': a, 'context_note': describe(ctx.manifest)}

    def explanations(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'explanation')

    def review_plans(self, pid, round_=None, assignment_id=None):
        with self.store.read() as c:
            if assignment_id:
                pid, round_, aid = self._scope(c, pid, round_, assignment_id)
                return A.current(c, pid, 'review_plan', round_=round_, assignment=aid)
            return A.current(c, pid, 'review_plan', round_=round_, assignment='')

    # ----- 讲评后再练 and comparison -----

    def max_round(self, pid):
        with self.store.read() as c:
            return c.execute('SELECT COALESCE(MAX(round),1) FROM artifacts WHERE project_id=?', (pid,)).fetchone()[0]

    @operation('practice')
    def practice(self, pid, pattern_ids=None, count=1, types=None, round_=None, assignment_id=None):
        """讲评后再练: questions targeting the confirmed problem patterns of ``round_`` (default: latest), as a new round.

        For a class assignment the patterns come from that assignment; the new questions are course content, and
        the teacher assigns them to the class as a follow-up assignment once confirmed (design doc 10.2).
        """
        llm = self._model()
        with self.store.read() as c:
            pid, round_, aid = self._scope(c, pid, round_ or self.max_round(pid) if pid else None, assignment_id)
            p = self._project(c, pid)
            pats = self._round_patterns(c, pid, round_, aid)
            if pattern_ids:
                pats = [x for x in pats if x['id'] in pattern_ids or x['lineage_id'] in pattern_ids]
            if not pats:
                raise Conflict(f'第 {round_} 轮还没有已确认的问题模式，请先在学情分析中确认')
            ctx = Context('practice')
            ctx.add_other('project', pid)
            review = next(
                (r for r in A.current(c, pid, 'review_plan', round_=round_, assignment=aid) if r['confirmed_version']),
                None,
            )
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
            for conf in self._scoped_questions(c, pid, round_, aid).values():
                if conf['content']['no'] in nos:
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
        return {
            'round': new_round,
            'questions': made,
            'dropped': dropped,
            'context_note': describe(ctx.manifest),
            'parent_assignment_id': aid,
        }

    def compare(self, pid, round_a=1, round_b=2, assignment_id=None):
        """两轮对比 of each confirmed problem pattern of ``round_a`` against the confirmed gradings of ``round_b``.

        Retested, improved, persisting and new are all counted on the same basis: rubric points of ``round_b``
        written for this pattern (``target_pattern``). Losing a point with the same error tag on another question
        is listed as ``same_tag_elsewhere`` for the teacher to look at, but does not count as the pattern
        persisting: a tag is broader than a pattern.
        """
        aid_a = aid_b = ''
        with self.store.read() as c:
            if assignment_id:
                # A class follow-up assignment is compared with the assignment it follows.
                pid, round_b, aid_b = self._scope(c, pid, None, assignment_id)
                parent = self._assignment_row(c, aid_b)['parent_id']
                if not parent:
                    raise Conflict('这次布置不是讲评后再练，没有可对比的上一轮')
                _, round_a, aid_a = self._scope(c, pid, None, parent)
            pats = self._round_patterns(c, pid, round_a, aid_a)
            gradings_b = [
                A.confirmed_in_lineage(c, g['lineage_id'])
                for g in A.current(c, pid, 'grading', round_=round_b, assignment=aid_b)
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
                c,
                pid,
                'comparison',
                content,
                origin='generated',
                refs=refs,
                round_=round_b,
                created_by='server',
                assignment_id=aid_b,
            )
        return a


def _review_sections(out, pats):
    """讲评稿 sections checked by the server: each must name a known pattern, and a typical error must be a verbatim
    piece of that pattern's student evidence."""
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
    return sections, dropped
