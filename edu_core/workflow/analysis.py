"""04 学情分析: statistics on the server, problem patterns grounded in student quotes."""

from __future__ import annotations

from edu_core import artifacts as A
from edu_core.context import Context, describe
from edu_core.storage import record_generation
from edu_core.text import verbatim_quote
from edu_core.workflow.common import (
    Conflict,
    _json,
)
from edu_core.workflow.prompts import (
    ANALYZE_SYSTEM,
    PITFALL_SYSTEM,
)
from edu_core.workflow.runs import operation
from edu_core.workflow.schemas import (
    AnalysisOutput,
    PitfallOutput,
)


class AnalysisMixin:
    """04 学情分析."""

    # ----- analysis -----

    def _coverage(self, c, scope):
        """Who is in this analysis (design doc 7.3): roster, learners who answered, valid gradings and what is pending.

        A grading is valid when its latest version is confirmed and its basis has not changed.
        """
        subs = scope.current(c, 'submission', round_=scope.round)
        by_sub = {g['content']['submission_lineage']: g for g in scope.current(c, 'grading', round_=scope.round)}
        valid, pending, ungraded, stale = [], 0, 0, 0
        for s in subs:
            g = by_sub.get(s['lineage_id'])
            if g is None:
                ungraded += 1
            elif g['stale']:
                stale += 1
            elif g['status'] != 'confirmed':
                pending += 1
            else:
                valid.append(g)
        roster = len(scope.roster(c))
        received = len({s['content']['learner_id'] for s in subs})
        collection = scope.coverage(c, subs)
        complete = bool(subs) and not (pending or ungraded or stale or collection['missing'])
        label = f'名单 {roster} 人，收到 {received} 人的作答，有效批改 {len(valid)}/{len(subs)} 份'
        label += f'；应收 {collection["expected"]} 份，缺少 {collection["missing"]} 份（未收到或未导入）'
        if pending or ungraded or stale:
            label += f'（未批改 {ungraded}，待复核或待确认 {pending}，依据已变化 {stale}）'
        return valid, {
            'roster': roster,
            'received': received,
            'submissions': len(subs),
            'valid': len(valid),
            'pending': pending,
            'ungraded': ungraded,
            'stale': stale,
            **collection,
            'complete': complete,
            'label': label,
        }

    @operation('analyze')
    def analyze(self, pid, round_=1, assignment_id=None, partial=False):
        """学情分析 of one assignment (or the project's unbound work).

        A complete analysis needs every submission to have a valid grading. With ``partial`` the teacher asks for
        an analysis of what is graded so far; it is labelled with its coverage and never counts as the class result.
        """
        llm = self._model()
        with self.store.read() as c:
            scope = self.scope(c, pid, round_, assignment_id)
            pid, round_, aid = scope.project_id, scope.round, scope.assignment_id
            p = self._project(c, pid)
            subs = scope.current(c, 'submission', round_=round_)
            confirmed, coverage = self._coverage(c, scope)
            questions = list(scope.questions(c).values())
            if subs and not coverage['complete'] and not partial:
                raise Conflict(f'{coverage["label"]}。处理完复核和确认后再生成完整分析，或选择“只分析已确认的部分”')
            if subs and not confirmed:
                raise Conflict('还没有已确认的批改，请先确认批改结果（可以批量确认）')
            ctx = Context('pitfalls' if not subs else 'analyze')
            ctx.add_other('project', pid)
            plan = self._plan(c, pid)
            if plan:
                ctx.add_artifact(plan)
            goals = self._goals(plan)
            objectives = [g['content']['text'] for g in goals]
            q_view = []
            for q in questions:
                ctx.add_artifact(q)
                rubric = scope.rubric(c, q['lineage_id'])
                if rubric:
                    ctx.add_artifact(rubric)
                q_view.append(
                    {
                        '题号': q['content']['no'],
                        '题目': q['content']['stem'][:500],
                        '评分要点': [
                            x['description'] for x in (rubric or {'content': {'points': []}})['content']['points']
                        ],
                    }
                )
            existing = []
            if subs:
                for pat in scope.current(c, 'problem_pattern'):
                    if pat['confirmed_version']:
                        conf = A.confirmed_in_lineage(c, pat['lineage_id'])
                        ctx.add_artifact(conf)
                        existing.append(
                            {
                                '编号': conf['lineage_id'],
                                '名称': conf['content']['name'],
                                '描述': conf['content']['description'],
                            }
                        )
                answers = {}
                for g in confirmed:
                    ctx.add_artifact(g)
                    s = A.get(c, g['content']['submission_id'])
                    ctx.add_artifact(s)
                    answers[g['id']] = s['content']['text']
        if not subs:
            if not q_view:
                raise Conflict('还没有已确认的题目，无法分析')
            system = PITFALL_SYSTEM.format(subject=p['pack']['name'])
            out, meta = llm.complete(
                'analyst', 'pitfalls', system, _json({'教学目标': objectives, '题目': q_view}), PitfallOutput
            )
            with self.store.tx() as c:
                gid = record_generation(c, pid, meta, ctx.manifest, 'pitfalls-v1')
                content = {
                    'mode': 'pitfalls',
                    'note': '未基于学生作答，只是可能的易错点',
                    'pitfalls': [x.model_dump() for x in out.pitfalls],
                    'round': round_,
                }
                a = A.create(
                    c,
                    pid,
                    'analysis',
                    content,
                    origin='generated',
                    refs=ctx.refs(),
                    generation_id=gid,
                    round_=round_,
                    assignment_id=aid,
                )
                self._guard_basis(c, pid, a['id'], ctx.manifest, scope)
            return {'analysis': a, 'patterns': [], 'context_note': describe(ctx.manifest)}

        evidence, by_tag, learners_all = {}, {}, set()
        stats = {}
        for g in confirmed:
            content = g['content']
            learners_all.add(content['learner_alias'])
            qs = stats.setdefault(content['question_no'], {'n': 0, 'score': 0.0, 'max': 0.0, 'points': {}})
            qs['n'] += 1
            qs['score'] += content['total']
            qs['max'] += content['max_total']
            for pt in content['points']:
                ps = qs['points'].setdefault(pt['id'], {'description': pt['description'], 'score': 0.0, 'max': 0.0})
                ps['score'] += pt['score'] or 0
                ps['max'] += pt['max']
                if pt['status'] in ('partial', 'missed'):
                    eid = f'E{len(evidence) + 1}'
                    quote = pt.get('evidence_quote') or answers[g['id']][:150]
                    evidence[eid] = {
                        'eid': eid,
                        'grading_id': g['id'],
                        'submission_id': content['submission_id'],
                        'learner': content['learner_alias'],
                        'question_no': content['question_no'],
                        'point_id': pt['id'],
                        'point': pt['description'],
                        'tag': pt.get('error_tag') or '其他',
                        'note': pt.get('error_note', ''),
                        'quote': quote,
                        'target_pattern': pt.get('target_pattern'),
                    }
                    by_tag.setdefault(evidence[eid]['tag'], []).append(eid)
        tag_summary = [
            {
                '错误标签': tag,
                '涉及学生数': len({evidence[e]['learner'] for e in eids}),
                '证据': [
                    {
                        'eid': e,
                        '题号': evidence[e]['question_no'],
                        '要点': evidence[e]['point'],
                        '学生原文': evidence[e]['quote'][:200],
                        '备注': evidence[e]['note'],
                    }
                    for e in eids[:10]
                ],
            }
            for tag, eids in sorted(by_tag.items(), key=lambda kv: -len(kv[1]))
        ]
        stats_view = {
            no: {
                '人数': s['n'],
                '得分率': round(s['score'] / s['max'], 3) if s['max'] else None,
                '要点得分率': {
                    pid_: round(x['score'] / x['max'], 3) if x['max'] else None for pid_, x in s['points'].items()
                },
            }
            for no, s in sorted(stats.items())
        }
        objective_stats = _objective_stats(goals, questions, confirmed)
        if not evidence:
            content = {
                'mode': 'evidence',
                'round': round_,
                'stats': stats_view,
                'objective_stats': objective_stats,
                'summary': '本轮所有已确认的批改都没有失分，没有可归纳的问题。',
                'patterns': [],
                'insufficient': [],
                'learners': len(learners_all),
                'coverage': coverage,
                'partial': not coverage['complete'],
            }
            with self.store.tx() as c:
                a = A.create(
                    c,
                    pid,
                    'analysis',
                    content,
                    origin='generated',
                    refs=ctx.refs(),
                    round_=round_,
                    created_by='server',
                    assignment_id=aid,
                )
                self._guard_basis(c, pid, a['id'], ctx.manifest, scope)
            return {'analysis': a, 'patterns': [], 'context_note': describe(ctx.manifest)}
        user = _json(
            {
                '学科': p['pack']['name'],
                '教学目标': objectives,
                '题目': q_view,
                '得分统计': stats_view,
                '目标达成': [
                    {k: o[k] for k in ('目标', '题号', '人数', '得分率', '全部得分人数')} for o in objective_stats
                ],
                '错误汇总': tag_summary,
                '已有问题模式': existing,
            }
        )
        out, meta = llm.complete(
            'analyst', 'analyze', ANALYZE_SYSTEM.format(subject=p['pack']['name']), user, AnalysisOutput
        )
        existing_ids = {e['编号'] for e in existing}
        patterns, dropped = [], []
        for pat in out.patterns:
            valid = []
            for ev in pat.evidence:
                item = evidence.get(ev.eid)
                if item is None:
                    continue
                quote = verbatim_quote(ev.quote, answers[item['grading_id']]) if ev.quote else None
                if quote:
                    valid.append({**item, 'quote': quote})
            if not valid:
                dropped.append(pat.name)
                continue
            tags = [t for t in pat.tags if t in by_tag]
            # Learners the pattern cites, plus learners who lost the same rubric point of the same question with
            # the same error tag (the model sees at most 10 quotes per tag). A tag alone is broader than a
            # pattern (“条件遗漏” spans questions), so it no longer counts on its own.
            same_point = {(v['question_no'], v['point_id'], v['tag']) for v in valid}
            affected = {v['learner'] for v in valid} | {
                e['learner'] for e in evidence.values() if (e['question_no'], e['point_id'], e['tag']) in same_point
            }
            patterns.append(
                {
                    'name': pat.name,
                    'description': pat.description,
                    'tags': tags,
                    'evidence': [
                        {
                            k: v[k]
                            for k in (
                                'eid',
                                'learner',
                                'question_no',
                                'point_id',
                                'quote',
                                'submission_id',
                                'grading_id',
                            )
                        }
                        for v in valid
                    ],
                    'affected_learners': sorted(affected),
                    'total_learners': len(learners_all),
                    'round': round_,
                    'lineage': pat.existing_pattern if pat.existing_pattern in existing_ids else None,
                }
            )
        # The summary is model text: it stands only when every pattern it summarizes passed the evidence check.
        # Otherwise it is replaced by a server-written statement of what the evidence shows (design doc 7.2).
        if patterns and not dropped:
            summary, summary_source = out.summary, 'model'
        elif patterns:
            summary = '有作答证据的问题：' + '；'.join(
                f'{x["name"]}（{len(x["affected_learners"])}/{x["total_learners"]} 人）' for x in patterns
            )
            summary_source = 'server'
        else:
            summary = (
                f'已确认的批改中有 {len({e["learner"] for e in evidence.values()})} 名学生失分，'
                '但归纳出的共性问题都没有通过作答证据核对，暂不形成学情结论；请看得分统计和补查问题。'
            )
            summary_source = 'server'
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'analyze-v2')
            content = {
                'mode': 'evidence',
                'round': round_,
                'stats': stats_view,
                'objective_stats': objective_stats,
                'summary': summary,
                'summary_source': summary_source,
                'unverified_summary': out.summary if summary_source == 'server' else '',
                'coverage': coverage,
                'partial': not coverage['complete'],
                'insufficient': [x.model_dump() for x in out.insufficient],
                'dropped_patterns': dropped,
                'learners': len(learners_all),
                'tag_counts': {t: len({evidence[e]['learner'] for e in eids}) for t, eids in by_tag.items()},
            }
            analysis = A.create(
                c,
                pid,
                'analysis',
                content,
                origin='generated',
                refs=ctx.refs(),
                generation_id=gid,
                round_=round_,
                assignment_id=aid,
            )
            based_on_old = self._guard_basis(c, pid, analysis['id'], ctx.manifest, scope)
            made = []
            for pat in patterns:
                lineage = pat.pop('lineage')
                refs = [(analysis['id'], 'analysis')] + [
                    (gid_, 'grading') for gid_ in sorted({e['grading_id'] for e in pat['evidence']})
                ]
                made.append(
                    A.create(
                        c,
                        pid,
                        'problem_pattern',
                        pat,
                        origin='generated',
                        refs=refs,
                        generation_id=gid,
                        lineage_id=lineage,
                        round_=round_,
                        assignment_id=aid,
                    )
                )
                if based_on_old:
                    self._guard_basis(c, pid, made[-1]['id'], ctx.manifest, scope)
        return {'analysis': analysis, 'patterns': made, 'context_note': describe(ctx.manifest)}

    def analyses(self, pid, assignment_id=None):
        with self.store.read() as c:
            return self.scope(c, pid, None, assignment_id).current(c, 'analysis')

    def patterns(self, pid, assignment_id=None):
        with self.store.read() as c:
            return self.scope(c, pid, None, assignment_id).current(c, 'problem_pattern')


def _objective_stats(goals, questions, gradings):
    """Per objective of the lesson plan: score rate over its questions, and how many learners earned full marks
    on all of them. Questions without an objective are not counted here (they stay in the per-question table)."""
    obj_of = {q['lineage_id']: set(q['content'].get('objective_ids') or []) for q in questions}
    rows = []
    for g in goals:
        oid = g['lineage_id']
        nos, per_learner = set(), {}
        for gr in gradings:
            ct = gr['content']
            if oid not in obj_of.get(ct['question_lineage'], ()):
                continue
            nos.add(ct['question_no'])
            s = per_learner.setdefault(ct['learner_alias'], [0.0, 0.0])
            s[0] += ct['total']
            s[1] += ct['max_total']
        score, top = sum(s[0] for s in per_learner.values()), sum(s[1] for s in per_learner.values())
        rows.append(
            {
                'id': oid,
                '目标': g['content']['text'],
                '题号': sorted(nos),
                '人数': len(per_learner),
                '得分率': round(score / top, 3) if top else None,
                '全部得分人数': sum(1 for s in per_learner.values() if s[1] and s[0] >= s[1] - 1e-9),
            }
        )
    return rows
