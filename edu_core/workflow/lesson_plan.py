"""01 备课设计: draft the lesson plan against the curriculum standard."""

from __future__ import annotations

import json

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core.context import Context, describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import record_generation
from edu_core.text import sha
from edu_core import progress
from edu_core.workflow.common import (
    Conflict,
    _json,
    evidence_checks,
    normalize_plan,
)
from edu_core.workflow.prompts import (
    LESSON_PLAN_SYSTEM,
    PLAN_CHECK_SYSTEM,
    REVISE_SYSTEM,
)
from edu_core.workflow.runs import operation
from edu_core.workflow.schemas import (
    LessonPlanOutput,
    PlanReview,
    RevisedPart,
)


# A plan with worked examples, written by a reasoning model, ran to ~16k tokens; start with room instead of a retry.
LONG_REPLY = 12000


def plan_for_teacher(content):
    """The plan as a teacher sees it (目标 1, 活动 2, 例题 1, Chinese headings), for the reviewer: given the stored
    structure, it wrote its remarks with objective_ids, exit_check and O3."""
    no = {o['id']: f'目标 {i}' for i, o in enumerate(content['objectives'], 1)}

    def goals(x):
        return '、'.join(no[i] for i in x.get('objective_ids', []) if i in no)

    return {
        '教学目标': [{'编号': no[o['id']], '目标': o['text'], '怎样算达到': o.get('success_criterion', '')} for o in content['objectives']],
        '重点': content.get('key_points', []),
        '难点': content.get('difficulties', []),
        '课堂活动': [
            {'编号': f'活动 {i}', '名称': a['name'], '用时': f'{a.get("minutes", 0)} 分钟', '做法': a.get('description', ''),
             '对应': goals(a), '检验': a.get('evidence', '')}
            for i, a in enumerate(content.get('activities', []), 1)
        ],
        '例题': [{'编号': f'例题 {i}', '对应': goals(x), '题目': x['problem'], '解答': x.get('solution', '')}
                for i, x in enumerate(content.get('examples', []), 1)],
        '当堂检测': [{'编号': f'当堂检测第 {i} 题', '对应': goals(x), '题目': x['question'], '答案': x.get('answer', '')}
                 for i, x in enumerate(content.get('exit_check', []), 1)],
        '备注': content.get('notes', ''),
    }


class LessonPlanMixin:
    """01 备课设计."""

    # ----- 01 lesson plan (重点难点与课堂活动) -----

    @operation('draft_lesson_plan')
    def draft_lesson_plan(self, pid):
        """备课方案: objectives with success criteria, key points, difficulties and timed activities, confirmed as one."""
        llm = self._model()
        self._confirm_standards(pid, llm)  # before reading the project: the plan uses the confirmed pick
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('draft_lesson_plan')
            ctx.add_other('project', pid)
            materials = self._material_context(c, ctx, pid, limit=8000)
            if not (p.get('knowledge_module') or materials):
                raise Conflict('请先填写课时信息中的知识模块，或添加参考材料')
            prior = self._plan(c, pid, confirmed=False)
        with self.store.tx() as c:
            std = self._curriculum_for_plan(c, p)
        with self.store.read() as c:
            if std:
                ctx.add_other(
                    'curriculum',
                    p['subject'],
                    sha(_json([it['id'] for it in std['content']])),
                    items=[it['id'] for it in std['content']],
                )
            carry = self._prior_status(c, ctx, p)
        extra = {}
        if carry:
            prior_key = '上一课时' if carry['label'] == '上一课时学情' else '班级与进度'
            extra[carry['label']] = {
                prior_key: carry['from_lesson'] or carry['from_title'],
                '上一课时目标' if prior_key == '上一课时' else '近期目标': carry['objectives'],
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
                '讲评重点': carry['review_focus'],
                **({'说明': carry['note']} if carry.get('note') else {}),
            }
        if std:
            extra['课程标准'] = {
                '来源': CUR.load(p['subject'])['title'],
                '内容要求': [
                    {
                        '编号': it['id'],
                        '位置': it['label'] + ('（选学，不作为考试要求）' if it.get('optional') else ''),
                        '原文': it['text'],
                    }
                    for it in std['content']
                ],
                '学业要求': [it['text'] for it in std['requirements']],
                '教学提示': [it['text'] for it in std['hints']],
            }
        user = _json(
            {
                '学科': p['pack']['name'],
                '年级': p['grade'],
                '知识模块': p.get('knowledge_module', ''),
                '本课时内容': p.get('lesson_scope', ''),
                '课时': p.get('lesson', ''),
                '学生基础': p.get('learner_profile', ''),
                '老师说明': p['notes'],
                '项目名称': p['title'],
                '参考材料': materials,
                **extra,
            }
        )
        out, meta = llm.complete(
            'generator',
            'draft_lesson_plan',
            LESSON_PLAN_SYSTEM.format(subject=p['pack']['name']),
            user,
            LessonPlanOutput,
            max_tokens=LONG_REPLY,
        )
        if not out.objectives:
            raise ModelUnavailable('模型没有给出教学目标，请重试')
        allowed_ids = {it['id'] for it in std['content']} if std else set()
        objectives = []
        for o in out.objectives:
            d = o.model_dump()
            d['standard_ids'] = [i for i in d['standard_ids'] if i in allowed_ids]  # invented ids are dropped
            if not d.pop('beyond_standard'):
                d['beyond_reason'] = ''
            objectives.append(d)
        def goal(i):  # the model numbers objectives from 0; the plan keeps O1, O2, ...
            return [f'O{i + 1}'] if 0 <= i < len(out.objectives) else []

        content = normalize_plan(
            {
                'objectives': objectives,
                'curriculum': {'ids': sorted(allowed_ids)} if std else None,
                'key_points': out.key_points,
                'difficulties': out.difficulties,
                'activities': [
                    {**a.model_dump(exclude={'objective_index'}), 'objective_ids': goal(a.objective_index)}
                    for a in out.activities
                ],
                'examples': [
                    {**x.model_dump(exclude={'objective_index'}), 'objective_ids': goal(x.objective_index)}
                    for x in out.examples
                ],
                'exit_check': [
                    {**x.model_dump(exclude={'objective_index'}), 'objective_ids': goal(x.objective_index)}
                    for x in out.exit_check
                ],
                'notes': out.notes,
            }
        )
        CUR.plan_checks(p['subject'], content['objectives'], used=bool(std))
        evidence_checks(content)
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'lesson-plan-v5')
            plan = A.create(
                c,
                pid,
                'lesson_plan',
                content,
                origin='generated',
                refs=ctx.refs(),
                generation_id=gid,
                lineage_id=prior['lineage_id'] if prior else None,
            )
        return {'lesson_plan': plan, 'context_note': describe(ctx.manifest)}

    @operation('check_plan', lambda self, args, kwargs: _plan_project(self, args[0] if args else kwargs['aid']))
    def check_plan(self, aid):
        """AI 帮我看看: asked for by the teacher while giving revision remarks, never run on its own. The teacher
        reads the plan first; remarks the teacher did not ask for, at the top of every plan, were in the way.
        Returns at most three must-fix remarks written for the teacher (目标 1, 活动 2, 例题 1); nothing is stored
        in the plan, the call is recorded like any other."""
        with self.store.read() as c:
            plan = A.get(c, aid)
            if plan['type'] != 'lesson_plan':
                raise ValueError('只能看备课方案')
            p = self._project(c, plan['project_id'])
        ids = (plan['content'].get('curriculum') or {}).get('ids') or []
        std = CUR.context(p['subject'], ids, self._curriculum_query(p)) if ids else None
        review, meta, manifest = self._check_plan(p, plan['content'], std)
        if meta:
            with self.store.tx() as c:
                record_generation(c, p['id'], meta, manifest, 'plan-check-v1')
        return review

    def _check_plan(self, p, content, std):
        """Another provider reads the plan for knowledge errors, wrong example solutions, steps that check nothing
        and contradictions. A failed call says so instead of looking like a clean plan."""
        progress.step('AI 正在看教案')
        ctx = Context('check_plan')
        ctx.add_other('project', p['id'])
        ctx.add_other('plan_draft', p['id'], sha(_json(content)))
        try:
            out, meta = self.llm.complete(
                'reviewer',
                'check_plan',
                PLAN_CHECK_SYSTEM.format(subject=p['pack']['name']),
                _json(
                    {
                        '课时信息': {
                            '知识模块': p.get('knowledge_module', ''),
                            '课时': p.get('lesson', ''),
                            '本课时内容': p.get('lesson_scope', ''),
                        },
                        '学生基础': p.get('learner_profile', ''),
                        '课标条目': [{'编号': it['id'], '原文': it['text']} for it in (std or {}).get('content', [])],
                        '备课方案': plan_for_teacher(content),
                    }
                ),
                PlanReview,
            )
        except ModelUnavailable as e:
            return {'issues': [], 'error': f'这次没有看成：{e}'[:200]}, None, ctx.manifest
        review = {'issues': [x for x in out.issues if x.strip()][:3]}
        return review, meta, ctx.manifest

    def lesson_plans(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'lesson_plan')

    @operation('revise_plan', lambda self, args, kwargs: _plan_project(self, args[0] if args else kwargs['aid']))
    def revise_plan_part(self, aid, target, instruction):
        """局部修改 (design doc 4.3): “缩短导入活动”“改第 2 条目标”只改指定部分. The server keeps the rest of the plan
        byte for byte, saves a full new draft version and records what changed and why.

        ``target``: ``objective:O1``, ``activity:<n>``, ``example:<n>``, ``check:<n>`` (n from 1), ``key_points``,
        ``difficulties`` or ``notes``.
        """
        instruction = (instruction or '').strip()
        if not instruction:
            raise ValueError('请写明要怎样修改')
        llm = self._model()
        with self.store.read() as c:
            plan = A.get(c, aid)
            if plan['type'] != 'lesson_plan':
                raise ValueError('局部修改目前只用于备课方案')
            if plan['status'] == 'superseded':
                raise Conflict('该版本已被取代，请在最新版本上修改')
            p = self._project(c, plan['project_id'])
            content = json.loads(json.dumps(plan['content']))
            kind, _, key = target.partition(':')
            if kind == 'objective':
                part = next((o for o in content['objectives'] if o['id'] == key), None)
            elif kind == 'activity' and key.isdigit() and 1 <= int(key) <= len(content['activities']):
                part = content['activities'][int(key) - 1]
            elif kind in ('example', 'check') and key.isdigit():
                rows = content.get('examples' if kind == 'example' else 'exit_check', [])
                part = rows[int(key) - 1] if 1 <= int(key) <= len(rows) else None
            elif kind in ('key_points', 'difficulties', 'notes'):
                part = content.get(kind)
            else:
                part = None
            if part is None:
                raise ValueError(f'找不到要修改的部分：{target}')
            ctx = Context('revise_plan')
            ctx.add_other('project', p['id'])
            ctx.add_artifact(plan, require_confirmed=False)
        user = _json(
            {
                '学科': p['pack']['name'],
                '整份方案': plan_for_teacher(content),  # as the teacher sees it, so no field names come back
                '要修改的部分': {'位置': _part_label(content, kind, key), '当前内容': part},
                '老师的要求': instruction,
            }
        )
        out, meta = llm.complete(
            'generator', 'revise_plan', REVISE_SYSTEM.format(subject=p['pack']['name']), user, RevisedPart
        )
        if kind == 'objective':
            part.update(
                text=out.text.strip() or part['text'],
                success_criterion=out.success_criterion.strip() or part.get('success_criterion', ''),
            )
        elif kind == 'activity':
            part.update(
                name=out.name.strip() or part['name'],
                minutes=out.minutes or part.get('minutes', 0),
                description=out.description.strip() or part.get('description', ''),
            )
        elif kind == 'example':
            part.update(problem=out.question.strip() or part['problem'], solution=out.answer.strip() or part.get('solution', ''))
        elif kind == 'check':
            part.update(question=out.question.strip() or part['question'], answer=out.answer.strip() or part.get('answer', ''))
        elif kind == 'notes':
            content['notes'] = out.text.strip()
        else:
            content[kind] = [x for x in out.items if x.strip()] or content[kind]
        revised = normalize_plan(content)
        # The stored plan carries the curriculum checks; recompute them before comparing so only real edits count.
        CUR.plan_checks(p['subject'], revised['objectives'], used=bool(revised.get('curriculum')))
        evidence_checks(revised)
        changed = sorted(
            k for k in set(revised) | set(plan['content']) if revised.get(k) != plan['content'].get(k)
        )
        if not changed:
            raise ModelUnavailable('模型没有做出修改，请换个说法再试')
        with self.store.tx() as c:
            gid = record_generation(c, p['id'], meta, ctx.manifest, 'revise-plan-v1')
            new = A.create(
                c,
                p['id'],
                'lesson_plan',
                {
                    **revised,
                    # label: how the teacher names the part (目标 1), shown instead of the internal objective:O1
                    'revision': {'target': target, 'label': _part_label(content, kind, key), 'instruction': instruction, 'changed': changed},
                },
                origin='revised',
                refs=[(r['ref_id'], r['role']) for r in plan['refs']],
                generation_id=gid,
                lineage_id=plan['lineage_id'],
                edit_of=aid,
                created_by='model',
            )
            self._guard_basis(c, p['id'], new['id'], ctx.manifest)
        return {'lesson_plan': new, 'changed': changed, 'context_note': describe(ctx.manifest)}


def _part_label(content, kind, key):
    """How the teacher names the part being revised: 目标 2, 活动 3, 例题 1, 当堂检测第 1 题, 重点."""
    if kind == 'objective':
        n = next((i for i, o in enumerate(content['objectives'], 1) if o['id'] == key), 0)
        return f'目标 {n}'
    return {'activity': f'活动 {key}', 'example': f'例题 {key}', 'check': f'当堂检测第 {key} 题'}.get(
        kind, {'key_points': '重点', 'difficulties': '难点', 'notes': '备注'}.get(kind, kind)
    )


def _plan_project(self, aid):
    with self.store.read() as c:
        r = c.execute('SELECT project_id FROM artifacts WHERE id=?', (aid,)).fetchone()
    return r[0] if r else ''
