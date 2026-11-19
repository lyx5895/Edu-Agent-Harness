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
    REVISE_SYSTEM,
)
from edu_core.workflow.runs import operation
from edu_core.workflow.schemas import (
    LessonPlanOutput,
    RevisedPart,
)


class LessonPlanMixin:
    """01 备课设计."""

    # ----- 01 lesson plan (重点难点与课堂活动) -----

    @operation('draft_lesson_plan')
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

    @operation('revise_plan', lambda self, args, kwargs: _plan_project(self, args[0] if args else kwargs['aid']))
    def revise_plan_part(self, aid, target, instruction):
        """局部修改 (design doc 4.3): “缩短导入活动”“改第 2 条目标”只改指定部分. The server keeps the rest of the plan
        byte for byte, saves a full new draft version and records what changed and why.

        ``target``: ``objective:O1``, ``activity:<序号从 1 开始>``, ``key_points``, ``difficulties`` or ``notes``.
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
                '整份方案': content,
                '要修改的部分': {'位置': target, '当前内容': part},
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
        elif kind == 'notes':
            content['notes'] = out.text.strip()
        else:
            content[kind] = [x for x in out.items if x.strip()] or content[kind]
        revised = normalize_plan(content)
        # The stored plan carries the curriculum checks; recompute them before comparing so only real edits count.
        CUR.plan_checks(p['subject'], revised['objectives'], used=bool(revised.get('curriculum')))
        changed = sorted(k for k in set(revised) | set(plan['content']) if revised.get(k) != plan['content'].get(k))
        if not changed:
            raise ModelUnavailable('模型没有做出修改，请换个说法再试')
        with self.store.tx() as c:
            gid = record_generation(c, p['id'], meta, ctx.manifest, 'revise-plan-v1')
            new = A.create(
                c,
                p['id'],
                'lesson_plan',
                {**revised, 'revision': {'target': target, 'instruction': instruction, 'changed': changed}},
                origin='revised',
                refs=[(r['ref_id'], r['role']) for r in plan['refs']],
                generation_id=gid,
                lineage_id=plan['lineage_id'],
                edit_of=aid,
                created_by='model',
            )
            self._guard_basis(c, p['id'], new['id'], ctx.manifest)
        return {'lesson_plan': new, 'changed': changed, 'context_note': describe(ctx.manifest)}


def _plan_project(self, aid):
    with self.store.read() as c:
        r = c.execute('SELECT project_id FROM artifacts WHERE id=?', (aid,)).fetchone()
    return r[0] if r else ''
