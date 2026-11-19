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
from edu_core.workflow.schemas import (
    AnalysisOutput,
    PitfallOutput,
)


class AnalysisMixin:
    """04 学情分析."""

    # ----- analysis -----

    def analyze(self, pid, round_=1):
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            subs = A.current(c, pid, 'submission', round_=round_)
            current_gradings = A.current(c, pid, 'grading', round_=round_)
            confirmed = [A.confirmed_in_lineage(c, g['lineage_id']) for g in current_gradings if g['confirmed_version']]
            confirmed = [g for g in confirmed if g]
            questions = [
                A.confirmed_in_lineage(c, q['lineage_id'])
                for q in A.current(c, pid, 'question', round_=round_)
                if q['confirmed_version']
            ]
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
                rubric = self._rubric_for(c, q['lineage_id'])
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
                for pat in A.current(c, pid, 'problem_pattern'):
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
                    c, pid, 'analysis', content, origin='generated', refs=ctx.refs(), generation_id=gid, round_=round_
                )
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
            }
            with self.store.tx() as c:
                a = A.create(
                    c, pid, 'analysis', content, origin='generated', refs=ctx.refs(), round_=round_, created_by='server'
                )
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
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'analyze-v2')
            content = {
                'mode': 'evidence',
                'round': round_,
                'stats': stats_view,
                'objective_stats': objective_stats,
                'summary': out.summary,
                'insufficient': [x.model_dump() for x in out.insufficient],
                'dropped_patterns': dropped,
                'learners': len(learners_all),
                'tag_counts': {t: len({evidence[e]['learner'] for e in eids}) for t, eids in by_tag.items()},
            }
            analysis = A.create(
                c, pid, 'analysis', content, origin='generated', refs=ctx.refs(), generation_id=gid, round_=round_
            )
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
                    )
                )
        return {'analysis': analysis, 'patterns': made, 'context_note': describe(ctx.manifest)}

    def analyses(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'analysis')

    def patterns(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'problem_pattern')


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
