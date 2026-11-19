"""01 备课设计: draft the lesson plan against the curriculum standard."""

from __future__ import annotations

import json

from edu_core import artifacts as A
from edu_core import curriculum as CUR
from edu_core.context import Context, describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import record_generation
from edu_core.text import sha
from edu_core.workflow.common import (
    Conflict,
    _json,
    normalize_plan,
)
from edu_core.workflow.prompts import (
    LESSON_PLAN_SYSTEM,
)
from edu_core.workflow.schemas import (
    LessonPlanOutput,
)


class LessonPlanMixin:
    """01 备课设计."""

    # ----- 01 lesson plan (重点难点与课堂活动) -----

    def draft_lesson_plan(self, pid):
        """备课方案: objectives with success criteria, key points, difficulties and timed activities, confirmed as one."""
        llm = self._model()
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
            carry = None
            if p.get('carryover'):
                carry = json.loads(p['carryover'])
                ctx.add_other('carryover', carry['from_project'], sha(p['carryover']))
        extra = {}
        if carry:
            extra['上一课时学情'] = {
                '上一课时': carry['from_lesson'] or carry['from_title'],
                '上一课时目标': carry['objectives'],
                '学情概述': carry['summary'],
                '问题模式': [
                    {'名称': x['name'], '描述': x['description'], '涉及人数': x['affected'], '再练情况': x['after_practice']}
                    for x in carry['patterns']
                ],
                '讲评重点': carry['review_focus'],
            }
        if std:
            extra['课程标准'] = {
                '来源': CUR.load(p['subject'])['title'],
                '内容要求': [
                    {'编号': it['id'], '位置': it['label'] + ('（选学，不作为考试要求）' if it.get('optional') else ''), '原文': it['text']}
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
        content = normalize_plan(
            {
                'objectives': objectives,
                'curriculum': {'ids': sorted(allowed_ids)} if std else None,
                'key_points': out.key_points,
                'difficulties': out.difficulties,
                'activities': [
                    {
                        **a.model_dump(exclude={'objective_index'}),
                        'objective_ids': [f'O{a.objective_index + 1}']
                        if 0 <= a.objective_index < len(out.objectives)
                        else [],
                    }
                    for a in out.activities
                ],
                'notes': out.notes,
            }
        )
        CUR.plan_checks(p['subject'], content['objectives'], used=bool(std))
        with self.store.tx() as c:
            gid = record_generation(c, pid, meta, ctx.manifest, 'lesson-plan-v4')
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

    def lesson_plans(self, pid):
        with self.store.read() as c:
            return A.current(c, pid, 'lesson_plan')
