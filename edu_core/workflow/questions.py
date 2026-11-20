"""02 出题改题: generation, quality review and independent solving, A/B choice, paper import, 换一道."""

from __future__ import annotations

import json
import re
import time

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core import grading, progress
from edu_core.context import Context, describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import event, new_id, record_generation
from edu_core.subjects import KEYED_TYPES, QUESTION_TYPES, SUGGESTED_ONLY_TYPES
from edu_core.text import dump, sha
from edu_core.workflow.common import (
    Conflict,
    _json,
    _question_content,
    _rubric_from,
    normalize_plan,
    usable_drafts,
)
from edu_core.workflow.prompts import (
    GENERATE_SYSTEM,
    IMPORT_SYSTEM,
    INFER_GOALS_SYSTEM,
    QUALITY_SYSTEM,
    SOLVE_SYSTEM,
    VARIANTS,
)
from edu_core.workflow.runs import artifact_project, operation
from edu_core.workflow.schemas import (
    ImportedPaper,
    InferredGoals,
    QualityOutput,
    QuestionDraft,
    QuestionSet,
    SolveOutput,
)

AB_RULE_VERSION = 'ab-v1'
AB_UI_VERSION = 'ui-2026-11'
REASON_TAGS = ('更贴合目标', '难度更合适', '更清楚', '有事实错误', '依据不足', '其他')


def _bigrams(text):
    text = re.sub(r'\s+', '', text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _mark_similar(items, threshold=0.6):
    """Parts are written without seeing each other: a question much like an earlier one gets a suggestion that says
    so (never a 需核对 flag). Similarity is the overlap of character pairs of the stems, options included."""
    seen = []
    for k, (d, checks) in enumerate(items):
        grams = _bigrams(d.stem + ''.join(d.options))
        for j, other in seen:
            if grams and other and len(grams & other) / min(len(grams), len(other)) >= threshold:
                checks.setdefault('suggestions', []).append(f'与本次生成的第 {j + 1} 道题内容相近，可以换个角度')
                break
        seen.append((k, grams))


class QuestionsMixin:
    """02 出题改题."""

    # ----- questions -----

    def create_question(self, pid, question, rubric, teacher='teacher', round_=1):
        with self.store.tx() as c:
            p = self._project(c, pid)
            question = self._validate_content('question', question, {'round': round_})
            if question['type'] not in p['pack']['types']:
                raise ValueError(f'{p["pack"]["name"]}不支持题型：{question["type"]}')
            question['no'] = self._next_no(c, pid, round_)
            q = A.create(c, pid, 'question', question, origin='teacher', created_by=teacher, round_=round_)
            rubric = self._validate_content('rubric', {**rubric, 'question_lineage': q['lineage_id']}, {})
            A.create(
                c,
                pid,
                'rubric',
                rubric,
                origin='teacher',
                refs=[(q['id'], 'question')],
                created_by=teacher,
                round_=round_,
            )
            return q

    @operation('generate_questions')
    def generate_questions(
        self, pid, *, types=None, count=2, difficulty='中等', notes='', objective_ids=None, ab=None, purpose='课堂练习'
    ):
        llm = self._model()
        with self.store.read() as c:
            plan = self._plan(c, pid)
            draft = None if plan else self._plan(c, pid, confirmed=False)
        if plan is None:
            if self._teacher_draft(draft):
                raise Conflict('备课方案还是草稿：先在“01 备课设计”确认，再按目标出题')
            # 出一份练习 without a lesson plan: objectives inferred from a paper or from the request
            plan = draft if draft and draft['content'].get('objectives') else self._infer_goals(pid, llm, notes)
        with self.store.read() as c:
            p = self._project(c, pid)
            objs = self._confirmed_objectives(c, pid, objective_ids, plan=plan)
            if not objs:
                raise Conflict('备课方案里没有选中的教学目标')
            ctx = Context('generate_questions')
            ctx.add_other('project', pid)
            self._add_plan(ctx, plan)
            cited = sorted({i for o in objs for i in o['content'].get('standard_ids', [])})
            hints, standards = [], []
            if cited:
                std = CUR.context(p['subject'], cited, self._curriculum_query(p))
                hints = [it['text'] for it in std['hints']]
                standards = [{'位置': it['label'], '原文': it['text']} for it in std['content']]
                ctx.add_other('curriculum', p['subject'], sha(_json(cited)), items=cited)
            carry = self._carryover_for_prompt(c, ctx, p)
            materials = self._material_context(c, ctx, pid, limit=6000)
        allowed = [t for t in (types or p['pack']['types']) if t in p['pack']['types']]
        if not allowed:
            raise ValueError('所选题型不属于该学科')
        carry_part = {carry.pop('label'): carry} if carry else {}

        def request(part):
            """The generation input for some of the objectives (``part``: indexes into ``objs``). One part per
            objective keeps each reply short (a whole set used to run past max_tokens) and lets parts run side by
            side; the suggested types rotate per objective so the merged set does not repeat one question type."""
            sub = [objs[i] for i in part]
            lead = part[0] % len(allowed)
            return {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '教学目标': [
                    {'index': k, '目标': o['content']['text'], '达成标准': o['content'].get('success_criterion', '')}
                    for k, o in enumerate(sub)
                ],
                '本课重点': plan['content'].get('key_points', []),
                '本课难点': plan['content'].get('difficulties', []),
                '本课时内容': p.get('lesson_scope', ''),
                '课标条目': standards,
                '课标教学提示': hints,
                # the other objectives are another part's: named so this part stays inside its own
                '其他目标': [objs[i]['content']['text'] for i in range(len(objs)) if i not in part],
                '课堂活动': [a for a in self._activities_for_prompt(plan, sub) if a['对应目标序号']],
                '备课备注': plan['content'].get('notes', ''),
                # one 巩固题 for the whole set, not one per part
                **(carry_part if part[0] == 0 else {}),
                '学生基础': p.get('learner_profile', ''),
                '材料摘录': materials,
                '允许题型': [QUESTION_TYPES[t] + f'（{t}）' for t in allowed],
                '建议优先题型': [QUESTION_TYPES[t] for t in (allowed[lead:] + allowed[:lead])[:2]],
                '每个目标出题数': max(1, min(int(count), 5)),
                '练习用途': purpose,
                '难度': difficulty,
                '老师说明': notes or p['notes'],
            }

        parts = [[i] for i in range(len(objs))]
        user = _json(request(list(range(len(objs)))))  # the whole request: the comparison's shared input
        trigger, sample_rate = 'requested', 1.0
        if ab is None:
            trigger, sample_rate = 'sampled', self.settings.ab_rate
            use_ab = self.rng.random() < self.settings.ab_rate and self._ab_under_cap()
        else:
            use_ab = bool(ab)
        variants = ['a', 'b'] if use_ab else ['a']
        if use_ab:
            self.rng.shuffle(variants)  # randomize which variant is shown as A; fixed for this comparison

        def run_part(variant, part):
            system = GENERATE_SYSTEM.format(
                subject=p['pack']['name'],
                types='、'.join(f'{t}（{QUESTION_TYPES[t]}）' for t in allowed),
                variant=VARIANTS[variant],
            )
            out, meta = llm.complete('generator', 'generate_questions', system, _json(request(part)), QuestionSet)
            drafts, dropped = usable_drafts(out.questions, allowed)
            for d in drafts:  # the part numbered its objectives from 0: back to the plan's numbering
                d.objective_index = part[d.objective_index] if 0 <= d.objective_index < len(part) else part[0]
            progress.grow('独立解题与审核', len(drafts))

            def check(d):
                result = self._self_check(p, d, self._review_ctx(p, objs, d, difficulty))
                progress.tick()
                return result

            # each part's questions are checked as soon as that part is written, not after the slowest part
            return meta, list(zip(drafts, self._parallel(check, drafts))), dropped

        def run(variant):
            """All parts of one version side by side. A failed part costs its own questions only (reported with the
            dropped ones); the version fails only when every part failed."""

            def attempt_part(part):
                try:
                    return run_part(variant, part), None
                except Exception as e:  # noqa: BLE001 - reported per part, raised below when nothing came back
                    return None, f'第 {part[0] + 1} 条目标的题没有生成成功（{type(e).__name__}: {e}）'[:200]

            done = self._parallel(attempt_part, parts)
            pieces = [r for r, _ in done if r is not None]
            errors = [err for _, err in done if err]
            if not pieces:
                raise ModelUnavailable('；'.join(errors))
            _mark_similar([pair for _, items, _ in pieces for pair in items])
            dropped = [x for _, _, dr in pieces for x in dr] + errors
            return variant, [(meta, items) for meta, items, _ in pieces], dropped

        def attempt(variant):
            try:
                return run(variant), None
            except Exception as e:
                if not use_ab:
                    raise
                return None, f'{type(e).__name__}: {e}'[:200]

        # The two A/B versions share the same input and do not depend on each other: generate them side by side.
        progress.step('出题' if len(variants) == 1 else '出两版候选题')
        outcomes = self._parallel(attempt, variants)
        runs = [r for r, _ in outcomes if r is not None]
        failures = [err for _, err in outcomes if err]

        def stems(version):
            return {d.stem.strip() for _, items in version[1] for d, _ in items}

        if not runs:
            raise ModelUnavailable('两份候选都没有生成成功：' + '；'.join(failures))
        status = 'shown'
        if use_ab and failures:
            status = 'one_side_failed'
        elif use_ab and stems(runs[0]) == stems(runs[1]):
            status = 'duplicate'  # no meaningful comparison: deliver A, record why nothing was shown
        shown = use_ab and status == 'shown'
        group = new_id('ab') if use_ab else None
        result = {'context_note': describe(ctx.manifest), 'candidate_group': group if shown else None, 'candidates': []}
        if use_ab and not shown:
            result['ab_note'] = '一份候选生成失败，先给出成功的一份' if failures else '两份候选几乎相同，没有展示比较'
        with self.store.tx() as c:
            display = {}
            for position, (variant, pieces, dropped) in zip('AB', runs if shown else runs[:1]):
                # one generation record per part, all carrying the version's group and position
                gids, made = [], []
                for meta, items in pieces:
                    gid = record_generation(
                        c,
                        pid,
                        meta,
                        ctx.manifest,
                        'generate-v4',
                        group=group if shown else None,
                        position=position if shown else None,
                        variant=variant,
                    )
                    gids.append(gid)
                    made += self._save_questions(
                        c,
                        pid,
                        items,
                        objs,
                        gid,
                        status='candidate' if shown else 'draft',
                        round_=1,
                        extra={'difficulty_requested': difficulty, 'purpose': purpose},
                    )
                display[position] = {'generations': gids, 'variant': variant, 'candidates': [q['id'] for q in made]}
                result['candidates'].append(
                    {
                        'position': position,
                        'variant': variant,
                        'generation_id': gids[0],
                        'generation_ids': gids,
                        'questions': made,
                        'dropped': dropped,
                    }
                )
            if use_ab:
                # The comparison record (AB spec 2, 7): shared input, what was shown where, and how it was triggered.
                c.execute(
                    'INSERT INTO comparisons(candidate_group,project_id,unit,input_sha,factor,display,trigger,sample_rate,'
                    'rule_version,ui_version,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        group,
                        pid,
                        'question_set',
                        sha(user),
                        'prompt_variant',
                        dump(display),
                        trigger,
                        sample_rate,
                        AB_RULE_VERSION,
                        AB_UI_VERSION,
                        status,
                        time.time(),
                    ),
                )
        return result

    @staticmethod
    def _add_plan(ctx, plan):
        """A confirmed plan goes into the context as such. Objectives inferred from a request or a paper are not
        confirmed: they are listed as what they are, never passed off as a confirmed plan."""
        if plan['status'] == 'confirmed':
            ctx.add_artifact(plan)
        else:
            ctx.add_other(
                'inferred_objectives', plan['id'], plan['content_sha'], lineage_id=plan['lineage_id'], version=plan['version']
            )

    @staticmethod
    def _teacher_draft(plan):
        """An unconfirmed plan drafted in 01 (not objectives inferred from a paper or a request): it must be confirmed
        before questions are written against it."""
        objs = (plan or {}).get('content', {}).get('objectives') or []
        return bool(objs) and not all(o.get('inferred') for o in objs)

    def _infer_goals(self, pid, llm, notes=''):
        """Practice straight from the request: infer the objectives the questions will test, saved as a draft plan
        whose objectives are marked 由出题要求反推. The teacher checks them with the questions; nothing is confirmed
        on their behalf."""
        self._confirm_standards(pid, llm)
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('infer_objectives')
            ctx.add_other('project', pid)
            materials = self._material_context(c, ctx, pid, limit=6000)
            if not (p.get('knowledge_module') or p.get('lesson_scope') or materials or notes.strip()):
                raise Conflict('请先写下这次练习的主题或范围，或添加参考材料')
            current = self._plan(c, pid, confirmed=False)
        progress.step('反推教学目标')
        with self.store.tx() as c:
            std = self._curriculum_for_plan(c, p)
        if std:
            ids = [it['id'] for it in std['content']]
            ctx.add_other('curriculum', p['subject'], sha(_json(ids)), items=ids)
        out, meta = llm.complete(
            'generator',
            'infer_objectives',
            INFER_GOALS_SYSTEM.format(subject=p['pack']['name']),
            _json(
                {
                    '学科': p['pack']['name'],
                    '年级': p['grade'],
                    '主题': p.get('knowledge_module', ''),
                    '课时': p.get('lesson', ''),
                    '本课时内容': p.get('lesson_scope', ''),
                    '学生基础': p.get('learner_profile', ''),
                    '老师说明': notes or p.get('notes', ''),
                    '课标条目': [{'位置': it['label'], '原文': it['text']} for it in (std or {}).get('content', [])],
                    '材料摘录': materials,
                }
            ),
            InferredGoals,
        )
        goals = [
            {'text': o.text, 'success_criterion': o.success_criterion, 'inferred': True}
            for o in out.objectives[:4]
            if o.text.strip()
        ]
        if not goals:
            raise ModelUnavailable('没有得到可用的教学目标，请补充主题或范围后重试')
        base = current['content'] if current else {'key_points': [], 'difficulties': [], 'activities': [], 'notes': ''}
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'infer-goals-v1')
            return A.create(
                c,
                pid,
                'lesson_plan',
                normalize_plan({**base, 'objectives': goals}),
                origin='generated',
                generation_id=gid,
                lineage_id=current['lineage_id'] if current else None,
            )

    def _ab_under_cap(self):
        """Sampled comparisons stay under the daily cap; teacher-requested ones are not counted against it."""
        if self.settings.ab_daily_cap <= 0:
            return False
        day_start = time.time() - (time.time() % 86400)
        with self.store.read() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM comparisons WHERE trigger='sampled' AND created_at>=?", (day_start,)
            ).fetchone()[0]
        return n < self.settings.ab_daily_cap

    def _review_ctx(self, p, objs, d, difficulty=None, pattern=None):
        """What the quality review compares a question with: its objective, the lesson scope and cited standards."""
        obj = objs[d.objective_index]['content'] if 0 <= d.objective_index < len(objs) else None
        data = CUR.load(p['subject'])
        standards = [
            data['by_id'][i]['text'] for i in (obj or {}).get('standard_ids', []) if data and i in data['by_id']
        ]
        return {
            '教学目标': obj['text'] if obj else '',
            '达成标准': obj.get('success_criterion', '') if obj else '',
            '针对的问题': pattern or '',
            '本课时内容': p.get('lesson_scope', ''),
            '课标条目': standards,
            '学生基础': p.get('learner_profile', ''),
            '要求难度': difficulty or '',
        }

    def _quality(self, p, d: QuestionDraft, review):
        """Reviewer judgement: does the question test its objective, stay in scope, match the asked difficulty?"""
        out, meta = self.llm.complete(
            'reviewer',
            'review_question',
            QUALITY_SYSTEM.format(subject=p['pack']['name']),
            _json(
                {
                    **{k: v for k, v in review.items() if v},
                    '题型': QUESTION_TYPES[d.type],
                    '题目': d.stem,
                    '选项': [f'{"ABCDEFGH"[i]}. {o}' for i, o in enumerate(d.options[:8])],
                    '参考答案': d.reference,
                    '评分要点': [x.description for x in d.points],
                }
            ),
            QualityOutput,
        )
        flags = []
        if review.get('教学目标') and not out.tests_objective:
            flags.append(f'与目标不符：{out.objective_note or "没有考到这条目标"}')
        if not out.in_scope:
            flags.append(f'超出范围：{out.scope_note or "用到了本课时或课标以外的内容"}')
        flags += [f'题目问题：{x}' for x in out.issues if x.strip()][:4]
        # A difficulty off the asked one and improvement ideas are worth seeing, but they are not reasons to call a
        # question 需核对: every question used to carry ~4 flags, and the real errors drowned among them.
        asked = review.get('要求难度')
        off = asked in ('基础', '中等', '较难') and out.difficulty != asked
        return {
            'difficulty': out.difficulty,
            'difficulty_asked': asked if off else '',
            'flags': flags,
            'suggestions': [x for x in out.suggestions if x.strip()][:2],
        }, meta

    def _self_check(self, p, d: QuestionDraft, review=None):
        """Another model solves the question without the key; the grader checks that answer (design doc 4 ②).
        With ``review``, a reviewer also checks objective, scope and difficulty."""
        subject = p['subject']
        checks = {'notes': [], '_metas': []}
        solvable = d.type not in SUGGESTED_ONLY_TYPES and self.llm is not None

        def review_part():
            if review is None or self.llm is None:
                return None
            try:
                return self._quality(p, d, review)
            except ModelUnavailable as e:
                return e

        def solve_part():
            if not solvable:
                return None
            try:
                return self._solve_and_grade(p, d, subject)
            except ModelUnavailable as e:
                return e

        # The review and the independent solution do not depend on each other: run them side by side, then merge
        # in the original order (review, numeric check, solution) so the notes read the same as before.
        reviewed, solved = self._parallel(lambda part: part(), [review_part, solve_part])
        if isinstance(reviewed, ModelUnavailable):
            checks['notes'].append(f'题目审核失败：{reviewed}')
        elif reviewed is not None:
            quality, qmeta = reviewed
            checks.update(quality)
            checks['_metas'].append(qmeta)
        for x in d.points:
            if x.check_kind == 'numeric' and x.check_expected.strip():
                from edu_core.tools import check_numeric

                ok, _ = check_numeric(d.reference, x.check_expected)
                if ok is False:
                    checks['notes'].append(f'参考答案的最终数值与核对值 {x.check_expected} 不一致')
                    checks['reference_mismatch'] = True
        if not solvable:
            checks['solver'] = 'skipped'
            return checks
        if isinstance(solved, ModelUnavailable):
            checks.update(solver='error', notes=[*checks['notes'], f'自检失败：{solved}'])
        else:
            res, solution, metas = solved
            checks['_metas'] = [*checks['_metas'], *metas]
            agree = res['undetermined'] == 0 and res['total'] >= res['max_total'] - 1e-9
            checks.update(
                solver='agree' if agree else 'disagree',
                solver_answer=solution.answer[:2000],
                solver_final=solution.final_answer,
                solver_score=f'{res["total"]}/{res["max_total"]}',
                solver_model=metas[0].get('model_returned'),
            )
            if not agree:
                checks['notes'].append('另一个模型独立解题的结果与参考答案不一致，请核对答案')
        checks['answer_suspect'] = checks.get('solver') == 'disagree' or bool(checks.get('reference_mismatch'))
        return checks

    def _solve_and_grade(self, p, d: QuestionDraft, subject):
        """Another model solves the question without the key, then the grader marks that solution against the
        rubric. Returns (grading result, the solution, model metas: the solve call first)."""
        solve_ctx = Context('self_check_solve')
        solve_ctx.add_other('question_stem', sha(d.stem))
        solution, meta = self.llm.complete(
            'reviewer',
            'self_check_solve',
            SOLVE_SYSTEM.format(subject=p['pack']['name']),
            _json(
                {
                    '题型': QUESTION_TYPES[d.type],
                    '题目': d.stem,
                    '选项': [f'{"ABCDEFGH"[i]}. {o}' for i, o in enumerate(d.options[:8])],
                }
            ),
            SolveOutput,
        )
        question = _question_content(d, 0, 1, [])
        rubric = _rubric_from(d.points, 'self-check')
        # Keyed and fill-blank questions compare the final answer; other types grade the full solution.
        answer = solution.final_answer if d.type in KEYED_TYPES or d.type == 'fill_blank' else solution.answer
        res, grade_metas = grading.grade(
            self.llm, subject=subject, question=question, rubric=rubric, answer=answer, review=False
        )
        return res, solution, [meta, *grade_metas]

    def _save_questions(self, c, pid, items, objs, gid, *, status, round_, patterns=(), origin='generated', extra=None):
        made = []
        no = self._next_no(c, pid, round_)
        g = c.execute('SELECT context_manifest FROM generations WHERE id=?', (gid,)).fetchone() if gid else None
        basis = json.loads(g[0]) if g else []
        for d, checks in items:
            checks = dict(checks)
            metas = checks.pop('_metas', [])
            manifest = [{'kind': 'question_stem', 'id': sha(d.stem), 'sha': sha(d.stem)}]
            checks['generation_ids'] = [record_generation(c, pid, m, manifest, 'self-check-v2') for m in metas]
            obj = objs[d.objective_index] if 0 <= d.objective_index < len(objs) else None
            pattern = patterns[d.target_pattern_index] if 0 <= d.target_pattern_index < len(patterns) else None
            content = _question_content(
                d, no, round_, [obj['lineage_id']] if obj else [], [pattern['lineage_id']] if pattern else []
            )
            content['checks'] = checks
            content.update(extra or {})
            refs = ([(obj['id'], 'lesson_plan')] if obj else []) + (
                [(pattern['id'], 'problem_pattern')] if pattern else []
            )
            q = A.create(
                c, pid, 'question', content, origin=origin, refs=refs, generation_id=gid, status=status, round_=round_
            )
            rubric = _rubric_from(d.points, q['lineage_id'], pattern['lineage_id'] if pattern else None)
            r = A.create(
                c,
                pid,
                'rubric',
                rubric,
                origin=origin,
                refs=[(q['id'], 'question'), *refs],
                generation_id=gid,
                status=status,
                round_=round_,
            )
            q['rubric'] = r
            if status != 'candidate':
                self._guard_basis(c, pid, q['id'], basis)
            made.append(q)
            if status != 'candidate':
                no += 1
        return made

    def choose(
        self,
        pid,
        group,
        verdict,
        reason='',
        teacher='teacher',
        *,
        work_choice=None,
        preference='none',
        reason_tags=None,
    ):
        """A/B feedback (AB spec 3, 4), kept as separate facts instead of one win/loss label.

        ``verdict``: ``A`` / ``B`` (adopt that one), ``both_ok`` (both acceptable), ``neither``, ``skip`` (no label);
        ``same`` is the old “差不多” and means both acceptable with A taken to work on. ``work_choice`` is the one
        taken to work on (for ``both_ok``). ``preference`` is ``better`` only when the teacher said the adopted one fits
        this task better; ``work_only`` when it was just picked to edit; ``none`` otherwise. Only ``better`` becomes a
        preference pair in a training export. Unchosen candidates stay as research records.
        """
        if verdict == 'same':
            verdict, work_choice = 'both_ok', work_choice or 'A'
        if verdict not in ('A', 'B', 'both_ok', 'neither', 'skip'):
            raise ValueError('选择应为 A、B、both_ok（两份都可用）、neither（都不合适）或 skip（暂不比较）')
        if preference not in ('better', 'work_only', 'none'):
            raise ValueError('偏好应为 better、work_only 或 none')
        if preference == 'better' and verdict not in ('A', 'B'):
            raise ValueError('只有采用 A 或 B 时才能表示“更适合本次任务”')
        chosen = verdict if verdict in ('A', 'B') else (work_choice if verdict == 'both_ok' else None)
        if chosen not in (None, 'A', 'B'):
            raise ValueError('继续修改的一份应为 A 或 B')
        acceptable = {'A': 'A', 'B': 'B', 'both_ok': 'both', 'neither': 'none', 'skip': ''}[verdict]
        tags = [t for t in (reason_tags or []) if t in REASON_TAGS]
        with self.store.tx() as c:
            gens = {}  # position -> its generations, one per part (objective), in the order they were written
            for r in c.execute(
                'SELECT id, position FROM generations WHERE project_id=? AND candidate_group=? ORDER BY created_at, rowid',
                (pid, group),
            ):
                gens.setdefault(r['position'], []).append(r['id'])
            if not gens:
                raise KeyError('候选组不存在')
            if c.execute('SELECT 1 FROM choices WHERE candidate_group=? AND revoked_at IS NULL', (group,)).fetchone():
                raise Conflict('这一组已经选择过了；要改选请先撤回')
            cid = new_id('cho')
            c.execute(
                'INSERT INTO choices(id,project_id,candidate_group,verdict,chosen_position,reason,teacher,created_at,'
                'work_choice,preference,acceptable,reason_tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    cid,
                    pid,
                    group,
                    verdict,
                    chosen,
                    reason,
                    teacher,
                    time.time(),
                    chosen or '',
                    preference,
                    acceptable,
                    dump(tags),
                ),
            )
            promoted = [x for gid in gens[chosen] for x in self._promote(c, pid, gid)] if chosen else []
            event(
                c,
                pid,
                'ab_choice',
                {'group': group, 'choice': cid, 'verdict': verdict, 'work_choice': chosen, 'preference': preference},
            )
            return {'choice_id': cid, 'verdict': verdict, 'promoted': promoted}

    def _promote(self, c, pid, generation_id):
        promoted = []
        rows = c.execute(
            "SELECT id, type FROM artifacts WHERE generation_id=? AND status='candidate' ORDER BY created_at",
            (generation_id,),
        ).fetchall()
        no = self._next_no(c, pid, 1)
        for r in rows:
            if r['type'] == 'question':
                a = A.get(c, r['id'])
                content = {**a['content'], 'no': no}
                no += 1
                c.execute(
                    'UPDATE artifacts SET status=?, content=?, content_sha=? WHERE id=?',
                    ('draft', dump(content), sha(content), r['id']),
                )
            else:
                c.execute("UPDATE artifacts SET status='draft' WHERE id=?", (r['id'],))
            promoted.append(r['id'])
        return promoted

    def revoke_choice(self, choice_id, teacher='teacher'):
        """Withdraw a choice (AB spec 4): a new event, the old row is kept and marked; drafts already taken stay."""
        with self.store.tx() as c:
            r = c.execute('SELECT * FROM choices WHERE id=?', (choice_id,)).fetchone()
            if r is None:
                raise KeyError('选择记录不存在')
            if r['revoked_at']:
                raise Conflict('这条选择已经撤回')
            c.execute('UPDATE choices SET revoked_at=? WHERE id=?', (time.time(), choice_id))
            event(c, r['project_id'], 'ab_choice_revoked', {'choice': choice_id, 'teacher': teacher})
        return {'ok': True}

    def merge_candidates(self, pid, group, question, rubric, sources, teacher='teacher'):
        """结合两份修改 (AB spec 3, 4): a new draft question whose sources are candidates from both sides. The final
        text has two sources and is never recorded as either original candidate."""
        with self.store.tx() as c:
            positions = {}
            for sid in sources:
                a = A.get(c, sid)
                g = c.execute(
                    'SELECT position FROM generations WHERE id=? AND candidate_group=?', (a['generation_id'], group)
                ).fetchone()
                if a['project_id'] != pid or g is None:
                    raise Conflict('来源不是这一组的候选')
                positions[sid] = g['position']
            if set(positions.values()) != {'A', 'B'}:
                raise ValueError('结合修改需要分别来自 A 和 B 的候选')
            q = self._validate_content('question', {**question, 'no': self._next_no(c, pid, 1)}, {})
            made = A.create(
                c,
                pid,
                'question',
                {**q, 'merged_from': sources},
                origin='merged',
                created_by=teacher,
                refs=[(s, 'merged_from') for s in sources],
            )
            r = self._validate_content('rubric', {**rubric, 'question_lineage': made['lineage_id']}, {})
            made['rubric'] = A.create(
                c, pid, 'rubric', r, origin='merged', created_by=teacher, refs=[(made['id'], 'question')]
            )
            if not c.execute(
                'SELECT 1 FROM choices WHERE candidate_group=? AND revoked_at IS NULL', (group,)
            ).fetchone():
                c.execute(
                    'INSERT INTO choices(id,project_id,candidate_group,verdict,chosen_position,reason,teacher,created_at,'
                    'work_choice,preference,acceptable,reason_tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (new_id('cho'), pid, group, 'merged', None, '', teacher, time.time(), '', 'none', '', '[]'),
                )
            event(c, pid, 'ab_merged', {'group': group, 'sources': sources, 'question': made['id']})
            return made

    @operation('import_paper')
    def import_paper(self, pid, text, filename='导入试卷'):
        llm = self._model()
        material = self.add_material(pid, text, filename=filename, kind='paper')
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('import_paper')
            ctx.add_other('project', pid)
            ctx.add_other(
                'material', material['id'], sha(text.strip()), family_id=material['family_id'], filename=filename
            )
        with self.store.read() as c:
            current_plan = self._plan(c, pid, confirmed=False)
        existing = current_plan['content']['objectives'] if current_plan else []
        system = IMPORT_SYSTEM.format(subject=p['pack']['name'])
        out, meta = llm.complete(
            'generator',
            'import_paper',
            system,
            _json({'学科': p['pack']['name'], '已有教学目标': [o['text'] for o in existing], '试卷': text[:30000]}),
            ImportedPaper,
        )
        allowed = set(QUESTION_TYPES)
        drafts, dropped = usable_drafts(out.questions, allowed)
        checks = [{'notes': ['由导入试卷整理，评分要点和教学目标为反推，请核对'], 'imported': True} for _ in drafts]
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'import-v1')
            known = {o['text'].strip() for o in existing}
            added = [
                {'text': o.text, 'success_criterion': o.success_criterion, 'inferred': True}
                for o in out.objectives
                if o.text.strip() not in known
            ]
            base = (
                current_plan['content']
                if current_plan
                else {'key_points': [], 'difficulties': [], 'activities': [], 'notes': ''}
            )
            content = normalize_plan({**base, 'objectives': [*existing, *added]})
            plan = A.create(
                c,
                pid,
                'lesson_plan',
                content,
                origin='imported',
                generation_id=gid,
                lineage_id=current_plan['lineage_id'] if current_plan else None,
            )
            objs = self._goals(plan)  # existing objectives first, so the model's indexes line up
            made = self._save_questions(
                c, pid, list(zip(drafts, checks)), objs, gid, status='draft', round_=1, origin='imported'
            )
        return {
            'lesson_plan': plan,
            'objectives': [o['content'] for o in objs],
            'questions': made,
            'dropped': dropped,
            'context_note': describe(ctx.manifest),
        }

    @staticmethod
    def _activities_for_prompt(plan, objs):
        """课堂活动 of the plan, with the objective numbers as the generator sees them (index into its goal list)."""
        index = {o['lineage_id']: i for i, o in enumerate(objs)}
        return [
            {
                '活动': a['name'],
                '用时': f'{a.get("minutes", 0)} 分钟',
                '做法': a.get('description', ''),
                '对应目标序号': [index[x] for x in a.get('objective_ids', []) if x in index],
            }
            for a in plan['content'].get('activities', [])
        ]

    def _carryover_for_prompt(self, c, ctx, p):
        """上一课时学情 or 本班学情: problem patterns with their re-practice result."""
        carry = self._prior_status(c, ctx, p)
        if not carry:
            return None
        return {
            'label': carry['label'],
            '学情概述': carry['summary'],
            '问题模式': [
                {
                    '名称': x['name'],
                    '描述': x['description'],
                    '涉及人数': x['affected'],
                    '再练情况': x['after_practice'],
                }
                for x in carry['patterns']
            ],
            **({'说明': carry['note']} if carry.get('note') else {}),
        }

    def coverage(self, pid, round_=1):
        """目标覆盖: how many questions each confirmed objective has in a round, and how many need a look."""
        with self.store.read() as c:
            plan = self._working_plan(c, pid)
            goals = self._goals(plan)
        qs = self.questions(pid, round_=round_)
        rows = []
        for g in goals:
            mine = [
                q for q in qs if g['lineage_id'] in q['content'].get('objective_ids', []) and q['status'] != 'candidate'
            ]
            flagged = [
                q['content']['no']
                for q in mine
                if q['content'].get('checks', {}).get('answer_suspect') or q['content'].get('checks', {}).get('flags')
            ]
            rows.append(
                {
                    'id': g['lineage_id'],
                    'text': g['content']['text'],
                    'success_criterion': g['content'].get('success_criterion', ''),
                    'total': len(mine),
                    'confirmed': sum(1 for q in mine if q['confirmed_version']),
                    'flagged': flagged,
                }
            )
        loose = [q['content']['no'] for q in qs if not q['content'].get('objective_ids') and q['status'] != 'candidate']
        # Objectives not yet confirmed (inferred from the request or a paper, or a plan draft) are shown as such.
        unconfirmed = bool(plan) and not plan.get('confirmed_version') and plan['status'] != 'confirmed'
        return {
            'round': round_,
            'objectives': rows,
            'unassigned': loose,
            'unconfirmed': unconfirmed,
            'plan_id': plan['id'] if plan else None,
        }

    def adopt_solver_answer(self, qid, teacher='teacher'):
        """答案存疑 → take the independent solver's answer as a new draft version for the teacher to confirm."""
        with self.store.tx() as c:
            q = A.get(c, qid)
            if q['type'] != 'question':
                raise ValueError('只能对题目操作')
            ck = dict(q['content'].get('checks') or {})
            if not ck.get('solver_answer'):
                raise Conflict('这道题没有独立解题的结果')
            content = dict(q['content'])
            if content['type'] in KEYED_TYPES or content['type'] == 'fill_blank':
                content['answer_key'] = ck.get('solver_final', '').strip()
            content['reference'] = ck['solver_answer']
            ck.update(answer_suspect=False, solver='adopted', notes=['已采用独立解题的答案，请核对后确认'])
            content['checks'] = ck
            return A.edit(c, qid, content, teacher=teacher)

    @operation('regenerate_question', artifact_project)
    def regenerate_question(self, qid, instruction='', difficulty=None):
        """换一道: a new question for the same objective and type, saved as the next version of this question."""
        llm = self._model()
        with self.store.read() as c:
            q = A.get(c, qid)
            if q['type'] != 'question':
                raise ValueError('只能对题目操作')
            pid = q['project_id']
            p = self._project(c, pid)
            plan = self._working_plan(c, pid)
            objs = self._confirmed_objectives(c, pid, q['content'].get('objective_ids') or None, plan=plan) if plan else []
            objs = objs[:1]
            pattern = None
            for lineage in q['content'].get('target_patterns', []):
                pat = c.execute(
                    'SELECT content FROM artifacts WHERE lineage_id=? ORDER BY version DESC LIMIT 1', (lineage,)
                ).fetchone()
                if pat:
                    pc = json.loads(pat['content'])
                    pattern = f'{pc["name"]}：{pc["description"]}'
            old_rubric = self._rubric_for(c, q['lineage_id'], include_drafts=True)
            ctx = Context('generate_questions')
            ctx.add_other('project', pid)
            if plan:
                self._add_plan(ctx, plan)
            cited = sorted({i for o in objs for i in o['content'].get('standard_ids', [])})
            standards = []
            if cited:
                data = CUR.load(p['subject'])
                standards = [
                    {'位置': data['by_id'][i]['label'], '原文': data['by_id'][i]['text']}
                    for i in cited
                    if i in data['by_id']
                ]
                ctx.add_other('curriculum', p['subject'], sha(_json(cited)), items=cited)
        difficulty = difficulty or q['content'].get('difficulty_requested') or '中等'
        qtype = q['content']['type']
        system = GENERATE_SYSTEM.format(
            subject=p['pack']['name'], types=f'{qtype}（{QUESTION_TYPES[qtype]}）', variant=VARIANTS['a']
        )
        user = _json(
            {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '教学目标': [
                    {'index': 0, '目标': o['content']['text'], '达成标准': o['content'].get('success_criterion', '')}
                    for o in objs
                ],
                '针对的问题': pattern or '',
                '本课时内容': p.get('lesson_scope', ''),
                '课标条目': standards,
                '学生基础': p.get('learner_profile', ''),
                '每个目标出题数': 1,
                '难度': difficulty,
                '原题（请换一道，不要重复）': q['content']['stem'],
                '老师的修改要求': instruction or '换一道考查同一目标的新题',
            }
        )
        out, meta = llm.complete('generator', 'regenerate_question', system, user, QuestionSet)
        drafts, dropped = usable_drafts(out.questions, [qtype])
        if not drafts:
            raise ModelUnavailable('模型没有给出可用的题目，请重试' + (f'（{dropped[0]}）' if dropped else ''))
        d = drafts[0].model_copy(update={'objective_index': 0 if objs else -1})
        checks = self._self_check(p, d, self._review_ctx(p, objs, d, difficulty, pattern))
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'regenerate-v1')
            metas = checks.pop('_metas', [])
            manifest = [{'kind': 'question_stem', 'id': sha(d.stem), 'sha': sha(d.stem)}]
            checks['generation_ids'] = [record_generation(c, pid, m, manifest, 'self-check-v2') for m in metas]
            content = _question_content(
                d,
                q['content']['no'],
                q['round'],
                q['content'].get('objective_ids', []),
                q['content'].get('target_patterns', []),
            )
            content.update(
                checks=checks,
                difficulty_requested=difficulty,
                regenerated_from=qid,
                instruction=instruction or '',
                purpose=q['content'].get('purpose', ''),
            )
            refs = [(r['ref_id'], r['role']) for r in A.get(c, qid)['refs']]
            new = A.create(
                c,
                pid,
                'question',
                content,
                origin='generated',
                refs=refs,
                generation_id=gid,
                lineage_id=q['lineage_id'],
                round_=q['round'],
            )
            rubric = _rubric_from(d.points, q['lineage_id'], (q['content'].get('target_patterns') or [None])[0])
            new['rubric'] = A.create(
                c,
                pid,
                'rubric',
                rubric,
                origin='generated',
                refs=[(new['id'], 'question'), *refs],
                generation_id=gid,
                lineage_id=old_rubric['lineage_id'] if old_rubric else None,
                round_=q['round'],
            )
            self._guard_basis(c, pid, new['id'], ctx.manifest)
        return {'question': new, 'context_note': describe(ctx.manifest)}

    def questions(self, pid, round_=None):
        with self.store.read() as c:
            qs = A.current(c, pid, 'question', round_=round_)
            for q in qs:
                q['rubric'] = self._rubric_for(c, q['lineage_id'], include_drafts=True)
                if q['rubric']:
                    q['rubric']['stale'] = A.is_stale(c, q['rubric']['id'])
                    q['rubric']['confirmed_version'] = (A.confirmed_in_lineage(c, q['rubric']['lineage_id']) or {}).get(
                        'version'
                    )
                if q['generation_id']:
                    g = c.execute(
                        'SELECT candidate_group, position, variant FROM generations WHERE id=?', (q['generation_id'],)
                    ).fetchone()
                    q['candidate'] = dict(g) if g and g['candidate_group'] else None
            # by question number, so a question keeps its place after it is edited or regenerated
            return sorted(qs, key=lambda q: (q['round'], q['status'] == 'candidate', q['content'].get('no', 0)))
