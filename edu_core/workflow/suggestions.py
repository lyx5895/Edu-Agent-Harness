"""AI 提示 for teacher inputs: draft one field from what the project already has.

One short call, no artifact is written: the text goes back to the input box and the teacher edits it before
saving. Each field has its own label and length so the prompt asks for the right thing (a scope, a profile,
a remark), not a generic paragraph.
"""

from __future__ import annotations

import re

from edu_core.context import Context, describe
from edu_core.llm import ModelUnavailable
from edu_core.storage import record_generation
from edu_core.workflow.common import _json
from edu_core.workflow.prompts import SUGGEST_SYSTEM
from edu_core.workflow.runs import operation
from edu_core.workflow.schemas import FieldSuggestion

# field id (the input's data-suggest) -> (what the teacher calls it, max characters)
SUGGEST_FIELDS = {
    'lesson_scope': ('本课时内容', 120),
    'learner_profile': ('学生基础', 100),
    'course_notes': ('其他要求', 60),
    'q_topic': ('这次练习的主题或范围', 60),
    'q_notes': ('出题说明', 60),
    'revise_instruction': ('修改意见', 80),
    'remark': ('修改意见', 80),
    'regen_note': ('换一道的其他要求', 60),
    'nl_topic': ('这节课的主题', 40),
    'nl_no': ('课时', 20),
}


class SuggestionsMixin:
    """AI 提示 on the input boxes of备课 and出题."""

    @operation('suggest_field')
    def suggest_field(self, pid, field, current='', about=''):
        """Draft ``field`` from the project's course info, objectives and materials. The teacher decides whether to
        use it; nothing is saved here."""
        spec = SUGGEST_FIELDS.get(field)
        if spec is None:
            raise ValueError('这个输入框还不支持 AI 提示')
        label, max_chars = spec
        llm = self._model()
        with self.store.read() as c:
            p = self._project(c, pid)
            ctx = Context('suggest_field')
            ctx.add_other('project', pid)
            materials = self._material_context(c, ctx, pid, limit=2000)
            plan = self._working_plan(c, pid)
            objectives = [o['content']['text'] for o in self._goals(plan)] if plan else []
        out, meta = llm.complete(
            'generator',
            'suggest_field',
            SUGGEST_SYSTEM.format(subject=p['pack']['name'], field=label, max_chars=max_chars),
            _json(
                {
                    '学科': p['pack']['name'],
                    '年级': p['grade'],
                    '知识模块': p.get('knowledge_module', ''),
                    '课时': p.get('lesson', ''),
                    '本课时内容': p.get('lesson_scope', ''),
                    '学生基础': p.get('learner_profile', ''),
                    '其他要求': p.get('notes', ''),
                    '教学目标': objectives,
                    '材料摘录': materials,
                    '当前内容': (current or '').strip()[:1000],
                    '针对的内容': (about or '').strip()[:1500],
                }
            ),
            FieldSuggestion,
            max_tokens=800,
        )
        text = out.text.strip()[:max_chars]
        if not text:
            raise ModelUnavailable('模型没有给出可用的提示，请重试')
        with self.store.tx() as c:
            record_generation(c, pid, meta, ctx.manifest, 'suggest-v1')
        return {'field': field, 'text': text, 'context_note': describe(ctx.manifest)}

    @operation('suggest_field')
    def suggest_course_field(self, course_id, field='nl_topic', current=''):
        """AI 提示 on the home page's 新的一节课, before any project exists: draft the topic from the course's own
        materials and the lessons already taught, so a new lesson does not repeat the last one."""
        if field == 'nl_no':
            # Deterministic, no model: the next number after the lessons this course already has.
            with self.store.read() as c:
                self._course(c, course_id)
                rows = [r[0] for r in c.execute('SELECT lesson FROM projects WHERE course_id=?', (course_id,))]
            numbers = [int(m.group(1)) for x in rows if (m := re.search(r'第\s*(\d+)\s*课时', x or ''))]
            n = max(numbers) + 1 if numbers else len(rows) + 1
            return {
                'field': field,
                'text': f'第 {n} 课时',
                'context_note': f'已参考：课程（已有 {len(rows)} 节课）',
            }
        if field != 'nl_topic':
            raise ValueError('这个输入框还不支持 AI 提示')
        llm = self._model()
        with self.store.read() as c:
            course = self._course(c, course_id)
            mats = [
                dict(r)
                for r in c.execute(
                    'SELECT id, filename, sha256, text FROM materials WHERE course_id=? ORDER BY created_at DESC LIMIT 5',
                    (course_id,),
                )
            ]
            taught = [
                r[0]
                for r in c.execute(
                    "SELECT knowledge_module FROM projects WHERE course_id=? AND knowledge_module!='' "
                    'ORDER BY created_at DESC LIMIT 8',
                    (course_id,),
                )
            ]
            ctx = Context('suggest_field')
            ctx.add_other('course', course_id)
            excerpt, used = [], 0
            for m in mats:
                piece = (m['text'] or '')[: max(0, 2000 - used)]
                used += len(piece)
                ctx.add_other('material', m['id'], m['sha256'], filename=m['filename'], chars=len(m['text']), sent=len(piece))
                if piece:
                    excerpt.append(f'{m["filename"]}：{piece}')
        out, meta = llm.complete(
            'generator',
            'suggest_field',
            SUGGEST_SYSTEM.format(subject=course['subject_name'], field='这节课的主题', max_chars=40),
            _json(
                {
                    '学科': course['subject_name'],
                    '年级': course['grade'],
                    '课程名称': course['title'],
                    '教材': course.get('textbook', ''),
                    '课程说明': course.get('notes', ''),
                    '已经上过的主题': taught,
                    '材料摘录': '\n'.join(excerpt),
                    '当前内容': (current or '').strip()[:200],
                    '针对的内容': '',
                }
            ),
            FieldSuggestion,
            max_tokens=600,
        )
        text = out.text.strip()[:40]
        if not text:
            raise ModelUnavailable('模型没有给出可用的提示，请重试')
        with self.store.tx() as c:
            record_generation(c, course_id, meta, ctx.manifest, 'suggest-v1')
        return {'field': field, 'text': text, 'context_note': describe(ctx.manifest)}
