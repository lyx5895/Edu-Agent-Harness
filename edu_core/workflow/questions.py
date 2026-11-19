"""02 出题改题: generation, quality review and independent solving, A/B choice, paper import, 换一道."""

from __future__ import annotations

import json
import time

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core import grading
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
    QUALITY_SYSTEM,
    SOLVE_SYSTEM,
    VARIANTS,
)
from edu_core.workflow.schemas import (
    ImportedPaper,
    QualityOutput,
    QuestionDraft,
    QuestionSet,
    SolveOutput,
)


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

    def generate_questions(
        self, pid, *, types=None, count=2, difficulty='中等', notes='', objective_ids=None, ab=None, purpose='课堂练习'
    ):
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            plan = self._plan(c, pid)
            if plan is None:
                raise Conflict('请先确认备课方案，再按目标出题')
            objs = self._confirmed_objectives(c, pid, objective_ids)
            if not objs:
                raise Conflict('备课方案里没有选中的教学目标')
            ctx = Context('generate_questions')
            ctx.add_other('project', pid)
            ctx.add_artifact(plan)
            cited = sorted({i for o in objs for i in o['content'].get('standard_ids', [])})
            hints, standards = [], []
            if cited:
                std = CUR.context(p['subject'], cited, self._curriculum_query(p))
                hints = [it['text'] for it in std['hints']]
                standards = [{'位置': it['label'], '原文': it['text']} for it in std['content']]
                ctx.add_other('curriculum', p['subject'], sha(_json(cited)), items=cited)
            carry = self._carryover_for_prompt(c, ctx, p)
            goal_list = [
                {'index': i, '目标': o['content']['text'], '达成标准': o['content'].get('success_criterion', '')}
                for i, o in enumerate(objs)
            ]
            materials = self._material_context(c, ctx, pid, limit=6000)
        allowed = [t for t in (types or p['pack']['types']) if t in p['pack']['types']]
        if not allowed:
            raise ValueError('所选题型不属于该学科')
        user = _json(
            {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '教学目标': goal_list,
                '本课重点': plan['content'].get('key_points', []),
                '本课难点': plan['content'].get('difficulties', []),
                '本课时内容': p.get('lesson_scope', ''),
                '课标条目': standards,
                '课标教学提示': hints,
                '课堂活动': self._activities_for_prompt(plan, objs),
                '备课备注': plan['content'].get('notes', ''),
                **({carry.pop('label'): carry} if carry else {}),
                '学生基础': p.get('learner_profile', ''),
                '材料摘录': materials,
                '允许题型': [QUESTION_TYPES[t] + f'（{t}）' for t in allowed],
                '每个目标出题数': max(1, min(int(count), 5)),
                '练习用途': purpose,
                '难度': difficulty,
                '老师说明': notes or p['notes'],
            }
        )
        use_ab = ab if ab is not None else self.rng.random() < self.settings.ab_rate
        variants = ['a', 'b'] if use_ab else ['a']
        if use_ab:
            self.rng.shuffle(variants)  # randomize which variant is shown as A

        def run(variant):
            system = GENERATE_SYSTEM.format(
                subject=p['pack']['name'],
                types='、'.join(f'{t}（{QUESTION_TYPES[t]}）' for t in allowed),
                variant=VARIANTS[variant],
            )
            out, meta = llm.complete('generator', 'generate_questions', system, user, QuestionSet)
            drafts, dropped = usable_drafts(out.questions, allowed)
            checks = self._parallel(lambda d: self._self_check(p, d, self._review_ctx(p, objs, d, difficulty)), drafts)
            return variant, meta, list(zip(drafts, checks)), dropped

        runs = [run(v) for v in variants]
        group = new_id('ab') if use_ab else None
        result = {'context_note': describe(ctx.manifest), 'candidate_group': group, 'candidates': []}
        with self.store.tx() as c:
            for position, (variant, meta, items, dropped) in zip('AB', runs):
                gid = record_generation(
                    c,
                    pid,
                    meta,
                    ctx.manifest,
                    'generate-v3',
                    group=group,
                    position=position if group else None,
                    variant=variant,
                )
                made = self._save_questions(
                    c,
                    pid,
                    items,
                    objs,
                    gid,
                    status='candidate' if group else 'draft',
                    round_=1,
                    extra={'difficulty_requested': difficulty, 'purpose': purpose},
                )
                result['candidates'].append(
                    {
                        'position': position,
                        'variant': variant,
                        'generation_id': gid,
                        'questions': made,
                        'dropped': dropped,
                    }
                )
        return result

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
        asked = review.get('要求难度')
        levels = {'基础': 0, '中等': 1, '较难': 2}
        if asked in levels and out.difficulty != asked:
            flags.append(
                f'难度偏{"高" if levels[out.difficulty] > levels[asked] else "低"}：要求{asked}，评估为{out.difficulty}'
            )
        flags += [f'题目问题：{x}' for x in out.issues if x.strip()][:4]
        return {'difficulty': out.difficulty, 'flags': flags}, meta

    def _self_check(self, p, d: QuestionDraft, review=None):
        """Another model solves the question without the key; the grader checks that answer (design doc 4 ②).
        With ``review``, a reviewer also checks objective, scope and difficulty."""
        subject = p['subject']
        checks = {'notes': [], '_metas': []}
        if review is not None and self.llm is not None:
            try:
                quality, qmeta = self._quality(p, d, review)
                checks.update(quality)
                checks['_metas'].append(qmeta)
            except ModelUnavailable as e:
                checks['notes'].append(f'题目审核失败：{e}')
        for x in d.points:
            if x.check_kind == 'numeric' and x.check_expected.strip():
                from edu_core.tools import check_numeric

                ok, _ = check_numeric(d.reference, x.check_expected)
                if ok is False:
                    checks['notes'].append(f'参考答案的最终数值与核对值 {x.check_expected} 不一致')
                    checks['reference_mismatch'] = True
        if d.type in SUGGESTED_ONLY_TYPES or self.llm is None:
            checks['solver'] = 'skipped'
            return checks
        try:
            solve_ctx = Context('self_check_solve')
            solve_ctx.add_other('question_stem', sha(d.stem))
            solved, meta = self.llm.complete(
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
            answer = solved.final_answer if d.type in KEYED_TYPES or d.type == 'fill_blank' else solved.answer
            res, grade_metas = grading.grade(
                self.llm, subject=subject, question=question, rubric=rubric, answer=answer, review=False
            )
            checks['_metas'] = [*checks['_metas'], meta, *grade_metas]
            agree = res['undetermined'] == 0 and res['total'] >= res['max_total'] - 1e-9
            checks.update(
                solver='agree' if agree else 'disagree',
                solver_answer=solved.answer[:2000],
                solver_final=solved.final_answer,
                solver_score=f'{res["total"]}/{res["max_total"]}',
                solver_model=meta.get('model_returned'),
            )
            if not agree:
                checks['notes'].append('另一个模型独立解题的结果与参考答案不一致，请核对答案')
        except ModelUnavailable as e:
            checks.update(solver='error', notes=[*checks['notes'], f'自检失败：{e}'])
        checks['answer_suspect'] = checks.get('solver') == 'disagree' or bool(checks.get('reference_mismatch'))
        return checks

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

    def choose(self, pid, group, verdict, reason='', teacher='teacher'):
        """A/B choice: A, B, same (adopt A) or neither. Unchosen candidates stay as research records."""
        if verdict not in ('A', 'B', 'same', 'neither'):
            raise ValueError('选择应为 A、B、same（差不多）或 neither（都不合适）')
        with self.store.tx() as c:
            gens = {
                r['position']: r['id']
                for r in c.execute(
                    'SELECT id, position FROM generations WHERE project_id=? AND candidate_group=?', (pid, group)
                )
            }
            if not gens:
                raise KeyError('候选组不存在')
            if c.execute('SELECT 1 FROM choices WHERE candidate_group=?', (group,)).fetchone():
                raise Conflict('这一组已经选择过了')
            chosen = {'A': 'A', 'B': 'B', 'same': 'A'}.get(verdict)
            c.execute(
                'INSERT INTO choices VALUES(?,?,?,?,?,?,?,?)',
                (new_id('cho'), pid, group, verdict, chosen, reason, teacher, time.time()),
            )
            promoted = []
            if chosen:
                rows = c.execute(
                    "SELECT id, type FROM artifacts WHERE generation_id=? AND status='candidate' ORDER BY created_at",
                    (gens[chosen],),
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
            event(c, pid, 'ab_choice', {'group': group, 'verdict': verdict, 'reason': reason})
            return {'verdict': verdict, 'promoted': promoted}

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
            goals = self._goals(self._plan(c, pid))
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
                    'total': len(mine),
                    'confirmed': sum(1 for q in mine if q['confirmed_version']),
                    'flagged': flagged,
                }
            )
        loose = [q['content']['no'] for q in qs if not q['content'].get('objective_ids') and q['status'] != 'candidate']
        return {'round': round_, 'objectives': rows, 'unassigned': loose}

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

    def regenerate_question(self, qid, instruction='', difficulty=None):
        """换一道: a new question for the same objective and type, saved as the next version of this question."""
        llm = self._model()
        with self.store.read() as c:
            q = A.get(c, qid)
            if q['type'] != 'question':
                raise ValueError('只能对题目操作')
            pid = q['project_id']
            p = self._project(c, pid)
            plan = self._plan(c, pid)
            objs = self._confirmed_objectives(c, pid, q['content'].get('objective_ids') or None) if plan else []
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
                ctx.add_artifact(plan)
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
