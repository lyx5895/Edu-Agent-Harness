"""AI 提示 for teacher inputs: draft one field from what the project already has.

One short call, no artifact is written: the text goes back to the input box and the teacher edits it before
saving. Each field has its own label and length so the prompt asks for the right thing (a scope, a profile,
a remark), not a generic paragraph.
"""

from __future__ import annotations

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
}


class SuggestionsMixin:
    """AI 提示 on the input boxes of备课 and出题."""

    @operation('suggest_field')
    def suggest_field(self, pid, field, current=''):
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
