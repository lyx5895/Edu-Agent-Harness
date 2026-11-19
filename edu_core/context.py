"""Per-task context whitelists (design doc 5.2).

Prompt assembly may only read artifacts through a :class:`Context`. It enforces which artifact types a
task may see, and by default only confirmed versions. The manifest it collects is stored with the
generation and is also what the UI shows as "已参考", so the note always matches what the model saw.
"""

from edu_core.artifacts import TYPES

WHITELIST = {
    'generate_questions': {'project', 'material', 'lesson_plan', 'curriculum'},
    'import_paper': {'project', 'material'},
    'self_check_solve': {'question_stem'},
    'grade': {'question', 'rubric', 'submission'},
    'review': {'question', 'rubric', 'submission'},
    'analyze': {'project', 'lesson_plan', 'question', 'rubric', 'grading', 'submission', 'problem_pattern'},
    'pitfalls': {'project', 'lesson_plan', 'question', 'rubric'},
    'practice': {'project', 'lesson_plan', 'question', 'rubric', 'problem_pattern', 'review_plan'},
    'draft_lesson_plan': {'project', 'material', 'carryover', 'curriculum'},
    'draft_review': {'project', 'lesson_plan', 'question', 'rubric', 'analysis', 'problem_pattern'},
}

LABELS = {**TYPES, 'project': '项目背景', 'material': '材料', 'question_stem': '题干', 'carryover': '上一课时学情', 'curriculum': '课程标准'}


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
        if kind in ('project', 'carryover'):
            parts.append(label)
        elif kind == 'curriculum':
            parts.append(f'{label} {sum(len(i.get("items", [])) for i in items)} 条')
        elif kind == 'material':
            parts.append(f'{label} {len(items)} 份')
        elif len(items) == 1 and 'version' in items[0]:
            state = '已确认' if items[0].get('status') == 'confirmed' else '未确认'
            parts.append(f'{label}第 {items[0]["version"]} 版（{state}）')
        else:
            confirmed = sum(1 for i in items if i.get('status') == 'confirmed')
            parts.append(f'{label} {len(items)} 份' + (f'（{confirmed} 份已确认）' if confirmed else ''))
    return '已参考：' + '、'.join(parts)
