"""Per-task context whitelists (design doc 5.2).

Prompt assembly may only read artifacts through a :class:`Context`. It enforces which artifact types a
task may see, and by default only confirmed versions. The manifest it collects is stored with the
generation and is also what the UI shows as "已参考", so the note always matches what the model saw.
"""

from edu_core.artifacts import TYPES

WHITELIST = {
    'generate_questions': {
        'project', 'material', 'lesson_plan', 'inferred_objectives', 'curriculum', 'carryover', 'class_status',
    },
    'import_paper': {'project', 'material'},
    'infer_objectives': {'project', 'material', 'curriculum'},
    'pick_standards': {'project', 'curriculum'},
    # 教案审核: the draft just written, before anyone confirms it (it is reviewed, not passed on)
    'review_plan': {'project', 'plan_draft'},
    'transcribe': {'project', 'question_stem', 'photo'},
    'self_check_solve': {'question_stem'},
    'review_question': {'question_stem', 'lesson_plan', 'curriculum'},
    'grade': {'question', 'rubric', 'submission'},
    'review': {'question', 'rubric', 'submission'},
    'analyze': {'project', 'lesson_plan', 'question', 'rubric', 'grading', 'submission', 'problem_pattern'},
    'pitfalls': {'project', 'lesson_plan', 'question', 'rubric'},
    'practice': {'project', 'lesson_plan', 'question', 'rubric', 'problem_pattern', 'review_plan'},
    'draft_lesson_plan': {'project', 'material', 'carryover', 'class_status', 'curriculum'},
    # 直接准备讲解 (design doc 5.5): the question and one optional student answer the teacher pasted, nothing else.
    'explain': {'project', 'material', 'pasted'},
    # 单元讲评 (design doc 5.6): several assignments of one class; their patterns, pinned questions, confirmed analyses.
    'unit_review': {'project', 'assignments'},
    # 局部修改 (design doc 4.3): the plan being revised and the teacher's instruction.
    'revise_plan': {'project', 'lesson_plan'},
    'draft_review': {'project', 'lesson_plan', 'question', 'rubric', 'analysis', 'problem_pattern'},
}

LABELS = {
    **TYPES,
    'project': '项目背景',
    'material': '材料',
    'question_stem': '题干',
    'photo': '作答照片',
    'carryover': '上一课时学情',
    'class_status': '本班学情',
    'pasted': '老师粘贴的题目与作答',
    'assignments': '所选的几份作业',
    'curriculum': '课程标准',
    'inferred_objectives': '由要求反推的教学目标（未确认）',
    'plan_draft': '本次生成的备课方案草稿',
}


class ContextViolation(PermissionError):
    """A task tried to read an artifact type outside its whitelist."""


class Context:
    def __init__(self, task: str):
        if task not in WHITELIST:
            raise ContextViolation(f'未声明白名单的任务：{task}')
        self.task = task
        self.manifest: list[dict] = []

    def allow(self, kind: str):
        if kind not in WHITELIST[self.task]:
            raise ContextViolation(f'任务 {self.task} 不允许读取 {kind}')

    def add_artifact(self, artifact: dict, *, require_confirmed=True, view=None):
        """Register an artifact the prompt uses; returns ``view`` (or its content) for the prompt."""
        self.allow(artifact['type'])
        if require_confirmed and artifact['status'] != 'confirmed':
            raise ContextViolation(
                f'{LABELS[artifact["type"]]}（第 {artifact["version"]} 版）尚未确认，不能自动传给下一步'
            )
        self.manifest.append(
            {
                'kind': artifact['type'],
                'id': artifact['id'],
                'lineage_id': artifact['lineage_id'],
                'version': artifact['version'],
                'sha': artifact['content_sha'],
                'status': artifact['status'],
            }
        )
        return artifact['content'] if view is None else view

    def add_other(self, kind: str, ident: str, sha: str = '', **extra):
        self.allow(kind)
        self.manifest.append({'kind': kind, 'id': ident, 'sha': sha, **extra})

    def refs(self, role_by_kind=None):
        """(artifact_id, role) pairs for artifact_refs."""
        role_by_kind = role_by_kind or {}
        return [(m['id'], role_by_kind.get(m['kind'], m['kind'])) for m in self.manifest if m['kind'] in TYPES]


def describe(manifest: list[dict]) -> str:
    """Human-readable "已参考" note generated from a manifest."""
    if not manifest:
        return '已参考：无'
    groups: dict[str, list[dict]] = {}
    for m in manifest:
        groups.setdefault(m['kind'], []).append(m)
    parts = []
    for kind, items in groups.items():
        label = LABELS.get(kind, kind)
        if kind in ('project', 'carryover', 'pasted', 'assignments', 'inferred_objectives'):
            parts.append(label)
        elif kind == 'curriculum':
            parts.append(f'{label} {sum(len(i.get("items", [])) for i in items)} 条')
        elif kind == 'material':
            off = sum(1 for i in items if i.get('excluded'))
            focused = sum(1 for i in items if 'focus' in i)
            cut = sum(1 for i in items if 0 < i.get('sent', i.get('chars', 0)) < i.get('selected', i.get('chars', 0)))
            unsent = sum(1 for i in items if 'sent' in i and i['sent'] == 0 and not i.get('excluded'))
            notes = []
            if off:
                notes.append(f'{off} 份已排除')
            if focused:
                notes.append(f'{focused} 份只发送指定章节')
            if cut:
                notes.append(f'{cut} 份截断')
            if unsent:
                notes.append(f'{unsent} 份未发送（超出长度）')
            parts.append(f'{label} {len(items)} 份' + (f'（{"，".join(notes)}）' if notes else ''))
        elif kind == 'class_status':
            parts.append(label)
        elif len(items) == 1 and 'version' in items[0]:
            state = '已确认' if items[0].get('status') == 'confirmed' else '未确认'
            parts.append(f'{label}第 {items[0]["version"]} 版（{state}）')
        else:
            confirmed = sum(1 for i in items if i.get('status') == 'confirmed')
            parts.append(f'{label} {len(items)} 份' + (f'（{confirmed} 份已确认）' if confirmed else ''))
    return '已参考：' + '、'.join(parts)
